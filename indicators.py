import numpy as np
import logging
from collections import deque
from config import Config

logger = logging.getLogger(__name__)


class BollingerBands:
    """Скользящие полосы Боллинджера на потоке цен закрытия."""

    def __init__(self, period: int = None, num_std: float = None):
        self.period = period or Config.BOLLINGER_PERIOD
        self.num_std = num_std or Config.BOLLINGER_STD
        self.closes: deque[float] = deque(maxlen=self.period)
        self.ready = False

    def update(self, close_price: float) -> dict | None:
        """Добавить цену закрытия. Возвращает dict с upper/middle/lower или None."""
        self.closes.append(close_price)
        if len(self.closes) < self.period:
            return None
        self.ready = True
        arr = np.array(self.closes)
        middle = float(np.mean(arr))
        std = float(np.std(arr, ddof=0))
        upper = middle + self.num_std * std
        lower = middle - self.num_std * std
        return {"upper": upper, "middle": middle, "lower": lower}

    def check_signal(self, price: float) -> str | None:
        """Проверить, пробила ли цена линию Боллинджера.
        Возвращает 'buy' / 'sell' / None.
        """
        bands = self.update(price)
        if bands is None:
            return None
        if price <= bands["lower"]:
            logger.info("BB buy signal: price %.2f <= lower %.2f", price, bands["lower"])
            return "buy"
        if price >= bands["upper"]:
            logger.info("BB sell signal: price %.2f >= upper %.2f", price, bands["upper"])
            return "sell"
        return None

    @property
    def last_bands(self) -> dict | None:
        if not self.ready:
            return None
        arr = np.array(self.closes)
        middle = float(np.mean(arr))
        std = float(np.std(arr, ddof=0))
        upper = middle + self.num_std * std
        lower = middle - self.num_std * std
        return {"upper": upper, "middle": middle, "lower": lower}
