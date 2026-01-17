"""Market data provider interface and implementations."""
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict, List, Tuple
from datetime import datetime, date
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class MarketDataProvider(ABC):
    """Abstract interface for market data providers."""

    @abstractmethod
    async def get_quote(self, ticker: str) -> Optional[Dict[str, float]]:
        """
        Get current quote for ticker.
        Returns dict with keys: 'price', 'previous_close', 'percent_change'
        or None if unavailable.
        """
        pass

    @abstractmethod
    async def get_daily_bars(self, ticker: str, days: int = 10) -> Optional[List[Dict]]:
        """
        Get daily bars (OHLC) for ticker.
        Returns list of dicts with keys: 'date', 'open', 'high', 'low', 'close', 'volume'
        or None if unavailable.
        Dates are trading dates (regular-hours close dates).
        """
        pass

    async def get_today_rth_open(self, ticker: str, trading_date: Optional[date] = None) -> Optional[float]:
        """
        Get today's RTH open price (daily bar Open) for a specific trading date (ET date).
        Providers may ignore trading_date and return the most recent daily open.
        """
        raise NotImplementedError

    async def get_latest_rth_price_1m(
        self,
        ticker: str,
        timezone: ZoneInfo = ZoneInfo("America/New_York"),
    ) -> Optional[Tuple[float, datetime]]:
        """
        Get latest RTH price from 1m bars (last Close) during RTH.
        Returns (price, as_of_dt) where as_of_dt is timezone-aware in the provided timezone.
        """
        raise NotImplementedError


class YahooFinanceProvider(MarketDataProvider):
    """Market data provider using yfinance."""

    def __init__(self):
        try:
            import yfinance as yf
            self.yf = yf
        except ImportError:
            logger.error("yfinance not installed. Install with: pip install yfinance")
            self.yf = None

    async def get_quote(self, ticker: str) -> Optional[Dict[str, float]]:
        """Get quote from Yahoo Finance."""
        if not self.yf:
            return None

        try:
            import asyncio

            # Run blocking yfinance call in thread
            def fetch():
                stock = self.yf.Ticker(ticker)
                _ = stock.info
                hist = stock.history(period="2d")

                if hist.empty:
                    return None

                current_price = hist["Close"].iloc[-1]
                previous_close = hist["Close"].iloc[-2] if len(hist) > 1 else current_price

                percent_change = ((current_price - previous_close) / previous_close) * 100

                return {
                    "price": float(current_price),
                    "previous_close": float(previous_close),
                    "percent_change": float(percent_change),
                }

            result = await asyncio.to_thread(fetch)
            return result

        except Exception as e:
            logger.error(f"Failed to fetch quote for {ticker}: {e}")
            return None

    async def get_daily_bars(self, ticker: str, days: int = 10) -> Optional[List[Dict]]:
        """
        Get daily bars (OHLC) for ticker.
        Returns list of dicts with keys: 'date', 'open', 'high', 'low', 'close', 'volume'.
        Dates are trading dates (regular-hours close dates).
        """
        if not self.yf:
            return None

        try:
            import asyncio

            def fetch():
                stock = self.yf.Ticker(ticker)
                # Fetch enough days to account for weekends/holidays
                hist = stock.history(period=f"{days + 5}d")

                if hist.empty:
                    return None

                bars = []
                for idx, row in hist.iterrows():
                    # Convert index (Timestamp) to date
                    trading_date = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx).split()[0])

                    bars.append(
                        {
                            "date": trading_date,
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"]),  # Regular-hours close
                            "volume": int(row["Volume"]) if "Volume" in row else 0,
                        }
                    )

                # Return most recent first (reverse order)
                bars.reverse()
                return bars[:days] if len(bars) > days else bars

            result = await asyncio.to_thread(fetch)
            return result

        except Exception as e:
            logger.error(f"Failed to fetch daily bars for {ticker}: {e}")
            return None

    async def get_today_rth_open(self, ticker: str, trading_date: Optional[date] = None) -> Optional[float]:
        """Get today's RTH open (daily bar Open) using yfinance daily history."""
        if not self.yf:
            return None

        try:
            import asyncio

            et = ZoneInfo("America/New_York")
            target_date = trading_date or datetime.now(et).date()

            def fetch() -> Optional[float]:
                stock = self.yf.Ticker(ticker)
                hist = stock.history(period="7d", interval="1d", auto_adjust=False)
                if hist is None or getattr(hist, "empty", True):
                    return None
                for idx, row in hist.iterrows():
                    d = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx).split()[0])
                    if d == target_date:
                        try:
                            return float(row["Open"])
                        except Exception:
                            return None
                return None

            return await asyncio.to_thread(fetch)
        except Exception as e:
            logger.error(f"Failed to fetch today's open for {ticker}: {e}")
            return None

    async def get_latest_rth_price_1m(
        self,
        ticker: str,
        timezone: ZoneInfo = ZoneInfo("America/New_York"),
    ) -> Optional[Tuple[float, datetime]]:
        """Get latest RTH 1m close price during RTH using yfinance 1m bars."""
        if not self.yf:
            return None

        try:
            import asyncio

            def fetch() -> Optional[Tuple[float, datetime]]:
                stock = self.yf.Ticker(ticker)
                hist = stock.history(period="2d", interval="1m", prepost=False, auto_adjust=False)
                if hist is None or getattr(hist, "empty", True):
                    return None

                idx = hist.index
                # Convert index to requested timezone (if tz-naive, assume UTC)
                try:
                    if getattr(idx, "tz", None) is None:
                        idx = idx.tz_localize("UTC").tz_convert(timezone)
                    else:
                        idx = idx.tz_convert(timezone)
                except Exception:
                    return None

                today = datetime.now(timezone).date()
                rth_start = datetime.combine(today, datetime.min.time(), timezone).replace(hour=9, minute=30)
                rth_end = datetime.combine(today, datetime.min.time(), timezone).replace(hour=16, minute=0)

                mask = (idx >= rth_start) & (idx <= rth_end)
                if not mask.any():
                    return None

                filtered = hist.loc[mask]
                if getattr(filtered, "empty", True):
                    return None

                last_ts = idx[mask][-1]
                try:
                    last_close = float(filtered["Close"].iloc[-1])
                except Exception:
                    return None

                as_of = last_ts.to_pydatetime() if hasattr(last_ts, "to_pydatetime") else last_ts
                return last_close, as_of

            return await asyncio.to_thread(fetch)
        except Exception as e:
            logger.error(f"Failed to fetch latest 1m RTH price for {ticker}: {e}")
            return None
