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

    def test_exposes_only_the_six_approved_tools(self) -> None:
        async def list_tools() -> dict[str, dict[str, object]]:
            async with Client(self.server) as client:
                result = await client.list_tools()
                return {tool.name: tool.input_schema for tool in result.tools}

        tools = asyncio.run(list_tools())
        self.assertEqual(
            set(tools),
            {
                "add_expense",
                "list_spending",
                "list_budget_categories",
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

    def test_large_expense_requires_caller_acknowledgement(self) -> None:
        arguments = {
            "request_id": "telegram-mcp-2",
            "member": "Member 1",
            "category": "Everyday",
            "amount": "600.00",
            "occurred_at": "2026-08-02T09:00:00-04:00",
        }
        rejected = self.call("add_expense", arguments)
        self.assertTrue(rejected.is_error)
        self.assertIn("caller policy must acknowledge", rejected.content[0].text)
        self.assertEqual(self.ledger.list_spending(month="2026-08")["spent_cents"], 0)

        accepted = self.call(
            "add_expense", {**arguments, "confirm_large_expense": True}
        )
        self.assertFalse(accepted.is_error)
        self.assertEqual(accepted.structured_content["amount_cents"], 60000)

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
