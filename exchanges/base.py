from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Optional, Tuple


class BaseExchangeClient(ABC):
    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def get_position(self, market_id: str) -> Decimal:
        ...

    @abstractmethod
    async def get_balance(self) -> Decimal:
        ...

    @abstractmethod
    async def cancel_all_orders(self, market_id: str) -> None:
        ...

    @abstractmethod
    async def close_position(self, market_id: str, position: Decimal, slippage_pct: Decimal) -> bool:
        ...

    @abstractmethod
    def get_bbo(self) -> Tuple[Optional[Decimal], Optional[Decimal]]:
        """Returns (best_bid, best_ask) or (None, None) if not available."""
        ...
