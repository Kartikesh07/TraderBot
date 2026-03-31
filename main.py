"""
╔═══════════════════════════════════════════════════════════════════════╗
║  MAIN.PY — Async Orchestrator & Entry Point                         ║
║                                                                       ║
║  Launches all concurrent tasks:                                      ║
║  1. Binance WebSocket price feed                                     ║
║  2. Coinbase WebSocket price feed                                    ║
║  3. Polymarket market scanner (REST polling)                         ║
║  4. Polymarket CLOB orderbook feed                                   ║
║  5. Strategy evaluation loop (200ms cycle)                           ║
║  6. Position monitor (stop-loss, expiry settlement)                  ║
║  7. Dashboard renderer (500ms refresh)                               ║
║  8. Health server (for Render keep-alive)                            ║
║                                                                       ║
║  All tasks run concurrently via asyncio.gather().                    ║
║  Graceful shutdown on Ctrl+C / SIGTERM.                              ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import signal
import sys
import time
import logging
import os
from collections import deque

from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler

from config import (
    STRATEGY_LOOP_INTERVAL, POSITION_CHECK_INTERVAL,
    DASHBOARD_REFRESH_INTERVAL, LOG_DIR,
)
from data_ingestion import (
    SharedState, binance_feed, coinbase_feed,
    polymarket_scanner, polymarket_book_feed,
)
from pricing_engine import (
    calculate_rolling_volatility, evaluate_contract,
    SignalType, TradingSignal
)
from paper_engine import PaperTrader
from dashboard import render_dashboard
from keep_alive import start_health_server, set_references

# ─────────────────────────────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────────────────────────────

os.makedirs(LOG_DIR, exist_ok=True)

# Keep a reference to the RichHandler so we can remove it when the
# Live dashboard takes over the terminal (prevents log lines from
# scrolling the in-place dashboard rendering).
_rich_handler = RichHandler(
    console=Console(stderr=True),
    show_time=True,
    show_path=False,
    markup=True,
)

_file_handler = logging.FileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    mode="a",
    encoding="utf-8",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    datefmt="[%H:%M:%S]",
    handlers=[_rich_handler, _file_handler],
)

# Reduce noise from libraries
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)

logger = logging.getLogger("main")
console = Console()

# ─────────────────────────────────────────────────────────────────────
# EVENT LOG — shared deque displayed inside the dashboard itself
# so important events remain visible even after console logging is muted
# ─────────────────────────────────────────────────────────────────────
event_log: deque = deque(maxlen=12)


# ─────────────────────────────────────────────────────────────────────
# STRATEGY LOOP
# ─────────────────────────────────────────────────────────────────────

async def strategy_loop(state: SharedState, trader: PaperTrader):
    """
    Core strategy evaluation loop — runs every 200ms.
    
    For each active Polymarket contract:
    1. Get the current spot price from exchange feeds
    2. Calculate rolling volatility from tick history
    3. Run the pricing engine to evaluate true probability
    4. Check for mispricing or latency arbitrage signals
    5. If signal is strong enough, execute a paper trade
    
    This is the "brain" of the bot — where all signal detection
    and trade execution decisions are made.
    """
    logger.info("🧠 Strategy loop started — evaluating every 200ms")

    # Wait for initial data
    await asyncio.sleep(5)

    while True:
        try:
            now = time.time()

            # Iterate over all active contracts
            for market_id, contract in list(state.active_contracts.items()):
                if not contract.active:
                    continue

                # ── Get spot price ──
                spot = state.get_best_price(contract.asset_symbol)
                if spot is None:
                    continue  # No price data yet

                # ── Calculate volatility ──
                vol = calculate_rolling_volatility(
                    state.price_history.get(contract.asset_symbol, [])
                )

                # ── Time to expiry ──
                tte = contract.expiry_ts - now
                if tte <= 0:
                    continue  # Expired

                # ── PM orderbook data ──
                ob = contract.orderbook
                if ob.last_update_ts <= 0:
                    continue  # No orderbook data yet

                pm_book_age_ms = (now - ob.last_update_ts) * 1000

                # ── Check if we already have a position in this market ──
                is_positioned = trader.is_already_positioned(market_id)

                # ── Evaluate signal ──
                signal = evaluate_contract(
                    spot_price=spot,
                    strike_price=contract.strike_price,
                    direction=contract.direction,
                    time_to_expiry_seconds=tte,
                    volatility=vol,
                    pm_best_ask=ob.best_ask,
                    pm_midpoint=ob.midpoint,
                    pm_book_age_ms=pm_book_age_ms,
                    is_open_position=is_positioned,
                    asset_symbol=contract.asset_symbol,
                    market_id=market_id,
                )

                # ── Act on signal ──
                if signal.signal_type == SignalType.MISPRICING_BUY:
                    if trader.can_open_position and not is_positioned:
                        event_log.append(
                            f"[bright_magenta]📐 MISPRICING[/] {contract.asset_symbol} {contract.direction} "
                            f"| Prob: {signal.true_probability:.0%} vs PM: ${ob.midpoint:.3f} "
                            f"| Gap: {signal.gap_pct:.0%} | TTL: {tte:.0f}s"
                        )
                        logger.info(
                            f"📐 MISPRICING detected: {contract.asset_symbol} "
                            f"{contract.direction} | True Prob: {signal.true_probability:.1%} "
                            f"vs PM: ${ob.midpoint:.3f} | Gap: {signal.gap_pct:.1%} "
                            f"| σ: {signal.volatility:.0%} | TTL: {tte:.0f}s"
                        )
                        trader.open_position(
                            market_id=market_id,
                            asset_symbol=contract.asset_symbol,
                            direction=contract.direction,
                            signal_type="MISPRICING",
                            token_price=ob.best_ask,
                            true_prob=signal.true_probability,
                            gap=signal.gap_pct,
                            spot_price=spot,
                            strike_price=contract.strike_price,
                            expiry_ts=contract.expiry_ts,
                            question=contract.question,
                            orderbook_asks=ob.asks,
                        )

                elif signal.signal_type == SignalType.LATENCY_ARB:
                    if trader.can_open_position and not is_positioned:
                        event_log.append(
                            f"[bright_cyan]⚡ LATENCY ARB[/] {contract.asset_symbol} {contract.direction} "
                            f"| Spot: {format_price(spot)} crossed {format_price(contract.strike_price)} "
                            f"| Book stale: {pm_book_age_ms:.0f}ms"
                        )
                        logger.info(
                            f"⚡ LATENCY ARB detected: {contract.asset_symbol} "
                            f"{contract.direction} | Spot: {format_price(spot)} "
                            f"crossed strike: {format_price(contract.strike_price)} "
                            f"| PM book stale: {pm_book_age_ms:.0f}ms "
                            f"| PM ask: ${ob.best_ask:.3f}"
                        )
                        trader.open_position(
                            market_id=market_id,
                            asset_symbol=contract.asset_symbol,
                            direction=contract.direction,
                            signal_type="LATENCY_ARB",
                            token_price=ob.best_ask,
                            true_prob=signal.true_probability,
                            gap=signal.gap_pct,
                            spot_price=spot,
                            strike_price=contract.strike_price,
                            expiry_ts=contract.expiry_ts,
                            question=contract.question,
                            orderbook_asks=ob.asks,
                        )

                elif signal.signal_type == SignalType.EXIT_MEAN_REVERT:
                    if is_positioned:
                        for pos in trader.open_positions:
                            if pos.market_id == market_id:
                                event_log.append(
                                    f"[yellow]📉 EXIT[/] {contract.asset_symbol} "
                                    f"gap closed to {signal.gap_pct:.1%}"
                                )
                                logger.info(
                                    f"📉 Mean reversion exit: {contract.asset_symbol} "
                                    f"gap closed to {signal.gap_pct:.1%}"
                                )
                                trader.close_position(
                                    pos, ob.best_bid, "MEAN_REVERT",
                                    orderbook_bids=ob.bids,
                                )
                                break

                elif signal.signal_type == SignalType.EXIT_EXPIRY:
                    if is_positioned:
                        for pos in trader.open_positions:
                            if pos.market_id == market_id:
                                # Settle based on spot vs strike
                                if contract.direction == "UP":
                                    won = spot > contract.strike_price
                                else:
                                    won = spot < contract.strike_price
                                exit_price = 1.0 if won else 0.0
                                reason = "EXPIRY_WIN" if won else "EXPIRY_LOSS"
                                trader.close_position(pos, exit_price, reason)
                                break

        except asyncio.CancelledError:
            logger.info("🧠 Strategy loop cancelled")
            return
        except Exception as e:
            logger.error(f"🧠 Strategy error: {e}", exc_info=True)

        await asyncio.sleep(STRATEGY_LOOP_INTERVAL)


def format_price(price: float) -> str:
    """Quick price formatter for logging."""
    if price >= 1000:
        return f"${price:,.2f}"
    return f"${price:.4f}"


# ─────────────────────────────────────────────────────────────────────
# POSITION MONITOR
# ─────────────────────────────────────────────────────────────────────

async def position_monitor(state: SharedState, trader: PaperTrader):
    """
    Monitors open positions for exit conditions:
    - Stop-loss (position value drops > 50%)
    - Expiry settlement (contract has expired)
    
    Runs every 1 second.
    """
    logger.info("🛡️ Position monitor started")
    await asyncio.sleep(10)  # Wait for initial data

    while True:
        try:
            # ── Check stop-losses ──
            def get_pm_price(market_id: str):
                contract = state.active_contracts.get(market_id)
                if contract and contract.orderbook.last_update_ts > 0:
                    return contract.orderbook.midpoint
                return None

            trader.check_stop_losses(get_current_price_fn=get_pm_price)

            # ── Check expiries ──
            spot_prices = {}
            for asset in state.binance_prices:
                spot_prices[asset] = state.get_best_price(asset)
            trader.check_expiries(spot_prices)

        except asyncio.CancelledError:
            logger.info("🛡️ Position monitor cancelled")
            return
        except Exception as e:
            logger.error(f"🛡️ Monitor error: {e}")

        await asyncio.sleep(POSITION_CHECK_INTERVAL)


# ─────────────────────────────────────────────────────────────────────
# DASHBOARD LOOP
# ─────────────────────────────────────────────────────────────────────

async def dashboard_loop(state: SharedState, trader: PaperTrader):
    """
    Renders the Rich dashboard to the terminal.
    
    Uses Rich's Live context for smooth in-place updates
    every 500ms. Before starting, we remove the RichHandler
    from the root logger so log messages don't scroll the
    terminal — instead, important events are shown inside
    the dashboard via the shared event_log deque.
    """
    logger.info("📺 Dashboard starting in 3 seconds...")
    await asyncio.sleep(3)

    # Mute console logging — Live needs exclusive terminal control.
    # All logs will still go to the file handler.
    root = logging.getLogger()
    root.removeHandler(_rich_handler)

    try:
        with Live(
            render_dashboard(state, trader, event_log),
            console=console,
            refresh_per_second=2,
            vertical_overflow="ellipsis",
            screen=True,          # full-screen mode — clears & redraws in place
        ) as live:
            while True:
                try:
                    live.update(render_dashboard(state, trader, event_log))
                except Exception as e:
                    pass  # logged to file only
                await asyncio.sleep(DASHBOARD_REFRESH_INTERVAL)
    except asyncio.CancelledError:
        pass
    finally:
        # Restore console logging for the shutdown summary
        root.addHandler(_rich_handler)
        logger.info("📺 Dashboard stopped")


# ─────────────────────────────────────────────────────────────────────
# STARTUP BANNER
# ─────────────────────────────────────────────────────────────────────

def print_banner():
    """Print a beautiful startup banner."""
    banner = """
[bold bright_cyan]
    ╔═══════════════════════════════════════════════════════════════╗
    ║                                                               ║
    ║   🚀  POLYMARKET CRYPTO PAPER TRADING BOT                    ║
    ║                                                               ║
    ║   Strategies:                                                 ║
    ║     ⚡ Latency Arbitrage (Binance/Coinbase → Polymarket)     ║
    ║     📐 Probability Mispricing (Black-Scholes Binary)         ║
    ║                                                               ║
    ║   Assets: BTC, ETH, SOL, XRP, DOGE, ADA, AVAX, LINK,       ║
    ║           DOT, POL                                            ║
    ║                                                               ║
    ║   Paper Trading: $1,000 USDC Simulated Balance               ║
    ║   Risk: 10% per trade │ Max 3 concurrent positions           ║
    ║                                                               ║
    ║   Press Ctrl+C to stop gracefully                             ║
    ║                                                               ║
    ╚═══════════════════════════════════════════════════════════════╝
[/bold bright_cyan]"""
    console.print(banner)


# ─────────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────────

async def main():
    """
    Main async entry point — launches all concurrent tasks:
    
    Task 1: Binance price stream (WebSocket)
    Task 2: Coinbase price stream (WebSocket)
    Task 3: Polymarket market scanner (REST polling)
    Task 4: Polymarket CLOB orderbook (REST polling)
    Task 5: Strategy evaluation (200ms loop)
    Task 6: Position monitor (1s loop)
    Task 7: Dashboard renderer (500ms refresh)
    Task 8: Health server (for Render keep-alive)
    
    All tasks share the same SharedState and PaperTrader instances.
    """
    print_banner()

    # ── Initialize shared objects ──
    state = SharedState()
    trader = PaperTrader()

    # ── Set up health server references ──
    set_references(state, trader)

    # ── Start health server (for Render deployment) ──
    await start_health_server()

    logger.info("🔌 Initializing data feeds...")
    logger.info(f"📁 Trade logs: ./{LOG_DIR}/")

    # ── Handle graceful shutdown ──
    shutdown_event = asyncio.Event()

    def _signal_handler():
        logger.info("\n🛑 Shutdown signal received — closing gracefully...")
        shutdown_event.set()

    # Register signal handlers
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler for SIGTERM
        pass

    # ── Launch all concurrent tasks ──
    tasks = [
        asyncio.create_task(binance_feed(state), name="binance"),
        asyncio.create_task(coinbase_feed(state), name="coinbase"),
        asyncio.create_task(polymarket_scanner(state), name="pm_scanner"),
        asyncio.create_task(polymarket_book_feed(state), name="pm_book"),
        asyncio.create_task(strategy_loop(state, trader), name="strategy"),
        asyncio.create_task(position_monitor(state, trader), name="pos_monitor"),
        asyncio.create_task(dashboard_loop(state, trader), name="dashboard"),
    ]

    try:
        # Wait for shutdown signal or any task to crash
        done, pending = await asyncio.wait(
            tasks + [asyncio.create_task(shutdown_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )

        # Check if a task crashed (not the shutdown event)
        for task in done:
            if task.get_name() != "Task-8" and not task.cancelled():
                exc = task.exception()
                if exc:
                    logger.error(f"Task crashed: {exc}")

    except KeyboardInterrupt:
        logger.info("\n🛑 Keyboard interrupt — shutting down...")
    finally:
        # Cancel all running tasks
        for task in tasks:
            task.cancel()

        # Wait for all tasks to finish cancellation
        await asyncio.gather(*tasks, return_exceptions=True)

        # ── Print final stats ──
        stats = trader.get_stats()
        console.print("\n[bold bright_cyan]═══ FINAL RESULTS ═══[/bold bright_cyan]")
        console.print(f"  💰 Final Balance: [bold]${stats.current_balance:,.2f}[/bold]")

        pnl = stats.current_balance - trader.initial_balance
        pnl_color = "green" if pnl >= 0 else "red"
        console.print(f"  📈 Total PnL: [{pnl_color}]${pnl:+,.2f}[/{pnl_color}]")
        console.print(f"  📊 Total Trades: {stats.total_trades}")

        if stats.total_trades > 0:
            wr_color = "green" if stats.win_rate >= 50 else "red"
            console.print(f"  🏆 Win Rate: [{wr_color}]{stats.win_rate:.1f}%[/{wr_color}]")
            console.print(f"  ⚡ Latency Arb Trades: {stats.latency_arb_count}")
            console.print(f"  📐 Mispricing Trades: {stats.mispricing_count}")

        console.print(f"\n  📁 Trade logs saved to: ./{LOG_DIR}/")
        console.print("[bold bright_cyan]═══════════════════[/bold bright_cyan]\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
