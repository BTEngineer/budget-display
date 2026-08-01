"""SQLite-backed household budget ledger.

Money crosses this boundary as a decimal string and is stored as integer cents.
Expenses are immutable; undo creates a linked reversal instead of deleting data.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class BudgetValidationError(ValueError):
    """Raised when a requested ledger operation is invalid or ambiguous."""


class DuplicateRequestError(BudgetValidationError):
    """Raised when a request ID is reused for a different operation."""


DEFAULT_MEMBERS = ("Member 1", "Member 2")
DEFAULT_CATEGORIES = (
    ("Everyday", None, 100_000),
    ("Occasional", None, 50_000),
    ("Meals", None, 30_000),
    ("Food", "Meals", None),
    ("Drinks", "Meals", None),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_cents(value: str | Decimal) -> int:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BudgetValidationError("amount must be a decimal monetary value") from exc
    if not amount.is_finite() or amount <= 0:
        raise BudgetValidationError("amount must be greater than zero")
    if amount.as_tuple().exponent < -2:
        raise BudgetValidationError("amount cannot have more than two decimal places")
    return int(amount * 100)


def _validate_month(month: str) -> None:
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise BudgetValidationError("month must use YYYY-MM format") from exc


class BudgetLedger:
    """Narrow, validated interface to the authoritative SQLite ledger."""

    def __init__(
        self,
        database: str | Path,
        *,
        household_timezone: str = "America/New_York",
        members: Sequence[str] = DEFAULT_MEMBERS,
        categories: Sequence[tuple[str, str | None, int | None]] = DEFAULT_CATEGORIES,
    ):
        self.database = Path(database)
        self.members = tuple(members)
        self.categories = tuple(categories)
        try:
            self.household_timezone = ZoneInfo(household_timezone)
        except ZoneInfoNotFoundError as exc:
            raise BudgetValidationError(
                f"unknown household timezone: {household_timezone}"
            ) from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS members (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE,
                    parent_id INTEGER REFERENCES categories(id),
                    default_monthly_budget_cents INTEGER
                        CHECK (default_monthly_budget_cents > 0),
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    UNIQUE (parent_id, name)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS categories_root_name
                ON categories(name) WHERE parent_id IS NULL;

                CREATE TABLE IF NOT EXISTS monthly_budgets (
                    category_id INTEGER NOT NULL REFERENCES categories(id),
                    month TEXT NOT NULL CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
                    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                    PRIMARY KEY (category_id, month)
                );

                CREATE TABLE IF NOT EXISTS budget_months (
                    month TEXT PRIMARY KEY
                        CHECK (month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ledger_entries (
                    id INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    member_id INTEGER NOT NULL REFERENCES members(id),
                    category_id INTEGER NOT NULL REFERENCES categories(id),
                    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
                    description TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    local_month TEXT NOT NULL
                        CHECK (local_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
                    created_at TEXT NOT NULL,
                    reverses_entry_id INTEGER UNIQUE REFERENCES ledger_entries(id)
                );

                CREATE INDEX IF NOT EXISTS ledger_entries_occurred_at
                ON ledger_entries(occurred_at);
                """
            )
            category_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(categories)").fetchall()
            }
            if "default_monthly_budget_cents" not in category_columns:
                connection.execute(
                    "ALTER TABLE categories ADD COLUMN default_monthly_budget_cents INTEGER"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO budget_months(month, created_at)
                SELECT DISTINCT month, 'migrated' FROM monthly_budgets
                """
            )
            connection.execute(
                """
                UPDATE ledger_entries AS reversal
                SET local_month = (
                    SELECT original.local_month
                    FROM ledger_entries AS original
                    WHERE original.id = reversal.reverses_entry_id
                )
                WHERE reversal.reverses_entry_id IS NOT NULL
                  AND local_month != (
                      SELECT original.local_month
                      FROM ledger_entries AS original
                      WHERE original.id = reversal.reverses_entry_id
                  )
                """
            )
            connection.execute("UPDATE members SET active = 0")
            for member in self.members:
                connection.execute(
                    "INSERT OR IGNORE INTO members(name) VALUES (?)", (member,)
                )
                connection.execute(
                    "UPDATE members SET active = 1 WHERE name = ?", (member,)
                )

            connection.execute("UPDATE categories SET active = 0")
            for category, parent, cents in self.categories:
                if parent is not None:
                    continue
                row = connection.execute(
                    "SELECT id FROM categories WHERE parent_id IS NULL AND name = ?",
                    (category,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO categories(
                            name, parent_id, default_monthly_budget_cents, active
                        ) VALUES (?, NULL, ?, 1)
                        """,
                        (category, cents),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE categories
                        SET default_monthly_budget_cents = ?, active = 1
                        WHERE id = ?
                        """,
                        (cents, row["id"]),
                    )

            for category, parent, cents in self.categories:
                if parent is None:
                    continue
                parent_row = connection.execute(
                    "SELECT id FROM categories WHERE parent_id IS NULL AND name = ? AND active = 1",
                    (parent,),
                ).fetchone()
                if parent_row is None:
                    raise BudgetValidationError(
                        f"category {category!r} references unknown parent {parent!r}"
                    )
                row = connection.execute(
                    "SELECT id FROM categories WHERE parent_id = ? AND name = ?",
                    (parent_row["id"], category),
                ).fetchone()
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO categories(
                            name, parent_id, default_monthly_budget_cents, active
                        ) VALUES (?, ?, NULL, 1)
                        """,
                        (category, parent_row["id"]),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE categories
                        SET default_monthly_budget_cents = NULL, active = 1
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )
            connection.commit()

    def list_budget_categories(self) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT child.name, parent.name AS parent_name,
                       EXISTS(SELECT 1 FROM categories nested
                              WHERE nested.parent_id = child.id AND nested.active = 1) AS has_children
                FROM categories child
                LEFT JOIN categories parent ON parent.id = child.parent_id
                WHERE child.active = 1
                ORDER BY COALESCE(parent.name, child.name), child.parent_id, child.name
                """
            ).fetchall()
        return [
            {
                "name": row["name"],
                "parent": row["parent_name"],
                "accepts_expenses": not bool(row["has_children"]),
            }
            for row in rows
        ]

    def add_expense(
        self,
        *,
        request_id: str,
        member: str,
        category: str,
        amount: str | Decimal,
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        request_id = request_id.strip()
        if not request_id:
            raise BudgetValidationError("request_id is required")
        cents = _parse_cents(amount)
        clean_description = description.strip()
        requested_timestamp = (
            self._normalize_timestamp(occurred_at)[0]
            if occurred_at is not None
            else None
        )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT entry.*, member.name AS member_name,
                       category.name AS category_name,
                       parent.name AS parent_category_name
                FROM ledger_entries entry
                JOIN members member ON member.id = entry.member_id
                JOIN categories category ON category.id = entry.category_id
                LEFT JOIN categories parent ON parent.id = category.parent_id
                WHERE entry.request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing:
                requested_category = self._normalize_category_path(category)
                existing_category = "/".join(
                    part
                    for part in (
                        existing["parent_category_name"],
                        existing["category_name"],
                    )
                    if part is not None
                )
                payload_matches = (
                    existing["reverses_entry_id"] is None
                    and existing["member_name"].casefold() == member.strip().casefold()
                    and existing_category.casefold() == requested_category.casefold()
                    and existing["amount_cents"] == cents
                    and existing["description"] == clean_description
                    and (
                        requested_timestamp is None
                        or existing["occurred_at"] == requested_timestamp
                    )
                )
                if not payload_matches:
                    raise DuplicateRequestError(
                        "request_id already belongs to a different ledger operation"
                    )
                connection.rollback()
                return self._entry(connection, existing["id"], duplicate=True)

            member_id = self._member_id(connection, member)
            category_id = self._category_id(connection, category)
            timestamp, local_month = self._normalize_timestamp(
                occurred_at if occurred_at is not None else _utc_now()
            )
            payload = (member_id, category_id, cents, clean_description, timestamp)
            cursor = connection.execute(
                """
                INSERT INTO ledger_entries(
                    request_id, member_id, category_id, amount_cents,
                    description, occurred_at, local_month, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, *payload, local_month, _utc_now()),
            )
            result = self._entry(connection, cursor.lastrowid, duplicate=False)
            connection.commit()
            return result

    def undo_last_expense(
        self, *, request_id: str, member: str | None = None
    ) -> dict[str, object]:
        request_id = request_id.strip()
        if not request_id:
            raise BudgetValidationError("request_id is required")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT entry.id, entry.reverses_entry_id, members.name AS member_name
                FROM ledger_entries entry
                JOIN members ON members.id = entry.member_id
                WHERE entry.request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing:
                member_matches = (
                    member is None
                    or existing["member_name"].casefold() == member.strip().casefold()
                )
                if existing["reverses_entry_id"] is None or not member_matches:
                    raise DuplicateRequestError(
                        "request_id already belongs to a different ledger operation"
                    )
                connection.rollback()
                return self._entry(connection, existing["id"], duplicate=True)

            parameters: list[object] = []
            member_filter = ""
            if member is not None:
                parameters.append(self._member_id(connection, member))
                member_filter = "AND original.member_id = ?"
            original = connection.execute(
                f"""
                SELECT original.*
                FROM ledger_entries original
                LEFT JOIN ledger_entries reversal
                  ON reversal.reverses_entry_id = original.id
                WHERE original.amount_cents > 0
                  AND original.reverses_entry_id IS NULL
                  AND reversal.id IS NULL
                  {member_filter}
                ORDER BY original.occurred_at DESC, original.id DESC
                LIMIT 1
                """,
                parameters,
            ).fetchone()
            if original is None:
                raise BudgetValidationError("no expense is available to undo")
            cursor = connection.execute(
                """
                INSERT INTO ledger_entries(
                    request_id, member_id, category_id, amount_cents,
                    description, occurred_at, local_month, created_at, reverses_entry_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    original["member_id"],
                    original["category_id"],
                    -original["amount_cents"],
                    f"Undo: {original['description']}".rstrip(),
                    (undo_time := _utc_now()),
                    original["local_month"],
                    undo_time,
                    original["id"],
                ),
            )
            result = self._entry(connection, cursor.lastrowid, duplicate=False)
            connection.commit()
            return result

    def set_monthly_budget(
        self, *, month: str, category: str, amount: str | Decimal
    ) -> dict[str, object]:
        _validate_month(month)
        cents = _parse_cents(amount)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_month_budgets(connection, month)
            category_id = self._category_id(connection, category, allow_parent=True)
            category_row = connection.execute(
                "SELECT parent_id FROM categories WHERE id = ?", (category_id,)
            ).fetchone()
            if category_row["parent_id"] is not None:
                raise BudgetValidationError(
                    "monthly limits belong to top-level categories; budget the parent rather than its subcategories"
                )
            connection.execute(
                """
                INSERT INTO monthly_budgets(category_id, month, amount_cents)
                VALUES (?, ?, ?)
                ON CONFLICT(category_id, month)
                DO UPDATE SET amount_cents = excluded.amount_cents
                """,
                (category_id, month, cents),
            )
            connection.commit()
        return {"month": month, "category": category, "amount_cents": cents}

    def set_default_monthly_budget(
        self, *, category: str, amount: str | Decimal
    ) -> dict[str, object]:
        cents = _parse_cents(amount)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            category_id = self._category_id(connection, category, allow_parent=True)
            category_row = connection.execute(
                "SELECT parent_id FROM categories WHERE id = ?", (category_id,)
            ).fetchone()
            if category_row["parent_id"] is not None:
                raise BudgetValidationError(
                    "monthly limits belong to top-level categories; budget the parent rather than its subcategories"
                )
            connection.execute(
                "UPDATE categories SET default_monthly_budget_cents = ? WHERE id = ?",
                (cents, category_id),
            )
            connection.commit()
        return {"category": category, "amount_cents": cents}

    def list_spending(self, *, month: str) -> dict[str, object]:
        _validate_month(month)
        with self._connection() as connection:
            self._ensure_month_budgets(connection, month)
            total = connection.execute(
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS cents
                FROM ledger_entries
                WHERE local_month = ?
                """,
                (month,),
            ).fetchone()["cents"]
            by_member = connection.execute(
                """
                SELECT members.name, COALESCE(SUM(ledger_entries.amount_cents), 0) AS cents
                FROM members
                LEFT JOIN ledger_entries
                  ON ledger_entries.member_id = members.id
                 AND ledger_entries.local_month = ?
                WHERE members.active = 1
                   OR EXISTS (
                       SELECT 1 FROM ledger_entries historic
                       WHERE historic.member_id = members.id
                         AND historic.local_month = ?
                   )
                GROUP BY members.id
                ORDER BY members.id
                """,
                (month, month),
            ).fetchall()
            by_category = connection.execute(
                """
                SELECT root.name, COALESCE(SUM(entry.amount_cents), 0) AS cents
                FROM categories root
                LEFT JOIN categories included
                  ON included.id = root.id OR included.parent_id = root.id
                LEFT JOIN ledger_entries entry
                  ON entry.category_id = included.id
                 AND entry.local_month = ?
                WHERE root.parent_id IS NULL
                  AND (
                      root.active = 1
                      OR EXISTS (
                          SELECT 1
                          FROM ledger_entries historic
                          LEFT JOIN categories historic_category
                            ON historic_category.id = historic.category_id
                          WHERE historic.local_month = ?
                            AND COALESCE(historic_category.parent_id,
                                         historic_category.id) = root.id
                      )
                  )
                GROUP BY root.id
                ORDER BY root.id
                """,
                (month, month),
            ).fetchall()
            budget = connection.execute(
                "SELECT SUM(amount_cents) AS cents FROM monthly_budgets WHERE month = ?",
                (month,),
            ).fetchone()["cents"]
            connection.commit()
        return {
            "month": month,
            "spent_cents": total,
            "budget_cents": budget,
            "remaining_cents": None if budget is None else budget - total,
            "by_member_cents": {row["name"]: row["cents"] for row in by_member},
            "by_category_cents": {
                row["name"]: row["cents"] for row in by_category
            },
        }

    @staticmethod
    def _ensure_month_budgets(connection: sqlite3.Connection, month: str) -> None:
        """Snapshot recurring defaults without altering existing month records."""
        cursor = connection.execute(
            "INSERT OR IGNORE INTO budget_months(month, created_at) VALUES (?, ?)",
            (month, _utc_now()),
        )
        if cursor.rowcount == 0:
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO monthly_budgets(category_id, month, amount_cents)
            SELECT id, ?, default_monthly_budget_cents
            FROM categories
            WHERE parent_id IS NULL
              AND active = 1
              AND default_monthly_budget_cents IS NOT NULL
            """,
            (month,),
        )

    def _normalize_timestamp(self, value: str) -> tuple[str, str]:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise BudgetValidationError("occurred_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise BudgetValidationError("occurred_at must include a timezone")
        local_month = parsed.astimezone(self.household_timezone).strftime("%Y-%m")
        utc_value = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        return utc_value, local_month

    @staticmethod
    def _member_id(connection: sqlite3.Connection, name: str) -> int:
        row = connection.execute(
            "SELECT id FROM members WHERE name = ? AND active = 1", (name.strip(),)
        ).fetchone()
        if row is None:
            raise BudgetValidationError(f"unknown household member: {name}")
        return row["id"]

    @staticmethod
    def _category_id(
        connection: sqlite3.Connection, path: str, *, allow_parent: bool = False
    ) -> int:
        parts = BudgetLedger._category_parts(path)
        if len(parts) == 1:
            rows = connection.execute(
                """
                SELECT id FROM categories
                WHERE parent_id IS NULL AND name = ? AND active = 1
                """,
                (parts[0],),
            ).fetchall()
        elif len(parts) == 2:
            rows = connection.execute(
                """
                SELECT child.id
                FROM categories child
                JOIN categories parent ON parent.id = child.parent_id
                WHERE parent.name = ? AND child.name = ? AND child.active = 1
                """,
                parts,
            ).fetchall()
        else:
            raise BudgetValidationError("category must be 'Category' or 'Parent/Child'")
        if len(rows) != 1:
            raise BudgetValidationError(f"unknown or ambiguous category: {path}")
        category_id = rows[0]["id"]
        has_children = connection.execute(
            "SELECT 1 FROM categories WHERE parent_id = ? AND active = 1 LIMIT 1",
            (category_id,),
        ).fetchone()
        if has_children and not allow_parent:
            raise BudgetValidationError(
                f"{path} requires a subcategory (for example, {path}/Food)"
            )
        return category_id

    @staticmethod
    def _category_parts(path: str) -> list[str]:
        parts = [part.strip() for part in path.split("/") if part.strip()]
        if len(parts) not in (1, 2):
            raise BudgetValidationError("category must be 'Category' or 'Parent/Child'")
        return parts

    @staticmethod
    def _normalize_category_path(path: str) -> str:
        return "/".join(BudgetLedger._category_parts(path))

    @staticmethod
    def _entry(
        connection: sqlite3.Connection, entry_id: int, *, duplicate: bool
    ) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT entry.id, entry.request_id, member.name AS member,
                   category.name AS category, parent.name AS parent_category,
                   entry.amount_cents, entry.description, entry.occurred_at,
                   entry.reverses_entry_id
            FROM ledger_entries entry
            JOIN members member ON member.id = entry.member_id
            JOIN categories category ON category.id = entry.category_id
            LEFT JOIN categories parent ON parent.id = category.parent_id
            WHERE entry.id = ?
            """,
            (entry_id,),
        ).fetchone()
        return {**dict(row), "duplicate": duplicate}
