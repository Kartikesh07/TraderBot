"""
╔═══════════════════════════════════════════════════════════════════════╗
║  PAPER_ENGINE.PY — Paper Trading Simulator                           ║
║                                                                       ║
║  Simulates realistic execution on Polymarket without real capital.   ║
║  Models:                                                              ║
║  • Dynamic taker fees (1.8% peak at $0.50, scaling to $0 at edges)  ║
║  • Slippage from CLOB orderbook depth                                ║
║  • Position sizing (10% of bankroll per trade)                       ║
║  • Max 3 concurrent positions                                        ║
║  • Trade logging (JSON + CSV)                                        ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

import json
import csv
import os
import time
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict
from enum import Enum

from config import (
    INITIAL_BALANCE, RISK_PER_TRADE, MAX_CONCURRENT_POSITIONS,
    PEAK_TAKER_FEE, BASE_SLIPPAGE_BPS, STOP_LOSS_PCT,
    LOG_DIR, LOG_TRADES_JSON, LOG_TRADES_CSV,
)

logger = logging.getLogger("paper_engine")


# ─────────────────────────────────────────────────────────────────────
# DATA MODELS
# ─────────────────────────────────────────────────────────────────────

class PositionStatus(Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass
class Position:
    """Represents a single paper trading position."""
    position_id: int = 0
    market_id: str = ""
    asset_symbol: str = ""         # e.g., "BTC"
    direction: str = ""            # "UP" or "DOWN"
    signal_type: str = ""          # What triggered the entry (MISPRICING / LATENCY_ARB)
    question: str = ""             # Polymarket market question

    # ── Entry ──
    entry_price: float = 0.0       # PM token price at entry (after slippage)
    entry_true_prob: float = 0.0   # Our probability when we entered
    entry_gap: float = 0.0         # Gap at entry (true_prob - pm_price)
    quantity: float = 0.0          # Number of tokens purchased
    cost_basis: float = 0.0        # Total cost including fees (in USDC)
    entry_fee: float = 0.0         # Fee paid on entry
    entry_slippage: float = 0.0    # Slippage cost on entry
    entry_time: float = 0.0        # Unix timestamp of entry

    # ── Exit ──
    exit_price: float = 0.0        # PM token price at exit
    exit_fee: float = 0.0          # Fee paid on exit
    exit_slippage: float = 0.0     # Slippage cost on exit
    exit_time: float = 0.0         # Unix timestamp of exit
    exit_reason: str = ""          # Why we exited (MEAN_REVERT / STOP_LOSS / EXPIRY)

    # ── PnL ──
    pnl: float = 0.0              # Profit/loss in USDC
    pnl_pct: float = 0.0          # PnL as percentage of cost basis
    status: PositionStatus = PositionStatus.OPEN

    # ── Market State at Entry ──
    spot_price_at_entry: float = 0.0
    strike_price: float = 0.0
    expiry_ts: float = 0.0


@dataclass
class TradeStats:
    """Aggregated trading statistics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl_per_trade: float = 0.0
    best_trade: float = 0.0
    worst_trade: float = 0.0
    max_drawdown: float = 0.0
    current_balance: float = INITIAL_BALANCE
    peak_balance: float = INITIAL_BALANCE
    latency_arb_count: int = 0
    mispricing_count: int = 0
    avg_hold_time_seconds: float = 0.0


# ─────────────────────────────────────────────────────────────────────
# FEE & SLIPPAGE MODELS
# ─────────────────────────────────────────────────────────────────────

def calculate_taker_fee(token_price: float, notional: float) -> float:
    """
    Calculates the Polymarket taker fee using their dynamic model.
    
    Polymarket's fee structure (Crypto category):
    - Peak fee: 1.8% at token price = $0.50 (maximum uncertainty)
    - Fee scales DOWN linearly toward $0.00 and $1.00
    - Formula: effective_rate = PEAK_FEE × 2 × min(price, 1 - price)
    
    Intuition: When the market is at 50/50, there's maximum uncertainty
    and therefore maximum fee. As outcomes become more certain (near
    $0 or $1), fees decrease because the market needs less incentive
    for liquidity — prices are nearly deterministic.
    
    Examples:
      Price $0.50 → rate = 1.8% × 2 × 0.50 = 1.8%  (peak)
      Price $0.30 → rate = 1.8% × 2 × 0.30 = 1.08%
      Price $0.10 → rate = 1.8% × 2 × 0.10 = 0.36%
      Price $0.90 → rate = 1.8% × 2 × 0.10 = 0.36%  (symmetric!)
    
    Args:
        token_price: Current token price (0 to 1)
        notional: Trade notional value in USDC
    
    Returns:
        Fee amount in USDC
    """
    # Clamp price to valid range
    price = max(0.01, min(0.99, token_price))

    # Effective fee rate based on distance from 0 or 1
    effective_rate = PEAK_TAKER_FEE * 2.0 * min(price, 1.0 - price)

    return notional * effective_rate


def calculate_slippage(
    token_price: float,
    quantity: float,
    orderbook_asks: List[Dict],
    base_bps: int = BASE_SLIPPAGE_BPS
) -> float:
    """
    Simulates realistic slippage based on CLOB orderbook depth.
    
    How it works:
    1. If we have orderbook data, walk through the ask levels
       to simulate filling our order across multiple price levels.
    2. If no orderbook data available, use a base slippage estimate.
    
    The idea: Large orders can't all fill at the best price.
    If the best ask has 100 tokens at $0.30, and we want 200 tokens,
    we'd fill 100 at $0.30 and the remaining 100 at the next level
    (maybe $0.31), resulting in an average fill price of $0.305.
    
    Args:
        token_price: Expected token price
        quantity: Number of tokens to buy
        orderbook_asks: Polymarket CLOB ask levels
        base_bps: Base slippage in basis points (fallback)
    
    Returns:
        Slippage cost in USDC (the extra cost above ideal execution)
    """
    if not orderbook_asks or len(orderbook_asks) == 0:
        # No orderbook data — use base slippage estimate
        return token_price * quantity * (base_bps / 10_000)

    # Walk the orderbook to calculate average fill price
    remaining = quantity
    total_cost = 0.0

    for level in orderbook_asks[:5]:  # Check top 5 levels
        try:
            level_price = float(level.get("price", token_price))
            level_size = float(level.get("size", 0))
        except (ValueError, TypeError):
            continue

        if level_size <= 0:
            continue

        fill_at_level = min(remaining, level_size)
        total_cost += fill_at_level * level_price
        remaining -= fill_at_level

        if remaining <= 0:
            break

    # If there's still remaining quantity, fill at a worse price (last level + spread)
    if remaining > 0:
        worst_price = min(token_price * 1.02, 0.99)  # 2% worse
        total_cost += remaining * worst_price

    # Slippage = actual cost - ideal cost (all at best price)
    ideal_cost = quantity * token_price
    slippage = max(0.0, total_cost - ideal_cost)

    return slippage


# ─────────────────────────────────────────────────────────────────────
# PAPER TRADER CLASS
# ─────────────────────────────────────────────────────────────────────

class PaperTrader:
    """
    Paper trading engine that simulates Polymarket execution.
    
    Key features:
    - Initialized with $1,000 USDC simulated balance
    - Sizes each trade at 10% of current bankroll
    - Max 3 concurrent positions
    - Accounts for taker fees and slippage on both entry and exit
    - Logs every trade to JSON and CSV for analysis
    """

    def __init__(self, initial_balance: float = INITIAL_BALANCE):
        self.balance: float = initial_balance
        self.initial_balance: float = initial_balance
        self.positions: List[Position] = []
        self.closed_positions: List[Position] = []
        self.trade_counter: int = 0
        self.peak_balance: float = initial_balance

        # Ensure log directory exists
        os.makedirs(LOG_DIR, exist_ok=True)

        logger.info(f"💰 PaperTrader initialized with ${initial_balance:.2f} USDC")

    @property
    def open_positions(self) -> List[Position]:
        """Returns only currently open positions."""
        return [p for p in self.positions if p.status == PositionStatus.OPEN]

    @property
    def can_open_position(self) -> bool:
        """Checks if we can open a new position (respects max concurrent limit)."""
        return len(self.open_positions) < MAX_CONCURRENT_POSITIONS

    def is_already_positioned(self, market_id: str) -> bool:
        """Check if we already have an open position in this market."""
        return any(
            p.market_id == market_id and p.status == PositionStatus.OPEN
            for p in self.positions
        )

    def open_position(
        self,
        market_id: str,
        asset_symbol: str,
        direction: str,
        signal_type: str,
        token_price: float,
        true_prob: float,
        gap: float,
        spot_price: float,
        strike_price: float,
        expiry_ts: float,
        question: str = "",
        orderbook_asks: List[Dict] = None,
    ) -> Optional[Position]:
        """
        Opens a new paper trading position.
        
        Execution Flow:
        1. Check if we can open (< 3 positions, have balance)
        2. Calculate position size (10% of current bankroll)
        3. Calculate and deduct taker fee
        4. Calculate and deduct slippage
        5. Record the position
        6. Log the trade
        
        Args:
            market_id: Polymarket market ID
            asset_symbol: e.g., "BTC"
            direction: "UP" or "DOWN"
            signal_type: "MISPRICING" or "LATENCY_ARB"
            token_price: Current PM token price (the price we're buying at)
            true_prob: Our calculated true probability
            gap: Difference between true_prob and token_price
            spot_price: Current exchange spot price
            strike_price: Contract strike price
            expiry_ts: Unix timestamp of contract expiry
            question: Market question text
            orderbook_asks: CLOB ask levels for slippage calculation
        
        Returns:
            The opened Position, or None if opening failed
        """
        if not self.can_open_position:
            logger.warning("⚠️ Cannot open position — max concurrent positions reached")
            return None

        if self.is_already_positioned(market_id):
            logger.debug(f"Already positioned in market {market_id}")
            return None

        # ── Position Sizing: 10% of current bankroll ──
        trade_budget = self.balance * RISK_PER_TRADE
        if trade_budget < 1.0:
            logger.warning("⚠️ Insufficient balance for trade")
            return None

        # ── Calculate fees ──
        entry_fee = calculate_taker_fee(token_price, trade_budget)

        # ── Calculate slippage ──
        # Number of tokens we can buy (before slippage)
        estimated_quantity = (trade_budget - entry_fee) / token_price if token_price > 0 else 0
        entry_slippage = calculate_slippage(
            token_price, estimated_quantity, orderbook_asks or []
        )

        # ── Effective entry ──
        effective_budget = trade_budget - entry_fee - entry_slippage
        if effective_budget <= 0 or token_price <= 0:
            logger.warning("⚠️ Entry costs exceed budget")
            return None

        quantity = effective_budget / token_price
        effective_entry_price = trade_budget / quantity if quantity > 0 else token_price

        # ── Deduct from balance ──
        self.balance -= trade_budget

        # ── Create position record ──
        self.trade_counter += 1
        position = Position(
            position_id=self.trade_counter,
            market_id=market_id,
            asset_symbol=asset_symbol,
            direction=direction,
            signal_type=signal_type,
            question=question,
            entry_price=effective_entry_price,
            entry_true_prob=true_prob,
            entry_gap=gap,
            quantity=quantity,
            cost_basis=trade_budget,
            entry_fee=entry_fee,
            entry_slippage=entry_slippage,
            entry_time=time.time(),
            spot_price_at_entry=spot_price,
            strike_price=strike_price,
            expiry_ts=expiry_ts,
            status=PositionStatus.OPEN,
        )

        self.positions.append(position)

        logger.info(
            f"📈 OPENED #{position.position_id}: {asset_symbol} {direction} "
            f"@ ${token_price:.3f} | Qty: {quantity:.1f} | Cost: ${trade_budget:.2f} "
            f"| Fee: ${entry_fee:.2f} | Slip: ${entry_slippage:.2f} "
            f"| Signal: {signal_type} | Gap: {gap:.1%}"
        )

        return position

    def close_position(
        self,
        position: Position,
        exit_price: float,
        reason: str,
        orderbook_bids: List[Dict] = None,
    ) -> Optional[Position]:
        """
        Closes an open paper trading position.
        
        Execution Flow:
        1. Calculate gross exit proceeds (quantity × exit_price)
        2. Deduct exit taker fee
        3. Deduct exit slippage
        4. Calculate P&L
        5. Credit balance
        6. Log the trade
        
        For expiry settlement:
        - If the contract resolves YES → price = $1.00
        - If the contract resolves NO  → price = $0.00
        
        Args:
            position: The position to close
            exit_price: Token price at exit (0-1, or 0/1 for settlement)
            reason: Why closing (MEAN_REVERT / STOP_LOSS / EXPIRY_WIN / EXPIRY_LOSS)
            orderbook_bids: CLOB bid levels for slippage calculation
        
        Returns:
            The closed Position with PnL calculated
        """
        if position.status != PositionStatus.OPEN:
            return None

        # ── Calculate gross proceeds ──
        gross_proceeds = position.quantity * exit_price

        # ── Exit fee ──
        exit_fee = calculate_taker_fee(exit_price, gross_proceeds)

        # ── Exit slippage (only for non-settlement exits) ──
        if reason in ["EXPIRY_WIN", "EXPIRY_LOSS"]:
            exit_slippage = 0.0  # No slippage on settlement
        else:
            exit_slippage = calculate_slippage(
                exit_price, position.quantity, orderbook_bids or [],
                base_bps=BASE_SLIPPAGE_BPS
            )

        # ── Net proceeds ──
        net_proceeds = gross_proceeds - exit_fee - exit_slippage

        # ── PnL ──
        pnl = net_proceeds - position.cost_basis
        pnl_pct = (pnl / position.cost_basis * 100) if position.cost_basis > 0 else 0

        # ── Update position ──
        position.exit_price = exit_price
        position.exit_fee = exit_fee
        position.exit_slippage = exit_slippage
        position.exit_time = time.time()
        position.exit_reason = reason
        position.pnl = pnl
        position.pnl_pct = pnl_pct
        position.status = PositionStatus.CLOSED

        # ── Credit balance ──
        self.balance += net_proceeds

        # ── Track peak balance for drawdown ──
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance

        # ── Move to closed list ──
        self.closed_positions.append(position)

        # ── Log to files ──
        self._log_trade(position)

        emoji = "✅" if pnl > 0 else "❌"
        logger.info(
            f"{emoji} CLOSED #{position.position_id}: {position.asset_symbol} "
            f"{position.direction} | Entry: ${position.entry_price:.3f} → "
            f"Exit: ${exit_price:.3f} | PnL: ${pnl:+.2f} ({pnl_pct:+.1f}%) "
            f"| Reason: {reason} | Bal: ${self.balance:.2f}"
        )

        return position

    def check_stop_losses(self, get_current_price_fn=None):
        """
        Checks all open positions for stop-loss conditions.
        
        Stop-loss triggers if the current token value drops below
        50% of our cost basis (configurable via STOP_LOSS_PCT).
        
        Args:
            get_current_price_fn: Optional callable(market_id) → current_price
        """
        for pos in self.open_positions:
            if get_current_price_fn:
                current_price = get_current_price_fn(pos.market_id)
                if current_price is None:
                    continue

                current_value = pos.quantity * current_price
                loss_pct = 1.0 - (current_value / pos.cost_basis) if pos.cost_basis > 0 else 0

                if loss_pct >= STOP_LOSS_PCT:
                    self.close_position(pos, current_price, "STOP_LOSS")

    def check_expiries(self, current_spot_prices: Dict[str, float]):
        """
        Checks all open positions for expiry settlement.
        
        If the current time has passed the contract expiry timestamp,
        we settle the position:
        - If spot price is favorable vs strike → settle at $1.00 (WIN)
        - If spot price is unfavorable → settle at $0.00 (LOSS)
        
        Args:
            current_spot_prices: Dict mapping asset symbols to current prices
        """
        now = time.time()

        for pos in self.open_positions:
            if now >= pos.expiry_ts:
                spot = current_spot_prices.get(pos.asset_symbol)
                if spot is None:
                    continue

                # Determine outcome
                if pos.direction == "UP":
                    won = spot > pos.strike_price
                else:  # DOWN
                    won = spot < pos.strike_price

                if won:
                    self.close_position(pos, 1.0, "EXPIRY_WIN")
                else:
                    self.close_position(pos, 0.0, "EXPIRY_LOSS")

    def get_stats(self) -> TradeStats:
        """Calculate aggregated trading statistics."""
        stats = TradeStats()
        stats.current_balance = self.balance
        stats.peak_balance = self.peak_balance

        closed = self.closed_positions
        stats.total_trades = len(closed)

        if stats.total_trades > 0:
            pnls = [p.pnl for p in closed]
            hold_times = [p.exit_time - p.entry_time for p in closed if p.exit_time > 0]

            stats.winning_trades = sum(1 for p in pnls if p > 0)
            stats.losing_trades = sum(1 for p in pnls if p <= 0)
            stats.win_rate = stats.winning_trades / stats.total_trades * 100
            stats.total_pnl = sum(pnls)
            stats.avg_pnl_per_trade = stats.total_pnl / stats.total_trades
            stats.best_trade = max(pnls)
            stats.worst_trade = min(pnls)
            stats.avg_hold_time_seconds = (
                sum(hold_times) / len(hold_times) if hold_times else 0
            )

            # Signal type counts
            stats.latency_arb_count = sum(
                1 for p in closed if p.signal_type == "LATENCY_ARB"
            )
            stats.mispricing_count = sum(
                1 for p in closed if p.signal_type == "MISPRICING"
            )

        # Max drawdown from peak
        if stats.peak_balance > 0:
            stats.max_drawdown = (
                (stats.peak_balance - self.balance) / stats.peak_balance * 100
            )

        return stats

    def _log_trade(self, position: Position):
        """Persist a closed trade to JSON and CSV log files."""
        trade_dict = {
            "position_id": position.position_id,
            "asset": position.asset_symbol,
            "direction": position.direction,
            "signal_type": position.signal_type,
            "entry_price": round(position.entry_price, 4),
            "exit_price": round(position.exit_price, 4),
            "quantity": round(position.quantity, 2),
            "cost_basis": round(position.cost_basis, 2),
            "entry_fee": round(position.entry_fee, 4),
            "exit_fee": round(position.exit_fee, 4),
            "entry_slippage": round(position.entry_slippage, 4),
            "exit_slippage": round(position.exit_slippage, 4),
            "pnl": round(position.pnl, 2),
            "pnl_pct": round(position.pnl_pct, 2),
            "entry_time": position.entry_time,
            "exit_time": position.exit_time,
            "hold_time_seconds": round(position.exit_time - position.entry_time, 1),
            "exit_reason": position.exit_reason,
            "spot_at_entry": position.spot_price_at_entry,
            "strike_price": position.strike_price,
            "entry_gap": round(position.entry_gap, 4),
            "question": position.question,
        }

        # ── JSON log ──
        json_path = os.path.join(LOG_DIR, LOG_TRADES_JSON)
        try:
            existing = []
            if os.path.exists(json_path):
                with open(json_path, "r") as f:
                    existing = json.load(f)
            existing.append(trade_dict)
            with open(json_path, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            logger.error(f"Error writing JSON log: {e}")

        # ── CSV log ──
        csv_path = os.path.join(LOG_DIR, LOG_TRADES_CSV)
        try:
            file_exists = os.path.exists(csv_path)
            with open(csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=trade_dict.keys())
                if not file_exists:
                    writer.writeheader()
                writer.writerow(trade_dict)
        except Exception as e:
            logger.error(f"Error writing CSV log: {e}")
