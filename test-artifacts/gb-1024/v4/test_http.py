"""HTTP 级冒烟：TestClient + SQLite，验证路由、鉴权与 seed 幂等。"""
import os
import sys
import types

ROOT = '/workspace/backend'
sys.path.insert(0, ROOT)
os.environ['DATABASE_URL'] = 'sqlite://'
os.environ['SEED_DEMO'] = 'false'
os.environ['JWT_SECRET'] = 'test-secret'

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import src.database as database

test_engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                            poolclass=StaticPool)
database.engine = test_engine
database.Session = sessionmaker(bind=test_engine, expire_on_commit=False)

fake_redis = types.SimpleNamespace(ping=lambda: True, rpush=lambda *a, **k: 1)
sys.modules.setdefault('redis', types.SimpleNamespace(
    Redis=types.SimpleNamespace(from_url=lambda *a, **k: fake_redis)))

from fastapi.testclient import TestClient  # noqa: E402
from src.app import app  # noqa: E402
from src.schema_upgrade import upgrade  # noqa: E402
from src.database import Base, Session  # noqa: E402

Base.metadata.create_all(test_engine)
upgrade()

# 跑两次 seed 验证幂等
sys.path.insert(0, ROOT)
from scripts.seed_demo import seed  # noqa: E402
seed(); seed()

client = TestClient(app)


def login(role):
    r = client.post('/auth/login', json={'email': f'{role}@qingling.test',
                                         'password': 'Solar2026!'})
    assert r.status_code == 200, r.text
    return r.json()['token']


def hdr(tok):
    return {'Authorization': f'Bearer {tok}'}


admin = login('admin')
planner = login('planner')
viewer = login('viewer')

# 1. 分段与预测接口
r = client.get('/segments', headers=hdr(viewer))
assert r.status_code == 200
segments_before = len(r.json())
print('seed segments:', segments_before)
assert segments_before >= 19  # 18 训练 + 1 历史

r = client.get('/missions', headers=hdr(admin))
missions = r.json()
train_mission = next(m for m in missions if m['status'] == 'accepted')
r = client.get(f"/missions/{train_mission['id']}/duration-prediction", headers=hdr(admin))
assert r.status_code == 200, r.text
ev = r.json()
print('prediction evidence keys:', sorted(ev.keys()))
assert ev['manual_review'] is False, ev
assert 'holdout_mae_minutes' in ev and 'holdout_mae_pct' in ev
print(f"holdout MAE: {ev['holdout_mae_minutes']:.2f} 分钟 / {ev['holdout_mae_pct']:.2f}%")

# 2. 跑完整业务剧本：草稿任务 -> 许可 -> 派工 -> 分段 -> 暂停 -> 续作 -> 恢复 -> 复测 -> 验收
with Session() as db:
    from src.models import ArrayBlock, SoilingSurvey
    blk = db.query(ArrayBlock).first()
    sv = SoilingSurvey(code='SV-E2E-01', block_id=blk.id, capacity_kw=blk.capacity_kw,
                       slope=blk.slope, irradiance=800, module_temp=35, rain_days=12,
                       dust=2.6, observed_kw=72, clean_kw=92, predicted_loss=20,
                       recommended_water=300, state='assessed')
    db.add(sv); db.commit()
    survey_id = sv.id
r = client.post('/missions', headers=hdr(planner),
                json={'survey_id': survey_id, 'water_liters': 300})
assert r.status_code == 200, r.text
mission = r.json()
mid = mission['id']

crews = client.get('/crews', headers=hdr(admin)).json()
import datetime
today = datetime.date.today().isoformat()
tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
r = client.post('/permits', headers=hdr(planner), json={
    'mission_id': mid, 'crew_id': crews[0]['id'], 'work_date': today,
    'minutes': 240, 'wind_mps': 4.2, 'isolation': True})
assert r.status_code == 200, r.text
permit = r.json()
pid = permit['id']
r = client.post(f'/permits/{pid}/sign', headers=hdr(admin))
assert r.status_code == 200, r.text
r = client.post(f'/missions/{mid}/dispatch', headers=hdr(admin))
assert r.status_code == 200, r.text
dispatched = r.json()
assert dispatched['execution_state'] == 'active'

r = client.post('/segments', headers=hdr(planner), json={
    'permit_id': pid, 'work_date': today, 'actual_minutes': 150,
    'water_used_liters': 180, 'segment_code': f'SEG-E2E-{mid}', 'note': '首日作业'})
assert r.status_code == 200, r.text
assert r.json()['over_capacity'] is False

# 观察员不能登记
r = client.post('/segments', headers=hdr(viewer), json={
    'permit_id': pid, 'work_date': tomorrow, 'actual_minutes': 30,
    'water_used_liters': 20, 'segment_code': 'SEG-DENY'})
assert r.status_code == 403

r = client.post(f'/missions/{mid}/pause', headers=hdr(planner),
                json={'reason': '收工跨日，明日换班'})
assert r.status_code == 200, r.text
paused = r.json()
print('pause release:', paused['released_minutes'], 'consumed:', paused['consumed_minutes'])
assert paused['released_minutes'] == 90

# 暂停态复测被拒
r = client.post(f'/missions/{mid}/retest', headers=hdr(planner),
                json={'post_kw': 100, 'post_irradiance': 800})
assert r.status_code == 409

# 续作申请（换班组）
r = client.post('/continuations', headers=hdr(planner), json={
    'mission_id': mid, 'crew_id': crews[1]['id'], 'work_date': tomorrow,
    'wind_mps': 3.8, 'isolation': True})
assert r.status_code == 200, r.text
cont = r.json()
cpid = cont['permit']['id']
print('continuation:', {k: cont[k] for k in ('capacity_available', 'manual_review')},
      'minutes:', cont['permit']['minutes'])
assert cont['permit']['state'] == 'pending'
assert cont['permit']['kind'] == 'continuation'

# 计划员不能承接
r = client.post(f'/continuations/{cpid}/approve', headers=hdr(planner))
assert r.status_code == 403
r = client.post(f'/continuations/{cpid}/approve', headers=hdr(admin))
assert r.status_code == 200, r.text
# 幂等重试：已非 pending 再承接被拒，不重复占额
r = client.post(f'/continuations/{cpid}/approve', headers=hdr(admin))
assert r.status_code == 409
r = client.post(f'/continuations/{cpid}/resume', headers=hdr(planner))
assert r.status_code == 200, r.text

# 次日分段（新班组）
r = client.post('/segments', headers=hdr(planner), json={
    'permit_id': cpid, 'work_date': tomorrow, 'actual_minutes': 70,
    'water_used_liters': 100, 'segment_code': f'SEG-E2E-{mid}-B', 'note': '次日续作'})
assert r.status_code == 200, r.text

# 复测 + 验收
r = client.post(f'/missions/{mid}/retest', headers=hdr(planner),
                json={'post_kw': 105, 'post_irradiance': 800})
assert r.status_code == 200, r.text
r = client.post(f'/missions/{mid}/accept', headers=hdr(admin))
assert r.status_code == 200, r.text

# 汇总
segments = client.get('/segments', headers=hdr(viewer)).json()
mine = [s for s in segments if s['mission_id'] == mid]
assert len(mine) == 2
total_min = sum(s['actual_minutes'] for s in mine)
total_water = sum(s['water_used_liters'] for s in mine)
print(f'E2E mission {mid}: 分段2段 总工时{total_min}分钟 总用水{total_water}L')
assert total_min == 220 and total_water == 280

# 许可状态：初始 used、续作 used；同任务两张许可共存
permits = client.get('/permits', headers=hdr(viewer)).json()
mine_permits = [p for p in permits if p['mission_id'] == mid]
assert len(mine_permits) == 2
assert {p['state'] for p in mine_permits} == {'used'}

print('HTTP E2E OK')
