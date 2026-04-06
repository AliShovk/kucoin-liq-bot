"""Bybit V5 WebSocket клиент — мульти-символ.

Подключается к wss://stream.bybit.com/v5/public/linear
Подписывается на:
  - allLiquidation.{symbol} — ликвидации для ВСЕХ символов
  - publicTrade.{symbol}    — лента сделок (динамически, для целевого символа)
  - orderbook.50.{symbol}   — стакан L2 50 уровней (динамически, для целевого)
"""

import asyncio
import json
import time
import logging
from typing import Callable, Awaitable

import aiohttp

logger = logging.getLogger(__name__)

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
MAX_ARGS_PER_SUB = 10  # Bybit позволяет до 10 args за одну подписку


class BybitWS:
    """Асинхронный WS-клиент для Bybit Linear Perpetuals."""

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self._on_liquidation: Callable | None = None
        self._on_trade: Callable | None = None
        self._on_orderbook: Callable | None = None
        self._ws = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._active_target: str | None = None
        self._ping_interval = 20

    async def _cleanup_connection(self):
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ── Регистрация callback-ов ──────────────────────────────────────

    def on_liquidation(self, fn: Callable[..., Awaitable]):
        self._on_liquidation = fn

    def on_trade(self, fn: Callable[..., Awaitable]):
        self._on_trade = fn

    def on_orderbook(self, fn: Callable[..., Awaitable]):
        self._on_orderbook = fn

    # ── Динамическая подписка на целевой символ ──────────────────────

    async def focus_symbol(self, symbol: str):
        """Подписаться на trades + orderbook для конкретного символа."""
        if self._ws is None or self._ws.closed:
            return

        if self._active_target and self._active_target != symbol:
            await self._send_unsub([
                f"publicTrade.{self._active_target}",
                f"orderbook.50.{self._active_target}",
            ])
            logger.info("Unfocused %s", self._active_target)

        if self._active_target == symbol:
            return

        await self._send_sub([
            f"publicTrade.{symbol}",
            f"orderbook.50.{symbol}",
        ])
        self._active_target = symbol
        logger.info("Focused → %s (trades + orderbook)", symbol)

    async def unfocus(self):
        """Отписаться от текущего целевого символа."""
        if self._active_target and self._ws and not self._ws.closed:
            await self._send_unsub([
                f"publicTrade.{self._active_target}",
                f"orderbook.50.{self._active_target}",
            ])
            logger.info("Unfocused %s", self._active_target)
            self._active_target = None

    # ── Основной цикл ────────────────────────────────────────────────

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._connect_and_listen()
            except Exception as e:
                logger.error("WS error: %s — reconnecting in 5s", e)
                await self._cleanup_connection()
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False
        await self._cleanup_connection()

    async def _connect_and_listen(self):
        await self._cleanup_connection()
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(
            BYBIT_WS_URL, heartbeat=self._ping_interval)
        logger.info("WS connected to %s", BYBIT_WS_URL)

        # Подписываемся на allLiquidation для ВСЕХ символов пачками
        liq_args = [f"allLiquidation.{s}" for s in self.symbols]
        for i in range(0, len(liq_args), MAX_ARGS_PER_SUB):
            batch = liq_args[i : i + MAX_ARGS_PER_SUB]
            await self._send_sub(batch)
            await asyncio.sleep(0.1)

        logger.info("Subscribed to allLiquidation for %d symbols", len(self.symbols))

        # Восстановить фокус, если был
        if self._active_target:
            old_target = self._active_target
            self._active_target = None
            await self.focus_symbol(old_target)

        # Запускаем ping и слушаем
        ping_task = asyncio.create_task(self._ping_loop())
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._dispatch(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("WS closed/error")
                    break
        finally:
            ping_task.cancel()
            await self._cleanup_connection()

    async def _ping_loop(self):
        """Bybit требует ping каждые 20с для поддержки соединения."""
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                if self._ws and not self._ws.closed:
                    await self._ws.send_json({"op": "ping"})
        except asyncio.CancelledError:
            pass

    async def _send_sub(self, args: list[str]):
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"op": "subscribe", "args": args})
            logger.debug("Sub → %s", args[:3])

    async def _send_unsub(self, args: list[str]):
        if self._ws and not self._ws.closed:
            await self._ws.send_json({"op": "unsubscribe", "args": args})
            logger.debug("Unsub → %s", args[:3])

    async def _dispatch(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Пропуск системных ответов
        op = data.get("op")
        if op in ("pong", "subscribe", "unsubscribe"):
            if not data.get("success", True):
                logger.warning("WS op failed: %s", data)
            return

        topic: str = data.get("topic", "")
        items = data.get("data", {})

        # ── Ликвидации ───────────────────────────────────────────────
        if topic.startswith("allLiquidation.") and self._on_liquidation:
            # Bybit присылает data как объект (не массив) для liquidation
            # Формат: { s: symbol, S: "Buy"|"Sell", v: "size", p: "price", ... }
            if isinstance(items, dict):
                items = [items]
            for item in items:
                await self._on_liquidation(item)

        # ── Сделки ───────────────────────────────────────────────────
        elif topic.startswith("publicTrade.") and self._on_trade:
            if isinstance(items, dict):
                items = [items]
            for item in items:
                await self._on_trade(item)

        # ── Стакан ───────────────────────────────────────────────────
        elif topic.startswith("orderbook.") and self._on_orderbook:
            await self._on_orderbook(items)
