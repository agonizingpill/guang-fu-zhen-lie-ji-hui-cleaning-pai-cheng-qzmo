from datetime import date
from pydantic import Field
from .revisit_schemas import Inputs


class SegmentInput(Inputs):
    permit_id: int
    work_date: date
    actual_minutes: int = Field(ge=1, le=1440)
    water_used_liters: float = Field(ge=0, le=100000)
    segment_code: str = Field(min_length=2, max_length=50)
    note: str = Field(default='', max_length=400)


class PauseInput(Inputs):
    reason: str = Field(min_length=2, max_length=400)


class ContinuationInput(Inputs):
    mission_id: int
    crew_id: int
    work_date: date
    wind_mps: float = Field(ge=0, le=60)
    isolation: bool
