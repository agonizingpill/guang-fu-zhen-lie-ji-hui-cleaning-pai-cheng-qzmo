import random
from datetime import datetime, timedelta, date
from sqlalchemy import select, func
from src.database import Base, engine, Session
from src.models import User, ArrayBlock, SoilingSurvey, WashMission
from src.revisit_models import WaterDelivery, FollowupSurvey
from src.auth import hash_password
from src.operations_models import WashCrew
from src.execution_models import WorkSegment

rng = random.Random(20261118)


def seed():
    Base.metadata.create_all(engine)
    with Session() as db:
        for role in ['admin', 'planner', 'viewer']:
            email = role + '@qingling.test'
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, password=hash_password('Solar2026!'), role=role))
        for code, minutes in [('CREW-SOUTH', 480), ('CREW-RIDGE', 360)]:
            if not db.scalar(select(WashCrew).where(WashCrew.code == code)):
                db.add(WashCrew(code=code, max_daily_minutes=minutes))
        blocks = [('SL-A01', '南坡一号', '南坡', 150, 23, 780, 1500),
                  ('SL-A02', '南坡二号', '南坡', 120, 25, 630, 1200),
                  ('SL-B01', '脊线一号', '脊线', 100, 18, 520, 900),
                  ('SL-C01', '西坡一号', '西坡', 80, 30, 420, 600)]
        for code, name, sector, cap, slope, area, water in blocks:
            if not db.scalar(select(ArrayBlock).where(ArrayBlock.code == code)):
                db.add(ArrayBlock(code=code, name=name, sector=sector, capacity_kw=cap,
                                  slope=slope, area_m2=area, water_liters=water))
        db.flush()
        rows = db.scalars(select(ArrayBlock).order_by(ArrayBlock.id)).all()
        measurements = [('SV-1117-A01', rows[0], 800, 35, 18, 3.2, 88),
                        ('SV-1117-A02', rows[1], 820, 33, 12, 2.1, 76),
                        ('SV-CLOUD-01', rows[2], 240, 23, 8, 1.2, 18)]
        for code, block, irr, temp, rain, dust, power in measurements:
            if not db.scalar(select(SoilingSurvey).where(SoilingSurvey.code == code)):
                db.add(SoilingSurvey(code=code, block_id=block.id, capacity_kw=block.capacity_kw,
                                     slope=block.slope, irradiance=irr, module_temp=temp,
                                     rain_days=rain, dust=dust, observed_kw=power, state='recorded'))
        db.flush()
        block = db.scalar(select(ArrayBlock).where(ArrayBlock.code == 'SL-C01'))
        if not db.scalar(select(WaterDelivery).where(WaterDelivery.delivery_code == 'WD-DEMO-01')):
            db.add(WaterDelivery(block_id=block.id, delivery_code='WD-DEMO-01', liters=500,
                                 source='西坡蓄水池 / 水车02'))
        historical = db.scalar(select(SoilingSurvey).where(SoilingSurvey.code == 'SV-DEMO-CLEAN'))
        if not historical:
            historical = SoilingSurvey(code='SV-DEMO-CLEAN', block_id=block.id,
                                       capacity_kw=block.capacity_kw, slope=block.slope,
                                       irradiance=800, module_temp=35, rain_days=15, dust=2,
                                       observed_kw=50, clean_kw=62, predicted_loss=12,
                                       recommended_water=100, state='closed')
            db.add(historical); db.flush()
        mission = db.scalar(select(WashMission).where(WashMission.survey_id == historical.id))
        if not mission:
            mission = WashMission(survey_id=historical.id, water_liters=100, available_liters=700,
                                  status='accepted', post_kw=62, post_irradiance=800,
                                  normalized_kw=62, verified_gain=12,
                                  dispatched_at=datetime.utcnow() - timedelta(days=7),
                                  accepted_at=datetime.utcnow() - timedelta(days=7))
            db.add(mission); db.flush()
        if not db.scalar(select(FollowupSurvey).where(
                FollowupSurvey.mission_id == mission.id, FollowupSurvey.days_after == 2)):
            db.add(FollowupSurvey(mission_id=mission.id, days_after=2, irradiance=800,
                                  observed_kw=61, normalized_kw=61, normalized_loss=100 / 62,
                                  state='stable',
                                  baseline_snapshot={'post_kw': 62, 'post_irradiance': 800,
                                                     'accepted_at': mission.accepted_at.isoformat(),
                                                     'block_id': block.id, 'block_code': block.code,
                                                     'survey_id': historical.id,
                                                     'survey_code': historical.code,
                                                     'threshold_pct': 5}))
        # 既有已验收任务补一段实耗，保证其进入工时训练集。
        if not db.scalar(select(WorkSegment).where(WorkSegment.mission_id == mission.id)):
            crew = db.scalar(select(WashCrew).where(WashCrew.code == 'CREW-RIDGE'))
            db.add(WorkSegment(segment_code='SEG-DEMO-000', mission_id=mission.id,
                               permit_id=None, crew_id=crew.id,
                               work_date=(mission.accepted_at - timedelta(days=1)).date(),
                               actual_minutes=140, water_used_liters=96.0, reporter='seed',
                               note='历史任务补录分段实耗'))
        _seed_duration_training(db, rows)
        db.commit()
        print({key: db.scalar(select(func.count()).select_from(model))
               for key, model in [('users', User), ('blocks', ArrayBlock),
                                  ('surveys', SoilingSurvey)]})


def _seed_duration_training(db, blocks):
    """18 条已完工任务+分段实耗，供剩余工时随机森林训练（独立留出集报 MAE）。"""
    crews = db.scalars(select(WashCrew).order_by(WashCrew.id)).all()
    base_day = date.today() - timedelta(days=60)
    for i in range(18):
        code = f'SV-TRAIN-{i + 1:02d}'
        if db.scalar(select(SoilingSurvey).where(SoilingSurvey.code == code)):
            continue
        block = blocks[i % len(blocks)]
        day = base_day + timedelta(days=i * 2)
        survey = SoilingSurvey(
            code=code, block_id=block.id, capacity_kw=block.capacity_kw, slope=block.slope,
            irradiance=round(rng.uniform(600, 980), 1), module_temp=round(rng.uniform(24, 48), 1),
            rain_days=rng.randint(5, 25), dust=round(rng.uniform(1.0, 4.5), 1),
            observed_kw=round(block.capacity_kw * 0.62, 2), clean_kw=round(block.capacity_kw * 0.8, 2),
            predicted_loss=round(block.capacity_kw * 0.18, 2),
            recommended_water=round(block.area_m2 * 0.6, 1), state='closed')
        db.add(survey); db.flush()
        water = round(block.area_m2 * 0.6 * (1 + 0.1 * survey.dust), 1)
        # 总工时与面积/水量/积灰正相关，叠加噪声；跨日任务拆 1~2 段。
        total = int(round(60 + block.area_m2 * 0.16 + water * 0.12 + survey.dust * 12
                          + rng.gauss(0, 14)))
        total = max(total, 70)
        done = day + timedelta(days=1)
        m = WashMission(survey_id=survey.id, water_liters=water, available_liters=block.water_liters,
                        status='accepted', post_kw=round(block.capacity_kw * 0.8, 2),
                        post_irradiance=survey.irradiance,
                        normalized_kw=round(block.capacity_kw * 0.8, 2),
                        verified_gain=round(block.capacity_kw * 0.18, 2),
                        dispatched_at=datetime.combine(day, datetime.min.time()),
                        accepted_at=datetime.combine(done, datetime.min.time()))
        db.add(m); db.flush()
        crew = crews[i % len(crews)]
        split = rng.random() < 0.45
        used_water = round(water * rng.uniform(0.9, 0.99), 1)
        if split:
            first = int(round(total * rng.uniform(0.45, 0.65)))
            db.add(WorkSegment(segment_code=f'SEG-TRAIN-{i + 1:02d}-A', mission_id=m.id,
                               permit_id=None, crew_id=crew.id, work_date=day,
                               actual_minutes=first, water_used_liters=round(used_water * 0.6, 1),
                               reporter='seed', note='训练样本：跨日首段'))
            db.add(WorkSegment(segment_code=f'SEG-TRAIN-{i + 1:02d}-B', mission_id=m.id,
                               permit_id=None, crew_id=crews[(i + 1) % len(crews)].id,
                               work_date=done, actual_minutes=total - first,
                               water_used_liters=round(used_water - used_water * 0.6, 1),
                               reporter='seed', note='训练样本：次日续段'))
        else:
            db.add(WorkSegment(segment_code=f'SEG-TRAIN-{i + 1:02d}-A', mission_id=m.id,
                               permit_id=None, crew_id=crew.id, work_date=day,
                               actual_minutes=total, water_used_liters=used_water,
                               reporter='seed', note='训练样本：当日完成'))


if __name__ == '__main__':
    seed()
