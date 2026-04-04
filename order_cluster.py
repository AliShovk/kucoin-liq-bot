import logging
from config import Config

logger = logging.getLogger(__name__)


class OrderClusterDetector:
    """Обнаружение скопления ордеров (bid/ask wall) в стакане."""

    def __init__(self):
        self.depth = Config.ORDER_CLUSTER_DEPTH
        self.ratio = Config.ORDER_CLUSTER_RATIO
        self.last_bids: list[list[float]] = []
        self.last_asks: list[list[float]] = []

    def update(self, bids: list[list[float]], asks: list[list[float]]):
        """Обновить стакан. bids/asks — [[price, size], ...]."""
        self.last_bids = bids[: self.depth]
        self.last_asks = asks[: self.depth]

    def _find_cluster(self, levels: list[list[float]]) -> tuple[float, float] | None:
        """Найти уровень, объём которого >= ratio * средний объём."""
        if len(levels) < 3:
            return None
        sizes = [lvl[1] for lvl in levels]
        avg_size = sum(sizes) / len(sizes)
        if avg_size == 0:
            return None
        for price, size in levels:
            if size >= self.ratio * avg_size:
                return (price, size)
        return None

    def check_signal(self, side_hint: str | None = None) -> dict | None:
        """Проверить наличие кластера.

        side_hint: 'buy' — ищем bid-wall (поддержку), 'sell' — ask-wall (сопротивление).
        Если None — проверяем обе стороны.

        Возвращает {'side': 'buy'|'sell', 'price': float, 'volume': float} или None.
        """
        if side_hint in (None, "buy"):
            cluster = self._find_cluster(self.last_bids)
            if cluster:
                logger.info("Bid cluster at %.2f vol=%.4f", cluster[0], cluster[1])
                return {"side": "buy", "price": cluster[0], "volume": cluster[1]}

        if side_hint in (None, "sell"):
            cluster = self._find_cluster(self.last_asks)
            if cluster:
                logger.info("Ask cluster at %.2f vol=%.4f", cluster[0], cluster[1])
                return {"side": "sell", "price": cluster[0], "volume": cluster[1]}

        return None
