"""Trade quality rating system for Binance Futures symbols."""
from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, asdict
from typing import Iterable, Optional

# --- Helpers ---

def clamp(value: float, min_value: float, max_value: float) -> float:
    """Clamp value between min and max."""
    return max(min_value, min(value, max_value))


def triangular(x: float, left: float, peak: float, right: float) -> float:
    """Triangular membership function. Returns 1.0 at peak, 0.0 outside [left, right]."""
    if x <= left or x >= right:
        return 0.0
    if x == peak:
        return 1.0
    if x < peak:
        return (x - left) / (peak - left)
    return (right - x) / (right - peak)


def percentile_rank(values: Iterable[float], value: float) -> float:
    """Return percentile rank of value within values (0.0 to 1.0)."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return 0.5
    count = bisect_right(ordered, value)
    return clamp((count - 1) / (len(ordered) - 1), 0.0, 1.0)


def find_swing_lows(lows: list[float]) -> list[float]:
    """Find local minima (swing lows) in a series of low prices."""
    if len(lows) < 3:
        return []
    swings: list[float] = []
    for i in range(1, len(lows) - 1):
        if lows[i] < lows[i - 1] and lows[i] <= lows[i + 1]:
            swings.append(lows[i])
    return swings

def calculate_rsi(prices: Iterable[float], period: int = 14) -> float:
    """Calculate RSI for a list of prices."""
    prices = list(prices)
    if len(prices) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i-1]
        if delta > 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(delta))
            
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

# --- Structures ---

@dataclass
class RateComponents:
    explosion: float
    volume: float
    rsi: float
    vol_z: float
    funding: float
    regime_bonus: float
    microstructure_bonus: float

@dataclass
class ShortSignal:
    triggered: bool
    confidence: float
    stop_loss: float
    take_profit: float
    reason: str

@dataclass
class RateResult:
    rate: float
    components: RateComponents
    debug: dict
    short_signal: Optional[ShortSignal] = None

# --- Short Signal Logic (New) ---

def check_short_signal(
    k5m: list[dict], 
    last_price: float
) -> Optional[ShortSignal]:
    """
    Check for 'False Breakout' Short Signal.
    Rule: Volume Explosion (>8x MA) + Green Candle + (Wick OR Reversal).
    """
    if not k5m or len(k5m) < 32:
        return None
    
    # 1. Analyze Last Closed Candle (k[-2])
    # k[-1] is current (open). k[-2] is last closed.
    candle = k5m[-2]
    
    # Check 1: Bullish Candle
    c_open = float(candle['open'])
    c_close = float(candle['close'])
    c_high = float(candle['high'])
    c_low = float(candle['low'])
    c_vol = float(candle['volume'])
    
    if c_close <= c_open:
        return None # Must be green (pump)
        
    # Check 2: Volume Explosion (8x MA)
    # MA of previous 30 (excluding analyzed candle? or including?)
    # User: "promedio móvil de volumen de las últimas 30 velas".
    # Let's use window [-32:-2] (30 candles before k[-2]).
    prev_vols = [float(k['volume']) for k in k5m[-32:-2]]
    if not prev_vols:
        return None
    
    ma_vol = sum(prev_vols) / len(prev_vols)
    if ma_vol == 0:
        return None
        
    vol_factor = c_vol / ma_vol
    if vol_factor < 8.0:
        return None # Not explosive enough
        
    # Check 3: Trend Filter (No Clean Uptrend)
    # If last 3 candles ([-3], [-4], [-5]) were all strong green, maybe don't short yet?
    # User: "asegurarse que no hay una secuencia alcista limpia".
    # Implementation: If [-3] and [-4] are Green AND have small wicks -> Clean trend.
    # Heuristic: Allow if [-2] has HUGE wick.
    
    # Wick Calculation
    candle_range = c_high - c_low
    if candle_range == 0:
        return None
        
    top_wick = c_high - c_close
    wick_ratio = top_wick / candle_range
    
    # Rejection Logic
    is_rejection = wick_ratio > 0.25
    
    # False Breakout Simulation
    if not is_rejection:
        # If no wick, maybe check 5m reversal in NEXT candle (current open)? 
        # But we are looking at CLOSED candle [-2]. 
        curr = k5m[-1]
        curr_open = float(curr['open'])
        curr_close = float(curr['close'])
        if curr_close < curr_open:
            pass # Reversal occurring now
        else:
            return None # Still pumping, no wick. Wait.
            
    # Result
    reason = f"Vol {vol_factor:.1f}x + Wick {wick_ratio:.2f}"
    
    # Risk Management
    # SL > Max (e.g. +0.5%)
    sl_price = c_high * 1.005
    # Entry is current price
    entry_price = last_price
    risk = abs(sl_price - entry_price) / entry_price
    if risk == 0: risk = 0.01
    tp_price = entry_price * (1.0 - (risk * 2.0)) # 1:2 Risk Limit
    
    return ShortSignal(
        triggered=True,
        confidence=0.9 if is_rejection else 0.7,
        stop_loss=sl_price,
        take_profit=tp_price,
        reason=reason
    )

# --- Rate Calculation Logic ---

def compute_rate(
    change24h_pct: float,
    quote_volume: float,
    volume_rank: float,
    last_price: float,
    # New heavy features
    rsi_val: float = 50.0,    # RSI 14
    vol_z: float = 0.0,       # Volume Z-Score
    change5m_pct: Optional[float] = None, # 5m Change for reversal detection
    funding_rate: Optional[float] = None,
    # Bonus inputs (legacy or secondary)
    regime_score: float = 1.0, 
    wick_ratio: float = 0.0, 
    # Deprecated/Ignored inputs kept for signature compatibility
    **kwargs
) -> RateResult:
    """
    Compute rate 0..10 based on detecting 'Obvious Manipulation' pumps and subsequent rejection.
    New Focus: Explosion + RSI + Vol Z + Rejection (Wick/Reversal).
    """

    # A) Explosion Score (Max 5.0 pts)
    # Huge pumps (>10%) should be high alert immediately.
    abs_chg = abs(change24h_pct)
    explosion_score = 0.0
    if abs_chg > 2.0:
        # Linear/Log mix to reward huge pumps
        # 5% -> 1.0, 10% -> 2.0, 20% -> 3.0, 30% -> 4.0, 40% -> 5.0
        explosion_score = clamp(abs_chg / 10.0 * 1.5, 0.0, 5.0) 
        if abs_chg > 15:
            explosion_score += 1.0
    # Cap at 5.0
    explosion_score = clamp(explosion_score, 0.0, 5.0)
    
    # --- Momentum Gate (Strict) ---
    # User: "I am interested in the last 1-5 minutes".
    # If 5m change is NOT POSITIVE, zero out the pump score.
    # Exception: Allow strictly flat (0.0) if volume is huge? No, user wants active pump.
    if change5m_pct is not None and change5m_pct <= 0.001: # Less than +0.1%
        # If not moving up NOW, it's not a "GO NOW" Pump.
        explosion_score = 0.0  # NUKE IT.

    # B) Volume Z-Score (Max 3.0 pts)
    # Z > 3 is extreme.
    vol_z_score = 0.0
    if vol_z > 1.0:
        vol_z_score = vol_z * 1.0  # Z=3 -> 3.0 pts.
    vol_z_score = clamp(vol_z_score, 0.0, 3.0)

    # C) RSI Momentum (Max 2.0 pts)
    # RSI > 70 is good. 
    rsi_score = 0.0
    if rsi_val > 55:
        # 55 -> 0.1 ... 70 -> 1.0 ... 85 -> 2.0
        rsi_score = (rsi_val - 55) / 15.0
    rsi_score = clamp(rsi_score, 0.0, 2.0)

    # D) Funding (0.something?)
    funding_score = 1.0 
    if funding_rate is not None and funding_rate < -0.01:
        funding_score = 0.0

    # F) Reversal Score (Bonus 2.0 pts)
    reversal_score = 0.0
    if abs_chg > 5.0 and change5m_pct is not None:
        if change5m_pct < -0.2: # Dropping
             reversal_score = 2.0
    
    # Microstructure (Wick) (Bonus 2.0 pts)
    micro_score = 0.0
    if wick_ratio > 0.25:
        micro_score += 1.0 
    if wick_ratio > 0.5:
        micro_score += 1.0
    
    # D item alternative: Volume Rank (0.5 pts)
    vol_rank_score = clamp(volume_rank, 0.0, 1.0) * 0.5

    # Bonuses
    regime_penalty = 0.0
    if regime_score < 0.3:
        regime_penalty = -1.0 
        
    # --- Trend Decay Penalty (New) ---
    # User Requirement: "Prime" only. Warn if it's dropping from High.
    # If price is > 3% below recent_high, it's stale/dumping.
    trend_penalty = 0.0
    recent_high = kwargs.get('recent_high')
    if recent_high and recent_high > 0:
        pullback = (recent_high - last_price) / recent_high
        if pullback > 0.03: # Down 3% from top
            trend_penalty = -2.0
        if pullback > 0.05: # Down 5% from top
            trend_penalty = -4.0

    # Final Sum
    # Expl(5) + VolZ(3) + RSI(2) + Funding(1) = 11.0 (Pure Pump)
    # Reversal/Wick are bonuses to push it to 10 if weak pump, or keep it 10 if strong pump type.
    
    raw_rate = (
        explosion_score +          # Max 5.0
        vol_z_score +              # Max 3.0
        rsi_score +                # Max 2.0
        reversal_score +           # Max 2.0 (Bonus)
        micro_score +              # Max 2.0 (Bonus)
        vol_rank_score +           # Max 0.5
        funding_score +            # Max 1.0
        regime_penalty +
        trend_penalty              # Stale Pump Filter
    )
    
    final_rate = clamp(raw_rate, 0.0, 10.0)

    debug_data = {
        "rsi": round(rsi_val, 2),
        "vol_z": round(vol_z, 2),
        "wick": round(wick_ratio, 2),
        "rev_5m": round(change5m_pct, 2) if change5m_pct is not None else None,
        "pullback": round(pullback, 3) if (recent_high and recent_high > 0) else 0.0
    }

    return RateResult(
        rate=round(final_rate, 1),
        components=RateComponents(
            explosion=round(explosion_score, 2),
            volume=round(reversal_score, 2), # Using 'volume' field for 'Reversal'
            rsi=round(rsi_score, 2),
            vol_z=round(vol_z_score, 2),
            funding=round(funding_score, 2),
            regime_bonus=round(regime_penalty, 2),
            microstructure_bonus=round(micro_score, 2)
        ),
        debug=debug_data,
        short_signal=None
    )
