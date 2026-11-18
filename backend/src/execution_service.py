"""跨日分段执行：分段实耗、暂停保留、续作换班、容量重算与超额阻签。

加锁顺序固定 Mission -> Permit -> Crew，避免并发死锁；READ COMMITTED 下行锁内重算占用。
一段作业只认一方：每许可每日唯一分段，续作待接单(pending)不占容量，重试不重复占额。
"""
from sqlalchemy import select, func
from fastapi import HTTPException
from .models import WashMission, SoilingSurvey, ArrayBlock
from .operations_models import WashCrew, WorkPermit
from .execution_models import WorkSegment
from .field_service import require, audit
from . import duration_model


# ---------- 汇总口径 ----------
def mission_totals(db, mission_id):
    minutes = int(db.scalar(select(func.coalesce(func.sum(WorkSegment.actual_minutes), 0))
                            .where(WorkSegment.mission_id == mission_id)))
    water = float(db.scalar(select(func.coalesce(func.sum(WorkSegment.water_used_liters), 0))
                            .where(WorkSegment.mission_id == mission_id)))
    return minutes, round(water, 3)


def _permit_used(db, permit_id):
    return int(db.scalar(select(func.coalesce(func.sum(WorkSegment.actual_minutes), 0))
                         .where(WorkSegment.permit_id == permit_id)))


def crew_capacity_view(db, crew_id, day):
    """同一把行锁内同时算预留占用与实耗，超额以实耗为准。"""
    reserved = int(db.scalar(
        select(func.coalesce(func.sum(WorkPermit.minutes), 0)).where(
            WorkPermit.crew_id == crew_id, WorkPermit.work_date == day,
            WorkPermit.state.in_(['signed', 'used']))))
    actual = int(db.scalar(
        select(func.coalesce(func.sum(WorkSegment.actual_minutes), 0)).where(
            WorkSegment.crew_id == crew_id, WorkSegment.work_date == day)))
    paused_quota = int(db.scalar(
        select(func.coalesce(func.sum(WorkPermit.minutes), 0)).where(
            WorkPermit.crew_id == crew_id, WorkPermit.work_date == day,
            WorkPermit.state == 'paused')))
    return reserved, actual, paused_quota


def _assert_capacity(db, crew, day, extra_reserved=0):
    """新签/承接前校验：实耗已超容量直接阻止新签，预留+新增也不得超容量。"""
    reserved, actual, _ = crew_capacity_view(db, crew.id, day)
    if actual > crew.max_daily_minutes:
        raise HTTPException(409, f'班组 {crew.code} {day} 实耗{actual}分钟已超日容量'
                                 f'{crew.max_daily_minutes}分钟，超额阻止新签许可')
    if reserved + extra_reserved > crew.max_daily_minutes:
        raise HTTPException(409, f'班组 {crew.code} {day} 工时容量不足：'
                                 f'预留{reserved}+新增{extra_reserved}>{crew.max_daily_minutes}分钟')


# ---------- 分段实耗登记 ----------
def report_segment(db, body, user):
    permit = require(db, WorkPermit, body.permit_id, True)
    mission = require(db, WashMission, permit.mission_id, True)
    crew = require(db, WashCrew, permit.crew_id, True)
    if mission.status != 'dispatched':
        raise HTTPException(409, '仅执行中的清洗任务可登记分段实耗')
    if permit.state not in ('used', 'paused'):
        raise HTTPException(409, '仅已派工许可（含暂停后补报）可登记分段实耗')
    if db.scalar(select(WorkSegment).where(WorkSegment.segment_code == body.segment_code)):
        raise HTTPException(409, '分段单号已存在，重复提交被拒绝')
    if db.scalar(select(WorkSegment).where(
            WorkSegment.permit_id == permit.id, WorkSegment.work_date == body.work_date)):
        raise HTTPException(409, '该许可当日已有分段，一段作业只认一方，不得重复登记')
    # 同一任务同一作业日只认一个班次：换班并发时不允许两方重复认账。
    if db.scalar(select(WorkSegment).where(
            WorkSegment.mission_id == mission.id,
            WorkSegment.work_date == body.work_date,
            WorkSegment.permit_id != permit.id)):
        raise HTTPException(409, '该任务当日已有另一班次分段，一段作业只认一方，禁止重复认账')
    # 已有续作许可在作业时，旧暂停许可不再承接新分段。
    if permit.state == 'paused' and db.scalar(select(WorkPermit.id).where(
            WorkPermit.mission_id == mission.id, WorkPermit.state == 'used',
            WorkPermit.id != permit.id)):
        raise HTTPException(409, '该许可已暂停并由续作班组承接，新分段请登记到续作许可')
    used_minutes, used_water = mission_totals(db, mission.id)
    if used_water + body.water_used_liters > mission.water_liters + 1e-6:
        raise HTTPException(409, f'累计用水将达 {round(used_water + body.water_used_liters, 1)}L，'
                                 f'超过任务计划 {mission.water_liters}L')
    segment = WorkSegment(
        segment_code=body.segment_code, mission_id=mission.id, permit_id=permit.id,
        crew_id=crew.id, work_date=body.work_date, actual_minutes=body.actual_minutes,
        water_used_liters=body.water_used_liters, note=body.note, reporter=user.email)
    db.add(segment); db.flush()
    # 迟报实耗仍归原班次：以原班组原日期重新核算可用工时。
    reserved, actual, _ = crew_capacity_view(db, crew.id, body.work_date)
    over = actual > crew.max_daily_minutes
    warning = ''
    if over:
        warning = (f'实耗{actual}分钟超出 {crew.code} 当日容量{crew.max_daily_minutes}分钟，'
                   '已阻止该班组同日新签许可')
    audit(db, user, f'登记分段 {body.segment_code} 许可{permit.id} '
                    f'{body.actual_minutes}分钟/{body.water_used_liters}L'
                    + ('；' + warning if warning else ''))
    db.commit()
    return {'segment': segment, 'over_capacity': over, 'warning': warning,
            'crew_actual_minutes': actual, 'crew_reserved_minutes': reserved}


# ---------- 暂停：保留已耗，释放未耗 ----------
def pause(db, key, body, user):
    mission = require(db, WashMission, key, True)
    if mission.status != 'dispatched' or mission.execution_state != 'active':
        raise HTTPException(409, '仅执行中的任务可暂停，已暂停任务请勿重复操作')
    permit = require(db, WorkPermit,
                     db.scalar(select(WorkPermit.id).where(
                         WorkPermit.mission_id == mission.id,
                         WorkPermit.state == 'used').order_by(WorkPermit.id.desc())), True)
    crew = require(db, WashCrew, permit.crew_id, True)
    used = _permit_used(db, permit.id)
    permit.state = 'paused'
    mission.execution_state = 'paused'
    released = max(permit.minutes - used, 0)
    audit(db, user, f'暂停任务 {key} 许可{permit.id}，保留实耗{used}分钟，'
                    f'原班组 {crew.code} 释放未耗工时{released}分钟；原因：{body.reason}')
    db.commit()
    return {'permit': permit, 'released_minutes': released, 'consumed_minutes': used}


# ---------- 续作申请：容量不足转待接单且不扣水 ----------
def request_continuation(db, body, user):
    mission = require(db, WashMission, body.mission_id, True)
    if mission.status != 'dispatched' or mission.execution_state != 'paused':
        raise HTTPException(409, '仅暂停待接单的任务可申请续作')
    crew = require(db, WashCrew, body.crew_id, True)
    if not crew.active:
        raise HTTPException(409, '班组已停用')
    if db.scalar(select(WorkPermit).where(
            WorkPermit.mission_id == mission.id,
            WorkPermit.state.in_(['pending', 'signed']))):
        raise HTTPException(409, '该任务已有待接单/已承接续作，恢复作业后再登记新分段，重试不会重复占额')
    parent = db.scalar(select(WorkPermit).where(
        WorkPermit.mission_id == mission.id,
        WorkPermit.state == 'paused').order_by(WorkPermit.id.desc()))
    total, evidence = duration_model.predict(
        db, mission, (WorkSegment, WashMission, SoilingSurvey, ArrayBlock))
    consumed_minutes, consumed_water = mission_totals(db, mission.id)
    if total is None:
        # 样本不足转人工核定：沿用上一张许可的未耗余量承接，不输出模型预测。
        remaining_minutes = None
    else:
        remaining_minutes = max(total - consumed_minutes, 0)
    fallback_remaining = max(parent.minutes - _permit_used(db, parent.id), 0) if parent else 0
    minutes = remaining_minutes if remaining_minutes is not None else fallback_remaining
    if minutes <= 0:
        raise HTTPException(409, '没有剩余工时可承接，请核对分段实耗后人工核定')
    permit = WorkPermit(
        mission_id=mission.id, crew_id=crew.id, work_date=body.work_date, minutes=minutes,
        wind_mps=body.wind_mps, isolation=body.isolation, state='pending',
        kind='continuation', parent_id=parent.id if parent else None,
        predicted_minutes=remaining_minutes, prediction_evidence=evidence)
    db.add(permit); db.flush()
    manual = evidence.get('manual_review', False)
    # 容量试算：不足则保留待接单，不扣水、不占工时
    reserved, actual, _ = crew_capacity_view(db, crew.id, body.work_date)
    enough = (not actual > crew.max_daily_minutes
              and reserved + minutes <= crew.max_daily_minutes)
    audit(db, user, f'续作申请 许可{permit.id} 任务{mission.id} 班组{crew.code} '
                    f'承接余量{minutes}分钟' + ('（样本不足转人工核定）' if manual else '')
                    + ('；容量不足保留待接单，不扣水' if not enough else '；待主管承接审批'))
    db.commit()
    return {'permit': permit, 'capacity_available': enough, 'manual_review': manual,
            'prediction': evidence, 'consumed_water_liters': consumed_water}


# ---------- 主管承接审批：幂等，重试不重复占额 ----------
def approve_continuation(db, key, user):
    permit = require(db, WorkPermit, key, True)
    if permit.kind != 'continuation' or permit.state != 'pending':
        raise HTTPException(409, '仅待接单续作许可可承接审批')
    mission = require(db, WashMission, permit.mission_id, True)
    crew = require(db, WashCrew, permit.crew_id, True)
    if not crew.active or not permit.isolation or permit.wind_mps > 10:
        raise HTTPException(409, '班组须启用，完成电气隔离且阵风不得超过10m/s')
    # 行锁内重算容量，超额阻止承接
    _assert_capacity(db, crew, permit.work_date, permit.minutes)
    permit.state = 'signed'; permit.signed_by = user.email
    mission.execution_state = 'active'
    audit(db, user, f'主管承接续作许可{key} 班组{crew.code} 占用{permit.minutes}分钟，任务恢复作业')
    db.commit()
    return permit


def reject_continuation(db, key, user):
    permit = require(db, WorkPermit, key, True)
    if permit.state != 'pending':
        raise HTTPException(409, '仅待接单续作许可可驳回')
    permit.state = 'void'
    audit(db, user, f'驳回复作许可{key}，任务保持待接单，未扣水未占工时')
    db.commit()
    return permit


# ---------- 续作派工：signed 续作许可转 used，水量不重复扣 ----------
def resume_dispatch(db, key, user):
    permit = require(db, WorkPermit, key, True)
    if permit.kind != 'continuation' or permit.state != 'signed':
        raise HTTPException(409, '仅已承接签发的续作许可可恢复作业')
    mission = require(db, WashMission, permit.mission_id, True)
    crew = require(db, WashCrew, permit.crew_id, True)
    if mission.status != 'dispatched' or mission.execution_state != 'active':
        raise HTTPException(409, '任务状态不允许恢复作业')
    permit.state = 'used'
    audit(db, user, f'续作派工 许可{key} 班组{crew.code} 开始作业，沿用已扣水量预算不重复扣水')
    db.commit()
    return permit


def prediction_report(db, mission_id):
    mission = require(db, WashMission, mission_id)
    total, evidence = duration_model.predict(
        db, mission, (WorkSegment, WashMission, SoilingSurvey, ArrayBlock))
    return evidence
