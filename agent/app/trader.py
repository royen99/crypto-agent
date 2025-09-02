from __future__ import annotations
import os, math, asyncio, time
from . import mexc
from .ta import atr 
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
VOL_RATIO_MIN = float(os.getenv("VOL_RATIO_MIN","40"))
# fees: env gives bps; convert to fraction
MAKER_FRAC = float(os.getenv("MAKER_BPS","0"))/10000.0
TAKER_FRAC = float(os.getenv("TAKER_BPS","5"))/10000.0

def _qty_for_budget(price: float) -> float:
    if price <= 0: return 0.0
    # assume quantity step 0.0001; you can refine using exchangeInfo filters later
    q = MAX_USDT / price
    # round down to 4 decimals
    return math.floor(q * 10000) / 10000.0

def _tp_price(buy_price: float, maker_fee=MAKER_FRAC, taker_fee=TAKER_FRAC) -> float:
    return buy_price * (1.0 + maker_fee) * (1.0 + TP_PCT) / (1.0 - taker_fee)

def _tp_price(buy_price: float,
              maker_fee: float = MAKER_FRAC,
              taker_fee: float = TAKER_FRAC) -> float:
    """
    Fee-aware TP: sell*(1 - fee_sell) >= buy*(1+fee_buy)*(1+TP)
    -> sell >= buy*(1+maker_fee)*(1+TP) / (1 - taker_fee)
    """
    return buy_price * (1.0 + maker_fee) * (1.0 + TP_PCT) / (1.0 - taker_fee)

async def trader_tick(symbols: list[str],
                      interval: str = "60m",
                      broadcast=None) -> None:
    """
    One lightweight trading pass:
      - Pull last close per symbol
      - Enforce volatility gate via ATR ratio (price / ATR14)
      - Respect exchange filters (tickSize/stepSize/minNotional) for sizing & rounding
      - If flat and rec is BUY/ACCUMULATE -> place LIMIT BUY (test/live per env)
      - If long -> place LIMIT SELL at fee-aware TP (test/live per env), mark 'closing'
    """
    if not TRADE_ENABLED:
        return

    # 1) fetch last close for each symbol
    closes: dict[str, float] = {}
    for sym in symbols:
        try:
            df = await mexc.klines(sym, interval=interval, limit=2)
            closes[sym] = float(df["close"].iloc[-1])
        except Exception:
            continue

    async with SessionLocal() as s:
        # ensure Position rows exist
        for sym in symbols:
            pos = await s.get(Position, sym)
            if not pos:
                s.add(Position(symbol=sym, qty=0.0, state="flat"))
        await s.commit()

        # current TA cache (from recommendations loop)
        from .recs import get_latest
        snap = get_latest() or {}
        rec_map = {r["symbol"]: r for r in snap.get("results", [])}

        for sym in symbols:
            price = closes.get(sym)
            if not price:
                continue

            pos: Position = await s.get(Position, sym)
            rec_entry = rec_map.get(sym) or {}
            recommendation = rec_entry.get("recommendation", "HOLD")

            # ---------- ENTRY: flat + BUY/ACCUMULATE ----------
            if pos.state == "flat" and recommendation in ("BUY", "ACCUMULATE"):
                # volatility gate (ATR ratio)
                atr_ratio = rec_entry.get("atr_ratio")
                if atr_ratio is None:
                    # fallback compute ATR quickly if cache missing
                    try:
                        df2 = await mexc.klines(sym, interval=interval, limit=50)
                        a = atr(df2, 14).iloc[-1]
                        if a and a > 0:
                            atr_ratio = float(df2["close"].iloc[-1] / a)
                    except Exception:
                        atr_ratio = None

                if (atr_ratio is not None) and (atr_ratio < VOL_RATIO_MIN):
                    if broadcast:
                        await broadcast("recs", {
                            "type": "trade_skip",
                            "symbol": sym,
                            "reason": f"atr_ratio {atr_ratio:.1f} < {VOL_RATIO_MIN}"
                        })
                    continue  # too whippy

                # round price to tick, size to step & minNotional
                price_rounded = await mexc.round_price(sym, price)
                qty = await mexc.size_order(sym, price_rounded, MAX_USDT)
                if qty <= 0:
                    if broadcast:
                        await broadcast("recs", {
                            "type": "trade_skip",
                            "symbol": sym,
                            "reason": "budget below minNotional / stepSize"
                        })
                    continue

                try:
                    # place LIMIT BUY (test or live)
                    _ = await new_order(
                        symbol=sym,
                        side="BUY",
                        order_type="LIMIT",
                        quantity=qty,
                        price=price_rounded,
                        tif="GTC",
                        test=TEST_ONLY,
                        client_order_id=f"botbuy_{int(time.time())}"
                    )
                    s.add(MexcOrder(symbol=sym, side="BUY", type="LIMIT",
                                    price=price_rounded, qty=qty,
                                    status="NEW", is_test=TEST_ONLY))
                    # assume filled (test) / or optimistic fill for MVP
                    pos.qty = qty
                    pos.avg_price = price_rounded
                    pos.state = "long"
                    pos.target_price = await mexc.round_price(sym, _tp_price(pos.avg_price))
                    pos.stop_price = (await mexc.round_price(sym, pos.avg_price * (1.0 - SL_PCT))
                                      if SL_PCT > 0 else None)
                    await s.commit()

                    if broadcast:
                        await broadcast("recs", {
                            "type": "trade_buy",
                            "symbol": sym,
                            "price": price_rounded,
                            "qty": qty,
                            "tp": pos.target_price,
                            "sl": pos.stop_price
                        })
                except MexcTradeError as e:
                    s.add(MexcOrder(symbol=sym, side="BUY", type="LIMIT",
                                    price=price_rounded, qty=qty,
                                    status="REJECTED", is_test=TEST_ONLY, error=str(e)))
                    await s.commit()
                    if broadcast:
                        await broadcast("recs", {"type": "trade_error", "symbol": sym, "error": str(e)})
                    continue

            # ---------- EXIT: long -> place TP SELL ----------
            if pos.state == "long" and (pos.qty or 0) > 0 and (pos.avg_price or 0) > 0:
                tp = pos.target_price or _tp_price(pos.avg_price)
                tp = await mexc.round_price(sym, tp)

                try:
                    _ = await new_order(
                        symbol=sym,
                        side="SELL",
                        order_type="LIMIT",
                        quantity=pos.qty,
                        price=tp,
                        tif="GTC",
                        test=TEST_ONLY,
                        client_order_id=f"botsell_{int(time.time())}"
                    )
                    s.add(MexcOrder(symbol=sym, side="SELL", type="LIMIT",
                                    price=tp, qty=pos.qty,
                                    status="NEW", is_test=TEST_ONLY))
                    pos.state = "closing"
                    await s.commit()

                    if broadcast:
                        await broadcast("recs", {
                            "type": "trade_sell_tp",
                            "symbol": sym,
                            "tp": tp,
                            "qty": pos.qty
                        })
                except MexcTradeError as e:
                    s.add(MexcOrder(symbol=sym, side="SELL", type="LIMIT",
                                    price=tp, qty=pos.qty,
                                    status="REJECTED", is_test=TEST_ONLY, error=str(e)))
                    await s.commit()
                    if broadcast:
                        await broadcast("recs", {"type": "trade_error", "symbol": sym, "error": str(e)})
                    continue

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
