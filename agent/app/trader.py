from __future__ import annotations
import os, math, asyncio, time
from . import mexc
from .mexc_signed import new_order, query_order, MexcTradeError
from .db import SessionLocal, Position, MexcOrder

TP_PCT = float(os.getenv("TP_PCT","0.02"))
SL_PCT = float(os.getenv("SL_PCT","0.00"))         # 0 disables
MAX_USDT = float(os.getenv("MAX_USDT_PER_TRADE","50"))
MAX_BUYS = int(os.getenv("MAX_OPEN_BUYS_PER_SYMBOL","1"))
TRADE_ENABLED = os.getenv("TRADE_ENABLED","false").lower()=="true"
TEST_ONLY = os.getenv("TRADE_TEST_ONLY","true").lower()=="true"
MAKER_BPS = float(os.getenv("MAKER_BPS","8"))/10000.0
TAKER_BPS = float(os.getenv("TAKER_BPS","10"))/10000.0

def _qty_for_budget(price: float) -> float:
    if price <= 0: return 0.0
    # assume quantity step 0.0001; you can refine using exchangeInfo filters later
    q = MAX_USDT / price
    # round down to 4 decimals
    return math.floor(q * 10000) / 10000.0

def _tp_price(buy_price: float, maker_fee=MAKER_BPS, taker_fee=TAKER_BPS) -> float:
    # Solve: sell*(1 - fee_sell) >= buy*(1+fee_buy)*(1+TP)
    return buy_price * (1.0 + maker_fee) * (1.0 + TP_PCT) / (1.0 - taker_fee)

async def trader_tick(symbols: list[str], interval="60m"):
    if not TRADE_ENABLED:
        return

    # fetch last closes
    closes = {}
    for sym in symbols:
        try:
            df = await mexc.klines(sym, interval=interval, limit=2)
            closes[sym] = float(df["close"].iloc[-1])
        except Exception:
            continue

    async with SessionLocal() as s:
        # ensure position rows exist
        for sym in symbols:
            pos = await s.get(Position, sym)
            if not pos:
                pos = Position(symbol=sym, qty=0.0, state="flat")
                s.add(pos)
        await s.commit()

        # load recs cache (from recs.py)
        from .recs import get_latest
        snap = get_latest() or {}
        rec_map = {r["symbol"]: r for r in snap.get("results", [])}

        for sym in symbols:
            price = closes.get(sym)
            if not price: continue

            pos = await s.get(Position, sym)
            rec = (rec_map.get(sym) or {}).get("recommendation", "HOLD")

            # 1) Entry: flat & BUY/ACCUMULATE => place a limit buy
            if pos.state == "flat" and rec in ("BUY","ACCUMULATE"):
                # rate limit check would go here if you batch many symbols
                qty = _qty_for_budget(price)
                if qty <= 0: continue

                try:
                    resp = await new_order(
                        symbol=sym, side="BUY", order_type="LIMIT",
                        quantity=qty, price=price, tif="GTC", test=TEST_ONLY,
                        client_order_id=f"botbuy_{int(time.time())}"
                    )
                    s.add(MexcOrder(symbol=sym, side="BUY", type="LIMIT", price=price, qty=qty,
                                    status="NEW", is_test=TEST_ONLY))
                    # assume filled in test mode (or you can query)
                    pos.qty = qty
                    pos.avg_price = price
                    pos.state = "long"
                    pos.target_price = _tp_price(price)
                    pos.stop_price = (price * (1.0 - SL_PCT)) if SL_PCT > 0 else None
                    await s.commit()
                except MexcTradeError as e:
                    s.add(MexcOrder(symbol=sym, side="BUY", type="LIMIT", price=price, qty=qty,
                                    status="REJECTED", is_test=TEST_ONLY, error=str(e)))
                    await s.commit()
                    continue

            # 2) Exit: long => ensure TP sell is placed (LIMIT)
            if pos.state == "long" and pos.qty > 0 and pos.avg_price:
                tp = pos.target_price or _tp_price(pos.avg_price)
                try:
                    resp = await new_order(
                        symbol=sym, side="SELL", order_type="LIMIT",
                        quantity=pos.qty, price=tp, tif="GTC", test=TEST_ONLY,
                        client_order_id=f"botsell_{int(time.time())}"
                    )
                    s.add(MexcOrder(symbol=sym, side="SELL", type="LIMIT", price=tp, qty=pos.qty,
                                    status="NEW", is_test=TEST_ONLY))
                    # assume immediate place; mark state "closing"
                    pos.state = "closing"
                    await s.commit()
                except MexcTradeError as e:
                    s.add(MexcOrder(symbol=sym, side="SELL", type="LIMIT", price=tp, qty=pos.qty,
                                    status="REJECTED", is_test=TEST_ONLY, error=str(e)))
                    await s.commit()
                    continue

            # 3) (Optional) fill/SL monitoring: for brevity we skip polling query_order here.
            # You can add a periodic check to query open orders and update pos.state back to flat when sell fills.

async def trader_loop(broadcast):
    # reuse same symbol source as recs loop
    from .recs import _symbols_from_env
    symbols = _symbols_from_env()
    while True:
        try:
            await trader_tick(symbols)
        except Exception as e:
            await broadcast("recs", {"type":"trade_error","error":str(e)})
        await asyncio.sleep(15)  # light touch; we only act when state warrants
