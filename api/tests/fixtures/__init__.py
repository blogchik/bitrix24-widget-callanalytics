"""Shared, milestone-scoped test doubles.

Kept as a package rather than more `conftest.py` fixtures because these are *builders*
that tests call explicitly (a scripted Bitrix24, a seeded portal in a chosen state), not
ambient state pytest should inject. `conftest.py` stays the place for the engine/database
lifecycle, which every test needs identically.
"""

from tests.fixtures.bitrix import (
    APP_KEY,
    CLIENT_ENDPOINT,
    DOMAIN,
    FRESH_ACCESS,
    FRESH_REFRESH,
    OAUTH_HOST,
    SEED_ACCESS,
    SEED_REFRESH,
    SERVER_ENDPOINT,
    USER_AUTH,
    USER_REFRESH,
    Err,
    FakeBitrix,
    RecordedRequest,
    SeededPortal,
    clear_rest_logs,
    delete_portal,
    fetch_rest_logs,
    install_form,
    install_query,
    new_member_id,
    patch_httpx,
    portal_snapshot,
    portal_sync_snapshot,
    seed_portal,
    token_response,
)

__all__ = [
    "APP_KEY",
    "CLIENT_ENDPOINT",
    "DOMAIN",
    "FRESH_ACCESS",
    "FRESH_REFRESH",
    "OAUTH_HOST",
    "SEED_ACCESS",
    "SEED_REFRESH",
    "SERVER_ENDPOINT",
    "USER_AUTH",
    "USER_REFRESH",
    "Err",
    "FakeBitrix",
    "RecordedRequest",
    "SeededPortal",
    "clear_rest_logs",
    "delete_portal",
    "fetch_rest_logs",
    "install_form",
    "install_query",
    "new_member_id",
    "patch_httpx",
    "portal_snapshot",
    "portal_sync_snapshot",
    "seed_portal",
    "token_response",
]
