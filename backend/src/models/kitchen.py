from sqlalchemy import String, Integer, Float, Boolean, JSON, Double, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class KitchenStall(Base):
    """档口级并发锚点：所有更正与确认都先锁这一行，保证重算与确认的顺序一致。"""
    __tablename__ = 'kitchen_stalls'
    id: Mapped[int] = mapped_column(primary_key=True)
    stall: Mapped[str] = mapped_column(String(80), unique=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class KitchenOperation(Base):
    """幂等操作记录。先独立提交 pending，崩溃后可由恢复任务原位重放。"""
    __tablename__ = 'kitchen_operations'
    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[str] = mapped_column(String(40))
    stall: Mapped[str] = mapped_column(String(80), default='')
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='pending')
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(String(1000), default='')
    error_status: Mapped[int] = mapped_column(Integer, default=0)
    actor: Mapped[str] = mapped_column(String(150), default='')
    actor_role: Mapped[str] = mapped_column(String(20), default='')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
    completed_at: Mapped[float | None] = mapped_column(Double, nullable=True)


class KitchenAdoptedPlan(Base):
    """按档口、按日登记实际采用的已批准建议；更正生成新版本，旧版本只做失效标记。"""
    __tablename__ = 'kitchen_adopted_plans'
    __table_args__ = (Index('ix_kitchen_plans_stall_day', 'stall', 'day'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    stall: Mapped[str] = mapped_column(String(80))
    day: Mapped[str] = mapped_column(String(10))
    qty: Mapped[int] = mapped_column(Integer)
    recommended: Mapped[int] = mapped_column(Integer)
    forecast: Mapped[float | None] = mapped_column(Float, nullable=True)
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey('kitchen_adopted_plans.id'), nullable=True)
    adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(500), default='')
    note: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    operation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)


class KitchenRequisitionBatch(Base):
    """实领批次。批次号在档口内唯一，重复上传同批次号视为更正并生成新版本。"""
    __tablename__ = 'kitchen_requisition_batches'
    __table_args__ = (
        UniqueConstraint('stall', 'batch_ref', 'revision', name='uq_kitchen_batch_revision'),
        Index('ix_kitchen_batches_stall_day', 'stall', 'day'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    stall: Mapped[str] = mapped_column(String(80))
    day: Mapped[str] = mapped_column(String(10))
    batch_ref: Mapped[str] = mapped_column(String(80))
    qty: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey('kitchen_requisition_batches.id'), nullable=True)
    source: Mapped[str] = mapped_column(String(200), default='')
    note: Mapped[str] = mapped_column(String(500), default='')
    created_by: Mapped[str] = mapped_column(String(150), default='')
    created_at: Mapped[float] = mapped_column(Double)
    operation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)


class KitchenAlert(Base):
    """同一连续超量段只有一条活动预警；合并/拆分时旧预警归档并保留前后任关系。"""
    __tablename__ = 'kitchen_alerts'
    __table_args__ = (
        Index('ix_kitchen_alerts_stall_status', 'stall', 'status'),
        Index('ix_kitchen_alerts_stall_range', 'stall', 'start_day', 'end_day'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_no: Mapped[str] = mapped_column(String(24), default='')
    stall: Mapped[str] = mapped_column(String(80))
    start_day: Mapped[str] = mapped_column(String(10))
    end_day: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default='open')
    version: Mapped[int] = mapped_column(Integer, default=1)
    basis: Mapped[dict] = mapped_column(JSON, default=dict)
    latest_basis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmed_basis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confirmed_by: Mapped[str] = mapped_column(String(150), default='')
    confirmed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    confirmed_note: Mapped[str] = mapped_column(String(1000), default='')
    revoked_reason: Mapped[str] = mapped_column(String(1000), default='')
    auto_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    predecessor_ids: Mapped[list] = mapped_column(JSON, default=list)
    successor_ids: Mapped[list] = mapped_column(JSON, default=list)
    correction_count: Mapped[int] = mapped_column(Integer, default=0)
    superseded_reason: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class KitchenAlertEvent(Base):
    __tablename__ = 'kitchen_alert_events'
    __table_args__ = (Index('ix_kitchen_alert_events_alert', 'alert_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey('kitchen_alerts.id'))
    type: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)
    actor: Mapped[str] = mapped_column(String(150), default='')
    note: Mapped[str] = mapped_column(String(1000), default='')
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[float] = mapped_column(Double)
    operation_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
