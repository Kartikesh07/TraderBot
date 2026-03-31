"""
╔═══════════════════════════════════════════════════════════════════════╗
║  CONFIG.PY — Central Configuration Hub                               ║
║  All tunable parameters, API endpoints, and trading constants.       ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

# ─────────────────────────────────────────────────────────────────────
# TRADING PARAMETERS
# ─────────────────────────────────────────────────────────────────────

INITIAL_BALANCE: float = 1000.0       # Starting simulated bankroll in USDC
RISK_PER_TRADE: float = 0.10          # Risk exactly 10% of current bankroll per trade
MAX_CONCURRENT_POSITIONS: int = 3     # Hard cap on simultaneous open positions

# ─────────────────────────────────────────────────────────────────────
# STRATEGY THRESHOLDS
# ─────────────────────────────────────────────────────────────────────

# Mispricing Arbitrage:
# If the calculated true probability exceeds the Polymarket token price
# by more than this threshold, trigger a BUY entry.
# Example: True prob = 55%, PM price = 30¢ → gap = 25% > 8% → BUY
MISPRICING_THRESHOLD: float = 0.08    # 8% minimum gap to enter

# Exit when mispricing gap closes to below this level (mean reversion exit)
MISPRICING_EXIT_THRESHOLD: float = 0.02  # 2% gap → exit

# Latency Arbitrage:
# If the Polymarket orderbook hasn't updated for this many milliseconds
# after a spot price cross, consider it a latency arb opportunity.
LATENCY_ARB_THRESHOLD_MS: int = 500   # 500ms stale book threshold

# Momentum confirmation: spot price must cross strike by at least this %
# to confirm an aggressive cross (filters noise)
LATENCY_ARB_CROSS_PCT: float = 0.001  # 0.1% beyond strike = confirmed cross

# ─────────────────────────────────────────────────────────────────────
# FEE & SLIPPAGE MODEL (Polymarket Crypto Category)
# ─────────────────────────────────────────────────────────────────────

# Polymarket uses a dynamic, probability-based fee model.
# Peak taker fee occurs at token price = $0.50 (maximum uncertainty).
# Fee scales DOWN linearly toward $0.00 and $1.00 endpoints.
# Formula: effective_fee = PEAK_TAKER_FEE * 2 * min(price, 1 - price)
PEAK_TAKER_FEE: float = 0.018        # 1.8% peak for crypto category

# Simulated slippage in basis points (1 bps = 0.01%)
# This is the BASE slippage; dynamic slippage is calculated from CLOB depth.
BASE_SLIPPAGE_BPS: int = 50           # 0.5% base slippage

# Stop-loss: exit if position value drops by this fraction
STOP_LOSS_PCT: float = 0.50           # 50% position value loss → emergency exit

# ─────────────────────────────────────────────────────────────────────
# VOLATILITY CALCULATION
# ─────────────────────────────────────────────────────────────────────

# Number of recent ticks to use for rolling volatility estimation
VOLATILITY_WINDOW: int = 60

# Minimum number of ticks required before we can calculate volatility
MIN_TICKS_FOR_VOL: int = 20

# ─────────────────────────────────────────────────────────────────────
# CRYPTO ASSETS TO MONITOR
# ─────────────────────────────────────────────────────────────────────

@dataclass
class CryptoAsset:
    """Represents a tracked crypto asset across exchanges."""
    name: str                    # Human-readable name (e.g., "Bitcoin")
    symbol: str                  # Canonical symbol (e.g., "BTC")
    binance_symbol: str          # Binance pair (e.g., "btcusdt")
    coinbase_product: str        # Coinbase product (e.g., "BTC-USD")
    polymarket_slug_pattern: str # Pattern used to find PM markets

TRACKED_ASSETS: List[CryptoAsset] = [
    CryptoAsset("Bitcoin",    "BTC",  "btcusdt",  "BTC-USD",  "btc"),
    CryptoAsset("Ethereum",   "ETH",  "ethusdt",  "ETH-USD",  "eth"),
    CryptoAsset("Solana",     "SOL",  "solusdt",  "SOL-USD",  "sol"),
    CryptoAsset("XRP",        "XRP",  "xrpusdt",  "XRP-USD",  "xrp"),
    CryptoAsset("Dogecoin",   "DOGE", "dogeusdt", "DOGE-USD", "doge"),
    CryptoAsset("Cardano",    "ADA",  "adausdt",  "ADA-USD",  "ada"),
    CryptoAsset("Avalanche",  "AVAX", "avaxusdt", "AVAX-USD", "avax"),
    CryptoAsset("Chainlink",  "LINK", "linkusdt", "LINK-USD", "link"),
    CryptoAsset("Polkadot",   "DOT",  "dotusdt",  "DOT-USD",  "dot"),
    CryptoAsset("Polygon",    "POL",  "polusdt",  "POL-USD",  "pol"),
]

# ─────────────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────

# Binance WebSocket — Combined aggTrade streams for all tracked assets
BINANCE_WS_BASE = "wss://stream.binance.com:9443"

# Coinbase Advanced Trade WebSocket — ticker channel
COINBASE_WS_URL = "wss://advanced-trade-ws.coinbase.com"

# Polymarket CLOB WebSocket — real-time orderbook updates
POLYMARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/"

# Polymarket Gamma API — market discovery (public, no auth needed)
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# Polymarket CLOB REST API — orderbook, midpoint, price (public, no auth needed)
CLOB_API_URL = "https://clob.polymarket.com"

# ─────────────────────────────────────────────────────────────────────
# TIMING & INTERVALS
# ─────────────────────────────────────────────────────────────────────

# How often to poll Polymarket for new markets (seconds)
MARKET_SCAN_INTERVAL: int = 30

# Strategy evaluation loop interval (seconds)
STRATEGY_LOOP_INTERVAL: float = 0.2  # 200ms — fast enough for latency arb

# Position monitor check interval (seconds)
POSITION_CHECK_INTERVAL: float = 1.0

# Dashboard refresh interval (seconds)
DASHBOARD_REFRESH_INTERVAL: float = 0.5

# WebSocket reconnect backoff settings
WS_RECONNECT_MIN_DELAY: float = 1.0    # Start at 1 second
WS_RECONNECT_MAX_DELAY: float = 30.0   # Cap at 30 seconds
WS_RECONNECT_MULTIPLIER: float = 2.0   # Exponential backoff factor

# ─────────────────────────────────────────────────────────────────────
# HEALTH / KEEP-ALIVE FOR RENDER DEPLOYMENT
# ─────────────────────────────────────────────────────────────────────

# The bot exposes a lightweight HTTP health endpoint on this port
# Render will route external traffic to this port.
HEALTH_PORT: int = int(os.environ.get("PORT", 10000))

# Self-ping interval in seconds (must be < 15 minutes = 900s)
# We ping ourselves every 10 minutes to prevent Render free-tier spin-down.
KEEP_ALIVE_INTERVAL: int = 600  # 10 minutes

# ─────────────────────────────────────────────────────────────────────
# LOGGING & OUTPUT
# ─────────────────────────────────────────────────────────────────────

LOG_DIR: str = "logs"
LOG_TRADES_JSON: str = "trades.json"
LOG_TRADES_CSV: str = "trades.csv"
