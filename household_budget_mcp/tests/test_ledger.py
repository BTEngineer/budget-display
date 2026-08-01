from __future__ import annotations

import tempfile
import unittest
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
        )
        self.assertEqual(entry["category"], "Food")
        self.assertIsNone(entry["parent_category"])


if __name__ == "__main__":
    unittest.main()
