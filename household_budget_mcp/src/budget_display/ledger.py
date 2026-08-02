"""SQLite-backed household budget ledger.

Money crosses this boundary as a decimal string and is stored as integer cents.
Expenses are immutable; undo creates a linked reversal instead of deleting data.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterator, Mapping, Sequence
from uuid import uuid4
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
MAX_SEARCH_LIMIT = 200
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_TEXT_LENGTH = 500
MAX_REQUEST_ID_LENGTH = 200
MAX_BUSINESS_NAME_LENGTH = 200
MAX_ENTRY_DESCRIPTION_LENGTH = 1000
SUPPORTED_OPERATION_TYPES = {"expense", "refund", "reversal"}
MAX_SPLIT_ALLOCATIONS = 20
CONFIRMATION_TTL_SECONDS = 10 * 60
LARGE_EXPENSE_CENTS = 50_000


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
        classification_aliases: Sequence[tuple[str, str]] = (),
    ):
        self.database = Path(database)
        self.members = tuple(members)
        self.categories = tuple(categories)
        self.classification_aliases = tuple(classification_aliases)
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
                    transaction_id TEXT UNIQUE,
                    request_id TEXT NOT NULL UNIQUE,
                    member_id INTEGER NOT NULL REFERENCES members(id),
                    category_id INTEGER NOT NULL REFERENCES categories(id),
                    amount_cents INTEGER NOT NULL CHECK (amount_cents != 0),
                    operation_type TEXT NOT NULL DEFAULT 'expense'
                        CHECK (operation_type IN ('expense', 'refund', 'reversal')),
                    business_name TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    local_month TEXT NOT NULL
                        CHECK (local_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
                    created_at TEXT NOT NULL,
                    reverses_entry_id INTEGER UNIQUE REFERENCES ledger_entries(id),
                    refunds_entry_id INTEGER REFERENCES ledger_entries(id),
                    corrects_entry_id INTEGER REFERENCES ledger_entries(id),
                    split_group_id TEXT
                );

                CREATE INDEX IF NOT EXISTS ledger_entries_occurred_at
                ON ledger_entries(occurred_at);

                CREATE TABLE IF NOT EXISTS ledger_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS receipt_files (
                    id INTEGER PRIMARY KEY,
                    digest TEXT NOT NULL UNIQUE,
                    relative_path TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    byte_size INTEGER NOT NULL CHECK (byte_size > 0),
                    original_filename TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    ledger_entry_id INTEGER REFERENCES ledger_entries(id)
                );

                CREATE TABLE IF NOT EXISTS receipt_drafts (
                    id INTEGER PRIMARY KEY,
                    receipt_file_id INTEGER NOT NULL UNIQUE
                        REFERENCES receipt_files(id),
                    status TEXT NOT NULL DEFAULT 'uploaded'
                        CHECK (status IN ('uploaded', 'analyzed', 'confirmed', 'rejected')),
                    ai_entity_id TEXT,
                    raw_ai_json TEXT,
                    merchant TEXT,
                    occurred_on TEXT,
                    subtotal TEXT,
                    tax TEXT,
                    tip TEXT,
                    total TEXT,
                    suggested_category TEXT,
                    notes TEXT,
                    confirmation_request_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS receipt_drafts_status
                ON receipt_drafts(status, updated_at);

                CREATE TABLE IF NOT EXISTS multi_entry_operations (
                    request_id TEXT PRIMARY KEY,
                    operation_type TEXT NOT NULL
                        CHECK (operation_type IN ('correction', 'split_expense')),
                    payload_digest TEXT NOT NULL,
                    primary_entry_id INTEGER REFERENCES ledger_entries(id),
                    group_id TEXT,
                    created_at TEXT NOT NULL
                );
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
            entry_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(ledger_entries)").fetchall()
            }
            if "transaction_id" not in entry_columns:
                connection.execute("ALTER TABLE ledger_entries ADD COLUMN transaction_id TEXT")
            if "operation_type" not in entry_columns:
                connection.execute(
                    "ALTER TABLE ledger_entries ADD COLUMN operation_type TEXT "
                    "NOT NULL DEFAULT 'expense' CHECK (operation_type IN "
                    "('expense', 'refund', 'reversal'))"
                )
            if "business_name" not in entry_columns:
                connection.execute(
                    "ALTER TABLE ledger_entries ADD COLUMN business_name TEXT NOT NULL DEFAULT ''"
                )
            if "refunds_entry_id" not in entry_columns:
                connection.execute(
                    "ALTER TABLE ledger_entries ADD COLUMN refunds_entry_id "
                    "INTEGER REFERENCES ledger_entries(id)"
                )
            if "corrects_entry_id" not in entry_columns:
                connection.execute(
                    "ALTER TABLE ledger_entries ADD COLUMN corrects_entry_id "
                    "INTEGER REFERENCES ledger_entries(id)"
                )
            if "split_group_id" not in entry_columns:
                connection.execute(
                    "ALTER TABLE ledger_entries ADD COLUMN split_group_id TEXT"
                )
            connection.execute(
                "UPDATE ledger_entries SET operation_type = 'reversal' "
                "WHERE reverses_entry_id IS NOT NULL"
            )
            missing_transaction_ids = connection.execute(
                "SELECT id FROM ledger_entries WHERE transaction_id IS NULL OR transaction_id = ''"
            ).fetchall()
            for row in missing_transaction_ids:
                connection.execute(
                    "UPDATE ledger_entries SET transaction_id = ? WHERE id = ?",
                    (self._new_transaction_id(), row["id"]),
                )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ledger_entries_transaction_id "
                "ON ledger_entries(transaction_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ledger_entries_search_order "
                "ON ledger_entries(occurred_at, transaction_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ledger_entries_refunds_entry "
                "ON ledger_entries(refunds_entry_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ledger_entries_corrections_entry "
                "ON ledger_entries(corrects_entry_id)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ledger_entries_split_group "
                "ON ledger_entries(split_group_id)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS multi_entry_operations_group "
                "ON multi_entry_operations(group_id) WHERE group_id IS NOT NULL"
            )
            connection.execute(
                "INSERT OR IGNORE INTO ledger_metadata(key, value) VALUES ('cursor_secret', ?)",
                (secrets.token_hex(32),),
            )
            connection.execute(
                "INSERT OR IGNORE INTO ledger_metadata(key, value) "
                "VALUES ('expense_confirmation_secret', ?)",
                (secrets.token_hex(32),),
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

    def register_receipt(
        self,
        *,
        digest: str,
        relative_path: str,
        content_type: str,
        byte_size: int,
        original_filename: str,
    ) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM receipt_files WHERE digest = ?", (digest,)
            ).fetchone()
            duplicate = existing is not None
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO receipt_files(
                        digest, relative_path, content_type, byte_size,
                        original_filename, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        digest,
                        relative_path,
                        content_type,
                        byte_size,
                        original_filename,
                        _utc_now(),
                    ),
                )
                receipt_id = cursor.lastrowid
                now = _utc_now()
                draft_cursor = connection.execute(
                    """
                    INSERT INTO receipt_drafts(
                        receipt_file_id, status, created_at, updated_at
                    ) VALUES (?, 'uploaded', ?, ?)
                    """,
                    (receipt_id, now, now),
                )
                draft_id = draft_cursor.lastrowid
            else:
                receipt_id = existing["id"]
                draft_id = connection.execute(
                    "SELECT id FROM receipt_drafts WHERE receipt_file_id = ?",
                    (receipt_id,),
                ).fetchone()["id"]
            connection.commit()
        result = self.get_receipt_draft(draft_id)
        result["duplicate"] = duplicate
        return result

    def update_receipt_draft(
        self,
        *,
        draft_id: int,
        ai_entity_id: str,
        fields: Mapping[str, object],
    ) -> dict[str, object]:
        allowed = (
            "merchant",
            "occurred_on",
            "subtotal",
            "tax",
            "tip",
            "total",
            "suggested_category",
            "notes",
        )
        normalized = {
            name: (None if fields.get(name) is None else str(fields.get(name)).strip())
            for name in allowed
        }
        raw_ai_json = json.dumps(
            dict(fields), separators=(",", ":"), sort_keys=True
        )
        if len(raw_ai_json) > 16000:
            raw_ai_json = json.dumps(
                {"truncated": True, "preview": raw_ai_json[:15000]},
                separators=(",", ":"),
                sort_keys=True,
            )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, confirmation_request_id FROM receipt_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise BudgetValidationError("unknown receipt draft")
            if row["status"] == "confirmed":
                raise BudgetValidationError("confirmed receipt drafts cannot be changed")
            if row["confirmation_request_id"] is not None:
                raise BudgetValidationError("receipt draft confirmation is in progress")
            connection.execute(
                """
                UPDATE receipt_drafts
                SET status = 'analyzed', ai_entity_id = ?, raw_ai_json = ?,
                    merchant = ?, occurred_on = ?, subtotal = ?, tax = ?, tip = ?,
                    total = ?, suggested_category = ?, notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    ai_entity_id[:255],
                    raw_ai_json,
                    *(normalized[name][:1000] if normalized[name] else None for name in allowed),
                    _utc_now(),
                    draft_id,
                ),
            )
            connection.commit()
        return self.get_receipt_draft(draft_id)

    def get_receipt_draft(self, draft_id: int) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT draft.*, receipt.digest, receipt.relative_path,
                       receipt.content_type, receipt.byte_size,
                       receipt.original_filename, receipt.ledger_entry_id
                FROM receipt_drafts draft
                JOIN receipt_files receipt ON receipt.id = draft.receipt_file_id
                WHERE draft.id = ?
                """,
                (draft_id,),
            ).fetchone()
        if row is None:
            raise BudgetValidationError("unknown receipt draft")
        result = dict(row)
        result.pop("raw_ai_json", None)
        return result

    def confirm_receipt_draft(
        self,
        *,
        draft_id: int,
        request_id: str,
        member: str,
        category: str,
        amount: str,
        business_name: str | None = None,
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        draft = self.get_receipt_draft(draft_id)
        if draft["status"] == "confirmed":
            if draft["confirmation_request_id"] != request_id:
                raise DuplicateRequestError("receipt draft was already confirmed")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status, confirmation_request_id FROM receipt_drafts WHERE id = ?",
                (draft_id,),
            ).fetchone()
            if current is None:
                raise BudgetValidationError("unknown receipt draft")
            claimed_request = current["confirmation_request_id"]
            if claimed_request is not None and claimed_request != request_id:
                raise DuplicateRequestError(
                    "receipt draft is already being confirmed by another request"
                )
            connection.execute(
                "UPDATE receipt_drafts SET confirmation_request_id = ?, updated_at = ? WHERE id = ?",
                (request_id, _utc_now(), draft_id),
            )
            connection.commit()
        try:
            entry = self.add_expense(
                request_id=request_id,
                member=member,
                category=category,
                amount=amount,
                business_name=(
                    business_name
                    if business_name is not None
                    else str(draft.get("merchant") or "")
                ),
                description=description,
                occurred_at=occurred_at,
            )
        except Exception:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE receipt_drafts
                    SET confirmation_request_id = NULL, updated_at = ?
                    WHERE id = ? AND status != 'confirmed'
                      AND confirmation_request_id = ?
                    """,
                    (_utc_now(), draft_id, request_id),
                )
                connection.commit()
            raise
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE receipt_drafts
                SET status = 'confirmed', confirmation_request_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (request_id, _utc_now(), draft_id),
            )
            connection.execute(
                """
                UPDATE receipt_files SET ledger_entry_id = ?
                WHERE id = (SELECT receipt_file_id FROM receipt_drafts WHERE id = ?)
                """,
                (entry["id"], draft_id),
            )
            connection.commit()
        return {"draft": self.get_receipt_draft(draft_id), "entry": entry}

    def list_recent_expenses(self, *, limit: int = 10) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise BudgetValidationError("limit must be between 1 and 100")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT entry.id
                FROM ledger_entries entry
                LEFT JOIN ledger_entries reversal
                  ON reversal.reverses_entry_id = entry.id
                WHERE entry.operation_type = 'expense'
                  AND reversal.id IS NULL
                ORDER BY entry.occurred_at DESC, entry.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._entry(connection, row["id"], duplicate=False) for row in rows]

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

    def prepare_expense(
        self,
        *,
        request_id: str,
        member: str,
        category: str,
        amount: str | Decimal,
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        """Create a short-lived confirmation token bound to an exact expense."""
        request_id = self._validated_request_id(request_id)
        cents = _parse_cents(amount)
        business_name = self._bounded_entry_text(
            business_name, "business_name", MAX_BUSINESS_NAME_LENGTH
        )
        description = self._bounded_entry_text(
            description, "description", MAX_ENTRY_DESCRIPTION_LENGTH
        )
        normalized_timestamp = self._normalize_timestamp(
            occurred_at if occurred_at is not None else _utc_now()
        )[0]
        with self._connection() as connection:
            member_id = self._member_id(connection, member)
            category_id = self._category_id(connection, category)
            member_name = connection.execute(
                "SELECT name FROM members WHERE id = ?", (member_id,)
            ).fetchone()["name"]
            category_path = self._category_path(connection, category_id)
            payload = {
                "confirmation_type": "expense",
                "request_id": request_id,
                "member": member_name,
                "category": category_path,
                "amount_cents": cents,
                "business_name": business_name,
                "description": description,
                "occurred_at": normalized_timestamp,
            }
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=CONFIRMATION_TTL_SECONDS
            )
            token = self._encode_confirmation(
                connection,
                {**payload, "expires_at": int(expires_at.timestamp())},
            )
        return {
            "expense": {
                **payload,
                "amount": self._decimal_amount(cents),
                "currency": "USD",
            },
            "confirmation_token": token,
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }

    def validate_expense_confirmation(
        self,
        *,
        token: str,
        request_id: str,
        member: str,
        category: str,
        amount: str | Decimal,
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> None:
        """Fail closed unless a confirmation token matches every expense field."""
        if occurred_at is None:
            raise BudgetValidationError(
                "occurred_at returned by prepare_expense is required with confirmation_token"
            )
        with self._connection() as connection:
            payload = self._decode_confirmation(connection, token)
            member_id = self._member_id(connection, member)
            category_id = self._category_id(connection, category)
            expected = {
                "confirmation_type": "expense",
                "request_id": self._validated_request_id(request_id),
                "member": connection.execute(
                    "SELECT name FROM members WHERE id = ?", (member_id,)
                ).fetchone()["name"],
                "category": self._category_path(connection, category_id),
                "amount_cents": _parse_cents(amount),
                "business_name": self._bounded_entry_text(
                    business_name, "business_name", MAX_BUSINESS_NAME_LENGTH
                ),
                "description": self._bounded_entry_text(
                    description, "description", MAX_ENTRY_DESCRIPTION_LENGTH
                ),
                "occurred_at": self._normalize_timestamp(occurred_at)[0],
            }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise BudgetValidationError(
                "confirmation token does not match the exact expense payload"
            )

    def prepare_split_expense(
        self,
        *,
        request_id: str,
        total_amount: str | Decimal,
        allocations: Sequence[Mapping[str, object]],
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        """Create a short-lived token bound to an exact split expense."""
        bound_occurred_at = occurred_at if occurred_at is not None else _utc_now()
        with self._connection() as connection:
            payload = self._split_confirmation_payload(
                connection,
                request_id=request_id,
                total_amount=total_amount,
                allocations=allocations,
                business_name=business_name,
                description=description,
                occurred_at=bound_occurred_at,
            )
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=CONFIRMATION_TTL_SECONDS
            )
            token = self._encode_confirmation(
                connection,
                {**payload, "expires_at": int(expires_at.timestamp())},
            )
        return {
            "split_expense": payload,
            "confirmation_token": token,
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }

    def validate_split_confirmation(
        self, *, token: str, **values: object
    ) -> None:
        """Fail closed unless a token matches every split-expense field."""
        if values.get("occurred_at") is None:
            raise BudgetValidationError(
                "occurred_at returned by prepare_split_expense is required with confirmation_token"
            )
        with self._connection() as connection:
            actual = self._decode_confirmation(connection, token)
            expected = self._split_confirmation_payload(connection, **values)
        if any(actual.get(key) != value for key, value in expected.items()):
            raise BudgetValidationError(
                "confirmation token does not match the exact split expense payload"
            )

    def add_expense(
        self,
        *,
        request_id: str,
        member: str,
        category: str,
        amount: str | Decimal,
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        request_id = request_id.strip()
        if not request_id:
            raise BudgetValidationError("request_id is required")
        if len(request_id) > MAX_REQUEST_ID_LENGTH:
            raise BudgetValidationError(
                f"request_id cannot exceed {MAX_REQUEST_ID_LENGTH} characters"
            )
        cents = _parse_cents(amount)
        clean_business_name = business_name.strip()
        clean_description = description.strip()
        if len(clean_business_name) > MAX_BUSINESS_NAME_LENGTH:
            raise BudgetValidationError(
                f"business_name cannot exceed {MAX_BUSINESS_NAME_LENGTH} characters"
            )
        if len(clean_description) > MAX_ENTRY_DESCRIPTION_LENGTH:
            raise BudgetValidationError(
                f"description cannot exceed {MAX_ENTRY_DESCRIPTION_LENGTH} characters"
            )
        requested_timestamp = (
            self._normalize_timestamp(occurred_at)[0]
            if occurred_at is not None
            else None
        )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM multi_entry_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone():
                raise DuplicateRequestError(
                    "request_id already belongs to a different ledger operation"
                )
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
                    and existing["business_name"] == clean_business_name
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
            cursor = connection.execute(
                """
                INSERT INTO ledger_entries(
                    transaction_id, request_id, member_id, category_id,
                    amount_cents, operation_type, business_name, description,
                    occurred_at, local_month, created_at
                ) VALUES (?, ?, ?, ?, ?, 'expense', ?, ?, ?, ?, ?)
                """,
                (
                    self._new_transaction_id(),
                    request_id,
                    member_id,
                    category_id,
                    cents,
                    clean_business_name,
                    clean_description,
                    timestamp,
                    local_month,
                    _utc_now(),
                ),
            )
            result = self._entry(connection, cursor.lastrowid, duplicate=False)
            connection.commit()
            return result

    def prepare_correction(
        self,
        *,
        request_id: str,
        transaction_id: str,
        member: str | None = None,
        category: str | None = None,
        amount: str | Decimal | None = None,
        business_name: str | None = None,
        description: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        """Prepare an exact correction for explicit confirmation."""
        with self._connection() as connection:
            original, resolved, payload, _ = self._resolve_correction(
                connection,
                request_id=request_id,
                transaction_id=transaction_id,
                member=member,
                category=category,
                amount=amount,
                business_name=business_name,
                description=description,
                occurred_at=occurred_at,
            )
            if original["reversed_by_id"] is not None:
                raise BudgetValidationError("a reversed expense cannot be corrected")
            if original["refunded_cents"]:
                raise BudgetValidationError("a refunded expense cannot be corrected")
            if original["split_group_id"] is not None:
                raise BudgetValidationError(
                    "individual split allocations cannot be corrected"
                )
            if self._correction_is_noop(original, resolved):
                raise BudgetValidationError(
                    "correction must change at least one expense field"
                )
            expires_at = datetime.now(timezone.utc) + timedelta(
                seconds=CONFIRMATION_TTL_SECONDS
            )
            token = self._encode_confirmation(
                connection,
                {**payload, "expires_at": int(expires_at.timestamp())},
            )
        return {
            "correction": {
                **payload,
                "amount": self._decimal_amount(int(payload["amount_cents"])),
                "currency": "USD",
            },
            "confirmation_token": token,
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }

    def correct_expense(
        self,
        *,
        request_id: str,
        transaction_id: str,
        member: str | None = None,
        category: str | None = None,
        amount: str | Decimal | None = None,
        business_name: str | None = None,
        description: str | None = None,
        occurred_at: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, object]:
        """Atomically reverse an expense and create its corrected replacement."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            original, payload, confirmation_payload, resolved_month = (
                self._resolve_correction(
                    connection,
                    request_id=request_id,
                    transaction_id=transaction_id,
                    member=member,
                    category=category,
                    amount=amount,
                    business_name=business_name,
                    description=description,
                    occurred_at=occurred_at,
                )
            )
            request_id = str(confirmation_payload["request_id"])
            transaction_id = str(confirmation_payload["transaction_id"])
            resolved_member_id = int(payload["member_id"])
            resolved_category_id = int(payload["category_id"])
            resolved_cents = int(payload["amount_cents"])
            resolved_business = str(payload["business_name"])
            resolved_description = str(payload["description"])
            resolved_timestamp = str(payload["occurred_at"])
            payload_digest = self._payload_digest(payload)
            existing_operation = connection.execute(
                "SELECT * FROM multi_entry_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing_operation is not None:
                if (
                    existing_operation["operation_type"] != "correction"
                    or existing_operation["payload_digest"] != payload_digest
                ):
                    raise DuplicateRequestError(
                        "request_id already belongs to a different ledger operation"
                    )
                replacement_id = existing_operation["primary_entry_id"]
                reversal_id = connection.execute(
                    "SELECT id FROM ledger_entries WHERE reverses_entry_id = ?",
                    (original["id"],),
                ).fetchone()["id"]
                connection.rollback()
                return {
                    "duplicate": True,
                    "original": self._canonical_entry(connection, original["id"]),
                    "reversal": self._canonical_entry(connection, reversal_id),
                    "replacement": self._canonical_entry(connection, replacement_id),
                }
            if connection.execute(
                "SELECT 1 FROM ledger_entries WHERE request_id = ?", (request_id,)
            ).fetchone():
                raise DuplicateRequestError(
                    "request_id already belongs to a different ledger operation"
                )
            if original["reversed_by_id"] is not None:
                raise BudgetValidationError("a reversed expense cannot be corrected")
            if original["refunded_cents"]:
                raise BudgetValidationError("a refunded expense cannot be corrected")
            if original["split_group_id"] is not None:
                raise BudgetValidationError(
                    "individual split allocations cannot be corrected"
                )
            if self._correction_is_noop(original, payload):
                raise BudgetValidationError(
                    "correction must change at least one expense field"
                )
            if resolved_cents > LARGE_EXPENSE_CENTS:
                if not confirmation_token:
                    raise BudgetValidationError(
                        "corrected expense exceeds $500; call prepare_correction and retry with its exact confirmation_token"
                    )
                actual_confirmation = self._decode_confirmation(
                    connection, confirmation_token
                )
                if any(
                    actual_confirmation.get(key) != value
                    for key, value in confirmation_payload.items()
                ):
                    raise BudgetValidationError(
                        "confirmation token does not match the exact correction payload"
                    )

            now = _utc_now()
            reversal = connection.execute(
                """
                INSERT INTO ledger_entries(
                    transaction_id, request_id, member_id, category_id,
                    amount_cents, operation_type, business_name, description,
                    occurred_at, local_month, created_at, reverses_entry_id
                ) VALUES (?, ?, ?, ?, ?, 'reversal', ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_transaction_id(), self._internal_request_id(),
                    original["member_id"], original["category_id"],
                    -original["amount_cents"], original["business_name"],
                    f"Correction: {original['description']}".rstrip(), now,
                    original["local_month"], now, original["id"],
                ),
            )
            replacement = connection.execute(
                """
                INSERT INTO ledger_entries(
                    transaction_id, request_id, member_id, category_id,
                    amount_cents, operation_type, business_name, description,
                    occurred_at, local_month, created_at, corrects_entry_id
                ) VALUES (?, ?, ?, ?, ?, 'expense', ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_transaction_id(), request_id, resolved_member_id,
                    resolved_category_id, resolved_cents, resolved_business,
                    resolved_description, resolved_timestamp, resolved_month,
                    now, original["id"],
                ),
            )
            connection.execute(
                """
                INSERT INTO multi_entry_operations(
                    request_id, operation_type, payload_digest,
                    primary_entry_id, created_at
                ) VALUES (?, 'correction', ?, ?, ?)
                """,
                (request_id, payload_digest, replacement.lastrowid, now),
            )
            result = {
                "duplicate": False,
                "original": self._canonical_entry(connection, original["id"]),
                "reversal": self._canonical_entry(connection, reversal.lastrowid),
                "replacement": self._canonical_entry(connection, replacement.lastrowid),
            }
            connection.commit()
            return result

    def add_split_expense(
        self,
        *,
        request_id: str,
        total_amount: str | Decimal,
        allocations: Sequence[Mapping[str, object]],
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        """Atomically record one purchase allocated across people or categories."""
        request_id = self._validated_request_id(request_id)
        total_cents = _parse_cents(total_amount)
        if not 2 <= len(allocations) <= MAX_SPLIT_ALLOCATIONS:
            raise BudgetValidationError(
                f"allocations must contain between 2 and {MAX_SPLIT_ALLOCATIONS} items"
            )
        business_name = self._bounded_entry_text(
            business_name, "business_name", MAX_BUSINESS_NAME_LENGTH
        )
        description = self._bounded_entry_text(
            description, "description", MAX_ENTRY_DESCRIPTION_LENGTH
        )
        requested_timestamp = (
            self._normalize_timestamp(occurred_at)[0]
            if occurred_at is not None
            else None
        )
        timestamp, local_month = self._normalize_timestamp(
            occurred_at if occurred_at is not None else _utc_now()
        )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            normalized: list[dict[str, object]] = []
            for index, allocation in enumerate(allocations, start=1):
                if not isinstance(allocation, Mapping):
                    raise BudgetValidationError(f"allocation {index} must be an object")
                member = str(allocation.get("member", "")).strip()
                category = str(allocation.get("category", "")).strip()
                amount_value = allocation.get("amount")
                if amount_value is None:
                    raise BudgetValidationError(f"allocation {index} requires amount")
                member_id = self._member_id(connection, member)
                category_id = self._category_id(connection, category)
                normalized.append(
                    {
                        "member_id": member_id,
                        "category_id": category_id,
                        "amount_cents": _parse_cents(str(amount_value)),
                    }
                )
            if sum(item["amount_cents"] for item in normalized) != total_cents:
                raise BudgetValidationError(
                    "allocation amounts must add up exactly to total_amount"
                )
            payload = {
                "total_cents": total_cents,
                "allocations": normalized,
                "business_name": business_name,
                "description": description,
                "occurred_at": requested_timestamp,
            }
            payload_digest = self._payload_digest(payload)
            existing_operation = connection.execute(
                "SELECT * FROM multi_entry_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing_operation is not None:
                if (
                    existing_operation["operation_type"] != "split_expense"
                    or existing_operation["payload_digest"] != payload_digest
                ):
                    raise DuplicateRequestError(
                        "request_id already belongs to a different ledger operation"
                    )
                rows = connection.execute(
                    "SELECT id FROM ledger_entries WHERE split_group_id = ? ORDER BY id",
                    (existing_operation["group_id"],),
                ).fetchall()
                connection.rollback()
                return self._split_result(
                    connection, existing_operation["group_id"], total_cents,
                    rows, duplicate=True
                )
            if connection.execute(
                "SELECT 1 FROM ledger_entries WHERE request_id = ?", (request_id,)
            ).fetchone():
                raise DuplicateRequestError(
                    "request_id already belongs to a different ledger operation"
                )

            group_id = f"split_{uuid4().hex}"
            entry_ids: list[int] = []
            now = _utc_now()
            for item in normalized:
                cursor = connection.execute(
                    """
                    INSERT INTO ledger_entries(
                        transaction_id, request_id, member_id, category_id,
                        amount_cents, operation_type, business_name, description,
                        occurred_at, local_month, created_at, split_group_id
                    ) VALUES (?, ?, ?, ?, ?, 'expense', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self._new_transaction_id(), self._internal_request_id(),
                        item["member_id"], item["category_id"], item["amount_cents"],
                        business_name, description, timestamp, local_month, now, group_id,
                    ),
                )
                entry_ids.append(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO multi_entry_operations(
                    request_id, operation_type, payload_digest,
                    primary_entry_id, group_id, created_at
                ) VALUES (?, 'split_expense', ?, ?, ?, ?)
                """,
                (request_id, payload_digest, entry_ids[0], group_id, now),
            )
            rows = [{"id": entry_id} for entry_id in entry_ids]
            result = self._split_result(
                connection, group_id, total_cents, rows, duplicate=False
            )
            connection.commit()
            return result

    def undo_last_expense(
        self,
        *,
        request_id: str,
        member: str | None = None,
        entry_id: int | None = None,
    ) -> dict[str, object]:
        request_id = request_id.strip()
        if not request_id:
            raise BudgetValidationError("request_id is required")
        if len(request_id) > MAX_REQUEST_ID_LENGTH:
            raise BudgetValidationError(
                f"request_id cannot exceed {MAX_REQUEST_ID_LENGTH} characters"
            )
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM multi_entry_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone():
                raise DuplicateRequestError(
                    "request_id already belongs to a different ledger operation"
                )
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
                entry_matches = (
                    entry_id is None or existing["reverses_entry_id"] == entry_id
                )
                if (
                    existing["reverses_entry_id"] is None
                    or not member_matches
                    or not entry_matches
                ):
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
            entry_filter = ""
            if entry_id is not None:
                if entry_id <= 0:
                    raise BudgetValidationError("entry_id must be a positive integer")
                parameters.append(entry_id)
                entry_filter = "AND original.id = ?"
            original = connection.execute(
                f"""
                SELECT original.*
                FROM ledger_entries original
                LEFT JOIN ledger_entries reversal
                  ON reversal.reverses_entry_id = original.id
                WHERE original.amount_cents > 0
                  AND original.operation_type = 'expense'
                  AND reversal.id IS NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM ledger_entries refund
                      WHERE refund.refunds_entry_id = original.id
                        AND refund.operation_type = 'refund'
                  )
                  {member_filter}
                  {entry_filter}
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
                    transaction_id, request_id, member_id, category_id,
                    amount_cents, operation_type, business_name, description,
                    occurred_at, local_month, created_at, reverses_entry_id
                ) VALUES (?, ?, ?, ?, ?, 'reversal', ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_transaction_id(),
                    request_id,
                    original["member_id"],
                    original["category_id"],
                    -original["amount_cents"],
                    original["business_name"],
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

    def refund_expense(
        self,
        *,
        request_id: str,
        amount: str | Decimal,
        expense_id: str | None = None,
        member: str | None = None,
        category: str | None = None,
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        """Record a linked or explicitly categorized unlinked refund."""
        request_id = request_id.strip()
        if not request_id:
            raise BudgetValidationError("request_id is required")
        if len(request_id) > MAX_REQUEST_ID_LENGTH:
            raise BudgetValidationError(
                f"request_id cannot exceed {MAX_REQUEST_ID_LENGTH} characters"
            )
        cents = _parse_cents(amount)
        clean_expense_id = expense_id.strip() if expense_id is not None else None
        if clean_expense_id == "":
            clean_expense_id = None
        clean_business_name = business_name.strip()
        clean_description = description.strip()
        if len(clean_business_name) > MAX_BUSINESS_NAME_LENGTH:
            raise BudgetValidationError(
                f"business_name cannot exceed {MAX_BUSINESS_NAME_LENGTH} characters"
            )
        if len(clean_description) > MAX_ENTRY_DESCRIPTION_LENGTH:
            raise BudgetValidationError(
                f"description cannot exceed {MAX_ENTRY_DESCRIPTION_LENGTH} characters"
            )
        if clean_expense_id is None:
            if member is None or not member.strip():
                raise BudgetValidationError("member is required for an unlinked refund")
            if category is None or not category.strip():
                raise BudgetValidationError("category is required for an unlinked refund")
        requested_timestamp = (
            self._normalize_timestamp(occurred_at)[0]
            if occurred_at is not None
            else None
        )

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM multi_entry_operations WHERE request_id = ?",
                (request_id,),
            ).fetchone():
                raise DuplicateRequestError(
                    "request_id already belongs to a different ledger operation"
                )
            original = None
            if clean_expense_id is not None:
                original = connection.execute(
                    """
                    SELECT entry.*, member.name AS member_name,
                           category.name AS category_name,
                           parent.name AS parent_category_name,
                           reversal.id AS reversal_id,
                           COALESCE((
                               SELECT SUM(-refund.amount_cents)
                               FROM ledger_entries refund
                               WHERE refund.refunds_entry_id = entry.id
                                 AND refund.operation_type = 'refund'
                           ), 0) AS refunded_cents
                    FROM ledger_entries entry
                    JOIN members member ON member.id = entry.member_id
                    JOIN categories category ON category.id = entry.category_id
                    LEFT JOIN categories parent ON parent.id = category.parent_id
                    LEFT JOIN ledger_entries reversal
                      ON reversal.reverses_entry_id = entry.id
                    WHERE entry.transaction_id = ?
                      AND entry.operation_type = 'expense'
                    """,
                    (clean_expense_id,),
                ).fetchone()
                if original is None:
                    raise BudgetValidationError("verified expense was not found")
                if original["reversal_id"] is not None:
                    raise BudgetValidationError("a reversed expense cannot be refunded")

            existing = connection.execute(
                """
                SELECT entry.*, member.name AS member_name,
                       category.name AS category_name,
                       parent.name AS parent_category_name,
                       original.transaction_id AS original_transaction_id
                FROM ledger_entries entry
                JOIN members member ON member.id = entry.member_id
                JOIN categories category ON category.id = entry.category_id
                LEFT JOIN categories parent ON parent.id = category.parent_id
                LEFT JOIN ledger_entries original ON original.id = entry.refunds_entry_id
                WHERE entry.request_id = ?
                """,
                (request_id,),
            ).fetchone()
            if existing:
                requested_category = (
                    self._normalize_category_path(category) if category is not None else None
                )
                existing_category = "/".join(
                    part
                    for part in (
                        existing["parent_category_name"],
                        existing["category_name"],
                    )
                    if part is not None
                )
                if clean_expense_id is None:
                    member_matches = (
                        existing["member_name"].casefold() == member.strip().casefold()
                    )
                    category_matches = (
                        existing_category.casefold() == requested_category.casefold()
                    )
                    business_matches = existing["business_name"] == clean_business_name
                else:
                    member_matches = (
                        member is None
                        or existing["member_name"].casefold() == member.strip().casefold()
                    )
                    category_matches = (
                        requested_category is None
                        or existing_category.casefold() == requested_category.casefold()
                    )
                    business_matches = (
                        not clean_business_name
                        or existing["business_name"] == clean_business_name
                    )
                payload_matches = (
                    existing["operation_type"] == "refund"
                    and existing["amount_cents"] == -cents
                    and existing["original_transaction_id"] == clean_expense_id
                    and member_matches
                    and category_matches
                    and business_matches
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

            if original is not None:
                original_category = "/".join(
                    part
                    for part in (
                        original["parent_category_name"],
                        original["category_name"],
                    )
                    if part is not None
                )
                if (
                    member is not None
                    and original["member_name"].casefold()
                    != member.strip().casefold()
                ):
                    raise BudgetValidationError("refund member does not match the original expense")
                if (
                    category is not None
                    and original_category.casefold()
                    != self._normalize_category_path(category).casefold()
                ):
                    raise BudgetValidationError(
                        "refund category does not match the original expense"
                    )
                if clean_business_name and original["business_name"] != clean_business_name:
                    raise BudgetValidationError(
                        "refund business name does not match the original expense"
                    )
                remaining_cents = original["amount_cents"] - original["refunded_cents"]
                if cents > remaining_cents:
                    raise BudgetValidationError(
                        "refund exceeds the remaining refundable amount of "
                        f"{self._decimal_amount(remaining_cents)}"
                    )
                member_id = original["member_id"]
                category_id = original["category_id"]
                stored_business_name = original["business_name"]
                refunds_entry_id = original["id"]
            else:
                member_id = self._member_id(connection, member)
                category_id = self._category_id(connection, category)
                stored_business_name = clean_business_name
                refunds_entry_id = None

            timestamp, local_month = self._normalize_timestamp(
                occurred_at if occurred_at is not None else _utc_now()
            )
            cursor = connection.execute(
                """
                INSERT INTO ledger_entries(
                    transaction_id, request_id, member_id, category_id,
                    amount_cents, operation_type, business_name, description,
                    occurred_at, local_month, created_at, refunds_entry_id
                ) VALUES (?, ?, ?, ?, ?, 'refund', ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._new_transaction_id(),
                    request_id,
                    member_id,
                    category_id,
                    -cents,
                    stored_business_name,
                    clean_description,
                    timestamp,
                    local_month,
                    _utc_now(),
                    refunds_entry_id,
                ),
            )
            result = self._entry(connection, cursor.lastrowid, duplicate=False)
            connection.commit()
            return result

    def search_transactions(
        self,
        *,
        start_at: str | None = None,
        end_at: str | None = None,
        member: str | None = None,
        category: str | None = None,
        business_name: str | None = None,
        description_query: str | None = None,
        minimum_amount: str | Decimal | None = None,
        maximum_amount: str | Decimal | None = None,
        transaction_id: str | None = None,
        request_id: str | None = None,
        operation_type: str = "all",
        status: str = "active",
        sort_order: str = "descending",
        limit: int = DEFAULT_SEARCH_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, object]:
        """Return a bounded, cursor-paginated canonical transaction page."""
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise BudgetValidationError(f"limit must be between 1 and {MAX_SEARCH_LIMIT}")
        if operation_type not in (*sorted(SUPPORTED_OPERATION_TYPES), "all"):
            raise BudgetValidationError("unsupported operation_type")
        if status not in ("active", "reversed", "all"):
            raise BudgetValidationError("unsupported status")
        if sort_order not in ("ascending", "descending"):
            raise BudgetValidationError("sort_order must be ascending or descending")

        start_value = self._normalize_timestamp(start_at)[0] if start_at else None
        end_value = self._normalize_timestamp(end_at)[0] if end_at else None
        if start_value is not None and end_value is not None and end_value <= start_value:
            raise BudgetValidationError("end_at must be later than start_at")
        minimum_cents = _parse_cents(minimum_amount) if minimum_amount is not None else None
        maximum_cents = _parse_cents(maximum_amount) if maximum_amount is not None else None
        if (
            minimum_cents is not None
            and maximum_cents is not None
            and maximum_cents < minimum_cents
        ):
            raise BudgetValidationError("maximum_amount cannot be less than minimum_amount")

        clean_member = self._optional_search_text(member, "member")
        clean_category = self._optional_search_text(category, "category")
        clean_business = self._optional_search_text(business_name, "business_name")
        clean_description = self._optional_search_text(description_query, "description_query")
        clean_transaction_id = self._optional_search_text(transaction_id, "transaction_id")
        clean_request_id = self._optional_search_text(request_id, "request_id")
        filter_state = {
            "start_at": start_value,
            "end_at": end_value,
            "member": clean_member.casefold() if clean_member else None,
            "category": (
                self._normalize_category_path(clean_category).casefold()
                if clean_category
                else None
            ),
            "business_name": clean_business.casefold() if clean_business else None,
            "description_query": clean_description.casefold() if clean_description else None,
            "minimum_cents": minimum_cents,
            "maximum_cents": maximum_cents,
            "transaction_id": clean_transaction_id,
            "request_id": clean_request_id,
            "operation_type": operation_type,
            "status": status,
            "sort_order": sort_order,
        }

        with self._connection() as connection:
            conditions: list[str] = []
            parameters: list[object] = []
            if start_value is not None:
                conditions.append("entry.occurred_at >= ?")
                parameters.append(start_value)
            if end_value is not None:
                conditions.append("entry.occurred_at < ?")
                parameters.append(end_value)
            if clean_member is not None:
                conditions.append("member.name = ? COLLATE NOCASE")
                parameters.append(clean_member)
            if clean_category is not None:
                category_ids = self._category_search_ids(connection, clean_category)
                placeholders = ", ".join("?" for _ in category_ids)
                conditions.append(f"entry.category_id IN ({placeholders})")
                parameters.extend(category_ids)
            if clean_business is not None:
                conditions.append("LOWER(entry.business_name) LIKE ? ESCAPE '\\'")
                parameters.append(f"%{self._escape_like(clean_business.casefold())}%")
            if clean_description is not None:
                conditions.append("LOWER(entry.description) LIKE ? ESCAPE '\\'")
                parameters.append(f"%{self._escape_like(clean_description.casefold())}%")
            if minimum_cents is not None:
                conditions.append("ABS(entry.amount_cents) >= ?")
                parameters.append(minimum_cents)
            if maximum_cents is not None:
                conditions.append("ABS(entry.amount_cents) <= ?")
                parameters.append(maximum_cents)
            if clean_transaction_id is not None:
                conditions.append("entry.transaction_id = ?")
                parameters.append(clean_transaction_id)
            if clean_request_id is not None:
                conditions.append(
                    "(entry.request_id = ? OR EXISTS ("
                    "SELECT 1 FROM multi_entry_operations operation "
                    "WHERE operation.request_id = ? "
                    "AND operation.group_id = entry.split_group_id))"
                )
                parameters.extend((clean_request_id, clean_request_id))
            if operation_type != "all":
                conditions.append("entry.operation_type = ?")
                parameters.append(operation_type)
            if status == "active":
                conditions.append(
                    "NOT (entry.operation_type = 'expense' AND EXISTS "
                    "(SELECT 1 FROM ledger_entries reversal "
                    "WHERE reversal.reverses_entry_id = entry.id))"
                )
            elif status == "reversed":
                conditions.append(
                    "entry.operation_type = 'expense' AND EXISTS "
                    "(SELECT 1 FROM ledger_entries reversal "
                    "WHERE reversal.reverses_entry_id = entry.id)"
                )

            filter_digest = hashlib.sha256(
                json.dumps(filter_state, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if cursor is not None:
                cursor_state = self._decode_cursor(
                    connection, cursor=cursor, expected_filter_digest=filter_digest
                )
                comparator = ">" if sort_order == "ascending" else "<"
                conditions.append(
                    f"(entry.occurred_at {comparator} ? OR "
                    "(entry.occurred_at = ? AND "
                    f"entry.transaction_id {comparator} ?))"
                )
                parameters.extend(
                    [
                        cursor_state["occurred_at"],
                        cursor_state["occurred_at"],
                        cursor_state["transaction_id"],
                    ]
                )

            direction = "ASC" if sort_order == "ascending" else "DESC"
            where_clause = " AND ".join(conditions) if conditions else "1 = 1"
            rows = connection.execute(
                f"""
                SELECT entry.id
                FROM ledger_entries entry
                JOIN members member ON member.id = entry.member_id
                WHERE {where_clause}
                ORDER BY entry.occurred_at {direction}, entry.transaction_id {direction}
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            transactions = [
                self._canonical_entry(connection, row["id"]) for row in page_rows
            ]
            next_cursor = None
            if has_more and transactions:
                last = transactions[-1]
                next_cursor = self._encode_cursor(
                    connection,
                    {
                        "filter_digest": filter_digest,
                        "occurred_at": last["occurred_at"],
                        "transaction_id": last["transaction_id"],
                    },
                )
            return {
                "transactions": transactions,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "count": len(transactions),
            }

    def suggest_expense_classification(
        self,
        *,
        business_name: str = "",
        description: str = "",
        limit: int = 5,
    ) -> dict[str, object]:
        """Suggest categories from configured aliases and authoritative history."""
        business_name = self._bounded_entry_text(
            business_name, "business_name", MAX_BUSINESS_NAME_LENGTH
        )
        description = self._bounded_entry_text(
            description, "description", MAX_ENTRY_DESCRIPTION_LENGTH
        )
        if not business_name and not description:
            raise BudgetValidationError("business_name or description is required")
        if not 1 <= limit <= 10:
            raise BudgetValidationError("limit must be between 1 and 10")
        haystack = " ".join(
            " ".join(part for part in (business_name, description) if part)
            .casefold()
            .split()
        )
        candidates: dict[str, dict[str, object]] = {}

        def offer(
            category: str,
            *,
            score: int,
            reason: str,
            sample_count: int = 0,
            last_used_at: str | None = None,
        ) -> None:
            current = candidates.get(category.casefold())
            if current is None or score > int(current["score"]):
                candidates[category.casefold()] = {
                    "category": category,
                    "score": score,
                    "reason": reason,
                    "sample_count": sample_count,
                    "last_used_at": last_used_at,
                }

        with self._connection() as connection:
            for term, category in self.classification_aliases:
                clean_term = " ".join(term.split())
                if not clean_term:
                    continue
                category_id = self._category_id(connection, category)
                normalized_term = clean_term.casefold()
                if normalized_term == haystack:
                    score, reason = 100, "exact configured alias"
                elif self._contains_alias(haystack, normalized_term):
                    score, reason = 90, "configured alias"
                else:
                    continue
                offer(
                    self._category_path(connection, category_id),
                    score=score,
                    reason=reason,
                )

            if business_name:
                rows = connection.execute(
                    """
                    SELECT category.id AS category_id,
                           COUNT(DISTINCT COALESCE(entry.split_group_id,
                                                   entry.transaction_id)) AS uses,
                           MAX(entry.occurred_at) AS last_used_at
                    FROM ledger_entries entry
                    JOIN categories category ON category.id = entry.category_id
                    LEFT JOIN ledger_entries reversal
                      ON reversal.reverses_entry_id = entry.id
                    WHERE entry.operation_type = 'expense'
                      AND reversal.id IS NULL
                      AND lower(entry.business_name) = lower(?)
                    GROUP BY category.id
                    ORDER BY uses DESC, last_used_at DESC
                    """,
                    (business_name,),
                ).fetchall()
                for row in rows:
                    uses = int(row["uses"])
                    offer(
                        self._category_path(connection, row["category_id"]),
                        score=min(88, 70 + uses * 3),
                        reason="prior expenses for this business",
                        sample_count=uses,
                        last_used_at=row["last_used_at"],
                    )

            category_rows = connection.execute(
                """
                SELECT child.id, child.name, parent.name AS parent_name
                FROM categories child
                LEFT JOIN categories parent ON parent.id = child.parent_id
                WHERE child.active = 1
                  AND NOT EXISTS (SELECT 1 FROM categories nested
                                  WHERE nested.parent_id = child.id AND nested.active = 1)
                """
            ).fetchall()
            words = {word.strip(".,;:!?()[]{}") for word in haystack.split()}
            for row in category_rows:
                category_path = "/".join(
                    part for part in (row["parent_name"], row["name"]) if part
                )
                category_words = {
                    word.casefold()
                    for part in category_path.split("/")
                    for word in part.split()
                }
                if words & category_words:
                    offer(category_path, score=40, reason="category name appears in text")

        ranked = sorted(
            candidates.values(),
            key=lambda item: (-int(item["score"]), str(item["category"]).casefold()),
        )[:limit]
        for item in ranked:
            score = int(item.pop("score"))
            item["confidence"] = "high" if score >= 85 else "medium" if score >= 60 else "low"
        return {
            "business_name": business_name or None,
            "description": description,
            "suggestions": ranked,
            "requires_explicit_category": True,
        }

    def get_budget_outlook(
        self, *, month: str, as_of: str | None = None
    ) -> dict[str, object]:
        """Return deterministic pace, comparison, projection, and risk values."""
        _validate_month(month)
        if as_of is None:
            as_of_utc = datetime.now(timezone.utc)
        else:
            try:
                parsed = datetime.fromisoformat(as_of)
            except ValueError as exc:
                raise BudgetValidationError("as_of must be an ISO 8601 timestamp") from exc
            if parsed.tzinfo is None:
                raise BudgetValidationError("as_of must include a timezone")
            as_of_utc = parsed.astimezone(timezone.utc)
        local_as_of = as_of_utc.astimezone(self.household_timezone)
        target = datetime.strptime(month, "%Y-%m")
        target_key = (target.year, target.month)
        current_key = (local_as_of.year, local_as_of.month)
        if target_key > current_key:
            raise BudgetValidationError("outlook month cannot be later than as_of")
        days_in_month = calendar.monthrange(target.year, target.month)[1]
        elapsed_days = local_as_of.day if target_key == current_key else days_in_month
        spending = self.list_spending(month=month)
        cutoff = as_of_utc.isoformat(timespec="seconds")
        with self._connection() as connection:
            spent_cents = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(amount_cents), 0) AS cents
                    FROM ledger_entries
                    WHERE local_month = ?
                      AND occurred_at <= ?
                      AND created_at <= ?
                    """,
                    (month, cutoff, cutoff),
                ).fetchone()["cents"]
            )
            category_spent_as_of = {
                row["name"]: int(row["cents"])
                for row in connection.execute(
                    """
                    SELECT root.name, COALESCE(SUM(entry.amount_cents), 0) AS cents
                    FROM ledger_entries entry
                    JOIN categories category ON category.id = entry.category_id
                    JOIN categories root
                      ON root.id = COALESCE(category.parent_id, category.id)
                    WHERE entry.local_month = ?
                      AND entry.occurred_at <= ?
                      AND entry.created_at <= ?
                    GROUP BY root.id
                    """,
                    (month, cutoff, cutoff),
                ).fetchall()
            }
        projected_cents = (
            spent_cents
            if elapsed_days == days_in_month
            else int(
                (Decimal(spent_cents) * days_in_month / elapsed_days).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            )
        )
        if target.month == 1:
            previous_year, previous_month = target.year - 1, 12
        else:
            previous_year, previous_month = target.year, target.month - 1
        previous_month_key = f"{previous_year:04d}-{previous_month:02d}"
        previous_days = calendar.monthrange(previous_year, previous_month)[1]
        comparison_day = min(elapsed_days, previous_days)
        with self._connection() as connection:
            previous_rows = connection.execute(
                "SELECT amount_cents, occurred_at FROM ledger_entries "
                "WHERE local_month = ? AND created_at <= ?",
                (previous_month_key, cutoff),
            ).fetchall()
        previous_same_point_cents = sum(
            int(row["amount_cents"])
            for row in previous_rows
            if datetime.fromisoformat(row["occurred_at"])
            .astimezone(self.household_timezone)
            .day
            <= comparison_day
        )
        category_risks: list[dict[str, object]] = []
        for row in spending["category_rows"]:
            if row["budget_cents"] is None:
                continue
            row_spent_cents = category_spent_as_of.get(str(row["name"]), 0)
            category_projected = (
                row_spent_cents
                if elapsed_days == days_in_month
                else int(
                    (
                        Decimal(row_spent_cents)
                        * days_in_month
                        / elapsed_days
                    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
                )
            )
            if category_projected > int(row["budget_cents"]):
                category_risks.append(
                    {
                        "category": row["name"],
                        "budget_cents": row["budget_cents"],
                        "spent_cents": row_spent_cents,
                        "projected_cents": category_projected,
                        "projected_overage_cents": category_projected - int(row["budget_cents"]),
                    }
                )
        return {
            "month": month,
            "as_of": as_of_utc.isoformat(timespec="seconds"),
            "elapsed_days": elapsed_days,
            "days_in_month": days_in_month,
            "spent_cents": spent_cents,
            "budget_cents": spending["budget_cents"],
            "remaining_discretionary_cents": (
                None
                if spending["budget_cents"] is None
                else int(spending["budget_cents"]) - spent_cents
            ),
            "daily_spending_rate_cents": int(
                (Decimal(spent_cents) / elapsed_days).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
            ),
            "projected_month_end_cents": projected_cents,
            "projected_remaining_cents": (
                None
                if spending["budget_cents"] is None
                else int(spending["budget_cents"]) - projected_cents
            ),
            "previous_month": previous_month_key,
            "previous_same_point_cents": previous_same_point_cents,
            "pace_change_cents": spent_cents - previous_same_point_cents,
            "categories_at_risk": category_risks,
            "currency": "USD",
        }

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
            direct_category_member = connection.execute(
                """
                SELECT category.name AS category_name,
                       parent.name AS parent_name,
                       members.name AS member_name,
                       COALESCE(SUM(entry.amount_cents), 0) AS cents
                FROM categories category
                LEFT JOIN categories parent ON parent.id = category.parent_id
                CROSS JOIN members
                LEFT JOIN ledger_entries entry
                  ON entry.category_id = category.id
                 AND entry.member_id = members.id
                 AND entry.local_month = ?
                WHERE category.active = 1 AND members.active = 1
                GROUP BY category.id, members.id
                ORDER BY category.id, members.id
                """,
                (month,),
            ).fetchall()
            category_budgets = connection.execute(
                """
                SELECT categories.name, monthly_budgets.amount_cents
                FROM monthly_budgets
                JOIN categories ON categories.id = monthly_budgets.category_id
                WHERE monthly_budgets.month = ? AND categories.active = 1
                """,
                (month,),
            ).fetchall()
            budget = connection.execute(
                "SELECT SUM(amount_cents) AS cents FROM monthly_budgets WHERE month = ?",
                (month,),
            ).fetchone()["cents"]
            connection.commit()
        direct: dict[tuple[str | None, str], dict[str, int]] = {}
        for row in direct_category_member:
            direct.setdefault(
                (row["parent_name"], row["category_name"]), {}
            )[row["member_name"]] = row["cents"]
        budget_by_root = {row["name"]: row["amount_cents"] for row in category_budgets}
        category_rows: list[dict[str, object]] = []
        for name, parent, _ in self.categories:
            if parent is None:
                included = [
                    amounts
                    for (row_parent, row_name), amounts in direct.items()
                    if (row_parent is None and row_name == name)
                    or row_parent == name
                ]
                member_totals = {
                    member: sum(amounts.get(member, 0) for amounts in included)
                    for member in self.members
                }
                row_budget = budget_by_root.get(name)
            else:
                member_totals = {
                    member: direct.get((parent, name), {}).get(member, 0)
                    for member in self.members
                }
                row_budget = None
            category_rows.append(
                {
                    "name": name,
                    "parent": parent,
                    "budget_cents": row_budget,
                    "spent_cents": sum(member_totals.values()),
                    "by_member_cents": member_totals,
                }
            )

        return {
            "month": month,
            "spent_cents": total,
            "budget_cents": budget,
            "remaining_cents": None if budget is None else budget - total,
            "by_member_cents": {row["name"]: row["cents"] for row in by_member},
            "by_category_cents": {
                row["name"]: row["cents"] for row in by_category
            },
            "category_rows": category_rows,
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
    def _new_transaction_id() -> str:
        return f"txn_{uuid4().hex}"

    @staticmethod
    def _internal_request_id() -> str:
        return f"internal_{uuid4().hex}"

    @staticmethod
    def _validated_request_id(value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise BudgetValidationError("request_id is required")
        if len(cleaned) > MAX_REQUEST_ID_LENGTH:
            raise BudgetValidationError(
                f"request_id cannot exceed {MAX_REQUEST_ID_LENGTH} characters"
            )
        return cleaned

    @staticmethod
    def _bounded_entry_text(value: str, name: str, maximum: int) -> str:
        cleaned = value.strip()
        if len(cleaned) > maximum:
            raise BudgetValidationError(f"{name} cannot exceed {maximum} characters")
        return cleaned

    @staticmethod
    def _payload_digest(payload: Mapping[str, object]) -> str:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _contains_alias(haystack: str, normalized_term: str) -> bool:
        """Match an alias without accepting it inside a larger word."""
        prefix = r"(?<!\w)" if normalized_term[0].isalnum() else ""
        suffix = r"(?!\w)" if normalized_term[-1].isalnum() else ""
        return re.search(f"{prefix}{re.escape(normalized_term)}{suffix}", haystack) is not None

    @staticmethod
    def _correction_is_noop(
        original: sqlite3.Row, payload: Mapping[str, object]
    ) -> bool:
        return all(
            (
                int(payload["member_id"]) == int(original["member_id"]),
                int(payload["category_id"]) == int(original["category_id"]),
                int(payload["amount_cents"]) == int(original["amount_cents"]),
                str(payload["business_name"]) == str(original["business_name"]),
                str(payload["description"]) == str(original["description"]),
                str(payload["occurred_at"]) == str(original["occurred_at"]),
            )
        )

    def _resolve_correction(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: str,
        transaction_id: str,
        member: str | None,
        category: str | None,
        amount: str | Decimal | None,
        business_name: str | None,
        description: str | None,
        occurred_at: str | None,
    ) -> tuple[sqlite3.Row, dict[str, object], dict[str, object], str]:
        request_id = self._validated_request_id(request_id)
        transaction_id = transaction_id.strip()
        if not transaction_id or len(transaction_id) > MAX_SEARCH_TEXT_LENGTH:
            raise BudgetValidationError("transaction_id is invalid")
        original = connection.execute(
            """
            SELECT entry.*, members.name AS member_name,
                   categories.name AS category_name,
                   parent.name AS parent_category_name,
                   reversed_by.id AS reversed_by_id,
                   COALESCE((SELECT SUM(-refund.amount_cents)
                             FROM ledger_entries refund
                             WHERE refund.refunds_entry_id = entry.id
                               AND refund.operation_type = 'refund'), 0) AS refunded_cents
            FROM ledger_entries entry
            JOIN members ON members.id = entry.member_id
            JOIN categories ON categories.id = entry.category_id
            LEFT JOIN categories parent ON parent.id = categories.parent_id
            LEFT JOIN ledger_entries reversed_by
              ON reversed_by.reverses_entry_id = entry.id
            WHERE entry.transaction_id = ? AND entry.operation_type = 'expense'
            """,
            (transaction_id,),
        ).fetchone()
        if original is None:
            raise BudgetValidationError("expense transaction was not found")
        resolved_member_id = (
            self._member_id(connection, member)
            if member is not None
            else original["member_id"]
        )
        resolved_category_id = (
            self._category_id(connection, category)
            if category is not None
            else original["category_id"]
        )
        resolved_cents = (
            _parse_cents(amount) if amount is not None else original["amount_cents"]
        )
        resolved_business = (
            self._bounded_entry_text(
                business_name, "business_name", MAX_BUSINESS_NAME_LENGTH
            )
            if business_name is not None
            else original["business_name"]
        )
        resolved_description = (
            self._bounded_entry_text(
                description, "description", MAX_ENTRY_DESCRIPTION_LENGTH
            )
            if description is not None
            else original["description"]
        )
        resolved_timestamp, resolved_month = (
            self._normalize_timestamp(occurred_at)
            if occurred_at is not None
            else (original["occurred_at"], original["local_month"])
        )
        persistence_payload = {
            "transaction_id": transaction_id,
            "member_id": resolved_member_id,
            "category_id": resolved_category_id,
            "amount_cents": resolved_cents,
            "business_name": resolved_business,
            "description": resolved_description,
            "occurred_at": resolved_timestamp,
        }
        confirmation_payload = {
            "confirmation_type": "correction",
            "request_id": request_id,
            "transaction_id": transaction_id,
            "member": connection.execute(
                "SELECT name FROM members WHERE id = ?", (resolved_member_id,)
            ).fetchone()["name"],
            "category": self._category_path(connection, resolved_category_id),
            "amount_cents": resolved_cents,
            "business_name": resolved_business,
            "description": resolved_description,
            "occurred_at": resolved_timestamp,
        }
        return original, persistence_payload, confirmation_payload, resolved_month

    def _split_confirmation_payload(
        self,
        connection: sqlite3.Connection,
        *,
        request_id: object,
        total_amount: object,
        allocations: object,
        business_name: object = "",
        description: object = "",
        occurred_at: object = None,
    ) -> dict[str, object]:
        if not isinstance(request_id, str):
            raise BudgetValidationError("request_id is required")
        if not isinstance(allocations, Sequence) or isinstance(
            allocations, (str, bytes)
        ):
            raise BudgetValidationError("allocations must be a list")
        if not 2 <= len(allocations) <= MAX_SPLIT_ALLOCATIONS:
            raise BudgetValidationError(
                f"allocations must contain between 2 and {MAX_SPLIT_ALLOCATIONS} items"
            )
        normalized: list[dict[str, object]] = []
        for index, allocation in enumerate(allocations, start=1):
            if not isinstance(allocation, Mapping):
                raise BudgetValidationError(f"allocation {index} must be an object")
            member_id = self._member_id(
                connection, str(allocation.get("member", ""))
            )
            category_id = self._category_id(
                connection, str(allocation.get("category", ""))
            )
            if allocation.get("amount") is None:
                raise BudgetValidationError(f"allocation {index} requires amount")
            normalized.append(
                {
                    "member": connection.execute(
                        "SELECT name FROM members WHERE id = ?", (member_id,)
                    ).fetchone()["name"],
                    "category": self._category_path(connection, category_id),
                    "amount_cents": _parse_cents(str(allocation["amount"])),
                }
            )
        total_cents = _parse_cents(str(total_amount))
        if sum(int(item["amount_cents"]) for item in normalized) != total_cents:
            raise BudgetValidationError(
                "allocation amounts must add up exactly to total_amount"
            )
        if not isinstance(business_name, str) or not isinstance(description, str):
            raise BudgetValidationError("business_name and description must be text")
        if occurred_at is not None and not isinstance(occurred_at, str):
            raise BudgetValidationError("occurred_at must be an ISO 8601 timestamp")
        return {
            "confirmation_type": "split_expense",
            "request_id": self._validated_request_id(request_id),
            "total_cents": total_cents,
            "allocations": normalized,
            "business_name": self._bounded_entry_text(
                business_name, "business_name", MAX_BUSINESS_NAME_LENGTH
            ),
            "description": self._bounded_entry_text(
                description, "description", MAX_ENTRY_DESCRIPTION_LENGTH
            ),
            "occurred_at": (
                self._normalize_timestamp(occurred_at)[0]
                if occurred_at is not None
                else None
            ),
        }

    @staticmethod
    def _category_path(connection: sqlite3.Connection, category_id: int) -> str:
        row = connection.execute(
            """
            SELECT child.name, parent.name AS parent_name
            FROM categories child
            LEFT JOIN categories parent ON parent.id = child.parent_id
            WHERE child.id = ?
            """,
            (category_id,),
        ).fetchone()
        if row is None:
            raise BudgetValidationError("category was not found")
        return "/".join(part for part in (row["parent_name"], row["name"]) if part)

    @classmethod
    def _split_result(
        cls,
        connection: sqlite3.Connection,
        group_id: str,
        total_cents: int,
        rows: Sequence[Mapping[str, object]],
        *,
        duplicate: bool,
    ) -> dict[str, object]:
        return {
            "split_transaction_id": group_id,
            "total_amount": cls._decimal_amount(total_cents),
            "currency": "USD",
            "duplicate": duplicate,
            "allocations": [
                cls._canonical_entry(connection, int(row["id"])) for row in rows
            ],
        }

    @staticmethod
    def _decimal_amount(cents: int) -> str:
        absolute = abs(cents)
        return f"{absolute // 100}.{absolute % 100:02d}"

    @staticmethod
    def _optional_search_text(value: str | None, name: str) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if len(cleaned) > MAX_SEARCH_TEXT_LENGTH:
            raise BudgetValidationError(
                f"{name} cannot exceed {MAX_SEARCH_TEXT_LENGTH} characters"
            )
        return cleaned

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _cursor_secret(connection: sqlite3.Connection) -> bytes:
        row = connection.execute(
            "SELECT value FROM ledger_metadata WHERE key = 'cursor_secret'"
        ).fetchone()
        if row is None:
            raise BudgetValidationError("transaction cursor state is unavailable")
        return row["value"].encode("ascii")

    @staticmethod
    def _confirmation_secret(connection: sqlite3.Connection) -> bytes:
        row = connection.execute(
            "SELECT value FROM ledger_metadata "
            "WHERE key = 'expense_confirmation_secret'"
        ).fetchone()
        if row is None:
            raise BudgetValidationError("expense confirmation state is unavailable")
        return row["value"].encode("ascii")

    @classmethod
    def _encode_confirmation(
        cls, connection: sqlite3.Connection, payload: Mapping[str, object]
    ) -> str:
        encoded_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        signature = hmac.new(
            cls._confirmation_secret(connection), encoded_payload, hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(signature + encoded_payload).decode("ascii")

    @classmethod
    def _decode_confirmation(
        cls, connection: sqlite3.Connection, token: str
    ) -> dict[str, object]:
        if not token or len(token) > 4096:
            raise BudgetValidationError("confirmation token is invalid")
        try:
            raw = base64.b64decode(token.encode("ascii"), altchars=b"-_", validate=True)
            signature, encoded_payload = raw[:32], raw[32:]
            expected_signature = hmac.new(
                cls._confirmation_secret(connection), encoded_payload, hashlib.sha256
            ).digest()
            if len(signature) != 32 or not hmac.compare_digest(signature, expected_signature):
                raise ValueError
            payload = json.loads(encoded_payload.decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("expires_at"), int):
                raise ValueError
            if payload["expires_at"] < int(datetime.now(timezone.utc).timestamp()):
                raise BudgetValidationError("confirmation token has expired")
        except BudgetValidationError:
            raise
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise BudgetValidationError("confirmation token is invalid") from exc
        return payload

    @classmethod
    def _encode_cursor(
        cls, connection: sqlite3.Connection, payload: Mapping[str, object]
    ) -> str:
        encoded_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(
            cls._cursor_secret(connection), encoded_payload, hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(signature + encoded_payload).decode("ascii")

    @classmethod
    def _decode_cursor(
        cls,
        connection: sqlite3.Connection,
        *,
        cursor: str,
        expected_filter_digest: str,
    ) -> dict[str, str]:
        try:
            raw = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
            signature, encoded_payload = raw[:32], raw[32:]
            expected_signature = hmac.new(
                cls._cursor_secret(connection), encoded_payload, hashlib.sha256
            ).digest()
            if len(signature) != 32 or not hmac.compare_digest(signature, expected_signature):
                raise ValueError
            payload = json.loads(encoded_payload.decode("utf-8"))
            if (
                not isinstance(payload, dict)
                or payload.get("filter_digest") != expected_filter_digest
                or not isinstance(payload.get("occurred_at"), str)
                or not isinstance(payload.get("transaction_id"), str)
            ):
                raise ValueError
        except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise BudgetValidationError("cursor is invalid or does not match the search") from exc
        return {
            "occurred_at": payload["occurred_at"],
            "transaction_id": payload["transaction_id"],
        }

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
    def _category_search_ids(connection: sqlite3.Connection, path: str) -> list[int]:
        parts = BudgetLedger._category_parts(path)
        if len(parts) == 1:
            root = connection.execute(
                "SELECT id FROM categories WHERE parent_id IS NULL AND name = ?",
                (parts[0],),
            ).fetchone()
            if root is None:
                raise BudgetValidationError(f"unknown category: {path}")
            rows = connection.execute(
                "SELECT id FROM categories WHERE id = ? OR parent_id = ?",
                (root["id"], root["id"]),
            ).fetchall()
            return [row["id"] for row in rows]
        row = connection.execute(
            """
            SELECT child.id
            FROM categories child
            JOIN categories parent ON parent.id = child.parent_id
            WHERE parent.name = ? AND child.name = ?
            """,
            parts,
        ).fetchone()
        if row is None:
            raise BudgetValidationError(f"unknown category: {path}")
        return [row["id"]]

    @staticmethod
    def _category_parts(path: str) -> list[str]:
        parts = [part.strip() for part in path.split("/") if part.strip()]
        if len(parts) not in (1, 2):
            raise BudgetValidationError("category must be 'Category' or 'Parent/Child'")
        return parts

    @staticmethod
    def _normalize_category_path(path: str) -> str:
        return "/".join(BudgetLedger._category_parts(path))

    @classmethod
    def _canonical_entry(
        cls, connection: sqlite3.Connection, entry_id: int
    ) -> dict[str, object]:
        row = connection.execute(
            """
            SELECT entry.id, entry.request_id, member.name AS member,
                   category.name AS category, parent.name AS parent_category,
                   entry.amount_cents, entry.description, entry.occurred_at,
                   entry.created_at, entry.transaction_id, entry.operation_type,
                   entry.business_name, entry.reverses_entry_id,
                   entry.refunds_entry_id, entry.corrects_entry_id,
                   entry.split_group_id,
                   reversal_original.transaction_id AS reversal_of_transaction_id,
                   refund_original.transaction_id AS refund_of_transaction_id,
                   reversed_by.transaction_id AS reversed_by_transaction_id,
                   corrected_original.transaction_id AS corrects_transaction_id,
                   corrected_by.transaction_id AS corrected_by_transaction_id,
                   split_operation.request_id AS split_request_id,
                   COALESCE((
                       SELECT SUM(-refund.amount_cents)
                       FROM ledger_entries refund
                       WHERE refund.refunds_entry_id = entry.id
                         AND refund.operation_type = 'refund'
                   ), 0) AS refunded_cents
            FROM ledger_entries entry
            JOIN members member ON member.id = entry.member_id
            JOIN categories category ON category.id = entry.category_id
            LEFT JOIN categories parent ON parent.id = category.parent_id
            LEFT JOIN ledger_entries reversal_original
              ON reversal_original.id = entry.reverses_entry_id
            LEFT JOIN ledger_entries refund_original
              ON refund_original.id = entry.refunds_entry_id
            LEFT JOIN ledger_entries reversed_by
              ON reversed_by.reverses_entry_id = entry.id
            LEFT JOIN ledger_entries corrected_original
              ON corrected_original.id = entry.corrects_entry_id
            LEFT JOIN ledger_entries corrected_by
              ON corrected_by.corrects_entry_id = entry.id
            LEFT JOIN multi_entry_operations split_operation
              ON split_operation.group_id = entry.split_group_id
             AND split_operation.operation_type = 'split_expense'
            WHERE entry.id = ?
            """,
            (entry_id,),
        ).fetchone()
        if row is None:
            raise BudgetValidationError("transaction was not found")
        category_path = "/".join(
            part for part in (row["parent_category"], row["category"]) if part
        )
        refunded_cents = int(row["refunded_cents"])
        remaining_cents = (
            max(int(row["amount_cents"]) - refunded_cents, 0)
            if row["operation_type"] == "expense"
            else None
        )
        if row["operation_type"] == "expense":
            status = "reversed" if row["reversed_by_transaction_id"] else "active"
            if refunded_cents == 0:
                refund_status = "none"
            elif remaining_cents == 0:
                refund_status = "full"
            else:
                refund_status = "partial"
        else:
            status = "active"
            refund_status = None
        return {
            "transaction_id": row["transaction_id"],
            "request_id": row["split_request_id"] or row["request_id"],
            "operation_type": row["operation_type"],
            "member": row["member"],
            "category": category_path,
            "amount": cls._decimal_amount(row["amount_cents"]),
            "currency": "USD",
            "business_name": row["business_name"] or None,
            "description": row["description"],
            "occurred_at": row["occurred_at"],
            "recorded_at": row["created_at"],
            "status": status,
            "reversal_of_transaction_id": row["reversal_of_transaction_id"],
            "reversed_by_transaction_id": row["reversed_by_transaction_id"],
            "corrects_transaction_id": row["corrects_transaction_id"],
            "corrected_by_transaction_id": row["corrected_by_transaction_id"],
            "split_transaction_id": row["split_group_id"],
            "refund_of_transaction_id": row["refund_of_transaction_id"],
            "refund_link_status": (
                "linked" if row["refunds_entry_id"] is not None else "unlinked"
            ) if row["operation_type"] == "refund" else None,
            "refund_status": refund_status,
            "refunded_amount": (
                cls._decimal_amount(refunded_cents)
                if row["operation_type"] == "expense"
                else None
            ),
            "remaining_refundable_amount": (
                cls._decimal_amount(remaining_cents)
                if remaining_cents is not None
                else None
            ),
        }

    @classmethod
    def _entry(
        cls, connection: sqlite3.Connection, entry_id: int, *, duplicate: bool
    ) -> dict[str, object]:
        legacy = connection.execute(
            """
            SELECT entry.id, entry.request_id, member.name AS member,
                   category.name AS category, parent.name AS parent_category,
                   entry.amount_cents, entry.description, entry.occurred_at,
                   entry.reverses_entry_id, entry.refunds_entry_id
            FROM ledger_entries entry
            JOIN members member ON member.id = entry.member_id
            JOIN categories category ON category.id = entry.category_id
            LEFT JOIN categories parent ON parent.id = category.parent_id
            WHERE entry.id = ?
            """,
            (entry_id,),
        ).fetchone()
        return {
            **dict(legacy),
            "duplicate": duplicate,
            "transaction": cls._canonical_entry(connection, entry_id),
        }
