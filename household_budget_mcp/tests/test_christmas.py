from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from budget_display import BudgetLedger, BudgetValidationError, DuplicateRequestError
from budget_display.christmas import ChristmasLedger


class ChristmasLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(Path(self.temporary.name) / "budget.db")
        self.ledger.initialize()
        self.christmas = ChristmasLedger(self.ledger)
        self.alex = self.christmas.upsert_recipient(request_id="alex", name="Alex")["recipient_id"]
        self.blair = self.christmas.upsert_recipient(request_id="blair", name="Blair")["recipient_id"]
        self.christmas.configure_year(request_id="year", year=2026,
                                      child_recipient_ids=[self.alex, self.blair])

    def tearDown(self):
        self.temporary.cleanup()

    def expense(self, request_id="purchase", amount="110.00", occurred_at="2026-12-01T12:00:00-05:00"):
        result = self.ledger.add_expense(request_id=request_id, member="Member 1",
                                         category="Everyday", amount=amount, occurred_at=occurred_at)
        return {**result, "transaction_id": result["transaction"]["transaction_id"]}

    def assign(self, entry, **overrides):
        values = dict(request_id="attribute", transaction_id=entry["transaction_id"],
                      gifts=[{"description": "Shared gift", "amount": "100.00",
                              "recipient_ids": [self.alex, self.blair]}],
                      overhead_amount="10.00", explicitly_requested=True)
        values.update(overrides)
        return self.christmas.assign_purchase(**values)

    def refund(self, entry, amount, request_id="refund"):
        result = self.ledger.refund_expense(request_id=request_id, expense_id=entry["transaction_id"],
                                            amount=amount, occurred_at="2027-01-02T12:00:00-05:00")
        return {**result, "transaction_id": result["transaction"]["transaction_id"]}

    def child_values(self, result):
        return {row["recipient_id"]: row["gift_value_cents"] for row in result["child_totals"]}

    def test_opt_in_does_not_change_monthly_money_and_exact_retry(self):
        entry = self.expense()
        with self.assertRaisesRegex(BudgetValidationError, "explicit"):
            self.assign(entry, explicitly_requested=False)
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 0)
        assigned = self.assign(entry)
        self.assertFalse(assigned["duplicate"])
        self.assertTrue(self.assign(entry)["duplicate"])
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 11000)
        self.assertEqual(self.child_values(self.christmas.summary(year=2026)), {self.alex: 5000, self.blair: 5000})
        self.assertEqual(self.ledger.list_spending(month="2026-12")["spent_cents"], 11000)
        with self.assertRaises(DuplicateRequestError):
            self.assign(entry, overhead_amount="9.00")

    def test_mixed_unassigned_and_exact_allocation_validation(self):
        entry = self.expense(amount="70.00")
        self.assign(entry, gifts=[{"description": "Gift", "amount": "20.00"}],
                    overhead_amount="10.00", ordinary_amount="40.00")
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 3000)
        self.assertEqual(result["unassigned_gift_cents"], 2000)
        self.assertFalse(result["comparison_complete"])
        self.assertTrue(all(value is None for value in self.child_values(result).values()))
        with self.assertRaisesRegex(BudgetValidationError, "equal"):
            self.assign(entry, request_id="wrong", gifts=[], overhead_amount="71.00")

    def test_pending_category_amounts_do_not_infer_a_year_or_count_twice(self):
        purchase = self.ledger.add_expense(
            request_id="pending", member="Member 1", category="Christmas", amount="67.00",
            explicitly_requested_christmas=True, occurred_at="2026-01-01T12:00:00-05:00",
        )
        purchase_id = purchase["transaction"]["transaction_id"]
        self.ledger.refund_expense(request_id="pending-refund", expense_id=purchase_id, amount="2.00")
        for year in (2026, 2027):
            summary = self.christmas.summary(year=year)
            self.assertIsNone(summary["spent_cents"])
            self.assertEqual(summary["known_spent_cents"], 0)
            self.assertEqual(summary["pending_purchase_count"], 1)
            self.assertEqual(summary["pending_purchase_cents"], 6500)
        self.christmas.assign_purchase(
            request_id="pending-metadata", transaction_id=purchase_id, gifts=[],
            overhead_amount="67.00", year=2027, explicitly_requested=True,
        )
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 0)
        summary = self.christmas.summary(year=2027)
        self.assertEqual(summary["pending_purchase_count"], 0)
        self.assertEqual(summary["spent_cents"], 6500)

    def test_clearing_a_categorized_purchase_after_refund_keeps_financial_christmas_pending(self):
        entry = self.ledger.add_expense(
            request_id="categorized-for-clear", member="Member 1", category="Christmas", amount="110.00",
            occurred_at="2026-12-04T12:00:00-05:00", explicitly_requested_christmas=True,
        )
        entry["transaction_id"] = entry["transaction"]["transaction_id"]
        self.assign(entry)
        refund = self.refund(entry, "21.00")
        self.christmas.resolve_refund(
            request_id="before-clear-refund", transaction_id=refund["transaction_id"],
            gifts=[{"gift_id": "gift_1", "amount": "20.00"}], overhead_amount="1.00",
            explicitly_requested=True,
        )
        self.assign(entry, request_id="clear-categorized", gifts=[],
                    overhead_amount="0.00", ordinary_amount="110.00")
        summary = self.christmas.summary(year=2026)
        self.assertEqual(summary["pending_purchase_count"], 1)
        self.assertEqual(summary["pending_purchase_cents"], 8900)
        self.assertIsNone(summary["spent_cents"])
        self.assertEqual(self.ledger.list_spending(month="2026-12")["allowance_spent_cents"], 0)
        self.assign(entry, request_id="assign-again")
        summary = self.christmas.summary(year=2026)
        self.assertEqual(summary["pending_purchase_count"], 0)
        self.assertEqual(summary["spent_cents"], 8900)
        self.assertEqual(summary["unresolved_refund_cents"], 2100)

    def test_pending_category_ignores_reversal_and_tracks_correction_only_once(self):
        expense = self.ledger.add_expense(
            request_id="initial", member="Member 1", category="Christmas", amount="10.00",
            explicitly_requested_christmas=True,
        )
        self.ledger.correct_expense(
            request_id="correction", transaction_id=expense["transaction"]["transaction_id"], amount="12.00",
        )
        self.assertEqual(self.christmas.summary(year=2026)["pending_purchase_cents"], 1200)
        self.ledger.undo_last_expense(request_id="undo-correction", member="Member 1")
        summary = self.christmas.summary(year=2026)
        self.assertEqual(summary["pending_purchase_count"], 0)
        self.assertEqual(summary["spent_cents"], 0)
        self.ledger.refund_expense(
            request_id="unlinked", member="Member 1", category="Christmas", amount="3.00",
            explicitly_requested_christmas=True,
        )
        summary = self.christmas.summary(year=2026)
        self.assertEqual(summary["pending_unlinked_refund_count"], 1)
        self.assertEqual(summary["pending_unlinked_refund_cents"], 300)
        self.assertIsNone(summary["spent_cents"])

    def test_mixed_financial_category_and_metadata_match_and_unresolved_refund_keeps_annual_cash(self):
        payment = self.ledger.add_split_expense(
            request_id="holiday-split", total_amount="70.00", explicitly_requested_christmas=True,
            allocations=[
                {"member": "Member 1", "category": "Christmas", "amount": "30.00"},
                {"member": "Member 1", "category": "Everyday", "amount": "40.00"},
            ],
            occurred_at="2026-12-10T12:00:00-05:00",
        )
        transaction_id = payment["split_transaction_id"]
        attribution = dict(request_id="split-details", transaction_id=transaction_id,
                           gifts=[{"description": "Gift", "amount": "20.00", "recipient_ids": [self.alex]}],
                           year=2026, explicitly_requested=True)
        with self.assertRaisesRegex(BudgetValidationError, "financial Christmas category portion"):
            self.christmas.assign_purchase(**attribution, overhead_amount="9.00")
        self.christmas.assign_purchase(**attribution, overhead_amount="10.00")
        ordinary_id = next(row["transaction_id"] for row in payment["allocations"] if row["category"] == "Everyday")
        christmas_id = next(row["transaction_id"] for row in payment["allocations"] if row["category"] == "Christmas")
        self.ledger.refund_expense(request_id="return-ordinary", expense_id=ordinary_id, amount="5.00")
        ordinary_return = self.christmas.summary(year=2026)
        self.assertEqual(ordinary_return["spent_cents"], 3000)
        self.assertEqual(ordinary_return["unresolved_refund_cents"], 0)
        self.assertTrue(ordinary_return["comparison_complete"])
        refund = self.ledger.refund_expense(request_id="return-christmas", expense_id=christmas_id, amount="6.00")
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 2400)
        self.assertEqual(result["unresolved_refund_cents"], 600)
        self.assertFalse(result["comparison_complete"])
        with self.assertRaisesRegex(BudgetValidationError, "linked financial category"):
            self.christmas.resolve_refund(
                request_id="wrong-category", transaction_id=refund["transaction"]["transaction_id"],
                gifts=[], ordinary_amount="6.00", explicitly_requested=True,
            )

        self.ledger.refund_expense(request_id="return-rest", expense_id=christmas_id, amount="24.00")
        complete = self.christmas.summary(year=2026)
        self.assertEqual(complete["spent_cents"], 0)
        self.assertEqual(complete["unresolved_refund_cents"], 0)
        self.assertTrue(complete["comparison_complete"])
        self.assertEqual(self.child_values(complete), {self.alex: 0, self.blair: 0})

    def test_partial_refunds_are_unresolved_until_gift_and_overhead_are_explicit(self):
        entry = self.expense()
        self.assign(entry)
        refund = self.refund(entry, "21.00")
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 8900)
        self.assertFalse(result["comparison_complete"])
        self.assertEqual(result["unresolved_refund_cents"], 2100)
        self.christmas.resolve_refund(request_id="resolve", transaction_id=refund["transaction_id"],
                                      gifts=[{"gift_id": "gift_1", "amount": "20.00"}],
                                      overhead_amount="1.00", explicitly_requested=True)
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 8900)
        self.assertEqual(self.child_values(result), {self.alex: 4000, self.blair: 4000})
        self.assertEqual(self.ledger.list_spending(month="2027-01")["spent_cents"], -2100)
        self.refund(entry, "89.00", request_id="remaining")
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 0)
        self.assertEqual(self.child_values(result), {self.alex: 0, self.blair: 0})

    def test_refund_capacity_and_recipient_year_correction_after_refund(self):
        entry = self.expense()
        self.assign(entry)
        refund = self.refund(entry, "21.00")
        with self.assertRaisesRegex(BudgetValidationError, "exceed"):
            self.christmas.resolve_refund(request_id="bad", transaction_id=refund["transaction_id"],
                                          gifts=[], overhead_amount="21.00", explicitly_requested=True)
        self.christmas.resolve_refund(request_id="good", transaction_id=refund["transaction_id"],
                                      gifts=[{"gift_id": "gift_1", "amount": "20.00"}],
                                      overhead_amount="1.00", explicitly_requested=True)
        self.christmas.configure_year(request_id="year27", year=2027, child_recipient_ids=[self.blair])
        self.assign(entry, request_id="move", year=2027,
                    gifts=[{"description": "Shared gift", "amount": "100.00", "recipient_ids": [self.blair]}])
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 0)
        self.assertEqual(self.child_values(self.christmas.summary(year=2027)), {self.blair: 8000})
        self.assertEqual(len(self.christmas.details(year=2027)["history"]), 3)

    def test_explicit_clear_after_partial_refund_preserves_money_and_audit_and_retries(self):
        entry = self.expense()
        self.assign(entry)
        self.assertTrue(self.assign(entry)["duplicate"])
        refund = self.refund(entry, "21.00")
        resolution = dict(request_id="resolve", transaction_id=refund["transaction_id"],
                          gifts=[{"gift_id": "gift_1", "amount": "20.00"}],
                          overhead_amount="1.00", explicitly_requested=True)
        self.christmas.resolve_refund(**resolution)
        december = self.ledger.list_spending(month="2026-12")["spent_cents"]
        january = self.ledger.list_spending(month="2027-01")["spent_cents"]
        cleared = self.assign(entry, request_id="clear", gifts=[],
                              overhead_amount="0.00", ordinary_amount="110.00")
        self.assertEqual(cleared["cleared_refund_count"], 1)
        self.assertEqual(cleared["cleared_refund_max_entry_id"], refund["id"])
        self.assertTrue(self.assign(entry)["duplicate"])
        self.assertTrue(self.christmas.resolve_refund(**resolution)["duplicate"])
        self.assertTrue(self.assign(entry, request_id="clear", gifts=[],
                                    overhead_amount="0.00", ordinary_amount="110.00")["duplicate"])
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 0)
        self.assertEqual(self.child_values(result), {self.alex: 0, self.blair: 0})
        self.assertTrue(result["comparison_complete"])
        self.assertEqual(self.ledger.list_spending(month="2026-12")["spent_cents"], december)
        self.assertEqual(self.ledger.list_spending(month="2027-01")["spent_cents"], january)
        history = self.christmas.details(year=2026)["history"]
        self.assertTrue(any(item["kind"] == "refund" and item["result"]["gifts"] == {"gift_1": 2000}
                            for item in history))
        self.refund(entry, "5.00", request_id="laterordinaryreturn")
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 0)
        self.assertEqual(result["unresolved_refund_cents"], 0)

    def test_explicit_clear_after_full_refund_updates_every_resolution(self):
        entry = self.expense()
        self.assign(entry)
        first = self.refund(entry, "21.00")
        self.christmas.resolve_refund(request_id="resolve", transaction_id=first["transaction_id"],
                                      gifts=[{"gift_id": "gift_1", "amount": "20.00"}],
                                      overhead_amount="1.00", explicitly_requested=True)
        last = self.refund(entry, "89.00", request_id="rest")
        cleared = self.assign(entry, request_id="clear", gifts=[],
                              overhead_amount="0.00", ordinary_amount="110.00")
        self.assertEqual(cleared["cleared_refund_count"], 2)
        self.assertEqual(cleared["cleared_refund_max_entry_id"], last["id"])
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 0)
        self.assertEqual(self.child_values(result), {self.alex: 0, self.blair: 0})
        with self.ledger._connection() as connection:
            resolutions = connection.execute("SELECT payload_json FROM christmas_refunds ORDER BY refund_entry_id").fetchall()
        self.assertEqual(len(resolutions), 2)
        self.assertTrue(all('"gifts":{}' in item["payload_json"] for item in resolutions))

    def test_correction_chain_inherits_and_changed_total_requires_reallocation(self):
        entry = self.expense()
        self.assign(entry)
        first = self.ledger.correct_expense(request_id="correct", transaction_id=entry["transaction_id"],
                                            description="corrected description")
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 11000)
        with self.ledger._connection() as connection:
            current = connection.execute("SELECT transaction_id FROM ledger_entries WHERE corrects_entry_id=?", (entry["id"],)).fetchone()[0]
        self.ledger.correct_expense(request_id="correct2", transaction_id=current, amount="120.00")
        self.assertIsNone(self.christmas.summary(year=2026)["spent_cents"])
        self.assign(entry, request_id="resize", ordinary_amount="10.00")
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 11000)
        self.ledger.undo_last_expense(request_id="undo", member="Member 1")
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 0)

    def test_split_group_has_one_attribution_across_all_aliases(self):
        result = self.ledger.add_split_expense(request_id="split", total_amount="110.00", allocations=[
            {"member": "Member 1", "category": "Everyday", "amount": "60.00"},
            {"member": "Member 2", "category": "Everyday", "amount": "50.00"},
        ], occurred_at="2026-12-01T12:00:00-05:00")
        with self.ledger._connection() as connection:
            entries = connection.execute("SELECT transaction_id,split_group_id FROM ledger_entries ORDER BY id").fetchall()
        self.assign({"transaction_id": entries[0]["transaction_id"]})
        self.assign({"transaction_id": entries[1]["transaction_id"]}, request_id="alias")
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 11000)
        self.assertEqual(len(self.christmas.details(year=2026)["attributions"]), 1)

    def test_custom_pennies_repeated_refunds_and_full_return(self):
        entry = self.expense(amount="0.05")
        self.assign(entry, overhead_amount="0.00", gifts=[{"gift_id": "odd", "description": "Small gift", "amount": "0.05",
                    "recipient_amounts": [{"recipient_id": self.alex, "amount": "0.04"},
                                          {"recipient_id": self.blair, "amount": "0.01"}]}])
        for index in range(4):
            refund = self.refund(entry, "0.01", f"penny{index}")
            self.christmas.resolve_refund(request_id=f"resolve{index}", transaction_id=refund["transaction_id"],
                                          gifts=[{"gift_id": "odd", "amount": "0.01"}], explicitly_requested=True)
            values = self.child_values(self.christmas.summary(year=2026))
            self.assertTrue(all(value >= 0 for value in values.values()))
            self.assertEqual(sum(values.values()), 4 - index)
        self.refund(entry, "0.01", "final")
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 0)

    def test_additional_refund_never_increases_any_recipient_gift_value(self):
        recipients = [self.alex, self.blair] + [self.christmas.upsert_recipient(
            request_id=f"other{index}", name=f"Other{index}")["recipient_id"] for index in range(4)]
        self.christmas.configure_year(request_id="allchildren", year=2026, child_recipient_ids=recipients)
        weights = ["15.00", "15.00", "9.00", "5.00", "5.00", "2.00"]
        entry = self.expense(amount="51.00")
        self.assign(entry, overhead_amount="0.00", gifts=[{"description": "Shared", "amount": "51.00",
                    "recipient_amounts": [{"recipient_id": value, "amount": amount}
                                          for value, amount in zip(recipients, weights)]}])
        first = self.refund(entry, "50.74")
        before = self.child_values(self.christmas.summary(year=2026))
        self.assertEqual(sum(before.values()), 26)
        second = self.refund(entry, "0.01", request_id="nextpenny")
        after = self.child_values(self.christmas.summary(year=2026))
        self.assertEqual(sum(after.values()), 25)
        self.assertTrue(all(after[value] <= before[value] for value in recipients))

    def test_sole_component_partial_refund_is_unambiguous(self):
        entry = self.expense(amount="20.00")
        self.assign(entry, overhead_amount="0.00", gifts=[{"description": "Gift", "amount": "20.00",
                                                          "recipient_ids": [self.alex]}])
        self.refund(entry, "5.00")
        result = self.christmas.summary(year=2026)
        self.assertEqual(result["spent_cents"], 1500)
        self.assertEqual(self.child_values(result)[self.alex], 1500)
        self.assertEqual(result["unresolved_refund_cents"], 0)

    def test_mixed_purchase_unresolved_refund_does_not_claim_exact_annual_spending(self):
        entry = self.expense(amount="70.00")
        self.assign(entry, overhead_amount="10.00", ordinary_amount="40.00",
                    gifts=[{"description": "Gift", "amount": "20.00", "recipient_ids": [self.alex]}])
        self.refund(entry, "5.00")
        result = self.christmas.summary(year=2026)
        self.assertIsNone(result["spent_cents"])
        self.assertFalse(result["spending_complete"])
        self.assertIsNone(result["remaining_cents"])
        self.assertEqual(result["known_spent_cents"], 3000)

    def test_attribution_edit_preserves_explicit_year_and_huge_offsets_are_rejected(self):
        entry = self.expense()
        self.assign(entry, year=2027)
        self.assign(entry, request_id="retag", gifts=[{"description": "Gift", "amount": "100.00",
                                                       "recipient_ids": [self.alex]}])
        self.assertEqual(self.christmas.summary(year=2027)["spent_cents"], 11000)
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 0)
        with self.assertRaises(BudgetValidationError):
            self.christmas.details(year=2027, offset=2**100)

    def test_year_limits_history_and_comparison_groups_do_not_roll(self):
        self.christmas.configure_year(request_id="cap", year=2026, budget_amount="10.00")
        self.christmas.configure_year(request_id="cap-edit", year=2026, budget_amount="12.00")
        self.christmas.configure_year(request_id="next", year=2027, child_recipient_ids=[self.alex])
        self.assertIsNone(self.christmas.summary(year=2027)["budget_cents"])
        self.assertEqual(len(self.christmas.summary(year=2026)["child_totals"]), 2)
        self.assertEqual(self.christmas.summary(year=2026)["budget_cents"], 1200)
        self.christmas.configure_year(request_id="clear", year=2026, clear_budget=True)
        self.assertIsNone(self.christmas.summary(year=2026)["remaining_cents"])
        self.assertEqual(len(self.christmas.details(year=2026, limit=2)["history"]), 2)
        self.assertTrue(self.christmas.details(year=2026, limit=2)["history_has_more"])

    def test_missing_used_schema_fails_closed(self):
        with self.ledger._connection() as connection:
            connection.execute("DROP TABLE christmas_refunds")
            connection.commit()
        with self.assertRaisesRegex(BudgetValidationError, "inconsistent Christmas schema"):
            self.ledger.initialize()

    def test_invalid_money_and_unsafe_bounds_fail_without_audit(self):
        for amount in ("NaN", "-1", "0.001", "1e100", "92233720368547758.08"):
            with self.assertRaises(BudgetValidationError):
                self.christmas.configure_year(request_id="invalid", year=2026, budget_amount=amount)


if __name__ == "__main__":
    unittest.main()
