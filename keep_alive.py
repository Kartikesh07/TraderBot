"""
╔═══════════════════════════════════════════════════════════════════════╗
║  KEEP_ALIVE.PY — Health Endpoint & Anti-Idle for Render Deployment   ║
║                                                                       ║
║  Problem: Render's free tier spins down after 15 minutes of no       ║
║  inbound HTTP traffic. Since our bot is a long-running WebSocket     ║
║  consumer (not a web server), Render thinks it's idle.               ║
║                                                                       ║
║  Solution:                                                            ║
║  1. Embed a lightweight aiohttp web server inside the bot            ║
║  2. Expose /health and /stats endpoints on the PORT env variable     ║
║  3. Use external cron service (cron-job.org / UptimeRobot) to ping  ║
║     /health every 10 minutes → keeps Render from sleeping.          ║
║                                                                       ║
║  Important: The internal self-ping approach does NOT work because    ║
║  when Render spins the service down, ALL internal code stops —       ║
║  including any self-ping scheduler. The ping MUST come from outside. ║
║                                                                       ║
║  Setup on cron-job.org (free):                                        ║
║  1. Go to https://cron-job.org and create a free account             ║
║  2. Create a new cron job with:                                       ║
║     - URL: https://your-service.onrender.com/health                  ║
║     - Schedule: Every 10 minutes                                      ║
║  3. Done! Your bot will stay alive 24/7.                             ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

import json
import time
import logging
from typing import Optional

from aiohttp import web

from config import HEALTH_PORT

logger = logging.getLogger("keep_alive")


# References to shared objects (set by main.py at startup)
_state = None
_trader = None


def set_references(state, trader):
    """Called by main.py to give us access to shared state."""
    global _state, _trader
    _state = state
    _trader = trader


# ─────────────────────────────────────────────────────────────────────
# HTTP REQUEST HANDLERS
# ─────────────────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    """
    GET /health — Lightweight health check endpoint.
    
    This is what the external cron service pings every 10 minutes
    to prevent Render from spinning down the service.
    
    Returns 200 OK with basic status info.
    """
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

    logger.debug(f"Health check pinged — uptime: {int(uptime)}s")
    return web.json_response(body)


async def handle_stats(request: web.Request) -> web.Response:
    """
    GET /stats — Returns detailed trading statistics.
    
    Useful for monitoring bot performance remotely
    (e.g., from your phone) without needing terminal access.
    """
    if not _trader:
        return web.json_response({"error": "Trader not initialized"}, status=503)

    stats = _trader.get_stats()
    open_positions = []

    for pos in _trader.open_positions:
        open_positions.append({
            "id": pos.position_id,
            "asset": pos.asset_symbol,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "cost_basis": pos.cost_basis,
            "signal_type": pos.signal_type,
        })

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
        "open_positions": open_positions,
        "uptime_seconds": int(time.time() - _state.bot_start_time) if _state else 0,
    }

    return web.json_response(body)


async def handle_positions(request: web.Request) -> web.Response:
    """
    GET /positions — Returns all open and recent closed positions.
    """
    if not _trader:
        return web.json_response({"error": "Trader not initialized"}, status=503)

    open_pos = [{
        "id": p.position_id,
        "asset": p.asset_symbol,
        "direction": p.direction,
        "entry_price": round(p.entry_price, 4),
        "cost_basis": round(p.cost_basis, 2),
        "signal_type": p.signal_type,
        "entry_time": p.entry_time,
    } for p in _trader.open_positions]

    recent_closed = [{
        "id": p.position_id,
        "asset": p.asset_symbol,
        "direction": p.direction,
        "entry_price": round(p.entry_price, 4),
        "exit_price": round(p.exit_price, 4),
        "pnl": round(p.pnl, 2),
        "exit_reason": p.exit_reason,
    } for p in _trader.closed_positions[-10:]]

    return web.json_response({
        "open": open_pos,
        "recent_closed": recent_closed,
    })


# ─────────────────────────────────────────────────────────────────────
# SERVER STARTUP
# ─────────────────────────────────────────────────────────────────────

async def start_health_server():
    """
    Starts the lightweight aiohttp web server.
    
    Endpoints:
    - GET /health    → Health check (for keep-alive pings)
    - GET /stats     → Trading statistics JSON
    - GET /positions → Open/closed positions JSON
    
    Runs on the port specified by Render's PORT env variable
    (defaults to 10000).
    """
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/stats", handle_stats)
    app.router.add_get("/positions", handle_positions)
    app.router.add_get("/", handle_health)  # Root also returns health

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_PORT)
    await site.start()

    logger.info(
        f"🌐 Health server started on port {HEALTH_PORT} "
        f"— endpoints: /health, /stats, /positions"
    )
    logger.info(
        "💡 To keep bot alive on Render free tier, set up a cron ping:\n"
        "   → https://cron-job.org — ping /health every 10 minutes"
    )
