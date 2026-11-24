"""SQLAlchemy ORM models for the unit catalog and effective query models."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Batch(Base):
    __tablename__ = "batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    batch_no: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="effective", index=True)
    request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Field(Base):
    __tablename__ = "fields"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    known_units: Mapped[list["KnownUnit"]] = relationship(back_populates="field")


class KnownUnit(Base):
    __tablename__ = "known_units"
    __table_args__ = (UniqueConstraint("evidence_no", name="uq_known_evidence_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    field_id: Mapped[int] = mapped_column(ForeignKey("fields.id", ondelete="RESTRICT"), index=True)
    unit: Mapped[str] = mapped_column(String(20), index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("batches.batch_id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    field: Mapped[Field] = relationship(back_populates="known_units")


class Formula(Base):
    __tablename__ = "formulas"
    __table_args__ = (UniqueConstraint("formula_no", name="uq_formula_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    formula_no: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    result_field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="RESTRICT"), index=True
    )
    left_field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="RESTRICT"), index=True
    )
    right_field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="RESTRICT"), index=True
    )
    operator: Mapped[str] = mapped_column(String(2))
    expression: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("batches.batch_id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReasoningGroup(Base):
    __tablename__ = "reasoning_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    feasible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    conflict_core: Mapped[list] = mapped_column(JSON, default=list)
    formula_count: Mapped[int] = mapped_column(Integer, default=0)
    known_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_batch_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FieldEffective(Base):
    __tablename__ = "field_effective"

    field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("reasoning_groups.id", ondelete="CASCADE"), index=True
    )
    feasible: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    unique_unit: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WarehouseEntry(Base):
    __tablename__ = "warehouse_entries"

    field_id: Mapped[int] = mapped_column(
        ForeignKey("fields.id", ondelete="CASCADE"), primary_key=True
    )
    unit: Mapped[str] = mapped_column(String(20), index=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("reasoning_groups.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
