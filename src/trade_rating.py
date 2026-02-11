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

# --- Structures ---

@dataclass
class RateComponents:
    explosion: float
    volume: float
    pullback: float
    support: float
    funding: float
    regime_bonus: float
    microstructure_bonus: float

@dataclass
class RateResult:
    rate: float
    components: RateComponents
    debug: dict

# --- Core Logic ---

def compute_rate(
    change24h_pct: float,
    quote_volume: float,
    volume_rank: float,
    last_price: float,
    # Optional heavy features
    recent_high: Optional[float] = None,
    nearest_support: Optional[float] = None,
    funding_rate: Optional[float] = None,
    # Bonus inputs
    regime_score: float = 1.0,  # 0.0 (bad) to 1.0 (good/neutral)
    wick_ratio: float = 0.0,    # 0.0 to 1.0
    vol_z: float = 0.0,         # z-score
) -> RateResult:
    """
    Compute trade quality rate (0.0 to 10.0).
    Aggressively robust: defaults to 0.0 if critical data missing.
    """
    
    # A) Explosion (0-4 pts)
    # High: >= 40% (4pts), Low: < 8% (0pts)
    abs_chg = abs(change24h_pct)
    explosion_score = 0.0
    if abs_chg >= 8.0:
        explosion_score = clamp((abs_chg - 8) / (40 - 8), 0.0, 1.0) * 4.0

    # B) Volume Rank (0-2 pts)
    volume_score = clamp(volume_rank, 0.0, 1.0) * 2.0

    # C) Pullback (0-2 pts)
    pullback_score = 0.0
    pullback_pct = None
    if recent_high and recent_high > 0 and last_price > 0:
        # Calculate pullback from recent high (for shorts or longs, assumes volatility logic generally)
        # For SHORT context: we want price to be somewhat below high but not too far (rejection)
        # or actually for a breakdown? Use the triangular logic requested: 2-6%, optim 4%
        pullback_pct = (recent_high - last_price) / recent_high
         # If price > recent_high (new high), pullback is negative => 0 score
        if pullback_pct > 0:
            pullback_score = triangular(pullback_pct * 100, left=2.0, peak=4.0, right=8.0) * 2.0

    # D) Support Proximity (0-2 pts)
    support_score = 0.0
    dist_support_pct = None
    if nearest_support and nearest_support > 0 and last_price > 0:
        dist_support_pct = (last_price - nearest_support) / last_price
        # Ideal <= 1.5% distance
        if dist_support_pct >= 0:
             # Score 1.0 at 0% dist, 0.0 at 1.5% dist? Or simpler logic?
             # "Distancia ideal <= 1.5%" -> implied high score if close.
             # Let's map 0%..1.5% -> 2..0 pts linear
             if dist_support_pct <= 0.015:
                 support_score = (1.0 - (dist_support_pct / 0.015)) * 2.0

    # E) Funding Score (-2 to +1 pts)
    # For SHORT: Positive funding = longs pay = good. Negative = shorts pay = bad.
    funding_score = 0.0
    if funding_rate is not None:
        if funding_rate >= 0.005:    # Very positive: longs are crowded (great for short)
            funding_score = 1.0
        elif funding_rate >= 0.002:  # Moderately positive
            funding_score = 0.5
        elif funding_rate <= -0.005: # Very negative: shorts are crowded (bad for short)
            funding_score = -2.0
        elif funding_rate <= -0.002: # Moderately negative
            funding_score = -1.0
    
    # Bonuses
    # 3.1 Regime (0-1 pt). 
    # Logic: If regime_score is low (bad market), we REDUCE total.
    # The requirement says: "si el mercado está demasiado direccional... bajar confianza."
    # "Define regime_score en [0..1] donde 1 = equilibrado, 0 = extremo."
    # "Si ... muy alcista, baja el Rate (aplica como penalty o reduce 0–1 punto)."
    # Let's treat it as a multiplier or a deduction. 
    # Implementation: If regime < 0.5, deduct up to 1 pt.
    regime_penalty = 0.0
    if regime_score < 0.5:
         regime_penalty = -1.0 * (1.0 - regime_score * 2.0) # Linearly scale penalty

    # 3.2 Microstructure (0-1 pt)
    # Wick ratio > 0.3 (mecha grande) implies rejection.
    # Vol Z > 2.0 implies real explosion.
    micro_score = 0.0
    if wick_ratio > 0.3:
        micro_score += 0.5
    if vol_z > 2.0:
        micro_score += 0.5
    micro_score = clamp(micro_score, 0.0, 1.0)

    # Final Sum
    raw_rate = (
        explosion_score + 
        volume_score + 
        pullback_score + 
        support_score + 
        funding_score + 
        regime_penalty + 
        micro_score
    )
    
    final_rate = clamp(raw_rate, 0.0, 10.0)

    debug_data = {
        "pullback_pct": round(pullback_pct * 100, 2) if pullback_pct else None,
        "dist_support_pct": round(dist_support_pct * 100, 2) if dist_support_pct else None,
        "wick_ratio": round(wick_ratio, 2),
        "vol_z": round(vol_z, 2),
        "regime_score": round(regime_score, 2),
        "funding_raw": funding_rate,
        "raw_sum": round(raw_rate, 2) 
    }

    return RateResult(
        rate=round(final_rate, 1),
        components=RateComponents(
            explosion=round(explosion_score, 2),
            volume=round(volume_score, 2),
            pullback=round(pullback_score, 2),
            support=round(support_score, 2),
            funding=round(funding_score, 2),
            regime_bonus=round(regime_penalty, 2), # Storing penalty as negative bonus for visibility
            microstructure_bonus=round(micro_score, 2)
        ),
        debug=debug_data
    )
