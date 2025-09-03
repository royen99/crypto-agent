from __future__ import annotations
import os, asyncio, time
from typing import List, Dict, Any, Tuple
import httpx, time
import pandas as pd
from decimal import Decimal, ROUND_DOWN, ROUND_UP, getcontext

BASE = os.getenv("MEXC_BASE", "https://api.mexc.com")
_http_lock = asyncio.Semaphore(4)  # simple throttle
getcontext().prec = 28

_EXINFO_CACHE = None
_EXINFO_AT = 0
_EXINFO_TTL = 300 

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

async def exchange_info(symbols: list[str] | str | None = None) -> dict:
    # /api/v3/exchangeInfo supports no param, symbol, or symbols
    params = None
    if symbols:
        if isinstance(symbols, list):
            params = {"symbols": ",".join(s.upper() for s in symbols)}
        else:
            params = {"symbols": str(symbols).upper()}
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

_EXINFO_CACHE = None
_EXINFO_AT = 0
_EXINFO_TTL = 300

def _as_dec(x, default="0"):
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)

async def _get_exchange_info():
    global _EXINFO_CACHE, _EXINFO_AT
    now = time.time()
    if _EXINFO_CACHE and (now - _EXINFO_AT) < _EXINFO_TTL:
        return _EXINFO_CACHE
    info = await exchange_info()
    _EXINFO_CACHE, _EXINFO_AT = info, now
    return info

# cache per symbol
_SYMBOL_FILTERS: dict[str, dict] = {}

def _parse_symbol_filters(sym_obj: dict) -> dict:
    """
    Return {'tickSize','stepSize','minQty','minNotional'} as Decimals or None.
    If 'filters' is empty, fall back to baseSizePrecision/quotePrecision fields.
    """
    res = {"tickSize": None, "stepSize": None, "minQty": None, "minNotional": None}

    # 1) Preferred: filters[]
    filters = sym_obj.get("filters") or []
    for f in filters:
        ftype = (f.get("filterType") or f.get("type") or f.get("name") or "").upper()
        if "PRICE" in ftype:
            res["tickSize"] = _as_dec(f.get("tickSize"))
        if "LOT" in ftype or "QUANTITY" in ftype:
            res["stepSize"] = _as_dec(f.get("stepSize"))
            if f.get("minQty") is not None:
                res["minQty"] = _as_dec(f.get("minQty"))
        if "NOTIONAL" in ftype:
            res["minNotional"] = _as_dec(f.get("minNotional"))

    # 2) Fallbacks when filters are missing/empty
    if not filters:
        # stepSize from baseSizePrecision if present
        bsp = sym_obj.get("baseSizePrecision")
        if bsp is not None:
            # can be "0.01" or numeric
            try:
                res["stepSize"] = _as_dec(bsp)
            except Exception:
                pass
        # else from baseAssetPrecision (digits -> 10^-digits)
        if res["stepSize"] in (None, Decimal("0")):
            bap = sym_obj.get("baseAssetPrecision")
            if isinstance(bap, int) and bap >= 0:
                res["stepSize"] = Decimal(1) / (Decimal(10) ** bap)

        # tickSize from max(quotePrecision, quoteAssetPrecision)
        qp = sym_obj.get("quotePrecision")
        qap = sym_obj.get("quoteAssetPrecision", qp)
        prec = None
        if isinstance(qp, int): prec = qp
        if isinstance(qap, int): prec = max(prec or 0, qap)
        if prec is not None:
            res["tickSize"] = Decimal(1) / (Decimal(10) ** int(prec))

        # minQty default to stepSize if not given (many venues allow 1 step as min)
        if res["minQty"] is None and res["stepSize"] not in (None, Decimal("0")):
            res["minQty"] = res["stepSize"]

    # 3) minNotional fallback from env
    if not res["minNotional"]:
        res["minNotional"] = _as_dec(os.getenv("MIN_NOTIONAL_FALLBACK_USDT", "5"))

    return res

async def symbol_filters(symbol: str) -> dict:
    sym = symbol.upper()
    if sym in _SYMBOL_FILTERS:
        return _SYMBOL_FILTERS[sym]

    # Pull just this symbol if possible (faster, up-to-date)
    info = await exchange_info(sym)
    arr = info.get("symbols") or []
    obj = (arr[0] if arr else {}) or {}
    parsed = _parse_symbol_filters(obj)
    _SYMBOL_FILTERS[sym] = parsed
    return parsed

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

def _quantize_down(x: Decimal, step: Decimal) -> Decimal:
    if not step or step == 0: return x
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step

def _quantize_up(x: Decimal, step: Decimal) -> Decimal:
    if not step or step == 0: return x
    return (x / step).to_integral_value(rounding=ROUND_UP) * step

async def round_price(symbol: str, price: float) -> float:
    f = await symbol_filters(symbol)
    tick = f.get("tickSize") or Decimal("0")
    return float(_quantize_down(_as_dec(price), tick) if tick else _as_dec(price))

async def round_qty(symbol: str, qty: float) -> float:
    f = await symbol_filters(symbol)
    step = f.get("stepSize") or Decimal("0")
    q = _quantize_down(_as_dec(qty), step) if step else _as_dec(qty)
    minq = f.get("minQty") or Decimal("0")
    if minq and q < minq:
        q = minq
    return float(q)

async def size_order(symbol: str, price: float, budget_usdt: float) -> float:
    f = await symbol_filters(symbol)
    p = _as_dec(price)
    budget = _as_dec(budget_usdt)
    step = f.get("stepSize") or Decimal("0")
    minq = f.get("minQty") or Decimal("0")
    minn = f.get("minNotional") or Decimal("0")
    if p <= 0 or budget <= 0: return 0.0

    raw = budget / p
    q = _quantize_down(raw, step) if step else raw
    if minq and q < minq:
        q = minq

    notional = q * p
    if minn and notional < minn:
        need_q = _quantize_up(minn / p, step) if step else (minn / p)
        if (need_q * p) > budget:
            return 0.0
        q = need_q
    return float(q) if q > 0 else 0.0

async def base_quote(symbol: str) -> tuple[str, str]:
    info = await _get_exchange_info()
    bysym = {s["symbol"].upper(): s for s in (info.get("symbols") or [])}
    o = bysym.get(symbol.upper()) or {}
    return (o.get("baseAsset") or "BASE", o.get("quoteAsset") or "USDT")

async def quote_asset(symbol: str) -> str:
    return (await base_quote(symbol))[1]
