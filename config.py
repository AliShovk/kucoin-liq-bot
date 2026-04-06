import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Telegram
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "")
    TRADE_TELEGRAM_BOT_TOKEN = os.getenv("TRADE_TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    TRADE_TELEGRAM_CHAT_ID = os.getenv("TRADE_TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    # Bybit — ликвидации
    # Минимальный объём ликвидации (USD) для попадания в агрегатор
    BYBIT_MIN_SIZE = float(os.getenv("BYBIT_MIN_SIZE", "1000"))
    # Порог LONG ликвидаций (суммарный USD за окно) для сигнала
    BYBIT_LONG_THRESHOLD = float(os.getenv("BYBIT_LONG_THRESHOLD", "50000"))
    # Порог SHORT ликвидаций (суммарный USD за окно) для сигнала
    BYBIT_SHORT_THRESHOLD = float(os.getenv("BYBIT_SHORT_THRESHOLD", "50000"))
    # Интервал агрегации ликвидаций (секунды)
    BYBIT_AGGREGATION_INTERVAL = int(os.getenv("BYBIT_AGGREGATION_INTERVAL", "10"))

    # Price Reclaim — цена должна вернуться через экстремум выноса
    # Процент отката от экстремума для подтверждения reclaim (0.002 = 0.2%)
    RECLAIM_PCT = float(os.getenv("RECLAIM_PCT", "0.002"))

    # Bollinger Bands
    BOLLINGER_PERIOD = int(os.getenv("BOLLINGER_PERIOD", "20"))
    BOLLINGER_STD = float(os.getenv("BOLLINGER_STD", "2.0"))

    # Скопление ордеров: глубина стакана (уровней) и ratio для кластера
    ORDER_CLUSTER_DEPTH = int(os.getenv("ORDER_CLUSTER_DEPTH", "15"))
    ORDER_CLUSTER_RATIO = float(os.getenv("ORDER_CLUSTER_RATIO", "3.0"))

    # Перевес: окно (секунд) и порог доминирования
    DOMINANCE_SHIFT_WINDOW = int(os.getenv("DOMINANCE_SHIFT_WINDOW", "30"))
    DOMINANCE_SHIFT_THRESHOLD = float(os.getenv("DOMINANCE_SHIFT_THRESHOLD", "0.6"))

    # ── KuCoin Futures — торговля ──────────────────────────────────
    KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
    KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
    KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")

    # Автоторговля вкл/выкл (по умолчанию ВЫКЛ для безопасности)
    AUTOTRADE_ENABLED = os.getenv("AUTOTRADE_ENABLED", "false").lower() == "true"

    # Плечо
    TRADE_LEVERAGE = int(os.getenv("TRADE_LEVERAGE", "2"))
    # Базовый размер позиции (USDT)
    TRADE_SIZE_USDT = float(os.getenv("TRADE_SIZE_USDT", "10"))
    # Стоп-лосс (%)
    TRADE_SL_PCT = float(os.getenv("TRADE_SL_PCT", "10"))
    # Тейк-профит (%) — берём среднее 1-3%, ставим 2%
    TRADE_TP_PCT = float(os.getenv("TRADE_TP_PCT", "2"))
    # Порог винрейта для наращивания размера (%)
    TRADE_WINRATE_THRESHOLD = float(os.getenv("TRADE_WINRATE_THRESHOLD", "65"))
    # На сколько процентов наращиваем размер при хорошем винрейте
    TRADE_SIZE_SCALE_PCT = float(os.getenv("TRADE_SIZE_SCALE_PCT", "2"))
    # Минимум сделок для оценки винрейта
    TRADE_MIN_TRADES_FOR_SCALE = int(os.getenv("TRADE_MIN_TRADES_FOR_SCALE", "10"))
    TRADE_MONITOR_INTERVAL = int(os.getenv("TRADE_MONITOR_INTERVAL", "5"))
