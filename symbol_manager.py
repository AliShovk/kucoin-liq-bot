"""Получение списка всех активных линейных (USDT) фьючерсных пар Bybit."""

import logging
import aiohttp

logger = logging.getLogger(__name__)

INSTRUMENTS_URL = "https://api.bybit.com/v5/market/instruments-info?category=linear"

EXCLUDED_SYMBOLS = {"BANANAS31USDT", "PUMPFUNUSDT"}
EXCLUDED_BASE_PREFIXES = ("BTC", "ETH")


def _is_excluded_symbol(symbol: str) -> bool:
    return symbol in EXCLUDED_SYMBOLS or symbol.startswith(EXCLUDED_BASE_PREFIXES)


async def fetch_all_symbols() -> list[str]:
    """Возвращает список символов вида ['BTCUSDT', 'ETHUSDT', ...].

    Bybit linear perpetuals, фильтруем исключения.
    """
    symbols: list[str] = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(INSTRUMENTS_URL) as resp:
                data = await resp.json()
                for item in data.get("result", {}).get("list", []):
                    sym = item.get("symbol", "")
                    if sym and not _is_excluded_symbol(sym):
                        symbols.append(sym)
        logger.info("Fetched %d active Bybit linear symbols", len(symbols))
    except Exception as e:
        logger.error("Failed to fetch symbols: %s", e)
        symbols = ["XRPUSDT"]
    return symbols


def symbol_to_display(symbol: str) -> str:
    """BTCUSDT → BTC/USDT, ETHUSDT → ETH/USDT."""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}/USDT"
    return symbol
