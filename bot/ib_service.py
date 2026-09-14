from __future__ import annotations

import logging
import os
import threading
import time
import asyncio
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from ib_insync import IB, Stock, Order, MarketOrder, LimitOrder, StopOrder, Trade



logger = logging.getLogger(__name__)


class PacingLimitError(RuntimeError):
    """Raised when the historical-data pacing budget (IB's ~60 req/10min limit) is exhausted."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"Historical data pacing limit reached; retry after {retry_after:.1f}s")


class HistoricalDataError(RuntimeError):
    """Raised when IB returns no bars for a historical data request, carrying IB's own error if reported."""

    def __init__(self, message: str, ib_error_code: Optional[int] = None) -> None:
        self.ib_error_code = ib_error_code
        super().__init__(message)


class _PacingGate:
    """
    Sliding-window rate limiter shared by every caller of a given request type.

    Lives inside the service (not in callers) so the limit is enforced no matter
    who calls -- a script, a scheduled routine, a notebook -- since callers will
    forget to rate-limit themselves and the service shouldn't rely on them to.
    """

    def __init__(self, max_calls: int = 60, window_seconds: float = 600.0) -> None:
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def acquire(self, cost: int = 1) -> None:
        """
        Consume `cost` tokens from the budget. IB counts some request types
        as more than one message against its pacing limit -- e.g. BID_ASK
        historical requests count double -- so callers can charge for that
        instead of the gate silently under-counting them.
        """
        now = time.time()
        with self._lock:
            while self._calls and now - self._calls[0] > self.window_seconds:
                self._calls.popleft()
            if len(self._calls) + cost > self.max_calls:
                retry_after = self.window_seconds - (now - self._calls[0]) if self._calls else 0.0
                raise PacingLimitError(retry_after)
            for _ in range(cost):
                self._calls.append(now)


@dataclass
class IBConfig:
    """
    Configuration for connecting to TWS / IB Gateway.
    """

    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 0

    @classmethod
    def from_env(cls) -> "IBConfig":
        """
        Build configuration from environment variables, falling back to sane defaults.
        """
        host = os.getenv("TWS_HOST", cls.host)
        port = int(os.getenv("TWS_PORT", cls.port))
        client_id = int(os.getenv("CLIENT_ID", cls.client_id))
        return cls(host=host, port=port, client_id=client_id)


class IBService:
    """
    Thin service wrapper around a single IB instance.

    - Owns the IB connection and background event loop thread.
    - Exposes high-level methods used by the FastAPI layer.
    - Keeps reconnect signalling encapsulated behind a simple API.
    """

    def __init__(self, config: Optional[IBConfig] = None, app_settings: Optional[Dict[str, Any]] = None) -> None:
        self.config: IBConfig = config or IBConfig.from_env()
        self.app_settings: Dict[str, Any] = app_settings or {}
        self.max_qty = self.app_settings.get("max_qty", 1)
        self.max_notional = self.app_settings.get("max_notional", 100)
        self._ib: IB = IB()

        # Thread / lifecycle
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconnect_requested = threading.Event()

        # Simple status tracking for health endpoint
        self._status_lock = threading.Lock()
        self._status: str = "disconnected"
        self._last_error: Optional[str] = None

        # Reference to the background thread's asyncio event loop (set once
        # the connection loop starts). Needed so synchronous request/response
        # calls (e.g. reqHistoricalData) issued from other threads (FastAPI's
        # worker thread) can be scheduled onto the loop that actually owns
        # the IB socket connection, via asyncio.run_coroutine_threadsafe.
        self._loop: Optional[Any] = None

        # IB's documented historical-data pacing limit is ~60 requests per 10
        # minutes. Enforced here, once, so every caller inherits it.
        self._historical_pacing = _PacingGate(max_calls=60, window_seconds=600.0)

        # Watchlist storage
        self._watchlist_file = str(Path(__file__).resolve().parent.parent / "watchlist.json")
        self._watchlist: List[str] = self._load_watchlist()

        # Hook up execution event listener
        self._ib.execDetailsEvent += self._on_exec_details

        # Real-time ticker streaming
        self._ticker_callbacks: Dict[str, List[Any]] = {}
        self._active_tickers: Dict[str, Any] = {}
        self._ib.pendingTickersEvent += self._on_pending_tickers

    def _on_exec_details(self, trade, fill):
        try:
            from bot.db import insert_trade
            action = "BUY" if fill.execution.side == "BOT" else "SELL" if fill.execution.side == "SLD" else fill.execution.side
            
            # fill.time is sometimes datetime.datetime, sometimes string
            time_str = fill.execution.time.isoformat() if hasattr(fill.execution.time, 'isoformat') else str(fill.execution.time)
            
            insert_trade(
                execId=fill.execution.execId,
                time=time_str,
                symbol=fill.contract.symbol,
                action=action,
                quantity=fill.execution.shares,
                price=fill.execution.price,
                orderType="MKT" # We don't always know it from execution, assuming MKT
            )
        except Exception as e:
            logger.error(f"Error handling exec details: {e}")

    def _load_watchlist(self) -> List[str]:
        import json
        if os.path.exists(self._watchlist_file):
            try:
                with open(self._watchlist_file, "r") as f:
                    return json.load(f)
            except Exception:
                return ["AAPL", "TSLA", "NVDA", "SPY"] # Default defaults
        return ["AAPL", "TSLA", "NVDA", "SPY"]

    def _save_watchlist(self) -> None:
        import json
        with open(self._watchlist_file, "w") as f:
            json.dump(self._watchlist, f)

    def get_watchlist(self) -> List[Dict[str, Any]]:
        """
        Get quotes for all symbols in the watchlist.
        """
        if not self._ib or not self._ib.isConnected():
            return [{"symbol": s, "error": "Not connected"} for s in self._watchlist]
        
        results = []
        for symbol in self._watchlist:
            try:
                # Re-use get_quote logic to fetch data
                quote = self.get_quote(symbol)
                results.append(quote)
            except Exception as e:
                results.append({"symbol": symbol, "error": str(e)})
        return results

    def add_to_watchlist(self, symbol: str) -> List[str]:
        symbol = symbol.upper()
        if symbol not in self._watchlist:
            self._watchlist.append(symbol)
            self._save_watchlist()
        return self._watchlist

    def remove_from_watchlist(self, symbol: str) -> List[str]:
        symbol = symbol.upper()
        if symbol in self._watchlist:
            self._watchlist.remove(symbol)
            self._save_watchlist()
        return self._watchlist


    @property
    def ib(self) -> IB:
        """
        Direct access to the underlying IB instance.
        Intended for internal use and well-contained helpers.
        """
        return self._ib

    # ------------------------------------------------------------------
    # Lifecycle / background loop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start the background IB loop thread if not already running.
        """
        if self._thread and self._thread.is_alive():
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """
        Signal the background thread to stop and disconnect from IB.
        """
        self._stop_event.set()
        try:
            if self._ib.isConnected():
                self._ib.disconnect()
        finally:
            if self._thread and self._thread.is_alive():
                # Give the thread a little time to exit gracefully.
                self._thread.join(timeout=5)

    def request_reconnect(self) -> None:
        """
        Public hook used by the API layer to request a reconnect.
        """
        self._reconnect_requested.set()

    def _set_status(self, status: str, error: Optional[str] = None) -> None:
        with self._status_lock:
            self._status = status
            self._last_error = error

    def _run_loop(self) -> None:
        """
        Core loop that owns the IB connection and event loop.
        Mirrors the old run_ib_loop behaviour but encapsulated here.
        """
        import asyncio  # Imported here to avoid leaking event loop concerns.

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        host = self.config.host
        port = self.config.port
        client_id = self.config.client_id

        print("Starting IBService thread...")

        while not self._stop_event.is_set():
            try:
                if not self._ib.isConnected():
                    print(f"Connecting to IBKR ({host}:{port}) with clientId={client_id}...")
                    # Allow order placement by setting readonly to False
                    self._ib.connect(host, port, clientId=client_id, readonly=False)
                    # Request delayed market data to avoid Error 10089 on paper accounts
                    self._ib.reqMarketDataType(3)
                    self._set_status("connected", None)
                    print("IB Connected (Market data type set to Delayed)!")
                    # Request executions to backfill local DB for the day
                    try:
                        self._ib.reqExecutions()
                    except Exception as e:
                        print(f"Failed to request executions on startup: {e}")

                # Blocks until disconnect or stop signal
                self._ib.run()

            except Exception as e:
                err_msg = f"IB Connection Error: {e}"
                print(err_msg)
                self._set_status("error", str(e))

            if self._stop_event.is_set():
                break

            print("Disconnected from IBKR. Waiting to retry or reconnect...")
            self._set_status("disconnected", None)

            # Wait 5 seconds OR until reconnect requested or stop requested
            start_wait = time.time()
            while time.time() - start_wait < 5:
                if self._stop_event.is_set():
                    break
                if self._reconnect_requested.is_set():
                    print("Reconnection requested manually!")
                    self._reconnect_requested.clear()
                    break
                time.sleep(0.5)

        print("IBService loop exiting, disconnecting IB if connected...")
        if self._ib.isConnected():
            self._ib.disconnect()
        self._set_status("disconnected", None)

    # ------------------------------------------------------------------
    # Helpers used by FastAPI endpoints
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """
        Return a health/status summary similar to the existing /health endpoint.
        """
        if not self._ib:
            return {"status": "disconnected", "operational": False}

        status = "disconnected"
        operational = False
        details: Dict[str, Any] = {}
        accounts: List[str] = []

        if self._ib.isConnected():
            status = "connected"  # Socket connected
            try:
                accounts = self._ib.managedAccounts()
                if not accounts:
                    status = "connected_no_accounts"
                else:
                    operational = True

                # Get server details if possible
                details["server_version"] = self._ib.client.serverVersion()
                details["host"] = self._ib.client.host
                details["port"] = self._ib.client.port
                details["client_id"] = self._ib.client.clientId

            except Exception as e:
                status = "error"
                details["error"] = str(e)

        # Overlay the internal status/error if we have something more precise.
        with self._status_lock:
            internal_status = self._status
            last_error = self._last_error

        if internal_status and internal_status != status:
            status = internal_status
        if last_error and "error" not in details:
            details["error"] = last_error

        return {
            "status": status,
            "operational": operational,
            "connection": details,
            "accounts": accounts,
        }

    def get_account_summary(self) -> Dict[str, Any]:
        """
        Fetch a thin account summary, mirroring the existing /account behaviour.
        """
        if not self._ib or not self._ib.isConnected():
            raise RuntimeError("Bot not connected to IBKR")

        summary: Dict[str, Any] = {}
        try:
            for tag in ["NetLiquidation", "TotalCashValue", "UnrealizedPnL", "RealizedPnL", "BuyingPower"]:
                vals = [v for v in self._ib.accountValues() if v.tag == tag and v.currency == "USD"]
                if vals:
                    summary[tag] = vals[0].value
        except Exception as e:
            # Preserve existing behaviour of returning an error field rather than raising.
            return {"error": str(e)}

        return summary

    def get_orders(self) -> List[Dict[str, Any]]:
        """
        Return a list of trades mirroring the existing /orders endpoint.
        """
        if not self._ib:
            return []

        trades = self._ib.trades()
        result: List[Dict[str, Any]] = []
        for t in trades:
            result.append(
                {
                    "orderId": t.order.orderId,
                    "ticker": t.contract.symbol,
                    "action": t.order.action,
                    "quantity": t.order.totalQuantity,
                    "status": t.orderStatus.status,
                    "orderType": t.order.orderType,
                    "lmtPrice": getattr(t.order, "lmtPrice", None),
                    "filled": t.orderStatus.filled,
                    "remaining": t.orderStatus.remaining,
                    "avgFillPrice": t.orderStatus.avgFillPrice,
                    "lastUpdateTime": t.log[-1].time.strftime("%H:%M:%S") if t.log else "",
                }
            )
        return result

    def cancel_order(self, order_id: int) -> Dict[str, Any]:
        """Cancel an open order by its orderId."""
        if not self._ib:
            raise RuntimeError("IB service not connected")
        
        trade = next((t for t in self._ib.trades() if getattr(t.order, "orderId", None) == order_id), None)
        if not trade:
            raise ValueError(f"Order with ID {order_id} not found or is no longer active.")
            
        self._ib.cancelOrder(trade.order)
        return {"status": "success", "message": f"Cancel request sent for order {order_id}"}

    def modify_order(self, order_id: int, quantity: float, lmt_price: Optional[float] = None) -> Dict[str, Any]:
        """Modify an open order's quantity and limit price."""
        if not self._ib:
            raise RuntimeError("IB service not connected")
            
        trade = next((t for t in self._ib.trades() if getattr(t.order, "orderId", None) == order_id), None)
        if not trade:
            raise ValueError(f"Order with ID {order_id} not found or is no longer active.")
            
        trade.order.totalQuantity = quantity
        if trade.order.orderType == "LMT" and lmt_price is not None:
            trade.order.lmtPrice = lmt_price
            
        self._ib.placeOrder(trade.contract, trade.order)
        return {"status": "success", "message": f"Modify request sent for order {order_id}"}

    def get_executions(self) -> List[Dict[str, Any]]:
        """
        Return lifetime executions from the local SQLite database.
        """
        try:
            from bot.db import get_all_trades
            return get_all_trades()
        except Exception as e:
            logger.error(f"Error fetching executions from DB: {e}")
            return []

    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Return a list of portfolio items mirroring the existing /positions endpoint
        but using portfolio() to get P&L and market data.
        """
        if not self._ib:
            return []

        # Use portfolio() instead of positions() to get P&L and market data
        portfolio_items = self._ib.portfolio()
        result: List[Dict[str, Any]] = []
        for p in portfolio_items:
            result.append(
                {
                    "ticker": p.contract.symbol,
                    "position": p.position,
                    "avgCost": self._sanitize_float(p.averageCost),
                    "marketPrice": self._sanitize_float(p.marketPrice),
                    "marketValue": self._sanitize_float(p.marketValue),
                    "unrealizedPNL": self._sanitize_float(p.unrealizedPNL),
                    "realizedPNL": self._sanitize_float(p.realizedPNL),
                    "account": p.account,
                }
            )
        return result

    @staticmethod
    def _sanitize_float(value: Any) -> Optional[float]:
        """
        Sanitize float values to ensure JSON compliance.
        Converts NaN and Infinity to None.
        """
        import math
        if value is None:
            return None
        try:
            if math.isnan(value) or math.isinf(value):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """
        Fetch a simple quote snapshot for the given symbol.

        This uses a per-request market data call and returns a small
        JSON-serializable dict suitable for the /quote endpoint.
        """
        symbol_upper = symbol.strip().upper() if symbol else ""
        logger.info("Quote request: symbol=%s", symbol_upper)

        if not symbol_upper:
            logger.warning("Quote failed: symbol is required")
            raise ValueError("Symbol is required")

        if not self._ib or not self._ib.isConnected():
            logger.warning("Quote failed: bot not connected to IBKR")
            raise RuntimeError("Bot not connected")

        if self._loop is None:
            raise RuntimeError("IB event loop not available")

        contract = Stock(symbol_upper, "SMART", "USD")

        try:
            async def _fetch_quote():
                # Request delayed market data (genericTickList="") for accounts without live data.
                ticker = self._ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)
                # Wait for data to populate on the IB event loop
                await asyncio.sleep(2)
                quote_dict = {
                    "symbol": symbol_upper,
                    "last": self._sanitize_float(ticker.last),
                    "bid": self._sanitize_float(ticker.bid),
                    "ask": self._sanitize_float(ticker.ask),
                    "close": self._sanitize_float(ticker.close),
                    "high": self._sanitize_float(ticker.high),
                    "low": self._sanitize_float(ticker.low),
                    "volume": self._sanitize_float(ticker.volume),
                    "time": getattr(ticker.time, "isoformat", lambda: None)() if getattr(ticker, "time", None) else None,
                }
                try:
                    self._ib.cancelMktData(contract)
                    
                    # FIX: ib-insync keeps Ticker objects in memory even after cancellation.
                    # We must manually scrub them to prevent memory leaks, especially when
                    # polled frequently by the autotrader.
                    if hasattr(self._ib, "wrapper") and getattr(self._ib.wrapper, "tickers", None) is not None:
                        req_ids = [k for k, v in self._ib.wrapper.reqId2Ticker.items() if v.contract == contract]
                        for rid in req_ids:
                            self._ib.wrapper.reqId2Ticker.pop(rid, None)
                        self._ib.wrapper.tickers.pop(id(contract), None)
                        
                except Exception as e:
                    logger.debug("Error cancelling mkt data: %s", e)
                return quote_dict

            if not self._loop.is_running():
                raise RuntimeError("IB event loop is not running")

            future = asyncio.run_coroutine_threadsafe(_fetch_quote(), self._loop)
            quote = future.result(timeout=10.0)

            # If no market data received, try to get price from portfolio positions as fallback
            if all(value is None for key, value in quote.items() if key not in {"symbol"}):
                logger.info("No market data from subscription, checking portfolio for %s", symbol_upper)
                portfolio_items = self._ib.portfolio()
                for item in portfolio_items:
                    if item.contract.symbol == symbol_upper:
                        quote["last"] = self._sanitize_float(item.marketPrice)
                        quote["close"] = self._sanitize_float(item.marketPrice)
                        logger.info("Quote from portfolio: symbol=%s last=%s", symbol_upper, quote.get("last"))
                        break

            # Final check if we got any data
            if all(value is None for key, value in quote.items() if key not in {"symbol"}):
                quote["error"] = "No market data received for symbol"
                logger.warning("Quote failed for %s: no market data received (check permissions or competing session)", symbol_upper)
            else:
                logger.info("Quote success: symbol=%s last=%s bid=%s ask=%s", symbol_upper, quote.get("last"), quote.get("bid"), quote.get("ask"))

            return quote
        except Exception as e:
            logger.exception("Quote failed for %s: %s", symbol_upper, e)
            raise

    def _format_quote(self, ticker) -> Dict[str, Any]:
        quote_dict = {
            "symbol": ticker.contract.symbol,
            "last": self._sanitize_float(ticker.last),
            "bid": self._sanitize_float(ticker.bid),
            "ask": self._sanitize_float(ticker.ask),
            "close": self._sanitize_float(ticker.close),
            "high": self._sanitize_float(ticker.high),
            "low": self._sanitize_float(ticker.low),
            "volume": self._sanitize_float(ticker.volume),
            "time": getattr(ticker.time, "isoformat", lambda: None)() if getattr(ticker, "time", None) else None,
        }
        if all(value is None for key, value in quote_dict.items() if key not in {"symbol", "time"}):
            portfolio_items = self._ib.portfolio()
            for item in portfolio_items:
                if item.contract.symbol == ticker.contract.symbol:
                    quote_dict["last"] = self._sanitize_float(item.marketPrice)
                    quote_dict["close"] = self._sanitize_float(item.marketPrice)
                    break
        return quote_dict

    def subscribe_ticker_updates(self, symbol: str, callback: Any) -> None:
        """
        Subscribe to live tick updates for a given symbol.
        The callback will be invoked with a quote dictionary whenever the ticker updates.
        """
        symbol_upper = symbol.strip().upper()
        if symbol_upper not in self._ticker_callbacks:
            self._ticker_callbacks[symbol_upper] = []
        self._ticker_callbacks[symbol_upper].append(callback)

        if symbol_upper not in self._active_tickers:
            import asyncio
            async def _sub():
                contract = Stock(symbol_upper, "SMART", "USD")
                ticker = self._ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)
                self._active_tickers[symbol_upper] = (contract, ticker)
                
                # Wait briefly for initial data, then push immediately so UI doesn't hang
                await asyncio.sleep(1)
                if symbol_upper in self._active_tickers:
                    quote_dict = self._format_quote(ticker)
                    for cb in self._ticker_callbacks.get(symbol_upper, []):
                        try:
                            cb(quote_dict)
                        except Exception:
                            pass
            
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(_sub(), self._loop)
        else:
            # Already active, send current state immediately to the new subscriber
            contract, ticker = self._active_tickers[symbol_upper]
            import asyncio
            async def _send_current():
                quote_dict = self._format_quote(ticker)
                try:
                    callback(quote_dict)
                except Exception:
                    pass
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(_send_current(), self._loop)

    def unsubscribe_ticker_updates(self, symbol: str, callback: Any) -> None:
        """
        Remove a callback. If no callbacks remain for the symbol, cancel the market data.
        """
        symbol_upper = symbol.strip().upper()
        if symbol_upper in self._ticker_callbacks:
            try:
                self._ticker_callbacks[symbol_upper].remove(callback)
            except ValueError:
                pass
            if not self._ticker_callbacks[symbol_upper]:
                del self._ticker_callbacks[symbol_upper]
                if symbol_upper in self._active_tickers:
                    contract, ticker = self._active_tickers.pop(symbol_upper)
                    import asyncio
                    async def _unsub():
                        self._ib.cancelMktData(contract)
                        if hasattr(self._ib, "wrapper") and getattr(self._ib.wrapper, "tickers", None) is not None:
                            req_ids = [k for k, v in self._ib.wrapper.reqId2Ticker.items() if v.contract == contract]
                            for rid in req_ids:
                                self._ib.wrapper.reqId2Ticker.pop(rid, None)
                            self._ib.wrapper.tickers.pop(id(contract), None)
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(_unsub(), self._loop)

    def _on_pending_tickers(self, tickers):
        for ticker in tickers:
            symbol = ticker.contract.symbol
            if symbol in self._ticker_callbacks:
                quote_dict = self._format_quote(ticker)
                for cb in self._ticker_callbacks[symbol]:
                    try:
                        cb(quote_dict)
                    except Exception as e:
                        logger.error(f"Error in ticker callback: {e}")

    def get_historical_data(
        self,
        symbol: str,
        duration: str = "1 M",
        bar_size: str = "1 day",
        what_to_show: str = "TRADES",
        use_rth: bool = True,
        end_datetime: str = "",
        primary_exchange: str = "",
    ) -> Dict[str, Any]:
        """
        Fetch historical market data for a symbol.

        Args:
            symbol: Stock symbol
            duration: How far back to fetch (e.g., "1 M", "1 Y", "5 D")
            bar_size: Bar size (e.g., "1 day", "1 hour", "5 mins")
            what_to_show: IB data type -- TRADES (split-adjusted only, no
                dividends), MIDPOINT, BID, ASK, BID_ASK, ADJUSTED_LAST
                (split AND dividend adjusted -- total return), etc.
            use_rth: Regular trading hours only (True) or include extended
                hours (False). Mainly matters for intraday bars.
            end_datetime: IB datetime string for the end of the window, or
                "" for "now". ADJUSTED_LAST is only available when this is
                blank -- IB does not compute dividend adjustments for a
                window ending in the past.
            primary_exchange: Disambiguates SMART-routed symbols that exist
                on multiple exchanges, so the wrong contract isn't silently
                resolved.

        Returns:
            {"bars": [...], "meta": {...}}. Raises HistoricalDataError
            (never returns an empty 200) if IB reports zero bars, so a bad
            request can't be mistaken for "no data for this symbol".
        """
        symbol_upper = symbol.strip().upper() if symbol else ""
        what_to_show_upper = (what_to_show or "TRADES").strip().upper()
        logger.info(
            "Historical data request: symbol=%s duration=%s bar_size=%s what_to_show=%s use_rth=%s end_datetime=%s",
            symbol_upper, duration, bar_size, what_to_show_upper, use_rth, end_datetime or "(now)",
        )

        if not symbol_upper:
            logger.warning("Historical data failed: symbol is required")
            raise ValueError("Symbol is required")

        valid_what_to_show = {
            "TRADES", "MIDPOINT", "BID", "ASK", "BID_ASK", "ADJUSTED_LAST",
            "HISTORICAL_VOLATILITY", "OPTION_IMPLIED_VOLATILITY",
            "REBATE_RATE", "FEE_RATE", "YIELD_BID", "YIELD_ASK",
            "YIELD_BID_ASK", "YIELD_LAST",
        }
        if what_to_show_upper not in valid_what_to_show:
            raise ValueError(
                f"Invalid what_to_show '{what_to_show}'. Must be one of: {sorted(valid_what_to_show)}"
            )

        if what_to_show_upper == "ADJUSTED_LAST" and end_datetime:
            # IB silently ignores/downgrades this combination rather than
            # erroring -- reject it here instead of letting "adjusted" data
            # quietly turn into unadjusted data.
            raise ValueError(
                "what_to_show='ADJUSTED_LAST' requires end_datetime to be blank "
                "(IB only computes split+dividend adjustments for a window ending now). "
                "Drop end_datetime, or use what_to_show='TRADES' for a historical window."
            )

        if not self._ib or not self._ib.isConnected():
            logger.warning("Historical data failed: bot not connected to IBKR")
            raise RuntimeError("Bot not connected")

        if self._loop is None:
            raise RuntimeError("IB event loop not available")

        # BID_ASK counts as two messages against IB's pacing budget.
        pacing_cost = 2 if what_to_show_upper == "BID_ASK" else 1
        self._historical_pacing.acquire(cost=pacing_cost)

        contract = Stock(symbol_upper, "SMART", "USD", primaryExchange=primary_exchange or "")

        import asyncio

        captured_error: Dict[str, Any] = {}

        def _on_error(reqId, errorCode, errorString, contract_, *_args):
            if errorCode and (2100 <= errorCode < 2200):
                return
            captured_error["errorCode"] = errorCode
            captured_error["errorString"] = errorString

        async def _fetch_bars():
            # reqHistoricalData() blocks on the IB client's own event loop
            # (via ib_insync's _run()/util.run()), so it cannot be called
            # directly from FastAPI's request thread (whose loop is already
            # running) nor from a brand-new event loop in another thread
            # (which never sees the socket responses handled by the
            # connection loop). This coroutine is instead scheduled onto
            # the IBService background thread's loop -- the one actually
            # driving self._ib.run().
            qualified = await self._ib.qualifyContractsAsync(contract)
            if not qualified:
                raise HistoricalDataError(
                    f"Could not resolve a unique contract for symbol={symbol_upper}"
                    + (f" primary_exchange={primary_exchange}" if primary_exchange else "")
                    + " -- try passing primary_exchange to disambiguate."
                )
            resolved = qualified[0]
            self._ib.errorEvent += _on_error
            try:
                bars = await self._ib.reqHistoricalDataAsync(
                    resolved,
                    endDateTime=end_datetime,  # "" means "now"
                    durationStr=duration,
                    barSizeSetting=bar_size,
                    whatToShow=what_to_show_upper,
                    useRTH=use_rth,
                    formatDate=2,  # UTC epoch for intraday bars; plain YYYYMMDD for daily+ -- sidesteps TWS-local-timezone ambiguity
                    timeout=30,  # 30 second timeout for HMDS connection
                )
            finally:
                self._ib.errorEvent -= _on_error
            return resolved, bars

        try:
            future = asyncio.run_coroutine_threadsafe(_fetch_bars(), self._loop)
            resolved_contract, bars = future.result(timeout=35)  # Slightly longer than reqHistoricalData timeout

            if not bars:
                detail = captured_error.get("errorString") or (
                    "IB returned zero bars and reported no error -- check symbol/date range/"
                    "market data permissions for this account."
                )
                raise HistoricalDataError(detail, ib_error_code=captured_error.get("errorCode"))

            # Convert bars to JSON-serializable format
            result: List[Dict[str, Any]] = []
            for bar in bars:
                result.append(
                    {
                        "date": bar.date.isoformat() if hasattr(bar.date, "isoformat") else str(bar.date),
                        "open": self._sanitize_float(bar.open),
                        "high": self._sanitize_float(bar.high),
                        "low": self._sanitize_float(bar.low),
                        "close": self._sanitize_float(bar.close),
                        "volume": self._sanitize_float(bar.volume),
                    }
                )

            logger.info("Historical data success: symbol=%s bars=%d", symbol_upper, len(result))

            return {
                "bars": result,
                "meta": {
                    "symbol": symbol_upper,
                    "what_to_show": what_to_show_upper,
                    "bar_size": bar_size,
                    "requested_duration": duration,
                    "use_rth": use_rth,
                    "end_datetime": end_datetime or None,
                    "actual_start": result[0]["date"],
                    "actual_end": result[-1]["date"],
                    "bar_count": len(result),
                    "contract_id": resolved_contract.conId,
                    "exchange": resolved_contract.exchange,
                    "primary_exchange": resolved_contract.primaryExchange,
                    "currency": resolved_contract.currency,
                },
            }

        except HistoricalDataError:
            raise
        except Exception as e:
            logger.exception("Historical data failed for %s: %s", symbol_upper, e)
            raise

    def place_order(
        self, symbol: str, action: str, quantity: float, order_type: str = "MKT", lmt_price: Optional[float] = None, outside_rth: bool = False,
        take_profit_pct: Optional[float] = None, stop_loss_pct: Optional[float] = None, transmit: bool = True
    ) -> Dict[str, Any]:
        """
        Place a buy or sell order after performing risk checks.
        """
        symbol_upper = symbol.strip().upper() if symbol else ""
        action_upper = action.strip().upper() if action else ""

        if action_upper not in ["BUY", "SELL"]:
            raise ValueError("Action must be 'BUY' or 'SELL'")
            
        if quantity <= 0:
            raise ValueError("Quantity must be greater than zero")

        if quantity > self.max_qty:
            raise ValueError(f"Order quantity {quantity} exceeds max_qty limit of {self.max_qty}")

        if not self._ib or not self._ib.isConnected():
            raise RuntimeError("Bot not connected to IBKR")

        if self._loop is None:
            raise RuntimeError("IB event loop not available")

        # Check max notional (current price * quantity)
        # We can use the existing get_quote to find the current price
        try:
            quote = self.get_quote(symbol_upper)
            # Find a valid price from the quote (last, close, bid/ask midpoint etc)
            current_price = quote.get("last") or quote.get("close")
            
            if current_price is None and (quote.get("bid") is not None and quote.get("ask") is not None):
                current_price = (quote["bid"] + quote["ask"]) / 2.0
                
            if current_price is None:
                # If it's a limit order, we could theoretically use the limit price as the fallback
                if order_type.upper() == "LMT" and lmt_price is not None:
                    current_price = lmt_price
                else:
                    raise ValueError(f"Could not determine current price for {symbol_upper} to perform notional risk check")
            
            notional = current_price * quantity
            if notional > self.max_notional:
                raise ValueError(
                    f"Order notional {notional:.2f} (qty {quantity} * price {current_price:.2f}) "
                    f"exceeds max_notional limit of {self.max_notional}"
                )
        except Exception as e:
            if isinstance(e, ValueError) and "notional" in str(e).lower():
                raise
            # If quote fails completely, we might want to block the order for safety
            raise RuntimeError(f"Risk check failed: could not fetch quote for {symbol_upper}: {e}")

        contract = Stock(symbol_upper, "SMART", "USD")
        
        order: Order
        if order_type.upper() == "MKT":
            if outside_rth:
                raise ValueError("Extended hours trading requires a Limit (LMT) order")
            order = MarketOrder(action_upper, quantity)
        elif order_type.upper() == "LMT":
            if lmt_price is None:
                raise ValueError("lmt_price is required for LMT orders")
            order = LimitOrder(action_upper, quantity, lmt_price)
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

        order.outsideRth = outside_rth

        import asyncio

        async def _place():
            # First qualify the contract so the order is valid
            qualified = await self._ib.qualifyContractsAsync(contract)
            if not qualified:
                raise RuntimeError(f"Could not qualify contract for {symbol_upper}")
            
            resolved_contract = qualified[0]
            
            if take_profit_pct is not None or stop_loss_pct is not None:
                # Need to use bracket order logic
                parent_id = self._ib.client.getReqId()
                order.orderId = parent_id
                order.transmit = False
                self._ib.placeOrder(resolved_contract, order)
                
                # We need a reference price to calculate absolute bracket prices
                ref_price = current_price  # Already fetched during risk check
                
                if take_profit_pct is not None:
                    tp_price = ref_price * (1 + take_profit_pct) if action_upper == "BUY" else ref_price * (1 - take_profit_pct)
                    # Round to nearest 0.01
                    tp_price = round(tp_price, 2)
                    tp_action = "SELL" if action_upper == "BUY" else "BUY"
                    tp_order = LimitOrder(tp_action, quantity, tp_price)
                    tp_order.orderId = self._ib.client.getReqId()
                    tp_order.parentId = parent_id
                    tp_order.transmit = False  # Transmit false unless it's the last order
                    # If there's no stop loss, this is the last one
                    if stop_loss_pct is None:
                        tp_order.transmit = transmit
                    self._ib.placeOrder(resolved_contract, tp_order)
                    
                if stop_loss_pct is not None:
                    sl_price = ref_price * (1 - stop_loss_pct) if action_upper == "BUY" else ref_price * (1 + stop_loss_pct)
                    sl_price = round(sl_price, 2)
                    sl_action = "SELL" if action_upper == "BUY" else "BUY"
                    sl_order = StopOrder(sl_action, quantity, sl_price)
                    sl_order.orderId = self._ib.client.getReqId()
                    sl_order.parentId = parent_id
                    sl_order.transmit = transmit # Last order transmits
                    self._ib.placeOrder(resolved_contract, sl_order)
                
                # Fetch the trade object for the parent
                trade = next((t for t in self._ib.trades() if getattr(t.order, "orderId", None) == parent_id), None)
            else:
                order.transmit = transmit
                trade = self._ib.placeOrder(resolved_contract, order)
            
            
            # Wait a tiny bit for the order to be submitted to the broker
            await asyncio.sleep(0.5)
            
            return trade

        try:
            future = asyncio.run_coroutine_threadsafe(_place(), self._loop)
            trade = future.result(timeout=10)
            
            return {
                "symbol": symbol_upper,
                "action": trade.order.action,
                "quantity": trade.order.totalQuantity,
                "order_type": trade.order.orderType,
                "status": trade.orderStatus.status,
                "filled": trade.orderStatus.filled,
                "remaining": trade.orderStatus.remaining,
                "avg_fill_price": trade.orderStatus.avgFillPrice,
            }
        except Exception as e:
            logger.exception("Order placement failed for %s: %s", symbol_upper, e)
            raise RuntimeError(f"Failed to place order: {e}")

