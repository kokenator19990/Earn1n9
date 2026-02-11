from __future__ import annotations

import json
from typing import Any
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .explosion_monitor import ExplosionMonitor
from .scanner import Scanner
from .storage import Storage
from .trade_rating import compute_rate, find_swing_lows, percentile_rank


def create_app(scanner: Scanner, storage: Storage, monitor: ExplosionMonitor) -> FastAPI:
    """Create the FastAPI dashboard app."""
    app = FastAPI(title="19MoneyScanner", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def _get_recent_high(symbol: str) -> float | None:
        """Get recent high from 1m or 1h klines."""
        klines_1m = scanner.get_cached_klines(symbol, "1m")
        if klines_1m:
            highs = [row["high"] for row in klines_1m[-60:]]
            return max(highs) if highs else None

        klines_1h = scanner.get_cached_klines(symbol, "1h")
        if klines_1h:
            highs = [row["high"] for row in klines_1h[-6:]]
            return max(highs) if highs else None

        return None

    def _get_nearest_support(symbol: str, last_price: float) -> float | None:
        """Get nearest support level below current price."""
        supports: list[float] = []
        for interval in ("1h", "1d"):
            klines = scanner.get_cached_klines(symbol, interval)
            lows = [row["low"] for row in klines]
            supports.extend(find_swing_lows(lows))

        below = [level for level in supports if level <= last_price]
        return max(below) if below else None

    def _build_rate_data(
        top_rows: list[dict[str, Any]],
        symbols_status: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Build rate data for all top symbols."""
        volume_values = [row["quoteVolume"] for row in top_rows]
        funding_by_symbol = {
            row["symbol"]: row.get("fundingRate") for row in symbols_status
        }

        rate_data: dict[str, dict[str, Any]] = {}
        for row in top_rows:
            symbol = row["symbol"]
            last_price = row["lastPrice"]
            recent_high = _get_recent_high(symbol)
            nearest_support = _get_nearest_support(symbol, last_price)
            volume_rank = percentile_rank(volume_values, row["quoteVolume"])
            res = compute_rate(
                change24h_pct=row["priceChangePercent"],
                quote_volume=row["quoteVolume"],
                volume_rank=volume_rank,
                last_price=last_price,
                recent_high=recent_high,
                nearest_support=nearest_support,
                funding_rate=funding_by_symbol.get(symbol),
            )
            rate_data[symbol] = {
                "rate": res.rate,
                "components": asdict(res.components),
                "debug": res.debug
            }
        return rate_data

    @app.get("/top/recent-entries")
    def top_recent_entries() -> dict[str, Any]:
        """Get symbols that recently entered Top N."""
        return {"recent_entries": scanner.get_recent_entries()}

    @app.get("/symbols/status")
    def symbols_status_endpoint() -> dict[str, Any]:
        """Get symbol statuses with rate data."""
        top_rows = scanner.get_top()
        statuses = monitor.get_symbol_statuses()
        rate_data = _build_rate_data(top_rows, statuses)

        results: list[dict[str, Any]] = []
        for row in statuses:
            symbol = row["symbol"]
            rate_info = rate_data.get(symbol, {})
            results.append(
                {
                    **row,
                    "rate": rate_info.get("rate"),
                    "pullback_pct": rate_info.get("pullback_pct"),
                    "nearest_support": rate_info.get("nearest_support"),
                    "dist_support_pct": rate_info.get("dist_support_pct"),
                }
            )

        return {"symbols": results}

    @app.get("/short_setups/latest")
    def short_setups_latest(limit: int = 50) -> dict[str, Any]:
        """Get latest short setups with rate data."""
        top_rows = scanner.get_top()
        statuses = monitor.get_symbol_statuses()
        rate_data = _build_rate_data(top_rows, statuses)
        short_setups = storage.get_latest_events_by_type("SHORT_SETUP", limit=limit)

        enriched: list[dict[str, Any]] = []
        for row in short_setups:
            symbol = row["symbol"]
            rate_info = rate_data.get(symbol, {})
            enriched.append(
                {
                    **row,
                    "rate": rate_info.get("rate"),
                    "pullback_pct": rate_info.get("pullback_pct"),
                    "nearest_support": rate_info.get("nearest_support"),
                    "dist_support_pct": rate_info.get("dist_support_pct"),
                }
            )

        return {"short_setups": enriched}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        # --- Gather data ---
        alerts = storage.get_latest_alerts(limit=50)
        events = storage.get_latest_events(limit=50)
        short_setups = storage.get_latest_events_by_type("SHORT_SETUP", limit=50)
        symbols_status = monitor.get_symbol_statuses()
        top_rows = scanner.get_top()
        rate_data_map = _build_rate_data(top_rows, symbols_status)

        # Helper: Binance Futures link for a symbol
        def binance_link(symbol: str) -> str:
            return f'<a href="#" onclick="openBinance(\'{symbol}\'); return false;" class="binance-link" title="{symbol}">🪐</a>'
        
        # Rate helpers
        def get_rate(sym: str) -> float:
            info = rate_data_map.get(sym, {})
            return info.get("rate", 0.0) if isinstance(info, dict) else 0.0

        rate_map_for_scanner = {s: d.get("rate", 0.0) for s, d in rate_data_map.items()}
        
        # GO NOW: ALL top symbols with rate >= 4.0 (no age filter)
        go_now_candidates = []
        for row in top_rows:
            sym = row["symbol"]
            r_info = rate_data_map.get(sym, {})
            rate_val = r_info.get("rate", 0.0) if isinstance(r_info, dict) else 0.0
            short_sig = r_info.get("short_signal")
            
            # GO NOW Rule: Rate >= 4.0 OR Triggered Short Signal
            if rate_val >= 4.0 or (short_sig and short_sig.get('triggered')):
                go_now_candidates.append({
                    "symbol": sym,
                    "rate": rate_val,
                    "components": r_info.get("components", {}),
                    "debug": r_info.get("debug", {}),
                    "short_signal": short_sig,
                })

        # --- DEFINE ALL HELPERS BEFORE USE ---

        # Funding map & tag
        funding_map = {row["symbol"]: row.get("fundingRate") for row in symbols_status}

        def get_funding_tag(rate_val):
            if rate_val is None:
                return "—"
            if abs(rate_val) < 0.0001:
                return '<span class="badge badge-neutral">NEUTRAL</span>'
            if rate_val > 0.0001:
                return '<span class="badge badge-success">EARN</span>'
            return '<span class="badge badge-danger">PAY</span>'

        def get_5m_change(symbol):
            klines = scanner.get_cached_klines(symbol, "1m")
            if not klines or len(klines) < 5:
                return None
            try:
                current = klines[-1]["close"]
                prev = klines[-min(len(klines), 6)]["close"]
                return ((current - prev) / prev) * 100
            except Exception:
                return None

        def get_feature(row, key):
            metrics = row.get("metrics", {})
            val = metrics.get(key)
            if val is None:
                return "—"
            if isinstance(val, (int, float)):
                if "ret_" in key:
                    color_class = "positive" if val > 0 else "negative"
                    return f'<span class="{color_class}">{val:+.2f}%</span>'
                if "vol_z" in key:
                    return f"{val:.1f}"
                return f"{val:.2f}"
            return str(val)

        def get_rate_html(row):
            rate = row.get("rate", 0.0)
            comps = row.get("components", {}) or {}
            short_sig = row.get("short_signal")

            if rate >= 7.5:
                badge_class = "rate-fire"
            elif rate >= 5.5:
                badge_class = "rate-high"
            elif rate >= 4.0:
                badge_class = "rate-medium"
            else:
                badge_class = "rate-low"

            tooltip_content = (
                f"<div class='tt-row'><span class='tt-label'>Explosion</span><span class='tt-val'>{comps.get('explosion',0)}</span></div>"
                f"<div class='tt-row'><span class='tt-label'>Vol Rank</span><span class='tt-val'>{comps.get('volume',0)}</span></div>"
                f"<div class='tt-row'><span class='tt-label'>RSI</span><span class='tt-val'>{comps.get('rsi',0)}</span></div>"
                f"<div class='tt-row'><span class='tt-label'>Vol Z</span><span class='tt-val'>{comps.get('vol_z',0)}</span></div>"
                f"<div class='tt-row'><span class='tt-label'>Funding</span><span class='tt-val'>{comps.get('funding',0)}</span></div>"
                f"<div class='tt-sep'></div>"
                f"<div class='tt-row'><span class='tt-label'>Regime</span><span class='tt-val'>{comps.get('regime_bonus',0)}</span></div>"
                f"<div class='tt-row'><span class='tt-label'>Micro</span><span class='tt-val'>{comps.get('microstructure_bonus',0)}</span></div>"
            )
            
            if short_sig and short_sig.get('triggered'):
                sl = short_sig.get('stop_loss', 0)
                tp = short_sig.get('take_profit', 0)
                reason = short_sig.get('reason', '')
                tooltip_content += (
                    f"<div class='tt-sep'></div>"
                    f"<div class='tt-row warning'><span class='tt-label'>SHORT ALERT</span></div>"
                    f"<div class='tt-row'><span class='tt-label'>Stop Loss</span><span class='tt-val'>{sl:.4f}</span></div>"
                    f"<div class='tt-row'><span class='tt-label'>Take Profit</span><span class='tt-val'>{tp:.4f}</span></div>"
                    f"<div class='tt-row'><span class='tt-label'>Reason</span><span class='tt-val'>{reason}</span></div>"
                )
                # Override badge or append
                return (
                    f'<div class="tooltip-wrap">'
                    f'<span class="rate-badge rate-fire">SHORT</span>'
                    f'<div class="tooltip-box">{tooltip_content}</div>'
                    f'</div>'
                )

            return (
                f'<div class="tooltip-wrap">'
                f'<span class="rate-badge {badge_class}">{rate:.1f}</span>'
                f'<div class="tooltip-box">{tooltip_content}</div>'
                f'</div>'
            )

        # Recent entries
        recent_entries = scanner.get_recent_entries()

        # --- BUILD TABLES ---

        # GO NOW: all qualified symbols sorted by Rate DESC
        go_now_sorted = sorted(go_now_candidates, key=lambda r: (-r.get("rate", 0.0)))
        
        go_now_rows = ""
        for row in go_now_sorted:
            symbol = row["symbol"]
            rate = row.get("rate", 0.0)
            
            # Determine urgency level
            if rate >= 7.5:
                alert_class = "go-fire"
                alert_icon = "🔥"
                alert_label = "GO NOW"
            elif rate >= 5.5:
                alert_class = "go-hot"
                alert_icon = "⚡"
                alert_label = "GO"
            else:
                alert_class = "go-watch"
                alert_icon = "👁️"
                alert_label = "WATCH"
            
            # Updated Metrics (User Request: 15m % and 3h Growth)
            metrics = row.get("metrics", {})
            
            # 15m %
            ret_15m = metrics.get("ret_15m")
            chg_15m_html = "—"
            if ret_15m is not None:
                pct_15 = ret_15m * 100.0
                color_class = "positive" if pct_15 > 0 else "negative"
                chg_15m_html = f'<span class="{color_class}">{pct_15:+.2f}%</span>'

            # 3h Growth (Ret 3h)
            ret_3h = metrics.get("ret_3h")
            grow_3h_html = "—"
            if ret_3h is not None:
                # Use strong color for good growth
                pct_3h = ret_3h * 100.0
                color_class = "positive-strong" if pct_3h > 5.0 else ("positive" if pct_3h > 0 else "negative")
                grow_3h_html = f'<span class="{color_class}">{pct_3h:+.2f}%</span>'
            
            go_now_rows += (
                f'<tr class="row-animate {alert_class}-row">'
                f'<td><span class="go-badge {alert_class}">{alert_icon} {alert_label}</span></td>'
                f'<td class="symbol-cell-go">{binance_link(symbol)} {symbol}</td>'
                f'<td>{get_rate_html(row)}</td>'
                f'<td>{get_funding_tag(funding_map.get(symbol))}</td>'
                f'<td>{chg_15m_html}</td>'
                f'<td>{grow_3h_html}</td>'
                f'</tr>'
            )

        go_now_empty = (
            '<tr><td colspan="6" class="empty-state">'
            '<div class="scanning-anim">'
            '<span class="scan-dot"></span>'
            '<span>Scanning for GO setups...</span>'
            '</div>'
            '</td></tr>'
        )

        # Market Hot Spots
        recent_entries_table = ""
        for row in recent_entries:
            symbol = row["symbol"]
            chg = get_5m_change(symbol)
            chg_class = "positive-strong" if chg and chg > 0 else "negative"
            chg_text = f"{chg:+.2f}%" if chg is not None else "—"
            recent_entries_table += (
                f'<tr class="row-animate">'
                f'<td class="symbol-cell"><span style="font-size:1.2rem">🔥</span> {binance_link(symbol)} {symbol}</td>'
                f'<td class="{chg_class}">{chg_text}</td>'
                f'<td>${row["quoteVolume"]:,.0f}</td>'
                f'<td>{get_funding_tag(funding_map.get(symbol))}</td>'
                f'<td style="color:var(--text-secondary)">{row["entryTimestamp"][-8:]}</td>'
                f'</tr>'
            )

        alert_table = "".join(
            "<tr>"
            f"<td>{binance_link(row['symbol'])} {row['symbol']}</td>"
            f"<td><span class=\"badge badge-new\">{row['alert_type']}</span></td>"
            f"<td>{row['created_at'][-8:]}</td>"
            "</tr>"
            for row in alerts
        )

        event_table = "".join(
            "<tr>"
            f"<td>{binance_link(row['symbol'])} {row['symbol']}</td>"
            f"<td>{row['type']}</td>"
            f"<td>{row['created_at'][-8:]}</td>"
            "</tr>"
            for row in events
        )

        # Short Setups
        short_setups_with_rate = []
        for row in short_setups:
            rate_val = get_rate(row["symbol"])
            row_copy = dict(row)
            row_copy["rate"] = rate_val
            short_setups_with_rate.append(row_copy)
        
        short_table = "".join(
            "<tr>"
            f"<td>{binance_link(row['symbol'])} {row['symbol']}</td>"
            f"<td>{row['created_at'][-8:]}</td>"
            f"<td><span class=\"rate-badge rate-medium\">{row['rate']:.1f}</span></td>"
            "</tr>"
            for row in short_setups_with_rate
        )

        # Symbol Status
        symbols_status_with_rate = []
        for row in symbols_status:
            r_info = rate_data_map.get(row["symbol"], {})
            row_copy = dict(row)
            row_copy["rate"] = r_info.get("rate", 0.0) if isinstance(r_info, dict) else 0.0
            symbols_status_with_rate.append(row_copy)
        
        status_table = "".join(
            "<tr>"
            f"<td>{binance_link(row['symbol'])} {row['symbol']}</td>"
            f"<td><span class=\"badge { 'badge-new' if row['status'] == 'READY_SHORT' else 'badge-stable' }\">{row['status']}</span></td>"
            f"<td class=\"price\">{row['lastPrice']}</td>"
            f"<td>{row['rate']:.1f}</td>"
            "</tr>"
            for row in symbols_status_with_rate
        )

        num_go = len(go_now_sorted)
        num_fire = sum(1 for r in go_now_sorted if r.get("rate", 0) >= 7.5)

        return HTMLResponse(
            f'''
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <title>PerrochicoApp - Pro Terminal</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&display=swap" rel="stylesheet">
            <style>
                :root {{
                    --bg-dark: #0f172a;
                    --bg-card: rgba(30, 41, 59, 0.7);
                    --text-primary: #f8fafc;
                    --text-secondary: #94a3b8;
                    --accent-primary: #8b5cf6;
                    --accent-secondary: #06b6d4;
                    --success: #10b981;
                    --danger: #ef4444;
                    --warning: #f59e0b;
                    --glass-border: 1px solid rgba(255, 255, 255, 0.1);
                    --glass-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
                }}

                * {{ margin: 0; padding: 0; box-sizing: border-box; }}

                body {{
                    font-family: 'Inter', sans-serif;
                    background-color: var(--bg-dark);
                    background-image: 
                        radial-gradient(at 0% 0%, hsla(253,16%,7%,1) 0, transparent 50%), 
                        radial-gradient(at 50% 0%, hsla(225,39%,30%,1) 0, transparent 50%), 
                        radial-gradient(at 100% 0%, hsla(339,49%,30%,1) 0, transparent 50%);
                    color: var(--text-primary);
                    min-height: 100vh;
                    padding: 2rem;
                    overflow-x: hidden;
                }}

                ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
                ::-webkit-scrollbar-track {{ background: var(--bg-dark); }}
                ::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 4px; }}
                ::-webkit-scrollbar-thumb:hover {{ background: #475569; }}

                .container {{ max-width: 1600px; margin: 0 auto; }}

                .hero {{
                    text-align: center;
                    margin-bottom: 3rem;
                    position: relative;
                }}

                .dog-container {{
                    font-size: 9rem;
                    line-height: 1;
                    margin-bottom: 0.5rem;
                    display: inline-block;
                    filter: drop-shadow(0 0 20px rgba(139, 92, 246, 0.5));
                    animation: float 6s ease-in-out infinite;
                    cursor: pointer;
                    transition: transform 0.3s;
                }}
                .dog-container:hover {{
                    transform: scale(1.1) rotate(5deg);
                    filter: drop-shadow(0 0 40px rgba(139, 92, 246, 0.8));
                }}

                @keyframes float {{
                    0% {{ transform: translateY(0px); }}
                    50% {{ transform: translateY(-15px); }}
                    100% {{ transform: translateY(0px); }}
                }}

                h1 {{
                    font-size: 3rem;
                    font-weight: 800;
                    background: linear-gradient(to right, #c084fc, #6366f1, #38bdf8);
                    -webkit-background-clip: text;
                    background-clip: text;
                    color: transparent;
                    margin-bottom: 0.3rem;
                    letter-spacing: -0.05em;
                }}

                .subtitle {{
                    color: var(--text-secondary);
                    font-size: 1rem;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    gap: 10px;
                }}

                .live-dot {{
                    width: 10px; height: 10px;
                    background-color: var(--success);
                    border-radius: 50%;
                    box-shadow: 0 0 10px var(--success);
                    animation: pulse 2s infinite;
                }}
                @keyframes pulse {{
                    0% {{ opacity: 1; transform: scale(1); }}
                    50% {{ opacity: 0.5; transform: scale(0.9); }}
                    100% {{ opacity: 1; transform: scale(1); }}
                }}

                /* === CARD === */
                .card {{
                    background: var(--bg-card);
                    backdrop-filter: blur(12px);
                    -webkit-backdrop-filter: blur(12px);
                    border: var(--glass-border);
                    border-radius: 20px;
                    padding: 1.5rem;
                    margin-bottom: 2rem;
                    box-shadow: var(--glass-shadow);
                }}

                /* === GO NOW CARD (special) === */
                .card-go {{
                    background: linear-gradient(135deg, rgba(239,68,68,0.08), rgba(249,115,22,0.08), rgba(30,41,59,0.9));
                    border: 1px solid rgba(239, 68, 68, 0.25);
                    position: relative;
                    overflow: hidden;
                }}
                .card-go::before {{
                    content: '';
                    position: absolute;
                    top: 0; left: -100%;
                    width: 200%; height: 2px;
                    background: linear-gradient(90deg, transparent, #ef4444, #f59e0b, transparent);
                    animation: scanLine 3s linear infinite;
                }}
                @keyframes scanLine {{
                    0% {{ left: -100%; }}
                    100% {{ left: 100%; }}
                }}

                .card-header {{
                    display: flex;
                    justify-content: space-between;
                    align-items: center;
                    margin-bottom: 1.5rem;
                    border-bottom: 1px solid rgba(255,255,255,0.05);
                    padding-bottom: 1rem;
                }}

                .card-title {{
                    font-size: 1.4rem;
                    font-weight: 700;
                    color: var(--text-primary);
                    display: flex;
                    align-items: center;
                    gap: 10px;
                }}

                .go-title {{
                    font-size: 1.5rem;
                    font-weight: 900;
                    letter-spacing: 0.05em;
                    background: linear-gradient(to right, #ef4444, #f59e0b);
                    -webkit-background-clip: text;
                    background-clip: text;
                    color: transparent;
                }}

                .icon {{ font-size: 1.5rem; }}

                .controls {{ display: flex; gap: 1rem; align-items: center; }}

                /* === TABLE === */
                .table-responsive {{ overflow-x: auto; }}

                table {{ width: 100%; border-collapse: separate; border-spacing: 0; white-space: nowrap; }}

                th {{
                    text-align: left; padding: 0.8rem 1rem;
                    color: var(--text-secondary);
                    font-weight: 500; font-size: 0.8rem;
                    text-transform: uppercase; letter-spacing: 0.05em;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.1);
                }}

                td {{
                    padding: 0.8rem 1rem;
                    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
                    color: var(--text-primary);
                    font-size: 0.95rem;
                    vertical-align: middle;
                }}

                tr:last-child td {{ border-bottom: none; }}
                tr:hover td {{ background: rgba(255, 255, 255, 0.02); }}

                .row-animate {{ animation: slideIn 0.3s ease-out forwards; }}
                @keyframes slideIn {{
                    from {{ opacity: 0; transform: translateX(-10px); }}
                    to {{ opacity: 1; transform: translateX(0); }}
                }}

                /* GO NOW row highlights */
                .go-fire-row {{ background: rgba(239,68,68,0.06); }}
                .go-fire-row:hover td {{ background: rgba(239,68,68,0.1) !important; }}
                .go-hot-row {{ background: rgba(249,115,22,0.04); }}
                .go-watch-row {{ background: transparent; }}

                .symbol-cell, .symbol-cell-go {{
                    font-weight: 700;
                    color: var(--text-primary);
                }}
                .symbol-cell {{ display: flex; align-items: center; gap: 8px; }}
                .symbol-cell-go {{ font-size: 1.05rem; letter-spacing: 0.02em; }}

                .price {{ font-family: 'Courier New', monospace; }}

                .positive {{ color: var(--success); text-shadow: 0 0 10px rgba(16, 185, 129, 0.3); }}
                .negative {{ color: var(--danger); }}
                .neutral {{ color: var(--text-secondary); }}
                .positive-strong {{ color: var(--success); font-weight: 700; text-shadow: 0 0 15px rgba(16, 185, 129, 0.5); }}

                /* === BADGES === */
                .badge {{
                    padding: 4px 8px; border-radius: 6px;
                    font-size: 0.75rem; font-weight: 600; letter-spacing: 0.05em;
                    display: inline-block;
                }}
                .badge-new {{ background: rgba(16, 185, 129, 0.1); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.2); }}
                .badge-stable {{ background: rgba(148, 163, 184, 0.1); color: var(--text-secondary); border: 1px solid rgba(148, 163, 184, 0.2); }}
                .badge-success {{ background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.25); }}
                .badge-danger {{ background: rgba(239,68,68,0.12); color: #f87171; border: 1px solid rgba(239,68,68,0.25); }}
                .badge-neutral {{ background: rgba(148,163,184,0.1); color: #94a3b8; border: 1px solid rgba(148,163,184,0.2); }}

                /* === GO BADGES === */
                .go-badge {{
                    padding: 6px 14px;
                    border-radius: 8px;
                    font-weight: 800;
                    font-size: 0.85rem;
                    letter-spacing: 0.08em;
                    display: inline-flex;
                    align-items: center;
                    gap: 6px;
                    transition: all 0.2s;
                }}
                .go-fire {{
                    background: linear-gradient(135deg, rgba(239,68,68,0.25), rgba(249,115,22,0.25));
                    color: #fbbf24;
                    border: 1px solid rgba(239,68,68,0.4);
                    box-shadow: 0 0 20px rgba(239,68,68,0.15);
                    animation: goPulse 2s ease-in-out infinite;
                }}
                .go-hot {{
                    background: linear-gradient(135deg, rgba(249,115,22,0.2), rgba(245,158,11,0.2));
                    color: #fb923c;
                    border: 1px solid rgba(249,115,22,0.3);
                    box-shadow: 0 0 12px rgba(249,115,22,0.1);
                }}
                .go-watch {{
                    background: rgba(99,102,241,0.12);
                    color: #a5b4fc;
                    border: 1px solid rgba(99,102,241,0.2);
                }}
                @keyframes goPulse {{
                    0% {{ box-shadow: 0 0 20px rgba(239,68,68,0.15); }}
                    50% {{ box-shadow: 0 0 30px rgba(239,68,68,0.35); }}
                    100% {{ box-shadow: 0 0 20px rgba(239,68,68,0.15); }}
                }}

                /* === RATE BADGES === */
                .rate-badge {{
                    display: inline-block;
                    padding: 4px 10px;
                    border-radius: 8px;
                    font-weight: 700;
                    min-width: 40px;
                    text-align: center;
                    font-size: 0.9rem;
                }}
                .rate-fire {{
                    background: linear-gradient(135deg, rgba(239,68,68,0.2), rgba(249,115,22,0.2));
                    color: #fbbf24;
                    border: 1px solid rgba(239,68,68,0.35);
                    box-shadow: 0 0 10px rgba(239,68,68,0.15);
                }}
                .rate-high {{
                    background: linear-gradient(135deg, rgba(16, 185, 129, 0.2), rgba(6, 182, 212, 0.2));
                    color: #fff;
                    border: 1px solid rgba(16, 185, 129, 0.4);
                    box-shadow: 0 0 10px rgba(16, 185, 129, 0.2);
                }}
                .rate-medium {{
                    background: rgba(245,158,11,0.1);
                    color: #fbbf24;
                    border: 1px solid rgba(245,158,11,0.25);
                }}
                .rate-low {{ color: var(--text-secondary); opacity: 0.6; }}

                /* === TOOLTIP === */
                .tooltip-wrap {{
                    position: relative;
                    display: inline-block;
                    cursor: pointer;
                }}
                .tooltip-box {{
                    visibility: hidden;
                    opacity: 0;
                    position: absolute;
                    bottom: 120%;
                    left: 50%;
                    transform: translateX(-50%);
                    min-width: 180px;
                    background: rgba(15, 23, 42, 0.97);
                    border: 1px solid rgba(255,255,255,0.15);
                    border-radius: 10px;
                    padding: 10px 12px;
                    z-index: 999;
                    transition: all 0.15s;
                    box-shadow: 0 8px 30px rgba(0,0,0,0.5);
                }}
                .tooltip-wrap:hover .tooltip-box {{
                    visibility: visible;
                    opacity: 1;
                }}
                .tt-row {{
                    display: flex;
                    justify-content: space-between;
                    padding: 2px 0;
                    font-size: 0.78rem;
                }}
                .tt-label {{ color: #94a3b8; }}
                .tt-val {{ color: #f8fafc; font-weight: 600; }}
                .tt-sep {{
                    height: 1px;
                    background: rgba(255,255,255,0.1);
                    margin: 4px 0;
                }}

                /* === COUNTER BADGE === */
                .counter-badge {{
                    padding: 6px 14px;
                    border-radius: 10px;
                    font-weight: 700;
                    font-size: 0.9rem;
                }}
                .counter-fire {{
                    background: linear-gradient(135deg, rgba(239,68,68,0.15), rgba(249,115,22,0.15));
                    color: #fbbf24;
                    border: 1px solid rgba(239,68,68,0.3);
                }}
                .counter-zero {{
                    background: rgba(148,163,184,0.08);
                    color: var(--text-secondary);
                    border: 1px solid rgba(148,163,184,0.15);
                }}

                /* === EMPTY STATE === */
                .empty-state {{
                    text-align: center;
                    padding: 2.5rem !important;
                    color: var(--text-secondary);
                }}
                .scanning-anim {{
                    display: flex;
                    align-items: center;
                    justify-content: center;
                    gap: 10px;
                    font-size: 0.95rem;
                }}
                .scan-dot {{
                    width: 8px; height: 8px;
                    background: var(--accent-primary);
                    border-radius: 50%;
                    animation: scanPulse 1.5s ease-in-out infinite;
                }}
                @keyframes scanPulse {{
                    0% {{ opacity: 0.3; transform: scale(0.8); }}
                    50% {{ opacity: 1; transform: scale(1.2); }}
                    100% {{ opacity: 0.3; transform: scale(0.8); }}
                }}

                pre {{
                    background: rgba(0, 0, 0, 0.3);
                    padding: 10px; border-radius: 8px;
                    max-width: 300px; overflow: auto;
                    font-size: 0.8rem; color: #cbd5e1;
                }}
                .footer {{
                    text-align: center; margin-top: 4rem;
                    color: var(--text-secondary);
                    font-size: 0.9rem; padding-bottom: 2rem;
                }}

                /* === BINANCE LINK === */
                .binance-link {{
                    text-decoration: none;
                    font-size: 1.15rem;
                    display: inline-block;
                    transition: transform 0.2s, filter 0.2s;
                    vertical-align: middle;
                    margin-right: 4px;
                }}
                .binance-link:hover {{
                    transform: scale(1.3) rotate(15deg);
                    filter: drop-shadow(0 0 8px rgba(245, 158, 11, 0.6));
                }}

                /* === MOBILE OPTIMIZATION === */
                @media (max-width: 768px) {{
                    body {{ padding: 0.5rem; }}
                    .container {{ padding: 0; }}
                    h1 {{ font-size: 1.8rem; margin-bottom: 0.2rem; }}
                    .subtitle {{ font-size: 0.85rem; flex-wrap: wrap; }}
                    
                    .dog-container {{ font-size: 6rem; }} /* Smaller on mobile */
                    
                    .card {{ padding: 1rem; border-radius: 16px; margin-bottom: 1.5rem; }}
                    .card-header {{ 
                        flex-direction: column; 
                        align-items: flex-start; 
                        gap: 0.8rem;
                        padding-bottom: 0.8rem;
                    }}
                    .controls {{ width: 100%; justify-content: space-between; }}
                    
                    /* Bigger touch targets */
                    .binance-link {{ 
                        font-size: 1.4rem; 
                        padding: 4px;
                        margin-right: 6px; 
                    }}
                    
                    td, th {{ padding: 0.6rem 0.4rem; font-size: 0.8rem; }}
                    .symbol-cell, .symbol-cell-go {{ font-size: 0.9rem; }}
                }}
            </style>

            <script>
                async function updateDashboard() {{
                    try {{
                        const [recentRes, statusRes] = await Promise.all([
                            fetch('/top/recent-entries'),
                            fetch('/symbols/status')
                        ]);
                        const recentData = await recentRes.json();
                        updateRecentEntries(recentData.recent_entries);
                    }} catch (e) {{
                        console.error("Update failed", e);
                    }}
                }}

                function updateRecentEntries(entries) {{
                    const tbody = document.getElementById('recent-entries-body');
                    if (!entries || entries.length === 0) return;
                    const html = entries.map(row => {{
                        const changeClass = row.priceChangePercent > 0 ? 'positive-strong' : 'negative';
                        return `
                            <tr class="row-animate">
                                <td class="symbol-cell">
                                    <span style="font-size:1.2rem">🔥</span>
                                    ${{row.symbol}}
                                </td>
                                <td class="price">${{formatNumber(row.lastPrice)}}</td>
                                <td class="${{changeClass}}">${{formatNumber(row.priceChangePercent)}}%</td>
                                <td>$${{formatNumber(row.quoteVolume, 0)}}</td>
                                <td style="color:var(--text-secondary)">${{row.entryTimestamp.slice(11, 19)}}</td>
                            </tr>
                        `;
                    }}).join('');
                    tbody.innerHTML = html;
                }}

                function formatNumber(num, decimals=2) {{
                    if (num === null || num === undefined) return '—';
                    return new Intl.NumberFormat('en-US', {{ minimumFractionDigits: decimals, maximumFractionDigits: decimals }}).format(num);
                }}

                setInterval(updateDashboard, 5000);
                setTimeout(() => window.location.reload(), 45000);

                // Bark sound using Web Audio API (no external files needed)
                // Realistic Bark Sound (Noise + Filter)
                function bark() {{
                    try {{
                        const AudioContext = window.AudioContext || window.webkitAudioContext;
                        if (!AudioContext) return;
                        const ctx = new AudioContext();
                        
                        function playWoof(time, freq, duration) {{
                            // Create noise buffer
                            const bufferSize = ctx.sampleRate * 2.0;
                            const buffer = ctx.createBuffer(1, bufferSize, ctx.sampleRate);
                            const data = buffer.getChannelData(0);
                            for (let i = 0; i < bufferSize; i++) {{
                                data[i] = Math.random() * 2 - 1;
                            }}

                            const noise = ctx.createBufferSource();
                            noise.buffer = buffer;

                            // Bandpass filter for "woof" tonality
                            const filter = ctx.createBiquadFilter();
                            filter.type = 'bandpass';
                            filter.frequency.value = freq;
                            filter.Q.value = 1.5;

                            // Envelope
                            const gain = ctx.createGain();
                            gain.gain.setValueAtTime(0, time);
                            gain.gain.linearRampToValueAtTime(1, time + 0.05);
                            gain.gain.exponentialRampToValueAtTime(0.01, time + duration);

                            noise.connect(filter);
                            filter.connect(gain);
                            gain.connect(ctx.destination);

                            noise.start(time);
                            noise.stop(time + duration);
                        }}

                        const now = ctx.currentTime;
                        playWoof(now, 400, 0.25);       // First woof
                        playWoof(now + 0.3, 350, 0.3);  // Second woof (lower)
                        
                    }} catch(e) {{ console.error('Bark error:', e); }}
                }}
            </script>
        </head>
        <body>
            <div class="container">

                <div class="hero">
                    <div class="dog-container" onclick="bark(); this.style.animation = 'none'; void this.offsetWidth; this.style.animation = 'float 6s ease-in-out infinite';">
                        🐶
                    </div>
                    <h1>PERROCHICO APP</h1>
                    <div class="subtitle">
                        <span class="live-dot"></span>
                        PRO TERMINAL v2.0 • LIVE DATA STREAM
                    </div>
                </div>

                <!-- ====== GO NOW — Trading Alerts (TOP OF PAGE) ====== -->
                <div class="card card-go">
                    <div class="card-header">
                        <div class="card-title">
                            <span class="icon">🚨</span>
                            <span class="go-title">GO NOW — Trading Alerts</span>
                        </div>
                        <div class="controls">
                            <span class="counter-badge {'counter-fire' if num_go > 0 else 'counter-zero'}">
                                {'🔥 ' + str(num_fire) + ' FIRE' if num_fire else ''} 
                                {' • ' if num_fire and num_go > num_fire else ''}
                                {str(num_go) + ' Active' if num_go > 0 else 'Scanning...'}
                            </span>
                        </div>
                    </div>

                    <div class="table-responsive">
                        <table>
                            <thead>
                                <tr>
                                    <th>Signal</th>
                                    <th>Pair</th>
                                    <th>Rate</th>
                                    <th>Funding</th>
                                    <th>15m %</th>
                                    <th>GROW (3H)</th>
                                </tr>
                            </thead>
                            <tbody>
                                {go_now_rows or go_now_empty}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- ====== Market Hot Spots ====== -->
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">
                            <span class="icon">🔥</span>
                            <span>Market Hot Spots (Last 5 Min)</span>
                        </div>
                    </div>

                    <div class="table-responsive">
                        <table>
                            <thead>
                                <tr>
                                    <th>Asset</th>
                                    <th>5m %</th>
                                    <th>Volume</th>
                                    <th>Funding</th>
                                    <th>Detected At</th>
                                </tr>
                            </thead>
                            <tbody id="recent-entries-body">
                                {recent_entries_table or '<tr><td colspan="5" class="empty-state">Scanning market for opportunities...</td></tr>'}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- ====== Symbol Status ====== -->
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">
                            <span class="icon">⚡</span>
                            <span>Symbol Status</span>
                        </div>
                    </div>
                    <div class="table-responsive">
                        <table>
                            <thead>
                                <tr>
                                    <th>Symbol</th>
                                    <th>Status</th>
                                    <th>Price</th>
                                    <th>Rate</th>
                                </tr>
                            </thead>
                            <tbody>
                                {status_table}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- ====== Short Setups ====== -->
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">
                            <span class="icon">✅</span>
                            <span>Short Setups</span>
                        </div>
                    </div>
                    <div class="table-responsive">
                        <table>
                            <thead>
                                <tr>
                                    <th>Symbol</th>
                                    <th>Time</th>
                                    <th>Rate</th>
                                </tr>
                            </thead>
                            <tbody>
                                {short_table}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- ====== Alerts Log ====== -->
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">
                            <span class="icon">🔔</span>
                            <span>Alerts Log</span>
                        </div>
                    </div>
                    <div class="table-responsive">
                        <table>
                            <thead>
                                <tr>
                                    <th>Symbol</th>
                                    <th>Type</th>
                                    <th>Time</th>
                                </tr>
                            </thead>
                            <tbody>
                                {alert_table}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- ====== Events Log ====== -->
                <div class="card">
                    <div class="card-header">
                        <div class="card-title">
                            <span class="icon">💥</span>
                            <span>Events Log</span>
                        </div>
                    </div>
                    <div class="table-responsive">
                        <table>
                            <thead>
                                <tr>
                                    <th>Symbol</th>
                                    <th>Type</th>
                                    <th>Time</th>
                                </tr>
                            </thead>
                            <tbody>
                                {event_table}
                            </tbody>
                        </table>
                    </div>
                </div>

                <div class="footer">
                    Built with 💜 by Earn1n9 Protocol
                </div>
            </div>
        <script>
            function openBinance(symbol) {{
                var url = "https://www.binance.com/es-LA/futures/" + symbol;
                var isMobile = /iPhone|iPad|iPod|Android/i.test(navigator.userAgent);
                if (isMobile) {{
                    window.location.href = "binance://futures/" + symbol;
                    setTimeout(function() {{
                        window.location.href = url;
                    }}, 800);
                }} else {{
                    window.open(url, "_blank");
                }}
            }}
        </script>
        </body>
        </html>
            ''',
            headers={"Cache-Control": "no-store"},
        )

    return app
