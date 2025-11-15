"""Coinbase exchange subclass"""

import logging
from typing import Any

import ccxt

from freqtrade.constants import BuySell
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import DDosProtection, OperationalException, TemporaryError
from freqtrade.exchange import Exchange
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange_types import CcxtBalances, FtHas


logger = logging.getLogger(__name__)


class Coinbase(Exchange):
    """Coinbase exchange class.
    Contains adjustments needed for Freqtrade to work with this exchange.

    Note: This implements Coinbase Advanced Trade API (formerly Coinbase Pro)
    """

    _ft_has: FtHas = {
        "stoploss_on_exchange": True,
        "stop_price_param": "stopPrice",
        "stop_price_prop": "stopPrice",
        "stoploss_order_types": {"limit": "limit", "market": "market"},
        "order_time_in_force": ["GTC", "GTT", "IOC", "FOK"],
        "ohlcv_has_history": True,
        "trades_pagination": "time",
        "trades_pagination_arg": "before",
        "trades_pagination_overlap": True,
        "trades_has_history": True,
        "mark_ohlcv_timeframe": "1h",
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
    ]

    def market_is_tradable(self, market: dict[str, Any]) -> bool:
        """
        Check if the market symbol is tradable by Freqtrade.
        Default checks + ensure market is active and not delisted.
        """
        parent_check = super().market_is_tradable(market)

        # Coinbase-specific: Check trading is enabled
        return (
            parent_check
            and market.get("active", True) is True
            and market.get("info", {}).get("status", "") != "delisted"
        )

    @retrier
    def get_balances(self) -> CcxtBalances:
        """
        Fetch balances from Coinbase.
        Coinbase returns balances in a standard format.
        """
        if self._config["dry_run"]:
            return {}

        try:
            balances = self._api.fetch_balance()
            # Remove additional info from ccxt results
            balances.pop("info", None)
            balances.pop("free", None)
            balances.pop("total", None)
            balances.pop("used", None)
            self._log_exchange_response("fetch_balances", balances)

            return balances
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not get balance due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _set_leverage(
        self,
        leverage: float,
        pair: str | None = None,
        accept_fail: bool = False,
    ):
        """
        Coinbase spot trading does not support leverage.
        This is a no-op for Coinbase.
        """
        return

    def _get_params(
        self,
        side: BuySell,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = "GTC",
    ) -> dict:
        """
        Get order parameters for Coinbase.
        Handles time_in_force and post-only orders.
        """
        params = super()._get_params(
            side=side,
            ordertype=ordertype,
            leverage=leverage,
            reduceOnly=reduceOnly,
            time_in_force=time_in_force,
        )

        # Coinbase uses different time_in_force values
        if time_in_force:
            params["time_in_force"] = time_in_force

        return params

    def get_fee(self, symbol: str, type: str = "", side: str = "", amount: float = 1,
                price: float = 1, taker_or_maker: str = "maker") -> float:
        """
        Get trading fee for a symbol.
        Coinbase has a tiered fee structure based on 30-day volume.
        Default maker: 0.4%, taker: 0.6% for low volume traders.
        """
        try:
            return super().get_fee(symbol, type, side, amount, price, taker_or_maker)
        except Exception:
            # Fallback to default Coinbase fees
            return 0.006 if taker_or_maker == "taker" else 0.004
