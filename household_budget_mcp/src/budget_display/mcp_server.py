"""Narrow stdio MCP boundary for Hermes budget operations.

This server intentionally exposes no arbitrary SQL, filesystem, shell, budget
configuration, or deletion tools.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from .ledger import BudgetLedger, BudgetValidationError


LARGE_EXPENSE_CENTS = 50_000
MAX_REQUEST_ID_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500


def default_database_path() -> Path:
    """Resolve the ledger path without depending on the host's working directory."""
    configured = os.environ.get("BUDGET_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data" / "budget.db"


def _validate_bounded_text(value: str, *, name: str, maximum: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise BudgetValidationError(f"{name} is required")
    if len(cleaned) > maximum:
        raise BudgetValidationError(f"{name} cannot exceed {maximum} characters")
    return cleaned


def _amount_cents_for_confirmation(amount: str) -> int:
    try:
        parsed = Decimal(amount)
    except (InvalidOperation, ValueError) as exc:
        raise BudgetValidationError("amount must be a decimal monetary value") from exc
    if not parsed.is_finite() or parsed <= 0 or parsed.as_tuple().exponent < -2:
        raise BudgetValidationError(
            "amount must be greater than zero with no more than two decimal places"
        )
    return int(parsed * 100)


def create_server(ledger: BudgetLedger) -> MCPServer:
    """Build an MCP server bound only to the supplied budget ledger."""
    server = MCPServer("Household Budget")

    def ready_ledger() -> BudgetLedger:
        ledger.initialize()
        return ledger

    @server.tool()
    def add_expense(
        request_id: str,
        member: str,
        category: str,
        amount: str,
        description: str = "",
        occurred_at: str | None = None,
        confirm_large_expense: bool = False,
    ) -> dict[str, Any]:
        """Record one household expense.

        Use the source message ID as request_id. Member and category must match
        the Home Assistant app configuration; a parent with children requires
        Parent/Child notation. Amount is a positive decimal string such as
        "8.00". Expenses over $500 require confirm_large_expense=true after
        explicit user confirmation. occurred_at must be an ISO 8601 timestamp
        with timezone when supplied.
        """
        clean_request_id = _validate_bounded_text(
            request_id, name="request_id", maximum=MAX_REQUEST_ID_LENGTH
        )
        clean_description = description.strip()
        if len(clean_description) > MAX_DESCRIPTION_LENGTH:
            raise BudgetValidationError(
                f"description cannot exceed {MAX_DESCRIPTION_LENGTH} characters"
            )
        cents = _amount_cents_for_confirmation(amount)
        if cents > LARGE_EXPENSE_CENTS and not confirm_large_expense:
            raise BudgetValidationError(
                "expense exceeds $500; ask the user to confirm the exact amount, member, and category, then retry with confirm_large_expense=true"
            )
        return ready_ledger().add_expense(
            request_id=clean_request_id,
            member=member,
            category=category,
            amount=amount,
            description=clean_description,
            occurred_at=occurred_at,
        )

    @server.tool()
    def list_spending(month: str) -> dict[str, Any]:
        """Return budget, remaining, member, and category totals for YYYY-MM."""
        return ready_ledger().list_spending(month=month)

    @server.tool()
    def list_budget_categories() -> list[dict[str, Any]]:
        """List valid categories and identify which accept expenses."""
        return ready_ledger().list_budget_categories()

    @server.tool()
    def undo_last_expense(request_id: str, member: str) -> dict[str, Any]:
        """Reverse, without deleting, the named member's latest active expense.

        Use the current source message ID as request_id. The original entry and
        linked reversal remain in the audit history.
        """
        clean_request_id = _validate_bounded_text(
            request_id, name="request_id", maximum=MAX_REQUEST_ID_LENGTH
        )
        return ready_ledger().undo_last_expense(
            request_id=clean_request_id, member=member
        )

    return server


mcp = create_server(BudgetLedger(default_database_path()))


def main() -> None:
    """Run the server over stdio; stdout is reserved for MCP protocol data."""
    mcp.run()


if __name__ == "__main__":
    main()
