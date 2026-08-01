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

    def test_exposes_only_the_four_approved_tools(self) -> None:
        async def list_tools() -> set[str]:
            async with Client(self.server) as client:
                result = await client.list_tools()
                return {tool.name for tool in result.tools}

        self.assertEqual(
            asyncio.run(list_tools()),
            {"add_expense", "list_spending", "list_budget_categories", "undo_last_expense"},
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


if __name__ == "__main__":
    unittest.main()
