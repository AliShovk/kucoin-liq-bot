"""KuCoin Futures REST API — открытие/закрытие позиций с TP/SL.

Использует ccxt для упрощения работы с REST API.
Маппинг символов: Bybit BTCUSDT → KuCoin BTCUSDTM (добавляем 'M').

Автоматическое наращивание размера позиции:
  - Ведём историю сделок (wins / losses)
  - Если winrate >= порога — увеличиваем базовый размер на N%
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import ccxt.async_support as ccxt

from config import Config

logger = logging.getLogger(__name__)

TRADES_LOG = Path(__file__).parent / "trades_history.json"


# ═════════════════════════════════════════════════════════════════════
# Маппинг символов Bybit → KuCoin
# ═════════════════════════════════════════════════════════════════════

def bybit_to_kucoin_symbol(bybit_symbol: str) -> str:
    """BTCUSDT → BTC/USDT:USDT (ccxt unified format for KuCoin futures)."""
    if bybit_symbol.endswith("USDT"):
        base = bybit_symbol[:-4]
        return f"{base}/USDT:USDT"
    return bybit_symbol


# ═════════════════════════════════════════════════════════════════════
# Трекер сделок — винрейт + автонаращивание
# ═════════════════════════════════════════════════════════════════════

class TradeTracker:
    """Отслеживает историю сделок и автоматически масштабирует размер."""

    def __init__(self):
        self.base_size: float = Config.TRADE_SIZE_USDT
        self.current_size: float = Config.TRADE_SIZE_USDT
        self.winrate_threshold: float = Config.TRADE_WINRATE_THRESHOLD
        self.scale_pct: float = Config.TRADE_SIZE_SCALE_PCT
        self.min_trades: int = Config.TRADE_MIN_TRADES_FOR_SCALE
        self.trades: list[dict] = []
        self._load()

    def _load(self):
        """Загрузить историю из файла."""
        try:
            if TRADES_LOG.exists():
                data = json.loads(TRADES_LOG.read_text(encoding="utf-8"))
                self.trades = data.get("trades", [])
                self.current_size = data.get("current_size", self.base_size)
                logger.info("Loaded %d trades, current size=$%.2f",
                            len(self.trades), self.current_size)
        except Exception as e:
            logger.warning("Could not load trades history: %s", e)

    def _save(self):
        """Сохранить историю в файл."""
        try:
            TRADES_LOG.write_text(json.dumps({
                "trades": self.trades[-200:],  # последние 200
                "current_size": self.current_size,
            }, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Could not save trades history: %s", e)

    @property
    def total(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.get("pnl", 0) > 0)

    @property
    def winrate(self) -> float:
        if self.total == 0:
            return 0.0
        return (self.wins / self.total) * 100

    def record_trade(self, symbol: str, side: str, entry: float,
                     exit_price: float, pnl: float, reason: str):
        """Записать результат сделки."""
        self.trades.append({
            "symbol": symbol,
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "pnl": pnl,
            "reason": reason,  # 'tp' | 'sl' | 'manual'
            "ts": time.time(),
        })
        self._recalc_size()
        self._save()
        logger.info("Trade recorded: %s %s pnl=%.2f | winrate=%.1f%% (%d/%d) | size=$%.2f",
                     symbol, side, pnl, self.winrate, self.wins, self.total,
                     self.current_size)

    def _recalc_size(self):
        """Пересчитать размер позиции на основе винрейта."""
        if self.total < self.min_trades:
            return
        if self.winrate >= self.winrate_threshold:
            # Наращиваем на scale_pct от базового размера
            growth = self.base_size * (self.scale_pct / 100)
            old = self.current_size
            self.current_size += growth
            logger.info("Winrate %.1f%% >= %.1f%% → size $%.2f → $%.2f (+$%.2f)",
                         self.winrate, self.winrate_threshold,
                         old, self.current_size, growth)

    def get_size(self) -> float:
        """Текущий размер позиции в USDT."""
        return self.current_size

    def summary(self) -> str:
        """Строка-сводка для TG."""
        return (f"Сделок: {self.total} | "
                f"Win: {self.wins} | "
                f"Winrate: {self.winrate:.1f}% | "
                f"Размер: ${self.current_size:.2f}")


# ═════════════════════════════════════════════════════════════════════
# KuCoin Futures Trader
# ═════════════════════════════════════════════════════════════════════

class KuCoinTrader:
    """Открывает позиции на KuCoin Futures через ccxt."""

    def __init__(self):
        self.enabled = Config.AUTOTRADE_ENABLED
        self.leverage = Config.TRADE_LEVERAGE
        self.sl_pct = Config.TRADE_SL_PCT
        self.tp_pct = Config.TRADE_TP_PCT
        self.tracker = TradeTracker()
        self.active_trades: dict[str, dict] = {}
        self._exchange: ccxt.kucoinfutures | None = None

        if self.enabled:
            if not all([Config.KUCOIN_API_KEY, Config.KUCOIN_API_SECRET,
                        Config.KUCOIN_API_PASSPHRASE]):
                logger.error("AUTOTRADE_ENABLED=true but KuCoin API keys are missing!")
                self.enabled = False
            else:
                self._exchange = ccxt.kucoinfutures({
                    "apiKey": Config.KUCOIN_API_KEY,
                    "secret": Config.KUCOIN_API_SECRET,
                    "password": Config.KUCOIN_API_PASSPHRASE,
                    "enableRateLimit": True,
                })
                logger.info("KuCoin Futures trader initialized (leverage=%dx)",
                            self.leverage)
        else:
            logger.info("Autotrade DISABLED — signals only")

    async def close(self):
        """Закрыть ccxt exchange."""
        if self._exchange:
            await self._exchange.close()

    async def execute_signal(self, bybit_symbol: str, side: str,
                             entry_price: float) -> dict | None:
        """Открыть позицию на KuCoin Futures по финальному сигналу.

        Args:
            bybit_symbol: символ Bybit (BTCUSDT)
            side: 'buy' | 'sell'
            entry_price: текущая цена

        Returns:
            dict с деталями ордера или None
        """
        if not self.enabled or not self._exchange:
            logger.info("Trade skipped (autotrade disabled): %s %s @ %.2f",
                         bybit_symbol, side, entry_price)
            return None

        kucoin_symbol = bybit_to_kucoin_symbol(bybit_symbol)
        size_usdt = self.tracker.get_size()

        try:
            # Загружаем рынки если ещё не загружены
            if not self._exchange.markets:
                await self._exchange.load_markets()

            # Проверяем что символ существует
            if kucoin_symbol not in self._exchange.markets:
                logger.warning("Symbol %s not found on KuCoin Futures", kucoin_symbol)
                return None

            market = self._exchange.markets[kucoin_symbol]

            # Устанавливаем плечо
            try:
                await self._exchange.set_leverage(self.leverage, kucoin_symbol)
            except Exception as e:
                logger.warning("Set leverage warning: %s", e)

            # Рассчитываем количество контрактов
            contract_size = market.get("contractSize", 1)
            if contract_size and contract_size > 0:
                amount = size_usdt / (entry_price * contract_size)
            else:
                amount = size_usdt / entry_price

            # Округляем до допустимого лота
            amount = self._exchange.amount_to_precision(kucoin_symbol, amount)
            amount = float(amount)

            if amount <= 0:
                logger.warning("Calculated amount is 0 for %s", kucoin_symbol)
                return None

            # Рассчитываем TP/SL цены
            if side == "buy":
                tp_price = entry_price * (1 + self.tp_pct / 100)
                sl_price = entry_price * (1 - self.sl_pct / 100)
            else:
                tp_price = entry_price * (1 - self.tp_pct / 100)
                sl_price = entry_price * (1 + self.sl_pct / 100)

            # Открываем market ордер
            logger.info("Opening %s %s: amount=%.4f price=~%.2f size=$%.2f "
                         "TP=%.2f SL=%.2f leverage=%dx",
                         side.upper(), kucoin_symbol, amount, entry_price,
                         size_usdt, tp_price, sl_price, self.leverage)

            order = await self._exchange.create_order(
                symbol=kucoin_symbol,
                type="market",
                side=side,
                amount=amount,
                params={"leverage": self.leverage},
            )

            order_id = order.get("id", "?")
            logger.info("Order placed: %s (id=%s)", kucoin_symbol, order_id)

            # Выставляем TP ордер
            try:
                tp_side = "sell" if side == "buy" else "buy"
                await self._exchange.create_order(
                    symbol=kucoin_symbol,
                    type="limit",
                    side=tp_side,
                    amount=amount,
                    price=self._exchange.price_to_precision(kucoin_symbol, tp_price),
                    params={
                        "reduceOnly": True,
                    },
                )
                logger.info("TP order set at %.2f", tp_price)
            except Exception as e:
                logger.error("Failed to set TP: %s", e)

            # Выставляем SL ордер (stop-market)
            try:
                sl_side = "sell" if side == "buy" else "buy"
                await self._exchange.create_order(
                    symbol=kucoin_symbol,
                    type="market",
                    side=sl_side,
                    amount=amount,
                    params={
                        "reduceOnly": True,
                        "stopLossPrice": float(
                            self._exchange.price_to_precision(kucoin_symbol, sl_price)),
                    },
                )
                logger.info("SL order set at %.2f", sl_price)
            except Exception as e:
                logger.error("Failed to set SL: %s", e)

            trade_info = {
                "order_id": order_id,
                "symbol": kucoin_symbol,
                "bybit_symbol": bybit_symbol,
                "side": side,
                "amount": amount,
                "entry_price": entry_price,
                "tp_price": tp_price,
                "sl_price": sl_price,
                "size_usdt": size_usdt,
                "opened_at": time.time(),
            }
            self.active_trades[kucoin_symbol] = trade_info
            return trade_info

        except Exception as e:
            logger.error("Trade execution failed for %s: %s", kucoin_symbol, e)
            return None

    async def poll_updates(self) -> list[dict]:
        """Проверить активные позиции и вернуть события закрытия."""
        if not self.enabled or not self._exchange or not self.active_trades:
            return []

        events: list[dict] = []
        for symbol, trade in list(self.active_trades.items()):
            try:
                positions = await self._exchange.fetch_positions([symbol])
                position_open = False
                for pos in positions or []:
                    contracts = pos.get("contracts")
                    if contracts is None:
                        contracts = pos.get("info", {}).get("currentQty")
                    try:
                        if abs(float(contracts or 0)) > 0:
                            position_open = True
                            break
                    except (TypeError, ValueError):
                        continue

                if position_open:
                    continue

                ticker = await self._exchange.fetch_ticker(symbol)
                exit_price = float(ticker.get("last") or ticker.get("close") or trade["entry_price"])

                if trade["side"] == "buy":
                    pnl_pct = ((exit_price - trade["entry_price"]) / trade["entry_price"]) * 100
                    if exit_price >= trade["tp_price"]:
                        reason = "tp"
                    elif exit_price <= trade["sl_price"]:
                        reason = "sl"
                    else:
                        reason = "manual"
                else:
                    pnl_pct = ((trade["entry_price"] - exit_price) / trade["entry_price"]) * 100
                    if exit_price <= trade["tp_price"]:
                        reason = "tp"
                    elif exit_price >= trade["sl_price"]:
                        reason = "sl"
                    else:
                        reason = "manual"

                old_size = self.tracker.get_size()
                self.tracker.record_trade(
                    trade["bybit_symbol"],
                    trade["side"],
                    trade["entry_price"],
                    exit_price,
                    pnl_pct,
                    reason,
                )
                new_size = self.tracker.get_size()

                events.append({
                    "type": "trade_closed",
                    "symbol": trade["symbol"],
                    "side": trade["side"],
                    "exit_price": exit_price,
                    "pnl_pct": pnl_pct,
                    "reason": reason,
                    "summary": self.trade_summary_text(),
                })

                if new_size > old_size:
                    events.append({
                        "type": "size_scaled",
                        "old_size": old_size,
                        "new_size": new_size,
                        "winrate": self.tracker.winrate,
                    })

                self.active_trades.pop(symbol, None)

            except Exception as e:
                logger.error("Trade monitor error for %s: %s", symbol, e)

        return events

    def trade_summary_text(self) -> str:
        """Текст для TG — сводка по трейдам."""
        return self.tracker.summary()
