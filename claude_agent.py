"""
Claude-агент с инструментами для работы с PostgreSQL.
Использует Tool Use API — Claude сам решает, какой SQL выполнить.
"""

import os
import io
import logging
import psycopg2
import psycopg2.extras
import pandas as pd
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
    {
        "name": "export_data",
        "description": (
            "Выполнить SELECT и вернуть результат в виде файла. "
            "Используй когда пользователь просит выгрузить данные в Excel или картинкой/изображением."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "SELECT-запрос"},
                "format": {
                    "type": "string",
                    "enum": ["excel", "image"],
                    "description": "Формат: excel (.xlsx) или image (PNG)",
                },
                "filename": {
                    "type": "string",
                    "description": "Имя файла без расширения, например 'players'",
                },
            },
            "required": ["sql", "format"],
        },
    },
]

# ─── Системный промпт ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты — ассистент для работы с PostgreSQL базой данных. Работаешь на модели claude-sonnet-4-5-20250929.

АЛГОРИТМ ОБРАБОТКИ ЗАПРОСА:

Если пользователь прислал готовый SQL-запрос — сразу переходи к выполнению (шаг 4).

Иначе:
1. УТОЧНЕНИЕ: если в запросе есть что-то неоднозначное или непонятное — задай уточняющий вопрос. Не угадывай.
2. ПОКАЗ ЗАПРОСА: если всё понятно — покажи сгенерированный SQL. Жди подтверждения.
3. ПОДТВЕРЖДЕНИЕ: пользователь подтверждает (текстом или реакцией/эмодзи) или корректирует.
4. ВЫПОЛНЕНИЕ: запускай SQL и возвращай результат.

ФОРМАТ ОТВЕТА:
- Данные сразу, без вступлений типа "Выполнено, получено N строк из таблицы X"
- Никаких описаний структуры таблиц в ответе если пользователь не просил
- Данные выводи максимально читаемо: выравнивай колонки, используй разделители
- Если строк много — спроси нужно ли больше

ЖЁСТКИЕ ЗАПРЕТЫ:
- НИКОГДА не выполняй DROP TABLE, DROP DATABASE, TRUNCATE — без исключений
- DROP FUNCTION — только после явного текстового подтверждения от пользователя, не по реакции/эмодзи
- Не делай SQL-инъекции, всегда используй параметризованные запросы

ПРОЧЕЕ:
- Перед первым запросом в новом диалоге вызови get_schema
"""

# ─── Агент ───────────────────────────────────────────────────────────────────

class ClaudeAgent:
    def __init__(self):
        self.client   = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.db_url   = os.environ.get("DATABASE_URL")
        self.schemas  = ["public"]
        self.model    = "claude-sonnet-4-5-20250929"
        self._pending_file: dict | None = None

    # ── Очистка истории от осиротевших tool_result ────────────────────────────

    @staticmethod
    def _sanitize_history(history: list) -> list:
        """Убирает tool_result блоки без соответствующего tool_use в предыдущем сообщении."""
        clean = []
        for i, msg in enumerate(history):
            if msg["role"] == "user" and isinstance(msg["content"], list):
                # Собираем tool_use id из предыдущего assistant-сообщения
                valid_ids = set()
                if clean and clean[-1]["role"] == "assistant":
                    prev = clean[-1]["content"]
                    if isinstance(prev, list):
                        valid_ids = {b.id for b in prev if hasattr(b, "type") and b.type == "tool_use"}
                    elif isinstance(prev, str):
                        valid_ids = set()

                filtered = [
                    b for b in msg["content"]
                    if not (isinstance(b, dict) and b.get("type") == "tool_result")
                    or b.get("tool_use_id") in valid_ids
                ]
                if filtered:
                    clean.append({**msg, "content": filtered})
            else:
                clean.append(msg)
        return clean

    def _get_conn(self):
        return psycopg2.connect(self.db_url)

    def tool_get_schema(self, table_name: str | None = None) -> str:
        schemas_placeholder = ",".join(["%s"] * len(self.schemas))
        sql = f"""
            SELECT
                c.table_schema,
                c.table_name,
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default
            FROM information_schema.columns c
            WHERE c.table_schema IN ({schemas_placeholder})
            {"AND c.table_name = %s" if table_name else ""}
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """
        params = list(self.schemas) + ([table_name] if table_name else [])

        with self._get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        if not rows:
            return "Таблицы не найдены."

        tables: dict[str, list] = {}
        for r in rows:
            key = f"{r['table_schema']}.{r['table_name']}"
            tables.setdefault(key, []).append(r)

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

        # Безусловный запрет
        hard_block = ("DROP TABLE", "TRUNCATE", "DROP DATABASE", "DROP SCHEMA")
        if any(d in clean for d in hard_block):
            return "⛔ Операция запрещена. DROP TABLE, DROP DATABASE, DROP SCHEMA и TRUNCATE не выполняются никогда."

        # DROP FUNCTION — только с явным подтверждением
        if "DROP FUNCTION" in clean and "ПОДТВЕРЖДАЮ" not in confirm_message.upper():
            return (
                "⚠️ DROP FUNCTION — опасная операция. "
                "Напиши явно ПОДТВЕРЖДАЮ чтобы выполнить."
            )

        with self._get_conn() as conn, conn.cursor() as cur:
            cur.execute(sql)
            affected = cur.rowcount
            conn.commit()

        return f"✅ {confirm_message}\nЗатронуто строк: {affected}"

    def tool_export_data(self, sql: str, format: str, filename: str | None = None) -> str | bytes:
        clean = sql.strip().upper()
        if not clean.startswith("SELECT") and not clean.startswith("WITH"):
            return "⛔ export_data допускает только SELECT / WITH."

        with self._get_conn() as conn:
            df = pd.read_sql_query(sql, conn.cursor().connection)

        if df.empty:
            return "Запрос вернул пустой результат — файл не создан."

        name = filename or "export"

        if format == "excel":
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                df.to_excel(writer, index=False, sheet_name="Data")
            buf.seek(0)
            # Возвращаем метаданные — бот сам отправит файл
            self._pending_file = {
                "data":     buf.read(),
                "filename": f"{name}.xlsx",
                "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }
            return f"__FILE__:{name}.xlsx:{len(df)} строк, {len(df.columns)} колонок"

        elif format == "image":
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt

            # Ограничиваем для читабельности
            preview = df.head(50)
            fig, ax = plt.subplots(figsize=(max(8, len(df.columns) * 1.5), max(4, len(preview) * 0.4 + 1.5)))
            ax.axis("off")
            tbl = ax.table(
                cellText=preview.values.tolist(),
                colLabels=df.columns.tolist(),
                cellLoc="center",
                loc="center",
            )
            tbl.auto_set_font_size(False)
            tbl.set_fontsize(9)
            tbl.auto_set_column_width(col=list(range(len(df.columns))))

            # Заголовок — серый фон
            for j in range(len(df.columns)):
                tbl[0, j].set_facecolor("#4472C4")
                tbl[0, j].set_text_props(color="white", fontweight="bold")

            # Зебра
            for i in range(1, len(preview) + 1):
                for j in range(len(df.columns)):
                    tbl[i, j].set_facecolor("#EEF2FF" if i % 2 == 0 else "white")

            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)

            suffix = f" (показано 50 из {len(df)})" if len(df) > 50 else ""
            self._pending_file = {
                "data":     buf.read(),
                "filename": f"{name}.png",
                "mimetype": "image/png",
            }
            return f"__FILE__:{name}.png:{len(df)} строк{suffix}"

        return f"Неизвестный формат: {format}"

    # ── Основной цикл агента ─────────────────────────────────────────────────

    def chat(self, user_message: str, history: list, db_url: str | None = None, schemas: list | None = None) -> tuple[str, list]:
        """
        Принимает сообщение, историю, опциональный db_url и список схем.
        Возвращает (ответ_текст, новая_история).
        """
        if db_url:
            self.db_url = db_url
        if schemas:
            self.schemas = schemas
        self._pending_file = None
        history = self._sanitize_history(history)
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
                return text, history, self._pending_file

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
        return "Что-то пошло не так.", history, None

    def _dispatch_tool(self, name: str, inputs: dict) -> str:
        try:
            if name == "get_schema":
                return self.tool_get_schema(inputs.get("table_name"))
            elif name == "execute_query":
                return self.tool_execute_query(inputs["sql"], inputs.get("limit", 50))
            elif name == "execute_mutation":
                return self.tool_execute_mutation(inputs["sql"], inputs["confirm_message"])
            elif name == "export_data":
                return self.tool_export_data(inputs["sql"], inputs["format"], inputs.get("filename"))
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