from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

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

    def test_exposes_only_the_thirteen_approved_tools(self) -> None:
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
                "refund_expense",
            },
        )
        self.assertTrue(
            {"category", "business_name", "cursor", "operation_type"}
            <= set(tools["search_transactions"]["properties"])
        )
        self.assertEqual(
            set(tools["refund_expense"]["required"]), {"request_id", "amount"}
        )

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

    def test_new_write_and_read_tools_round_trip_through_mcp(self) -> None:
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
