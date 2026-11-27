"""Narrow stdio MCP boundary for Hermes budget operations.

This server intentionally exposes no arbitrary SQL, filesystem, shell, general
budget configuration, or deletion tools. Christmas attribution is opt-in only.
"""

from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from pydantic import StrictBool

from .ledger import (
    BudgetLedger,
    BudgetValidationError,
    MAX_ENTRY_DESCRIPTION_LENGTH,
)
from .christmas import ChristmasLedger


MAX_REQUEST_ID_LENGTH = 200
MAX_DESCRIPTION_LENGTH = MAX_ENTRY_DESCRIPTION_LENGTH
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
    def prepare_expense(
        request_id: str,
        member: str,
        category: str,
        amount: str,
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """Prepare an exact expense for explicit confirmation.

        The returned short-lived token is bound to every supplied field. Use
        it as confirmation_token in add_expense without changing the payload.
        This two-step flow is mandatory for expenses over $500.
        """
        return ready_ledger().prepare_expense(
            request_id=request_id,
            member=member,
            category=category,
            amount=amount,
            business_name=business_name,
            description=description,
            occurred_at=occurred_at,
        )

    @server.tool()
    def add_expense(
        request_id: str,
        member: str,
        category: str,
        amount: str,
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Record one household expense.

        Use the source message ID as request_id. Member and category must match
        the Home Assistant app configuration; a parent with children requires
        Parent/Child notation. Amount is a positive decimal string such as
        "8.00". Store a merchant in business_name rather than embedding it only
        in description. Expenses over $500 require a token from prepare_expense
        that matches the exact payload. occurred_at must be an ISO 8601
        timestamp with timezone when supplied.

        Ordinary expenses are never automatically Christmas spending. Do not
        infer Christmas intent from a receipt, date, merchant, item description,
        or suggested category, or ask unsolicited Christmas questions. Only
        use set_christmas_attribution after the user specifically requests it.
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
        active_ledger = ready_ledger()
        return active_ledger.add_expense(
            request_id=clean_request_id,
            member=member,
            category=category,
            amount=amount,
            business_name=clean_business_name,
            description=clean_description,
            occurred_at=occurred_at,
            confirmation_token=confirmation_token,
            require_confirmation=True,
        )

    @server.tool()
    def prepare_correction(
        request_id: str,
        transaction_id: str,
        member: str | None = None,
        category: str | None = None,
        amount: str | None = None,
        business_name: str | None = None,
        description: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """Prepare an exact correction for explicit confirmation.

        The returned token is required when the resolved replacement exceeds
        $500 and is bound to the target plus every resolved replacement field.
        """
        return ready_ledger().prepare_correction(
            request_id=request_id,
            transaction_id=transaction_id,
            member=member,
            category=category,
            amount=amount,
            business_name=business_name,
            description=description,
            occurred_at=occurred_at,
        )

    @server.tool()
    def correct_expense(
        request_id: str,
        transaction_id: str,
        member: str | None = None,
        category: str | None = None,
        amount: str | None = None,
        business_name: str | None = None,
        description: str | None = None,
        occurred_at: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Correct a verified expense without deleting history.

        Only supplied fields change. The server atomically records a linked
        reversal plus replacement. Refunded or reversed targets fail closed.
        """
        return ready_ledger().correct_expense(
            request_id=request_id,
            transaction_id=transaction_id,
            member=member,
            category=category,
            amount=amount,
            business_name=business_name,
            description=description,
            occurred_at=occurred_at,
            confirmation_token=confirmation_token,
        )

    @server.tool()
    def prepare_split_expense(
        request_id: str,
        total_amount: str,
        allocations: list[dict[str, str]],
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        """Prepare an exact split expense for explicit confirmation."""
        return ready_ledger().prepare_split_expense(
            request_id=request_id,
            total_amount=total_amount,
            allocations=allocations,
            business_name=business_name,
            description=description,
            occurred_at=occurred_at,
        )

    @server.tool()
    def add_split_expense(
        request_id: str,
        total_amount: str,
        allocations: list[dict[str, str]],
        business_name: str = "",
        description: str = "",
        occurred_at: str | None = None,
        confirmation_token: str | None = None,
    ) -> dict[str, Any]:
        """Atomically split one purchase across people or categories.

        Each allocation needs member, category, and positive decimal amount.
        Allocations must add up exactly to total_amount or nothing is recorded.
        Never infer Christmas intent or ask Christmas questions for ordinary
        receipts. Explicitly requested Christmas details are assigned separately
        through set_christmas_attribution without posting another expense.
        """
        return ready_ledger().add_split_expense(
            request_id=request_id,
            total_amount=total_amount,
            allocations=allocations,
            business_name=business_name,
            description=description,
            occurred_at=occurred_at,
            confirmation_token=confirmation_token,
            require_confirmation=True,
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
    def suggest_expense_classification(
        business_name: str = "",
        description: str = "",
        limit: int = 5,
    ) -> dict[str, Any]:
        """Suggest categories from configured aliases and prior expenses.

        Suggestions are read-only evidence. The caller must still supply an
        explicit category to a write tool.
        A suggestion never supplies Christmas intent. Do not infer Christmas
        attribution or ask unsolicited Christmas questions from these results.
        """
        return ready_ledger().suggest_expense_classification(
            business_name=business_name,
            description=description,
            limit=limit,
        )

    @server.tool()
    def get_budget_outlook(
        month: str, as_of: str | None = None
    ) -> dict[str, Any]:
        """Return server-calculated budget pace, projection, and category risks."""
        return ready_ledger().get_budget_outlook(month=month, as_of=as_of)

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
        reconciliation_status: str = "all",
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
            reconciliation_status=reconciliation_status,
            sort_order=sort_order,
            limit=limit,
            cursor=cursor,
        )

    @server.tool()
    def set_transaction_reconciliation(
        request_id: str,
        transaction_id: str,
        reconciled: bool,
    ) -> dict[str, Any]:
        """Mark a posted card transaction as reconciled or unreconciled.

        Use a stable source request ID. Split allocations are always updated
        together. Internal reversal entries are not valid targets.
        """
        return ready_ledger().set_transaction_reconciliation(
            request_id=request_id,
            transaction_id=transaction_id,
            reconciled=reconciled,
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

    @server.tool()
    def upsert_christmas_recipient(
        request_id: str,
        name: str,
        recipient_id: str | None = None,
        active: bool = True,
    ) -> dict[str, Any]:
        """Create or update a reusable Christmas gift recipient.

        A recipient identifies who receives a gift, independently of the
        expense's spender/member. Use the returned stable recipient_id for
        allocations and each year's comparison group. Deactivation preserves
        history and never rewrites previous years' groups.
        """
        return ChristmasLedger(ready_ledger()).upsert_recipient(
            request_id=request_id,
            name=name,
            recipient_id=recipient_id,
            active=active,
        )

    @server.tool()
    def list_christmas_recipients(
        include_inactive: bool = False,
    ) -> dict[str, Any]:
        """List reusable gift recipients and their stable recipient IDs."""
        return ChristmasLedger(ready_ledger()).list_recipients(
            include_inactive=include_inactive,
        )

    @server.tool()
    def configure_christmas_year(
        request_id: str,
        year: int,
        child_recipient_ids: list[str] | None = None,
        budget_amount: str | None = None,
        clear_budget: bool = False,
    ) -> dict[str, Any]:
        """Set a Christmas year's annual limit or child comparison group.

        Supply only the settings being changed. budget_amount is a decimal
        amount; clear_budget removes the limit, showing it as not set rather
        than zero. Changes are audited and target only the specified year.
        No automatic rollover or general monthly-budget changes are performed.
        An unset or exceeded limit never prevents recording actual purchases.
        """
        return ChristmasLedger(ready_ledger()).configure_year(
            request_id=request_id,
            year=year,
            child_recipient_ids=child_recipient_ids,
            budget_amount=budget_amount,
            clear_budget=clear_budget,
        )

    @server.tool()
    def set_christmas_attribution(
        request_id: str,
        transaction_id: str,
        gifts: list[dict[str, Any]],
        explicitly_requested: StrictBool,
        overhead_amount: str = "0.00",
        ordinary_amount: str | None = None,
        year: int | None = None,
    ) -> dict[str, Any]:
        """Assign Christmas details only after a specific user request.

        NEVER infer Christmas intent from receipt uploads or extraction, dates,
        merchants, item descriptions, or suggested categories. Do not ask
        unsolicited Christmas questions for ordinary purchases. Set
        explicitly_requested=true only when the user specifically asks to
        track this purchase or identified group of purchases for Christmas;
        their explicit instruction already supplies opt-in.

        Record the expense normally first, then target its transaction_id (or
        split purchase/group ID). This audited assignment does not post another
        financial expense and may correct existing attribution. year defaults
        to the purchase's local year on first explicit assignment; on edits an
        omitted year preserves its existing intended Christmas year.

        Each gift has description and positive decimal amount, an optional
        gift_id to retain on edits, and either recipient_ids for equal shares
        or recipient_amounts=[{recipient_id, amount}] for exact custom shares.
        Omit recipients for a visibly unassigned gift. Tax, shipping, wrapping,
        and other Christmas costs belong in overhead_amount, not gift value.
        ordinary_amount is the non-Christmas remainder; portions must reconcile
        to the purchase. An empty gifts list allows overhead-only attribution
        or removal of previous attribution when all money is ordinary.
        """
        return ChristmasLedger(ready_ledger()).assign_purchase(
            request_id=request_id,
            transaction_id=transaction_id,
            gifts=gifts,
            explicitly_requested=explicitly_requested,
            overhead_amount=overhead_amount,
            ordinary_amount=ordinary_amount,
            year=year,
        )

    @server.tool()
    def set_christmas_refund_attribution(
        request_id: str,
        transaction_id: str,
        gifts: list[dict[str, Any]],
        explicitly_requested: StrictBool,
        overhead_amount: str = "0.00",
        ordinary_amount: str = "0.00",
    ) -> dict[str, Any]:
        """Resolve a linked refund's Christmas breakdown on explicit request.

        Target the refund's transaction_id. Each gifts item contains the
        original gift_id and positive decimal amount refunded for that gift.
        Put refunded tax/shipping/wrapping in overhead_amount and refunded
        non-Christmas items in ordinary_amount; portions must equal the refund.
        Shared gifts retain their original recipient proportions. A full
        refund inherits the original explicit allocation; ambiguous partial
        returns remain visibly unresolved until their breakdown is supplied.
        Never guess Christmas intent from an ordinary refund receipt or infer
        a partial return's gift-versus-overhead breakdown.
        """
        return ChristmasLedger(ready_ledger()).resolve_refund(
            request_id=request_id,
            transaction_id=transaction_id,
            gifts=gifts,
            explicitly_requested=explicitly_requested,
            overhead_amount=overhead_amount,
            ordinary_amount=ordinary_amount,
        )

    @server.tool()
    def get_christmas_summary(year: int) -> dict[str, Any]:
        """Return a server-calculated annual Christmas and gift comparison.

        Includes only explicitly attributed purchases and their linked returns.
        Ordinary or untagged receipts are never classified by this query. Show
        unresolved/unassigned warnings with totals and comparisons; zero
        recorded gift value does not establish that a child received no gifts.
        Limits, remaining amounts, and gaps are informational, not write blocks.
        """
        return ChristmasLedger(ready_ledger()).summary(year=year)

    @server.tool()
    def list_christmas_attributions(
        year: int,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List bounded Christmas purchase details, allocations, and history.

        Follow pagination for detail; get_christmas_summary calculates complete
        annual totals independently of this page. Only explicit attribution is
        included, and incomplete refund or recipient attribution stays visible.
        """
        return ChristmasLedger(ready_ledger()).details(
            year=year,
            limit=limit,
            offset=offset,
        )

    return server


mcp = create_server(BudgetLedger(default_database_path()))


def main() -> None:
    """Run the server over stdio; stdout is reserved for MCP protocol data."""
    mcp.run()


if __name__ == "__main__":
    main()
