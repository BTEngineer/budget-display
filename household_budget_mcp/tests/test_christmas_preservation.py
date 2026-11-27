from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from budget_display import BudgetLedger
from budget_display.christmas import ChristmasLedger


class ChristmasPreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(Path(self.temporary.name) / "budget.db")
        self.ledger.initialize()
        self.christmas = ChristmasLedger(self.ledger)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def expense(self, request_id: str, amount: str = "70.00") -> str:
        return self.ledger.add_expense(
            request_id=request_id, member="Member 1", category="Everyday",
            amount=amount, occurred_at="2026-12-10T12:00:00-05:00",
        )["transaction"]["transaction_id"]

    def original_tables(self) -> dict:
        with self.ledger._connection() as connection:
            tables = connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'christmas_%' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            result = {}
            for table in tables:
                name = table["name"]
                rows = connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
                if name == "ledger_metadata":
                    rows = [row for row in rows if not row["key"].startswith("christmas_")]
                result[name] = (table["sql"], [tuple(row) for row in rows])
            return result

    def test_additive_migration_and_attribution_preserve_all_original_tables(self) -> None:
        purchase = self.expense("purchase")
        split = self.ledger.add_split_expense(
            request_id="split", total_amount="40.00",
            allocations=[
                {"member": "Member 1", "category": "Everyday", "amount": "20.00"},
                {"member": "Member 2", "category": "Occasional", "amount": "20.00"},
            ], occurred_at="2026-12-11T12:00:00-05:00",
        )
        refund = self.ledger.refund_expense(
            request_id="refund", expense_id=purchase, amount="21.00",
            occurred_at="2027-01-02T12:00:00-05:00",
        )["transaction"]["transaction_id"]
        self.ledger.set_transaction_reconciliation(
            request_id="reconcile", transaction_id=purchase, reconciled=True,
        )
        december = self.ledger.list_spending(month="2026-12")
        january = self.ledger.list_spending(month="2027-01")
        with self.ledger._connection() as connection:
            for table in ("christmas_refunds", "christmas_attributions", "christmas_years",
                          "christmas_recipients", "christmas_history"):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DELETE FROM ledger_metadata WHERE key='christmas_schema_version'")
            connection.commit()
        before = self.original_tables()
        self.ledger.initialize()
        self.ledger.initialize()
        first = self.christmas.upsert_recipient(request_id="child-a", name="Child A")["recipient_id"]
        second = self.christmas.upsert_recipient(request_id="child-b", name="Child B")["recipient_id"]
        self.christmas.configure_year(
            request_id="year", year=2026, budget_amount="100.00",
            child_recipient_ids=[first, second],
        )
        self.christmas.assign_purchase(
            request_id="attribute", transaction_id=purchase, explicitly_requested=True,
            gifts=[{"gift_id": "toy", "description": "Toy", "amount": "20.00",
                    "recipient_ids": [first]}], overhead_amount="10.00", ordinary_amount="40.00",
        )
        self.christmas.resolve_refund(
            request_id="attribute-refund", transaction_id=refund, explicitly_requested=True,
            gifts=[{"gift_id": "toy", "amount": "20.00"}], overhead_amount="1.00",
        )
        split_id = split["allocations"][0]["transaction_id"]
        self.christmas.assign_purchase(
            request_id="attribute-split", transaction_id=split_id, explicitly_requested=True,
            gifts=[{"description": "Shared gift", "amount": "40.00",
                    "recipient_ids": [first, second]}],
        )
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 4900)
        self.christmas.details(year=2026)
        self.assertEqual(self.ledger.list_spending(month="2026-12"), december)
        self.assertEqual(self.ledger.list_spending(month="2027-01"), january)
        self.assertEqual(self.original_tables(), before)
        with self.ledger._connection() as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_concurrent_delivery_records_one_attribution(self) -> None:
        purchase = self.expense("concurrent", "10.00")
        arguments = dict(
            request_id="concurrent-attribution", transaction_id=purchase,
            explicitly_requested=True,
            gifts=[{"description": "Gift", "amount": "10.00"}],
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: self.christmas.assign_purchase(**arguments), range(2)))
        self.assertEqual(sorted(result["duplicate"] for result in results), [False, True])
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 1000)
        with self.ledger._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM christmas_history WHERE kind='purchase'"
            ).fetchone()[0], 1)

    def test_retry_of_prior_assignment_never_reverts_later_recipient_revision(self) -> None:
        purchase = self.expense("retry", "10.00")
        first = self.christmas.upsert_recipient(request_id="a", name="Child A")["recipient_id"]
        second = self.christmas.upsert_recipient(request_id="b", name="Child B")["recipient_id"]
        self.christmas.configure_year(request_id="group", year=2027, child_recipient_ids=[first, second])
        original = dict(
            request_id="first-attribution", transaction_id=purchase, explicitly_requested=True,
            year=2027, gifts=[{"gift_id": "toy", "description": "Toy", "amount": "10.00",
                               "recipient_ids": [first]}],
        )
        self.christmas.assign_purchase(**original)
        self.christmas.assign_purchase(
            request_id="correct-recipient", transaction_id=purchase, explicitly_requested=True,
            gifts=[{"gift_id": "toy", "description": "Toy", "amount": "10.00",
                    "recipient_ids": [second]}],
        )
        self.assertTrue(self.christmas.assign_purchase(**original)["duplicate"])
        result = self.christmas.summary(year=2027)
        self.assertEqual(result["by_recipient_cents"][first], 0)
        self.assertEqual(result["by_recipient_cents"][second], 1000)
        self.assertEqual(self.christmas.summary(year=2026)["spent_cents"], 0)

    def test_changed_financial_total_and_refund_do_not_produce_negative_known_spending(self) -> None:
        purchase = self.expense("changed-total", "10.00")
        self.christmas.assign_purchase(
            request_id="attribute-original", transaction_id=purchase, explicitly_requested=True,
            gifts=[{"description": "Gift", "amount": "10.00"}],
        )
        corrected = self.ledger.correct_expense(
            request_id="correct-total", transaction_id=purchase, amount="20.00",
        )
        replacement = corrected["replacement"]["transaction_id"]
        self.ledger.refund_expense(
            request_id="large-return", expense_id=replacement, amount="15.00",
            occurred_at="2027-01-02T12:00:00-05:00",
        )
        summary = self.christmas.summary(year=2026)
        self.assertFalse(summary["spending_complete"])
        self.assertIsNone(summary["spent_cents"])
        self.assertGreaterEqual(summary["known_spent_cents"], 0)
        self.assertTrue(any("correction" in warning for warning in summary["warnings"]))


if __name__ == "__main__":
    unittest.main()
