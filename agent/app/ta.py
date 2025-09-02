from __future__ import annotations
import numpy as np
import pandas as pd

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
    price = float(close.iloc[latest])
    out = {
        "price": price,
        "ema20": float(ema20.iloc[latest]),
        "ema50": float(ema50.iloc[latest]),
        "ema200": float(ema200.iloc[latest]),
        "rsi14": float(r.iloc[latest]),
        "macd": float(m.iloc[latest]),
        "macd_signal": float(s.iloc[latest]),
        "macd_hist": float(h.iloc[latest]),
        "atr14": float(a.iloc[latest]),
    }

    reasons = []
    score = 0

    if out["ema20"] > out["ema50"]: score += 1; reasons.append("EMA20>EMA50 (short-term uptrend)")
    if out["ema50"] > out["ema200"]: score += 1; reasons.append("EMA50>EMA200 (medium uptrend)")
    if out["macd"] > out["macd_signal"]: score += 1; reasons.append("MACD>signal (bullish momentum)")
    if out["macd_hist"] > 0: score += 0.5
    if 45 <= out["rsi14"] <= 65: score += 0.5; reasons.append("RSI in neutral power zone")
    if out["rsi14"] < 35: score -= 1; reasons.append("RSI oversold risks")
    if out["rsi14"] > 70: score -= 1; reasons.append("RSI overbought risks")

    # volatility sanity: price/atr
    if out["atr14"] > 0:
        vol_ratio = price / out["atr14"]
        out["atr_ratio"] = float(vol_ratio)
        if vol_ratio < 40: score -= 0.5; reasons.append("High volatility (ATR ratio low)")
    else:
        out["atr_ratio"] = None

    # map score -> recommendation
    if score >= 2.5:
        rec = "BUY"
    elif score >= 1:
        rec = "ACCUMULATE"
    elif score <= -1:
        rec = "AVOID/SELL"
    else:
        rec = "HOLD"

    out["score"] = float(score)
    out["recommendation"] = rec
    out["reasons"] = reasons
    return out
