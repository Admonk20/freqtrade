"""
Arbitrage Pair List Handler - Detects cross-exchange arbitrage opportunities
"""

import logging
from typing import Any

from freqtrade.constants import Config
from freqtrade.exceptions import OperationalException
from freqtrade.exchange import Exchange
from freqtrade.plugins.pairlist.IPairList import IPairList, PairlistParameter, SupportsBacktesting
from freqtrade.resolvers.exchange_resolver import ExchangeResolver


logger = logging.getLogger(__name__)


class ArbitragePairList(IPairList):
    """
    Arbitrage PairList provider for cross-exchange arbitrage detection.

    Filters pairs based on price differences between two exchanges.
    Requires a second exchange to be configured in the pairlist config.

    Usage:
    "pairlists": [
        {
            "method": "ArbitragePairList",
            "params": {
                "second_exchange": "coinbase",
                "min_spread_pct": 2.0,
                "include_fees": true,
                "max_pairs": 10
            }
        }
    ]
    """

    is_pairlist_generator = True
    supports_backtesting = SupportsBacktesting.NO

    def __init__(
        self,
        exchange: Exchange,
        pairlistmanager,
        config: Config,
        pairlistconfig: dict[str, Any],
        pairlist_pos: int,
    ) -> None:
        super().__init__(exchange, pairlistmanager, config, pairlistconfig, pairlist_pos)

        # Get second exchange name
        self._second_exchange_name = self._pairlistconfig.get("second_exchange", "")
        if not self._second_exchange_name:
            raise OperationalException(
                "ArbitragePairList requires 'second_exchange' parameter in config."
            )

        # Initialize second exchange
        self._second_exchange = self._init_second_exchange()

        # Get arbitrage parameters
        self._min_spread_pct = self._pairlistconfig.get("min_spread_pct", 1.0)
        self._include_fees = self._pairlistconfig.get("include_fees", True)
        self._max_pairs = self._pairlistconfig.get("max_pairs", 10)

        # Refresh period (in seconds)
        self.refresh_period = self._pairlistconfig.get("refresh_period", 30)

        self.log_on_refresh(
            logger.info,
            f"Arbitrage detection initialized: {self._exchange.name} <-> "
            f"{self._second_exchange_name}, min spread: {self._min_spread_pct}%",
        )

    def _init_second_exchange(self) -> Exchange:
        """Initialize the second exchange for arbitrage detection."""
        # Create config for second exchange
        second_config = self._config.copy()
        second_config["exchange"]["name"] = self._second_exchange_name

        # Load second exchange
        try:
            second_exchange = ExchangeResolver.load_exchange(second_config)
            logger.info(
                f"Successfully initialized second exchange: {self._second_exchange_name}"
            )
            return second_exchange
        except Exception as e:
            raise OperationalException(
                f"Failed to initialize second exchange {self._second_exchange_name}: {e}"
            ) from e

    @property
    def needstickers(self) -> bool:
        """
        Boolean property defining if tickers are necessary.
        """
        return True

    @staticmethod
    def description() -> str:
        return "Filter pairs by cross-exchange arbitrage opportunities."

    @staticmethod
    def available_parameters() -> dict[str, PairlistParameter]:
        return {
            "second_exchange": {
                "type": "string",
                "default": "",
                "description": "Second exchange name",
                "help": "Name of the second exchange to compare prices (e.g., 'coinbase')",
            },
            "min_spread_pct": {
                "type": "number",
                "default": 1.0,
                "description": "Minimum spread percentage",
                "help": "Minimum price difference percentage to consider as arbitrage opportunity",
            },
            "include_fees": {
                "type": "boolean",
                "default": True,
                "description": "Include trading fees",
                "help": "Include trading fees in spread calculation",
            },
            "max_pairs": {
                "type": "number",
                "default": 10,
                "description": "Maximum pairs",
                "help": "Maximum number of arbitrage pairs to return",
            },
            "refresh_period": {
                "type": "number",
                "default": 30,
                "description": "Refresh period (seconds)",
                "help": "Time in seconds before refreshing arbitrage opportunities",
            },
        }

    def _calculate_spread(
        self, pair: str, price1: float, price2: float, fee1: float = 0, fee2: float = 0
    ) -> float:
        """
        Calculate spread percentage between two prices.

        Returns positive spread if buying on exchange 1 and selling on exchange 2 is profitable.
        Returns negative spread if buying on exchange 2 and selling on exchange 1 is profitable.
        """
        if price1 <= 0 or price2 <= 0:
            return 0.0

        # Calculate spread as percentage
        # Positive: buy on exchange1 (lower), sell on exchange2 (higher)
        # Negative: buy on exchange2 (lower), sell on exchange1 (higher)
        spread = ((price2 - price1) / price1) * 100

        # Include fees if configured
        if self._include_fees:
            # Total fee = buy fee + sell fee (both as percentages)
            total_fee_pct = (fee1 + fee2) * 100
            spread -= total_fee_pct

        return spread

    def _get_exchange_fees(self, exchange: Exchange, pair: str) -> float:
        """Get trading fee for an exchange and pair."""
        try:
            # Get taker fee (more conservative for arbitrage)
            fee = exchange.get_fee(pair, taker_or_maker="taker")
            return fee
        except Exception:
            # Return default conservative fee if unable to fetch
            return 0.006  # 0.6%

    def gen_pairlist(self, tickers: dict) -> list[str]:
        """
        Generate pairlist based on arbitrage opportunities.

        :param tickers: Tickers dict from primary exchange
        :return: List of pairs with arbitrage opportunities
        """
        # Get common pairs between both exchanges
        pairs_exchange1 = set(self._exchange.get_markets().keys())
        pairs_exchange2 = set(self._second_exchange.get_markets().keys())
        common_pairs = list(pairs_exchange1.intersection(pairs_exchange2))

        if not common_pairs:
            self.log_on_refresh(
                logger.warning,
                f"No common pairs found between {self._exchange.name} and "
                f"{self._second_exchange_name}",
            )
            return []

        # Fetch tickers from second exchange
        try:
            tickers_exchange2 = self._second_exchange.get_tickers(common_pairs)
        except Exception as e:
            self.log_on_refresh(
                logger.error, f"Error fetching tickers from {self._second_exchange_name}: {e}"
            )
            return []

        # Calculate spreads for all common pairs
        arbitrage_opportunities = []

        for pair in common_pairs:
            if pair not in tickers or pair not in tickers_exchange2:
                continue

            ticker1 = tickers[pair]
            ticker2 = tickers_exchange2[pair]

            # Get bid/ask prices (more realistic for arbitrage)
            # Buy at ask, sell at bid
            ask1 = ticker1.get("ask")
            bid1 = ticker1.get("bid")
            ask2 = ticker2.get("ask")
            bid2 = ticker2.get("bid")

            if not all([ask1, bid1, ask2, bid2]):
                continue

            # Get fees
            fee1 = self._get_exchange_fees(self._exchange, pair)
            fee2 = self._get_exchange_fees(self._second_exchange, pair)

            # Calculate both arbitrage directions
            # Direction 1: Buy on exchange1, sell on exchange2
            spread1 = self._calculate_spread(pair, ask1, bid2, fee1, fee2)

            # Direction 2: Buy on exchange2, sell on exchange1
            spread2 = self._calculate_spread(pair, ask2, bid1, fee2, fee1)

            # Take the maximum profitable spread
            max_spread = max(abs(spread1), abs(spread2))

            if max_spread >= self._min_spread_pct:
                # Determine which direction is more profitable
                if abs(spread1) > abs(spread2):
                    direction = f"{self._exchange.name} -> {self._second_exchange_name}"
                    buy_price = ask1
                    sell_price = bid2
                    actual_spread = spread1
                else:
                    direction = f"{self._second_exchange_name} -> {self._exchange.name}"
                    buy_price = ask2
                    sell_price = bid1
                    actual_spread = spread2

                arbitrage_opportunities.append(
                    {
                        "pair": pair,
                        "spread_pct": actual_spread,
                        "direction": direction,
                        "buy_price": buy_price,
                        "sell_price": sell_price,
                    }
                )

        # Sort by spread (descending) and limit to max_pairs
        arbitrage_opportunities.sort(key=lambda x: abs(x["spread_pct"]), reverse=True)
        top_opportunities = arbitrage_opportunities[: self._max_pairs]

        # Log opportunities
        if top_opportunities:
            self.log_on_refresh(
                logger.info,
                f"Found {len(top_opportunities)} arbitrage opportunities. "
                f"Top spread: {top_opportunities[0]['spread_pct']:.2f}% "
                f"({top_opportunities[0]['pair']} - {top_opportunities[0]['direction']})",
            )
        else:
            self.log_on_refresh(
                logger.info,
                f"No arbitrage opportunities found with min spread {self._min_spread_pct}%",
            )

        return [opp["pair"] for opp in top_opportunities]

    def filter_pairlist(self, pairlist: list[str], tickers: dict) -> list[str]:
        """
        Filter pairlist (not used as generator, but required by interface).
        """
        return pairlist
