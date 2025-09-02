from __future__ import annotations
import os, time, hmac, hashlib
from urllib.parse import urlencode
import httpx

BASE = os.getenv("MEXC_BASE", "https://api.mexc.com")
KEY  = os.getenv("MEXC_API_KEY", "")
SEC  = os.getenv("MEXC_API_SECRET", "")

class MexcTradeError(RuntimeError): pass

async def _server_time_ms() -> int:
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"{BASE}/api/v3/time")
        r.raise_for_status()
        return int(r.json()["serverTime"])

def _sign(params: dict) -> str:
    # params must be querystring before signing
    qs = urlencode(params, safe=",")            # simple; fine for single-order
    sig = hmac.new(SEC.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return qs + "&signature=" + sig

async def _post_signed(path: str, params: dict) -> dict:
    if not KEY or not SEC:
        raise MexcTradeError("Missing API key/secret")
    ts = int(time.time()*1000)
    params.setdefault("timestamp", ts)
    params.setdefault("recvWindow", 5000)
    body = _sign(params)
    headers = {"X-MEXC-APIKEY": KEY, "Content-Type": "application/x-www-form-urlencoded"}
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.post(f"{BASE}{path}", content=body, headers=headers)
        if r.status_code != 200:
            raise MexcTradeError(f"HTTP {r.status_code}: {r.text}")
        return r.json() if r.text else {}

async def _get_signed(path: str, params: dict) -> dict:
    if not KEY or not SEC:
        raise MexcTradeError("Missing API key/secret")
    ts = int(time.time()*1000)
    params.setdefault("timestamp", ts)
    params.setdefault("recvWindow", 5000)
    qs = _sign(params)
    headers = {"X-MEXC-APIKEY": KEY}
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.get(f"{BASE}{path}?{qs}", headers=headers)
        if r.status_code != 200:
            raise MexcTradeError(f"HTTP {r.status_code}: {r.text}")
        return r.json() if r.text else {}

# --- public helpers we’ll use ---
async def new_order(symbol:str, side:str, order_type:str, quantity:float, price:float|None=None,
                    tif:str="GTC", test:bool=True, client_order_id:str|None=None):
    path = "/api/v3/order/test" if test else "/api/v3/order"
    p = {"symbol":symbol, "side":side, "type":order_type, "quantity":quantity}
    if price is not None: p["price"] = price
    if tif: p["timeInForce"] = tif
    if client_order_id: p["newClientOrderId"] = client_order_id
    return await _post_signed(path, p)

async def query_order(symbol:str, order_id:int|None=None, client_order_id:str|None=None):
    p = {"symbol":symbol}
    if order_id is not None: p["orderId"] = order_id
    if client_order_id is not None: p["origClientOrderId"] = client_order_id
    return await _get_signed("/api/v3/order", p)

async def cancel_order(symbol:str, order_id:int|None=None, client_order_id:str|None=None):
    p = {"symbol":symbol}
    if order_id is not None: p["orderId"] = order_id
    if client_order_id is not None: p["origClientOrderId"] = client_order_id
    return await _delete_signed("/api/v3/order", p)

async def _delete_signed(path:str, params:dict) -> dict:
    if not KEY or not SEC:
        raise MexcTradeError("Missing API key/secret")
    ts = int(time.time()*1000)
    params.setdefault("timestamp", ts)
    params.setdefault("recvWindow", 5000)
    qs = _sign(params)
    headers = {"X-MEXC-APIKEY": KEY}
    async with httpx.AsyncClient(timeout=10.0) as c:
        r = await c.delete(f"{BASE}{path}?{qs}", headers=headers)
        if r.status_code != 200:
            raise MexcTradeError(f"HTTP {r.status_code}: {r.text}")
        return r.json() if r.text else {}
