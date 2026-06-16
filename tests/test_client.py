"""Tests for FireflyClient account creation + dashboard wiring (httpx MockTransport, no network)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from firefly_bot.config import FireflySettings
from firefly_bot.firefly.client import FireflyClient

_BASE = "https://firefly.test"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> FireflyClient:
    settings = FireflySettings(_env_file=None, base_url=_BASE, token="t", verify_tls=False)
    fc = FireflyClient(settings)
    # Swap the real transport for the mock; everything else (base_url, headers) is irrelevant here.
    fc._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=_BASE)
    return fc


def _no_assets(request: httpx.Request) -> httpx.Response | None:
    """Shared: no existing asset accounts, and POST returns a new account id 42."""
    if request.method == "GET" and request.url.path == "/api/v1/accounts":
        return httpx.Response(200, json={"data": [], "meta": {"pagination": {"total_pages": 1}}})
    if request.method == "POST" and request.url.path == "/api/v1/accounts":
        return httpx.Response(200, json={"data": {"id": "42"}})
    return None


def test_created_asset_account_is_appended_to_dashboard() -> None:
    puts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        shared = _no_assets(request)
        if shared is not None:
            return shared
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/preferences/frontpageAccounts":
            return httpx.Response(
                200, json={"data": {"attributes": {"name": "frontpageAccounts", "data": [5, 6]}}}
            )
        if request.method == "PUT" and path == "/api/v1/preferences/frontpageAccounts":
            puts.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"attributes": {"data": [5, 6, 42]}}})
        return httpx.Response(404)

    new_id = _client(handler).ensure_asset_account("NL00BANK0123456789", "EUR", "defaultAsset", "X")
    assert new_id == "42"
    # Appended (not overwritten) to the existing non-empty list.
    assert puts == [{"data": [5, 6, 42]}]


def test_empty_dashboard_pref_is_left_untouched() -> None:
    # An empty/unset frontpageAccounts means Firefly shows ALL asset accounts; writing to it would
    # hide the others, so the client must NOT PUT.
    puts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        shared = _no_assets(request)
        if shared is not None:
            return shared
        path = request.url.path
        if request.method == "GET" and path == "/api/v1/preferences/frontpageAccounts":
            return httpx.Response(200, json={"data": {"attributes": {"data": []}}})
        if request.method == "PUT" and path == "/api/v1/preferences/frontpageAccounts":
            puts.append(json.loads(request.content))
            return httpx.Response(200, json={"data": {"attributes": {"data": []}}})
        return httpx.Response(404)

    _client(handler).ensure_asset_account("NL00BANK0123456789", "EUR", "defaultAsset", "X")
    assert puts == []


def test_dashboard_failure_does_not_break_creation() -> None:
    # A 500 from the preferences API must be swallowed — the account id is still returned.
    def handler(request: httpx.Request) -> httpx.Response:
        shared = _no_assets(request)
        if shared is not None:
            return shared
        return httpx.Response(500)

    new_id = _client(handler).ensure_asset_account("NL00BANK0123456789", "EUR", "defaultAsset", "X")
    assert new_id == "42"


def test_existing_account_matched_by_iban_is_not_recreated() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/api/v1/accounts":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "7",
                            "attributes": {
                                "name": "Existing",
                                "type": "asset",
                                "iban": "NL00BANK0123456789",
                                "currency_code": "EUR",
                                "account_role": "defaultAsset",
                            },
                        }
                    ],
                    "meta": {"pagination": {"total_pages": 1}},
                },
            )
        return httpx.Response(404)

    account_id = _client(handler).ensure_asset_account(
        "NL00BANK0123456789", "EUR", "defaultAsset", "X"
    )
    assert account_id == "7"
    # Matched an existing account: no POST (create) and no PUT (dashboard) calls.
    assert not any(c.startswith(("POST", "PUT")) for c in calls)
