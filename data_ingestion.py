"""
╔═══════════════════════════════════════════════════════════════════════╗
║  DATA_INGESTION.PY — Real-Time Market Data Feeds                     ║
║                                                                       ║
║  Connects to Binance, Coinbase, and Polymarket via WebSockets        ║
║  to provide ultra-low latency price data for the strategy engine.    ║
║                                                                       ║
║  Architecture:                                                        ║
║  • Each feed runs as an independent asyncio task                     ║
║  • All feeds write to a shared SharedState dataclass                 ║
║  • Auto-reconnect with exponential backoff on disconnect             ║
║  • Polymarket market scanner polls Gamma API every 30s               ║
╚═══════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from collections import deque

import aiohttp
import websockets

from config import (
    TRACKED_ASSETS, CryptoAsset,
    BINANCE_WS_BASE, COINBASE_WS_URL,
    POLYMARKET_WS_URL, GAMMA_API_URL, CLOB_API_URL,
    MARKET_SCAN_INTERVAL, VOLATILITY_WINDOW,
    WS_RECONNECT_MIN_DELAY, WS_RECONNECT_MAX_DELAY, WS_RECONNECT_MULTIPLIER,
)

logger = logging.getLogger("data_ingestion")


# ─────────────────────────────────────────────────────────────────────
# SHARED STATE — Thread-safe container for all market data
# ─────────────────────────────────────────────────────────────────────

@dataclass
class OrderBookSnapshot:
    """Snapshot of Polymarket CLOB orderbook for a single token."""
    token_id: str = ""
    bids: List[Dict] = field(default_factory=list)   # [{"price": "0.50", "size": "100"}, ...]
    asks: List[Dict] = field(default_factory=list)
    best_bid: float = 0.0
    best_ask: float = 1.0
    midpoint: float = 0.5
    last_update_ts: float = 0.0   # Unix timestamp of last update


@dataclass
class PolymarketContract:
    """Represents a single Polymarket crypto directional contract."""
    market_id: str = ""
    condition_id: str = ""
    token_id_yes: str = ""         # Token ID for YES outcome
    token_id_no: str = ""          # Token ID for NO outcome
    question: str = ""             # e.g., "Will BTC be above $87,250 at 12:05 PM?"
    asset_symbol: str = ""         # e.g., "BTC"
    direction: str = ""            # "UP" or "DOWN"
    strike_price: float = 0.0      # The target price threshold
    expiry_ts: float = 0.0         # Unix timestamp of contract expiration
    duration_minutes: int = 0      # 5 or 15 minutes
    slug: str = ""
    active: bool = True
    orderbook: OrderBookSnapshot = field(default_factory=OrderBookSnapshot)


@dataclass
class SharedState:
    """
    Central data store shared across all async tasks.
    Contains real-time prices from exchanges and Polymarket market data.
    """
    # ── Exchange Spot Prices ──
    # Key: asset symbol (e.g., "BTC"), Value: latest price
    binance_prices: Dict[str, float] = field(default_factory=dict)
    coinbase_prices: Dict[str, float] = field(default_factory=dict)

    # ── Tick History (for volatility calculation) ──
    # Key: asset symbol, Value: deque of (timestamp, price) tuples
    price_history: Dict[str, deque] = field(default_factory=lambda: {
        asset.symbol: deque(maxlen=VOLATILITY_WINDOW * 5)
        for asset in TRACKED_ASSETS
    })

    # ── Polymarket Contracts ──
    # Key: market_id, Value: PolymarketContract
    active_contracts: Dict[str, PolymarketContract] = field(default_factory=dict)

    # ── Connection Status ──
    binance_connected: bool = False
    coinbase_connected: bool = False
    polymarket_ws_connected: bool = False

    # ── Timing ──
    last_binance_tick: float = 0.0
    last_coinbase_tick: float = 0.0
    last_market_scan: float = 0.0
    bot_start_time: float = field(default_factory=time.time)

    def get_best_price(self, symbol: str) -> Optional[float]:
        """
        Returns the best available spot price for an asset.
        Averages Binance and Coinbase if both available; falls back to either.
        """
        bn = self.binance_prices.get(symbol)
        cb = self.coinbase_prices.get(symbol)
        if bn and cb:
            return (bn + cb) / 2.0
        return bn or cb or None


# ─────────────────────────────────────────────────────────────────────
# BINANCE WEBSOCKET FEED
# ─────────────────────────────────────────────────────────────────────

async def binance_feed(state: SharedState):
    """
    Connects to Binance's combined aggTrade WebSocket stream.
    
    aggTrade gives us individual trade executions in real-time,
    which is the lowest-latency public price data available.
    Combined streams allow multiple symbols on one connection.
    
    Data format from Binance:
    {
        "stream": "btcusdt@aggTrade",
        "data": {
            "p": "87312.40",   // Price
            "q": "0.001",      // Quantity
            "T": 1711878000000 // Trade time (ms)
        }
    }
    """
    # Build combined stream URL: btcusdt@aggTrade/ethusdt@aggTrade/...
    streams = "/".join(f"{a.binance_symbol}@aggTrade" for a in TRACKED_ASSETS)
    url = f"{BINANCE_WS_BASE}/stream?streams={streams}"

    delay = WS_RECONNECT_MIN_DELAY

    while True:
        try:
            logger.info(f"[BINANCE] Connecting to {url[:80]}...")
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                state.binance_connected = True
                delay = WS_RECONNECT_MIN_DELAY  # Reset backoff on successful connect
                logger.info("[BINANCE] ✅ Connected — streaming aggTrade data")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        stream_name = data.get("stream", "")
                        trade = data.get("data", {})

                        # Extract the symbol from stream name: "btcusdt@aggTrade" → "btcusdt"
                        binance_symbol = stream_name.split("@")[0]

                        # Map binance symbol back to our canonical symbol
                        for asset in TRACKED_ASSETS:
                            if asset.binance_symbol == binance_symbol:
                                price = float(trade.get("p", 0))
                                if price > 0:
                                    state.binance_prices[asset.symbol] = price
                                    state.last_binance_tick = time.time()
                                    # Record tick for volatility calculation
                                    state.price_history[asset.symbol].append(
                                        (time.time(), price)
                                    )
                                break
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        logger.debug(f"[BINANCE] Parse error: {e}")
                        continue

        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            state.binance_connected = False
            logger.warning(f"[BINANCE] ❌ Disconnected: {e}. Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * WS_RECONNECT_MULTIPLIER, WS_RECONNECT_MAX_DELAY)
        except asyncio.CancelledError:
            logger.info("[BINANCE] Feed cancelled — shutting down")
            state.binance_connected = False
            return
        except Exception as e:
            state.binance_connected = False
            logger.error(f"[BINANCE] Unexpected error: {e}. Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * WS_RECONNECT_MULTIPLIER, WS_RECONNECT_MAX_DELAY)


# ─────────────────────────────────────────────────────────────────────
# COINBASE WEBSOCKET FEED
# ─────────────────────────────────────────────────────────────────────

async def coinbase_feed(state: SharedState):
    """
    Connects to Coinbase Advanced Trade WebSocket for ticker data.
    
    This provides a second independent price source for cross-validation
    and more robust price estimation. If Binance has a flash crash or
    stale data, Coinbase acts as a sanity check.
    
    Subscription message format:
    {
        "type": "subscribe",
        "product_ids": ["BTC-USD", "ETH-USD", ...],
        "channel": "ticker"
    }
    """
    products = [a.coinbase_product for a in TRACKED_ASSETS]
    subscribe_msg = {
        "type": "subscribe",
        "product_ids": products,
        "channel": "ticker"
    }

    delay = WS_RECONNECT_MIN_DELAY

    while True:
        try:
            logger.info(f"[COINBASE] Connecting to {COINBASE_WS_URL}...")
            async with websockets.connect(COINBASE_WS_URL, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps(subscribe_msg))
                state.coinbase_connected = True
                delay = WS_RECONNECT_MIN_DELAY
                logger.info("[COINBASE] ✅ Connected — streaming ticker data")

                async for message in ws:
                    try:
                        data = json.loads(message)
                        msg_type = data.get("channel", data.get("type", ""))

                        if msg_type == "ticker":
                            events = data.get("events", [])
                            for event in events:
                                tickers = event.get("tickers", [])
                                for ticker in tickers:
                                    product_id = ticker.get("product_id", "")
                                    price_str = ticker.get("price", "0")
                                    price = float(price_str)

                                    # Map coinbase product to canonical symbol
                                    for asset in TRACKED_ASSETS:
                                        if asset.coinbase_product == product_id and price > 0:
                                            state.coinbase_prices[asset.symbol] = price
                                            state.last_coinbase_tick = time.time()
                                            # Also record for volatility if Binance is down
                                            if asset.symbol not in state.binance_prices:
                                                state.price_history[asset.symbol].append(
                                                    (time.time(), price)
                                                )
                                            break
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        logger.debug(f"[COINBASE] Parse error: {e}")
                        continue

        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            state.coinbase_connected = False
            logger.warning(f"[COINBASE] ❌ Disconnected: {e}. Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * WS_RECONNECT_MULTIPLIER, WS_RECONNECT_MAX_DELAY)
        except asyncio.CancelledError:
            logger.info("[COINBASE] Feed cancelled — shutting down")
            state.coinbase_connected = False
            return
        except Exception as e:
            state.coinbase_connected = False
            logger.error(f"[COINBASE] Unexpected error: {e}. Reconnecting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * WS_RECONNECT_MULTIPLIER, WS_RECONNECT_MAX_DELAY)


# ─────────────────────────────────────────────────────────────────────
# POLYMARKET MARKET SCANNER (REST Polling)
# ─────────────────────────────────────────────────────────────────────

def _parse_pm_question(question: str, asset_symbol: str) -> dict:
    """
    Attempts to parse a Polymarket crypto directional market question
    to extract strike price, direction, and duration.
    
    Examples of market questions:
      "Will BTC be above $87,250 at 12:05 PM UTC?"
      "Will ETH be below $2,050 at 12:15 PM UTC?"
      "Bitcoin above $87000 by 12:05?"
    
    Returns dict with: strike_price, direction, duration_minutes (or empty dict on failure).
    """
    import re

    result = {}
    q_lower = question.lower()

    # Detect direction
    if "above" in q_lower or "up" in q_lower or "higher" in q_lower:
        result["direction"] = "UP"
    elif "below" in q_lower or "down" in q_lower or "lower" in q_lower:
        result["direction"] = "DOWN"
    else:
        return {}

    # Extract strike price — look for dollar amounts like $87,250 or $87250 or 87250
    price_match = re.search(r'\$?([\d,]+\.?\d*)', question)
    if price_match:
        price_str = price_match.group(1).replace(",", "")
        try:
            result["strike_price"] = float(price_str)
        except ValueError:
            return {}
    else:
        return {}

    # Detect duration from question text (5-min vs 15-min intervals)
    # Polymarket crypto directionals typically have timestamps like "12:05" (5-min) or "12:15" (15-min)
    time_match = re.search(r'(\d{1,2}):(\d{2})', question)
    if time_match:
        minutes = int(time_match.group(2))
        if minutes % 15 == 0:
            result["duration_minutes"] = 15
        elif minutes % 5 == 0:
            result["duration_minutes"] = 5
        else:
            result["duration_minutes"] = 5  # Default to 5-min
    else:
        result["duration_minutes"] = 5

    return result


async def polymarket_scanner(state: SharedState):
    """
    Polls the Polymarket Gamma API to discover active short-term
    crypto directional markets (5-min and 15-min UP/DOWN contracts).
    
    Gamma API endpoint: GET https://gamma-api.polymarket.com/markets
    
    We filter for:
    1. Active, non-closed markets
    2. Markets whose questions mention our tracked crypto assets
    3. Short-term directional contracts (UP/DOWN/above/below)
    """
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    try:
                        # Search for crypto-related markets
                        for asset in TRACKED_ASSETS:
                            params = {
                                "active": "true",
                                "closed": "false",
                                "limit": 50,
                            }

                            url = f"{GAMMA_API_URL}/markets"
                            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                                if resp.status != 200:
                                    logger.warning(f"[PM-SCANNER] Gamma API returned {resp.status}")
                                    continue

                                markets = await resp.json()
                                if not isinstance(markets, list):
                                    continue

                                for mkt in markets:
                                    question = mkt.get("question", "")
                                    q_lower = question.lower()

                                    # Check if this market is about one of our tracked assets
                                    matched_asset = None
                                    for a in TRACKED_ASSETS:
                                        if (a.polymarket_slug_pattern in q_lower or
                                                a.symbol.lower() in q_lower or
                                                a.name.lower() in q_lower):
                                            matched_asset = a
                                            break

                                    if not matched_asset:
                                        continue

                                    # Check if it's a directional market
                                    parsed = _parse_pm_question(question, matched_asset.symbol)
                                    if not parsed:
                                        continue

                                    market_id = mkt.get("id", "")
                                    condition_id = mkt.get("conditionId", mkt.get("condition_id", ""))

                                    # Extract token IDs
                                    clob_token_ids = mkt.get("clobTokenIds", [])
                                    token_id_yes = clob_token_ids[0] if len(clob_token_ids) > 0 else ""
                                    token_id_no = clob_token_ids[1] if len(clob_token_ids) > 1 else ""

                                    # Get expiry timestamp
                                    end_date = mkt.get("endDate", mkt.get("end_date_iso", ""))
                                    expiry_ts = 0.0
                                    if end_date:
                                        try:
                                            from datetime import datetime, timezone
                                            # Handle ISO format dates
                                            if "T" in str(end_date):
                                                dt = datetime.fromisoformat(
                                                    str(end_date).replace("Z", "+00:00")
                                                )
                                                expiry_ts = dt.timestamp()
                                            else:
                                                expiry_ts = float(end_date)
                                        except (ValueError, TypeError):
                                            pass

                                    # Only add if contract hasn't expired and has token IDs
                                    if expiry_ts > time.time() and token_id_yes:
                                        contract = PolymarketContract(
                                            market_id=market_id,
                                            condition_id=condition_id,
                                            token_id_yes=token_id_yes,
                                            token_id_no=token_id_no,
                                            question=question,
                                            asset_symbol=matched_asset.symbol,
                                            direction=parsed["direction"],
                                            strike_price=parsed["strike_price"],
                                            expiry_ts=expiry_ts,
                                            duration_minutes=parsed["duration_minutes"],
                                            slug=mkt.get("slug", ""),
                                            active=True,
                                        )
                                        state.active_contracts[market_id] = contract

                        # Prune expired contracts
                        now = time.time()
                        expired_keys = [
                            k for k, v in state.active_contracts.items()
                            if v.expiry_ts <= now
                        ]
                        for k in expired_keys:
                            state.active_contracts[k].active = False

                        state.last_market_scan = now

                        n_active = sum(1 for v in state.active_contracts.values() if v.active)
                        if n_active > 0:
                            logger.info(f"[PM-SCANNER] Found {n_active} active crypto directional contracts")
                        else:
                            logger.info("[PM-SCANNER] No active crypto contracts found — will keep scanning")

                    except aiohttp.ClientError as e:
                        logger.warning(f"[PM-SCANNER] HTTP error: {e}")
                    except Exception as e:
                        logger.error(f"[PM-SCANNER] Error during scan: {e}")

                    await asyncio.sleep(MARKET_SCAN_INTERVAL)

        except asyncio.CancelledError:
            logger.info("[PM-SCANNER] Scanner cancelled — shutting down")
            return
        except Exception as e:
            logger.error(f"[PM-SCANNER] Session error: {e}. Restarting in 10s...")
            await asyncio.sleep(10)


# ─────────────────────────────────────────────────────────────────────
# POLYMARKET CLOB ORDERBOOK FEED
# ─────────────────────────────────────────────────────────────────────

async def polymarket_book_feed(state: SharedState):
    """
    Fetches orderbook data for active Polymarket contracts.
    
    Uses REST polling to GET /book?token_id=<id> from the CLOB API.
    This gives us the full orderbook (bids + asks) to:
    1. Know the current market price of YES/NO tokens
    2. Calculate realistic slippage for paper trades
    3. Detect stale books (for latency arbitrage)
    
    We also fetch midpoint prices: GET /midpoint?token_id=<id>
    """
    delay = WS_RECONNECT_MIN_DELAY

    while True:
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    active_contracts = [
                        c for c in state.active_contracts.values()
                        if c.active and c.token_id_yes
                    ]

                    for contract in active_contracts:
                        try:
                            # Fetch orderbook for YES token
                            book_url = f"{CLOB_API_URL}/book"
                            async with session.get(
                                book_url,
                                params={"token_id": contract.token_id_yes},
                                timeout=aiohttp.ClientTimeout(total=10)
                            ) as resp:
                                if resp.status == 200:
                                    book_data = await resp.json()
                                    bids = book_data.get("bids", [])
                                    asks = book_data.get("asks", [])

                                    snapshot = OrderBookSnapshot(
                                        token_id=contract.token_id_yes,
                                        bids=bids,
                                        asks=asks,
                                        best_bid=float(bids[0]["price"]) if bids else 0.0,
                                        best_ask=float(asks[0]["price"]) if asks else 1.0,
                                        midpoint=(
                                            (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
                                            if bids and asks else 0.5
                                        ),
                                        last_update_ts=time.time(),
                                    )
                                    contract.orderbook = snapshot

                        except (aiohttp.ClientError, KeyError, IndexError, ValueError) as e:
                            logger.debug(f"[PM-BOOK] Error fetching book for {contract.asset_symbol}: {e}")
                            continue

                        # Small delay between requests to avoid rate limiting
                        await asyncio.sleep(0.2)

                    # If no active contracts, just wait
                    if not active_contracts:
                        await asyncio.sleep(5)
                    else:
                        await asyncio.sleep(1)  # Refresh books every ~1s

        except asyncio.CancelledError:
            logger.info("[PM-BOOK] Book feed cancelled — shutting down")
            return
        except Exception as e:
            logger.error(f"[PM-BOOK] Error: {e}. Restarting in {delay:.0f}s...")
            await asyncio.sleep(delay)
            delay = min(delay * WS_RECONNECT_MULTIPLIER, WS_RECONNECT_MAX_DELAY)
