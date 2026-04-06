"""Главный оркестратор — мульти-символьный конвейер сигналов (Bybit).

Архитектура:
  1. Через REST получаем ВСЕ активные linear пары Bybit
  2. Подписываемся на allLiquidation для всех символов
  3. Агрегируем ликвидации per-symbol за окно (как в Node.js)
  4. Когда суммарный объём по символу превышает порог — стартуем pipeline
  5. Динамически подписываемся на trades/orderbook для этого символа
  6. Запускаем 4-этапный pipeline подтверждений

Этапы:
  🟢         Агрегированная ликвидация превысила порог
  🟢🟢       Price Reclaim — цена вернулась через экстремум выноса
  🟢🟢🟢     Боллинджер подтверждает
  🟢🟢🟢🟢   Скопление ордеров подтверждает
  🟢🟢🟢🟢🟢 Перевес объёмов подтверждён
  🎆         Финальный сигнал
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
from kucoin_trader import KuCoinTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("bot")

# ── Таймаут сигнального окна (секунды) ──────────────────────────────
SIGNAL_WINDOW = 120  # если все 5 этапов не собрались за 2 мин — сброс


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
        self._events_in_window = 0

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
        self._events_in_window += 1

    def flush(self) -> list[dict]:
        """Проверить пороги и вернуть список сработавших сигналов.

        Возвращает: [{"symbol": str, "side": "Buy"|"Sell", "amount": float}, ...]
        """
        signals = []
        ranked = []
        for symbol, data in self._agg.items():
            long_amt = data["long"]
            short_amt = data["short"]
            top_amt = max(long_amt, short_amt)
            if top_amt > 0:
                ranked.append((symbol, long_amt, short_amt, top_amt))

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

        ranked.sort(key=lambda item: item[3], reverse=True)
        if self._events_in_window > 0:
            top_preview = ", ".join(
                f"{symbol}:L=${long_amt:,.0f}/S=${short_amt:,.0f}"
                for symbol, long_amt, short_amt, _ in ranked[:5]
            )
            logger.info(
                "Aggregation flush: events=%d symbols=%d top=%s signals=%d",
                self._events_in_window,
                len(ranked),
                top_preview or "none",
                len(signals),
            )
        self._events_in_window = 0

        return signals

    def clear_cache(self):
        """Очистить кеш отправленных сигналов (раз в 10 мин)."""
        self._sent_signals.clear()
        logger.info("Signal cache cleared")


# ═════════════════════════════════════════════════════════════════════
# Pipeline подтверждений (per-symbol)
# ═════════════════════════════════════════════════════════════════════

class SignalPipeline:
    """Конечный автомат: собирает 5 подтверждений и выдаёт финальный сигнал."""

    def __init__(self, notifier: TelegramNotifier, on_final_signal=None):
        self.notifier = notifier
        self.symbol: str = ""
        self.display: str = ""
        self.reclaim_pct: float = Config.RECLAIM_PCT
        self._on_final = on_final_signal  # async callback(symbol, side, price)
        self.reset()

    def reset(self):
        self.stage = 0
        self.side: str | None = None  # 'buy' | 'sell'
        self.start_time: float = 0
        self.liq_amount: float = 0
        self.last_price: float = 0
        self.symbol = ""
        self.display = ""
        # Price Reclaim tracking
        self.extreme_price: float = 0  # экстремум после выноса

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

    # ── Этап 2: Price Reclaim ──────────────────────────────────────────

    def update_extreme(self, price: float):
        """Обновить экстремум выноса (вызывается на каждом тике после Stage 1)."""
        if self.stage != 1:
            return
        if self.side == "buy" and (self.extreme_price == 0 or price < self.extreme_price):
            self.extreme_price = price
        elif self.side == "sell" and (self.extreme_price == 0 or price > self.extreme_price):
            self.extreme_price = price

    def check_reclaim(self, price: float) -> bool:
        """Проверить, вернулась ли цена через экстремум выноса."""
        if self.stage != 1 or self.extreme_price == 0:
            return False
        if self.side == "buy":
            return price > self.extreme_price * (1 + self.reclaim_pct)
        else:
            return price < self.extreme_price * (1 - self.reclaim_pct)

    async def on_reclaim(self, price: float):
        if self._expired():
            self.reset()
            return
        if self.stage != 1:
            return

        self.stage = 2
        self.last_price = price
        await self.notifier.stage_2_reclaim(
            self.display, self.side, self.extreme_price, price)
        logger.info("Stage 2 ✓  %s RECLAIM extreme=%.2f now=%.2f",
                     self.symbol, self.extreme_price, price)

    # ── Этап 3: Боллинджер ───────────────────────────────────────────

    async def on_bollinger(self, bb_side: str, price: float,
                            lower: float, upper: float):
        if self._expired():
            self.reset()
            return
        if self.stage != 2:
            return
        if bb_side != self.side:
            return

        self.stage = 3
        self.last_price = price
        await self.notifier.stage_3_bollinger(
            self.display, bb_side, price, lower, upper)
        logger.info("Stage 3 ✓  %s BB %s @ %.2f", self.symbol, bb_side, price)

    # ── Этап 4: Скопление ордеров ────────────────────────────────────

    async def on_order_cluster(self, cluster_side: str, cluster_price: float,
                                cluster_vol: float):
        if self._expired():
            self.reset()
            return
        if self.stage != 3:
            return
        if cluster_side != self.side:
            return

        self.stage = 4
        await self.notifier.stage_4_order_cluster(
            self.display, cluster_side, cluster_price, cluster_vol)
        logger.info("Stage 4 ✓  %s cluster %s @ %.2f",
                     self.symbol, cluster_side, cluster_price)

    # ── Этап 5: Перевес ──────────────────────────────────────────────

    async def on_dominance_shift(self, dom_side: str, buy_pct: float,
                                  sell_pct: float, price: float):
        if self._expired():
            self.reset()
            return
        if self.stage != 4:
            return
        if dom_side != self.side:
            return

        self.stage = 5
        self.last_price = price
        await self.notifier.stage_5_dominance_shift(
            self.display, dom_side, buy_pct, sell_pct)

        # 🎆 Все 5 подтверждений → финальный сигнал
        await self.notifier.stage_final_signal(self.display, self.side, price)
        logger.info("Stage 5 ✓  FINAL SIGNAL  %s %s @ %.2f 🎆",
                     self.symbol, self.side, price)

        # Вызываем callback для исполнения сделки
        if self._on_final:
            try:
                await self._on_final(self.symbol, self.side, price)
            except Exception as e:
                logger.error("Final signal callback error: %s", e)

        self.reset()


# ═════════════════════════════════════════════════════════════════════
# Главное приложение
# ═════════════════════════════════════════════════════════════════════

class TradingBot:
    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.notifier = TelegramNotifier()
        self.trade_notifier = TelegramNotifier(use_trade_channel=True)
        self.trader = KuCoinTrader()
        self.pipeline = SignalPipeline(
            self.notifier, on_final_signal=self._execute_trade)
        self.aggregator = LiquidationAggregator()
        self._liq_debug_seen = 0

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

    # ── Исполнение сделки на KuCoin Futures ────────────────────────

    async def _execute_trade(self, symbol: str, side: str, price: float):
        """Callback из pipeline: финальный сигнал → открытие позиции."""
        result = await self.trader.execute_signal(symbol, side, price)
        if result:
            await self.trade_notifier.trade_opened(
                result["symbol"],
                side,
                result["size_usdt"],
                self.trader.leverage,
                result["entry_price"],
                result["tp_price"],
                result["sl_price"],
            )
        elif self.trader.enabled:
            await self.trade_notifier.trade_open_failed(
                symbol,
                side,
                price,
                "order rejected / API error",
            )

    async def _trade_monitor_loop(self):
        while True:
            await asyncio.sleep(Config.TRADE_MONITOR_INTERVAL)
            try:
                events = await self.trader.poll_updates()
                for event in events:
                    if event["type"] == "trade_closed":
                        await self.trade_notifier.trade_closed(
                            event["symbol"],
                            event["side"],
                            event["exit_price"],
                            event["pnl_pct"],
                            event["reason"],
                            event["summary"],
                        )
                    elif event["type"] == "size_scaled":
                        await self.trade_notifier.trade_size_scaled(
                            event["old_size"],
                            event["new_size"],
                            event["winrate"],
                        )
            except Exception as e:
                logger.error("Trade monitor loop error: %s", e)

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

        # ── Price Reclaim (Stage 1 → 2) ─────────────────────────────
        if self.pipeline.stage == 1:
            self.pipeline.update_extreme(price)
            if self.pipeline.check_reclaim(price):
                await self.pipeline.on_reclaim(price)

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

            size_usd = size * price  # v = кол-во монет, переводим в USD
            if self._liq_debug_seen < 10:
                logger.info(
                    "Liq sample %d: symbol=%s side=%s v=%s p=%s usd=%.0f",
                    self._liq_debug_seen + 1,
                    symbol,
                    side,
                    data.get("v"),
                    data.get("p"),
                    size_usd,
                )
                self._liq_debug_seen += 1
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

        trade_status = (
            f"✅ Автоторговля: KuCoin Futures\n"
            f"Плечо: {Config.TRADE_LEVERAGE}x | "
            f"Размер: ${self.trader.tracker.get_size():.2f}\n"
            f"TP: {Config.TRADE_TP_PCT}% | SL: {Config.TRADE_SL_PCT}%\n"
            f"📊 {self.trader.trade_summary_text()}"
        ) if Config.AUTOTRADE_ENABLED else "❌ Автоторговля выключена (только сигналы)"

        await self.notifier.send(
            f"📡 <b>Liquidation Bot запущен</b>\n"
            f"Биржа: Bybit (все символы)\n"
            f"Мониторинг: <b>{len(self.symbols)} пар</b>\n"
            f"LONG порог: ${Config.BYBIT_LONG_THRESHOLD:,.0f}\n"
            f"SHORT порог: ${Config.BYBIT_SHORT_THRESHOLD:,.0f}\n"
            f"Агрегация: {Config.BYBIT_AGGREGATION_INTERVAL}с\n"
            f"BB({Config.BOLLINGER_PERIOD}, {Config.BOLLINGER_STD})\n"
            f"───────────────\n"
            f"{trade_status}"
        )

        # Запускаем WS, aggregation loop и cache cleaner параллельно
        await asyncio.gather(
            self.ws.run(),
            self._aggregation_loop(),
            self._cache_cleaner(),
            self._trade_monitor_loop(),
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
        await bot.trader.close()


if __name__ == "__main__":
    asyncio.run(main())
