from __future__ import annotations
import numpy as np
import pandas as pd
import math

def _sf(x):
    """safe float → None for NaN/inf/None, else float"""
    try:
        v = float(x)
    except Exception:
        return None
    return None if (math.isnan(v) or math.isinf(v)) else v

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int=14) -> pd.Series:
    delta = series.diff()
    gain = (delta.clip(lower=0)).rolling(window=period).mean()
    loss = (-delta.clip(upper=0)).rolling(window=period).mean()
    rs = gain / (loss.replace(0, np.nan))
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)

def macd(series: pd.Series, fast: int=12, slow: int=26, signal: int=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    m = ema_fast - ema_slow
    s = ema(m, signal)
    hist = m - s
    return m, s, hist

def atr(df: pd.DataFrame, period: int=14) -> pd.Series:
    # df needs columns: high, low, close
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()

def ta_summary(df: pd.DataFrame) -> dict:
    # assumes df sorted by time asc
    close = df["close"]
    ema20 = ema(close, 20)
    ema50 = ema(close, 50)
    ema200 = ema(close, 200)
    r = rsi(close, 14)
    m, s, h = macd(close, 12, 26, 9)
    a = atr(df, 14)

    latest = len(df)-1
    price = _sf(close.iloc[latest])
    out = {
        "price": _sf(close.iloc[latest]),
        "ema20": _sf(ema20.iloc[latest]),
        "ema50": _sf(ema50.iloc[latest]),
        "ema200": _sf(ema200.iloc[latest]),
        "rsi14": _sf(r.iloc[latest]),
        "macd": _sf(m.iloc[latest]),
        "macd_signal": _sf(s.iloc[latest]),
        "macd_hist": _sf(h.iloc[latest]),
        "atr14": _sf(a.iloc[latest]),
    }
    reasons, score = [], 0.0

    def gt(a, b): return (a is not None) and (b is not None) and (a > b)

    if gt(out["ema20"], out["ema50"]): score += 1; reasons.append("EMA20>EMA50 (short-term uptrend)")
    if gt(out["ema50"], out["ema200"]): score += 1; reasons.append("EMA50>EMA200 (medium uptrend)")
    if gt(out["macd"], out["macd_signal"]): score += 1; reasons.append("MACD>signal (bullish momentum)")
    if out["macd_hist"] is not None and out["macd_hist"] > 0: score += 0.5
    if out["rsi14"] is not None and 45 <= out["rsi14"] <= 65: score += 0.5; reasons.append("RSI in neutral power zone")
    if out["rsi14"] is not None and out["rsi14"] < 35: score -= 1; reasons.append("RSI oversold risks")
    if out["rsi14"] is not None and out["rsi14"] > 70: score -= 1; reasons.append("RSI overbought risks")

    if out["atr14"] and price:
        vr = price / out["atr14"] if out["atr14"] != 0 else None
        out["atr_ratio"] = _sf(vr)
        if out["atr_ratio"] is not None and out["atr_ratio"] < 40:
            score -= 0.5; reasons.append("High volatility (ATR ratio low)")
    else:
        out["atr_ratio"] = None

    out["score"] = _sf(score)
    out["recommendation"] = "BUY" if (out["score"] is not None and out["score"] >= 2.5) else \
                            "ACCUMULATE" if (out["score"] is not None and out["score"] >= 1) else \
                            "AVOID/SELL" if (out["score"] is not None and out["score"] <= -1) else \
                            "HOLD"
    out["reasons"] = reasons
    return out
