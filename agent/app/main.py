# app/main.py
from __future__ import annotations
import uuid, os, asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import httpx
from sqlalchemy import text
from .db import init_db, SessionLocal, Run, RunStatus
from .agent_loop import run_agent
from .ws import ws_manager

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
