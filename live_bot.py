# ─────────────────────────────────────────────
# BTC/ETH/SOL/XRP Prediction Market — 24/7 Live Trading Bot
# Signal: RSI(14) with YES/NO bid entry filter
#
# Default Signal Generation:
#   - Buy YES when RSI < RSI Oversold (RSI < 35)
#   - Buy NO when RSI > RSI Overbought (RSI > 65)
#   - Entry zone: 0.30 to 0.70 for both sides
# ─────────────────────────────────────────────

import os
import time
import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import requests
import duckdb
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# --- CONFIGURATION ---
load_dotenv()

KEY_ID              = os.getenv("KALSHI_KEY_ID", "")
PRIVATE_KEY_PATH    = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
KALSHI_ENV          = os.getenv("KALSHI_ENV", "prod")
DB_PATH             = os.getenv("DB_PATH", "market_data.db")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

API_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
API_BASE = API_URLS.get(KALSHI_ENV, API_URLS["prod"])

# Trading Parameters
TARGET_ASSETS       = {"BTC", "ETH", "XRP", "SOL"}
TRACKED_SERIES      = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]
POLL_INTERVAL_SEC   = 1
CONTRACTS_PER_TRADE = 100
DRY_RUN             = True  # Paper trade by default

# RSI Strategy Parameters
RSI_PERIOD          = 14     # Wilder's RSI lookback (candles)
RSI_OVERSOLD        = 35     # buy YES when RSI < RSI_OVERSOLD
RSI_OVERBOUGHT      = 65     # buy NO  when RSI > RSI_OVERBOUGHT
ENTRY_BID_MIN       = 0.30   # only enter when bid is in [0.30, 0.70]
ENTRY_BID_MAX       = 0.70
BINANCE_RSI_INTERVAL    = "1m"  # candle size for Binance spot RSI
BINANCE_RSI_REFRESH_SEC = 60    # re-fetch Binance klines once per minute
BINANCE_TICKER_REFRESH_SEC = 1  # refresh spot bid/ask every poll

# Time Guardrails (per 15-min chain)
MIN_PCT_ELAPSED   = 0.20  # skip first 20% of chain life
MIN_SECS_TO_CLOSE = 60    # skip if < 60s to expiry

# Status print interval
STATUS_INTERVAL_SEC = 30
BINANCE_REST_BASE   = "https://api.binance.us/api/v3"  # public, no auth needed
KALSHI_TZ           = ZoneInfo("America/New_York")


def _parse_close_dt_from_ticker(ticker: str) -> "datetime | None":
    """Derive close time from ticker string (e.g. KXBTC15M-26MAR172115-15).
    Kalshi close_time API field is labeled UTC but is actually Eastern Time,
    so we parse the ticker directly instead.
    """
    try:
        date_str = ticker.split("-")[1]          # e.g. "26MAR172115"
        dt_naive = datetime.strptime(date_str, "%y%b%d%H%M")
        expiry_et = dt_naive.replace(tzinfo=KALSHI_TZ)
        return expiry_et.astimezone(timezone.utc)
    except Exception:
        return None


# --- BINANCE SPOT PRICE ---
def fetch_binance_ticker(asset: str) -> dict | None:
    """Fetch best bid/ask from Binance.US public REST API (no auth)."""
    try:
        resp = requests.get(
            f"{BINANCE_REST_BASE}/ticker/bookTicker",
            params={"symbol": f"{asset}USDT"},
            timeout=3,
        )
        resp.raise_for_status()
        d = resp.json()
        return {"bid": float(d["bidPrice"]), "ask": float(d["askPrice"])}
    except Exception:
        return None


def fetch_binance_rsi(asset: str) -> float | None:
    """Fetch recent 1-min klines from Binance and compute RSI on close prices."""
    try:
        resp = requests.get(
            f"{BINANCE_REST_BASE}/klines",
            params={"symbol": f"{asset}USDT", "interval": BINANCE_RSI_INTERVAL,
                    "limit": RSI_PERIOD + 2},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        # Ensure candles are in ascending time order (defensive against API ordering)
        data = sorted(data, key=lambda k: k[0])
        closes = [float(k[4]) for k in data]  # index 4 = close price
        rsi = compute_rsi(closes, RSI_PERIOD)
        # Log a hint when RSI is pinned at an extreme (often monotonic data)
        if rsi in (0.0, 100.0):
            deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
            if deltas and (all(d <= 0 for d in deltas) or all(d >= 0 for d in deltas)):
                logging.debug(
                    f"RSI pinned for {asset}: monotonic closes over last {len(closes)-1} intervals"
                )
        return rsi
    except Exception:
        return None


# --- RSI HELPER ---
def compute_rsi(prices: list, period: int = 14) -> float | None:
    """Wilder's smoothed RSI. Returns None if insufficient data."""
    if len(prices) < period + 1:
        return None
    deltas = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


# --- AUTH HELPERS ---
def load_private_key(path: str):
    if not path or not os.path.exists(path):
        logging.error(f"Private key not found: {path}")
        return None
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def make_headers(private_key, method: str, path_with_params: str) -> dict:
    path_only    = path_with_params.split("?")[0]
    signing_path = (f"/trade-api/v2{path_only}"
                    if not path_only.startswith("/trade-api/v2") else path_only)
    ts      = str(int(time.time() * 1000))
    message = (ts + method + signing_path).encode("utf-8")
    sig     = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY":       KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type":            "application/json",
    }


# --- DISCORD ALERTS ---
def send_discord_alert(message: str, color: int = 0x2196F3):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"embeds": [{"description": message, "color": color,
                            "timestamp": datetime.now(timezone.utc).isoformat()}]}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        logging.error(f"Discord alert failed: {e}")


# --- KALSHI API CLIENT ---
class KalshiClient:
    def __init__(self, key_id, private_key):
        self.key_id      = key_id
        self.private_key = private_key

    def get_active_markets(self, series_ticker: str):
        path    = f"/markets?series_ticker={series_ticker}&status=open&limit=100"
        headers = make_headers(self.private_key, "GET", path)
        try:
            resp = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)
            resp.raise_for_status()
            return resp.json().get("markets", [])
        except Exception as e:
            logging.error(f"Market discovery failed for {series_ticker}: {e}")
            return []

    def get_market(self, ticker: str):
        path    = f"/markets/{ticker}"
        headers = make_headers(self.private_key, "GET", path)
        try:
            resp = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)
            resp.raise_for_status()
            return resp.json().get("market", {})
        except Exception as e:
            logging.error(f"Market fetch failed for {ticker}: {e}")
            return None

    def fetch_orderbook(self, ticker: str):
        path    = f"/markets/{ticker}/orderbook"
        headers = make_headers(self.private_key, "GET", path)
        try:
            resp = requests.get(f"{API_BASE}{path}", headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            return data.get("orderbook_fp") or data.get("orderbook") or {}
        except Exception:
            return None

    def place_order(self, ticker: str, side: str, count: int,
                    price_cents: int, client_order_id: str):
        path    = "/portfolio/orders"
        headers = make_headers(self.private_key, "POST", path)
        payload = {
            "ticker":          ticker,
            "side":            side,
            "action":          "buy",
            "client_order_id": client_order_id,
            "count":           count,
            "time_in_force":   "fill_or_kill",
        }
        if side == "yes":
            payload["yes_price"] = price_cents
        else:
            payload["no_price"]  = price_cents
        try:
            resp = requests.post(f"{API_BASE}{path}", headers=headers,
                                 json=payload, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logging.error(f"Order failed: {e.response.text}")
            return None
        except Exception as e:
            logging.error(f"Order request error: {e}")
            return None


# --- DATABASE ---
def init_db(db_path: str):
    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id                   BIGINT PRIMARY KEY,
            fetched_at           TIMESTAMPTZ NOT NULL,
            market_ticker        VARCHAR     NOT NULL,
            asset                VARCHAR     NOT NULL,
            direction            VARCHAR     NOT NULL,
            yes_best_bid_dollars DOUBLE,
            yes_best_bid_qty     DOUBLE,
            no_best_bid_dollars  DOUBLE,
            no_best_bid_qty      DOUBLE,
            mid_dollars          DOUBLE,
            spread_dollars       DOUBLE,
            yes_bids_json        JSON,
            no_bids_json         JSON
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_orderbook_asset_time
        ON orderbook_snapshots (asset, fetched_at)
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS binance_snapshots (
            id          BIGINT PRIMARY KEY,
            fetched_at  TIMESTAMPTZ NOT NULL,
            asset       VARCHAR     NOT NULL,
            bid         DOUBLE,
            ask         DOUBLE,
            mid         DOUBLE,
            rsi         DOUBLE,
            interval    VARCHAR,
            source      VARCHAR
        )
    """)
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_binance_asset_time
        ON binance_snapshots (asset, fetched_at)
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS live_trades (
            id                BIGINT PRIMARY KEY,
            traded_at         TIMESTAMPTZ NOT NULL,
            market_ticker     VARCHAR     NOT NULL,
            side              VARCHAR     NOT NULL,
            contracts         INTEGER     NOT NULL,
            limit_price_cents INTEGER     NOT NULL,
            is_dry_run        BOOLEAN     NOT NULL,
            status            VARCHAR     NOT NULL,
            mid_at_entry      DOUBLE      NOT NULL,
            rsi_at_entry      DOUBLE      NOT NULL,
            client_order_id   VARCHAR     NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trade_resolutions (
            id              BIGINT PRIMARY KEY,
            resolved_at     TIMESTAMPTZ NOT NULL,
            market_ticker   VARCHAR     NOT NULL,
            side            VARCHAR     NOT NULL,
            price_cents     INTEGER     NOT NULL,
            contracts       INTEGER     NOT NULL,
            result          VARCHAR     NOT NULL,
            outcome         VARCHAR     NOT NULL,
            profit_cents    INTEGER     NOT NULL,
            is_dry_run      BOOLEAN     NOT NULL
        )
    """)
    return con


def _infer_resolution_from_snapshots(db_con, ticker: str, close_dt: datetime | None):
    """Infer resolution from orderbook snapshots when a side pins at 99? near expiry."""
    try:
        if close_dt is not None:
            since = close_dt - timedelta(minutes=3)
            df = db_con.execute(
                """
                SELECT fetched_at, yes_best_bid_dollars, no_best_bid_dollars
                FROM orderbook_snapshots
                WHERE market_ticker = ? AND fetched_at >= ?
                ORDER BY fetched_at DESC
                LIMIT 50
                """,
                [ticker, since],
            ).df()
        else:
            df = db_con.execute(
                """
                SELECT fetched_at, yes_best_bid_dollars, no_best_bid_dollars
                FROM orderbook_snapshots
                WHERE market_ticker = ?
                ORDER BY fetched_at DESC
                LIMIT 50
                """,
                [ticker],
            ).df()
        if df.empty:
            return None
        pinned = df[
            (df["yes_best_bid_dollars"] >= 0.99)
            | (df["no_best_bid_dollars"] >= 0.99)
        ]
        if pinned.empty:
            return None
        row = pinned.iloc[0]
        yes_bid = row["yes_best_bid_dollars"]
        no_bid = row["no_best_bid_dollars"]
        result = "yes" if yes_bid >= 0.99 and (no_bid is None or yes_bid >= no_bid) else "no"
        return result, row["fetched_at"]
    except Exception as e:
        logging.debug(f"Resolution inference failed for {ticker}: {e}")
        return None


# --- MARKET STATE ---
class MarketState:
    def __init__(self, ticker: str, close_time_utc: str = ""):
        self.ticker         = ticker
        self.history        = []   # (timestamp, mid, yes_bid, no_bid)
        self.traded         = False
        self.attempts       = 0
        self.n_polls        = 0    # orderbook fetches attempted
        self.n_updates      = 0    # fetches that had at least one-sided liquidity
        # Parse close time from ticker (API close_time is mislabeled as UTC but is ET)
        self.close_dt = _parse_close_dt_from_ticker(ticker)

    def update(self, orderbook: dict):
        self.n_polls += 1
        if not orderbook:
            return
        # New API: yes_dollars / no_dollars are [[price_dollars, qty], ...] lists
        # Old API: yes / no are [[price_cents, qty], ...] lists
        if "yes_dollars" in orderbook or "no_dollars" in orderbook:
            yes_raw = orderbook.get("yes_dollars", [])
            no_raw  = orderbook.get("no_dollars",  [])
            y_best  = max(yes_raw, key=lambda x: float(x[0])) if yes_raw else None
            n_best  = max(no_raw,  key=lambda x: float(x[0])) if no_raw  else None
            yes_bid = float(y_best[0]) if y_best else None
            no_bid  = float(n_best[0]) if n_best else None
        else:
            yes_bids = orderbook.get("yes", [])
            no_bids  = orderbook.get("no",  [])
            y_best   = max(yes_bids, key=lambda x: x[0]) if yes_bids else None
            n_best   = max(no_bids,  key=lambda x: x[0]) if no_bids  else None
            yes_bid  = float(y_best[0]) / 100 if y_best else None
            no_bid   = float(n_best[0]) / 100 if n_best else None

        if yes_bid is not None and no_bid is not None:
            if yes_bid + no_bid > 1.0:
                return  # crossed book
            mid = (yes_bid + (1 - no_bid)) / 2
        elif yes_bid is not None:
            mid = yes_bid
        elif no_bid is not None:
            mid = 1 - no_bid
        else:
            return  # both sides empty

        self.n_updates += 1
        self.history.append((time.time(), mid, yes_bid, no_bid))

# --- LIVE ENGINE ---
def run_live(client: KalshiClient, db_con, dry_run: bool):
    active_markets      = {}
    traded_chains       = set()
    pending_resolutions = {}
    snapshot_id         = int(time.time() * 1e6)
    binance_snapshot_id = int(time.time() * 1e6) + 1
    trade_id            = int(time.time() * 1e6)
    last_status         = 0.0
    binance_rsi_cache   = {}   # asset -> {"rsi": float | None, "last_fetch": float}
    binance_ticker_cache = {}  # asset -> {"bid": float|None, "ask": float|None, "last_fetch": float}

    # ── Restore any unresolved trades from previous sessions ──────────────────
    try:
        unresolved = db_con.execute("""
            SELECT t.market_ticker, t.side, t.limit_price_cents,
                   t.contracts, t.is_dry_run
            FROM live_trades t
            WHERE t.status IN ('success', 'paper_trade')
              AND t.market_ticker NOT IN (
                  SELECT DISTINCT market_ticker FROM trade_resolutions
              )
        """).fetchall()
        for ticker, side, price_cents, contracts, is_dry_run in unresolved:
            pending_resolutions[ticker] = {
                "side":        side,
                "price_cents": price_cents,
                "contracts":   contracts,
                "dry_run":     bool(is_dry_run),
                "close_dt":    _parse_close_dt_from_ticker(ticker),
                "last_check":  0,
            }
            logging.info(f"Restored pending resolution for {ticker} ({side} @ {price_cents}¢)")
    except Exception as e:
        logging.warning(f"Could not restore pending resolutions: {e}")

    logging.info(f"Live Bot started | Assets: {TARGET_ASSETS} | Dry run: {dry_run}")

    while True:
        try:
            now_dt = datetime.now(timezone.utc)

            # 1. Market Discovery
            current_tickers = set()
            for series in TRACKED_SERIES:
                for m in client.get_active_markets(series):
                    ticker = m.get("ticker")
                    if not ticker:
                        continue
                    asset = ticker.split("15M")[0].replace("KX", "")
                    if asset not in TARGET_ASSETS:
                        continue
                    current_tickers.add(ticker)
                    if ticker not in active_markets:
                        active_markets[ticker] = MarketState(ticker, m.get("close_time", ""))
                        logging.info(f"Tracking: {ticker}")

            # Remove expired / closed markets
            to_remove = [
                t for t, s in active_markets.items()
                if t not in current_tickers
                or (s.close_dt and (s.close_dt - now_dt).total_seconds() < 0)
            ]
            for ticker in to_remove:
                del active_markets[ticker]
                logging.info(f"Removed: {ticker}")

            # 2a. Refresh Binance spot RSI (once per minute per asset)
            active_assets = {t.split("15M")[0].replace("KX", "") for t in active_markets}
            for asset in active_assets:
                cache = binance_rsi_cache.get(asset, {})
                if time.time() - cache.get("last_fetch", 0) >= BINANCE_RSI_REFRESH_SEC:
                    rsi_val = fetch_binance_rsi(asset)
                    # Preserve last known RSI if the refresh fails (no leakage: only forward-fill)
                    if rsi_val is None and "rsi" in cache:
                        rsi_val = cache.get("rsi")
                    binance_rsi_cache[asset] = {"rsi": rsi_val, "last_fetch": time.time()}
                    logging.info(f"Binance RSI {asset} ({BINANCE_RSI_INTERVAL}): "
                                 + (f"{rsi_val:.1f}" if rsi_val is not None else "n/a"))

                # Refresh spot bid/ask each poll (or as often as allowed)
                tcache = binance_ticker_cache.get(asset, {})
                if time.time() - tcache.get("last_fetch", 0) >= BINANCE_TICKER_REFRESH_SEC:
                    bnc = fetch_binance_ticker(asset)
                    if bnc:
                        tcache = {"bid": bnc["bid"], "ask": bnc["ask"], "last_fetch": time.time()}
                        binance_ticker_cache[asset] = tcache
                    else:
                        # keep last known bid/ask if API hiccups
                        tcache = {
                            "bid": tcache.get("bid"),
                            "ask": tcache.get("ask"),
                            "last_fetch": time.time(),
                        }
                        binance_ticker_cache[asset] = tcache

                # Persist Binance snapshot (bid/ask + RSI) every poll
                bid = tcache.get("bid")
                ask = tcache.get("ask")
                mid = (bid + ask) / 2 if (bid is not None and ask is not None) else None
                rsi_val = binance_rsi_cache.get(asset, {}).get("rsi")
                binance_snapshot_id += 1
                try:
                    db_con.execute(
                        "INSERT INTO binance_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                        [binance_snapshot_id, now_dt, asset, bid, ask, mid,
                         rsi_val, BINANCE_RSI_INTERVAL, "binance.us"],
                    )
                except Exception as e:
                    logging.error(f"DB binance snapshot error: {e}")

            # 2b. Poll (always) + Signal (only if not already traded)
            for ticker, state in active_markets.items():
                book = client.fetch_orderbook(ticker)
                if not book:
                    continue

                state.update(book)

                # Log orderbook snapshot (dual-format: new API vs old API)
                if "yes_dollars" in book or "no_dollars" in book:
                    yes_raw  = book.get("yes_dollars", [])
                    no_raw   = book.get("no_dollars",  [])
                    y_best   = max(yes_raw, key=lambda x: float(x[0])) if yes_raw else None
                    n_best   = max(no_raw,  key=lambda x: float(x[0])) if no_raw  else None
                    yes_bid  = float(y_best[0]) if y_best else None
                    no_bid   = float(n_best[0]) if n_best else None
                    y_qty    = float(y_best[1]) if y_best else None
                    n_qty    = float(n_best[1]) if n_best else None
                    yes_bids = [[int(round(float(p)*100)), float(q)] for p, q in yes_raw]
                    no_bids  = [[int(round(float(p)*100)), float(q)] for p, q in no_raw]
                else:
                    yes_bids  = book.get("yes", [])
                    no_bids   = book.get("no",  [])
                    y_best    = max(yes_bids, key=lambda x: x[0]) if yes_bids else None
                    n_best    = max(no_bids,  key=lambda x: x[0]) if no_bids  else None
                    yes_bid   = float(y_best[0]) / 100 if y_best else None
                    no_bid    = float(n_best[0]) / 100 if n_best else None
                    y_qty     = float(y_best[1]) if y_best else None
                    n_qty     = float(n_best[1]) if n_best else None
                mid       = state.history[-1][1] if state.history else None
                spread    = (1.0 - yes_bid - no_bid) if (yes_bid and no_bid) else None
                asset     = ticker.split("15M")[0].replace("KX", "")
                direction = ("UP"   if ticker.endswith("-00") else
                             "DOWN" if ticker.endswith("-01") else
                             "STRIKE-" + ticker.split("-")[-1])
                rsi       = binance_rsi_cache.get(asset, {}).get("rsi")

                snapshot_id += 1
                try:
                    db_con.execute(
                        "INSERT INTO orderbook_snapshots "
                        "(id, fetched_at, market_ticker, asset, direction, "
                        "yes_best_bid_dollars, yes_best_bid_qty, "
                        "no_best_bid_dollars, no_best_bid_qty, "
                        "mid_dollars, spread_dollars, "
                        "yes_bids_json, no_bids_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [snapshot_id, now_dt, ticker, asset, direction,
                         yes_bid, y_qty, no_bid, n_qty,
                         mid, spread,
                         json.dumps(yes_bids), json.dumps(no_bids)],
                    )
                except Exception as e:
                    logging.error(f"DB snapshot error: {e}")

                # If already traded for this chain, skip signal logic but keep collecting data
                if state.traded or ticker in traded_chains:
                    continue

                # Time guardrails — data is always collected above; only signals are gated
                if state.close_dt:
                    secs_left   = (state.close_dt - now_dt).total_seconds()
                    pct_elapsed = (15 * 60 - secs_left) / (15 * 60)
                    if pct_elapsed < MIN_PCT_ELAPSED:
                        logging.debug(f"[SKIP] {ticker} too early ({pct_elapsed:.0%} elapsed)")
                        continue
                    if secs_left < MIN_SECS_TO_CLOSE:
                        logging.debug(f"[SKIP] {ticker} too close to expiry ({secs_left:.0f}s left)")
                        continue

                # RSI Signal Check
                if rsi is None or mid is None:
                    continue

                signal              = 0
                side                = None
                entry_price_dollars = None

                # YES: RSI oversold + YES bid in entry zone
                if (rsi < RSI_OVERSOLD
                        and yes_bid is not None
                        and ENTRY_BID_MIN <= yes_bid <= ENTRY_BID_MAX):
                    yes_ask = (1 - no_bid) if no_bid is not None else None
                    if yes_ask and 0 < yes_ask < 1:
                        signal, side, entry_price_dollars = 1, "yes", yes_ask

                # NO: RSI overbought + NO bid in entry zone
                elif (rsi > RSI_OVERBOUGHT
                        and no_bid is not None
                        and ENTRY_BID_MIN <= no_bid <= ENTRY_BID_MAX):
                    no_ask = (1 - yes_bid) if yes_bid is not None else None
                    if no_ask and 0 < no_ask < 1:
                        signal, side, entry_price_dollars = -1, "no", no_ask

                if signal == 0 or entry_price_dollars is None:
                    continue

                base_cents  = int(round(entry_price_dollars * 100))
                price_cents = min(99, max(1, base_cents + 1))  # +1¢ slippage buffer
                client_oid  = f"{ticker}-{int(time.time())}"

                logging.info(
                    f"SIGNAL: {ticker} | {side.upper()} | RSI={rsi:.1f}"
                    f" | Mid={mid:.3f} | Limit={price_cents}¢"
                )
                alert_msg = (
                    f"**SIGNAL**\nMarket: `{ticker}`\nSide: **{side.upper()}**"
                    f"\nContracts: {CONTRACTS_PER_TRADE}\nLimit: {price_cents}¢"
                    f"\nRSI: {rsi:.1f} | Mid: {mid:.3f}"
                )

                if not dry_run:
                    state.attempts += 1
                    order_resp = client.place_order(
                        ticker, side, CONTRACTS_PER_TRADE, price_cents, client_oid
                    )
                    if order_resp:
                        status = "success"
                        state.traded = True
                        traded_chains.add(ticker)
                        send_discord_alert(alert_msg + "\n\n✅ order placed", color=0x4CAF50)
                    else:
                        status = "failed"
                        send_discord_alert(alert_msg + "\n\n❌ order failed", color=0xF44336)
                        if state.attempts >= 3:
                            state.traded = True
                            traded_chains.add(ticker)
                else:
                    status = "paper_trade"
                    state.traded = True
                    traded_chains.add(ticker)
                    send_discord_alert(alert_msg + "\n\n📝 paper trade", color=0xFF9800)

                trade_id += 1
                try:
                    db_con.execute(
                        "INSERT INTO live_trades VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        [trade_id, now_dt, ticker, side, CONTRACTS_PER_TRADE,
                         price_cents, dry_run, status, mid, rsi, client_oid],
                    )
                    if status in ("success", "paper_trade"):
                        pending_resolutions[ticker] = {
                            "side":        side,
                            "price_cents": price_cents,
                            "contracts":   CONTRACTS_PER_TRADE,
                            "dry_run":     dry_run,
                            "close_dt":    state.close_dt,
                            "last_check":  0,
                        }
                except Exception as e:
                    logging.error(f"DB trade log error: {e}")

            # 3. Check Settlements (API or inferred from orderbook pin near close)
            resolved = []
            for ticker, info in pending_resolutions.items():
                close_dt = info["close_dt"]
                if close_dt and now_dt < close_dt:
                    continue
                if time.time() - info.get("last_check", 0) < 30:
                    continue
                info["last_check"] = time.time()
                try:
                    market = client.get_market(ticker)
                    result = ""
                    # Use result if present regardless of status (Kalshi may mark closed before settled)
                    if market and market.get("result"):
                        result = market.get("result", "") or ""
                    elif market and market.get("status") in ("determined", "settled", "resolved", "final"):
                        result = market.get("result", "") or ""
                    inferred = None
                    if not result:
                        # If API not ready, infer from orderbook snapshots near expiry
                        inferred = _infer_resolution_from_snapshots(db_con, ticker, close_dt)
                        if inferred:
                            result, resolved_at = inferred
                        else:
                            continue
                    else:
                        resolved_at = now_dt
                    if result == info["side"]:
                        outcome = "WIN"
                        profit  = (100 - info["price_cents"]) * info["contracts"]
                        color   = 0x4CAF50
                    else:
                        outcome = "LOSS"
                        profit  = -info["price_cents"] * info["contracts"]
                        color   = 0xF44336
                    mode = "[PAPER]" if info["dry_run"] else "[LIVE]"
                    pl   = profit / 100
                    src = "INFERRED" if inferred else "API"
                    send_discord_alert(
                        f"**{outcome}** {mode} ({src})\n`{ticker}`\n"
                        f"Entry: {info['price_cents']}¢\nResult: {result.upper()}\n"
                        f"P/L: **${pl:+.2f}**",
                        color=color,
                    )
                    logging.info(f"Settled {ticker}: {result.upper()} | P/L: ${pl:+.2f}")
                    try:
                        res_id = int(time.time() * 1e6)
                        db_con.execute(
                            "INSERT INTO trade_resolutions VALUES (?,?,?,?,?,?,?,?,?,?)",
                            [res_id, resolved_at, ticker, info["side"],
                             info["price_cents"], info["contracts"],
                             result, outcome, profit, info["dry_run"]],
                        )
                    except Exception as db_err:
                        logging.error(f"DB resolution error: {db_err}")
                    resolved.append(ticker)
                except Exception as e:
                    logging.error(f"Settlement check error for {ticker}: {e}")

            for ticker in resolved:
                del pending_resolutions[ticker]

            # 4. Periodic status print
            if time.time() - last_status >= STATUS_INTERVAL_SEC:
                last_status = time.time()
                W = 72
                print("\n" + "=" * W)
                print(f"  STATUS  {now_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}  |  "
                      f"markets: {len(active_markets)}  |  pending: {len(pending_resolutions)}  |  "
                      f"mode: {'DRY RUN' if dry_run else 'LIVE'}")
                print("-" * W)
                print(f"  {'ASSET':<5} {'TICKER':<30} {'YES BID':>8} {'NO BID':>7} "
                      f"{'MID':>7} {'RSI':>7}  {'POLLS':>6} {'LIQ%':>5}  "
                      f"{'SPOT BID':>12} {'SPOT ASK':>12}")
                print("-" * W)

                seen_assets: set = set()
                for ticker, state in sorted(active_markets.items()):
                    asset   = ticker.split("15M")[0].replace("KX", "")
                    yes_bid = state.history[-1][2] if state.history else None
                    no_bid  = state.history[-1][3] if state.history else None
                    mid     = state.history[-1][1] if state.history else None
                    rsi     = binance_rsi_cache.get(asset, {}).get("rsi")

                    y_str   = f"{yes_bid:.3f}" if yes_bid is not None else "   ---"
                    n_str   = f"{no_bid:.3f}"  if no_bid  is not None else "  ---"
                    m_str   = f"{mid:.3f}"     if mid     is not None else "   ---"
                    rsi_str = f"{rsi:5.1f}"    if rsi     is not None else "   n/a"

                    # Warm-up / traded tag
                    if state.traded:
                        tag = "[done]"
                    elif not state.history:
                        tag = "(init)"
                    else:
                        tag = "      "

                    # Poll stats: polls attempted, liquidity hit rate
                    polls   = state.n_polls
                    liq_pct = f"{100*state.n_updates/polls:.0f}%" if polls > 0 else "  --"

                    # Binance spot — fetch once per asset per status cycle
                    bnc_str = "            ---           ---"
                    if asset not in seen_assets:
                        bnc = fetch_binance_ticker(asset)
                        if bnc:
                            bnc_str = f"  ${bnc['bid']:>10,.2f}  ${bnc['ask']:>10,.2f}"
                        seen_assets.add(asset)

                    print(f"  {asset:<5} {ticker:<30}  {y_str:>8} {n_str:>7}"
                          f"  {m_str:>7} {rsi_str} {tag}"
                          f"  {polls:>5}  {liq_pct:>4}  {bnc_str}")

                print("=" * W + "\n")

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            logging.info("Bot stopped.")
            break
        except Exception as e:
            logging.error(f"Main loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi RSI Live Bot")
    parser.add_argument("--live",      action="store_true", help="Execute real trades")
    parser.add_argument("--contracts", type=int, default=5,  help="Contracts per trade")
    args = parser.parse_args()

    CONTRACTS_PER_TRADE = args.contracts
    is_dry_run          = not args.live

    if not KEY_ID or not PRIVATE_KEY_PATH:
        logging.error("Missing KALSHI_KEY_ID or KALSHI_PRIVATE_KEY_PATH env vars.")
        exit(1)

    pk = load_private_key(PRIVATE_KEY_PATH)
    if not pk:
        exit(1)

    logging.info(f"Connecting to DuckDB: {DB_PATH}")
    db_con = init_db(DB_PATH)
    client = KalshiClient(KEY_ID, pk)
    run_live(client, db_con, is_dry_run)