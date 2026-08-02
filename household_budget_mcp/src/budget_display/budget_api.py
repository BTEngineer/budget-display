"""Authenticated JSON API used by the Home Assistant frontend integration."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .ledger import BudgetLedger, BudgetValidationError, DuplicateRequestError
from .receipt_store import MAX_RECEIPT_BYTES, ReceiptStore


MAX_JSON_BYTES = 64 * 1024
LARGE_EXPENSE_CENTS = 50_000


class ReceiptCleanupError(Exception):
    """Raised after a safe ledger operation when receipt deletion fails."""


def _bounded_text(value: object, *, name: str, maximum: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise BudgetValidationError(f"{name} must contain 1 to {maximum} characters")
    return result


def _confirm_large_amount(amount: object, confirmed: object) -> None:
    try:
        decimal = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise BudgetValidationError("amount must be a decimal monetary value") from exc
    if decimal.is_finite() and int(decimal * 100) > LARGE_EXPENSE_CENTS and confirmed is not True:
        raise BudgetValidationError(
            "expense exceeds $500 and requires explicit large-expense acknowledgement"
        )


class BudgetJSONAPI:
    """Intercept `/api/v1` while preserving the MCP application's lifespan."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        ledger: BudgetLedger,
        receipts: ReceiptStore,
        allowed_hosts: tuple[str, ...],
    ):
        self.app = app
        self.ledger = ledger
        self.receipts = receipts
        self.allowed_hosts = {host.casefold() for host in allowed_hosts}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/v1"):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.headers.get("host", "").casefold() not in self.allowed_hosts:
            response = JSONResponse({"error": "untrusted Host header"}, status_code=421)
            await response(scope, receive, send)
            return
        try:
            response = await self._dispatch(request)
        except DuplicateRequestError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=409)
        except BudgetValidationError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=422)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = JSONResponse({"error": "request body must be valid JSON"}, status_code=400)
        except (TypeError, ValueError) as exc:
            response = JSONResponse({"error": f"invalid field value: {exc}"}, status_code=422)
        except ReceiptCleanupError as exc:
            response = JSONResponse({"error": str(exc)}, status_code=500)
        await response(scope, receive, send)

    async def _json(self, request: Request) -> dict[str, Any]:
        declared = int(request.headers.get("content-length", "0") or 0)
        if declared > MAX_JSON_BYTES:
            raise BudgetValidationError("JSON request is too large")
        body = await request.body()
        if len(body) > MAX_JSON_BYTES:
            raise BudgetValidationError("JSON request is too large")
        parsed = json.loads(body or b"{}")
        if not isinstance(parsed, dict):
            raise BudgetValidationError("JSON request must be an object")
        return parsed

    def _occurred_at(self, payload: dict[str, Any]) -> str | None:
        if payload.get("occurred_at"):
            return str(payload["occurred_at"])
        if not payload.get("occurred_on"):
            return None
        try:
            date = datetime.strptime(str(payload["occurred_on"]), "%Y-%m-%d")
        except ValueError as exc:
            raise BudgetValidationError("occurred_on must use YYYY-MM-DD format") from exc
        return date.replace(hour=12, tzinfo=self.ledger.household_timezone).isoformat()

    async def _dispatch(self, request: Request) -> JSONResponse:
        path = request.url.path.rstrip("/")
        method = request.method.upper()
        if method == "GET" and path == "/api/v1/config":
            return JSONResponse(
                {
                    "members": list(self.ledger.members),
                    "categories": self.ledger.list_budget_categories(),
                    "maximum_receipt_bytes": self.receipts.maximum_bytes,
                }
            )
        if method == "GET" and path == "/api/v1/summary":
            month = request.query_params.get("month") or datetime.now(
                self.ledger.household_timezone
            ).strftime("%Y-%m")
            return JSONResponse(self.ledger.list_spending(month=month))
        if method == "GET" and path == "/api/v1/recent":
            try:
                limit = int(request.query_params.get("limit", "10"))
            except ValueError as exc:
                raise BudgetValidationError("limit must be an integer") from exc
            return JSONResponse({"expenses": self.ledger.list_recent_expenses(limit=limit)})
        if method == "POST" and path == "/api/v1/expenses":
            payload = await self._json(request)
            _confirm_large_amount(payload.get("amount"), payload.get("confirm_large_expense"))
            entry = self.ledger.add_expense(
                request_id=_bounded_text(payload.get("request_id"), name="request_id", maximum=200),
                member=_bounded_text(payload.get("member"), name="member", maximum=80),
                category=_bounded_text(payload.get("category"), name="category", maximum=170),
                amount=str(payload.get("amount", "")),
                business_name=str(payload.get("business_name", "")).strip()[:200],
                description=str(payload.get("description", "")).strip()[:1000],
                occurred_at=self._occurred_at(payload),
            )
            return JSONResponse({"entry": entry}, status_code=201)
        if method == "POST" and path == "/api/v1/undo":
            payload = await self._json(request)
            entry = self.ledger.undo_last_expense(
                request_id=_bounded_text(payload.get("request_id"), name="request_id", maximum=200),
                member=_bounded_text(payload.get("member"), name="member", maximum=80),
                entry_id=(int(payload["entry_id"]) if payload.get("entry_id") is not None else None),
            )
            return JSONResponse({"entry": entry}, status_code=201)
        if method == "POST" and path == "/api/v1/receipts":
            declared = int(request.headers.get("content-length", "0") or 0)
            if declared > MAX_RECEIPT_BYTES:
                raise BudgetValidationError("receipt exceeds the 12 MB limit")
            content = await request.body()
            stored = self.receipts.store(
                content=content,
                content_type=request.headers.get("content-type", ""),
                original_filename=request.headers.get("x-receipt-filename", "receipt"),
            )
            draft = self.ledger.register_receipt(
                digest=stored.digest,
                relative_path=stored.relative_path,
                content_type=stored.content_type,
                byte_size=stored.byte_size,
                original_filename=stored.original_filename,
            )
            if draft["status"] == "confirmed":
                try:
                    self.receipts.delete(stored.relative_path)
                except OSError as exc:
                    raise ReceiptCleanupError(
                        "confirmed duplicate receipt cleanup failed; try again"
                    ) from exc
            return JSONResponse({"draft": draft}, status_code=200 if draft["duplicate"] else 201)

        parts = path.split("/")
        if len(parts) == 6 and parts[:4] == ["", "api", "v1", "receipts"]:
            try:
                draft_id = int(parts[4])
            except ValueError as exc:
                raise BudgetValidationError("receipt draft ID must be an integer") from exc
            action = parts[5]
            if method == "POST" and action == "analysis":
                payload = await self._json(request)
                fields = payload.get("fields")
                if not isinstance(fields, dict):
                    raise BudgetValidationError("analysis fields must be an object")
                draft = self.ledger.update_receipt_draft(
                    draft_id=draft_id,
                    ai_entity_id=_bounded_text(
                        payload.get("ai_entity_id"), name="ai_entity_id", maximum=255
                    ),
                    fields=fields,
                )
                return JSONResponse({"draft": draft})
            if method == "POST" and action == "confirm":
                payload = await self._json(request)
                _confirm_large_amount(payload.get("amount"), payload.get("confirm_large_expense"))
                result = self.ledger.confirm_receipt_draft(
                    draft_id=draft_id,
                    request_id=_bounded_text(
                        payload.get("request_id"), name="request_id", maximum=200
                    ),
                    member=_bounded_text(payload.get("member"), name="member", maximum=80),
                    category=_bounded_text(
                        payload.get("category"), name="category", maximum=170
                    ),
                    amount=str(payload.get("amount", "")),
                    business_name=(
                        str(payload["business_name"]).strip()[:200]
                        if payload.get("business_name") is not None
                        else None
                    ),
                    description=str(payload.get("description", "")).strip()[:1000],
                    occurred_at=self._occurred_at(payload),
                )
                try:
                    self.receipts.delete(str(result["draft"]["relative_path"]))
                except OSError as exc:
                    raise ReceiptCleanupError(
                        "transaction was recorded but receipt cleanup failed; retry with the same request ID"
                    ) from exc
                result["receipt_deleted"] = True
                return JSONResponse(result, status_code=201)
        return JSONResponse({"error": "not found"}, status_code=404)
