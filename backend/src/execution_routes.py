from fastapi import APIRouter, Depends
from sqlalchemy import select
from .database import session
from .auth import current, editor, admin
from .execution_models import WorkSegment
from .execution_schemas import SegmentInput, PauseInput, ContinuationInput
from . import execution_service

router = APIRouter()


@router.get('/segments')
def segments(u=Depends(current), db=Depends(session)):
    return db.scalars(select(WorkSegment).order_by(WorkSegment.id.desc())).all()


@router.post('/segments')
def report_segment(b: SegmentInput, u=Depends(editor), db=Depends(session)):
    return execution_service.report_segment(db, b, u)


@router.post('/missions/{key}/pause')
def pause(key: int, b: PauseInput, u=Depends(editor), db=Depends(session)):
    return execution_service.pause(db, key, b, u)


@router.post('/continuations')
def request_continuation(b: ContinuationInput, u=Depends(editor), db=Depends(session)):
    return execution_service.request_continuation(db, b, u)


@router.post('/continuations/{key}/approve')
def approve(key: int, u=Depends(admin), db=Depends(session)):
    return execution_service.approve_continuation(db, key, u)


@router.post('/continuations/{key}/reject')
def reject(key: int, u=Depends(admin), db=Depends(session)):
    return execution_service.reject_continuation(db, key, u)


@router.post('/continuations/{key}/resume')
def resume(key: int, u=Depends(editor), db=Depends(session)):
    return execution_service.resume_dispatch(db, key, u)


@router.get('/missions/{key}/duration-prediction')
def duration_prediction(key: int, u=Depends(current), db=Depends(session)):
    return execution_service.prediction_report(db, key)
