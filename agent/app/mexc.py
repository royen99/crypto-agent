from __future__ import annotations
import os, asyncio, time
from typing import List, Dict, Any, Tuple
import httpx
import pandas as pd

BASE = os.getenv("MEXC_BASE", "https://api.mexc.com")
_http_lock = asyncio.Semaphore(4)  # simple throttle

class MexcError(RuntimeError): pass

# top-level, under imports
VALID_INTERVALS = {"1m","5m","15m","30m","60m","4h","1d","1W","1M"}
ALIASES = {"1h":"60m", "4hr":"4h", "1w":"1W", "1mo":"1M"}

def _norm_interval(iv: str) -> str:
    iv = (iv or "").strip()
    iv = ALIASES.get(iv, iv)
    if iv not in VALID_INTERVALS:
        raise MexcError(f"invalid interval '{iv}'; use one of {sorted(VALID_INTERVALS)}")
    return iv

async def _get(path: str, params: dict|None=None) -> Any:
    url = f"{BASE}{path}"
    async with _http_lock, httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(url, params=params or {})
        if r.status_code != 200:
            raise MexcError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

async def exchange_info(symbols: list[str]|None=None) -> dict:
    # /api/v3/exchangeInfo supports no param, symbol, or symbols
    params = None
    if symbols is None:
        params = None
    elif len(symbols) == 1:
        params = {"symbol": symbols[0]}
    else:
        params = {"symbols": ",".join(symbols)}
    return await _get("/api/v3/exchangeInfo", params)

async def list_usdt_symbols(online_only: bool=True) -> list[str]:
    info = await exchange_info()
    raw = info.get("symbols") or []
    out = []
    for s in raw:
        try:
            if s.get("quoteAsset") != "USDT":
                continue
            if online_only and str(s.get("status")) != "1":
                continue
            if not s.get("isSpotTradingAllowed", False):
                continue
            out.append(s["symbol"])
        except Exception:
            continue
    return sorted(out)

async def klines(symbol: str, interval: str="60m", limit: int=200,
                 start_ms: int|None=None, end_ms: int|None=None) -> pd.DataFrame:
    interval = _norm_interval(interval)
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if start_ms and end_ms:
        params.update({"startTime": start_ms, "endTime": end_ms})
    data = await _get("/api/v3/klines", params)
    # Response: [ [open_time, open, high, low, close, volume, close_time, quote_vol], ... ]
    if not isinstance(data, list) or not data:
        raise MexcError("Empty klines")
    df = pd.DataFrame(data, columns=[
        "t_open","open","high","low","close","volume","t_close","quote_volume"
    ], dtype="float64")
    df["t_open"] = pd.to_datetime(df["t_open"], unit="ms", utc=True)
    df["t_close"] = pd.to_datetime(df["t_close"], unit="ms", utc=True)
    # ensure numeric
    for c in ("open","high","low","close","volume","quote_volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df

async def ticker24(symbols: list[str]|None=None) -> list[dict]:
    params = None
    if symbols is None:
        params = None
    elif len(symbols) == 1:
        params = {"symbol": symbols[0]}
    else:
        params = {"symbols": ",".join(symbols)}
    data = await _get("/api/v3/ticker/24hr", params)
    return data if isinstance(data, list) else [data]
