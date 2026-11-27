from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp import Client

from budget_display import BudgetLedger
from budget_display.mcp_server import create_server


class BudgetMCPServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.ledger = BudgetLedger(Path(self.temporary_directory.name) / "budget.db")
        self.ledger.initialize()
        self.server = create_server(self.ledger)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def call(self, tool: str, arguments: dict[str, object]):
        async def invoke():
            async with Client(self.server) as client:
                return await client.call_tool(tool, arguments)

        return asyncio.run(invoke())

    def test_exposes_only_the_approved_budget_and_christmas_tools(self) -> None:
        async def list_tools() -> dict[str, dict[str, object]]:
            async with Client(self.server) as client:
                result = await client.list_tools()
                return {tool.name: tool.input_schema for tool in result.tools}

        tools = asyncio.run(list_tools())
        self.assertEqual(
            set(tools),
            {
                "add_expense",
                "prepare_expense",
                "prepare_correction",
                "correct_expense",
                "prepare_split_expense",
                "add_split_expense",
                "list_spending",
                "list_budget_categories",
                "suggest_expense_classification",
                "get_budget_outlook",
                "undo_last_expense",
                "search_transactions",
                "set_transaction_reconciliation",
                "refund_expense",
                "upsert_christmas_recipient",
                "list_christmas_recipients",
                "configure_christmas_year",
                "set_christmas_attribution",
                "set_christmas_refund_attribution",
                "get_christmas_summary",
                "list_christmas_attributions",
            },
        )
        self.assertTrue(
            {
                "category",
                "business_name",
                "cursor",
                "operation_type",
                "reconciliation_status",
            }
            <= set(tools["search_transactions"]["properties"])
        )
        self.assertEqual(
            set(tools["set_transaction_reconciliation"]["required"]),
            {"request_id", "transaction_id", "reconciled"},
        )
        self.assertEqual(
            set(tools["refund_expense"]["required"]), {"request_id", "amount"}
        )
        for name in ("set_christmas_attribution", "set_christmas_refund_attribution"):
            self.assertIn("explicitly_requested", tools[name]["required"])
            self.assertEqual(
                tools[name]["properties"]["explicitly_requested"]["type"], "boolean"
            )
        self.assertNotIn("christmas", tools["add_expense"]["properties"])

    def add_christmas_test_expense(self, request_id: str, amount: str = "110.00") -> str:
        added = self.call(
            "add_expense",
            {
                "request_id": request_id,
                "member": "Member 1",
                "category": "Everyday",
                "amount": amount,
                "business_name": "Christmas Toy Shop",
                "description": "Christmas gifts and wrapping",
                "occurred_at": "2026-12-03T12:00:00-05:00",
            },
        )
        self.assertFalse(added.is_error)
        return added.structured_content["transaction"]["transaction_id"]

    def add_christmas_test_recipient(self, name: str) -> str:
        result = self.call(
            "upsert_christmas_recipient",
            {"request_id": f"recipient-{name}", "name": name},
        )
        self.assertFalse(result.is_error)
        return result.structured_content["recipient_id"]

    def test_christmas_looking_expense_is_not_automatically_attributed(self) -> None:
        self.add_christmas_test_expense("ordinary-december")
        summary = self.call("get_christmas_summary", {"year": 2026})
        self.assertFalse(summary.is_error)
        self.assertEqual(summary.structured_content["spent_cents"], 0)
        self.assertIsNone(summary.structured_content["budget_cents"])
        self.assertIsNone(summary.structured_content["remaining_cents"])
        detail = self.call("list_christmas_attributions", {"year": 2026})
        self.assertFalse(detail.is_error)
        self.assertEqual(detail.structured_content["attributions"], [])
        self.assertEqual(self.ledger.list_spending(month="2026-12")["spent_cents"], 11000)

    def test_christmas_explicit_opt_in_shared_gifts_and_idempotent_assignment(self) -> None:
        transaction_id = self.add_christmas_test_expense("christmas-shared")
        first_child = self.add_christmas_test_recipient("Child A")
        second_child = self.add_christmas_test_recipient("Child B")
        configured = self.call(
            "configure_christmas_year",
            {
                "request_id": "christmas-cap",
                "year": 2026,
                "budget_amount": "100.00",
                "child_recipient_ids": [first_child, second_child],
            },
        )
        self.assertFalse(configured.is_error)
        recipients = self.call("list_christmas_recipients", {})
        self.assertFalse(recipients.is_error)
        self.assertEqual(len(recipients.structured_content["recipients"]), 2)
        arguments = {
            "request_id": "christmas-shared-assign",
            "transaction_id": transaction_id,
            "gifts": [
                {
                    "description": "Shared gift",
                    "amount": "100.00",
                    "recipient_ids": [first_child, second_child],
                }
            ],
            "overhead_amount": "10.00",
        }
        missing_opt_in = self.call("set_christmas_attribution", arguments)
        self.assertTrue(missing_opt_in.is_error)
        rejected = self.call(
            "set_christmas_attribution", {**arguments, "explicitly_requested": False}
        )
        self.assertTrue(rejected.is_error)
        self.assertIn("explicit", rejected.content[0].text.lower())
        for implicit_flag in ("true", 1):
            coerced = self.call(
                "set_christmas_attribution",
                {**arguments, "explicitly_requested": implicit_flag},
            )
            self.assertTrue(coerced.is_error)
        unchanged = self.call("get_christmas_summary", {"year": 2026})
        self.assertEqual(unchanged.structured_content["spent_cents"], 0)
        accepted = self.call(
            "set_christmas_attribution", {**arguments, "explicitly_requested": True}
        )
        self.assertFalse(accepted.is_error)
        duplicate = self.call(
            "set_christmas_attribution", {**arguments, "explicitly_requested": True}
        )
        self.assertFalse(duplicate.is_error)
        self.assertTrue(duplicate.structured_content["duplicate"])
        summary = self.call("get_christmas_summary", {"year": 2026}).structured_content
        self.assertEqual(summary["spent_cents"], 11000)
        self.assertEqual(summary["remaining_cents"], -1000)
        self.assertEqual(summary["overspent_cents"], 1000)
        self.assertEqual(summary["by_recipient_cents"], {first_child: 5000, second_child: 5000})
        self.assertTrue(summary["comparison_complete"])
        self.assertEqual([child["gap_to_highest_cents"] for child in summary["child_totals"]], [0, 0])
        self.assertEqual(self.ledger.list_spending(month="2026-12")["spent_cents"], 11000)

    def test_christmas_mixed_receipt_missing_recipient_and_audited_correction(self) -> None:
        transaction_id = self.add_christmas_test_expense("christmas-mixed", "70.00")
        child = self.add_christmas_test_recipient("Child A")
        self.call(
            "configure_christmas_year",
            {"request_id": "mixed-group", "year": 2026, "child_recipient_ids": [child]},
        )
        initial = self.call(
            "set_christmas_attribution",
            {
                "request_id": "mixed-assign",
                "transaction_id": transaction_id,
                "gifts": [{"description": "Gift awaiting recipient", "amount": "20.00"}],
                "overhead_amount": "10.00",
                "ordinary_amount": "40.00",
                "explicitly_requested": True,
            },
        )
        self.assertFalse(initial.is_error)
        incomplete = self.call("get_christmas_summary", {"year": 2026}).structured_content
        self.assertEqual(incomplete["spent_cents"], 3000)
        self.assertEqual(incomplete["unassigned_gift_cents"], 2000)
        self.assertFalse(incomplete["comparison_complete"])
        gift_id = initial.structured_content["gifts"][0]["gift_id"]
        corrected = self.call(
            "set_christmas_attribution",
            {
                "request_id": "mixed-recipient-correction",
                "transaction_id": transaction_id,
                "gifts": [{"gift_id": gift_id, "description": "Gift", "amount": "20.00", "recipient_ids": [child]}],
                "overhead_amount": "10.00",
                "ordinary_amount": "40.00",
                "explicitly_requested": True,
            },
        )
        self.assertFalse(corrected.is_error)
        complete = self.call("get_christmas_summary", {"year": 2026}).structured_content
        self.assertEqual(complete["by_recipient_cents"][child], 2000)
        self.assertEqual(complete["unassigned_gift_cents"], 0)
        self.assertTrue(complete["comparison_complete"])
        details = self.call("list_christmas_attributions", {"year": 2026, "limit": 1}).structured_content
        self.assertEqual(len(details["attributions"]), 1)
        history = self.call("list_christmas_attributions", {"year": 2026}).structured_content
        self.assertGreaterEqual(len(history["history"]), 2)
        transactions = self.call("search_transactions", {}).structured_content
        self.assertEqual(transactions["count"], 1)

    def test_christmas_partial_and_full_refunds_preserve_original_year(self) -> None:
        transaction_id = self.add_christmas_test_expense("christmas-refund")
        child = self.add_christmas_test_recipient("Child A")
        self.call(
            "configure_christmas_year",
            {"request_id": "refund-group", "year": 2026, "child_recipient_ids": [child]},
        )
        assigned = self.call(
            "set_christmas_attribution",
            {
                "request_id": "refund-assign",
                "transaction_id": transaction_id,
                "gifts": [{"description": "Gift", "amount": "100.00", "recipient_ids": [child]}],
                "overhead_amount": "10.00",
                "explicitly_requested": True,
            },
        )
        self.assertFalse(assigned.is_error)
        refund = self.call(
            "refund_expense",
            {
                "request_id": "christmas-partial-refund",
                "expense_id": transaction_id,
                "amount": "21.00",
                "occurred_at": "2027-01-03T12:00:00-05:00",
            },
        )
        self.assertFalse(refund.is_error)
        unresolved = self.call("get_christmas_summary", {"year": 2026}).structured_content
        self.assertFalse(unresolved["comparison_complete"])
        self.assertEqual(unresolved["spent_cents"], 8900)
        self.assertTrue(unresolved["spending_complete"])
        self.assertEqual(unresolved["unresolved_refund_cents"], 2100)
        resolved = self.call(
            "set_christmas_refund_attribution",
            {
                "request_id": "christmas-partial-resolve",
                "transaction_id": refund.structured_content["transaction"]["transaction_id"],
                "gifts": [{"gift_id": assigned.structured_content["gifts"][0]["gift_id"], "amount": "20.00"}],
                "overhead_amount": "1.00",
                "explicitly_requested": True,
            },
        )
        self.assertFalse(resolved.is_error)
        net = self.call("get_christmas_summary", {"year": 2026}).structured_content
        self.assertEqual(net["spent_cents"], 8900)
        self.assertEqual(net["by_recipient_cents"][child], 8000)
        self.assertTrue(net["comparison_complete"])
        rest = self.call(
            "refund_expense",
            {
                "request_id": "christmas-final-refund",
                "expense_id": transaction_id,
                "amount": "89.00",
                "occurred_at": "2027-01-04T12:00:00-05:00",
            },
        )
        self.assertFalse(rest.is_error)
        empty = self.call("get_christmas_summary", {"year": 2026}).structured_content
        self.assertEqual(empty["spent_cents"], 0)
        self.assertEqual(empty["by_recipient_cents"][child], 0)
        next_year = self.call("get_christmas_summary", {"year": 2027}).structured_content
        self.assertEqual(next_year["spent_cents"], 0)
        self.assertEqual(self.ledger.list_spending(month="2027-01")["spent_cents"], -11000)

    def test_add_and_list_round_trip_through_mcp(self) -> None:
        added = self.call(
            "add_expense",
            {
                "request_id": "telegram-mcp-1",
                "member": "Member 2",
                "category": "Meals/Drinks",
                "amount": "8.00",
                "description": "Coffee",
                "occurred_at": "2026-08-01T09:00:00-04:00",
            },
        )
        self.assertFalse(added.is_error)
        self.assertEqual(added.structured_content["amount_cents"], 800)

        summary = self.call("list_spending", {"month": "2026-08"})
        self.assertFalse(summary.is_error)
        self.assertEqual(summary.structured_content["spent_cents"], 800)
        self.assertEqual(summary.structured_content["budget_cents"], 180000)

    def test_large_expense_requires_exact_confirmation_token(self) -> None:
        arguments = {
            "request_id": "telegram-mcp-2",
            "member": "Member 1",
            "category": "Everyday",
            "amount": "600.00",
            "occurred_at": "2026-08-02T09:00:00-04:00",
        }
        rejected = self.call("add_expense", arguments)
        self.assertTrue(rejected.is_error)
        self.assertIn("call prepare_expense", rejected.content[0].text)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 0)

        boolean_only = self.call(
            "add_expense", {**arguments, "confirm_large_expense": True}
        )
        self.assertTrue(boolean_only.is_error)
        prepared = self.call("prepare_expense", arguments)
        self.assertFalse(prepared.is_error)
        token = prepared.structured_content["confirmation_token"]
        changed = self.call(
            "add_expense",
            {**arguments, "amount": "601.00", "confirmation_token": token},
        )
        self.assertTrue(changed.is_error)
        self.assertIn("does not match", changed.content[0].text)
        accepted = self.call("add_expense", {**arguments, "confirmation_token": token})
        self.assertFalse(accepted.is_error)
        self.assertEqual(accepted.structured_content["amount_cents"], 60000)

    def test_prepare_and_commit_share_the_description_limit(self) -> None:
        arguments = {
            "request_id": "description-limit",
            "member": "Member 1",
            "category": "Everyday",
            "amount": "600.00",
            "description": "x" * 1000,
            "occurred_at": "2026-08-02T09:00:00-04:00",
        }
        prepared = self.call("prepare_expense", arguments)
        self.assertFalse(prepared.is_error)
        committed = self.call(
            "add_expense",
            {
                **arguments,
                "confirmation_token": prepared.structured_content["confirmation_token"],
            },
        )
        self.assertFalse(committed.is_error)
        self.assertEqual(len(committed.structured_content["transaction"]["description"]), 1000)

        too_long = {**arguments, "request_id": "description-too-long", "description": "x" * 1001}
        self.assertTrue(self.call("prepare_expense", too_long).is_error)
        self.assertTrue(self.call("add_expense", too_long).is_error)

    @patch("budget_display.ledger._utc_now", return_value="2026-08-10T15:00:00+00:00")
    def test_new_write_and_read_tools_round_trip_through_mcp(self, _clock) -> None:
        original = self.call(
            "add_expense",
            {
                "request_id": "new-tools-original",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "10.00",
                "business_name": "Corner Store",
                "occurred_at": "2026-08-01T09:00:00-04:00",
            },
        )
        corrected = self.call(
            "correct_expense",
            {
                "request_id": "new-tools-correction",
                "transaction_id": original.structured_content["transaction"]["transaction_id"],
                "amount": "12.00",
            },
        )
        self.assertFalse(corrected.is_error)
        self.assertEqual(corrected.structured_content["replacement"]["amount"], "12.00")
        split = self.call(
            "add_split_expense",
            {
                "request_id": "new-tools-split",
                "total_amount": "8.00",
                "allocations": [
                    {"member": "Member 1", "category": "Everyday", "amount": "3.00"},
                    {"member": "Member 2", "category": "Occasional", "amount": "5.00"},
                ],
                "occurred_at": "2026-08-02T09:00:00-04:00",
            },
        )
        self.assertFalse(split.is_error)
        self.assertEqual(len(split.structured_content["allocations"]), 2)
        suggested = self.call(
            "suggest_expense_classification", {"business_name": "Corner Store"}
        )
        self.assertFalse(suggested.is_error)
        self.assertEqual(suggested.structured_content["suggestions"][0]["category"], "Everyday")
        outlook = self.call(
            "get_budget_outlook",
            {"month": "2026-08", "as_of": "2026-08-10T12:00:00-04:00"},
        )
        self.assertFalse(outlook.is_error)
        self.assertEqual(outlook.structured_content["spent_cents"], 2000)

    def test_large_split_requires_exact_confirmation_token(self) -> None:
        arguments = {
            "request_id": "large-split",
            "total_amount": "600.00",
            "allocations": [
                {"member": "Member 1", "category": "Everyday", "amount": "300.00"},
                {"member": "Member 2", "category": "Occasional", "amount": "300.00"},
            ],
        }
        rejected = self.call("add_split_expense", arguments)
        self.assertTrue(rejected.is_error)
        prepared = self.call("prepare_split_expense", arguments)
        self.assertFalse(prepared.is_error)
        occurred_at = prepared.structured_content["split_expense"]["occurred_at"]
        missing_bound_time = self.call(
            "add_split_expense",
            {
                **arguments,
                "confirmation_token": prepared.structured_content["confirmation_token"],
            },
        )
        self.assertTrue(missing_bound_time.is_error)
        self.assertIn("occurred_at", missing_bound_time.content[0].text)
        accepted = self.call(
            "add_split_expense",
            {
                **arguments,
                "occurred_at": occurred_at,
                "confirmation_token": prepared.structured_content["confirmation_token"],
            },
        )
        self.assertFalse(accepted.is_error)
        self.assertEqual(accepted.structured_content["total_amount"], "600.00")

    def test_large_write_retries_are_idempotent_without_a_live_token(self) -> None:
        expense_arguments = {
            "request_id": "large-expense-retry",
            "member": "Member 1",
            "category": "Everyday",
            "amount": "600.00",
            "occurred_at": "2026-08-03T09:00:00-04:00",
        }
        expense_prepared = self.call("prepare_expense", expense_arguments)
        expense_first = self.call(
            "add_expense",
            {
                **expense_arguments,
                "confirmation_token": expense_prepared.structured_content[
                    "confirmation_token"
                ],
            },
        )
        self.assertFalse(expense_first.is_error)
        expense_retry = self.call("add_expense", expense_arguments)
        self.assertFalse(expense_retry.is_error)
        self.assertTrue(expense_retry.structured_content["duplicate"])

        with self.ledger._connection() as connection:
            expired_token = self.ledger._encode_confirmation(
                connection, {"expires_at": 0}
            )
        expired_expense_retry = self.call(
            "add_expense",
            {**expense_arguments, "confirmation_token": expired_token},
        )
        self.assertFalse(expired_expense_retry.is_error)
        self.assertTrue(expired_expense_retry.structured_content["duplicate"])
        conflicting_expense = self.call(
            "add_expense", {**expense_arguments, "amount": "601.00"}
        )
        self.assertTrue(conflicting_expense.is_error)
        self.assertIn("different ledger operation", conflicting_expense.content[0].text)

        split_arguments = {
            "request_id": "large-split-retry",
            "total_amount": "600.00",
            "allocations": [
                {"member": "Member 1", "category": "Everyday", "amount": "300.00"},
                {"member": "Member 2", "category": "Occasional", "amount": "300.00"},
            ],
            "occurred_at": "2026-08-04T09:00:00-04:00",
        }
        split_prepared = self.call("prepare_split_expense", split_arguments)
        split_first = self.call(
            "add_split_expense",
            {
                **split_arguments,
                "confirmation_token": split_prepared.structured_content[
                    "confirmation_token"
                ],
            },
        )
        self.assertFalse(split_first.is_error)
        split_retry = self.call("add_split_expense", split_arguments)
        self.assertFalse(split_retry.is_error)
        self.assertTrue(split_retry.structured_content["duplicate"])
        expired_split_retry = self.call(
            "add_split_expense",
            {**split_arguments, "confirmation_token": expired_token},
        )
        self.assertFalse(expired_split_retry.is_error)
        self.assertTrue(expired_split_retry.structured_content["duplicate"])
        conflicting_split = self.call(
            "add_split_expense",
            {
                **split_arguments,
                "allocations": [
                    {"member": "Member 1", "category": "Everyday", "amount": "299.00"},
                    {"member": "Member 2", "category": "Occasional", "amount": "301.00"},
                ],
            },
        )
        self.assertTrue(conflicting_split.is_error)
        self.assertIn("different ledger operation", conflicting_split.content[0].text)

    def test_large_correction_requires_exact_confirmation_token(self) -> None:
        original = self.call(
            "add_expense",
            {
                "request_id": "large-correction-original",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "1.00",
                "occurred_at": "2026-08-01T09:00:00-04:00",
            },
        )
        transaction_id = original.structured_content["transaction"]["transaction_id"]
        arguments = {
            "request_id": "large-correction",
            "transaction_id": transaction_id,
            "amount": "600.00",
        }
        rejected = self.call("correct_expense", arguments)
        self.assertTrue(rejected.is_error)
        self.assertIn("prepare_correction", rejected.content[0].text)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 100)

        prepared = self.call("prepare_correction", arguments)
        self.assertFalse(prepared.is_error)
        token = prepared.structured_content["confirmation_token"]
        changed = self.call(
            "correct_expense",
            {**arguments, "amount": "601.00", "confirmation_token": token},
        )
        self.assertTrue(changed.is_error)
        self.assertIn("does not match", changed.content[0].text)
        accepted = self.call(
            "correct_expense", {**arguments, "confirmation_token": token}
        )
        self.assertFalse(accepted.is_error)
        self.assertEqual(accepted.structured_content["replacement"]["amount"], "600.00")

    def test_correction_threshold_boundary_is_exact(self) -> None:
        first = self.call(
            "add_expense",
            {
                "request_id": "correction-boundary-first",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "1.00",
            },
        )
        exactly_500 = self.call(
            "correct_expense",
            {
                "request_id": "correction-exactly-500",
                "transaction_id": first.structured_content["transaction"]["transaction_id"],
                "amount": "500.00",
            },
        )
        self.assertFalse(exactly_500.is_error)

        second = self.call(
            "add_expense",
            {
                "request_id": "correction-boundary-second",
                "member": "Member 2",
                "category": "Occasional",
                "amount": "1.00",
            },
        )
        over_500 = self.call(
            "correct_expense",
            {
                "request_id": "correction-over-500",
                "transaction_id": second.structured_content["transaction"]["transaction_id"],
                "amount": "500.01",
            },
        )
        self.assertTrue(over_500.is_error)
        self.assertIn("prepare_correction", over_500.content[0].text)

    def test_ambiguous_parent_category_fails_closed(self) -> None:
        result = self.call(
            "add_expense",
            {
                "request_id": "telegram-mcp-3",
                "member": "Member 1",
                "category": "Meals",
                "amount": "20.00",
            },
        )
        self.assertTrue(result.is_error)
        self.assertIn("requires a subcategory", result.content[0].text)

    def test_undo_requires_member_and_preserves_audit_behavior(self) -> None:
        self.call(
            "add_expense",
            {
                "request_id": "telegram-mcp-4",
                "member": "Member 1",
                "category": "Occasional",
                "amount": "40.00",
                "occurred_at": "2026-08-03T09:00:00-04:00",
            },
        )
        undone = self.call(
            "undo_last_expense",
            {"request_id": "telegram-mcp-5", "member": "Member 1"},
        )
        self.assertFalse(undone.is_error)
        self.assertEqual(undone.structured_content["amount_cents"], -4000)
        self.assertIsNotNone(undone.structured_content["reverses_entry_id"])

    def test_search_and_linked_refund_round_trip_through_mcp(self) -> None:
        added = self.call(
            "add_expense",
            {
                "request_id": "telegram-mcp-refund-original",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "30.00",
                "business_name": "Corner Market",
                "description": "Supplies",
                "occurred_at": "2026-08-04T09:00:00-04:00",
            },
        )
        transaction_id = added.structured_content["transaction"]["transaction_id"]
        searched = self.call(
            "search_transactions",
            {"business_name": "market", "operation_type": "expense"},
        )
        self.assertFalse(searched.is_error)
        self.assertEqual(searched.structured_content["count"], 1)
        self.assertEqual(
            searched.structured_content["transactions"][0]["transaction_id"],
            transaction_id,
        )

        refunded = self.call(
            "refund_expense",
            {
                "request_id": "telegram-mcp-refund",
                "expense_id": transaction_id,
                "amount": "12.50",
                "description": "Returned item",
                "occurred_at": "2026-08-05T09:00:00-04:00",
            },
        )
        self.assertFalse(refunded.is_error)
        canonical = refunded.structured_content["transaction"]
        self.assertEqual(canonical["operation_type"], "refund")
        self.assertEqual(canonical["refund_link_status"], "linked")
        self.assertEqual(canonical["refund_of_transaction_id"], transaction_id)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 1750)

    def test_reconciliation_round_trip_through_mcp(self) -> None:
        added = self.call(
            "add_expense",
            {
                "request_id": "mcp-reconciliation-expense",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "4.00",
            },
        )
        transaction_id = added.structured_content["transaction"]["transaction_id"]
        marked = self.call(
            "set_transaction_reconciliation",
            {
                "request_id": "mcp-reconciliation-mark",
                "transaction_id": transaction_id,
                "reconciled": True,
            },
        )
        self.assertFalse(marked.is_error)
        self.assertTrue(marked.structured_content["changed"])
        searched = self.call(
            "search_transactions", {"reconciliation_status": "reconciled"}
        )
        self.assertFalse(searched.is_error)
        self.assertEqual(searched.structured_content["count"], 1)
        invalid_boolean = self.call(
            "set_transaction_reconciliation",
            {
                "request_id": "mcp-reconciliation-invalid",
                "transaction_id": transaction_id,
                "reconciled": {"yes": True},
            },
        )
        self.assertTrue(invalid_boolean.is_error)

    def test_unlinked_refund_requires_member_and_category(self) -> None:
        rejected = self.call(
            "refund_expense",
            {"request_id": "unlinked-missing", "amount": "5.00"},
        )
        self.assertTrue(rejected.is_error)
        self.assertIn("member is required", rejected.content[0].text)

        accepted = self.call(
            "refund_expense",
            {
                "request_id": "unlinked-complete",
                "amount": "5.00",
                "member": "Member 2",
                "category": "Occasional",
                "business_name": "Online Store",
            },
        )
        self.assertFalse(accepted.is_error)
        self.assertEqual(
            accepted.structured_content["transaction"]["refund_link_status"],
            "unlinked",
        )


if __name__ == "__main__":
    unittest.main()
