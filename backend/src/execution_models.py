from datetime import date, datetime
from sqlalchemy import String, Float, ForeignKey, CheckConstraint, Date, DateTime, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base


class WorkSegment(Base):
    """山地清洗跨日分段实耗：一段作业只认一个班组/班次。"""

    __tablename__ = 'work_segments'
    __table_args__ = (
        CheckConstraint('actual_minutes>0 AND water_used_liters>=0'),
        UniqueConstraint('segment_code', name='uq_work_segments_code'),
        UniqueConstraint('permit_id', 'work_date', name='uq_work_segments_permit_day'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    segment_code: Mapped[str] = mapped_column(String(50))
    mission_id: Mapped[int] = mapped_column(ForeignKey('wash_missions.id'))
    # 历史已完工任务可能没有许可凭证，允许为空；NULL 不参与每许可每日唯一约束。
    permit_id: Mapped[int | None] = mapped_column(ForeignKey('work_permits.id'), nullable=True)
    crew_id: Mapped[int] = mapped_column(ForeignKey('wash_crews.id'))
    work_date: Mapped[date] = mapped_column(Date)
    actual_minutes: Mapped[int] = mapped_column()
    water_used_liters: Mapped[float] = mapped_column(Float)
    note: Mapped[str] = mapped_column(String(400), default='')
    reporter: Mapped[str] = mapped_column(String(120), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
