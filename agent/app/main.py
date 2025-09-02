# app/main.py
from __future__ import annotations
import uuid, os, asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
from sqlalchemy import text
from typing import Optional, List
from .db import init_db, SessionLocal, Run, RunStatus
from .agent_loop import run_agent
from .ws import ws_manager
from . import mexc
from .ta import ta_summary
import datetime as dt

app = FastAPI(title="Autonomous Agent MVP")

@app.on_event("startup")
async def startup():
    await init_db()

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))

class RunCreate(BaseModel):
    goal: str

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
async def recommendations(interval: str = "60m",
                          symbols: Optional[str] = None,
                          limit: int = 300):
    """
    Batch TA over a symbol set and return sorted results.
    - interval: 1m,5m,15m,30m,60m,4h,1d,1W,1M (1h alias -> 60m handled in mexc.py)
    - symbols: comma-separated list like 'BTCUSDT,ETHUSDT'; if empty, use UNIVERSE env
    """
    # decide symbol set
    if symbols:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    else:
        env_uni = os.getenv("UNIVERSE", "")
        if env_uni.strip():
            syms = [s.strip().upper() for s in env_uni.split(",") if s.strip()]
        else:
            # fallback: a small default universe
            syms = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "SUIUSDT"]

    # worker
    async def one(sym: str):
        try:
            df = await mexc.klines(sym, interval=interval, limit=limit)
            summ = ta_summary(df)
            # enrich 24h change (best-effort)
            try:
                t24 = await mexc.ticker24([sym])
                if t24 and isinstance(t24, list):
                    ch = t24[0].get("priceChangePercent")
                    if ch is not None:
                        summ["change24h"] = float(ch)
            except Exception:
                pass
            return {
                "symbol": sym,
                "interval": interval,
                "price": summ.get("price"),
                "score": summ.get("score"),
                "recommendation": summ.get("recommendation"),
                "rsi14": summ.get("rsi14"),
                "macd_hist": summ.get("macd_hist"),
                "ema20": summ.get("ema20"),
                "ema50": summ.get("ema50"),
                "ema200": summ.get("ema200"),
                "atr14": summ.get("atr14"),
                "atr_ratio": summ.get("atr_ratio"),
                "change24h": summ.get("change24h"),
                "reasons": (summ.get("reasons") or [])[:2],
            }
        except Exception as e:
            return {"symbol": sym, "interval": interval, "error": str(e)}

    results = await asyncio.gather(*(one(s) for s in syms))
    ok = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]

    # sort strongest first
    ok.sort(key=lambda r: (r.get("score") is not None, r.get("score", -1e9)), reverse=True)

    return {
        "as_of": dt.datetime.utcnow().isoformat() + "Z",
        "interval": interval,
        "count": len(results),
        "symbols": syms,
        "results": ok,
        "errors": err,
    }

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
