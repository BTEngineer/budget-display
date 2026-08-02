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
MAX_BUSINESS_NAME_LENGTH = 200


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
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
        confirm_large_expense: bool = False,
    ) -> dict[str, Any]:
        """Record one household expense.

        Use the source message ID as request_id. Member and category must match
        the Home Assistant app configuration; a parent with children requires
        Parent/Child notation. Amount is a positive decimal string such as
        "8.00". Store a merchant in business_name rather than embedding it only
        in description. Expenses over $500 require the caller to acknowledge
        the amount with confirm_large_expense=true. This flag is an advisory
        client-policy safeguard; the server cannot prove that a person approved
        it. occurred_at must be an ISO 8601 timestamp with timezone when supplied.
        """
        clean_request_id = _validate_bounded_text(
            request_id, name="request_id", maximum=MAX_REQUEST_ID_LENGTH
        )
        clean_description = description.strip()
        if len(clean_description) > MAX_DESCRIPTION_LENGTH:
            raise BudgetValidationError(
                f"description cannot exceed {MAX_DESCRIPTION_LENGTH} characters"
            )
        clean_business_name = business_name.strip()
        if len(clean_business_name) > MAX_BUSINESS_NAME_LENGTH:
            raise BudgetValidationError(
                f"business_name cannot exceed {MAX_BUSINESS_NAME_LENGTH} characters"
            )
        cents = _amount_cents_for_confirmation(amount)
        if cents > LARGE_EXPENSE_CENTS and not confirm_large_expense:
            raise BudgetValidationError(
                "expense exceeds $500; caller policy must acknowledge the exact amount, member, and category, then retry with confirm_large_expense=true"
            )
        return ready_ledger().add_expense(
            request_id=clean_request_id,
            member=member,
            category=category,
            amount=amount,
            business_name=clean_business_name,
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

    @server.tool()
    def search_transactions(
        start_at: str | None = None,
        end_at: str | None = None,
        member: str | None = None,
        category: str | None = None,
        business_name: str | None = None,
        description_query: str | None = None,
        minimum_amount: str | None = None,
        maximum_amount: str | None = None,
        transaction_id: str | None = None,
        request_id: str | None = None,
        operation_type: str = "all",
        status: str = "active",
        sort_order: str = "descending",
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Search canonical transactions with bounded filters and pagination.

        Business and description searches are case-insensitive literal
        substrings. Category can be a top-level category (including its
        children) or an exact Parent/Child path. Follow next_cursor until
        has_more is false when complete reconciliation is required.
        """
        return ready_ledger().search_transactions(
            start_at=start_at,
            end_at=end_at,
            member=member,
            category=category,
            business_name=business_name,
            description_query=description_query,
            minimum_amount=minimum_amount,
            maximum_amount=maximum_amount,
            transaction_id=transaction_id,
            request_id=request_id,
            operation_type=operation_type,
            status=status,
            sort_order=sort_order,
            limit=limit,
            cursor=cursor,
        )

    @server.tool()
    def refund_expense(
        request_id: str,
        amount: str,
        expense_id: str | None = None,
        member: str | None = None,
        category: str | None = None,
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """Record a linked or unlinked household refund.

        Supply expense_id from search_transactions for a verified linked
        refund. If no matching expense can be found, omit expense_id and supply
        member, category, and the exact refund amount. Full refunds use the
        remaining_refundable_amount returned by search_transactions.
        """
        clean_request_id = _validate_bounded_text(
            request_id, name="request_id", maximum=MAX_REQUEST_ID_LENGTH
        )
        clean_description = description.strip()
        if len(clean_description) > MAX_DESCRIPTION_LENGTH:
            raise BudgetValidationError(
                f"description cannot exceed {MAX_DESCRIPTION_LENGTH} characters"
            )
        clean_business_name = business_name.strip()
        if len(clean_business_name) > MAX_BUSINESS_NAME_LENGTH:
            raise BudgetValidationError(
                f"business_name cannot exceed {MAX_BUSINESS_NAME_LENGTH} characters"
            )
        _amount_cents_for_confirmation(amount)
        return ready_ledger().refund_expense(
            request_id=clean_request_id,
            amount=amount,
            expense_id=expense_id,
            member=member,
            category=category,
            business_name=clean_business_name,
            description=clean_description,
            occurred_at=occurred_at,
        )

    return server


mcp = create_server(BudgetLedger(default_database_path()))


def main() -> None:
    """Run the server over stdio; stdout is reserved for MCP protocol data."""
    mcp.run()


if __name__ == "__main__":
    main()
