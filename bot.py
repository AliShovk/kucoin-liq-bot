"""Главный оркестратор — конвейер сигналов.

Логика:
  1. 🟢       Обнаружена крупная ликвидация
  2. 🟢🟢     Цена пробила/коснулась линии Боллинджера (подтверждение)
  3. 🟢🟢🟢   Скопление ордеров в стакане на стороне разворота
  4. 🟢🟢🟢🟢 Перевес объёмов (покупатели vs продавцы) подтверждён
  → 🎆 Финальный сигнал: ПОКУПКА / ПРОДАЖА
"""

import asyncio
import time
import logging

from config import Config
from kucoin_ws import KuCoinFuturesWS
from indicators import BollingerBands
from order_cluster import OrderClusterDetector
from dominance import DominanceShiftDetector
from telegram_notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bot")

# ── Таймаут сигнального окна (секунды) ──────────────────────────────
SIGNAL_WINDOW = 120  # если все 4 этапа не собрались за 2 мин — сброс


class SignalPipeline:
    """Конечный автомат: собирает 4 подтверждения и выдаёт финальный сигнал."""

    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier
        self.reset()

    def reset(self):
        self.stage = 0
        self.side: str | None = None  # 'buy' | 'sell'
        self.start_time: float = 0
        self.liq_size: float = 0
        self.last_price: float = 0
        logger.info("Pipeline reset")

    def _expired(self) -> bool:
        return self.stage > 0 and (time.time() - self.start_time > SIGNAL_WINDOW)

    # ── Этап 1: Ликвидация ───────────────────────────────────────────

    async def on_liquidation(self, side: str, size_usd: float, price: float):
        if self._expired():
            self.reset()

        # Любая крупная ликвидация стартует новый сигнал
        # sell-ликвидация (long liquidated) → ожидаем разворот вверх (buy)
        # buy-ликвидация (short liquidated) → ожидаем разворот вниз (sell)
        signal_side = "buy" if side == "sell" else "sell"

        self.reset()
        self.stage = 1
        self.side = signal_side
        self.start_time = time.time()
        self.liq_size = size_usd
        self.last_price = price

        await self.notifier.stage_1_liquidation(Config.SYMBOL, side, size_usd)
        logger.info("Stage 1 ✓  side=%s  liq_size=$%.0f", self.side, size_usd)

    # ── Этап 2: Боллинджер ───────────────────────────────────────────

    async def on_bollinger(self, bb_side: str, price: float,
                            lower: float, upper: float):
        if self._expired():
            self.reset()
            return
        if self.stage != 1:
            return
        if bb_side != self.side:
            return

        self.stage = 2
        self.last_price = price
        await self.notifier.stage_2_bollinger(Config.SYMBOL, bb_side, price, lower, upper)
        logger.info("Stage 2 ✓  BB %s @ %.2f", bb_side, price)

    # ── Этап 3: Скопление ордеров ────────────────────────────────────

    async def on_order_cluster(self, cluster_side: str, cluster_price: float,
                                cluster_vol: float):
        if self._expired():
            self.reset()
            return
        if self.stage != 2:
            return
        if cluster_side != self.side:
            return

        self.stage = 3
        await self.notifier.stage_3_order_cluster(
            Config.SYMBOL, cluster_side, cluster_price, cluster_vol)
        logger.info("Stage 3 ✓  cluster %s @ %.2f vol=%.4f",
                     cluster_side, cluster_price, cluster_vol)

    # ── Этап 4: Перевес ──────────────────────────────────────────────

    async def on_dominance_shift(self, dom_side: str, buy_pct: float,
                                  sell_pct: float, price: float):
        if self._expired():
            self.reset()
            return
        if self.stage != 3:
            return
        if dom_side != self.side:
            return

        self.stage = 4
        self.last_price = price
        await self.notifier.stage_4_dominance_shift(
            Config.SYMBOL, dom_side, buy_pct, sell_pct)

        # 🎆 Все 4 подтверждения → финальный сигнал
        await self.notifier.stage_final_signal(Config.SYMBOL, self.side, price)
        logger.info("Stage 4 ✓  FINAL SIGNAL  %s @ %.2f 🎆", self.side, price)
        self.reset()


# ═════════════════════════════════════════════════════════════════════
# Главное приложение
# ═════════════════════════════════════════════════════════════════════

class TradingBot:
    def __init__(self):
        self.notifier = TelegramNotifier()
        self.pipeline = SignalPipeline(self.notifier)
        self.bb = BollingerBands()
        self.cluster = OrderClusterDetector()
        self.dominance = DominanceShiftDetector()
        self.ws = KuCoinFuturesWS()
        self._last_price: float = 0

        # Регистрируем callback-и
        self.ws.on_trade(self._handle_trade)
        self.ws.on_orderbook(self._handle_orderbook)
        self.ws.on_liquidation(self._handle_liquidation)

    # ── Обработка сделок ─────────────────────────────────────────────

    async def _handle_trade(self, data: dict):
        try:
            price = float(data.get("price", 0))
            size = float(data.get("size", 0))
            side = data.get("side", "buy").lower()  # 'buy' | 'sell'
            ts = float(data.get("ts", time.time() * 1e9)) / 1e9  # наносек → сек
        except (ValueError, TypeError):
            return

        if price <= 0:
            return

        self._last_price = price

        # ── Боллинджер ────────────────────────────────────────────────
        bb_side = self.bb.check_signal(price)
        if bb_side:
            bands = self.bb.last_bands
            if bands:
                await self.pipeline.on_bollinger(
                    bb_side, price, bands["lower"], bands["upper"])

        # ── Перевес ───────────────────────────────────────────────────
        self.dominance.add_trade(ts, side, size)
        dom = self.dominance.check_signal(side_hint=self.pipeline.side)
        if dom:
            await self.pipeline.on_dominance_shift(
                dom["side"], dom["buy_pct"], dom["sell_pct"], price)

    # ── Обработка стакана ────────────────────────────────────────────

    async def _handle_orderbook(self, data: dict):
        try:
            bids = [[float(p), float(s)] for p, s in data.get("bids", [])]
            asks = [[float(p), float(s)] for p, s in data.get("asks", [])]
        except (ValueError, TypeError):
            return

        self.cluster.update(bids, asks)
        cl = self.cluster.check_signal(side_hint=self.pipeline.side)
        if cl:
            await self.pipeline.on_order_cluster(
                cl["side"], cl["price"], cl["volume"])

    # ── Обработка ликвидаций ─────────────────────────────────────────

    async def _handle_liquidation(self, data: dict):
        """Обрабатываем snapshot, ищем признаки ликвидации.

        KuCoin Futures snapshot содержит поля вроде:
          - data.data с информацией об ордере
        Также используем эвристику: очень крупная маркет-сделка
        может быть принудительной ликвидацией.
        """
        try:
            # Попробуем извлечь данные из snapshot
            inner = data if isinstance(data, dict) else {}

            # KuCoin может присылать данные в разном формате
            # Пробуем несколько вариантов
            price = float(inner.get("price", inner.get("markPrice", 0)))
            size = float(inner.get("size", inner.get("qty", 0)))
            side = inner.get("side", "").lower()

            if price <= 0:
                return

            # Оценка в USD (size в контрактах, 1 контракт ≈ 1 USD для BTC)
            size_usd = size * price

            if size_usd >= Config.LIQUIDATION_THRESHOLD_USD:
                logger.info("Liquidation detected: %s $%.0f @ %.2f",
                            side, size_usd, price)
                if side in ("buy", "sell"):
                    await self.pipeline.on_liquidation(side, size_usd, price)

        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Liquidation parse skip: %s", e)

    # ── Запуск ────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=== KuCoin Liquidation Bot started ===")
        logger.info("Symbol: %s | BB(%d, %.1f) | Liq threshold: $%.0f",
                     Config.SYMBOL, Config.BOLLINGER_PERIOD,
                     Config.BOLLINGER_STD, Config.LIQUIDATION_THRESHOLD_USD)

        await self.notifier.send(
            f"🤖 <b>Бот запущен</b>\n"
            f"Пара: {Config.SYMBOL}\n"
            f"Порог ликвидации: ${Config.LIQUIDATION_THRESHOLD_USD:,.0f}\n"
            f"BB({Config.BOLLINGER_PERIOD}, {Config.BOLLINGER_STD})"
        )

        await self.ws.run()


async def main():
    bot = TradingBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        await bot.ws.stop()


if __name__ == "__main__":
    asyncio.run(main())
