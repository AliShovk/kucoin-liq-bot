"""Главный оркестратор — мульти-символьный конвейер сигналов (Bybit).

Архитектура:
  1. Через REST получаем ВСЕ активные linear пары Bybit
  2. Подписываемся на allLiquidation для всех символов
  3. Агрегируем ликвидации per-symbol за окно (как в Node.js)
  4. Когда суммарный объём по символу превышает порог — стартуем pipeline
  5. Динамически подписываемся на trades/orderbook для этого символа
  6. Запускаем 4-этапный pipeline подтверждений

Этапы:
  🟢       Агрегированная ликвидация превысила порог
  🟢🟢     Боллинджер подтверждает
  🟢🟢🟢   Скопление ордеров подтверждает
  🟢🟢🟢🟢 Перевес объёмов подтверждён
  🎆       Финальный сигнал
"""

import asyncio
import time
import logging

from config import Config
from bybit_ws import BybitWS
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
# Агрегатор ликвидаций (по аналогии с Node.js кодом)
# ═════════════════════════════════════════════════════════════════════

class LiquidationAggregator:
    """Агрегирует ликвидации per-symbol за скользящее окно.

    Логика из Node.js кода:
      - Каждая ликвидация >= min_size попадает в аккумулятор
      - По таймеру (aggregation_interval) проверяем суммы
      - Если long_sum >= long_threshold → LONG SPIKE
      - Если short_sum >= short_threshold → SHORT SPIKE
      - После проверки обнуляем аккумулятор
    """

    def __init__(self):
        self.min_size = Config.BYBIT_MIN_SIZE
        self.long_threshold = Config.BYBIT_LONG_THRESHOLD
        self.short_threshold = Config.BYBIT_SHORT_THRESHOLD
        # {symbol: {"long": float, "short": float}}
        self._agg: dict[str, dict[str, float]] = {}
        self._sent_signals: set[str] = set()

    def add(self, symbol: str, side: str, size_usd: float):
        """Добавить ликвидацию. side = 'Buy' | 'Sell'."""
        if size_usd < self.min_size:
            return
        if symbol not in self._agg:
            self._agg[symbol] = {"long": 0.0, "short": 0.0}
        if side == "Buy":
            self._agg[symbol]["long"] += size_usd
        else:
            self._agg[symbol]["short"] += size_usd

    def flush(self) -> list[dict]:
        """Проверить пороги и вернуть список сработавших сигналов.

        Возвращает: [{"symbol": str, "side": "Buy"|"Sell", "amount": float}, ...]
        """
        signals = []
        for symbol, data in self._agg.items():
            long_amt = data["long"]
            short_amt = data["short"]

            long_key = f"{symbol}-LONG-{int(long_amt)}"
            short_key = f"{symbol}-SHORT-{int(short_amt)}"

            if long_amt >= self.long_threshold and long_key not in self._sent_signals:
                self._sent_signals.add(long_key)
                signals.append({"symbol": symbol, "side": "Buy", "amount": long_amt})
                logger.info("LONG spike: %s $%.0f", symbol, long_amt)

            if short_amt >= self.short_threshold and short_key not in self._sent_signals:
                self._sent_signals.add(short_key)
                signals.append({"symbol": symbol, "side": "Sell", "amount": short_amt})
                logger.info("SHORT spike: %s $%.0f", symbol, short_amt)

            # Обнулить после проверки
            data["long"] = 0.0
            data["short"] = 0.0

        return signals

    def clear_cache(self):
        """Очистить кеш отправленных сигналов (раз в 10 мин)."""
        self._sent_signals.clear()
        logger.info("Signal cache cleared")


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
        self.liq_amount: float = 0
        self.last_price: float = 0
        self.symbol = ""
        self.display = ""

    def _expired(self) -> bool:
        return self.stage > 0 and (time.time() - self.start_time > SIGNAL_WINDOW)

    @property
    def active(self) -> bool:
        return self.stage > 0 and not self._expired()

    # ── Этап 1: Ликвидация (агрегированный спайк) ────────────────────

    async def on_liquidation(self, symbol: str, side: str, amount: float):
        # Buy-ликвидация (лонги ликвидированы) → ожидаем разворот вверх (buy)
        # Sell-ликвидация (шорты ликвидированы) → ожидаем разворот вниз (sell)
        signal_side = "buy" if side == "Buy" else "sell"

        self.reset()
        self.stage = 1
        self.symbol = symbol
        self.display = symbol_to_display(symbol)
        self.side = signal_side
        self.start_time = time.time()
        self.liq_amount = amount

        await self.notifier.stage_1_liquidation(
            self.display, side.lower(), amount)
        logger.info("Stage 1 ✓  %s  signal_side=%s  liq=$%.0f",
                     symbol, self.side, amount)

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
        self.aggregator = LiquidationAggregator()

        # Индикаторы (создаются заново при фокусе на новый символ)
        self.bb = BollingerBands()
        self.cluster = OrderClusterDetector()
        self.dominance = DominanceShiftDetector()

        self.ws = BybitWS(symbols)
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
        """Bybit publicTrade: {s, S: 'Buy'|'Sell', v: size, p: price, T: ts_ms}."""
        try:
            price = float(data.get("p", 0))
            size = float(data.get("v", 0))
            side = data.get("S", "Buy").lower()  # 'buy' | 'sell'
            ts = float(data.get("T", time.time() * 1000)) / 1000
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
        self.dominance.add_trade(ts, side, size * price)
        dom = self.dominance.check_signal(side_hint=self.pipeline.side)
        if dom:
            await self.pipeline.on_dominance_shift(
                dom["side"], dom["buy_pct"], dom["sell_pct"], price)

    # ── Обработка стакана (для целевого символа) ─────────────────────

    async def _handle_orderbook(self, data: dict):
        """Bybit orderbook: {s, b: [[price, size]], a: [[price, size]]}."""
        try:
            bids = [[float(p), float(s)] for p, s in data.get("b", [])]
            asks = [[float(p), float(s)] for p, s in data.get("a", [])]
        except (ValueError, TypeError):
            return

        self.cluster.update(bids, asks)
        cl = self.cluster.check_signal(side_hint=self.pipeline.side)
        if cl:
            await self.pipeline.on_order_cluster(
                cl["side"], cl["price"], cl["volume"])

    # ── Обработка ликвидаций (все символы) ───────────────────────────

    async def _handle_liquidation(self, data: dict):
        """Bybit allLiquidation: {s: symbol, S: 'Buy'|'Sell', v: size_str, p: price_str}."""
        try:
            symbol = data.get("s", "")
            side = data.get("S", "")  # 'Buy' | 'Sell'
            size = float(data.get("v", 0))
            price = float(data.get("p", 0))

            if not symbol or not side or price <= 0:
                return

            size_usd = size  # Bybit v уже в USD для linear
            self.aggregator.add(symbol, side, size_usd)

        except (ValueError, TypeError, KeyError) as e:
            logger.debug("Liquidation parse skip: %s", e)

    # ── Периодический flush агрегатора ────────────────────────────────

    async def _aggregation_loop(self):
        """Каждые N секунд проверяем агрегированные ликвидации."""
        interval = Config.BYBIT_AGGREGATION_INTERVAL
        while True:
            await asyncio.sleep(interval)
            try:
                signals = self.aggregator.flush()
                if not signals:
                    continue

                # Выбираем самый крупный сигнал
                best = max(signals, key=lambda s: s["amount"])

                # Не прерываем активный pipeline
                if self.pipeline.active:
                    logger.debug("Pipeline active, skipping aggregation signal")
                    continue

                logger.info("🎯 Biggest liquidation spike: %s %s $%.0f",
                             best["symbol"], best["side"], best["amount"])

                # Сброс индикаторов и фокус на новый символ
                self._reset_indicators()
                await self.ws.focus_symbol(best["symbol"])

                # Запускаем pipeline — этап 1
                await self.pipeline.on_liquidation(
                    best["symbol"], best["side"], best["amount"])

            except Exception as e:
                logger.error("Aggregation loop error: %s", e)

    async def _cache_cleaner(self):
        """Раз в 10 минут очищаем кеш отправленных сигналов."""
        while True:
            await asyncio.sleep(10 * 60)
            self.aggregator.clear_cache()

    # ── Запуск ────────────────────────────────────────────────────────

    async def run(self):
        logger.info("=== Bybit Liquidation Bot started ===")
        logger.info("Monitoring %d symbols | BB(%d, %.1f) | "
                     "LONG threshold: $%.0f | SHORT threshold: $%.0f | "
                     "Aggregation: %ds | Min size: $%.0f",
                     len(self.symbols), Config.BOLLINGER_PERIOD,
                     Config.BOLLINGER_STD, Config.BYBIT_LONG_THRESHOLD,
                     Config.BYBIT_SHORT_THRESHOLD,
                     Config.BYBIT_AGGREGATION_INTERVAL, Config.BYBIT_MIN_SIZE)

        await self.notifier.send(
            f"📡 <b>Liquidation Bot запущен</b>\n"
            f"Биржа: Bybit (все символы)\n"
            f"Мониторинг: <b>{len(self.symbols)} пар</b>\n"
            f"LONG порог: ${Config.BYBIT_LONG_THRESHOLD:,.0f}\n"
            f"SHORT порог: ${Config.BYBIT_SHORT_THRESHOLD:,.0f}\n"
            f"Агрегация: {Config.BYBIT_AGGREGATION_INTERVAL}с\n"
            f"BB({Config.BOLLINGER_PERIOD}, {Config.BOLLINGER_STD})"
        )

        # Запускаем WS, aggregation loop и cache cleaner параллельно
        await asyncio.gather(
            self.ws.run(),
            self._aggregation_loop(),
            self._cache_cleaner(),
        )


async def main():
    logger.info("Fetching active Bybit linear symbols...")
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
