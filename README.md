# Telegram-бот Claude + PostgreSQL

Бот позволяет управлять PostgreSQL-базой через обычные сообщения в Telegram.
Claude сам разбирает запрос, смотрит схему БД и выполняет нужный SQL.

## Быстрый старт

```bash
# 1. Клонируй / скопируй файлы
cd tg_claude_pg_bot

# 2. Создай виртуальное окружение
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Установи зависимости
pip install -r requirements.txt

# 4. Настрой переменные окружения
cp .env.example .env
nano .env   # заполни TELEGRAM_TOKEN, ANTHROPIC_API_KEY, DATABASE_URL

# 5. Загрузи .env и запусти
export $(grep -v '^#' .env | xargs)
python bot.py
```

## Переменные окружения

| Переменная         | Описание                                              |
|--------------------|-------------------------------------------------------|
| `TELEGRAM_TOKEN`   | Токен бота от @BotFather                             |
| `ANTHROPIC_API_KEY`| Ключ из console.anthropic.com                        |
| `DATABASE_URL`     | `postgresql://user:pass@host:5432/db`                |
| `ALLOWED_USER_IDS` | ID пользователей через запятую (пусто = все)         |

## Примеры запросов

```
Покажи все таблицы
Сколько заказов со статусом 'pending'?
Добавь товар: название "Ноутбук", цена 75000, склад 5 штук
Измени email пользователя с id=3 на new@mail.ru
Удали все логи старше 30 дней
Покажи последние 10 заказов с именами клиентов
```

## Архитектура

```
Telegram сообщение
       ↓
   bot.py (python-telegram-bot)
       ↓
   claude_agent.py
       ↓  ↑  tool_use loop
   Anthropic API (claude-sonnet-4)
       ↓
   PostgreSQL (psycopg2)
```

## Безопасность

- Доступ ограничен по `ALLOWED_USER_IDS`
- `execute_query` принимает только SELECT / WITH
- DROP TABLE / TRUNCATE требуют слово "ПОДТВЕРЖДАЮ" в сообщении
- История диалога хранится только в памяти, сбрасывается по /clear или перезапуску

## Деплой на сервере (systemd)

```ini
# /etc/systemd/system/tgbot.service
[Unit]
Description=Claude PG Telegram Bot
After=network.target

[Service]
WorkingDirectory=/opt/tg_claude_pg_bot
EnvironmentFile=/opt/tg_claude_pg_bot/.env
ExecStart=/opt/tg_claude_pg_bot/.venv/bin/python bot.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable tgbot && systemctl start tgbot
```
