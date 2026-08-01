"""Authenticated Streamable HTTP host for the Home Assistant add-on."""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import uvicorn
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.types import Receive, Scope, Send

from .ledger import (
    DEFAULT_CATEGORIES,
    DEFAULT_MEMBERS,
    BudgetLedger,
    BudgetValidationError,
)
from .mcp_server import create_server


@dataclass(frozen=True)
class HTTPRuntimeConfig:
    database: Path
    api_token: str
    allowed_hosts: tuple[str, ...]
    members: tuple[str, ...] = DEFAULT_MEMBERS
    categories: tuple[tuple[str, str | None, int | None], ...] = DEFAULT_CATEGORIES
    household_timezone: str = "America/New_York"
    bind_host: str = "0.0.0.0"
    port: int = 8099


def _load_members(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise BudgetValidationError("members must contain at least one name")
    members = tuple(str(item).strip() for item in value)
    if any(not member or len(member) > 80 for member in members):
        raise BudgetValidationError("member names must contain 1 to 80 characters")
    if len({member.casefold() for member in members}) != len(members):
        raise BudgetValidationError("member names must be unique")
    return members


def _load_categories(
    value: object,
) -> tuple[tuple[str, str | None, int | None], ...]:
    if not isinstance(value, list) or not value:
        raise BudgetValidationError("categories must contain at least one category")
    parsed: list[tuple[str, str | None, int | None]] = []
    for item in value:
        if not isinstance(item, dict):
            raise BudgetValidationError("each category must be an object")
        name = str(item.get("name", "")).strip()
        parent = str(item.get("parent", "")).strip() or None
        raw_budget = str(item.get("monthly_budget", "")).strip()
        if not name or len(name) > 80:
            raise BudgetValidationError("category names must contain 1 to 80 characters")
        if parent is not None and len(parent) > 80:
            raise BudgetValidationError("category parent names cannot exceed 80 characters")
        if "/" in name or (parent is not None and "/" in parent):
            raise BudgetValidationError(
                "category names cannot contain '/' because it separates parent and child names"
            )
        if parent is not None and raw_budget:
            raise BudgetValidationError(
                f"subcategory {name!r} cannot define a monthly budget"
            )
        cents: int | None = None
        if parent is None:
            try:
                amount = Decimal(raw_budget)
            except (InvalidOperation, ValueError) as exc:
                raise BudgetValidationError(
                    f"top-level category {name!r} needs a valid monthly_budget"
                ) from exc
            if (
                not amount.is_finite()
                or amount <= 0
                or amount.as_tuple().exponent < -2
            ):
                raise BudgetValidationError(
                    f"monthly_budget for {name!r} must be positive with at most two decimal places"
                )
            cents = int(amount * 100)
        parsed.append((name, parent, cents))

    paths = {
        ((parent.casefold() + "/") if parent else "") + name.casefold()
        for name, parent, _ in parsed
    }
    if len(paths) != len(parsed):
        raise BudgetValidationError("category paths must be unique")
    roots = {name.casefold() for name, parent, _ in parsed if parent is None}
    for name, parent, _ in parsed:
        if parent is not None and parent.casefold() not in roots:
            raise BudgetValidationError(
                f"category {name!r} references unknown parent {parent!r}"
            )
    return tuple(parsed)


class StaticTokenVerifier(TokenVerifier):
    """Constant-time verifier for the single Hermes service token."""

    def __init__(self, expected_token: str):
        self.expected_token = expected_token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not secrets.compare_digest(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id="hermes",
            subject="hermes-budget-client",
            scopes=["budget"],
        )


class HTTPRequireAuthMiddleware(RequireAuthMiddleware):
    """Apply the SDK auth guard to HTTP while passing ASGI lifespan through."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


def load_runtime_config(
    options_path: str | Path | None = None,
) -> HTTPRuntimeConfig:
    """Load add-on options, with environment overrides for local testing."""
    configured_path = options_path or os.environ.get(
        "BUDGET_OPTIONS_PATH", "/data/options.json"
    )
    options: dict[str, object] = {}
    path = Path(configured_path)
    if path.exists():
        options = json.loads(path.read_text(encoding="utf-8"))

    token = os.environ.get("BUDGET_API_TOKEN") or str(options.get("api_token", ""))
    if len(token) < 32:
        raise BudgetValidationError(
            "api_token must contain at least 32 characters; generate a random secret in the add-on configuration"
        )

    raw_hosts = options.get("allowed_hosts", [])
    env_hosts = os.environ.get("BUDGET_ALLOWED_HOSTS")
    if env_hosts:
        raw_hosts = [host.strip() for host in env_hosts.split(",") if host.strip()]
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise BudgetValidationError("allowed_hosts must contain at least one host:port")
    hosts = tuple(str(host).strip() for host in raw_hosts if str(host).strip())
    if not hosts or any("/" in host or "://" in host for host in hosts):
        raise BudgetValidationError(
            "allowed_hosts entries must use host:port format without a URL scheme or path"
        )

    members = _load_members(options.get("members", list(DEFAULT_MEMBERS)))
    default_category_options = [
        {
            "name": name,
            "parent": parent or "",
            "monthly_budget": "" if cents is None else f"{cents / 100:.2f}",
        }
        for name, parent, cents in DEFAULT_CATEGORIES
    ]
    categories = _load_categories(
        options.get("categories", default_category_options)
    )
    household_timezone = str(
        options.get("household_timezone", "America/New_York")
    ).strip()
    if not household_timezone or len(household_timezone) > 100:
        raise BudgetValidationError("household_timezone is required")

    database = Path(os.environ.get("BUDGET_DB_PATH", "/data/budget.db"))
    bind_host = os.environ.get("BUDGET_BIND_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("BUDGET_PORT", "8099"))
    except ValueError as exc:
        raise BudgetValidationError("BUDGET_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise BudgetValidationError("BUDGET_PORT must be between 1 and 65535")
    return HTTPRuntimeConfig(
        database=database,
        api_token=token,
        allowed_hosts=hosts,
        members=members,
        categories=categories,
        household_timezone=household_timezone,
        bind_host=bind_host,
        port=port,
    )


def create_http_app(config: HTTPRuntimeConfig):
    """Create a bearer-protected, stateless Streamable HTTP MCP app."""
    server = create_server(
        BudgetLedger(
            config.database,
            household_timezone=config.household_timezone,
            members=config.members,
            categories=config.categories,
        )
    )
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(config.allowed_hosts),
        allowed_origins=[f"http://{host}" for host in config.allowed_hosts],
    )
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=transport_security,
        host=config.bind_host,
    )
    protected_app = HTTPRequireAuthMiddleware(
        mcp_app, required_scopes=["budget"], resource_metadata_url=None
    )
    return AuthenticationMiddleware(
        protected_app,
        backend=BearerAuthBackend(StaticTokenVerifier(config.api_token)),
    )


def main() -> None:
    config = load_runtime_config()
    app = create_http_app(config)
    uvicorn.run(
        app,
        host=config.bind_host,
        port=config.port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
