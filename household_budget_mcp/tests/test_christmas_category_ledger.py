from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from budget_display import BudgetLedger, BudgetValidationError
from budget_display.mcp_http import _load_categories


class ChristmasCategoryLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = BudgetLedger(
            Path(self.temporary.name) / "budget.db",
            members=("Alex", "Morgan"),
            categories=(
                ("Everyday", None, 50_000),
                ("Other", None, None),
                ("Christmas", None, None),
                ("Gifts", "Christmas", None),
            ),
            classification_aliases=(("market", "Christmas/Gifts"), ("pantry", "Other")),
        )
        self.ledger.initialize()

    def expense(self, request_id: str, category: str, amount: str, **fields):
        return self.ledger.add_expense(
            request_id=request_id, category=category, amount=amount, member="Alex",
            **fields,
        )["transaction"]["transaction_id"]

    def test_mixed_purchase_and_inactive_christmas_refund_use_the_correct_basis(self) -> None:
        purchase = dict(
            request_id="mixed", total_amount="70.00", occurred_at="2026-12-10T16:00:00+00:00",
            allocations=[
                {"category": "Other", "member": "Alex", "amount": "40.00"},
                {"category": " / chRistmas / Gifts / ", "member": "Morgan", "amount": "30.00"},
            ],
        )
        self.expense("regular", "Everyday", "160.00", occurred_at=purchase["occurred_at"])
        with self.assertRaisesRegex(BudgetValidationError, "explicit Christmas request"):
            self.ledger.add_split_expense(**purchase)
        self.assertEqual(self.ledger.search_transactions(request_id="mixed")["count"], 0)
        first = self.ledger.add_split_expense(**purchase, explicitly_requested_christmas=True)
        retry = self.ledger.add_split_expense(**purchase, explicitly_requested_christmas=True)
        self.assertTrue(retry["duplicate"])
        self.assertEqual(first["split_transaction_id"], retry["split_transaction_id"])

        summary = self.ledger.list_spending(month="2026-12")
        self.assertEqual(summary["spent_cents"], 23_000)
        self.assertEqual(summary["allowance_spent_cents"], 20_000)
        self.assertEqual(summary["outside_allowance_spent_cents"], 3_000)
        self.assertEqual(summary["remaining_cents"], 30_000)
        self.assertEqual(summary["by_member_cents"], {"Alex": 20_000, "Morgan": 3_000})
        self.assertEqual(summary["allowance_by_member_cents"], {"Alex": 20_000, "Morgan": 0})
        rows = {row["name"]: row for row in summary["category_rows"]}
        self.assertTrue(rows["Christmas"]["outside_monthly_allowance"])
        self.assertTrue(rows["Gifts"]["outside_monthly_allowance"])
        self.assertFalse(rows["Other"]["outside_monthly_allowance"])
        self.assertEqual(rows["Christmas"]["spent_cents"], 3_000)
        self.assertIsNone(rows["Christmas"]["budget_cents"])

        reconfigured = BudgetLedger(
            self.ledger.database, members=("Alex",),
            categories=(("Everyday", None, 50_000), ("Other", None, None)),
        )
        reconfigured.initialize()
        reconfigured.refund_expense(
            request_id="linked-refund", amount="30.00",
            expense_id=first["allocations"][1]["transaction_id"],
            occurred_at="2027-01-03T16:00:00+00:00",
        )
        january = reconfigured.list_spending(month="2027-01")
        self.assertEqual(january["spent_cents"], -3_000)
        self.assertEqual(january["by_category_cents"]["Christmas"], -3_000)
        self.assertEqual(january["allowance_spent_cents"], 0)
        self.assertEqual(january["outside_allowance_spent_cents"], -3_000)
        self.assertEqual(january["remaining_cents"], 50_000)
        self.assertEqual(january["allowance_by_member_cents"]["Morgan"], 0)
        self.assertEqual(reconfigured.list_spending(month="2026-12")["remaining_cents"], 30_000)

    def test_legacy_christmas_month_limit_is_ignored_without_rewriting_it(self) -> None:
        month = "2026-12"
        self.ledger.list_spending(month=month)
        with closing(sqlite3.connect(self.ledger.database)) as connection, connection:
            connection.execute(
                "INSERT INTO monthly_budgets(category_id, month, amount_cents) "
                "SELECT id, ?, 9900 FROM categories WHERE name = 'Christmas'",
                (month,),
            )
        summary = self.ledger.list_spending(month=month)
        self.assertEqual(summary["budget_cents"], 50_000)
        self.assertEqual(summary["remaining_cents"], 50_000)
        self.assertIsNone(next(row["budget_cents"] for row in summary["category_rows"]
                               if row["name"] == "Christmas"))
        for setter, extra in (
            (self.ledger.set_monthly_budget, {"month": month}),
            (self.ledger.set_default_monthly_budget, {}),
        ):
            with self.assertRaisesRegex(BudgetValidationError, "annual budget"):
                setter(category="christmas", amount="10.00", **extra)
        with closing(sqlite3.connect(self.ledger.database)) as connection, connection:
            self.assertEqual(connection.execute(
                "SELECT amount_cents FROM monthly_budgets WHERE amount_cents = 9900"
            ).fetchone(), (9900,))

    def test_outlook_uses_allowance_for_both_months_and_keeps_cutoff_cash_flow(self) -> None:
        with patch("budget_display.ledger._utc_now", return_value="2026-12-01T12:00:00+00:00"):
            self.expense("prev-ordinary", "Other", "100.00",
                         occurred_at="2026-12-03T17:00:00+00:00")
            gift = self.expense("prev-gift", "Christmas/Gifts", "300.00",
                                occurred_at="2026-12-04T17:00:00+00:00",
                                explicitly_requested_christmas=True)
            self.expense("curr-ordinary", "Everyday", "50.00",
                         occurred_at="2027-01-02T17:00:00+00:00")
            self.expense("future-ordinary", "Everyday", "500.00",
                         occurred_at="2027-01-20T17:00:00+00:00")
            self.ledger.refund_expense(
                request_id="jan-refund", expense_id=gift, amount="300.00",
                occurred_at="2027-01-03T17:00:00+00:00",
            )
        outlook = self.ledger.get_budget_outlook(
            month="2027-01", as_of="2027-01-10T17:00:00+00:00",
        )
        self.assertEqual(outlook["spent_cents"], 5_000)
        self.assertEqual(outlook["cash_flow_spent_cents"], -25_000)
        self.assertEqual(outlook["outside_allowance_spent_cents"], -30_000)
        self.assertEqual(outlook["daily_spending_rate_cents"], 500)
        self.assertEqual(outlook["projected_month_end_cents"], 15_500)
        self.assertEqual(outlook["previous_same_point_cents"], 10_000)
        self.assertEqual(outlook["pace_change_cents"], -5_000)
        self.assertEqual(outlook["categories_at_risk"], [])

    def test_confirmation_and_correction_require_explicit_intent_to_assign_christmas(self) -> None:
        original = self.expense("original", "Everyday", "100.00")
        correction = dict(
            request_id="correct", transaction_id=original, category="Christmas/Gifts", amount="600.00",
        )
        for operation in (self.ledger.prepare_correction, self.ledger.correct_expense):
            with self.assertRaisesRegex(BudgetValidationError, "explicit Christmas request"):
                operation(**correction)
            with self.assertRaisesRegex(BudgetValidationError, "must be a boolean"):
                operation(**correction, explicitly_requested_christmas="true")
        prepared = self.ledger.prepare_correction(**correction, explicitly_requested_christmas=True)
        self.assertIs(prepared["correction"]["explicitly_requested_christmas"], True)
        corrected = self.ledger.correct_expense(
            **correction, explicitly_requested_christmas=True,
            confirmation_token=prepared["confirmation_token"],
        )
        inherited = self.ledger.correct_expense(
            request_id="correct-description",
            transaction_id=corrected["replacement"]["transaction_id"],
            description="Corrected description", amount="500.00",
        )
        self.assertEqual(inherited["replacement"]["category"], "Christmas/Gifts")

        explicit_purchase = dict(
            request_id="large", member="Alex", category="Christmas/Gifts", amount="501.00"
        )
        with self.assertRaisesRegex(BudgetValidationError, "explicit Christmas request"):
            self.ledger.prepare_expense(**explicit_purchase)
        confirmed_price = self.ledger.prepare_expense(
            **explicit_purchase, explicitly_requested_christmas=True,
        )
        self.assertIs(confirmed_price["expense"]["explicitly_requested_christmas"], True)
        posted_fields = dict(
            **explicit_purchase, occurred_at=confirmed_price["expense"]["occurred_at"],
            confirmation_token=confirmed_price["confirmation_token"], require_confirmation=True,
        )
        with self.assertRaisesRegex(BudgetValidationError, "explicit Christmas request"):
            self.ledger.add_expense(**posted_fields)
        self.ledger.add_expense(**posted_fields, explicitly_requested_christmas=True)
        self.assertTrue(
            self.ledger.add_expense(**posted_fields, explicitly_requested_christmas=True)["duplicate"]
        )

    def test_large_mixed_confirmation_preserves_the_explicit_flag(self) -> None:
        purchase = dict(
            request_id="large-split", total_amount="600.00",
            allocations=[
                {"category": "Everyday", "member": "Alex", "amount": "300.00"},
                {"category": "Christmas/Gifts", "member": "Morgan", "amount": "300.00"},
            ],
        )
        with self.assertRaisesRegex(BudgetValidationError, "must be a boolean"):
            self.ledger.prepare_split_expense(**purchase, explicitly_requested_christmas=1)
        prepared = self.ledger.prepare_split_expense(**purchase, explicitly_requested_christmas=True)
        self.assertIs(prepared["split_expense"]["explicitly_requested_christmas"], True)
        post = dict(**purchase, occurred_at=prepared["split_expense"]["occurred_at"],
                    confirmation_token=prepared["confirmation_token"], require_confirmation=True)
        with self.assertRaisesRegex(BudgetValidationError, "explicit Christmas request"):
            self.ledger.add_split_expense(**post)
        split = self.ledger.add_split_expense(**post, explicitly_requested_christmas=True)
        self.assertEqual(len(split["allocations"]), 2)

    def test_christmas_cannot_be_suggested_or_confirmed_from_ai_fields(self) -> None:
        self.expense("prior-gift", "Christmas/Gifts", "50.00", business_name="Market",
                     explicitly_requested_christmas=True)
        self.expense("prior-normal", "Other", "10.00", business_name="Market")
        classification = self.ledger.suggest_expense_classification(
            business_name="Market", description="Christmas gifts"
        )
        self.assertEqual([row["category"] for row in classification["suggestions"]], ["Other"])
        draft = self.ledger.register_receipt(
            digest="a" * 64, relative_path="receipt.jpg", content_type="image/jpeg",
            byte_size=50, original_filename="receipt.jpg",
        )
        self.ledger.update_receipt_draft(
            draft_id=draft["id"], ai_entity_id="ai", fields={"suggested_category": "Christmas/Gifts"},
        )
        self.assertIsNone(self.ledger.get_receipt_draft(draft["id"])["suggested_category"])
        with self.assertRaisesRegex(BudgetValidationError, "explicit Christmas request"):
            self.ledger.confirm_receipt_draft(
                draft_id=draft["id"], request_id="draft", member="Alex",
                category="Christmas/Gifts", amount="50.00",
            )
        self.assertIsNone(self.ledger.get_receipt_draft(draft["id"])["confirmation_request_id"])
        with self.assertRaisesRegex(BudgetValidationError, "explicit Christmas request"):
            self.ledger.refund_expense(
                request_id="unlinked", member="Alex", category="Christmas/Gifts", amount="10.00",
            )

    def test_blank_category_limits_are_unset_and_christmas_has_no_monthly_limit(self) -> None:
        self.assertEqual(_load_categories([
            {"name": "Christmas", "monthly_budget": ""},
            {"name": "Other", "monthly_budget": None},
        ]), (("Christmas", None, None), ("Other", None, None)))
        with self.assertRaisesRegex(BudgetValidationError, "must be positive"):
            _load_categories([{"name": "Other", "monthly_budget": 0}])
        with self.assertRaisesRegex(BudgetValidationError, "annual budget"):
            _load_categories([{"name": "Christmas", "monthly_budget": "1.00"}])
        uncapped = BudgetLedger(
            Path(self.temporary.name) / "uncapped.db", categories=(("Other", None, None),)
        )
        uncapped.initialize()
        uncapped.add_expense(
            request_id="ordinary", category="Other", member="Member 1", amount="1.00",
            occurred_at="2026-12-15T17:00:00+00:00",
        )
        summary = uncapped.list_spending(month="2026-12")
        self.assertIsNone(summary["budget_cents"])
        self.assertIsNone(summary["remaining_cents"])
        self.assertEqual(summary["allowance_spent_cents"], 100)


if __name__ == "__main__":
    unittest.main()

