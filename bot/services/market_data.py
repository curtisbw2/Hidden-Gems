"""Market data provider interface and implementations."""
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict
from datetime import datetime

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
                info = stock.info
                hist = stock.history(period="2d")
                
                if hist.empty:
                    return None
                
                current_price = hist['Close'].iloc[-1]
                previous_close = hist['Close'].iloc[-2] if len(hist) > 1 else current_price
                
                percent_change = ((current_price - previous_close) / previous_close) * 100
                
                return {
                    "price": float(current_price),
                    "previous_close": float(previous_close),
                    "percent_change": float(percent_change)
                }
            
            result = await asyncio.to_thread(fetch)
            return result
            
        except Exception as e:
            logger.error(f"Failed to fetch quote for {ticker}: {e}")
            return None
