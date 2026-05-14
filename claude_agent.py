"""
Claude-агент с инструментами для работы с PostgreSQL.
Использует Tool Use API — Claude сам решает, какой SQL выполнить.
"""

import os
import json
import logging
import psycopg2
import psycopg2.extras
from anthropic import Anthropic

logger = logging.getLogger(__name__)

# ─── Описание инструментов для Claude ────────────────────────────────────────

TOOLS = [
    {
        "name": "get_schema",
        "description": (
            "Получить схему базы данных: список таблиц и их столбцов с типами. "
            "Вызывай ВСЕГДА перед первым SQL-запросом в новом диалоге, "
            "чтобы знать точные названия таблиц и колонок."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Конкретная таблица (опционально). Если не указана — вернёт все таблицы.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "execute_query",
        "description": (
            "Выполнить SELECT-запрос и вернуть результат. "
            "Используй только для чтения данных."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT-запрос"},
                "limit": {
                    "type": "integer",
                    "description": "Максимум строк (по умолчанию 50)",
                    "default": 50,
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "execute_mutation",
        "description": (
            "Выполнить INSERT / UPDATE / DELETE и вернуть количество затронутых строк. "
            "Перед выполнением обязательно объясни пользователю, что именно будет изменено."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "INSERT / UPDATE / DELETE запрос"},
                "confirm_message": {
                    "type": "string",
                    "description": "Короткое человекочитаемое описание операции для пользователя",
                },
            },
            "required": ["sql", "confirm_message"],
        },
    },
]

# ─── Системный промпт ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — ассистент для работы с PostgreSQL базой данных.

Правила:
1. Перед первым запросом в диалоге вызови get_schema, чтобы знать структуру БД.
2. Всегда используй параметризованные значения в SQL — никакого SQL-инъекционного кода.
3. Перед INSERT/UPDATE/DELETE кратко опиши пользователю, что именно изменишь.
4. Возвращай результаты в читаемом виде (таблица как текст или список).
5. Если запрос неоднозначен — уточни, прежде чем выполнять.
6. Никогда не выполняй DROP TABLE / TRUNCATE / DROP DATABASE без явного подтверждения.
7. Отвечай на том же языке, на котором пишет пользователь.
"""

# ─── Агент ───────────────────────────────────────────────────────────────────

class ClaudeAgent:
    def __init__(self):
        self.client  = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.db_url  = os.environ.get("DATABASE_URL")
        self.model   = "claude-sonnet-4-20250514"

    # ── Инструменты ──────────────────────────────────────────────────────────

    def _get_conn(self):
        return psycopg2.connect(self.db_url)

    def tool_get_schema(self, table_name: str | None = None) -> str:
        sql = """
            SELECT
                c.table_name,
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
            {filter}
            ORDER BY c.table_name, c.ordinal_position
        """.format(
            filter=f"AND c.table_name = %s" if table_name else ""
        )

        with self._get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, (table_name,) if table_name else ())
            rows = cur.fetchall()

        if not rows:
            return "Таблицы не найдены." if table_name else "БД пуста (нет таблиц в схеме public)."

        # Группируем по таблице
        tables: dict[str, list] = {}
        for r in rows:
            tables.setdefault(r["table_name"], []).append(r)

        lines = []
        for tname, cols in tables.items():
            lines.append(f"\n📋 {tname}")
            for c in cols:
                nullable = "" if c["is_nullable"] == "YES" else " NOT NULL"
                default  = f" DEFAULT {c['column_default']}" if c["column_default"] else ""
                lines.append(f"  • {c['column_name']} — {c['data_type']}{nullable}{default}")

        return "\n".join(lines)

    def tool_execute_query(self, sql: str, limit: int = 50) -> str:
        # Защита: только SELECT
        clean = sql.strip().upper()
        if not clean.startswith("SELECT") and not clean.startswith("WITH"):
            return "⛔ execute_query допускает только SELECT / WITH."

        # Добавляем LIMIT если нет
        if "LIMIT" not in clean:
            sql = f"{sql.rstrip(';')} LIMIT {limit}"

        with self._get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            rows = cur.fetchall()

        if not rows:
            return "Запрос выполнен. Строк не найдено."

        return _rows_to_text(rows)

    def tool_execute_mutation(self, sql: str, confirm_message: str) -> str:
        clean = sql.strip().upper()

        # Запрещаем деструктивные DDL без явного слова CONFIRM в confirm_message
        danger = ("DROP TABLE", "TRUNCATE", "DROP DATABASE", "DROP SCHEMA")
        if any(d in clean for d in danger):
            if "ПОДТВЕРЖДАЮ" not in confirm_message.upper():
                return (
                    "⚠️ Опасная операция! Для выполнения пользователь должен явно "
                    "написать ПОДТВЕРЖДАЮ в запросе."
                )

        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            affected = cur.rowcount
            conn.commit()

        return f"✅ {confirm_message}\nЗатронуто строк: {affected}"

    # ── Основной цикл агента ─────────────────────────────────────────────────

    def chat(self, user_message: str, history: list, db_url: str | None = None) -> tuple[str, list]:
        """
        Принимает сообщение, историю и опциональный db_url (для мульти-БД).
        Возвращает (ответ_текст, новая_история).
        """
        if db_url:
            self.db_url = db_url
        history = history + [{"role": "user", "content": user_message}]

        while True:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=history,
            )

            # Добавляем ответ ассистента в историю
            history.append({"role": "assistant", "content": response.content})

            # Если Claude закончил — возвращаем текст
            if response.stop_reason == "end_turn":
                text = " ".join(
                    block.text for block in response.content if hasattr(block, "text")
                )
                return text, history

            # Иначе — обрабатываем tool calls
            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                logger.info("Tool call: %s(%s)", block.name, block.input)
                result = self._dispatch_tool(block.name, block.input)
                logger.info("Tool result: %s", result[:200])

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            history.append({"role": "user", "content": tool_results})

        # На всякий случай
        return "Что-то пошло не так.", history

    def _dispatch_tool(self, name: str, inputs: dict) -> str:
        try:
            if name == "get_schema":
                return self.tool_get_schema(inputs.get("table_name"))
            elif name == "execute_query":
                return self.tool_execute_query(inputs["sql"], inputs.get("limit", 50))
            elif name == "execute_mutation":
                return self.tool_execute_mutation(inputs["sql"], inputs["confirm_message"])
            else:
                return f"Неизвестный инструмент: {name}"
        except psycopg2.Error as e:
            logger.error("DB error in %s: %s", name, e)
            return f"❌ Ошибка БД: {e.pgerror or str(e)}"
        except Exception as e:
            logger.exception("Tool error in %s", name)
            return f"❌ Ошибка: {e}"

# ─── Хелпер: строки → читаемый текст ─────────────────────────────────────────

def _rows_to_text(rows: list[dict]) -> str:
    if not rows:
        return "(пусто)"

    keys = list(rows[0].keys())
    col_widths = {k: max(len(k), max(len(str(r.get(k, ""))) for r in rows)) for k in keys}

    sep   = "┼".join("─" * (col_widths[k] + 2) for k in keys)
    header = "│".join(f" {k:<{col_widths[k]}} " for k in keys)

    lines = [header, sep]
    for row in rows:
        lines.append("│".join(f" {str(row.get(k, '')):<{col_widths[k]}} " for k in keys))

    lines.append(f"\n({len(rows)} строк)")
    return "\n".join(lines)
