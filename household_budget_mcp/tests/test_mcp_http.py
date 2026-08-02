from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from budget_display import BudgetValidationError
from budget_display.mcp_http import (
    HTTPRuntimeConfig,
    create_http_app,
    load_runtime_config,
)


TOKEN = "test-token-with-at-least-thirty-two-characters"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1"},
    },
}


class BudgetMCPHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.config = HTTPRuntimeConfig(
            database=Path(self.temporary_directory.name) / "budget.db",
            api_token=TOKEN,
            allowed_hosts=("testserver",),
        )
        self.client = TestClient(create_http_app(self.config))
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.temporary_directory.cleanup()

    def post(self, token: str | None):
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/mcp", headers=headers, json=INITIALIZE)

    def test_missing_token_is_rejected(self) -> None:
        self.assertEqual(self.post(None).status_code, 401)

    def test_wrong_token_is_rejected(self) -> None:
        self.assertEqual(self.post("wrong-token").status_code, 401)

    def test_valid_token_reaches_mcp_server(self) -> None:
        response = self.post(TOKEN)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["serverInfo"]["name"], "Household Budget")

    def test_wrong_host_is_rejected_even_with_valid_token(self) -> None:
        response = self.client.post(
            "http://untrusted.example/mcp",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json=INITIALIZE,
        )
        self.assertEqual(response.status_code, 421)

    def test_json_api_requires_auth_and_rejects_wrong_host(self) -> None:
        self.assertEqual(self.client.get("/api/v1/config").status_code, 401)
        response = self.client.get(
            "http://untrusted.example/api/v1/config",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        self.assertEqual(response.status_code, 421)

    def test_manual_expense_round_trip_through_json_api(self) -> None:
        headers = {"Authorization": f"Bearer {TOKEN}"}
        config = self.client.get("/api/v1/config", headers=headers)
        self.assertEqual(config.status_code, 200)
        added = self.client.post(
            "/api/v1/expenses",
            headers=headers,
            json={
                "request_id": "ha-manual-1",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "4.25",
                "business_name": "Local Shop",
                "description": "Phone entry",
                "occurred_on": "2026-08-01",
            },
        )
        self.assertEqual(added.status_code, 201)
        recent = self.client.get("/api/v1/recent", headers=headers).json()["expenses"]
        self.assertEqual(recent[0]["amount_cents"], 425)
        self.assertEqual(recent[0]["transaction"]["business_name"], "Local Shop")
        self.assertTrue(recent[0]["occurred_at"].startswith("2026-08-01T16:00:00"))

    def test_receipt_upload_analysis_and_confirmation(self) -> None:
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "image/png",
            "X-Receipt-Filename": "phone.png",
        }
        upload = self.client.post(
            "/api/v1/receipts", headers=headers, content=b"\x89PNG\r\n\x1a\nreceipt"
        )
        self.assertEqual(upload.status_code, 201)
        draft_id = upload.json()["draft"]["id"]
        auth = {"Authorization": f"Bearer {TOKEN}"}
        analysis = self.client.post(
            f"/api/v1/receipts/{draft_id}/analysis",
            headers=auth,
            json={"ai_entity_id": "ai_task.openai", "fields": {"total": "9.99"}},
        )
        self.assertEqual(analysis.json()["draft"]["status"], "analyzed")
        confirmed = self.client.post(
            f"/api/v1/receipts/{draft_id}/confirm",
            headers=auth,
            json={
                "request_id": "receipt-api-1",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "9.99",
            },
        )
        self.assertEqual(confirmed.status_code, 201)
        self.assertEqual(confirmed.json()["entry"]["amount_cents"], 999)
        self.assertTrue(confirmed.json()["receipt_deleted"])
        receipt_directory = Path(self.temporary_directory.name) / "receipts"
        self.assertEqual(list(receipt_directory.glob("*")), [])

        duplicate = self.client.post(
            "/api/v1/receipts", headers=headers, content=b"\x89PNG\r\n\x1a\nreceipt"
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json()["draft"]["status"], "confirmed")
        self.assertEqual(list(receipt_directory.glob("*")), [])

    def test_large_expense_requires_explicit_confirmation(self) -> None:
        response = self.client.post(
            "/api/v1/expenses",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={
                "request_id": "large",
                "member": "Member 1",
                "category": "Everyday",
                "amount": "501.00",
            },
        )
        self.assertEqual(response.status_code, 422)

    def test_options_file_is_validated_and_environment_can_override_paths(self) -> None:
        options = Path(self.temporary_directory.name) / "options.json"
        options.write_text(
            json.dumps(
                {
                    "api_token": TOKEN,
                    "allowed_hosts": ["testserver:8099", "homeassistant.local:8099"],
                    "household_timezone": "America/Chicago",
                    "members": ["Alpha", "Beta"],
                    "categories": [
                        {
                            "name": "Primary",
                            "parent": "",
                            "monthly_budget": "250.00",
                        },
                        {
                            "name": "Detail",
                            "parent": "Primary",
                            "monthly_budget": "",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"BUDGET_DB_PATH": "/tmp/test-budget.db"}, clear=False):
            config = load_runtime_config(options)
        self.assertEqual(config.database, Path("/tmp/test-budget.db"))
        self.assertEqual(config.allowed_hosts[0], "testserver:8099")
        self.assertEqual(config.household_timezone, "America/Chicago")
        self.assertEqual(config.members, ("Alpha", "Beta"))
        self.assertEqual(
            config.categories,
            (("Primary", None, 25000), ("Detail", "Primary", None)),
        )

    def test_unknown_category_parent_fails_closed(self) -> None:
        options = Path(self.temporary_directory.name) / "options.json"
        options.write_text(
            json.dumps(
                {
                    "api_token": TOKEN,
                    "allowed_hosts": ["testserver"],
                    "members": ["Alpha"],
                    "categories": [
                        {
                            "name": "Detail",
                            "parent": "Missing",
                            "monthly_budget": "",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BudgetValidationError, "unknown parent"):
            load_runtime_config(options)

    def test_category_names_cannot_contain_path_separator(self) -> None:
        options = Path(self.temporary_directory.name) / "options.json"
        options.write_text(
            json.dumps(
                {
                    "api_token": TOKEN,
                    "allowed_hosts": ["testserver"],
                    "members": ["Alpha"],
                    "categories": [
                        {
                            "name": "Bills/Utilities",
                            "parent": "",
                            "monthly_budget": "100.00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BudgetValidationError, "cannot contain"):
            load_runtime_config(options)

    def test_short_token_fails_closed(self) -> None:
        options = Path(self.temporary_directory.name) / "options.json"
        options.write_text(
            json.dumps({"api_token": "short", "allowed_hosts": ["testserver"]}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BudgetValidationError, "at least 32"):
            load_runtime_config(options)


if __name__ == "__main__":
    unittest.main()
