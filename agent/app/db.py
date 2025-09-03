from __future__ import annotations
import os, enum, json, datetime as dt, math
from typing import Optional
from sqlalchemy import String, Text, JSON, Enum, Integer, func, text, ForeignKey, Float, DateTime, BigInteger, Boolean
from sqlalchemy.orm import Mapped, mapped_column, declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://agent:agentpw@db:5432/aiagent")

Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class MexcOrder(Base):
    __tablename__ = "mexc_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(10))
    client_order_id: Mapped[str | None] = mapped_column(String(128))
    mexc_order_id: Mapped[str | None] = mapped_column(String(128), index=True)  # ← string
    price: Mapped[float | None] = mapped_column(Float)
    qty: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str | None] = mapped_column(String(24), index=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class Position(Base):
    __tablename__ = "positions"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float)
    state: Mapped[str] = mapped_column(String(16), default="flat")
    target_price: Mapped[float | None] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float)
    last_buy_order: Mapped[str | None] = mapped_column(String(128))   # ← string
    last_sell_order: Mapped[str | None] = mapped_column(String(128))  # ← string
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class RecPoint(Base):
    __tablename__ = "rec_points"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    interval: Mapped[str] = mapped_column(String(8), index=True)
    price: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)
    rsi14: Mapped[float | None] = mapped_column(Float)
    macd_hist: Mapped[float | None] = mapped_column(Float)
    change24h: Mapped[float | None] = mapped_column(Float)
    recommendation: Mapped[str | None] = mapped_column(String(16))
    reasons: Mapped[list] = mapped_column(JSON, default=list)
class RunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    success = "success"
    error = "error"
    stopped = "stopped"

class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)           # uuid str
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="runstatus"),  # 🔸 match PG enum created in SQL
        default=RunStatus.queued,
        nullable=False
    )
    final_answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), nullable=False)

    events: Mapped[list[RunEvent]] = relationship("RunEvent", back_populates="run", cascade="all, delete-orphan")

class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    step: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(32))  # thought | tool | observation | final | log | error
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=func.now(), nullable=False)

    run: Mapped[Run] = relationship("Run", back_populates="events")

class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[str] = mapped_column(Text)
    tags: Mapped[Optional[list[str]]] = mapped_column(JSON, default=list)
    last_seen: Mapped[dt.datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

def sanitize_json(obj):
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_json(v) for v in obj]
    # ints/str/bool/None are fine
    return obj

# helpers
async def add_event(session: AsyncSession, run_id: str, step: int, etype: str, content: dict):
    safe = sanitize_json(content)
    ev = RunEvent(run_id=run_id, step=step, type=etype, content=safe)
    session.add(ev)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    await session.refresh(ev)
    return ev
