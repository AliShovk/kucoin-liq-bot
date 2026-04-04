import time
import logging
from collections import deque
from config import Config

logger = logging.getLogger(__name__)


class DominanceShiftDetector:
    """Обнаружение перехода перевеса от покупателей к продавцам и наоборот.

    Работает на потоке сделок (trades). Аккумулирует объёмы buy/sell
    за скользящее окно и определяет, когда одна сторона доминирует.
    """

    def __init__(self):
        self.window = Config.DOMINANCE_SHIFT_WINDOW  # секунды
        self.threshold = Config.DOMINANCE_SHIFT_THRESHOLD
        # (timestamp, side, volume)
        self.trades: deque[tuple[float, str, float]] = deque()
        self._prev_dominant: str | None = None

    def add_trade(self, ts: float, side: str, volume: float):
        """Добавить сделку. side = 'buy' | 'sell'."""
        self.trades.append((ts, side, volume))
        self._prune(ts)

    def _prune(self, now: float):
        cutoff = now - self.window
        while self.trades and self.trades[0][0] < cutoff:
            self.trades.popleft()

    def check_signal(self, side_hint: str | None = None) -> dict | None:
        """Проверить, произошёл ли переход перевеса.

        Возвращает {'side': 'buy'|'sell', 'buy_pct': float, 'sell_pct': float}
        только при смене доминирования (или если side_hint совпадает).
        """
        if len(self.trades) < 5:
            return None

        buy_vol = sum(v for _, s, v in self.trades if s == "buy")
        sell_vol = sum(v for _, s, v in self.trades if s == "sell")
        total = buy_vol + sell_vol
        if total == 0:
            return None

        buy_pct = buy_vol / total
        sell_pct = sell_vol / total

        if buy_pct >= self.threshold:
            dominant = "buy"
        elif sell_pct >= self.threshold:
            dominant = "sell"
        else:
            return None

        # Фиксируем только при *смене* перевеса или если совпадает с hint
        shifted = (self._prev_dominant is not None and dominant != self._prev_dominant)
        matches_hint = (side_hint is not None and dominant == side_hint)

        self._prev_dominant = dominant

        if shifted or matches_hint:
            logger.info("Dominance shift → %s  buy=%.1f%% sell=%.1f%%",
                        dominant, buy_pct * 100, sell_pct * 100)
            return {"side": dominant, "buy_pct": buy_pct, "sell_pct": sell_pct}

        return None
