# app/agent_loop.py
from __future__ import annotations
import os, json, asyncio
from urllib.parse import urlparse
import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from . import mexc
from .ta import ta_summary
from .db import SessionLocal, Run, RunStatus, add_event, Memory
from .mexc_signed import new_order as signed_new_order, query_order as signed_query

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434")
MODEL = os.getenv("MODEL_NAME", "qwen2.5")
MAX_STEPS = int(os.getenv("MAX_STEPS", "8"))
ALLOWLIST = {h.strip() for h in os.getenv("ALLOWLIST", "api.github.com,example.com").split(",") if h.strip()}

SYSTEM_PROMPT = """You are an autonomous but cautious agent.
Output ONLY a single valid JSON object (no markdown, no extra text).

Allowed formats:
{"type":"tool","name":"<tool>","args":{...}}
{"type":"final","answer":"..."}

Available tools:
- http_get(url)                            # allowlisted GET
- write_note(key, text, tags?)             # persist text
- mexc_list_usdt()                         # list USDT symbols
- mexc_klines(symbol, interval, limit)     # candles
- mexc_ta(symbol, interval, limit)         # TA + recommendation
- mexc_price(symbol, interval="60m")       # last close
- mexc_buy(symbol, budget_usdt?, price?)   # TEST limit buy (respects filters)
- mexc_sell(symbol, qty, price?)           # TEST limit sell (respects filters)

Policy:
- You MAY place TEST or LIVE orders using mexc_buy/mexc_sell.
- If a tool reports live trading is disabled, finalize with a short explanation.
- Prefer intervals: 1m,5m,15m,30m,60m,4h,1d.
- If a tool fails, try an alternative or finalize gracefully.
"""

async def call_ollama(messages: list[dict]) -> dict:
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "format": "json",  # force JSON from Ollama
        "options": {"temperature": 0.2, "top_p": 0.9}
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        content = r.json()["message"]["content"]

    # primary parse
    try:
        return json.loads(content)
    except Exception:
        # surgical fallback: extract the largest {...} block
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(content[start:end+1])
            except Exception:
                pass
        return {"type": "final", "answer": "Model could not produce valid JSON; stopping."}

async def tool_mexc_buy(symbol:str, price:float, usdt:float):
    qty = max(0.0, int((usdt/price)*10000)/10000.0)
    r = await signed_new_order(symbol, "BUY", "LIMIT", qty, price, tif="GTC", test=os.getenv("TRADE_TEST_ONLY","true").lower()=="true")
    return {"ok": True, "qty": qty, "resp": r}

async def tool_mexc_sell(symbol:str, price:float, qty:float):
    r = await signed_new_order(symbol, "SELL", "LIMIT", qty, price, tif="GTC", test=os.getenv("TRADE_TEST_ONLY","true").lower()=="true")
    return {"ok": True, "resp": r}

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
    async with SessionLocal() as session:
        run = await session.get(Run, run_id)
        if not run:
            return
        run.status = RunStatus.running
        await session.commit()

        # 🔸 tell the UI we started
        try:
            await ws_broadcast(run.id, {"type":"log","step":0,"data":{"msg":"run started"}})
        except Exception:
            pass

        messages = [
            {"role":"system", "content": SYSTEM_PROMPT},
            {"role":"user", "content": run.goal},
        ]

        try:
            for step in range(1, MAX_STEPS + 1):
                # 🔸 time-box the model call so it can't hang forever
                try:
                    reply = await asyncio.wait_for(call_ollama(messages), timeout=25)
                except asyncio.TimeoutError:
                    await add_event(session, run.id, step, "error", {"msg":"LLM timeout"})
                    await ws_broadcast(run.id, {"type":"error","step":step,"data":{"msg":"LLM timeout"}})
                    run.status = RunStatus.error
                    await session.commit()
                    return

                await add_event(session, run.id, step, "thought", {"raw": reply})
                await ws_broadcast(run.id, {"type":"thought","step":step,"data":reply})

                if reply.get("type") == "tool":
                    name = reply.get("name")
                    args = reply.get("args", {})

                    if name == "http_get":
                        result = await tool_http_get(args.get("url",""))
                    elif name == "write_note":
                        result = await tool_write_note(session, args.get("key",""), args.get("text",""), args.get("tags"))
                    elif name == "mexc_list_usdt":
                        result = await tool_mexc_list_usdt()
                    elif name == "mexc_klines":
                        result = await tool_mexc_klines(args.get("symbol",""), args.get("interval","60m"), int(args.get("limit",200)))
                    elif name == "mexc_ta":
                        result = await tool_mexc_ta(args.get("symbol",""), args.get("interval","60m"), int(args.get("limit",300)))
                    elif name == "mexc_price":
                        result = await tool_mexc_price(args.get("symbol",""), args.get("interval","60m"))
                    elif name == "mexc_buy":
                        result = await tool_mexc_buy(args.get("symbol",""), args.get("budget_usdt"), args.get("price"))
                    elif name == "mexc_sell":
                        result = await tool_mexc_sell(args.get("symbol",""), float(args.get("qty", 0)), args.get("price"))
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

                # force JSON compliance next loop
                messages.append({"role":"user","content":"Please respond strictly in the JSON formats described."})

            run.status = RunStatus.stopped
            await session.commit()
            await add_event(session, run.id, MAX_STEPS, "error", {"msg":"Stopped due to max steps."})
            await ws_broadcast(run.id, {"type":"error","step":MAX_STEPS,"data":{"msg":"Stopped due to max steps."}})

        except Exception as e:
            run.status = RunStatus.error
            await session.commit()
            await add_event(session, run.id, 0, "error", {"msg": str(e)})
            try:
                await ws_broadcast(run.id, {"type":"error","step":0,"data":{"msg":str(e)}})
            except Exception:
                pass

async def tool_mexc_list_usdt() -> dict:
    syms = await mexc.list_usdt_symbols(online_only=True)
    # Optionally filter by UNIVERSE env
    universe_env = os.getenv("UNIVERSE", "")
    if universe_env.strip():
        uni = {x.strip().upper() for x in universe_env.split(",")}
        syms = [s for s in syms if s in uni]
    return {"symbols": syms}

async def tool_mexc_klines(symbol: str, interval: str="60m", limit: int=200) -> dict:
    df = await mexc.klines(symbol.upper(), interval=interval, limit=limit)
    # Return a compressed view to keep tokens lean:
    tail = df.tail(5)
    rows = [
        {
            "t_open": str(row.t_open),
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
        } for row in tail.itertuples(index=False)
    ]
    return {"symbol": symbol.upper(), "interval": interval, "last": rows, "last_close": float(df["close"].iloc[-1])}

async def tool_mexc_ta(symbol: str, interval: str="60m", limit: int=300) -> dict:
    df = await mexc.klines(symbol.upper(), interval=interval, limit=limit)
    summ = ta_summary(df)
    # enrich with 24h change if possible
    try:
        t24 = await mexc.ticker24([symbol.upper()])
        if t24 and isinstance(t24, list):
            ch = t24[0].get("priceChangePercent")
            if ch is not None:
                summ["change24h"] = float(ch)
    except Exception:
        pass
    summ["symbol"] = symbol.upper()
    summ["interval"] = interval
    return summ

async def tool_mexc_price(symbol: str, interval: str = "60m") -> dict:
    """Return last close for a symbol on a given interval."""
    df = await mexc.klines(symbol.upper(), interval=interval, limit=2)
    price = float(df["close"].iloc[-1])
    return {"symbol": symbol.upper(), "interval": interval, "price": price}

async def tool_mexc_buy(symbol: str,
                        budget_usdt: float | None = None,
                        price: float | None = None) -> dict:
    """
    Place a TEST LIMIT BUY using env MAX_USDT_PER_TRADE if budget not given.
    Respects tickSize/stepSize/minNotional. Returns qty/price used.
    """
    test = os.getenv("TRADE_TEST_ONLY", "true").lower() == "true"
    if not test:
        return {"error": "Live orders disabled. Set TRADE_TEST_ONLY=true for test orders."}

    max_usdt = float(os.getenv("MAX_USDT_PER_TRADE", "50"))
    budget = max_usdt if (budget_usdt is None) else float(budget_usdt)

    # pick a price (last close) if not provided, then round to tick
    if price is None:
        df = await mexc.klines(symbol.upper(), interval="60m", limit=2)
        price = float(df["close"].iloc[-1])

    price = await mexc.round_price(symbol, price)
    qty = await mexc.size_order(symbol, price, budget)
    if qty <= 0:
        return {"error": "Budget too small for minNotional/stepSize."}

    resp = await signed_new_order(
        symbol=symbol.upper(),
        side="BUY",
        order_type="LIMIT",
        quantity=qty,
        price=price,
        tif="GTC",
        test=True,  # always test here
        client_order_id=f"goalbuy_{int(time.time())}"
    )
    return {"ok": True, "mode": "test", "symbol": symbol.upper(), "qty": qty, "price": price, "resp": resp}

async def tool_mexc_sell(symbol: str,
                         qty: float,
                         price: float | None = None) -> dict:
    """
    Place a TEST LIMIT SELL for given qty. Rounds price; refuses if invalid.
    """
    test = os.getenv("TRADE_TEST_ONLY", "true").lower() == "true"
    if not test:
        return {"error": "Live orders disabled. Set TRADE_TEST_ONLY=true for test orders."}

    if qty is None or float(qty) <= 0:
        return {"error": "qty must be > 0"}

    if price is None:
        df = await mexc.klines(symbol.upper(), interval="60m", limit=2)
        price = float(df["close"].iloc[-1])
    price = await mexc.round_price(symbol, price)

    # also round qty to step/minQty
    qty = await mexc.round_qty(symbol, float(qty))
    if qty <= 0:
        return {"error": "qty below stepSize/minQty"}

    resp = await signed_new_order(
        symbol=symbol.upper(),
        side="SELL",
        order_type="LIMIT",
        quantity=qty,
        price=price,
        tif="GTC",
        test=True,  # always test here
        client_order_id=f"goalsell_{int(time.time())}"
    )
    return {"ok": True, "mode": "test", "symbol": symbol.upper(), "qty": qty, "price": price, "resp": resp}
