from __future__ import annotations
import os, math, asyncio, time
from . import mexc
from .ta import atr 
from .mexc_signed import new_order, query_order, account_info, MexcTradeError
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

def _tp_price(buy_price: float,
              maker_fee: float = MAKER_FRAC,
              taker_fee: float = TAKER_FRAC) -> float:
    return buy_price * (1.0 + maker_fee) * (1.0 + TP_PCT) / (1.0 - taker_fee)

async def _is_tradable(symbol: str) -> tuple[bool, dict]:
    info = await mexc.exchange_info([symbol.upper()])
    arr = info.get("symbols") or []
    if not arr:
        return False, {"reason": "unknown symbol"}
    s = arr[0]
    ok = str(s.get("status")) == "1" and bool(s.get("isSpotTradingAllowed", False))
    return ok, {
        "status": s.get("status"),
        "isSpotTradingAllowed": s.get("isSpotTradingAllowed"),
        "base": s.get("baseAsset"),
        "quote": s.get("quoteAsset"),
    }

async def trader_tick(symbols: list[str], interval: str = "60m", broadcast=None) -> None:
    if not TRADE_ENABLED:
        return

    # 0) Get last closes
    closes: dict[str, float] = {}
    for sym in symbols:
        try:
            df = await mexc.klines(sym, interval=interval, limit=2)
            closes[sym] = float(df["close"].iloc[-1])
        except Exception:
            continue

    # 1) Load recs cache
    from .recs import get_latest
    snap = get_latest() or {}
    rec_map = {r["symbol"]: r for r in snap.get("results", [])}

    # 2) Live balances (once per tick)
    quote_free: dict[str, float] = {}
    base_free: dict[str, float] = {}
    if not TEST_ONLY:
        try:
            acct = await account_info()
            bals = acct.get("balances", [])
            # map by asset symbol
            free_map = { (b.get("asset") or "").upper(): float(b.get("free", 0)) for b in bals }
            for sym in symbols:
                base, quote = await mexc.base_quote(sym)
                quote_free[sym] = free_map.get(quote.upper(), 0.0)
                base_free[sym]  = free_map.get(base.upper(), 0.0)
        except Exception as e:
            if broadcast:
                await broadcast("recs", {"type":"trade_error","error":f"account_info failed: {e}"})
            return

    async with SessionLocal() as s:
        # ensure Position rows
        for sym in symbols:
            pos = await s.get(Position, sym)
            if not pos:
                s.add(Position(symbol=sym, qty=0.0, state="flat"))
        await s.commit()

        # process symbols
        for sym in symbols:
            # Skip if not tradable by API
            tradable, meta = await _is_tradable(sym)
            if not tradable:
                if broadcast:
                    await broadcast("recs", {"type":"trade_skip","symbol":sym,"reason":"not tradable by API","meta":meta})
                continue

            price = closes.get(sym)
            if not price:
                continue

            pos: Position = await s.get(Position, sym)
            rec_entry = rec_map.get(sym) or {}
            recommendation = rec_entry.get("recommendation", "HOLD")

            # ---------- STATE: opening (live only) -> check fill ----------
            if not TEST_ONLY and pos.state == "opening" and pos.last_buy_order:
                try:
                    od = await query_order(symbol=sym, order_id=pos.last_buy_order)
                    st = (od.get("status") or "").upper()
                    # common statuses: NEW, PARTIALLY_FILLED, FILLED, CANCELED, REJECTED
                    if st == "FILLED":
                        # trust executedQty if present; else use base_free snapshot
                        exec_qty = od.get("executedQty")
                        qty = float(exec_qty) if exec_qty is not None else base_free.get(sym, 0.0)
                        pos.qty = await mexc.round_qty(sym, qty)
                        # price: if cummulativeQuoteQty present, avg = cq / qty
                        cq = od.get("cummulativeQuoteQty")
                        if cq is not None and qty > 0:
                            pos.avg_price = await mexc.round_price(sym, float(cq) / qty)
                        else:
                            pos.avg_price = await mexc.round_price(sym, price)
                        pos.state = "long"
                        await s.commit()
                        if broadcast:
                            await broadcast("recs", {"type":"trade_filled_buy","symbol":sym,"qty":pos.qty,"avg_price":pos.avg_price})
                    elif st in ("CANCELED","REJECTED"):
                        # reset to flat
                        pos.state = "flat"
                        pos.last_buy_order = None
                        await s.commit()
                        if broadcast:
                            await broadcast("recs", {"type":"trade_buy_cancelled","symbol":sym,"status":st})
                except MexcTradeError as e:
                    # transient errors → try next tick
                    if broadcast:
                        await broadcast("recs", {"type":"trade_error","symbol":sym,"error":str(e)})

            # ---------- ENTRY: flat + BUY/ACCUMULATE ----------
            if pos.state == "flat" and recommendation in ("BUY", "ACCUMULATE"):
                # volatility gate
                atr_ratio = rec_entry.get("atr_ratio")
                if atr_ratio is None:
                    try:
                        df2 = await mexc.klines(sym, interval=interval, limit=50)
                        a = atr(df2, 14).iloc[-1]
                        if a and a > 0:
                            atr_ratio = float(df2["close"].iloc[-1] / a)
                    except Exception:
                        atr_ratio = None
                if atr_ratio is not None and atr_ratio < VOL_RATIO_MIN:
                    if broadcast:
                        await broadcast("recs", {"type":"trade_skip","symbol":sym,"reason":f"atr_ratio {atr_ratio:.1f} < {VOL_RATIO_MIN}"})
                    continue

                # sizing with live balance cap
                price_r = await mexc.round_price(sym, price)
                # budget cap: policy and balance (live)
                budget = min(MAX_USDT, 1e12)
                if not TEST_ONLY:
                    avail = max(quote_free.get(sym, 0.0) * (1.0 - RESERVE_PCT), 0.0)
                    if avail <= 0:
                        if broadcast:
                            await broadcast("recs", {"type":"trade_skip","symbol":sym,"reason":"no quote balance"})
                        continue
                    budget = min(budget, avail)
                qty = await mexc.size_order(sym, price_r, budget)
                if qty <= 0:
                    if broadcast:
                        await broadcast("recs", {"type":"trade_skip","symbol":sym,"reason":"budget below minNotional/stepSize"})
                    continue

                try:
                    # place BUY
                    resp = await new_order(
                        symbol=sym, side="BUY", order_type="LIMIT",
                        quantity=qty, price=price_r, tif="GTC", test=TEST_ONLY,
                        client_order_id=f"botbuy_{int(time.time())}"
                    )
                    s.add(MexcOrder(symbol=sym, side="BUY", type="LIMIT",
                                    price=price_r, qty=qty,
                                    status="NEW", is_test=TEST_ONLY,
                                    mexc_order_id=resp.get("orderId") if isinstance(resp, dict) else None))
                    if TEST_ONLY:
                        # assume immediate fill in test
                        pos.qty = qty
                        pos.avg_price = price_r
                        pos.state = "long"
                        pos.target_price = await mexc.round_price(sym, _tp_price(pos.avg_price))
                        pos.stop_price = (await mexc.round_price(sym, pos.avg_price * (1.0 - SL_PCT))
                                          if SL_PCT > 0 else None)
                    else:
                        # live: wait for fill
                        pos.avg_price = price_r
                        pos.qty = 0.0
                        pos.state = "opening"
                        pos.last_buy_order = resp.get("orderId") if isinstance(resp, dict) else None
                    await s.commit()
                    if broadcast:
                        await broadcast("recs", {"type":"trade_buy_placed","symbol":sym,"price":price_r,"qty":qty,"mode":"test" if TEST_ONLY else "live"})
                except MexcTradeError as e:
                    s.add(MexcOrder(symbol=sym, side="BUY", type="LIMIT",
                                    price=price_r, qty=qty,
                                    status="REJECTED", is_test=TEST_ONLY, error=str(e)))
                    await s.commit()
                    if broadcast:
                        await broadcast("recs", {"type":"trade_error","symbol":sym,"error":str(e)})
                    continue

            # ---------- EXIT: place TP only when we actually hold base ----------
            if pos.state in ("long",) and (pos.qty or 0) > 0 and (pos.avg_price or 0) > 0:
                tp = await mexc.round_price(sym, _tp_price(pos.avg_price))

                # in live mode, confirm we have base; trim qty if needed
                qty = pos.qty
                if not TEST_ONLY:
                    free_base = base_free.get(sym, 0.0)
                    if free_base <= 0:
                        # nothing to sell yet; will try next tick
                        continue
                    if qty > free_base:
                        qty = await mexc.round_qty(sym, free_base)
                        if qty <= 0:
                            continue

                try:
                    resp = await new_order(
                        symbol=sym, side="SELL", order_type="LIMIT",
                        quantity=qty, price=tp, tif="GTC", test=TEST_ONLY,
                        client_order_id=f"botsell_{int(time.time())}"
                    )
                    s.add(MexcOrder(symbol=sym, side="SELL", type="LIMIT",
                                    price=tp, qty=qty,
                                    status="NEW", is_test=TEST_ONLY,
                                    mexc_order_id=resp.get("orderId") if isinstance(resp, dict) else None))
                    pos.state = "closing" if not TEST_ONLY else "closing"
                    pos.last_sell_order = resp.get("orderId") if isinstance(resp, dict) else None
                    await s.commit()
                    if broadcast:
                        await broadcast("recs", {"type":"trade_sell_tp_placed","symbol":sym,"tp":tp,"qty":qty,"mode":"test" if TEST_ONLY else "live"})
                except MexcTradeError as e:
                    s.add(MexcOrder(symbol=sym, side="SELL", type="LIMIT",
                                    price=tp, qty=qty,
                                    status="REJECTED", is_test=TEST_ONLY, error=str(e)))
                    await s.commit()
                    if broadcast:
                        await broadcast("recs", {"type":"trade_error","symbol":sym,"error":str(e)})
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
