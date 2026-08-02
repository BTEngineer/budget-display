"""Async client for the authenticated Household Budget add-on API."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError, ClientSession


class BudgetAPIError(Exception):
    """Raised when the budget app rejects or cannot complete a request."""


class BudgetClient:
    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {**self._headers, **(headers or {})}
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                data=data,
                headers=request_headers,
                params=params,
                timeout=30,
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise BudgetAPIError(payload.get("error", f"HTTP {response.status}"))
                return payload
        except (ClientError, TimeoutError) as exc:
            raise BudgetAPIError(str(exc)) from exc

    async def config(self) -> dict[str, Any]:
        return await self.request("GET", "/api/v1/config")
