from datetime import datetime
from sqlalchemy import select
from fastapi import HTTPException
from .models import ArrayBlock,SoilingSurvey,WashMission
from .field_service import require,audit

def prepare(db,body,user):
    survey=require(db,SoilingSurvey,body.survey_id,True)
    if survey.state!='assessed':raise HTTPException(409,'只能为已评估巡测编制清洗计划')
    if survey.predicted_loss is None or survey.predicted_loss<=0:raise HTTPException(409,'未发现积灰功率损失，不应安排清洗')
    block=require(db,ArrayBlock,survey.block_id)
    if db.scalar(select(WashMission).where(WashMission.survey_id==survey.id)):raise HTTPException(409,'该巡测已有清洗任务，请修订原计划')
    mission=WashMission(survey_id=survey.id,water_liters=body.water_liters,available_liters=block.water_liters)
    db.add(mission);audit(db,user,f'编制清洗计划 巡测{survey.id} 用水{body.water_liters}L');db.commit();return mission

def revise(db,key,body,user):
    mission=require(db,WashMission,key,True)
    if mission.status!='draft':raise HTTPException(409,'任务已派工，水量和原始巡测不得变更')
    survey=require(db,SoilingSurvey,mission.survey_id)
    block=require(db,ArrayBlock,survey.block_id)
    mission.water_liters=body.water_liters;mission.available_liters=block.water_liters
    audit(db,user,f'修订清洗任务 {key} 用水{body.water_liters}L');db.commit();return mission

def dispatch(db,key,user):
    mission=require(db,WashMission,key,True)
    if mission.status!='draft':raise HTTPException(409,'仅草稿任务可以派工')
    survey=require(db,SoilingSurvey,mission.survey_id,True)
    block=require(db,ArrayBlock,survey.block_id,True)
    if survey.state!='assessed':raise HTTPException(409,'巡测已失效或已有清洗排程')
    if block.cleaning_state=='washing':raise HTTPException(409,'同阵列已有执行中的清洗任务')
    if mission.water_liters>mission.available_liters or mission.water_liters>block.water_liters:raise HTTPException(409,'计划用水超过阵列配额/水车余量，请修订水量后派工')
    from .permit_service import dispatch_guard
    dispatch_guard(db,mission)
    # Lock the source budget and its dependent mission in one transaction.
    mission.available_liters=block.water_liters
    block.water_liters=round(block.water_liters-mission.water_liters,3);block.cleaning_state='washing'
    mission.status='dispatched';mission.execution_state='active';mission.dispatched_at=datetime.utcnow();survey.state='scheduled'
    audit(db,user,f'清洗任务 {key} 派工，扣减{mission.water_liters}L，余{block.water_liters}L');db.commit();return mission

def retest(db,key,body,user):
    mission=require(db,WashMission,key,True)
    if mission.status not in ['dispatched','retested']:raise HTTPException(409,'须先派工，已验收任务不可更改复测')
    if mission.execution_state!='active':raise HTTPException(409,'任务处于暂停待接单状态，续作恢复作业后才能登记复测')
    survey=require(db,SoilingSurvey,mission.survey_id,True)
    if body.post_kw>survey.capacity_kw*1.2:raise HTTPException(409,'复测功率超出额定容量120%，请核对读数')
    mission.post_kw=body.post_kw;mission.post_irradiance=body.post_irradiance
    mission.normalized_kw=body.post_kw*survey.irradiance/body.post_irradiance
    mission.verified_gain=mission.normalized_kw-survey.observed_kw
    mission.status='retested';survey.state='retested'
    audit(db,user,f'清洗任务 {key} 复测，归一化收益{mission.verified_gain:.3f}kW');db.commit();return mission

def accept(db,key,user):
    mission=require(db,WashMission,key,True)
    if mission.status!='retested' or mission.verified_gain is None:raise HTTPException(409,'没有清洗后复测记录，不可验收')
    if mission.verified_gain<=0:raise HTTPException(409,'复测未见正向功率收益，请排查遮挡或重新复测')
    if mission.execution_state!='active':raise HTTPException(409,'任务仍暂停待接单，不可验收')
    survey=require(db,SoilingSurvey,mission.survey_id,True);block=require(db,ArrayBlock,survey.block_id,True)
    mission.status='accepted';mission.accepted_at=datetime.utcnow();survey.state='closed'
    block.cleaning_state='ready';block.last_cleaned=mission.accepted_at
    from .operations_models import WorkPermit
    # 未消费的在办许可作废；暂停许可若已挂分段实耗，恢复 used 作为已消费凭证。
    open_permits=db.scalars(select(WorkPermit).where(WorkPermit.mission_id==mission.id,WorkPermit.state.in_(['signed','paused','pending']))).all()
    from .execution_models import WorkSegment
    for p in open_permits:
        if p.state in ('signed','pending'):p.state='void'
        else:
            has_segments=db.scalar(select(WorkSegment.id).where(WorkSegment.permit_id==p.id).limit(1))
            p.state='used' if has_segments else 'void'
    # Earlier pre-wash readings and their draft plans no longer represent the cleaned array.
    old=db.scalars(select(SoilingSurvey).where(SoilingSurvey.block_id==block.id,SoilingSurvey.id!=survey.id,SoilingSurvey.state.in_(['recorded','assessed','blocked','evaluating']))).all()
    for reading in old:
        reading.state='stale'
        draft=db.scalar(select(WashMission).where(WashMission.survey_id==reading.id,WashMission.status=='draft'))
        if draft:draft.status='superseded'
    audit(db,user,f'清洗任务 {key} 验收，实测增益{mission.verified_gain:.3f}kW');db.commit();return mission
