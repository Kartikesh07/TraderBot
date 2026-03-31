"""
╔═══════════════════════════════════════════════════════════════════════╗
║  KEEP_ALIVE.PY — Web Dashboard & Health Server for Render            ║
║                                                                       ║
║  Serves a beautiful real-time web dashboard at / so you can          ║
║  monitor the bot from any browser. Also provides:                    ║
║  - /health  → Lightweight JSON for cron-job.org keep-alive pings     ║
║  - /api/stats → Full trading stats JSON                              ║
║  - /api/positions → Open/closed positions JSON                       ║
║  - /api/prices → Live spot prices JSON                               ║
║  - /api/events → Recent event log JSON                               ║
║                                                                       ║
║  The HTML dashboard auto-refreshes every 2 seconds via fetch().      ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

import json
import time
import logging
from typing import Optional
from collections import deque

from aiohttp import web

from config import HEALTH_PORT, TRACKED_ASSETS

logger = logging.getLogger("keep_alive")


# References to shared objects (set by main.py at startup)
_state = None
_trader = None
_event_log: deque = None


def set_references(state, trader, event_log=None):
    """Called by main.py to give us access to shared state."""
    global _state, _trader, _event_log
    _state = state
    _trader = trader
    _event_log = event_log


# ─────────────────────────────────────────────────────────────────────
# API HANDLERS
# ─────────────────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    """GET /health — Lightweight health check for cron keep-alive."""
    uptime = time.time() - _state.bot_start_time if _state else 0
    body = {
        "status": "alive",
        "uptime_seconds": int(uptime),
        "binance_connected": _state.binance_connected if _state else False,
        "coinbase_connected": _state.coinbase_connected if _state else False,
        "active_contracts": (
            sum(1 for c in _state.active_contracts.values() if c.active)
            if _state else 0
        ),
        "timestamp": time.time(),
    }
    return web.json_response(body)


async def handle_api_stats(request: web.Request) -> web.Response:
    """GET /api/stats — Detailed trading statistics."""
    if not _trader:
        return web.json_response({"error": "Trader not initialized"}, status=503)

    stats = _trader.get_stats()
    body = {
        "balance": round(stats.current_balance, 2),
        "initial_balance": _trader.initial_balance,
        "pnl": round(stats.current_balance - _trader.initial_balance, 2),
        "pnl_pct": round(
            (stats.current_balance - _trader.initial_balance) / _trader.initial_balance * 100, 2
        ),
        "total_trades": stats.total_trades,
        "win_rate": round(stats.win_rate, 1),
        "winning_trades": stats.winning_trades,
        "losing_trades": stats.losing_trades,
        "avg_pnl_per_trade": round(stats.avg_pnl_per_trade, 2),
        "best_trade": round(stats.best_trade, 2),
        "worst_trade": round(stats.worst_trade, 2),
        "max_drawdown_pct": round(stats.max_drawdown, 2),
        "latency_arb_trades": stats.latency_arb_count,
        "mispricing_trades": stats.mispricing_count,
        "avg_hold_time": round(stats.avg_hold_time_seconds, 1),
        "uptime_seconds": int(time.time() - _state.bot_start_time) if _state else 0,
        "binance_connected": _state.binance_connected if _state else False,
        "coinbase_connected": _state.coinbase_connected if _state else False,
        "active_contracts": (
            sum(1 for c in _state.active_contracts.values() if c.active)
            if _state else 0
        ),
    }
    return web.json_response(body)


async def handle_api_prices(request: web.Request) -> web.Response:
    """GET /api/prices — Live spot prices from exchanges."""
    if not _state:
        return web.json_response({"error": "State not initialized"}, status=503)

    prices = []
    for asset in TRACKED_ASSETS:
        bn_price = _state.binance_prices.get(asset.symbol)
        cb_price = _state.coinbase_prices.get(asset.symbol)
        best = _state.get_best_price(asset.symbol)

        # Find active contracts for this asset
        contracts = []
        for c in _state.active_contracts.values():
            if c.active and c.asset_symbol == asset.symbol:
                contracts.append({
                    "market_id": c.market_id,
                    "direction": c.direction,
                    "strike_price": c.strike_price,
                    "duration_minutes": c.duration_minutes,
                    "expiry_ts": c.expiry_ts,
                    "ttl_seconds": max(0, int(c.expiry_ts - time.time())),
                    "pm_midpoint": round(c.orderbook.midpoint, 4),
                    "pm_best_bid": round(c.orderbook.best_bid, 4),
                    "pm_best_ask": round(c.orderbook.best_ask, 4),
                    "book_age_ms": round((time.time() - c.orderbook.last_update_ts) * 1000)
                        if c.orderbook.last_update_ts > 0 else None,
                    "question": c.question,
                })

        prices.append({
            "symbol": asset.symbol,
            "name": asset.name,
            "binance_price": round(bn_price, 6) if bn_price else None,
            "coinbase_price": round(cb_price, 6) if cb_price else None,
            "best_price": round(best, 6) if best else None,
            "contracts": contracts,
        })

    return web.json_response(prices)


async def handle_api_positions(request: web.Request) -> web.Response:
    """GET /api/positions — Open and recently closed positions."""
    if not _trader:
        return web.json_response({"error": "Trader not initialized"}, status=503)

    open_pos = [{
        "id": p.position_id,
        "asset": p.asset_symbol,
        "direction": p.direction,
        "entry_price": round(p.entry_price, 4),
        "cost_basis": round(p.cost_basis, 2),
        "quantity": round(p.quantity, 2),
        "signal_type": p.signal_type,
        "entry_time": p.entry_time,
        "entry_gap": round(p.entry_gap, 4),
        "strike_price": p.strike_price,
        "expiry_ts": p.expiry_ts,
        "ttl_seconds": max(0, int(p.expiry_ts - time.time())),
    } for p in _trader.open_positions]

    recent_closed = [{
        "id": p.position_id,
        "asset": p.asset_symbol,
        "direction": p.direction,
        "entry_price": round(p.entry_price, 4),
        "exit_price": round(p.exit_price, 4),
        "pnl": round(p.pnl, 2),
        "pnl_pct": round(p.pnl_pct, 2),
        "cost_basis": round(p.cost_basis, 2),
        "exit_reason": p.exit_reason,
        "signal_type": p.signal_type,
        "hold_time": round(p.exit_time - p.entry_time, 1) if p.exit_time > 0 else 0,
        "exit_time": p.exit_time,
    } for p in _trader.closed_positions[-20:]]

    return web.json_response({
        "open": open_pos,
        "recent_closed": list(reversed(recent_closed)),
    })


async def handle_api_events(request: web.Request) -> web.Response:
    """GET /api/events — Recent event log entries."""
    if _event_log is None:
        return web.json_response([])

    import re
    # Strip Rich markup tags for the web frontend
    def strip_markup(text):
        return re.sub(r'\[/?[^\]]*\]', '', str(text))

    events = [strip_markup(e) for e in list(_event_log)]
    return web.json_response(events)


# ─────────────────────────────────────────────────────────────────────
# WEB DASHBOARD (served as inline HTML)
# ─────────────────────────────────────────────────────────────────────

async def handle_dashboard(request: web.Request) -> web.Response:
    """GET / — Serves the beautiful web dashboard."""
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Paper Trading Bot</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg-primary: #0a0e17;
    --bg-card: #111827;
    --bg-card-hover: #1a2332;
    --bg-row-alt: #0f1520;
    --border: #1e293b;
    --border-glow: #06b6d4;
    --text-primary: #f1f5f9;
    --text-secondary: #94a3b8;
    --text-dim: #64748b;
    --accent-cyan: #06b6d4;
    --accent-purple: #a855f7;
    --accent-green: #22c55e;
    --accent-red: #ef4444;
    --accent-yellow: #eab308;
    --accent-orange: #f97316;
    --gradient-main: linear-gradient(135deg, #06b6d4 0%, #a855f7 100%);
  }

  * { margin:0; padding:0; box-sizing:border-box; }

  body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg-primary);
    color: var(--text-primary);
    min-height: 100vh;
    overflow-x: hidden;
  }

  /* ── Animated background ── */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background:
      radial-gradient(ellipse 800px 600px at 20% 20%, rgba(6,182,212,0.06) 0%, transparent 70%),
      radial-gradient(ellipse 600px 400px at 80% 80%, rgba(168,85,247,0.05) 0%, transparent 70%);
    pointer-events: none;
    z-index: 0;
  }

  .container {
    max-width: 1200px;
    margin: 0 auto;
    padding: 20px;
    position: relative;
    z-index: 1;
  }

  /* ── Header ── */
  .header {
    text-align: center;
    padding: 32px 0 24px;
  }
  .header h1 {
    font-size: 28px;
    font-weight: 800;
    background: var(--gradient-main);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: -0.5px;
  }
  .header .subtitle {
    color: var(--text-dim);
    font-size: 13px;
    margin-top: 6px;
    font-family: 'JetBrains Mono', monospace;
  }
  .live-dot {
    display: inline-block;
    width: 8px; height: 8px;
    border-radius: 50%;
    background: var(--accent-green);
    margin-right: 6px;
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity:1; box-shadow: 0 0 0 0 rgba(34,197,94,0.4); }
    50% { opacity:0.7; box-shadow: 0 0 0 6px rgba(34,197,94,0); }
  }

  /* ── Stats cards row ── */
  .stats-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }
  .stat-card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 18px;
    transition: border-color 0.3s, transform 0.2s;
  }
  .stat-card:hover {
    border-color: var(--border-glow);
    transform: translateY(-2px);
  }
  .stat-card .label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    font-weight: 600;
  }
  .stat-card .value {
    font-size: 24px;
    font-weight: 700;
    margin-top: 4px;
    font-family: 'JetBrains Mono', monospace;
  }
  .stat-card .sub {
    font-size: 12px;
    color: var(--text-dim);
    margin-top: 2px;
  }

  /* ── Section cards ── */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    margin-bottom: 16px;
    overflow: hidden;
  }
  .card-header {
    padding: 14px 18px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 8px;
    font-weight: 600;
    font-size: 14px;
  }
  .card-body { padding: 0; }

  /* ── Connection badges ── */
  .connections {
    display: flex;
    gap: 12px;
    padding: 14px 18px;
    flex-wrap: wrap;
  }
  .conn-badge {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 12px;
    font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
    border: 1px solid var(--border);
    background: var(--bg-row-alt);
  }
  .conn-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
  }
  .conn-dot.on { background: var(--accent-green); box-shadow: 0 0 6px var(--accent-green); }
  .conn-dot.off { background: var(--accent-red); }
  .conn-dot.warn { background: var(--accent-yellow); }

  /* ── Tables ── */
  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }
  thead th {
    padding: 10px 14px;
    text-align: left;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--text-dim);
    font-weight: 600;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  tbody td {
    padding: 10px 14px;
    border-bottom: 1px solid rgba(30,41,59,0.5);
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    white-space: nowrap;
  }
  tbody tr:hover { background: var(--bg-card-hover); }
  tbody tr:nth-child(even) { background: var(--bg-row-alt); }
  tbody tr:nth-child(even):hover { background: var(--bg-card-hover); }

  .text-green { color: var(--accent-green); }
  .text-red { color: var(--accent-red); }
  .text-yellow { color: var(--accent-yellow); }
  .text-cyan { color: var(--accent-cyan); }
  .text-purple { color: var(--accent-purple); }
  .text-dim { color: var(--text-dim); }
  .text-right { text-align: right; }
  .font-semibold { font-weight: 600; }

  .badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.3px;
  }
  .badge-green { background: rgba(34,197,94,0.15); color: var(--accent-green); }
  .badge-red { background: rgba(239,68,68,0.15); color: var(--accent-red); }
  .badge-cyan { background: rgba(6,182,212,0.15); color: var(--accent-cyan); }
  .badge-purple { background: rgba(168,85,247,0.15); color: var(--accent-purple); }
  .badge-yellow { background: rgba(234,179,8,0.15); color: var(--accent-yellow); }

  /* ── Event log ── */
  .event-list {
    padding: 12px 18px;
    max-height: 280px;
    overflow-y: auto;
  }
  .event-item {
    padding: 6px 0;
    font-size: 12px;
    font-family: 'JetBrains Mono', monospace;
    color: var(--text-secondary);
    border-bottom: 1px solid rgba(30,41,59,0.3);
    display: flex;
    gap: 10px;
  }
  .event-item:last-child { border-bottom: none; }
  .event-time { color: var(--text-dim); white-space: nowrap; }

  /* ── Empty state ── */
  .empty-state {
    padding: 32px;
    text-align: center;
    color: var(--text-dim);
    font-size: 13px;
  }

  /* ── Footer ── */
  .footer {
    text-align: center;
    padding: 24px 0;
    color: var(--text-dim);
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
  }

  /* ── Scrollbar ── */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }

  /* ── Responsive ── */
  @media (max-width: 768px) {
    .container { padding: 12px; }
    .header h1 { font-size: 20px; }
    .stats-row { grid-template-columns: repeat(2, 1fr); gap: 8px; }
    .stat-card .value { font-size: 18px; }
    table { font-size: 11px; }
    thead th, tbody td { padding: 8px 10px; }
  }
</style>
</head>
<body>

<div class="container">
  <!-- Header -->
  <div class="header">
    <h1>🚀 Polymarket Paper Trading Bot</h1>
    <div class="subtitle"><span class="live-dot" id="liveDot"></span><span id="statusText">Connecting...</span></div>
  </div>

  <!-- Stats Row -->
  <div class="stats-row">
    <div class="stat-card">
      <div class="label">💰 Balance</div>
      <div class="value" id="balance">$1,000.00</div>
      <div class="sub" id="balanceSub">Starting balance</div>
    </div>
    <div class="stat-card">
      <div class="label">📈 PnL</div>
      <div class="value" id="pnl">$0.00</div>
      <div class="sub" id="pnlPct">0.00%</div>
    </div>
    <div class="stat-card">
      <div class="label">🏆 Win Rate</div>
      <div class="value" id="winRate">—</div>
      <div class="sub" id="winRateSub">No trades yet</div>
    </div>
    <div class="stat-card">
      <div class="label">📊 Total Trades</div>
      <div class="value" id="totalTrades">0</div>
      <div class="sub" id="tradesSub">⚡ 0 arb · 📐 0 misp</div>
    </div>
    <div class="stat-card">
      <div class="label">🔄 Positions</div>
      <div class="value" id="openPos">0 / 3</div>
      <div class="sub" id="posSub">Open / max</div>
    </div>
    <div class="stat-card">
      <div class="label">⏱️ Uptime</div>
      <div class="value" id="uptime">0s</div>
      <div class="sub" id="uptimeSub">Bot running time</div>
    </div>
  </div>

  <!-- Connections -->
  <div class="card">
    <div class="card-header">📡 Data Feeds</div>
    <div class="connections" id="connections">
      <div class="conn-badge"><div class="conn-dot off" id="dotBinance"></div>Binance</div>
      <div class="conn-badge"><div class="conn-dot off" id="dotCoinbase"></div>Coinbase</div>
      <div class="conn-badge"><div class="conn-dot off" id="dotPolymarket"></div>Polymarket <span id="contractCount" style="margin-left:4px"></span></div>
    </div>
  </div>

  <!-- Live Prices -->
  <div class="card">
    <div class="card-header">📊 Live Prices & Contracts</div>
    <div class="card-body">
      <table>
        <thead>
          <tr>
            <th>Asset</th>
            <th class="text-right">Spot Price</th>
            <th>Source</th>
            <th>PM Contract</th>
            <th class="text-right">PM Price</th>
            <th class="text-right">TTL</th>
          </tr>
        </thead>
        <tbody id="pricesTable">
          <tr><td colspan="6" class="empty-state">Loading prices...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Open Positions -->
  <div class="card">
    <div class="card-header">📋 Open Positions</div>
    <div class="card-body">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Market</th>
            <th class="text-right">Entry</th>
            <th class="text-right">Cost</th>
            <th>Signal</th>
            <th class="text-right">TTL</th>
          </tr>
        </thead>
        <tbody id="openPosTable">
          <tr><td colspan="6" class="empty-state">No open positions — scanning for opportunities...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Recent Trades -->
  <div class="card">
    <div class="card-header">📜 Recent Trades</div>
    <div class="card-body">
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Market</th>
            <th class="text-right">Entry</th>
            <th class="text-right">Exit</th>
            <th class="text-right">PnL</th>
            <th>Reason</th>
            <th>Signal</th>
            <th>Hold</th>
          </tr>
        </thead>
        <tbody id="closedPosTable">
          <tr><td colspan="8" class="empty-state">No trades yet</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Event Log -->
  <div class="card">
    <div class="card-header">📝 Live Events</div>
    <div class="event-list" id="eventLog">
      <div class="empty-state">Waiting for events...</div>
    </div>
  </div>

  <!-- Stats Footer -->
  <div class="card">
    <div class="card-header">📊 Detailed Statistics</div>
    <div class="stats-row" style="padding: 16px; margin-bottom: 0;">
      <div class="stat-card" style="border: none; padding: 8px 0;">
        <div class="label">Avg PnL/Trade</div>
        <div class="value" id="avgPnl" style="font-size:16px">$0.00</div>
      </div>
      <div class="stat-card" style="border: none; padding: 8px 0;">
        <div class="label">Best Trade</div>
        <div class="value text-green" id="bestTrade" style="font-size:16px">$0.00</div>
      </div>
      <div class="stat-card" style="border: none; padding: 8px 0;">
        <div class="label">Worst Trade</div>
        <div class="value text-red" id="worstTrade" style="font-size:16px">$0.00</div>
      </div>
      <div class="stat-card" style="border: none; padding: 8px 0;">
        <div class="label">Max Drawdown</div>
        <div class="value text-yellow" id="maxDD" style="font-size:16px">0%</div>
      </div>
      <div class="stat-card" style="border: none; padding: 8px 0;">
        <div class="label">Avg Hold Time</div>
        <div class="value" id="avgHold" style="font-size:16px">—</div>
      </div>
    </div>
  </div>

  <div class="footer">
    Polymarket Paper Trading Bot · Latency Arbitrage & Probability Mispricing<br>
    Data refreshes every 2 seconds · <span id="lastUpdate">—</span>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function fmtPrice(p) {
  if (p === null || p === undefined) return '—';
  if (p >= 1000) return '$' + p.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
  if (p >= 1) return '$' + p.toFixed(4);
  return '$' + p.toFixed(6);
}
function fmtUSD(v) { return (v >= 0 ? '+' : '') + '$' + Math.abs(v).toFixed(2); }
function fmtPct(v) { return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; }
function fmtTime(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  if (h > 0) return h + 'h ' + m + 'm';
  if (m > 0) return m + 'm ' + s + 's';
  return s + 's';
}
function fmtTS(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString();
}

async function fetchJSON(url) {
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch(e) { return null; }
}

async function update() {
  const [stats, prices, positions, events] = await Promise.all([
    fetchJSON('/api/stats'),
    fetchJSON('/api/prices'),
    fetchJSON('/api/positions'),
    fetchJSON('/api/events'),
  ]);

  if (!stats) {
    $('statusText').textContent = 'Connection lost — retrying...';
    $('liveDot').style.background = '#ef4444';
    return;
  }

  // ── Status ──
  $('statusText').textContent = 'LIVE · Uptime ' + fmtTime(stats.uptime_seconds);
  $('liveDot').style.background = '#22c55e';

  // ── Stats cards ──
  $('balance').textContent = '$' + stats.balance.toLocaleString('en-US', {minimumFractionDigits:2});
  const pnlColor = stats.pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)';
  $('pnl').textContent = fmtUSD(stats.pnl);
  $('pnl').style.color = pnlColor;
  $('pnlPct').textContent = fmtPct(stats.pnl_pct);
  $('pnlPct').style.color = pnlColor;

  if (stats.total_trades > 0) {
    const wrColor = stats.win_rate >= 50 ? 'var(--accent-green)' : 'var(--accent-red)';
    $('winRate').textContent = stats.win_rate.toFixed(1) + '%';
    $('winRate').style.color = wrColor;
    $('winRateSub').textContent = stats.winning_trades + 'W / ' + stats.losing_trades + 'L';
  } else {
    $('winRate').textContent = '—';
    $('winRate').style.color = '';
    $('winRateSub').textContent = 'No trades yet';
  }

  $('totalTrades').textContent = stats.total_trades;
  $('tradesSub').textContent = '⚡ ' + stats.latency_arb_trades + ' arb · 📐 ' + stats.mispricing_trades + ' misp';
  $('uptime').textContent = fmtTime(stats.uptime_seconds);

  $('avgPnl').textContent = fmtUSD(stats.avg_pnl_per_trade);
  $('bestTrade').textContent = fmtUSD(stats.best_trade);
  $('worstTrade').textContent = fmtUSD(stats.worst_trade);
  $('maxDD').textContent = stats.max_drawdown_pct.toFixed(1) + '%';
  $('avgHold').textContent = stats.avg_hold_time > 0 ? fmtTime(Math.round(stats.avg_hold_time)) : '—';

  // ── Connections ──
  $('dotBinance').className = 'conn-dot ' + (stats.binance_connected ? 'on' : 'off');
  $('dotCoinbase').className = 'conn-dot ' + (stats.coinbase_connected ? 'on' : 'off');
  $('dotPolymarket').className = 'conn-dot ' + (stats.active_contracts > 0 ? 'on' : 'warn');
  $('contractCount').textContent = stats.active_contracts > 0 ? '(' + stats.active_contracts + ')' : '(scanning)';

  // ── Prices table ──
  if (prices && prices.length) {
    let html = '';
    for (const p of prices) {
      const src = p.binance_price && p.coinbase_price ? '<span class="text-green">BN+CB</span>'
        : p.binance_price ? '<span class="text-yellow">BN</span>'
        : p.coinbase_price ? '<span class="text-yellow">CB</span>'
        : '<span class="text-dim">—</span>';

      if (p.contracts && p.contracts.length > 0) {
        for (const c of p.contracts) {
          html += '<tr>' +
            '<td class="font-semibold">' + p.symbol + '</td>' +
            '<td class="text-right">' + fmtPrice(p.best_price) + '</td>' +
            '<td>' + src + '</td>' +
            '<td>' + p.symbol + ' ' + c.direction + ' ' + c.duration_minutes + 'm</td>' +
            '<td class="text-right text-yellow">$' + c.pm_midpoint.toFixed(3) + '</td>' +
            '<td class="text-right text-dim">' + fmtTime(c.ttl_seconds) + '</td>' +
            '</tr>';
        }
      } else {
        html += '<tr>' +
          '<td class="font-semibold">' + p.symbol + '</td>' +
          '<td class="text-right">' + fmtPrice(p.best_price) + '</td>' +
          '<td>' + src + '</td>' +
          '<td class="text-dim">No contract</td>' +
          '<td class="text-dim">—</td>' +
          '<td class="text-dim">—</td>' +
          '</tr>';
      }
    }
    $('pricesTable').innerHTML = html;
  }

  // ── Open positions ──
  if (positions) {
    $('openPos').textContent = positions.open.length + ' / 3';
    if (positions.open.length > 0) {
      let html = '';
      for (const p of positions.open) {
        const sigBadge = p.signal_type === 'LATENCY_ARB'
          ? '<span class="badge badge-cyan">⚡ ARB</span>'
          : '<span class="badge badge-purple">📐 MISP</span>';
        html += '<tr>' +
          '<td>' + p.id + '</td>' +
          '<td class="font-semibold">' + p.asset + ' ' + p.direction + '</td>' +
          '<td class="text-right">$' + p.entry_price.toFixed(3) + '</td>' +
          '<td class="text-right">$' + p.cost_basis.toFixed(2) + '</td>' +
          '<td>' + sigBadge + '</td>' +
          '<td class="text-right text-yellow">' + fmtTime(p.ttl_seconds) + '</td>' +
          '</tr>';
      }
      $('openPosTable').innerHTML = html;
    } else {
      $('openPosTable').innerHTML = '<tr><td colspan="6" class="empty-state">No open positions — scanning for opportunities...</td></tr>';
    }

    // ── Closed positions ──
    if (positions.recent_closed.length > 0) {
      let html = '';
      for (const p of positions.recent_closed) {
        const pnlC = p.pnl >= 0 ? 'text-green' : 'text-red';
        const emoji = p.pnl > 0 ? '✅' : '❌';
        const sigBadge = p.signal_type === 'LATENCY_ARB'
          ? '<span class="badge badge-cyan">⚡</span>'
          : '<span class="badge badge-purple">📐</span>';
        const reasonBadge = p.exit_reason.includes('WIN')
          ? '<span class="badge badge-green">' + p.exit_reason + '</span>'
          : p.exit_reason.includes('LOSS')
          ? '<span class="badge badge-red">' + p.exit_reason + '</span>'
          : '<span class="badge badge-yellow">' + p.exit_reason + '</span>';
        html += '<tr>' +
          '<td>' + emoji + ' ' + p.id + '</td>' +
          '<td class="font-semibold">' + p.asset + ' ' + p.direction + '</td>' +
          '<td class="text-right">$' + p.entry_price.toFixed(3) + '</td>' +
          '<td class="text-right">$' + p.exit_price.toFixed(3) + '</td>' +
          '<td class="text-right ' + pnlC + ' font-semibold">' + fmtUSD(p.pnl) + ' (' + fmtPct(p.pnl_pct) + ')</td>' +
          '<td>' + reasonBadge + '</td>' +
          '<td>' + sigBadge + '</td>' +
          '<td class="text-dim">' + fmtTime(Math.round(p.hold_time)) + '</td>' +
          '</tr>';
      }
      $('closedPosTable').innerHTML = html;
    } else {
      $('closedPosTable').innerHTML = '<tr><td colspan="8" class="empty-state">No trades yet</td></tr>';
    }
  }

  // ── Events ──
  if (events && events.length > 0) {
    let html = '';
    const now = new Date().toLocaleTimeString();
    for (const e of events) {
      html += '<div class="event-item"><span class="event-time">' + now + '</span><span>' + e + '</span></div>';
    }
    $('eventLog').innerHTML = html;
  }

  $('lastUpdate').textContent = 'Last update: ' + new Date().toLocaleTimeString();
}

// Initial fetch + interval
update();
setInterval(update, 2000);
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────────────
# SERVER STARTUP
# ─────────────────────────────────────────────────────────────────────

async def start_health_server():
    """
    Starts the aiohttp web server with dashboard + API.

    Routes:
    - GET /         → Web dashboard (HTML)
    - GET /health   → Health check JSON (for cron keep-alive)
    - GET /api/stats     → Trading statistics JSON
    - GET /api/prices    → Live spot prices JSON
    - GET /api/positions → Open/closed positions JSON
    - GET /api/events    → Recent event log JSON
    """
    app = web.Application()
    app.router.add_get("/", handle_dashboard)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/stats", handle_api_stats)
    app.router.add_get("/api/prices", handle_api_prices)
    app.router.add_get("/api/positions", handle_api_positions)
    app.router.add_get("/api/events", handle_api_events)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()

    logger.info(
        f"🌐 Health server started on port {HEALTH_PORT} "
        f"— endpoints: /health, /api/stats, /api/prices, /api/positions, /api/events"
    )
    logger.info(
        "💡 To keep bot alive on Render free tier, set up a cron ping:\n"
        "   → https://cron-job.org — ping /health every 10 minutes"
    )
