"""v4 跨日分段执行端到端测试：SQLite 内存库，不依赖 MySQL/Redis/Docker。

运行：.venv/bin/python -I test-artifacts/gb-1024/v4/test_execution.py
"""
import os
import sys
import unittest
from datetime import date, timedelta

ROOT = '/workspace/backend'
sys.path.insert(0, ROOT)
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['SEED_DEMO'] = 'false'
os.environ['JWT_SECRET'] = 'test-secret'

import sqlalchemy  # noqa: E402
from sqlalchemy import create_engine, select, func  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database as database  # noqa: E402

test_engine = create_engine('sqlite://', connect_args={'check_same_thread': False})
database.engine = test_engine
database.Session = sessionmaker(bind=test_engine, expire_on_commit=False)

# app 在 import 时连接 redis；用假对象顶替，避免外部依赖。
import types  # noqa: E402
fake_redis = types.SimpleNamespace(ping=lambda: True, rpush=lambda *a, **k: 1)
sys.modules.setdefault('redis', types.SimpleNamespace(
    Redis=types.SimpleNamespace(from_url=lambda *a, **k: fake_redis)))

from src.database import Base  # noqa: E402
from src import models, operations_models, execution_models, revisit_models  # noqa: E402
from src.schema_upgrade import upgrade  # noqa: E402
from src.auth import hash_password  # noqa: E402
from src.models import User, ArrayBlock, SoilingSurvey, WashMission  # noqa: E402
from src.operations_models import WashCrew, WorkPermit  # noqa: E402
from src.execution_models import WorkSegment  # noqa: E402
from src import execution_service, duration_model, permit_service, wash_service  # noqa: E402


class V4Test(unittest.TestCase):
    def setUp(self):
        Base.metadata.create_all(test_engine)
        upgrade()
        self.db = database.Session()
        self.admin = User(email='admin@qingling.test', password=hash_password('x'), role='admin')
        self.planner = User(email='planner@qingling.test',
                            password=hash_password('x'), role='planner')
        self.db.add_all([self.admin, self.planner])
        self.crew_a = WashCrew(code='CREW-A', max_daily_minutes=200)
        self.crew_b = WashCrew(code='CREW-B', max_daily_minutes=300)
        self.block = ArrayBlock(code='SL-T1', name='测试阵列', sector='南坡',
                                capacity_kw=120, slope=22, area_m2=500,
                                water_liters=2000, cleaning_state='ready')
        self.db.add_all([self.crew_a, self.crew_b, self.block])
        self.db.flush()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(test_engine)

    def _survey_mission(self, dust=2.5, water=400, code='SV-T1'):
        s = SoilingSurvey(code=code, block_id=self.block.id, capacity_kw=120, slope=22,
                          irradiance=800, module_temp=35, rain_days=10, dust=dust,
                          observed_kw=70, clean_kw=90, predicted_loss=20,
                          recommended_water=300, state='assessed')
        self.db.add(s); self.db.flush()
        m = WashMission(survey_id=s.id, water_liters=water,
                        available_liters=self.block.water_liters, status='draft')
        self.db.add(m); self.db.flush()
        return s, m

    def _dispatch(self, m, crew, minutes, day=date.today()):
        p = WorkPermit(mission_id=m.id, crew_id=crew.id, work_date=day, minutes=minutes,
                       wind_mps=4, isolation=True, state='draft', kind='initial')
        self.db.add(p); self.db.flush()
        permit_service.sign(self.db, p.id, self.admin)
        wash_service.dispatch(self.db, m.id, self.admin)
        return p

    # 1. 分段实耗登记与超水阻止
    def test_segment_and_water_cap(self):
        s, m = self._survey_mission()
        p = self._dispatch(m, self.crew_a, 180)
        from src.execution_schemas import SegmentInput
        body = SegmentInput(permit_id=p.id, work_date=date.today(), actual_minutes=100,
                            water_used_liters=300, segment_code='SEG-1', note='')
        out = execution_service.report_segment(self.db, body, self.planner)
        self.assertFalse(out['over_capacity'])
        body2 = SegmentInput(permit_id=p.id, work_date=date.today(), actual_minutes=60,
                             water_used_liters=200, segment_code='SEG-2', note='')
        # 同许可同日重复分段被拒（一段作业只认一方）
        with self.assertRaises(Exception):
            execution_service.report_segment(self.db, body2, self.planner)
        body2.work_date = date.today() + timedelta(days=1)
        with self.assertRaises(Exception):
            # 累计用水 300+200=500 > 计划 400
            execution_service.report_segment(self.db, body2, self.planner)

    # 2. 暂停保留已耗、释放未耗；容量从预留转实耗
    def test_pause_releases_unused(self):
        s, m = self._survey_mission()
        p = self._dispatch(m, self.crew_a, 180)
        from src.execution_schemas import SegmentInput, PauseInput
        execution_service.report_segment(self.db, SegmentInput(
            permit_id=p.id, work_date=date.today(), actual_minutes=110,
            water_used_liters=250, segment_code='SEG-P1'), self.planner)
        out = execution_service.pause(self.db, m.id, PauseInput(reason='收工跨日'), self.planner)
        self.assertEqual(out['released_minutes'], 70)
        self.assertEqual(out['consumed_minutes'], 110)
        self.db.refresh(p); self.db.refresh(m)
        self.assertEqual(p.state, 'paused')
        self.assertEqual(m.execution_state, 'paused')
        reserved, actual, paused_quota = execution_service.crew_capacity_view(
            self.db, self.crew_a.id, date.today())
        self.assertEqual(reserved, 0)
        self.assertEqual(actual, 110)
        self.assertEqual(paused_quota, 180)

    # 3. 续作换班：新班组承接余量；容量不足转待接单且不扣水
    def test_continuation_handoff_and_capacity(self):
        s, m = self._survey_mission()
        p = self._dispatch(m, self.crew_a, 180)
        from src.execution_schemas import SegmentInput, PauseInput, ContinuationInput
        execution_service.report_segment(self.db, SegmentInput(
            permit_id=p.id, work_date=date.today(), actual_minutes=110,
            water_used_liters=250, segment_code='SEG-H1'), self.planner)
        water_before = self.block.water_liters
        execution_service.pause(self.db, m.id, PauseInput(reason='跨日'), self.planner)
        # crew_b 容量 300：先签 260 占用（属于另一任务），剩 40；续作余量 70 -> 容量不足
        _, m_other = self._survey_mission(code='SV-OTHER', water=200)
        occ = WorkPermit(mission_id=m_other.id, crew_id=self.crew_b.id,
                         work_date=date.today() + timedelta(days=1), minutes=260,
                         wind_mps=3, isolation=True, state='signed', kind='initial')
        self.db.add(occ); self.db.flush()
        out = execution_service.request_continuation(self.db, ContinuationInput(
            mission_id=m.id, crew_id=self.crew_b.id,
            work_date=date.today() + timedelta(days=1), wind_mps=4, isolation=True), self.planner)
        self.assertFalse(out['capacity_available'])
        new_permit = out['permit']
        self.assertEqual(new_permit.state, 'pending')
        self.assertEqual(self.block.water_liters, water_before, '待接单不扣水')
        # 重复申请被拒（重试不重复占额）
        with self.assertRaises(Exception):
            execution_service.request_continuation(self.db, ContinuationInput(
                mission_id=m.id, crew_id=self.crew_b.id,
                work_date=date.today() + timedelta(days=1), wind_mps=4, isolation=True),
                self.planner)
        # 腾出容量后主管承接，幂等重试
        occ.state = 'void'; self.db.flush()
        execution_service.approve_continuation(self.db, new_permit.id, self.admin)
        self.db.refresh(new_permit); self.db.refresh(m)
        self.assertEqual(new_permit.state, 'signed')
        self.assertEqual(m.execution_state, 'active')
        execution_service.resume_dispatch(self.db, new_permit.id, self.planner)
        self.db.refresh(new_permit)
        self.assertEqual(new_permit.state, 'used')
        self.assertEqual(self.block.water_liters, water_before, '续作不重复扣水')

    # 4. 迟报实耗归原班次，超额阻止新签
    def test_late_report_over_capacity_blocks_sign(self):
        s, m = self._survey_mission()
        p = self._dispatch(m, self.crew_a, 180)
        from src.execution_schemas import SegmentInput
        out = execution_service.report_segment(self.db, SegmentInput(
            permit_id=p.id, work_date=date.today(), actual_minutes=205,
            water_used_liters=200, segment_code='SEG-LATE'), self.planner)
        self.assertTrue(out['over_capacity'])
        self.assertEqual(out['crew_actual_minutes'], 205)
        # 再给该班组同日签新许可必须被阻止
        s2, m2 = self._survey_mission(code='SV-T2', water=200)
        draft = WorkPermit(mission_id=m2.id, crew_id=self.crew_a.id, work_date=date.today(),
                           minutes=30, wind_mps=4, isolation=True, state='draft')
        self.db.add(draft); self.db.flush()
        with self.assertRaises(Exception):
            permit_service.sign(self.db, draft.id, self.admin)

    # 5. 暂停态阻止复测，恢复后放行
    def test_paused_blocks_retest(self):
        s, m = self._survey_mission()
        p = self._dispatch(m, self.crew_a, 180)
        from src.execution_schemas import SegmentInput, PauseInput
        execution_service.report_segment(self.db, SegmentInput(
            permit_id=p.id, work_date=date.today(), actual_minutes=120,
            water_used_liters=300, segment_code='SEG-R1'), self.planner)
        execution_service.pause(self.db, m.id, PauseInput(reason='跨日'), self.planner)
        from src.schemas import RetestInput
        with self.assertRaises(Exception):
            wash_service.retest(self.db, m.id, RetestInput(post_kw=100, post_irradiance=800),
                                self.planner)

    # 6. 预测：样本不足转人工；够样本后独立留出 MAE
    def test_prediction_manual_then_trained(self):
        # 无样本
        s, m = self._survey_mission()
        ev = execution_service.prediction_report(self.db, m.id)
        self.assertTrue(ev['manual_review'])
        self.assertNotIn('holdout_mae_minutes', ev)
        # 造 12 条已完工且带分段的任务
        for i in range(12):
            si, mi = self._survey_mission(code=f'SV-TR-{i}', water=300 + i * 5)
            mi.status = 'accepted'
            crew = [self.crew_a, self.crew_b][i % 2]
            total = 100 + i * 4
            self.db.add(WorkSegment(segment_code=f'SEG-TR-{i}', mission_id=mi.id, permit_id=None,
                                    crew_id=crew.id, work_date=date.today(),
                                    actual_minutes=total, water_used_liters=280, reporter='t'))
        self.db.flush()
        ev = execution_service.prediction_report(self.db, m.id)
        self.assertFalse(ev['manual_review'])
        self.assertIn('holdout_mae_minutes', ev)
        self.assertGreaterEqual(ev['train_rows'], duration_model.MIN_TRAIN_SAMPLES)
        self.assertGreater(ev['holdout_rows'], 0)
        self.assertGreater(ev['holdout_mae_minutes'], 0)


    # 7. 一段作业只认一方：续作恢复后旧暂停许可不得再认账；同日禁止两班重复
    def test_single_party_segment_guard(self):
        from src.execution_schemas import (SegmentInput, PauseInput,
                                           ContinuationInput)
        s, m = self._survey_mission(code='SV-SP', water=350)
        p = self._dispatch(m, self.crew_a, 180)
        execution_service.report_segment(self.db, SegmentInput(
            permit_id=p.id, work_date=date.today(), actual_minutes=100,
            water_used_liters=180, segment_code='SEG-SP-1'), self.planner)
        execution_service.pause(self.db, m.id, PauseInput(reason='跨日'), self.planner)
        out = execution_service.request_continuation(self.db, ContinuationInput(
            mission_id=m.id, crew_id=self.crew_b.id,
            work_date=date.today() + timedelta(days=1), wind_mps=4,
            isolation=True), self.planner)
        cpid = out['permit'].id
        execution_service.approve_continuation(self.db, cpid, self.admin)
        execution_service.resume_dispatch(self.db, cpid, self.planner)
        # 旧 paused 许可在续作用许可存在后停止接账
        with self.assertRaises(Exception):
            execution_service.report_segment(self.db, SegmentInput(
                permit_id=p.id, work_date=date.today() + timedelta(days=2),
                actual_minutes=30, water_used_liters=20,
                segment_code='SEG-SP-OLD'), self.planner)
        # 续作许可正常登记
        execution_service.report_segment(self.db, SegmentInput(
            permit_id=cpid, work_date=date.today() + timedelta(days=1),
            actual_minutes=60, water_used_liters=120,
            segment_code='SEG-SP-NEW'), self.planner)
        # 同一作业日另一许可（旧许可）再报，被同日只认一方拦截
        with self.assertRaises(Exception):
            execution_service.report_segment(self.db, SegmentInput(
                permit_id=p.id, work_date=date.today() + timedelta(days=1),
                actual_minutes=20, water_used_liters=10,
                segment_code='SEG-SP-DUP'), self.planner)


if __name__ == '__main__':
    unittest.main(verbosity=2)
