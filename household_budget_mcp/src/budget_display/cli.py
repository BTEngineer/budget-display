"""Small local CLI for exercising the ledger before Hermes integration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ledger import BudgetLedger, BudgetValidationError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="budget-display")
    parser.add_argument("--database", type=Path, default=Path("data/budget.db"))
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init")
    commands.add_parser("categories")

    add = commands.add_parser("add")
    add.add_argument("--request-id", required=True)
    add.add_argument("--member", required=True)
    add.add_argument("--category", required=True)
    add.add_argument("--amount", required=True)
    add.add_argument("--description", default="")
    add.add_argument("--occurred-at")

    undo = commands.add_parser("undo")
    undo.add_argument("--request-id", required=True)
    undo.add_argument("--member")

    spending = commands.add_parser("spending")
    spending.add_argument("--month", required=True)

    budget = commands.add_parser("set-budget")
    budget.add_argument("--month", required=True)
    budget.add_argument("--category", required=True)
    budget.add_argument("--amount", required=True)

    default_budget = commands.add_parser("set-default-budget")
    default_budget.add_argument("--category", required=True)
    default_budget.add_argument("--amount", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    ledger = BudgetLedger(args.database)
    ledger.initialize()
    try:
        if args.command == "init":
            result: object = {"database": str(args.database), "initialized": True}
        elif args.command == "categories":
            result = ledger.list_budget_categories()
        elif args.command == "add":
            result = ledger.add_expense(
                request_id=args.request_id,
                member=args.member,
                category=args.category,
                amount=args.amount,
                description=args.description,
                occurred_at=args.occurred_at,
            )
        elif args.command == "undo":
            result = ledger.undo_last_expense(
                request_id=args.request_id, member=args.member
            )
        elif args.command == "spending":
            result = ledger.list_spending(month=args.month)
        elif args.command == "set-budget":
            result = ledger.set_monthly_budget(
                month=args.month, category=args.category, amount=args.amount
            )
        else:
            result = ledger.set_default_monthly_budget(
                category=args.category, amount=args.amount
            )
    except BudgetValidationError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
