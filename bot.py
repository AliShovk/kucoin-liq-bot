"""Главный оркестратор — мульти-символьный конвейер сигналов.

Архитектура:
  1. Через REST получаем ВСЕ активные фьючерсные пары KuCoin
  2. Подписываемся на snapshot (ликвидации) по всем символам
  3. LiquidationCollector собирает ликвидации в пакетное окно (batch)
  4. По окончании окна выбирает КРУПНЕЙШУЮ ликвидацию
  5. Динамически подписываемся на trades/orderbook для этого символа
  6. Запускаем 4-этапный pipeline подтверждений

Этапы:
  🟢       Крупнейшая ликвидация из пакета
  🟢🟢     Боллинджер подтверждает
  🟢🟢🟢   Скопление ордеров подтверждает
  🟢🟢🟢🟢 Перевес объёмов подтверждён
  🎆       Финальный сигнал
"""

import asyncio
import time
import logging
from dataclasses import dataclass, field

from config import Config
from kucoin_ws import KuCoinFuturesWS
from indicators import BollingerBands
from order_cluster import OrderClusterDetector
from dominance import DominanceShiftDetector
from telegram_notifier import TelegramNotifier
from symbol_manager import fetch_all_symbols, symbol_to_display

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bot")

# ── Таймаут сигнального окна (секунды) ──────────────────────────────
SIGNAL_WINDOW = 120  # если все 4 этапа не собрались за 2 мин — сброс


# ═════════════════════════════════════════════════════════════════════
# Пакетный сборщик ликвидаций
# ═════════════════════════════════════════════════════════════════════

@dataclass
class LiqEvent:
    symbol: str
    side: str       # 'buy' | 'sell'
    size_usd: float
    price: float
    ts: float


class LiquidationCollector:
    """Собирает ликвидации в пакетное окно, затем выбирает крупнейшую."""

    def __init__(self, window: float = None):
        self.window = window or Config.LIQUIDATION_BATCH_WINDOW
        self._batch: list[LiqEvent] = []
        self._batch_start: float = 0

    def add(self, event: LiqEvent):
        now = time.time()
        if not self._batch:
            self._batch_start = now
        self._batch.append(event)

    def is_ready(self) -> bool:
        """Пакет готов, если окно истекло и есть события."""
        if not self._batch:
            return False
        return (time.time() - self._batch_start) >= self.window

    def flush(self) -> LiqEvent | None:
        """Вернуть крупнейшую ликвидацию из пакета и очистить."""
        if not self._batch:
            return None
        # Сортируем по size_usd (убывающий)
        best = max(self._batch, key=lambda e: e.size_usd)
        count = len(self._batch)
        total_usd = sum(e.size_usd for e in self._batch)
        logger.info(
            "Batch flush: %d liquidations, total $%.0f → BEST: %s %s $%.0f",
            count, total_usd, best.symbol, best.side, best.size_usd,
        )
        self._batch.clear()
        self._batch_start = 0
        return best

    @property
    def batch_count(self) -> int:
        return len(self._batch)

    @property
    def batch_summary(self) -> str:
        """Краткая сводка текущего пакета."""
        if not self._batch:
            return "пусто"
        symbols = set(e.symbol for e in self._batch)
        total = sum(e.size_usd for e in self._batch)
        return f"{len(self._batch)} liqs | {len(symbols)} symbols | ${total:,.0f}"


# ═════════════════════════════════════════════════════════════════════
# Pipeline подтверждений (per-symbol)
# ═════════════════════════════════════════════════════════════════════

class SignalPipeline:
    """Конечный автомат: собирает 4 подтверждения и выдаёт финальный сигнал."""

    def __init__(self, notifier: TelegramNotifier):
        self.notifier = notifier
        self.symbol: str = ""
        self.display: str = ""
        self.reset()

    def reset(self):
        self.stage = 0
        self.side: str | None = None  # 'buy' | 'sell'
        self.start_time: float = 0
        self.liq_size: float = 0
        self.last_price: float = 0
        self.symbol = ""
        self.display = ""

    def _expired(self) -> bool:
        return self.stage > 0 and (time.time() - self.start_time > SIGNAL_WINDOW)

    @property
    def active(self) -> bool:
        return self.stage > 0 and not self._expired()

    # ── Этап 1: Ликвидация (крупнейшая из пакета) ────────────────────

    async def on_liquidation(self, symbol: str, side: str,
                              size_usd: float, price: float,
                              batch_count: int, batch_summary: str):
        signal_side = "buy" if side == "sell" else "sell"

        self.reset()
        self.stage = 1
        self.symbol = symbol
        self.display = symbol_to_display(symbol)
        self.side = signal_side
        self.start_time = time.time()
        self.liq_size = size_usd
        self.last_price = price

        await self.notifier.stage_1_liquidation(
            self.display, side, size_usd, batch_count, batch_summary)
        logger.info("Stage 1 ✓  %s  side=%s  liq=$%.0f  (batch %d)",
                     symbol, self.side, size_usd, batch_count)

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
        await self.notifier.stage_2_bollinger(
            self.display, bb_side, price, lower, upper)
        logger.info("Stage 2 ✓  %s BB %s @ %.2f", self.symbol, bb_side, price)

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
            self.display, cluster_side, cluster_price, cluster_vol)
        logger.info("Stage 3 ✓  %s cluster %s @ %.2f",
                     self.symbol, cluster_side, cluster_price)

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
            self.display, dom_side, buy_pct, sell_pct)

        # 🎆 Все 4 подтверждения → финальный сигнал
        await self.notifier.stage_final_signal(self.display, self.side, price)
        logger.info("Stage 4 ✓  FINAL SIGNAL  %s %s @ %.2f 🎆",
                     self.symbol, self.side, price)
        self.reset()


# ═════════════════════════════════════════════════════════════════════
# Главное приложение
# ═════════════════════════════════════════════════════════════════════

class TradingBot:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.notifier = TelegramNotifier()
        self.pipeline = SignalPipeline(self.notifier)
        self.collector = LiquidationCollector()

        # Индикаторы (создаются заново при фокусе на новый символ)
        self.bb = BollingerBands()
        self.cluster = OrderClusterDetector()
        self.dominance = DominanceShiftDetector()

        self.ws = KuCoinFuturesWS(symbols)
        self._last_price: float = 0

        # Регистрируем callback-и
        self.ws.on_trade(self._handle_trade)
        self.ws.on_orderbook(self._handle_orderbook)
        self.ws.on_liquidation(self._handle_liquidation)

    def _reset_indicators(self):
        """Сбросить индикаторы при переключении символа."""
        self.bb = BollingerBands()
        self.cluster = OrderClusterDetector()
        self.dominance = DominanceShiftDetector()

    # ── Обработка сделок (для целевого символа) ──────────────────────

    async def _handle_trade(self, data: dict):
        try:
            price = float(data.get("price", 0))
            size = float(data.get("size", 0))
            side = data.get("side", "buy").lower()
            ts = float(data.get("ts", time.time() * 1e9)) / 1e9
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

    # ── Обработка стакана (для целевого символа) ─────────────────────

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

    # ── Обработка ликвидаций (все символы) ───────────────────────────

    async def _handle_liquidation(self, data: dict):
        try:
            inner = data if isinstance(data, dict) else {}
            symbol = inner.get("_symbol", "")
            price = float(inner.get("price", inner.get("markPrice", 0)))
            size = float(inner.get("size", inner.get("qty", 0)))
            side = inner.get("side", "").lower()

            if price <= 0 or side not in ("buy", "sell"):
                return

            size_usd = size * price

            if size_usd >= Config.LIQUIDATION_THRESHOLD_USD:
                event = LiqEvent(
                    symbol=symbol, side=side,
                    size_usd=size_usd, price=price, ts=time.time(),
                )
                self.collector.add(event)
                logger.info("Liq queued: %s %s $%.0f @ %.2f [batch: %s]",
                            symbol, side, size_usd, price,
                            self.collector.batch_summary)

                # Проверяем, готов ли пакет
                await self._try_flush_batch()

        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Liquidation parse skip: %s", e)

    async def _try_flush_batch(self):
        """Если пакетное окно истекло — выбрать лучшую ликвидацию и запустить pipeline."""
        if not self.collector.is_ready():
            return

        # Не прерываем активный pipeline
        if self.pipeline.active:
            logger.debug("Pipeline active, skipping batch flush")
            self.collector.flush()  # сбрасываем чтобы не копить
            return

        batch_count = self.collector.batch_count
        batch_summary = self.collector.batch_summary
        best = self.collector.flush()
        if not best:
            return

        # Переключаемся на символ с крупнейшей ликвидацией
        logger.info("🎯 Focusing on %s (biggest liq $%.0f)",
                     best.symbol, best.size_usd)
        self._reset_indicators()
        await self.ws.focus_symbol(best.symbol)

        # Запускаем pipeline — этап 1
        await self.pipeline.on_liquidation(
            best.symbol, best.side, best.size_usd, best.price,
            batch_count, batch_summary,
        )

    # ── Периодическая проверка пакета ────────────────────────────────

    async def _batch_checker(self):
        """Фоновая задача: проверяет готовность пакета каждую секунду."""
        while True:
            await asyncio.sleep(1)
            try:
                await self._try_flush_batch()
            except Exception as e:
                logger.error("Batch checker error: %s", e)

    # ── Запуск ────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=== KuCoin Liquidation Bot started ===")
        logger.info("Monitoring %d symbols | BB(%d, %.1f) | Liq threshold: $%.0f | Batch window: %ds",
                     len(self.symbols), Config.BOLLINGER_PERIOD,
                     Config.BOLLINGER_STD, Config.LIQUIDATION_THRESHOLD_USD,
                     Config.LIQUIDATION_BATCH_WINDOW)

        await self.notifier.send(
            f"🤖 <b>Бот запущен</b>\n"
            f"Мониторинг: <b>{len(self.symbols)} пар</b>\n"
            f"Порог ликвидации: ${Config.LIQUIDATION_THRESHOLD_USD:,.0f}\n"
            f"Пакетное окно: {Config.LIQUIDATION_BATCH_WINDOW}с\n"
            f"BB({Config.BOLLINGER_PERIOD}, {Config.BOLLINGER_STD})"
        )

        # Запускаем WS и batch-checker параллельно
        await asyncio.gather(
            self.ws.run(),
            self._batch_checker(),
        )


async def main():
    # Получаем все активные фьючерсные символы
    logger.info("Fetching active KuCoin Futures symbols...")
    symbols = await fetch_all_symbols()
    if not symbols:
        logger.error("No symbols found! Check internet connection.")
        return

    logger.info("Found %d symbols: %s ...", len(symbols), ", ".join(symbols[:10]))

    bot = TradingBot(symbols)
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Shutting down…")
        await bot.ws.stop()


if __name__ == "__main__":
    asyncio.run(main())
