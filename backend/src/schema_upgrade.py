"""v4 幂等结构升级：项目无在线迁移工具，启动时在 create_all 后补齐。

- work_permits 去掉 mission_id 唯一约束（同一任务跨日可有多张续作许可），补 v4 列
- wash_missions 补 execution_state
- work_segments 由 metadata 新建
"""
from sqlalchemy import text
from .database import engine


def _columns(conn, table):
    if engine.dialect.name == 'sqlite':
        rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
        return {r[1] for r in rows}
    rows = conn.execute(text(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {'t': table}).all()
    return {r[0] for r in rows}


def _indexes(conn, table):
    if engine.dialect.name == 'sqlite':
        rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
        return {r[1] for r in rows}
    rows = conn.execute(text(
        "SELECT INDEX_NAME FROM information_schema.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t"), {'t': table}).all()
    return {r[0] for r in rows}


_PERMIT_COLUMNS = {
    'kind': "VARCHAR(24) NOT NULL DEFAULT 'initial'",
    'parent_id': 'INT NULL',
    'predicted_minutes': 'INT NULL',
    'prediction_evidence': 'JSON NULL',
}


def upgrade():
    with engine.begin() as conn:
        existing = _columns(conn, 'work_permits')
        for name, ddl in _PERMIT_COLUMNS.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE work_permits ADD COLUMN {name} {ddl}"))
        # 旧库 mission_id 带唯一索引；新模型允许同任务多张许可。
        for index_name in _indexes(conn, 'work_permits'):
            if index_name.startswith('uq_work_permits_mission') or index_name == 'mission_id':
                conn.execute(text(f"DROP INDEX {index_name} ON work_permits"))
        mission_cols = _columns(conn, 'wash_missions')
        if 'execution_state' not in mission_cols:
            conn.execute(text(
                "ALTER TABLE wash_missions ADD COLUMN execution_state VARCHAR(24) "
                "NOT NULL DEFAULT 'active'"))
        if 'ix_work_permits_mission_id' not in _indexes(conn, 'work_permits'):
            conn.execute(text("CREATE INDEX ix_work_permits_mission_id ON work_permits (mission_id)"))
