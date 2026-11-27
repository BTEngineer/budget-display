"""Explicit, audited Christmas attribution of existing financial purchases.

These tables never post money. The financial ledger owns purchases, corrections,
reversals and refunds; attribution only explains their Christmas portions.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from .ledger import BudgetLedger, BudgetValidationError, DuplicateRequestError, _utc_now


MAX_COMPONENTS = 100
MAX_RECIPIENTS = 100
_TABLES = {
    "christmas_recipients": """CREATE TABLE christmas_recipients (
        recipient_id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        active INTEGER NOT NULL CHECK (active IN (0, 1)))""",
    "christmas_years": """CREATE TABLE christmas_years (
        year INTEGER PRIMARY KEY, budget_cents INTEGER CHECK (budget_cents >= 0),
        children_json TEXT NOT NULL)""",
    "christmas_attributions": """CREATE TABLE christmas_attributions (
        purchase_key TEXT PRIMARY KEY, year INTEGER NOT NULL REFERENCES christmas_years(year),
        payload_json TEXT NOT NULL)""",
    "christmas_refunds": """CREATE TABLE christmas_refunds (
        refund_entry_id INTEGER PRIMARY KEY REFERENCES ledger_entries(id),
        purchase_key TEXT NOT NULL REFERENCES christmas_attributions(purchase_key),
        payload_json TEXT NOT NULL)""",
    "christmas_history": """CREATE TABLE christmas_history (
        id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL,
        entity_key TEXT NOT NULL, payload_json TEXT NOT NULL,
        result_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
}
_COLUMNS = {
    "christmas_recipients": {"recipient_id", "name", "active"},
    "christmas_years": {"year", "budget_cents", "children_json"},
    "christmas_attributions": {"purchase_key", "year", "payload_json"},
    "christmas_refunds": {"refund_entry_id", "purchase_key", "payload_json"},
    "christmas_history": {
        "id", "request_id", "kind", "entity_key", "payload_json", "result_json", "created_at"
    },
}


def initialize_christmas_schema(connection: sqlite3.Connection) -> None:
    """Install additive tables atomically; never repair a used partial schema."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        marker = connection.execute(
            "SELECT value FROM ledger_metadata WHERE key = 'christmas_schema_version'"
        ).fetchone()
        existing = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'christmas_%'"
            ) if row["name"] in _TABLES
        }
        if marker is not None and marker["value"] != "1":
            raise BudgetValidationError("inconsistent Christmas schema: version marker")
        if existing != set(_TABLES) and (
            marker is not None or any(
                connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                for table in existing
            )
        ):
            raise BudgetValidationError("inconsistent Christmas schema: missing tables")
        for table, sql in _TABLES.items():
            if table not in existing:
                connection.execute(sql)
        _check_schema(connection)
        connection.execute(
            "INSERT OR IGNORE INTO ledger_metadata(key, value) "
            "VALUES ('christmas_schema_version', '1')"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _check_schema(connection: sqlite3.Connection) -> None:
    for table, expected in _COLUMNS.items():
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        if {row["name"] for row in rows} != expected:
            raise BudgetValidationError(f"inconsistent Christmas schema: {table} columns")
        actual_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()["sql"]
        normalize = lambda value: "".join(value.lower().split()).replace('"', '')
        if normalize(actual_sql) != normalize(_TABLES[table]):
            raise BudgetValidationError(f"inconsistent Christmas schema: {table} constraints")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _text(value: object, name: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise BudgetValidationError(f"{name} must contain 1 to {limit} characters")
    return value.strip()


def _cents(value: object, *, positive: bool = False) -> int:
    if not isinstance(value, (str, Decimal)):
        raise BudgetValidationError("amount must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise BudgetValidationError("amount must be a decimal monetary value") from exc
    if not parsed.is_finite() or parsed < 0 or (positive and parsed == 0):
        raise BudgetValidationError("amount must be positive" if positive else "amount cannot be negative")
    if parsed.as_tuple().exponent < -2:
        raise BudgetValidationError("amount cannot have more than two decimal places")
    if parsed > Decimal("92233720368547758.07"):
        raise BudgetValidationError("amount exceeds supported integer cents")
    return int(parsed * 100)


def _year(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 9999:
        raise BudgetValidationError("year must be an integer from 1 to 9999")
    return value


def _shares(total: int, weights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Allocate exact cents proportionally, breaking remainder ties by stable ID."""
    denominator = sum(item["amount_cents"] for item in weights)
    if not weights:
        return []
    if denominator == 0:
        return [{"recipient_id": item["recipient_id"], "amount_cents": 0} for item in weights]
    amounts = [total * item["amount_cents"] // denominator for item in weights]
    order = sorted(range(len(weights)), key=lambda index: (
        -(total * weights[index]["amount_cents"] % denominator), weights[index]["recipient_id"]
    ))
    for index in order[:total - sum(amounts)]:
        amounts[index] += 1
    return [{"recipient_id": item["recipient_id"], "amount_cents": amounts[index]}
            for index, item in enumerate(weights)]


def _remaining_shares(weights: list[dict[str, Any]], refunds: list[int]) -> list[dict[str, Any]]:
    """Round against original proportions without a refund increasing a share."""
    remaining = [dict(item) for item in weights]
    denominator = sum(item["amount_cents"] for item in weights)
    net = denominator
    for refunded in refunds:
        net -= refunded
        proposed = _shares(net, weights)
        amounts = [min(item["amount_cents"], remaining[index]["amount_cents"])
                   for index, item in enumerate(proposed)]
        # Hamilton rounding may increase a share by one cent as the total falls.
        # Reallocate those bounded rounding cents within each prior share's cap.
        for _ in range(net - sum(amounts)):
            eligible = [index for index in range(len(weights))
                        if amounts[index] < remaining[index]["amount_cents"]]
            selected = min(eligible, key=lambda index: (
                -(net * weights[index]["amount_cents"] - amounts[index] * denominator),
                weights[index]["recipient_id"],
            ))
            amounts[selected] += 1
        remaining = [{"recipient_id": item["recipient_id"], "amount_cents": amounts[index]}
                     for index, item in enumerate(weights)]
    return remaining


class ChristmasLedger:
    def __init__(self, ledger: BudgetLedger):
        self.ledger = ledger

    def _ready(self, connection: sqlite3.Connection) -> None:
        marker = connection.execute(
            "SELECT value FROM ledger_metadata WHERE key = 'christmas_schema_version'"
        ).fetchone()
        if marker is None or marker["value"] != "1":
            raise BudgetValidationError("Christmas schema is not initialized")
        _check_schema(connection)

    def _retry(self, connection: sqlite3.Connection, request_id: str,
               kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        existing = connection.execute(
            "SELECT kind, payload_json, result_json FROM christmas_history WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if existing is None:
            return None
        if existing["kind"] != kind or existing["payload_json"] != _json(payload):
            raise DuplicateRequestError("Christmas request_id already belongs to a different operation")
        return {**json.loads(existing["result_json"]), "duplicate": True}

    def _audit(self, connection: sqlite3.Connection, request_id: str, kind: str,
               key: str, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        result = {**result, "duplicate": False}
        connection.execute(
            "INSERT INTO christmas_history(request_id, kind, entity_key, payload_json, "
            "result_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (request_id, kind, key, _json(payload), _json(result), _utc_now()),
        )
        connection.commit()
        return result

    def _recipients(self, connection: sqlite3.Connection, ids: object) -> list[str]:
        if not isinstance(ids, list) or len(ids) > MAX_RECIPIENTS:
            raise BudgetValidationError(f"recipient IDs must be a list of at most {MAX_RECIPIENTS}")
        result = sorted(_text(value, "recipient_id") for value in ids)
        if len(set(result)) != len(result):
            raise BudgetValidationError("duplicate recipient IDs")
        for recipient_id in result:
            if connection.execute(
                "SELECT 1 FROM christmas_recipients WHERE recipient_id = ?", (recipient_id,)
            ).fetchone() is None:
                raise BudgetValidationError(f"unknown recipient: {recipient_id}")
        return result

    def upsert_recipient(self, *, request_id: str, name: str,
                         recipient_id: str | None = None, active: bool = True) -> dict[str, Any]:
        request_id = _text(request_id, "request_id")
        name = _text(name, "name", 80)
        if type(active) is not bool:
            raise BudgetValidationError("active must be boolean")
        if recipient_id is not None:
            recipient_id = _text(recipient_id, "recipient_id")
        payload = {"name": name, "recipient_id": recipient_id, "active": active}
        with self.ledger._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ready(connection)
            if (retry := self._retry(connection, request_id, "recipient", payload)) is not None:
                return retry
            if recipient_id is None:
                existing = connection.execute(
                    "SELECT recipient_id FROM christmas_recipients WHERE name = ? COLLATE NOCASE", (name,)
                ).fetchone()
                if existing is None and connection.execute(
                    "SELECT COUNT(*) FROM christmas_recipients"
                ).fetchone()[0] >= MAX_RECIPIENTS:
                    raise BudgetValidationError(f"at most {MAX_RECIPIENTS} reusable recipients are supported")
                recipient_id = existing["recipient_id"] if existing else f"recipient_{uuid4().hex}"
            elif connection.execute(
                "SELECT 1 FROM christmas_recipients WHERE recipient_id = ?", (recipient_id,)
            ).fetchone() is None:
                raise BudgetValidationError("unknown recipient_id; omit it to create a recipient")
            try:
                connection.execute(
                    "INSERT INTO christmas_recipients(recipient_id, name, active) VALUES (?, ?, ?) "
                    "ON CONFLICT(recipient_id) DO UPDATE SET name=excluded.name, active=excluded.active",
                    (recipient_id, name, int(active)),
                )
            except sqlite3.IntegrityError as exc:
                raise BudgetValidationError("recipient name already exists") from exc
            return self._audit(connection, request_id, "recipient", recipient_id, payload,
                               {"recipient_id": recipient_id, "name": name, "active": active})

    def list_recipients(self, *, include_inactive: bool = False) -> dict[str, Any]:
        if type(include_inactive) is not bool:
            raise BudgetValidationError("include_inactive must be boolean")
        with self.ledger._connection() as connection:
            self._ready(connection)
            rows = connection.execute(
                "SELECT * FROM christmas_recipients "
                + ("" if include_inactive else "WHERE active = 1 ") + "ORDER BY name, recipient_id"
            ).fetchall()
            return {"recipients": [{**dict(row), "active": bool(row["active"])} for row in rows]}

    def configure_year(self, *, request_id: str, year: int,
                       child_recipient_ids: list[str] | None = None,
                       budget_amount: str | None = None, clear_budget: bool = False) -> dict[str, Any]:
        request_id = _text(request_id, "request_id")
        year = _year(year)
        if type(clear_budget) is not bool or (clear_budget and budget_amount is not None):
            raise BudgetValidationError("clear_budget must be boolean and cannot accompany budget_amount")
        budget_cents = _cents(budget_amount) if budget_amount is not None else None
        with self.ledger._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ready(connection)
            children = self._recipients(connection, child_recipient_ids) if child_recipient_ids is not None else None
            payload = {"year": year, "child_recipient_ids": children,
                       "budget_cents": budget_cents, "clear_budget": clear_budget}
            if (retry := self._retry(connection, request_id, "year", payload)) is not None:
                return retry
            existing = connection.execute("SELECT * FROM christmas_years WHERE year = ?", (year,)).fetchone()
            if children is None:
                children = json.loads(existing["children_json"]) if existing else []
            if budget_amount is None and not clear_budget and existing:
                budget_cents = existing["budget_cents"]
            connection.execute(
                "INSERT INTO christmas_years(year,budget_cents,children_json) VALUES (?,?,?) "
                "ON CONFLICT(year) DO UPDATE SET budget_cents=excluded.budget_cents, children_json=excluded.children_json",
                (year, budget_cents, _json(children)),
            )
            return self._audit(connection, request_id, "year", str(year), payload,
                               {"year": year, "budget_cents": budget_cents, "child_recipient_ids": children})

    def _christmas_category_ids(self, connection: sqlite3.Connection) -> set[int]:
        return {
            row["id"] for row in connection.execute(
                "SELECT category.id, COALESCE(parent.name, category.name) AS root_name "
                "FROM categories category LEFT JOIN categories parent ON parent.id = category.parent_id"
            )
            if self.ledger.is_christmas_category(row["root_name"])
        }

    def _purchase(self, connection: sqlite3.Connection, transaction_id: str,
                  christmas_category_ids: set[int] | None = None) -> dict[str, Any]:
        transaction_id = _text(transaction_id, "transaction_id")
        entry = connection.execute(
            "SELECT * FROM ledger_entries WHERE transaction_id = ?", (transaction_id,)
        ).fetchone()
        if entry is None:
            entry = connection.execute(
                "SELECT * FROM ledger_entries WHERE split_group_id = ? ORDER BY id LIMIT 1", (transaction_id,)
            ).fetchone()
        if entry is None or entry["operation_type"] != "expense":
            raise BudgetValidationError("transaction must identify an existing expense or split purchase")
        while entry["corrects_entry_id"] is not None:
            entry = connection.execute("SELECT * FROM ledger_entries WHERE id = ?", (entry["corrects_entry_id"],)).fetchone()
        key = entry["split_group_id"] or entry["transaction_id"]
        if entry["split_group_id"]:
            current = connection.execute(
                "SELECT * FROM ledger_entries WHERE split_group_id = ? AND operation_type = 'expense' ORDER BY id", (key,)
            ).fetchall()
        else:
            while (replacement := connection.execute(
                "SELECT * FROM ledger_entries WHERE corrects_entry_id = ?", (entry["id"],)
            ).fetchone()) is not None:
                entry = replacement
            current = [entry]
        reversed_purchase = any(connection.execute(
            "SELECT 1 FROM ledger_entries WHERE reverses_entry_id = ?", (item["id"],)
        ).fetchone() for item in current)
        ids = [item["id"] for item in current]
        placeholders = ",".join("?" for _ in ids)
        refunds = connection.execute(
            f"SELECT * FROM ledger_entries WHERE refunds_entry_id IN ({placeholders}) "
            "AND operation_type = 'refund' ORDER BY id", ids
        ).fetchall()
        if christmas_category_ids is None:
            christmas_category_ids = self._christmas_category_ids(connection)
        christmas_entries = {item["id"] for item in current if item["category_id"] in christmas_category_ids}
        christmas_cents = sum(item["amount_cents"] for item in current if item["id"] in christmas_entries)
        christmas_refunds = {refund["id"] for refund in refunds if refund["refunds_entry_id"] in christmas_entries}
        return {"purchase_key": key, "purchase_cents": sum(item["amount_cents"] for item in current),
                "occurred_at": current[0]["occurred_at"], "refunds": refunds,
                "reversed": reversed_purchase,
                "christmas_category_cents": christmas_cents,
                "christmas_category_refund_ids": christmas_refunds,
                "christmas_category_remaining_cents": christmas_cents + sum(
                    refund["amount_cents"] for refund in refunds if refund["id"] in christmas_refunds
                ),
                "transaction_ids": [item["transaction_id"] for item in current]}

    def _gifts(self, connection: sqlite3.Connection, gifts: object) -> list[dict[str, Any]]:
        if not isinstance(gifts, list) or len(gifts) > MAX_COMPONENTS:
            raise BudgetValidationError(f"gifts must be a list of at most {MAX_COMPONENTS}")
        result = []
        for index, gift in enumerate(gifts, 1):
            if not isinstance(gift, dict) or set(gift) - {
                "gift_id", "description", "amount", "recipient_ids", "recipient_amounts"
            }:
                raise BudgetValidationError("invalid gift fields")
            gift_id = _text(gift.get("gift_id", f"gift_{index}"), "gift_id", 80)
            description = _text(gift.get("description"), "gift description", 500)
            amount = _cents(gift.get("amount"), positive=True)
            if "recipient_ids" in gift and "recipient_amounts" in gift:
                raise BudgetValidationError("provide recipient_ids or recipient_amounts, not both")
            if "recipient_amounts" in gift:
                custom = gift["recipient_amounts"]
                if not isinstance(custom, list) or not custom:
                    raise BudgetValidationError("recipient_amounts must be a nonempty list")
                if any(not isinstance(item, dict) or set(item) != {"recipient_id", "amount"} for item in custom):
                    raise BudgetValidationError("invalid recipient_amounts fields")
                self._recipients(connection, [item["recipient_id"] for item in custom])
                shares = sorted([{"recipient_id": item["recipient_id"].strip(),
                                  "amount_cents": _cents(item["amount"])} for item in custom],
                                key=lambda item: item["recipient_id"])
                if sum(item["amount_cents"] for item in shares) != amount:
                    raise BudgetValidationError("recipient amounts must add up exactly to gift amount")
            else:
                ids = self._recipients(connection, gift.get("recipient_ids", []))
                shares = _shares(amount, [{"recipient_id": value, "amount_cents": 1} for value in ids])
            result.append({"gift_id": gift_id, "description": description,
                           "amount_cents": amount, "recipient_shares": shares})
        if len({gift["gift_id"] for gift in result}) != len(result):
            raise BudgetValidationError("gift IDs must be unique within a purchase")
        return result

    def assign_purchase(self, *, request_id: str, transaction_id: str, gifts: list[dict[str, Any]],
                        overhead_amount: str = "0.00", ordinary_amount: str | None = None,
                        year: int | None = None, explicitly_requested: bool = False,
                        require_christmas_category: bool = False) -> dict[str, Any]:
        if explicitly_requested is not True:
            raise BudgetValidationError("Christmas attribution requires an explicit user request")
        request_id = _text(request_id, "request_id")
        # Store the requested defaults as well as the resolved payload for exact retries.
        request = {"transaction_id": _text(transaction_id, "transaction_id"), "gifts": gifts,
                   "overhead_amount": overhead_amount, "ordinary_amount": ordinary_amount,
                   "year": year, "explicitly_requested": True}
        with self.ledger._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ready(connection)
            purchase = self._purchase(connection, transaction_id) if require_christmas_category else None
            if purchase is not None and not purchase["christmas_category_cents"]:
                raise BudgetValidationError("transaction must be an expense in the Christmas category")
            if (retry := self._retry(connection, request_id, "purchase", request)) is not None:
                return retry
            if purchase is None:
                purchase = self._purchase(connection, transaction_id)
            if purchase["reversed"]:
                raise BudgetValidationError("a reversed purchase cannot receive Christmas attribution")
            existing = connection.execute(
                "SELECT year FROM christmas_attributions WHERE purchase_key=?", (purchase["purchase_key"],)
            ).fetchone()
            resolved_year = _year(year) if year is not None else (
                existing["year"] if existing else datetime.fromisoformat(
                    purchase["occurred_at"]
                ).astimezone(self.ledger.household_timezone).year
            )
            normalized = self._gifts(connection, gifts)
            overhead = _cents(overhead_amount)
            christmas = sum(item["amount_cents"] for item in normalized) + overhead
            ordinary = purchase["purchase_cents"] - christmas if ordinary_amount is None else _cents(ordinary_amount)
            if ordinary < 0 or christmas + ordinary != purchase["purchase_cents"]:
                raise BudgetValidationError("gift, overhead and ordinary amounts must equal the purchase total")
            categorized = purchase["christmas_category_cents"]
            if categorized and christmas and christmas != categorized:
                raise BudgetValidationError(
                    "gift and overhead amounts must equal the financial Christmas category portion"
                )
            payload = {"purchase_key": purchase["purchase_key"], "year": resolved_year,
                       "purchase_cents": purchase["purchase_cents"], "gifts": normalized,
                       "overhead_cents": overhead, "ordinary_cents": ordinary}
            connection.execute("INSERT OR IGNORE INTO christmas_years VALUES (?,NULL,'[]')", (resolved_year,))
            connection.execute(
                "INSERT INTO christmas_attributions(purchase_key,year,payload_json) VALUES (?,?,?) "
                "ON CONFLICT(purchase_key) DO UPDATE SET year=excluded.year,payload_json=excluded.payload_json",
                (purchase["purchase_key"], resolved_year, _json(payload)),
            )
            clear_audit = {}
            if christmas == 0:
                # Explicitly clearing Christmas also clears its linked return allocation.
                connection.executemany(
                    "INSERT INTO christmas_refunds(refund_entry_id,purchase_key,payload_json) VALUES (?,?,?) "
                    "ON CONFLICT(refund_entry_id) DO UPDATE SET payload_json=excluded.payload_json",
                    [(refund["id"], purchase["purchase_key"], _json({
                        "gifts": {}, "overhead_cents": 0, "ordinary_cents": -refund["amount_cents"],
                        **({"cleared_christmas_category": True} if categorized else {}),
                    })) for refund in purchase["refunds"]],
                )
                clear_audit = {
                    "cleared_refund_count": len(purchase["refunds"]),
                    "cleared_refund_max_entry_id": max((refund["id"] for refund in purchase["refunds"]), default=None),
                }
            self._evaluate(connection, payload)
            return self._audit(connection, request_id, "purchase", purchase["purchase_key"],
                               request, {**payload, **clear_audit})

    def resolve_refund(self, *, request_id: str, transaction_id: str, gifts: list[dict[str, Any]],
                       overhead_amount: str = "0.00", ordinary_amount: str = "0.00",
                       explicitly_requested: bool = False) -> dict[str, Any]:
        if explicitly_requested is not True:
            raise BudgetValidationError("Christmas refund attribution requires an explicit user request")
        request_id = _text(request_id, "request_id")
        transaction_id = _text(transaction_id, "transaction_id")
        request = {"transaction_id": transaction_id, "gifts": gifts,
                   "overhead_amount": overhead_amount, "ordinary_amount": ordinary_amount,
                   "explicitly_requested": True}
        with self.ledger._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ready(connection)
            if (retry := self._retry(connection, request_id, "refund", request)) is not None:
                return retry
            refund = connection.execute(
                "SELECT refund.*, original.transaction_id AS original_id FROM ledger_entries refund "
                "JOIN ledger_entries original ON original.id=refund.refunds_entry_id "
                "WHERE refund.transaction_id=? AND refund.operation_type='refund'", (transaction_id,)
            ).fetchone()
            if refund is None:
                raise BudgetValidationError("refund must be linked to a verified attributed purchase")
            purchase = self._purchase(connection, refund["original_id"])
            attribution = connection.execute(
                "SELECT payload_json FROM christmas_attributions WHERE purchase_key=?", (purchase["purchase_key"],)
            ).fetchone()
            if attribution is None:
                raise BudgetValidationError("original purchase has no explicit Christmas attribution")
            if not isinstance(gifts, list) or len(gifts) > MAX_COMPONENTS:
                raise BudgetValidationError("invalid refund gifts")
            amounts = {}
            for gift in gifts:
                if not isinstance(gift, dict) or set(gift) != {"gift_id", "amount"}:
                    raise BudgetValidationError("refund gifts require only gift_id and amount")
                gift_id = _text(gift["gift_id"], "gift_id", 80)
                if gift_id in amounts:
                    raise BudgetValidationError("duplicate refund gift_id")
                amounts[gift_id] = _cents(gift["amount"], positive=True)
            payload = {"gifts": amounts, "overhead_cents": _cents(overhead_amount),
                       "ordinary_cents": _cents(ordinary_amount)}
            christmas = sum(amounts.values()) + payload["overhead_cents"]
            if christmas + payload["ordinary_cents"] != -refund["amount_cents"]:
                raise BudgetValidationError("refund gift, overhead and ordinary amounts must equal the refund total")
            if purchase["christmas_category_cents"]:
                christmas_refund = refund["id"] in purchase["christmas_category_refund_ids"]
                if christmas != (-refund["amount_cents"] if christmas_refund else 0):
                    raise BudgetValidationError("refund attribution must match the linked financial category")
            connection.execute(
                "INSERT INTO christmas_refunds(refund_entry_id,purchase_key,payload_json) VALUES (?,?,?) "
                "ON CONFLICT(refund_entry_id) DO UPDATE SET payload_json=excluded.payload_json",
                (refund["id"], purchase["purchase_key"], _json(payload)),
            )
            self._evaluate(connection, json.loads(attribution["payload_json"]))
            return self._audit(connection, request_id, "refund", transaction_id, request,
                               {"transaction_id": transaction_id, "purchase_key": purchase["purchase_key"], **payload})

    def _evaluate(self, connection: sqlite3.Connection, attribution: dict[str, Any],
                  christmas_category_ids: set[int] | None = None) -> dict[str, Any]:
        purchase = self._purchase(connection, attribution["purchase_key"], christmas_category_ids)
        attributed_christmas = attribution["purchase_cents"] - attribution["ordinary_cents"]
        categorized = purchase["christmas_category_cents"]
        matches_category = bool(categorized and categorized == attributed_christmas)
        result = {**attribution, "transaction_ids": purchase["transaction_ids"], "warnings": [],
                  "unresolved_refund_cents": 0, "financially_complete": True, "annual_complete": True}
        gift_refunds = {gift["gift_id"]: 0 for gift in attribution["gifts"]}
        gift_refund_steps: dict[str, list[tuple[int, int]]] = {gift_id: [] for gift_id in gift_refunds}
        unresolved_steps = []
        overhead_refunds = ordinary_refunds = 0
        for refund in purchase["refunds"]:
            resolution = connection.execute(
                "SELECT payload_json FROM christmas_refunds WHERE refund_entry_id=?", (refund["id"],)
            ).fetchone()
            parts = json.loads(resolution["payload_json"]) if resolution else None
            if parts is None or (matches_category and parts.get("cleared_christmas_category")):
                if matches_category and refund["id"] not in purchase["christmas_category_refund_ids"]:
                    ordinary_refunds += -refund["amount_cents"]
                    continue
                result["unresolved_refund_cents"] += -refund["amount_cents"]
                unresolved_steps.append((refund["id"], -refund["amount_cents"]))
                continue
            for gift_id, amount in parts["gifts"].items():
                if gift_id not in gift_refunds:
                    raise BudgetValidationError("attribution must preserve gift IDs referenced by refund allocations")
                gift_refunds[gift_id] += amount
                gift_refund_steps[gift_id].append((refund["id"], amount))
            overhead_refunds += parts["overhead_cents"]
            ordinary_refunds += parts["ordinary_cents"]
        exceeds = any(gift_refunds[gift["gift_id"]] > gift["amount_cents"] for gift in attribution["gifts"])
        if exceeds or overhead_refunds > attribution["overhead_cents"] or ordinary_refunds > attribution["ordinary_cents"]:
            raise BudgetValidationError("refund allocations exceed the original gift, overhead or ordinary portion")
        refunded = sum(-refund["amount_cents"] for refund in purchase["refunds"])
        inactive = (
            purchase["reversed"] or refunded == purchase["purchase_cents"]
            or (matches_category and purchase["christmas_category_remaining_cents"] == 0)
        )
        if not inactive and result["unresolved_refund_cents"]:
            remaining_components = [
                ("gift", gift["gift_id"], gift["amount_cents"] - gift_refunds[gift["gift_id"]])
                for gift in attribution["gifts"]
            ] + [("overhead", None, attribution["overhead_cents"] - overhead_refunds),
                 ("ordinary", None, attribution["ordinary_cents"] - ordinary_refunds)]
            remaining_components = [
                item for item in remaining_components
                if item[2] > 0 and (not matches_category or item[0] != "ordinary")
            ]
            if len(remaining_components) == 1 and result["unresolved_refund_cents"] <= remaining_components[0][2]:
                kind, gift_id, _ = remaining_components[0]
                amount = result["unresolved_refund_cents"]
                if kind == "gift":
                    gift_refunds[gift_id] += amount
                    gift_refund_steps[gift_id].extend(unresolved_steps)
                elif kind == "overhead":
                    overhead_refunds += amount
                else:
                    ordinary_refunds += amount
                result["unresolved_refund_cents"] = 0
        if inactive:
            result["unresolved_refund_cents"] = 0
        elif purchase["purchase_cents"] != attribution["purchase_cents"]:
            result["financially_complete"] = False
            result["annual_complete"] = False
            result["warnings"].append("Financial correction changed the purchase total; update its Christmas attribution.")
        if not inactive and categorized and attributed_christmas and not matches_category:
            result["financially_complete"] = False
            result["annual_complete"] = False
            result["warnings"].append("Financial Christmas category and attribution amounts differ; update the details.")
        if not inactive and attributed_christmas and not categorized:
            result["warnings"].append(
                "Some attributed Christmas spending uses ordinary financial categories and counts toward the monthly allowance."
            )
        if result["unresolved_refund_cents"]:
            result["financially_complete"] = False
            if attribution["ordinary_cents"] and not matches_category:
                result["annual_complete"] = False
            result["warnings"].append("Partial refund allocation is unresolved; clarify gifts, overhead and ordinary portions.")
        result["net_gifts"] = []
        for gift in attribution["gifts"]:
            net = 0 if inactive else gift["amount_cents"] - gift_refunds[gift["gift_id"]]
            result["net_gifts"].append({**gift, "net_amount_cents": net,
                                        "net_recipient_shares": _shares(0, gift["recipient_shares"]) if inactive else
                                        _remaining_shares(gift["recipient_shares"], [amount for _, amount in
                                            sorted(gift_refund_steps[gift["gift_id"]])])})
        result["net_overhead_cents"] = 0 if inactive else attribution["overhead_cents"] - overhead_refunds
        result["net_christmas_cents"] = result["net_overhead_cents"] + sum(
            gift["net_amount_cents"] for gift in result["net_gifts"]
        )
        if not inactive and matches_category:
            result["net_christmas_cents"] = purchase["christmas_category_remaining_cents"]
        elif not attribution["ordinary_cents"] and result["annual_complete"]:
            result["net_christmas_cents"] -= result["unresolved_refund_cents"]
        result["reversed"] = purchase["reversed"]
        return result

    def _pending_activity(self, connection: sqlite3.Connection,
                          category_ids: set[int]) -> dict[str, int]:
        pending = {"pending_purchase_count": 0, "pending_purchase_cents": 0,
                   "pending_unlinked_refund_count": 0, "pending_unlinked_refund_cents": 0}
        if not category_ids:
            return pending
        placeholders = ",".join("?" for _ in category_ids)
        rows = connection.execute(
            f"SELECT DISTINCT COALESCE(entry.split_group_id, entry.transaction_id) AS purchase_id "
            f"FROM ledger_entries entry WHERE entry.category_id IN ({placeholders}) "
            "AND entry.operation_type = 'expense' AND NOT EXISTS ("
            "SELECT 1 FROM ledger_entries reversal WHERE reversal.reverses_entry_id = entry.id)",
            tuple(category_ids),
        )
        attributed = {
            row[0] for row in connection.execute(
                "SELECT purchase_key FROM christmas_attributions "
                "WHERE json_extract(payload_json, '$.purchase_cents') > json_extract(payload_json, '$.ordinary_cents')"
            )
        }
        for row in rows:
            if row["purchase_id"] in attributed:
                continue
            purchase = self._purchase(connection, row["purchase_id"], category_ids)
            if (not purchase["reversed"] and purchase["purchase_key"] not in attributed
                    and purchase["christmas_category_remaining_cents"] > 0):
                pending["pending_purchase_count"] += 1
                pending["pending_purchase_cents"] += purchase["christmas_category_remaining_cents"]
        refund = connection.execute(
            f"SELECT COUNT(*) AS count, COALESCE(SUM(-amount_cents), 0) AS cents "
            f"FROM ledger_entries WHERE category_id IN ({placeholders}) "
            "AND operation_type = 'refund' AND refunds_entry_id IS NULL",
            tuple(category_ids),
        ).fetchone()
        pending["pending_unlinked_refund_count"] = refund["count"]
        pending["pending_unlinked_refund_cents"] = refund["cents"]
        return pending

    def summary(self, *, year: int) -> dict[str, Any]:
        year = _year(year)
        with self.ledger._connection() as connection:
            connection.execute("BEGIN")
            self._ready(connection)
            config = connection.execute("SELECT * FROM christmas_years WHERE year=?", (year,)).fetchone()
            children = json.loads(config["children_json"]) if config else []
            recipients = {row["recipient_id"]: row["name"] for row in connection.execute("SELECT * FROM christmas_recipients")}
            totals = {recipient_id: 0 for recipient_id in children}
            known_spent = unassigned = unresolved = 0
            warnings = []
            category_ids = self._christmas_category_ids(connection)
            pending = self._pending_activity(connection, category_ids)
            complete = comparison_known = not (
                pending["pending_purchase_count"] or pending["pending_unlinked_refund_count"]
            )
            rows = connection.execute("SELECT payload_json FROM christmas_attributions WHERE year=?", (year,)).fetchall()
            for row in rows:
                evaluated = self._evaluate(connection, json.loads(row["payload_json"]), category_ids)
                known_spent += evaluated["net_christmas_cents"]
                unresolved += evaluated["unresolved_refund_cents"]
                complete = complete and evaluated["annual_complete"]
                comparison_known = comparison_known and evaluated["financially_complete"]
                warnings.extend(evaluated["warnings"])
                for gift in evaluated["net_gifts"]:
                    if not gift["recipient_shares"]:
                        unassigned += gift["net_amount_cents"]
                    for share in gift["net_recipient_shares"]:
                        totals[share["recipient_id"]] = totals.get(share["recipient_id"], 0) + share["amount_cents"]
            if unassigned:
                warnings.append("Unassigned gifts make the child comparison incomplete.")
            if not children:
                warnings.append("No children comparison group is configured for this year.")
            maximum = max((totals[value] for value in children), default=0)
            budget = config["budget_cents"] if config else None
            spent = known_spent if complete else None
            comparison_complete = comparison_known and not unassigned and bool(children)
            return {"year": year, "currency": "USD", "budget_cents": budget,
                    "spent_cents": spent, "known_spent_cents": known_spent,
                    "remaining_cents": budget - spent if budget is not None and spent is not None else None,
                    "overspent_cents": max(spent - budget, 0) if budget is not None and spent is not None else None,
                    "child_totals": [{"recipient_id": value, "name": recipients[value],
                                      "gift_value_cents": totals[value] if comparison_complete else None,
                                      "known_gift_value_cents": totals[value],
                                      "gap_to_highest_cents": maximum - totals[value] if comparison_complete else None}
                                     for value in children],
                    "by_recipient_cents": totals if comparison_known else None,
                    "unassigned_gift_cents": unassigned, "unresolved_refund_cents": unresolved,
                    "comparison_complete": comparison_complete,
                    "spending_complete": complete, **pending,
                    "warnings": list(dict.fromkeys(warnings)),
                    "coverage": "Annual totals use explicit attribution. Pending financial Christmas activity is shown separately across all years."}

    def details(self, *, year: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        year = _year(year)
        if (type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int
                or not 0 <= offset <= 9223372036854775807):
            raise BudgetValidationError("limit must be 1 to 200 and offset must be a supported nonnegative integer")
        with self.ledger._connection() as connection:
            connection.execute("BEGIN")
            self._ready(connection)
            rows = connection.execute(
                "SELECT payload_json FROM christmas_attributions WHERE year=? ORDER BY purchase_key LIMIT ? OFFSET ?",
                (year, limit + 1, offset),
            ).fetchall()
            items = [self._evaluate(connection, json.loads(row["payload_json"])) for row in rows[:limit]]
            history = connection.execute(
                "SELECT id, request_id, kind, entity_key, result_json, created_at FROM christmas_history "
                "WHERE (kind='year' AND entity_key=?) OR (kind='purchase' AND "
                "CAST(json_extract(result_json,'$.year') AS INTEGER)=?) "
                "OR (kind='refund' AND json_extract(result_json,'$.purchase_key') IN "
                "(SELECT purchase_key FROM christmas_attributions WHERE year=?)) "
                "ORDER BY id DESC LIMIT ? OFFSET ?", (str(year), year, year, limit + 1, offset)
            ).fetchall()
            return {"year": year, "attributions": items, "offset": offset,
                    "has_more": len(rows) > limit, "history_has_more": len(history) > limit,
                    "history": [{**{key: row[key] for key in ("id", "request_id", "kind", "entity_key", "created_at")},
                                 "result": json.loads(row["result_json"])} for row in history[:limit]]}
