from __future__ import annotations
import os, asyncio, datetime as dt
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from . import mexc
from .ta import ta_summary
from .db import SessionLocal, RecPoint

# in-memory cache
LATEST: Optional[dict] = None
PREV: Optional[dict] = None
LAST_PUSH_AT = None
LAST_COUNT = 0

def _symbols_from_env() -> list[str]:
    raw = os.getenv("REC_SYMBOLS", "").strip() or os.getenv("UNIVERSE", "")
    if raw.strip():
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return ["BTCUSDT","ETHUSDT","SOLUSDT","SUIUSDT"]

async def _compute_once(interval: str, symbols: list[str], limit: int) -> dict:
    async def one(sym: str):
        try:
            df = await mexc.klines(sym, interval=interval, limit=limit)
            summ = ta_summary(df)
            # 24h change best-effort
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

    import asyncio
    results = await asyncio.gather(*(one(s) for s in symbols))
    ok = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]
    ok.sort(key=lambda r: (r.get("score") is not None, r.get("score", -1e9)), reverse=True)
    return {
        "as_of": dt.datetime.utcnow().isoformat() + "Z",
        "interval": interval,
        "symbols": symbols,
        "results": ok,
        "errors": err,
    }

def get_meta():
    return {"last_push_at": LAST_PUSH_AT, "last_count": LAST_COUNT}

def _apply_deltas(cur: dict, prev: Optional[dict]) -> dict:
    if not prev:
        return cur
    prev_map = {r["symbol"]: r for r in prev.get("results", [])}
    for r in cur.get("results", []):
        p = prev_map.get(r["symbol"])
        if not p: 
            r["delta_score"] = None
            r["delta_price"] = None
            continue
        try:
            r["delta_score"] = (r.get("score") - p.get("score")) if (r.get("score") is not None and p.get("score") is not None) else None
        except Exception:
            r["delta_score"] = None
        try:
            r["delta_price"] = (r.get("price") - p.get("price")) if (r.get("price") is not None and p.get("price") is not None) else None
        except Exception:
            r["delta_price"] = None
    return cur

async def recs_loop(broadcast):
    global LATEST, PREV
    enabled = os.getenv("REC_ENABLED", "true").lower() == "true"
    if not enabled:
        return
    interval = os.getenv("REC_INTERVAL", "60m")
    period = int(os.getenv("REC_PERIOD_SEC", "60"))
    limit = int(os.getenv("REC_LIMIT", "300"))
    symbols = _symbols_from_env()
    persist = os.getenv("REC_SNAPSHOTS", "false").lower() == "true"

    while True:
        try:
            snap = await _compute_once(interval, symbols, limit)
            snap = _apply_deltas(snap, PREV)
            PREV, LATEST = LATEST, snap
            LAST_PUSH_AT = datetime.now(timezone.utc).isoformat()
            LAST_COUNT = len(snap.get("results", []))

            # optional persistence (simple append of whole snapshot)
            if persist:
                try:
                    async with SessionLocal() as s:
                        objs = []
                        for r in snap.get("results", []):
                            objs.append(RecPoint(
                                symbol=r["symbol"],
                                interval=interval,
                                price=r.get("price"),
                                score=r.get("score"),
                                rsi14=r.get("rsi14"),
                                macd_hist=r.get("macd_hist"),
                                change24h=r.get("change24h"),
                                recommendation=r.get("recommendation"),
                                reasons=r.get("reasons") or []
                            ))
                        if objs:
                            s.add_all(objs)
                            await s.commit()
                except Exception as e:
                    # optional: broadcast/log the error instead of swallowing
                    await broadcast("recs", {"type":"recs_persist_error", "error": str(e)})
        except Exception as e:
            await broadcast("recs", {"type": "recs_error", "error": str(e)})
        await asyncio.sleep(period)

def get_latest() -> Optional[dict]:
    return LATEST
