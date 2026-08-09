"""Authenticated browser API and OpenAI receipt orchestration."""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .client import BudgetAPIError, BudgetClient
from .const import CONF_AI_TASK_ENTITY, DATA_CLIENT, DOMAIN


MAX_RECEIPT_BYTES = 12 * 1024 * 1024
ALLOWED_MEDIA = {"image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"}
LOGGER = logging.getLogger(__name__)


def _client(hass: HomeAssistant) -> BudgetClient:
    return hass.data[DOMAIN][DATA_CLIENT]


def _entry(hass: HomeAssistant):
    return hass.data[DOMAIN]["entry"]


def _error(message: str, status: int = 422) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _admin_error(request: web.Request) -> web.Response | None:
    user = request.get("hass_user")
    if user is None or not user.is_admin:
        return _error("Household Budget is restricted to Home Assistant administrators", 403)
    return None


class BudgetConfigView(HomeAssistantView):
    url = "/api/household_budget/config"
    name = "api:household_budget:config"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        if denied := _admin_error(request):
            return denied
        try:
            return self.json(await _client(request.app["hass"]).config())
        except BudgetAPIError as exc:
            return _error(str(exc))


class BudgetSummaryView(HomeAssistantView):
    url = "/api/household_budget/summary"
    name = "api:household_budget:summary"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        if denied := _admin_error(request):
            return denied
        try:
            payload = await _client(request.app["hass"]).request(
                "GET", "/api/v1/summary", params={"month": request.query.get("month", "")}
            )
            return self.json(payload)
        except BudgetAPIError as exc:
            return _error(str(exc))


class BudgetRecentView(HomeAssistantView):
    url = "/api/household_budget/recent"
    name = "api:household_budget:recent"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        if denied := _admin_error(request):
            return denied
        try:
            payload = await _client(request.app["hass"]).request(
                "GET", "/api/v1/recent", params={"limit": request.query.get("limit", "10")}
            )
            return self.json(payload)
        except BudgetAPIError as exc:
            return _error(str(exc))


class BudgetExpenseView(HomeAssistantView):
    url = "/api/household_budget/expense"
    name = "api:household_budget:expense"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        if denied := _admin_error(request):
            return denied
        try:
            payload = await request.json()
            result = await _client(request.app["hass"]).request(
                "POST", "/api/v1/expenses", json=payload
            )
            return self.json(result, status_code=201)
        except (BudgetAPIError, json.JSONDecodeError) as exc:
            return _error(str(exc))


class BudgetUndoView(HomeAssistantView):
    url = "/api/household_budget/undo"
    name = "api:household_budget:undo"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        if denied := _admin_error(request):
            return denied
        try:
            result = await _client(request.app["hass"]).request(
                "POST", "/api/v1/undo", json=await request.json()
            )
            return self.json(result, status_code=201)
        except (BudgetAPIError, json.JSONDecodeError) as exc:
            return _error(str(exc))


class BudgetReceiptView(HomeAssistantView):
    url = "/api/household_budget/receipt"
    name = "api:household_budget:receipt"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        if denied := _admin_error(request):
            return denied
        hass: HomeAssistant = request.app["hass"]
        uploaded: dict[str, Any] | None = None
        media_type = request.content_type.lower()
        if media_type not in ALLOWED_MEDIA:
            return _error("Receipt must be a JPEG, PNG, or PDF file")
        body = await request.content.read(MAX_RECEIPT_BYTES + 1)
        if len(body) > MAX_RECEIPT_BYTES:
            return _error("Receipt exceeds the 12 MB limit", 413)
        filename = unquote(request.headers.get("X-Receipt-Filename", "receipt"))
        try:
            uploaded = await _client(hass).request(
                "POST",
                "/api/v1/receipts",
                data=body,
                headers={"Content-Type": media_type, "X-Receipt-Filename": filename},
            )
            draft_id = int(uploaded["draft"]["id"])
            if uploaded["draft"].get("duplicate") and uploaded["draft"].get("status") == "confirmed":
                return _error("This receipt is already linked to a confirmed expense", 409)
            if uploaded["draft"].get("duplicate") and uploaded["draft"].get("status") == "analyzed":
                return self.json({"draft": uploaded["draft"], "duplicate": True})
            config = await _client(hass).config()
            categories = [
                (f"{item['parent']}/{item['name']}" if item.get("parent") else item["name"])
                for item in config["categories"]
                if item.get("accepts_expenses")
            ]
            media_roots = hass.config.media_dirs
            media_key = "local" if "local" in media_roots else next(iter(media_roots), None)
            media_root = media_roots.get(media_key) if media_key else None
            if media_root is None:
                return _error("Home Assistant has no configured local media directory", 503)
            temporary_directory = Path(media_root) / "household_budget_pending"
            await hass.async_add_executor_job(temporary_directory.mkdir, 0o700, True, True)
            temporary_name = f"{uuid.uuid4().hex}{ALLOWED_MEDIA[media_type]}"
            temporary_path = temporary_directory / temporary_name
            await hass.async_add_executor_job(temporary_path.write_bytes, body)
            try:
                entry = _entry(hass)
                ai_entity = entry.options.get(
                    CONF_AI_TASK_ENTITY, entry.data[CONF_AI_TASK_ENTITY]
                )
                service_result = await hass.services.async_call(
                    "ai_task",
                    "generate_data",
                    {
                        "entity_id": ai_entity,
                        "task_name": "Extract household receipt",
                        "instructions": (
                            "Extract only visible receipt values. Return blank values when unreadable. "
                            "Do not invent a total or date. Suggest exactly one allowed leaf category "
                            f"or blank. Allowed categories: {', '.join(categories)}"
                        ),
                        "structure": {
                            "merchant": {"selector": {"text": {}}},
                            "occurred_on": {"selector": {"text": {}}},
                            "subtotal": {"selector": {"text": {}}},
                            "tax": {"selector": {"text": {}}},
                            "tip": {"selector": {"text": {}}},
                            "total": {"required": True, "selector": {"text": {}}},
                            "suggested_category": {"selector": {"text": {}}},
                            "notes": {"selector": {"text": {}}},
                        },
                        "attachments": {
                            "media_content_id": (
                                f"media-source://media_source/{media_key}/household_budget_pending/"
                                f"{temporary_name}"
                            ),
                            "media_content_type": media_type,
                        },
                    },
                    blocking=True,
                    return_response=True,
                )
            finally:
                await hass.async_add_executor_job(temporary_path.unlink, True)
            fields: Any = service_result.get("data", service_result or {})
            if not isinstance(fields, dict):
                return _error("OpenAI returned an invalid receipt structure", 502)
            analyzed = await _client(hass).request(
                "POST",
                f"/api/v1/receipts/{draft_id}/analysis",
                json={"ai_entity_id": ai_entity, "fields": fields},
            )
            analyzed["duplicate"] = uploaded["draft"].get("duplicate", False)
            return self.json(analyzed)
        except BudgetAPIError as exc:
            return _error(str(exc))
        except Exception:
            LOGGER.exception("Receipt analysis failed")
            payload: dict[str, Any] = {
                "error": "Receipt analysis failed; enter the values manually or try again"
            }
            if uploaded is not None:
                payload["draft"] = uploaded.get("draft")
            return web.json_response(payload, status=502)


class BudgetReceiptConfirmView(HomeAssistantView):
    url = "/api/household_budget/receipt/{draft_id}/confirm"
    name = "api:household_budget:receipt:confirm"
    requires_auth = True

    async def post(self, request: web.Request, draft_id: str) -> web.Response:
        if denied := _admin_error(request):
            return denied
        try:
            result = await _client(request.app["hass"]).request(
                "POST", f"/api/v1/receipts/{int(draft_id)}/confirm", json=await request.json()
            )
            return self.json(result, status_code=201)
        except (BudgetAPIError, ValueError, json.JSONDecodeError) as exc:
            return _error(str(exc))


VIEWS = (
    BudgetConfigView,
    BudgetSummaryView,
    BudgetRecentView,
    BudgetExpenseView,
    BudgetUndoView,
    BudgetReceiptView,
    BudgetReceiptConfirmView,
)
