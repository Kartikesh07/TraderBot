"""
╔═══════════════════════════════════════════════════════════════════════╗
║  DASHBOARD.PY — Rich Terminal Dashboard                              ║
║                                                                       ║
║  Provides a beautiful, real-time terminal UI using the Rich library. ║
║  Displays live prices, strategy signals, open positions, PnL,        ║
║  and trading statistics with color-coded formatting.                 ║
║                                                                       ║
║  Refreshes every 500ms using Rich's Live rendering context.          ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

import time
import logging
from collections import deque
from datetime import timedelta
from typing import Dict, List, Optional

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.columns import Columns
from rich import box

from data_ingestion import SharedState, PolymarketContract
from paper_engine import PaperTrader, TradeStats, PositionStatus
from pricing_engine import (
    calculate_rolling_volatility, evaluate_contract, SignalType
)
from config import TRACKED_ASSETS, MISPRICING_THRESHOLD

logger = logging.getLogger("dashboard")
console = Console()


# ─────────────────────────────────────────────────────────────────────
# FORMATTING HELPERS
# ─────────────────────────────────────────────────────────────────────

def format_uptime(seconds: float) -> str:
    """Format seconds into a human-readable uptime string."""
    td = timedelta(seconds=int(seconds))
    hours, remainder = divmod(int(td.total_seconds()), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


def format_price(price: float, symbol: str = "") -> str:
    """Format a crypto price with appropriate decimal places."""
    if price >= 1000:
        return f"${price:,.2f}"
    elif price >= 1:
        return f"${price:.4f}"
    else:
        return f"${price:.6f}"


def signal_emoji(gap: float, threshold: float = MISPRICING_THRESHOLD) -> str:
    """Returns appropriate emoji for signal strength."""
    if gap > threshold * 2:
        return "🟢 STRONG BUY"
    elif gap > threshold:
        return "🟢 BUY"
    elif gap > threshold * 0.5:
        return "🟡 WATCH"
    elif gap < -threshold:
        return "🔴 SELL"
    else:
        return "⚪ HOLD"


def connection_status(connected: bool) -> str:
    """Returns status indicator for a connection."""
    return "[green]● LIVE[/green]" if connected else "[red]● DOWN[/red]"


# ─────────────────────────────────────────────────────────────────────
# MAIN DASHBOARD RENDERER
# ─────────────────────────────────────────────────────────────────────

def render_dashboard(state: SharedState, trader: PaperTrader, event_log: deque = None) -> Table:
    """
    Renders the complete dashboard as a Rich renderable.
    
    Layout:
    ┌─────────────────────────────────────┐
    │  HEADER — Bot name, balance, PnL     │
    ├─────────────────────────────────────┤
    │  CONNECTIONS — Feed statuses          │
    ├─────────────────────────────────────┤
    │  LIVE PRICES — Spot vs PM prices     │
    ├─────────────────────────────────────┤
    │  OPEN POSITIONS — Current trades     │
    ├─────────────────────────────────────┤
    │  RECENT TRADES — Last 5 closed       │
    ├─────────────────────────────────────┤
    │  STATISTICS — Win rate, Sharpe, etc  │
    └─────────────────────────────────────┘
    """
    stats = trader.get_stats()
    uptime = time.time() - state.bot_start_time

    # ── Master container ──
    master = Table(
        show_header=False,
        show_edge=True,
        box=box.DOUBLE_EDGE,
        border_style="bright_cyan",
        width=90,
        padding=(0, 1),
    )
    master.add_column("content", justify="left", no_wrap=False)

    # ═══════════════════════════════════════════════════════════════
    # HEADER
    # ═══════════════════════════════════════════════════════════════
    pnl = stats.current_balance - trader.initial_balance
    pnl_pct = (pnl / trader.initial_balance * 100) if trader.initial_balance > 0 else 0
    pnl_color = "green" if pnl >= 0 else "red"

    header = Text()
    header.append("  🚀 POLYMARKET CRYPTO PAPER TRADING BOT\n", style="bold bright_white")
    header.append(f"  💰 Balance: ", style="white")
    header.append(f"${stats.current_balance:,.2f}", style="bold bright_white")
    header.append(f"    📈 PnL: ", style="white")
    header.append(f"${pnl:+,.2f} ({pnl_pct:+.2f}%)", style=f"bold {pnl_color}")
    header.append(f"\n  🏆 Win Rate: ", style="white")

    if stats.total_trades > 0:
        wr_color = "green" if stats.win_rate >= 50 else "red"
        header.append(
            f"{stats.win_rate:.1f}% ({stats.winning_trades}/{stats.total_trades})",
            style=f"bold {wr_color}"
        )
    else:
        header.append("N/A (no trades yet)", style="dim")

    header.append(f"    ⏱️  Uptime: ", style="white")
    header.append(format_uptime(uptime), style="bright_yellow")
    header.append(f"    🔄 Positions: ", style="white")
    header.append(
        f"{len(trader.open_positions)}/{3}",
        style="bright_yellow" if len(trader.open_positions) < 3 else "red"
    )

    master.add_row(header)

    # ═══════════════════════════════════════════════════════════════
    # CONNECTION STATUS
    # ═══════════════════════════════════════════════════════════════
    conn_text = Text()
    conn_text.append("  📡 Feeds: ", style="bold white")
    conn_text.append("Binance ", style="white")
    if state.binance_connected:
        conn_text.append("● ", style="green")
    else:
        conn_text.append("● ", style="red")
    conn_text.append("Coinbase ", style="white")
    if state.coinbase_connected:
        conn_text.append("● ", style="green")
    else:
        conn_text.append("● ", style="red")
    conn_text.append("Polymarket ", style="white")

    n_contracts = sum(1 for c in state.active_contracts.values() if c.active)
    if n_contracts > 0:
        conn_text.append(f"● ({n_contracts} contracts)", style="green")
    else:
        conn_text.append("● Scanning...", style="yellow")

    master.add_row(conn_text)

    # ═══════════════════════════════════════════════════════════════
    # LIVE PRICES TABLE
    # ═══════════════════════════════════════════════════════════════
    price_table = Table(
        title="  📊 LIVE PRICES & SIGNALS",
        title_style="bold bright_cyan",
        box=box.SIMPLE_HEAVY,
        show_edge=False,
        padding=(0, 1),
        width=86,
    )
    price_table.add_column("Asset", style="bold white", width=8)
    price_table.add_column("Spot Price", style="bright_white", width=16, justify="right")
    price_table.add_column("Source", style="dim", width=8)
    price_table.add_column("PM Contract", style="white", width=20)
    price_table.add_column("PM Price", style="bright_yellow", width=10, justify="right")
    price_table.add_column("Gap", width=10, justify="right")
    price_table.add_column("Signal", width=14)

    for asset in TRACKED_ASSETS[:6]:  # Show top 6 assets
        spot = state.get_best_price(asset.symbol)
        spot_str = format_price(spot) if spot else "[dim]—[/dim]"

        # Determine price source
        has_bn = asset.symbol in state.binance_prices
        has_cb = asset.symbol in state.coinbase_prices
        if has_bn and has_cb:
            source = "[green]BN+CB[/green]"
        elif has_bn:
            source = "[yellow]BN[/yellow]"
        elif has_cb:
            source = "[yellow]CB[/yellow]"
        else:
            source = "[red]—[/red]"

        # Find best matching active contract
        best_contract = None
        for c in state.active_contracts.values():
            if c.active and c.asset_symbol == asset.symbol:
                if best_contract is None or c.expiry_ts > best_contract.expiry_ts:
                    best_contract = c

        if best_contract and spot:
            pm_price = best_contract.orderbook.midpoint
            contract_label = f"{asset.symbol} {best_contract.direction} {best_contract.duration_minutes}m"

            # Calculate gap
            vol = calculate_rolling_volatility(state.price_history.get(asset.symbol, []))
            tte = max(0, best_contract.expiry_ts - time.time())

            if vol and tte > 0:
                from pricing_engine import calculate_true_probability
                true_prob = calculate_true_probability(
                    spot, best_contract.strike_price, tte, vol, best_contract.direction
                )
                gap = true_prob - pm_price
                gap_color = "green" if gap > MISPRICING_THRESHOLD else ("yellow" if gap > 0 else "red")
                gap_str = f"[{gap_color}]{gap:+.1%}[/{gap_color}]"
                sig = signal_emoji(gap)
            else:
                gap_str = "[dim]calc...[/dim]"
                sig = "⏳ WAIT"

            price_table.add_row(
                asset.symbol, spot_str, source,
                contract_label, f"${pm_price:.3f}",
                gap_str, sig
            )
        else:
            price_table.add_row(
                asset.symbol, spot_str, source,
                "[dim]No contract[/dim]", "[dim]—[/dim]",
                "[dim]—[/dim]", "[dim]—[/dim]"
            )

    master.add_row(price_table)

    # ═══════════════════════════════════════════════════════════════
    # OPEN POSITIONS
    # ═══════════════════════════════════════════════════════════════
    if trader.open_positions:
        pos_table = Table(
            title="  📋 OPEN POSITIONS",
            title_style="bold bright_green",
            box=box.SIMPLE,
            show_edge=False,
            padding=(0, 1),
            width=86,
        )
        pos_table.add_column("#", style="dim", width=4)
        pos_table.add_column("Market", style="bold white", width=16)
        pos_table.add_column("Entry", style="white", width=10, justify="right")
        pos_table.add_column("Current", style="bright_white", width=10, justify="right")
        pos_table.add_column("Qty", style="dim", width=8, justify="right")
        pos_table.add_column("Unrealized PnL", width=16, justify="right")
        pos_table.add_column("Signal", style="dim", width=12)
        pos_table.add_column("TTL", style="yellow", width=8, justify="right")

        for pos in trader.open_positions:
            # Find current PM price
            contract = state.active_contracts.get(pos.market_id)
            if contract:
                current_price = contract.orderbook.midpoint
                ttl = max(0, contract.expiry_ts - time.time())
                ttl_str = f"{int(ttl)}s"
            else:
                current_price = pos.entry_price
                ttl_str = "?"

            unrealized = (current_price - pos.entry_price) * pos.quantity
            unrealized_pct = (unrealized / pos.cost_basis * 100) if pos.cost_basis > 0 else 0
            pnl_color = "green" if unrealized >= 0 else "red"

            pos_table.add_row(
                str(pos.position_id),
                f"{pos.asset_symbol} {pos.direction}",
                f"${pos.entry_price:.3f}",
                f"${current_price:.3f}",
                f"{pos.quantity:.1f}",
                f"[{pnl_color}]${unrealized:+.2f} ({unrealized_pct:+.1f}%)[/{pnl_color}]",
                pos.signal_type,
                ttl_str,
            )

        master.add_row(pos_table)
    else:
        no_pos = Text("  📋 No open positions — scanning for opportunities...", style="dim italic")
        master.add_row(no_pos)

    # ═══════════════════════════════════════════════════════════════
    # RECENT TRADES (Last 5)
    # ═══════════════════════════════════════════════════════════════
    if trader.closed_positions:
        trade_table = Table(
            title="  📜 RECENT TRADES",
            title_style="bold bright_magenta",
            box=box.SIMPLE,
            show_edge=False,
            padding=(0, 1),
            width=86,
        )
        trade_table.add_column("Time", style="dim", width=10)
        trade_table.add_column("Market", style="bold white", width=14)
        trade_table.add_column("Entry→Exit", style="white", width=20)
        trade_table.add_column("PnL", width=16, justify="right")
        trade_table.add_column("Reason", style="dim", width=12)
        trade_table.add_column("", width=4)

        recent = trader.closed_positions[-5:]
        for trade in reversed(recent):
            t = time.strftime("%H:%M:%S", time.localtime(trade.exit_time))
            pnl_color = "green" if trade.pnl > 0 else "red"
            emoji = "✅" if trade.pnl > 0 else "❌"

            trade_table.add_row(
                t,
                f"{trade.asset_symbol} {trade.direction}",
                f"${trade.entry_price:.3f} → ${trade.exit_price:.3f}",
                f"[{pnl_color}]${trade.pnl:+.2f} ({trade.pnl_pct:+.1f}%)[/{pnl_color}]",
                trade.exit_reason,
                emoji,
            )

        master.add_row(trade_table)

    # ═══════════════════════════════════════════════════════════════
    # LIVE EVENT LOG (replaces console logging while dashboard is active)
    # ═══════════════════════════════════════════════════════════════
    if event_log and len(event_log) > 0:
        log_text = Text()
        log_text.append("  📝 LIVE EVENTS\n", style="bold bright_white")
        for entry in list(event_log)[-8:]:
            ts = time.strftime("%H:%M:%S")
            log_text.append(f"  {ts}  ", style="dim")
            log_text.append_text(Text.from_markup(str(entry)))
            log_text.append("\n")
        master.add_row(log_text)

    # ═══════════════════════════════════════════════════════════════
    # TRADING STATISTICS FOOTER
    # ═══════════════════════════════════════════════════════════════
    footer = Text()
    footer.append("  📊 Stats: ", style="bold white")
    footer.append(f"Trades: {stats.total_trades}", style="white")
    footer.append(f" │ Avg PnL: ${stats.avg_pnl_per_trade:+.2f}", style="white")
    footer.append(f" │ Best: ${stats.best_trade:+.2f}", style="green")
    footer.append(f" │ Worst: ${stats.worst_trade:+.2f}", style="red")
    footer.append(f" │ Max DD: {stats.max_drawdown:.1f}%", style="yellow")
    footer.append(
        f"\n  ⚡ Latency Arb: {stats.latency_arb_count}",
        style="bright_cyan"
    )
    footer.append(
        f" │ 📐 Mispricing: {stats.mispricing_count}",
        style="bright_magenta"
    )
    if stats.avg_hold_time_seconds > 0:
        footer.append(
            f" │ ⏱️  Avg Hold: {stats.avg_hold_time_seconds:.0f}s",
            style="dim"
        )

    master.add_row(footer)

    return master
