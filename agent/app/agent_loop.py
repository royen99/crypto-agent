# app/agent_loop.py
from __future__ import annotations
import os, json, asyncio
from urllib.parse import urlparse
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from .db import SessionLocal, Run, RunStatus, add_event, Memory

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
MODEL = os.getenv("MODEL_NAME", "qwen2.5")
MAX_STEPS = int(os.getenv("MAX_STEPS", "8"))
ALLOWLIST = {h.strip() for h in os.getenv("ALLOWLIST", "api.github.com,example.com").split(",") if h.strip()}

SYSTEM_PROMPT = """You are an autonomous but cautious agent.
You must respond ONLY with JSON objects, no extra text.
Two formats:
- {"type":"tool","name":"<tool>","args":{...}}
- {"type":"final","answer":"..."}
Available tools:
- http_get(url): GET from allowlisted domains (no auth). Return short text.
- write_note(key, text, tags?): store memory.
Think step-by-step; if a tool fails, try a different approach or finalize gracefully.
"""

async def call_ollama(messages: list[dict]) -> dict:
    payload = {"model": MODEL, "messages": messages, "stream": False}
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        content = r.json()["message"]["content"]
    try:
        return json.loads(content)
    except Exception:
        return {"type":"final","answer":"Model returned invalid JSON once; stopping."}

async def tool_http_get(url: str) -> dict:
    host = urlparse(url).hostname or ""
    if host not in ALLOWLIST:
        return {"error": f"host '{host}' not in allowlist"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.get(url)
            return {"status": r.status_code, "text": r.text[:5000]}
        except Exception as e:
            return {"error": str(e)}

async def tool_write_note(session: AsyncSession, key: str, text: str, tags: list[str] | None = None) -> dict:
    m = Memory(key=key, value=text, tags=tags or [])
    session.add(m)
    await session.commit()
    return {"ok": True, "id": m.id}

async def run_agent(run_id: str, ws_broadcast):
    # each worker has its own session
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if not run:
            return
        run.status = RunStatus.running
        await session.commit()

        messages = [
            {"role":"system", "content": SYSTEM_PROMPT},
            {"role":"user", "content": run.goal},
        ]

        try:
            for step in range(1, MAX_STEPS + 1):
                reply = await call_ollama(messages)
                await add_event(session, run.id, step, "thought", {"raw": reply})
                await ws_broadcast(run.id, {"type":"thought","step":step,"data":reply})

                if reply.get("type") == "tool":
                    name = reply.get("name")
                    args = reply.get("args", {})

                    if name == "http_get":
                        result = await tool_http_get(args.get("url",""))
                    elif name == "write_note":
                        result = await tool_write_note(session, args.get("key",""), args.get("text",""), args.get("tags"))
                    else:
                        result = {"error": "unknown tool"}

                    await add_event(session, run.id, step, "observation", {"tool": name, "result": result})
                    await ws_broadcast(run.id, {"type":"observation","step":step,"data":{"tool":name,"result":result}})

                    messages.append({"role":"assistant","content":json.dumps(reply)})
                    messages.append({"role":"tool","content":json.dumps({"tool":name,"result":result})})
                    messages.append({"role":"user","content":"Observe the tool result above and continue. Respond in JSON."})
                    continue

                if reply.get("type") == "final":
                    run.status = RunStatus.success
                    run.final_answer = reply.get("answer","")
                    await session.commit()
                    await add_event(session, run.id, step, "final", {"answer": run.final_answer})
                    await ws_broadcast(run.id, {"type":"final","step":step,"data":{"answer":run.final_answer}})
                    return

                messages.append({"role":"user","content":"Please respond strictly in the JSON formats described."})

            # exhausted steps
            run.status = RunStatus.stopped
            await session.commit()
            await add_event(session, run.id, MAX_STEPS, "error", {"msg":"Stopped due to max steps."})
            await ws_broadcast(run.id, {"type":"error","step":MAX_STEPS,"data":{"msg":"Stopped due to max steps."}})

        except Exception as e:
            run.status = RunStatus.error
            await session.commit()
            await add_event(session, run.id, 0, "error", {"msg": str(e)})
            await ws_broadcast(run.id, {"type":"error","step":0,"data":{"msg":str(e)}})
