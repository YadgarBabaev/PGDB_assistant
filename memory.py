"""
Модуль памяти пользователей.
Хранит предпочтения и паттерны каждого пользователя в отдельной PostgreSQL БД.
"""

import os
import logging
import psycopg2
import psycopg2.extras
from anthropic import Anthropic

logger = logging.getLogger(__name__)

MEMORY_DB_URL = os.environ.get("AI_MEMORY_DATABASE_URL")

# Каждые N сообщений обновляем память
UPDATE_EVERY_N = 6

MEMORY_UPDATE_PROMPT = """Ты анализируешь историю диалога пользователя с ботом управления PostgreSQL базой данных.

Текущая память о пользователе:
{current_memory}

История диалога:
{history}

Обнови память — сохрани только полезное для будущих диалогов:
- какие таблицы использует чаще всего
- предпочтения по формату (текст/файл, с лимитом или без)
- типичные фильтры и условия
- как формулирует запросы
- что раздражает или нравится в ответах бота

Правила:
- Пиши кратко, только факты, без воды
- Максимум 300 слов
- Не упоминай конкретные данные из БД (имена, id и т.д.)
- Если ничего нового — верни текущую память без изменений
- Отвечай только текстом памяти, без пояснений
"""


class MemoryManager:
    def __init__(self):
        self.client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self._ensure_table()

    def _get_conn(self):
        if not MEMORY_DB_URL:
            raise RuntimeError("MEMORY_DATABASE_URL не задан")
        return psycopg2.connect(MEMORY_DB_URL)

    def _ensure_table(self):
        """Создаёт таблицу если не существует."""
        if not MEMORY_DB_URL:
            logger.warning("MEMORY_DATABASE_URL не задан — память отключена")
            return
        try:
            with self._get_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS user_memory (
                        user_id     BIGINT PRIMARY KEY,
                        memory      TEXT NOT NULL DEFAULT '',
                        msg_count   INTEGER NOT NULL DEFAULT 0,
                        updated_at  TIMESTAMP DEFAULT NOW()
                    )
                """)
                conn.commit()
            logger.info("Memory table ready")
        except Exception as e:
            logger.error("Failed to create memory table: %s", e)

    def load(self, user_id: int) -> str:
        """Загружает память пользователя. Возвращает пустую строку если нет."""
        if not MEMORY_DB_URL:
            return ""
        try:
            with self._get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT memory FROM user_memory WHERE user_id = %s", (user_id,))
                row = cur.fetchone()
                return row["memory"] if row else ""
        except Exception as e:
            logger.error("Memory load error: %s", e)
            return ""

    def increment_and_check(self, user_id: int) -> bool:
        """
        Увеличивает счётчик сообщений.
        Возвращает True если пора обновить память.
        """
        if not MEMORY_DB_URL:
            return False
        try:
            with self._get_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_memory (user_id, memory, msg_count)
                    VALUES (%s, '', 1)
                    ON CONFLICT (user_id) DO UPDATE
                    SET msg_count = user_memory.msg_count + 1
                    RETURNING msg_count
                """, (user_id,))
                count = cur.fetchone()[0]
                conn.commit()
            return count % UPDATE_EVERY_N == 0
        except Exception as e:
            logger.error("Memory increment error: %s", e)
            return False

    def update(self, user_id: int, history: list, current_memory: str) -> str:
        """
        Просит Claude обновить память на основе истории диалога.
        Сохраняет результат в БД. Возвращает новую память.
        """
        if not MEMORY_DB_URL:
            return current_memory

        # Форматируем историю для промпта
        history_text = ""
        for msg in history[-20:]:  # последние 20 сообщений
            role = "Пользователь" if msg["role"] == "user" else "Бот"
            content = msg["content"]
            if isinstance(content, list):
                # tool_result или tool_use — пропускаем
                texts = [b.get("content", "") if isinstance(b, dict) else getattr(b, "text", "") for b in content]
                content = " ".join(t for t in texts if t and not str(t).startswith("__FILE__"))
            if content:
                history_text += f"{role}: {content}\n"

        if not history_text.strip():
            return current_memory

        try:
            response = self.client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=500,
                messages=[{
                    "role": "user",
                    "content": MEMORY_UPDATE_PROMPT.format(
                        current_memory=current_memory or "(пусто)",
                        history=history_text,
                    )
                }]
            )
            new_memory = response.content[0].text.strip()

            with self._get_conn() as conn, conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_memory (user_id, memory, msg_count)
                    VALUES (%s, %s, 0)
                    ON CONFLICT (user_id) DO UPDATE
                    SET memory = %s, updated_at = NOW()
                """, (user_id, new_memory, new_memory))
                conn.commit()

            logger.info("Memory updated for user_id=%s (%d chars)", user_id, len(new_memory))
            return new_memory

        except Exception as e:
            logger.error("Memory update error: %s", e)
            return current_memory

    def clear(self, user_id: int):
        """Сбрасывает память пользователя."""
        if not MEMORY_DB_URL:
            return
        try:
            with self._get_conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE user_memory SET memory = '', msg_count = 0, updated_at = NOW() WHERE user_id = %s",
                    (user_id,)
                )
                conn.commit()
        except Exception as e:
            logger.error("Memory clear error: %s", e)