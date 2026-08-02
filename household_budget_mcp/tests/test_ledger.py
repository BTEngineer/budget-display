from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from budget_display import BudgetLedger, BudgetValidationError, DuplicateRequestError


class BudgetLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(Path(self.temporary_directory.name) / "budget.db")
        self.ledger.initialize()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_initial_members_and_categories_accept_expected_expense(self) -> None:
        entry = self.ledger.add_expense(
            request_id="telegram-1",
            member="Member 2",
            category="Meals/Drinks",
            amount="8.00",
            description="Coffee",
            occurred_at="2026-08-01T09:00:00-04:00",
        )
        self.assertEqual(entry["amount_cents"], 800)
        self.assertEqual(entry["parent_category"], "Meals")
        self.assertEqual(entry["category"], "Drinks")

    def test_parent_category_requires_a_subcategory(self) -> None:
        with self.assertRaisesRegex(BudgetValidationError, "requires a subcategory"):
            self.ledger.add_expense(
                request_id="telegram-2",
                member="Member 1",
                category="Meals",
                amount="10.00",
            )

    def test_exact_duplicate_delivery_is_idempotent(self) -> None:
        values = dict(
            request_id="telegram-3",
            member="Member 1",
            category="Everyday",
            amount="40.00",
            occurred_at="2026-08-01T12:00:00+00:00",
        )
        first = self.ledger.add_expense(**values)
        second = self.ledger.add_expense(**values)
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["id"], second["id"])

    def test_duplicate_without_occurred_at_uses_stored_server_timestamp(self) -> None:
        with patch(
            "budget_display.ledger._utc_now",
            side_effect=(
                "2026-08-01T12:00:00+00:00",
                "2026-08-01T12:00:00+00:00",
                "2026-08-01T12:00:01+00:00",
            ),
        ):
            first = self.ledger.add_expense(
                request_id="timestamp-less-retry",
                member="Member 1",
                category="Everyday",
                amount="1.00",
            )
            duplicate = self.ledger.add_expense(
                request_id="timestamp-less-retry",
                member="Member 1",
                category="Everyday",
                amount="1.00",
            )
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["id"], duplicate["id"])

    def test_request_id_cannot_be_reused_for_different_payload(self) -> None:
        self.ledger.add_expense(
            request_id="telegram-4", member="Member 1", category="Occasional", amount="20"
        )
        with self.assertRaises(DuplicateRequestError):
            self.ledger.add_expense(
                request_id="telegram-4",
                member="Member 1",
                category="Occasional",
                amount="21",
            )

    def test_month_boundaries_and_member_totals(self) -> None:
        self.ledger.add_expense(
            request_id="july",
            member="Member 1",
            category="Everyday",
            amount="10",
            occurred_at="2026-07-31T23:59:59-04:00",
        )
        self.ledger.add_expense(
            request_id="august-tim",
            member="Member 1",
            category="Everyday",
            amount="20",
            occurred_at="2026-08-01T00:00:00-04:00",
        )
        self.ledger.add_expense(
            request_id="august-member-2",
            member="Member 2",
            category="Meals/Food",
            amount="30",
            occurred_at="2026-08-31T23:59:59-04:00",
        )
        august = self.ledger.list_spending(month="2026-08")
        self.assertEqual(august["spent_cents"], 5000)
        self.assertEqual(august["by_member_cents"], {"Member 1": 2000, "Member 2": 3000})
        self.assertEqual(august["by_category_cents"]["Everyday"], 2000)
        self.assertEqual(august["by_category_cents"]["Meals"], 3000)
        rows = {row["name"]: row for row in august["category_rows"]}
        self.assertEqual(rows["Everyday"]["budget_cents"], 100000)
        self.assertEqual(rows["Everyday"]["by_member_cents"]["Member 1"], 2000)
        self.assertEqual(rows["Meals"]["spent_cents"], 3000)
        self.assertEqual(rows["Meals"]["by_member_cents"]["Member 2"], 3000)
        self.assertEqual(rows["Food"]["spent_cents"], 3000)
        self.assertIsNone(rows["Food"]["budget_cents"])

    def test_month_uses_household_timezone_not_raw_offset_text(self) -> None:
        self.ledger.add_expense(
            request_id="local-july",
            member="Member 1",
            category="Everyday",
            amount="12",
            occurred_at="2026-08-01T03:30:00+00:00",
        )
        self.assertEqual(self.ledger.list_spending(month="2026-07")["spent_cents"], 1200)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 0)

    def test_undo_is_a_reversal_and_is_idempotent(self) -> None:
        self.ledger.add_expense(
            request_id="telegram-5",
            member="Member 2",
            category="Occasional",
            amount="55.25",
            occurred_at="2026-08-10T12:00:00+00:00",
        )
        reversal = self.ledger.undo_last_expense(
            request_id="telegram-6", member="Member 2"
        )
        duplicate = self.ledger.undo_last_expense(
            request_id="telegram-6", member="Member 2"
        )
        self.assertEqual(reversal["amount_cents"], -5525)
        self.assertIsNotNone(reversal["reverses_entry_id"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 0)

    def test_cross_month_undo_reverses_the_original_month(self) -> None:
        self.ledger.add_expense(
            request_id="july-expense",
            member="Member 1",
            category="Everyday",
            amount="10.00",
            occurred_at="2026-07-31T20:00:00-04:00",
        )
        with patch(
            "budget_display.ledger._utc_now",
            return_value="2026-08-01T12:00:00+00:00",
        ):
            self.ledger.undo_last_expense(
                request_id="august-undo", member="Member 1"
            )
        self.assertEqual(self.ledger.list_spending(month="2026-07")["spent_cents"], 0)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 0)

    def test_initialize_repairs_legacy_cross_month_reversal(self) -> None:
        self.ledger.add_expense(
            request_id="legacy-original",
            member="Member 1",
            category="Everyday",
            amount="10.00",
            occurred_at="2026-07-31T20:00:00-04:00",
        )
        self.ledger.undo_last_expense(request_id="legacy-undo", member="Member 1")
        with self.ledger._connection() as connection:
            connection.execute(
                "UPDATE ledger_entries SET local_month = '2026-08' WHERE request_id = ?",
                ("legacy-undo",),
            )
            connection.commit()
        self.ledger.initialize()
        self.assertEqual(self.ledger.list_spending(month="2026-07")["spent_cents"], 0)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 0)

    def test_undo_request_id_cannot_be_reused_for_another_member(self) -> None:
        self.ledger.add_expense(
            request_id="member-one-expense",
            member="Member 1",
            category="Everyday",
            amount="10.00",
        )
        self.ledger.undo_last_expense(
            request_id="member-one-undo", member="Member 1"
        )
        with self.assertRaises(DuplicateRequestError):
            self.ledger.undo_last_expense(
                request_id="member-one-undo", member="Member 2"
            )

    def test_dashboard_undo_targets_the_selected_entry(self) -> None:
        older = self.ledger.add_expense(
            request_id="older",
            member="Member 1",
            category="Everyday",
            amount="10.00",
            occurred_at="2026-08-01T12:00:00-04:00",
        )
        newer = self.ledger.add_expense(
            request_id="newer",
            member="Member 1",
            category="Everyday",
            amount="20.00",
            occurred_at="2026-08-02T12:00:00-04:00",
        )
        reversal = self.ledger.undo_last_expense(
            request_id="undo-older", member="Member 1", entry_id=older["id"]
        )
        self.assertEqual(reversal["reverses_entry_id"], older["id"])
        self.assertEqual(
            [entry["id"] for entry in self.ledger.list_recent_expenses(limit=10)],
            [newer["id"]],
        )

    def test_initial_monthly_limits_total_1800(self) -> None:
        summary = self.ledger.list_spending(month="2026-08")
        self.assertEqual(summary["budget_cents"], 180000)
        self.assertEqual(summary["remaining_cents"], 180000)

    def test_month_override_does_not_change_other_months(self) -> None:
        self.ledger.list_spending(month="2026-07")
        self.ledger.set_monthly_budget(
            month="2026-08", category="Occasional", amount="700"
        )
        august = self.ledger.list_spending(month="2026-08")
        july = self.ledger.list_spending(month="2026-07")
        self.assertEqual(august["budget_cents"], 200000)
        self.assertEqual(july["budget_cents"], 180000)

    def test_default_change_applies_only_to_unsnapshotted_months(self) -> None:
        august_before = self.ledger.list_spending(month="2026-08")
        self.ledger.set_default_monthly_budget(category="Everyday", amount="1100")
        august_after = self.ledger.list_spending(month="2026-08")
        september = self.ledger.list_spending(month="2026-09")
        self.assertEqual(august_before["budget_cents"], 180000)
        self.assertEqual(august_after["budget_cents"], 180000)
        self.assertEqual(september["budget_cents"], 190000)

    def test_subcategory_budget_is_rejected_to_avoid_double_counting(self) -> None:
        with self.assertRaisesRegex(BudgetValidationError, "top-level categories"):
            self.ledger.set_monthly_budget(
                month="2026-08", category="Meals/Drinks", amount="100"
            )

    def test_configuration_change_preserves_history_and_old_month_budget(self) -> None:
        self.ledger.add_expense(
            request_id="before-config-change",
            member="Member 1",
            category="Everyday",
            amount="25.00",
            occurred_at="2026-08-10T12:00:00-04:00",
        )
        before = self.ledger.list_spending(month="2026-08")

        reconfigured = BudgetLedger(
            self.ledger.database,
            members=("New Member",),
            categories=(("New Category", None, 42_000),),
        )
        reconfigured.initialize()
        after = reconfigured.list_spending(month="2026-08")

        self.assertEqual(after["spent_cents"], before["spent_cents"])
        self.assertEqual(after["budget_cents"], before["budget_cents"])
        self.assertEqual(after["by_member_cents"]["Member 1"], 2500)
        self.assertEqual(after["by_category_cents"]["Everyday"], 2500)
        self.assertEqual(
            reconfigured.list_spending(month="2026-09")["budget_cents"], 42000
        )
        with self.assertRaisesRegex(BudgetValidationError, "unknown household member"):
            reconfigured.add_expense(
                request_id="old-member",
                member="Member 1",
                category="New Category",
                amount="1.00",
            )

    def test_adding_subcategory_preserves_direct_parent_category_totals(self) -> None:
        database = Path(self.temporary_directory.name) / "category-history.db"
        original = BudgetLedger(
            database,
            categories=(("Meals", None, 30_000),),
        )
        original.initialize()
        original.add_expense(
            request_id="direct-parent-expense",
            member="Member 1",
            category="Meals",
            amount="10.00",
            occurred_at="2026-08-01T12:00:00+00:00",
        )

        reconfigured = BudgetLedger(
            database,
            categories=(("Meals", None, 30_000), ("Food", "Meals", None)),
        )
        reconfigured.initialize()
        summary = reconfigured.list_spending(month="2026-08")
        self.assertEqual(summary["spent_cents"], 1000)
        self.assertEqual(summary["by_category_cents"]["Meals"], 1000)

    def test_root_category_can_share_a_name_with_a_child(self) -> None:
        ledger = BudgetLedger(
            Path(self.temporary_directory.name) / "same-name.db",
            categories=(
                ("Food", None, 10_000),
                ("Meals", None, 20_000),
                ("Food", "Meals", None),
            ),
        )
        ledger.initialize()
        entry = ledger.add_expense(
            request_id="root-food",
            member="Member 1",
            category="Food",
            amount="1.00",
            occurred_at="2026-08-15T12:00:00-04:00",
        )
        self.assertEqual(entry["category"], "Food")
        self.assertIsNone(entry["parent_category"])
        rows = ledger.list_spending(month="2026-08")["category_rows"]
        root_food = next(
            row for row in rows if row["name"] == "Food" and row["parent"] is None
        )
        self.assertEqual(root_food["spent_cents"], 100)

    def test_receipt_draft_requires_confirmation_and_is_idempotent(self) -> None:
        draft = self.ledger.register_receipt(
            digest="a" * 64,
            relative_path="receipts/a.jpg",
            content_type="image/jpeg",
            byte_size=10,
            original_filename="receipt.jpg",
        )
        duplicate = self.ledger.register_receipt(
            digest="a" * 64,
            relative_path="receipts/a.jpg",
            content_type="image/jpeg",
            byte_size=10,
            original_filename="receipt-copy.jpg",
        )
        self.assertFalse(draft["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        analyzed = self.ledger.update_receipt_draft(
            draft_id=draft["id"],
            ai_entity_id="ai_task.openai",
            fields={"merchant": "Market", "total": "12.34", "suggested_category": "Everyday"},
        )
        self.assertEqual(analyzed["status"], "analyzed")
        confirmed = self.ledger.confirm_receipt_draft(
            draft_id=draft["id"],
            request_id="receipt-confirm-1",
            member="Member 1",
            category="Everyday",
            amount="12.34",
            description="Market",
        )
        retried = self.ledger.confirm_receipt_draft(
            draft_id=draft["id"],
            request_id="receipt-confirm-1",
            member="Member 1",
            category="Everyday",
            amount="12.34",
            description="Market",
        )
        self.assertEqual(confirmed["draft"]["status"], "confirmed")
        self.assertEqual(confirmed["entry"]["transaction"]["business_name"], "Market")
        self.assertTrue(retried["entry"]["duplicate"])
        self.assertEqual(len(self.ledger.list_recent_expenses(limit=10)), 1)

    def test_confirmed_receipt_rejects_a_different_request_id(self) -> None:
        draft = self.ledger.register_receipt(
            digest="b" * 64,
            relative_path="receipts/b.jpg",
            content_type="image/jpeg",
            byte_size=10,
            original_filename="receipt.jpg",
        )
        self.ledger.confirm_receipt_draft(
            draft_id=draft["id"], request_id="first", member="Member 1",
            category="Everyday", amount="1.00"
        )
        with self.assertRaises(DuplicateRequestError):
            self.ledger.confirm_receipt_draft(
                draft_id=draft["id"], request_id="second", member="Member 1",
                category="Everyday", amount="1.00"
            )

    def test_failed_receipt_confirmation_releases_request_claim(self) -> None:
        draft = self.ledger.register_receipt(
            digest="c" * 64,
            relative_path="receipts/c.jpg",
            content_type="image/jpeg",
            byte_size=10,
            original_filename="receipt.jpg",
        )
        with self.assertRaises(BudgetValidationError):
            self.ledger.confirm_receipt_draft(
                draft_id=draft["id"], request_id="invalid", member="Member 1",
                category="Everyday", amount="not-money"
            )
        confirmed = self.ledger.confirm_receipt_draft(
            draft_id=draft["id"], request_id="corrected", member="Member 1",
            category="Everyday", amount="1.00"
        )
        self.assertEqual(confirmed["draft"]["confirmation_request_id"], "corrected")

    def test_canonical_expense_includes_business_and_refundable_balance(self) -> None:
        entry = self.ledger.add_expense(
            request_id="canonical-expense",
            member="Member 1",
            category="Meals/Food",
            amount="24.30",
            business_name="North Market",
            description="Groceries",
            occurred_at="2026-08-01T12:00:00-04:00",
        )
        canonical = entry["transaction"]
        self.assertTrue(canonical["transaction_id"].startswith("txn_"))
        self.assertEqual(canonical["category"], "Meals/Food")
        self.assertEqual(canonical["business_name"], "North Market")
        self.assertEqual(canonical["amount"], "24.30")
        self.assertEqual(canonical["refund_status"], "none")
        self.assertEqual(canonical["remaining_refundable_amount"], "24.30")

    def test_partial_and_full_linked_refunds_are_audited_and_idempotent(self) -> None:
        original = self.ledger.add_expense(
            request_id="refund-original",
            member="Member 1",
            category="Everyday",
            amount="20.00",
            business_name="Hardware Shop",
            occurred_at="2026-07-31T18:00:00-04:00",
        )
        transaction_id = original["transaction"]["transaction_id"]
        partial = self.ledger.refund_expense(
            request_id="refund-partial",
            expense_id=transaction_id,
            amount="7.50",
            occurred_at="2026-08-01T10:00:00-04:00",
        )
        duplicate = self.ledger.refund_expense(
            request_id="refund-partial",
            expense_id=transaction_id,
            amount="7.50",
            occurred_at="2026-08-01T10:00:00-04:00",
        )
        self.assertEqual(partial["amount_cents"], -750)
        self.assertTrue(duplicate["duplicate"])
        after_partial = self.ledger.search_transactions(
            transaction_id=transaction_id, status="all"
        )["transactions"][0]
        self.assertEqual(after_partial["refund_status"], "partial")
        self.assertEqual(after_partial["remaining_refundable_amount"], "12.50")

        full = self.ledger.refund_expense(
            request_id="refund-full",
            expense_id=transaction_id,
            amount="12.50",
            occurred_at="2026-08-02T10:00:00-04:00",
        )
        self.assertEqual(full["transaction"]["refund_link_status"], "linked")
        after_full = self.ledger.search_transactions(
            transaction_id=transaction_id, status="all"
        )["transactions"][0]
        self.assertEqual(after_full["refund_status"], "full")
        self.assertEqual(after_full["refunded_amount"], "20.00")
        self.assertEqual(self.ledger.list_spending(month="2026-07")["spent_cents"], 2000)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], -2000)

        with self.assertRaisesRegex(BudgetValidationError, "remaining refundable"):
            self.ledger.refund_expense(
                request_id="refund-too-much",
                expense_id=transaction_id,
                amount="0.01",
            )

    def test_unlinked_refund_and_refunded_expense_cannot_be_undone(self) -> None:
        original = self.ledger.add_expense(
            request_id="expense-before-refund",
            member="Member 2",
            category="Occasional",
            amount="15.00",
        )
        self.ledger.refund_expense(
            request_id="linked-refund",
            expense_id=original["transaction"]["transaction_id"],
            amount="5.00",
        )
        with self.assertRaisesRegex(BudgetValidationError, "no expense is available"):
            self.ledger.undo_last_expense(
                request_id="undo-refunded",
                member="Member 2",
                entry_id=original["id"],
            )

        unlinked = self.ledger.refund_expense(
            request_id="unlinked-refund",
            amount="3.25",
            member="Member 2",
            category="Meals/Drinks",
            business_name="Cafe",
        )
        self.assertEqual(unlinked["transaction"]["refund_link_status"], "unlinked")
        self.assertIsNone(unlinked["transaction"]["refund_of_transaction_id"])
        duplicate = self.ledger.refund_expense(
            request_id="unlinked-refund",
            amount="3.25",
            member="Member 2",
            category="Meals/Drinks",
            business_name="Cafe",
        )
        self.assertTrue(duplicate["duplicate"])
        with self.assertRaises(DuplicateRequestError):
            self.ledger.refund_expense(
                request_id="unlinked-refund",
                amount="3.25",
                member="Member 2",
                category="Meals/Drinks",
                business_name="Different Cafe",
            )

    def test_search_filters_category_business_and_literal_wildcards(self) -> None:
        self.ledger.add_expense(
            request_id="search-one",
            member="Member 1",
            category="Meals/Food",
            amount="10.00",
            business_name="A_100% Market",
            description="Weekly food",
            occurred_at="2026-08-01T10:00:00-04:00",
        )
        self.ledger.add_expense(
            request_id="search-two",
            member="Member 2",
            category="Meals/Drinks",
            amount="5.00",
            business_name="Coffee House",
            description="Morning drink",
            occurred_at="2026-08-02T10:00:00-04:00",
        )
        parent = self.ledger.search_transactions(category="Meals")
        self.assertEqual(parent["count"], 2)
        literal = self.ledger.search_transactions(business_name="_100%")
        self.assertEqual(literal["count"], 1)
        combined = self.ledger.search_transactions(
            member="Member 2", category="Meals/Drinks", description_query="drink"
        )
        self.assertEqual(combined["count"], 1)
        self.assertEqual(combined["transactions"][0]["business_name"], "Coffee House")

    def test_search_cursor_is_stable_bound_to_filters_and_tamper_evident(self) -> None:
        for index in range(3):
            self.ledger.add_expense(
                request_id=f"page-{index}",
                member="Member 1",
                category="Everyday",
                amount="1.00",
                occurred_at="2026-08-01T12:00:00-04:00",
            )
        first = self.ledger.search_transactions(
            category="Everyday", sort_order="ascending", limit=2
        )
        self.assertTrue(first["has_more"])
        second = self.ledger.search_transactions(
            category="Everyday",
            sort_order="ascending",
            limit=2,
            cursor=first["next_cursor"],
        )
        ids = [row["transaction_id"] for row in first["transactions"] + second["transactions"]]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 3)
        with self.assertRaisesRegex(BudgetValidationError, "does not match"):
            self.ledger.search_transactions(
                member="Member 2", limit=2, cursor=first["next_cursor"]
            )
        tampered = first["next_cursor"][:-1] + ("A" if first["next_cursor"][-1] != "A" else "B")
        with self.assertRaisesRegex(BudgetValidationError, "cursor is invalid"):
            self.ledger.search_transactions(
                category="Everyday",
                sort_order="ascending",
                limit=2,
                cursor=tampered,
            )

    def test_search_time_amount_identifier_and_status_filters(self) -> None:
        first = self.ledger.add_expense(
            request_id="filter-first",
            member="Member 1",
            category="Everyday",
            amount="10.00",
            occurred_at="2026-08-01T00:00:00-04:00",
        )
        second = self.ledger.add_expense(
            request_id="filter-second",
            member="Member 1",
            category="Everyday",
            amount="20.00",
            occurred_at="2026-08-02T00:00:00-04:00",
        )
        self.ledger.undo_last_expense(
            request_id="filter-undo", member="Member 1", entry_id=second["id"]
        )
        bounded = self.ledger.search_transactions(
            start_at="2026-08-01T00:00:00-04:00",
            end_at="2026-08-02T00:00:00-04:00",
            operation_type="expense",
            status="all",
        )
        self.assertEqual(
            [row["transaction_id"] for row in bounded["transactions"]],
            [first["transaction"]["transaction_id"]],
        )
        reversed_expense = self.ledger.search_transactions(
            minimum_amount="15.00",
            maximum_amount="20.00",
            operation_type="expense",
            status="reversed",
        )
        self.assertEqual(reversed_expense["count"], 1)
        exact = self.ledger.search_transactions(
            request_id="filter-second", status="all"
        )
        self.assertEqual(exact["transactions"][0]["status"], "reversed")

    def test_initialize_migrates_legacy_entries_to_stable_canonical_records(self) -> None:
        database = Path(self.temporary_directory.name) / "legacy.db"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                CREATE TABLE members (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE categories (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE,
                    parent_id INTEGER REFERENCES categories(id),
                    active INTEGER NOT NULL DEFAULT 1,
                    UNIQUE (parent_id, name)
                );
                CREATE TABLE ledger_entries (
                    id INTEGER PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    member_id INTEGER NOT NULL REFERENCES members(id),
                    category_id INTEGER NOT NULL REFERENCES categories(id),
                    amount_cents INTEGER NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL,
                    local_month TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reverses_entry_id INTEGER UNIQUE REFERENCES ledger_entries(id)
                );
                INSERT INTO members(id, name) VALUES (1, 'Member 1');
                INSERT INTO categories(id, name, parent_id) VALUES (1, 'Everyday', NULL);
                INSERT INTO ledger_entries(
                    id, request_id, member_id, category_id, amount_cents,
                    description, occurred_at, local_month, created_at,
                    reverses_entry_id
                ) VALUES
                    (1, 'legacy-expense', 1, 1, 1000, 'Old purchase',
                     '2026-08-01T12:00:00+00:00', '2026-08',
                     '2026-08-01T12:00:01+00:00', NULL),
                    (2, 'legacy-undo', 1, 1, -1000, 'Undo: Old purchase',
                     '2026-08-02T12:00:00+00:00', '2026-08',
                     '2026-08-02T12:00:01+00:00', 1);
                """
            )
            connection.commit()
        finally:
            connection.close()
        ledger = BudgetLedger(database)
        ledger.initialize()
        first = ledger.search_transactions(status="all", sort_order="ascending")
        self.assertEqual(
            [row["operation_type"] for row in first["transactions"]],
            ["expense", "reversal"],
        )
        ids = [row["transaction_id"] for row in first["transactions"]]
        self.assertTrue(all(value.startswith("txn_") for value in ids))
        ledger.initialize()
        second = ledger.search_transactions(status="all", sort_order="ascending")
        self.assertEqual(ids, [row["transaction_id"] for row in second["transactions"]])

    def test_confirmation_token_is_bound_to_exact_expense(self) -> None:
        values = {
            "request_id": "confirm-1",
            "member": "Member 1",
            "category": "Everyday",
            "amount": "600.00",
            "business_name": "Furniture Store",
            "description": "Desk",
            "occurred_at": "2026-08-01T09:00:00-04:00",
        }
        prepared = self.ledger.prepare_expense(**values)
        self.ledger.validate_expense_confirmation(
            token=prepared["confirmation_token"], **values
        )
        with self.assertRaisesRegex(BudgetValidationError, "does not match"):
            self.ledger.validate_expense_confirmation(
                token=prepared["confirmation_token"],
                **{**values, "amount": "601.00"},
            )
        with self.assertRaisesRegex(BudgetValidationError, "invalid"):
            self.ledger.validate_expense_confirmation(token="tampered", **values)

    def test_correction_reverses_and_replaces_atomically_and_idempotently(self) -> None:
        original = self.ledger.add_expense(
            request_id="original-correction",
            member="Member 1",
            category="Everyday",
            amount="20.00",
            business_name="Shop",
            occurred_at="2026-08-02T09:00:00-04:00",
        )
        values = {
            "request_id": "correction-1",
            "transaction_id": original["transaction"]["transaction_id"],
            "category": "Occasional",
            "amount": "25.00",
            "description": "Corrected purchase",
        }
        corrected = self.ledger.correct_expense(**values)
        duplicate = self.ledger.correct_expense(**values)
        self.assertFalse(corrected["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(corrected["replacement"], duplicate["replacement"])
        self.assertEqual(corrected["replacement"]["category"], "Occasional")
        self.assertEqual(corrected["replacement"]["amount"], "25.00")
        self.assertEqual(
            corrected["replacement"]["corrects_transaction_id"],
            original["transaction"]["transaction_id"],
        )
        self.assertEqual(
            corrected["original"]["corrected_by_transaction_id"],
            corrected["replacement"]["transaction_id"],
        )
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 2500)
        with self.assertRaisesRegex(DuplicateRequestError, "different ledger operation"):
            self.ledger.correct_expense(**{**values, "amount": "26.00"})

    def test_refunded_expense_cannot_be_corrected(self) -> None:
        original = self.ledger.add_expense(
            request_id="refund-before-correction",
            member="Member 1",
            category="Everyday",
            amount="20.00",
        )
        self.ledger.refund_expense(
            request_id="refund-partial",
            expense_id=original["transaction"]["transaction_id"],
            amount="1.00",
        )
        with self.assertRaisesRegex(BudgetValidationError, "refunded"):
            self.ledger.correct_expense(
                request_id="correction-refunded",
                transaction_id=original["transaction"]["transaction_id"],
                amount="19.00",
            )

    def test_split_expense_is_atomic_balanced_and_idempotent(self) -> None:
        values = {
            "request_id": "split-1",
            "total_amount": "30.00",
            "allocations": [
                {"member": "Member 1", "category": "Everyday", "amount": "10.00"},
                {"member": "Member 2", "category": "Meals/Food", "amount": "20.00"},
            ],
            "business_name": "Warehouse",
            "occurred_at": "2026-08-03T09:00:00-04:00",
        }
        first = self.ledger.add_split_expense(**values)
        duplicate = self.ledger.add_split_expense(**values)
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["split_transaction_id"], duplicate["split_transaction_id"])
        self.assertEqual(len(first["allocations"]), 2)
        self.assertTrue(
            all(row["request_id"] == "split-1" for row in first["allocations"])
        )
        searched = self.ledger.search_transactions(request_id="split-1")
        self.assertEqual(searched["count"], 2)
        self.assertTrue(
            all(row["split_transaction_id"] for row in searched["transactions"])
        )
        summary = self.ledger.list_spending(month="2026-08")
        self.assertEqual(summary["spent_cents"], 3000)
        self.assertEqual(summary["by_member_cents"]["Member 1"], 1000)
        self.assertEqual(summary["by_member_cents"]["Member 2"], 2000)
        with self.assertRaisesRegex(BudgetValidationError, "add up exactly"):
            self.ledger.add_split_expense(
                request_id="split-invalid",
                total_amount="31.00",
                allocations=values["allocations"],
            )
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 3000)

    def test_classification_uses_aliases_and_history_without_writing(self) -> None:
        database = Path(self.temporary_directory.name) / "classification.db"
        ledger = BudgetLedger(
            database,
            classification_aliases=(("corner shop", "Meals/Food"),),
        )
        ledger.initialize()
        alias = ledger.suggest_expense_classification(business_name="Corner Shop")
        self.assertEqual(alias["suggestions"][0]["category"], "Meals/Food")
        self.assertEqual(alias["suggestions"][0]["confidence"], "high")
        ledger.add_expense(
            request_id="classification-history",
            member="Member 1",
            category="Occasional",
            amount="4.00",
            business_name="Book Barn",
        )
        history = ledger.suggest_expense_classification(business_name="Book Barn")
        self.assertEqual(history["suggestions"][0]["category"], "Occasional")
        self.assertTrue(history["requires_explicit_category"])
        self.assertEqual(ledger.list_spending(month=datetime.now().strftime("%Y-%m"))["spent_cents"], 400)

    def test_budget_outlook_calculates_projection_comparison_and_risk(self) -> None:
        self.ledger.add_expense(
            request_id="outlook-current",
            member="Member 1",
            category="Everyday",
            amount="600.00",
            occurred_at="2026-08-10T09:00:00-04:00",
        )
        self.ledger.add_expense(
            request_id="outlook-previous",
            member="Member 1",
            category="Everyday",
            amount="300.00",
            occurred_at="2026-07-10T09:00:00-04:00",
        )
        self.ledger.add_expense(
            request_id="outlook-future-dated",
            member="Member 1",
            category="Everyday",
            amount="100.00",
            occurred_at="2026-08-20T09:00:00-04:00",
        )
        outlook = self.ledger.get_budget_outlook(
            month="2026-08", as_of="2026-08-10T12:00:00-04:00"
        )
        self.assertEqual(outlook["elapsed_days"], 10)
        self.assertEqual(outlook["spent_cents"], 60000)
        self.assertEqual(outlook["projected_month_end_cents"], 186000)
        self.assertEqual(outlook["previous_same_point_cents"], 30000)
        self.assertEqual(outlook["pace_change_cents"], 30000)
        self.assertEqual(outlook["categories_at_risk"][0]["category"], "Everyday")


if __name__ == "__main__":
    unittest.main()
