"""Server-side FC View OAuth completion orchestration.

This module deliberately contains no Flask routes: the public callback transport
must be confirmed before a route is exposed. It consumes state before exchanging
the single-use authorization code and persists only encrypted token material.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from fcv_client import FCVError, exchange_code
from fcv_oauth_state import consume_oauth_state
from fcv_storage import FCVStorageError, save_tokens


class FCVOAuthFlowError(RuntimeError):
    """Safe OAuth-flow error suitable for a user-facing status message."""


def complete_authorization(
    conn: sqlite3.Connection,
    *,
    state: str,
    code: str | None = None,
    provider_error: str | None = None,
    exchange: Callable[[str], dict[str, Any]] = exchange_code,
    persist: Callable[[sqlite3.Connection, dict[str, Any]], None] = save_tokens,
) -> None:
    """Validate and consume state, exchange the code, then persist encrypted tokens.

    State is consumed before any exchange attempt. A failed exchange therefore
    requires a fresh authorization attempt rather than allowing callback replay.
    Provider error text and authorization codes are never included in exceptions.
    """
    if not consume_oauth_state(conn, state):
        raise FCVOAuthFlowError("FC View authorization could not be validated. Start the connection again.")

    if provider_error:
        raise FCVOAuthFlowError("FC View authorization was declined or could not be completed.")

    if not isinstance(code, str) or not code.strip() or len(code) > 512:
        raise FCVOAuthFlowError("FC View did not return a valid authorization code.")

    try:
        tokens = exchange(code)
        persist(conn, tokens)
    except (FCVError, FCVStorageError) as exc:
        raise FCVOAuthFlowError("FC View connection failed. Start a new authorization attempt.") from exc
    except Exception as exc:
        # Do not echo provider payloads, credentials, codes, or token values.
        raise FCVOAuthFlowError("FC View connection failed. Start a new authorization attempt.") from exc
