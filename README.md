# KuCoin Liquidation Bot 🟢🎆

Торговый бот для KuCoin Futures, который обнаруживает крупные ликвидации и подтверждает разворот по 4 этапам, отправляя поэтапные уведомления в Telegram.

## Стратегия (4 этапа подтверждения)

| Этап | Индикатор | Telegram |
|------|-----------|----------|
| 1 | Крупная ликвидация обнаружена | 🟢 |
| 2 | Цена пробила линию Боллинджера | 🟢🟢 |
| 3 | Скопление ордеров в стакане (wall) | 🟢🟢🟢 |
| 4 | Перевес покупателей/продавцов | 🟢🟢🟢🟢 |
| **Финал** | **Все подтверждения пройдены** | **🎆 ПОКУПКА / ПРОДАЖА** |

## Установка

```bash
# 1. Клонировать / скачать проект
cd kucoin-liq-bot

# 2. Создать виртуальное окружение
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/Mac

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Скопировать и заполнить .env
copy .env.example .env       # Windows
# cp .env.example .env       # Linux/Mac
```

## Настройка

Отредактируйте файл `.env`:

| Переменная | Описание |
|-----------|----------|
| `KUCOIN_API_KEY` | API ключ KuCoin (нужен для futures) |
| `KUCOIN_API_SECRET` | API секрет |
| `KUCOIN_API_PASSPHRASE` | Пароль API |
| `TELEGRAM_BOT_TOKEN` | Токен Telegram бота (@BotFather) |
| `TELEGRAM_CHAT_ID` | ID чата / канала для уведомлений |
| `SYMBOL` | Торговая пара, напр. `BTC/USDT` |
| `LIQUIDATION_THRESHOLD_USD` | Порог ликвидации в USD (по умолчанию 50000) |
| `BOLLINGER_PERIOD` | Период Боллинджера (по умолчанию 20) |
| `BOLLINGER_STD` | Множитель стандартного отклонения (по умолчанию 2.0) |
| `ORDER_CLUSTER_RATIO` | Во сколько раз объём кластера должен превышать средний (по умолчанию 3.0) |
| `DOMINANCE_SHIFT_THRESHOLD` | Порог доминирования (0-1, по умолчанию 0.6 = 60%) |

### Как получить Telegram Chat ID

1. Создайте бота через [@BotFather](https://t.me/BotFather) → получите токен
2. Отправьте боту любое сообщение
3. Откройте `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Найдите `"chat":{"id":123456}` — это ваш `TELEGRAM_CHAT_ID`

## Запуск

```bash
python bot.py
```

Бот подключится к KuCoin Futures WebSocket и начнёт мониторинг. При запуске отправит тестовое сообщение в Telegram.

## Архитектура

```
bot.py                  — главный оркестратор (SignalPipeline + TradingBot)
kucoin_ws.py            — WebSocket клиент KuCoin Futures
indicators.py           — Bollinger Bands
order_cluster.py        — детектор скоплений ордеров в стакане
dominance.py            — детектор перевеса покупателей/продавцов
telegram_notifier.py    — отправка поэтапных уведомлений в Telegram
config.py               — конфигурация из .env
```

## Логика работы

1. **Ликвидация** — WS получает snapshot / крупную сделку, превышающую порог → Этап 1
2. **Боллинджер** — цена касается/пробивает нижнюю (buy) или верхнюю (sell) ленту → Этап 2
3. **Стакан** — обнаружен кластер ордеров (bid-wall для buy, ask-wall для sell) → Этап 3
4. **Перевес** — объём сделок за окно показывает доминирование нужной стороны → Этап 4
5. **🎆 Сигнал** — все 4 этапа пройдены за 2 минуты → финальное уведомление

Если 4 этапа не собраны за 2 минуты — pipeline сбрасывается.
