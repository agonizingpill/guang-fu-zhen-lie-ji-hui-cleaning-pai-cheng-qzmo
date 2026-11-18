"""验证 v3 旧库 -> v4 的幂等结构升级（SQLite 模拟）。"""
import os, sys, types, datetime
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = '/workspace/backend'
sys.path.insert(0, ROOT)
os.environ['DATABASE_URL'] = 'sqlite://'; os.environ['SEED_DEMO'] = 'false'

import src.database as database
te = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
database.engine = te
database.Session = sessionmaker(bind=te, expire_on_commit=False)
fake = types.SimpleNamespace(ping=lambda: True, rpush=lambda *a, **k: 1)
sys.modules.setdefault('redis', types.SimpleNamespace(Redis=types.SimpleNamespace(from_url=lambda *a, **k: fake)))

# 1. 用 v3 旧结构手工建表（work_permits.mission_id 唯一、无 v4 列）
with te.begin() as conn:
    conn.execute(text("""CREATE TABLE wash_crews (
        id INTEGER PRIMARY KEY, code VARCHAR(40) UNIQUE,
        max_daily_minutes INTEGER NOT NULL, active BOOLEAN DEFAULT 1)"""))
    conn.execute(text("""CREATE TABLE wash_missions (
        id INTEGER PRIMARY KEY, survey_id INTEGER UNIQUE, water_liters FLOAT,
        available_liters FLOAT, status VARCHAR(24) DEFAULT 'draft',
        post_kw FLOAT, post_irradiance FLOAT, normalized_kw FLOAT, verified_gain FLOAT,
        dispatched_at DATETIME, accepted_at DATETIME)"""))
    conn.execute(text("""CREATE TABLE work_permits (
        id INTEGER PRIMARY KEY, mission_id INTEGER UNIQUE, crew_id INTEGER,
        work_date DATE, minutes INTEGER, wind_mps FLOAT, isolation BOOLEAN,
        state VARCHAR(24) DEFAULT 'draft', signed_by VARCHAR(120) DEFAULT '')"""))
    conn.execute(text("INSERT INTO wash_crews (code,max_daily_minutes,active) VALUES ('OLD-CREW',480,1)"))
    conn.execute(text("INSERT INTO wash_missions (survey_id,water_liters,available_liters,status) VALUES (1,100,100,'draft')"))
    conn.execute(text("INSERT INTO work_permits (mission_id,crew_id,work_date,minutes,wind_mps,isolation,state) VALUES (1,1,'2026-11-18',120,4,1,'draft')"))

# 2. 触发 metadata 建表 + 升级
from src.database import Base
import src.models, src.operations_models, src.execution_models, src.revisit_models  # noqa
from src.schema_upgrade import upgrade
Base.metadata.create_all(te)
upgrade(); upgrade()  # 第二次验证幂等

insp = inspect(te)
permit_cols = {c['name'] for c in insp.get_columns('work_permits')}
mission_cols = {c['name'] for c in insp.get_columns('wash_missions')}
assert {'kind', 'parent_id', 'predicted_minutes', 'prediction_evidence'} <= permit_cols, permit_cols
assert 'execution_state' in mission_cols
# work_segments 已建
assert 'work_segments' in insp.get_table_names()

# 3. 旧数据 kind 默认 initial；同任务多许可的写入由新建库（test_http）覆盖。
#    SQLite 的自动唯一索引不可 DROP，生产 MySQL 下 uq_work_permits_mission 会被 schema_upgrade 正常删除。
with te.begin() as conn:
    assert conn.execute(text("SELECT kind FROM work_permits WHERE id=1")).scalar() == 'initial'
    assert conn.execute(text("SELECT execution_state FROM wash_missions WHERE id=1")).scalar() == 'active'
print('旧库升级验证通过：', sorted(permit_cols), '| segments 表存在 | 旧行默认 initial / active')

# 4. 新建库（等同生产 create_all 后的形态）允许同任务多张许可
te2 = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
database.engine = te2; database.Session = sessionmaker(bind=te2, expire_on_commit=False)
Base.metadata.create_all(te2)
from src.operations_models import WashCrew, WorkPermit
from src.models import WashMission
with database.Session() as db:
    crew = WashCrew(code='C', max_daily_minutes=480)
    mission = WashMission(survey_id=9, water_liters=100, available_liters=100, status='draft')
    db.add_all([crew, mission]); db.flush()
    d1 = datetime.date(2026, 11, 18); d2 = datetime.date(2026, 11, 19)
    db.add(WorkPermit(mission_id=mission.id, crew_id=crew.id, work_date=d1,
                      minutes=120, wind_mps=4, isolation=True, state='used', kind='initial'))
    db.flush()
    db.add(WorkPermit(mission_id=mission.id, crew_id=crew.id, work_date=d2,
                      minutes=80, wind_mps=3, isolation=True, state='pending',
                      kind='continuation', parent_id=1))
    db.commit()
    n = db.query(WorkPermit).filter_by(mission_id=mission.id).count()
    assert n == 2
print('新建库同任务初始+续作双许可验证通过')
