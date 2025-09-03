# app/main.py
from __future__ import annotations
import uuid, os, asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
from sqlalchemy import text, select, desc
from typing import Optional, List
from .db import init_db, SessionLocal, Run, RunStatus, RecPoint
from .agent_loop import run_agent
from .ws import ws_manager
from . import mexc
from .ta import ta_summary
from .recs import recs_loop, get_latest, get_meta, _symbols_from_env
from .trader import trader_loop, trader_get_status, trader_tick

import datetime as dt

app = FastAPI(title="Autonomous Agent MVP")

@app.on_event("startup")
async def startup():
    await init_db()

    async def broadcast(room: str, message: dict):
        # reuse WS manager “rooms”
        await ws_manager.broadcast(room, message)

    import asyncio
    asyncio.create_task(recs_loop(broadcast))
    asyncio.create_task(trader_loop(broadcast))

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))

class RunCreate(BaseModel):
    goal: str

@app.get("/trader/status")
async def trader_status():
    return trader_get_status()

@app.post("/trader/run-now")
async def trader_run_now():
    # fire a single pass immediately for debugging
    async def broadcast(room, msg):
        await ws_manager.broadcast(room, msg)
    symbols = _symbols_from_env()
    actions = await trader_tick(symbols, interval=os.getenv("REC_INTERVAL","60m"), broadcast=broadcast)
    return {"ok": True, "actions": int(actions or 0)}

@app.get("/recs/history")
async def recs_history(symbols: str, interval: str = "60m", points: int = 48):
    syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
    if not syms:
        raise HTTPException(400, "symbols required")

    out = {}
    async with SessionLocal() as s:
        for sym in syms:
            stmt = (
                select(RecPoint.as_of, RecPoint.price, RecPoint.score)
                .where(RecPoint.symbol == sym, RecPoint.interval == interval)
                .order_by(desc(RecPoint.as_of))
                .limit(points)
            )
            rows = (await s.execute(stmt)).all()
            rows = rows[::-1]  # oldest→newest
            out[sym] = {
                "as_of":  [str(r[0]) for r in rows],
                "price":  [float(r[1]) if r[1] is not None else None for r in rows],
                "score":  [float(r[2]) if r[2] is not None else None for r in rows],
            }
    return {"interval": interval, "points": points, "series": out}

@app.get("/recs/status")
async def recs_status():
    latest = get_latest()
    meta = get_meta()
    return {
        "enabled": os.getenv("REC_ENABLED","true").lower()=="true",
        "interval": os.getenv("REC_INTERVAL","60m"),
        "period_sec": int(os.getenv("REC_PERIOD_SEC","60")),
        "symbols_env": os.getenv("REC_SYMBOLS") or os.getenv("UNIVERSE"),
        "last_push_at": meta["last_push_at"],
        "last_count": meta["last_count"],
        "cache_present": bool(latest),
        "cache_as_of": latest.get("as_of") if latest else None,
    }

@app.post("/recs/snapshot_now")
async def recs_snapshot_now(interval: str = "60m", symbols: str | None = None, limit: int = 300):
    # compute once and persist rows now
    from .recs import _compute_once as compute_once  # uses mexc + ta_summary
    from .db import SessionLocal, RecPoint

    syms = [s.strip().upper() for s in (symbols or os.getenv("UNIVERSE","")).split(",") if s.strip()]
    if not syms:
        syms = ["BTCUSDT","ETHUSDT","SOLUSDT","SUIUSDT"]

    snap = await compute_once(interval, syms, limit)
    async with SessionLocal() as s:
        objs = [RecPoint(
            symbol=r["symbol"], interval=interval, price=r.get("price"), score=r.get("score"),
            rsi14=r.get("rsi14"), macd_hist=r.get("macd_hist"), change24h=r.get("change24h"),
            recommendation=r.get("recommendation"), reasons=r.get("reasons") or []
        ) for r in snap.get("results", [])]
        if objs:
            s.add_all(objs)
            await s.commit()
    return {"inserted": len(snap.get("results", []))}

@app.post("/runs")
async def create_run(req: RunCreate):
    run_id = str(uuid.uuid4())
    # create the run in its own short-lived session
    async with SessionLocal() as session:
        run = Run(id=run_id, goal=req.goal, status=RunStatus.queued)
        session.add(run)
        await session.commit()

    async def broadcast(_run_id: str, message: dict):
        await ws_manager.broadcast(_run_id, message)

    # start background worker with ONLY the run_id
    asyncio.create_task(run_agent(run_id, broadcast))
    return {"id": run_id, "status": "queued"}

@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if not run:
            raise HTTPException(404, "run not found")
        return {
            "id": run.id,
            "goal": run.goal,
            "status": run.status.value,
            "final_answer": run.final_answer,
            "created_at": str(run.created_at),
            "updated_at": str(run.updated_at),
        }

from typing import Optional, List

@app.get("/recommendations")
async def recommendations(interval: str = "60m", symbols: Optional[str] = None, limit: int = 300):
    # if params match the loop config and we have a fresh cache, just return it
    from .recs import get_latest
    latest = get_latest()
    if latest and latest.get("interval") == interval and not symbols:
        return latest
    # fallback to on-demand compute (old behavior)
    return await (await importlib_import_recs_compute())(interval, symbols, limit)

# helper to lazily import the on-demand compute without circulars
async def importlib_import_recs_compute():
    from .recs import _compute_once as compute_once  # type: ignore
    async def _runner(interval, symbols_csv, limit):
        if symbols_csv:
            syms = [s.strip().upper() for s in symbols_csv.split(",") if s.strip()]
        else:
            from .recs import _symbols_from_env
            syms = _symbols_from_env()
        snap = await compute_once(interval, syms, limit)
        from .recs import _apply_deltas, PREV
        return _apply_deltas(snap, PREV)
    return _runner


@app.websocket("/ws/runs/{run_id}")
async def ws_run(ws: WebSocket, run_id: str):
    await ws_manager.connect(run_id, ws)
    try:
        while True:
            await ws.receive_text()  # keepalive
    except WebSocketDisconnect:
        ws_manager.remove(run_id, ws)
    except Exception:
        ws_manager.remove(run_id, ws)

@app.websocket("/ws/recs")
async def ws_recs(ws: WebSocket):
    room = "recs"
    await ws_manager.connect(room, ws)
    try:
        # on connect, send the latest snapshot immediately (if any)
        latest = get_latest()
        if latest:
            await ws.send_json({"type":"recs","data": latest})
        while True:
            await ws.receive_text()  # no client messages; keepalive
    except WebSocketDisconnect:
        ws_manager.remove(room, ws)
    except Exception:
        ws_manager.remove(room, ws)

@app.get("/debug/llm")
async def debug_llm():
    from .agent_loop import OLLAMA_URL, MODEL
    test_messages = [
        {"role":"system","content":"Reply with a single JSON object only: {\"type\":\"final\",\"answer\":\"ok\"}"},
        {"role":"user","content":"Say ok"}
    ]
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(f"{OLLAMA_URL}/api/chat", json={
            "model": MODEL,
            "messages": test_messages,
            "stream": False,
            "format": "json"
        })
        try:
            j = r.json()
        except Exception:
            return {"http_status": r.status_code, "text": await r.text()}
    return {"http_status": r.status_code, "raw": j.get("message",{}).get("content","")}
