from __future__ import annotations
import os, enum, json, datetime as dt
from typing import Optional
from sqlalchemy import String, Text, JSON, Enum, Integer, func, text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://agent:agentpw@db:5432/aiagent")

Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

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

# helpers
async def add_event(session: AsyncSession, run_id: str, step: int, etype: str, content: dict):
    ev = RunEvent(run_id=run_id, step=step, type=etype, content=content)
    session.add(ev)
    await session.commit()
    await session.refresh(ev)
    return ev
