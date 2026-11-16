"""Server-side Flight Crew View API adapter.

No browser code should import or call this module. Credentials and tokens remain
inside the Home Assistant add-on backend.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from fcv_config import setting

BASE_URL = "https://www.flightcrewview2.com/logbook/api"
AUTH_URL = "https://www.flightcrewview2.com/logbook/logbookuserauth/"
TOKEN_URL = f"{BASE_URL}/token/"
FLIGHTS_URL = f"{BASE_URL}/flights/"
REVOKE_URL = f"{BASE_URL}/revokeToken/"


class FCVError(RuntimeError):
    """An FC View API or configuration error."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward OAuth credentials or bearer tokens to a redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_without_redirects(request, *, timeout: int):
    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(request, timeout=timeout)


def configuration() -> dict[str, str]:
    client_id = setting("FCV_CLIENT_ID", "fcv_client_id")
    client_secret = setting("FCV_CLIENT_SECRET", "fcv_client_secret")
    redirect_uri = setting("FCV_REDIRECT_URI", "fcv_redirect_uri")
    if not client_id or not client_secret or not redirect_uri:
        raise FCVError("FC View requires FCV_CLIENT_ID, FCV_CLIENT_SECRET and FCV_REDIRECT_URI on the server.")
    if not redirect_uri.startswith("https://"):
        raise FCVError("FCV_REDIRECT_URI must use HTTPS.")
    return {"client_id": client_id, "client_secret": client_secret, "redirect_uri": redirect_uri}


def authorization_url(state: str | None = None) -> tuple[str, str]:
    cfg = configuration()
    state = state or secrets.token_urlsafe(32)
    query = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "state": state,
    })
    return f"{AUTH_URL}?{query}", state


def _request(url: str, *, method: str = "GET", form: dict[str, str] | None = None,
             access_token: str | None = None, basic_auth: bool = False, timeout: int = 20) -> dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": "PersonalPilotLogbook/0.4"}
    body = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(form).encode("utf-8")
    if basic_auth and access_token:
        raise FCVError("Choose either Basic client authentication or a Bearer access token, not both.")
    if basic_auth:
        headers["Authorization"] = _basic_auth_header()
    elif access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _open_without_redirects(request, timeout=timeout) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise FCVError("FC View response exceeded the 2 MB safety limit.")
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise FCVError("FC View returned an unexpected response.")
            return parsed
    except FCVError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if code and 300 <= code < 400:
            raise FCVError("FC View attempted an unexpected redirect; the request was not followed.") from exc
        if code == 429:
            raise FCVError("FC View rate limit reached; retry during the next permitted sync window.") from exc
        if code in (400, 401, 403):
            raise FCVError(f"FC View rejected the request (HTTP {code}); reauthorization or configuration may be required.") from exc
        if code and code >= 500:
            raise FCVError(f"FC View is temporarily unavailable (HTTP {code}).") from exc
        raise FCVError("Unable to reach FC View securely.") from exc


def _basic_auth_header() -> str:
    cfg = configuration()
    raw = f'{cfg["client_id"]}:{cfg["client_secret"]}'.encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def exchange_code(code: str) -> dict[str, Any]:
    if not isinstance(code, str) or not code.strip() or len(code) > 512:
        raise FCVError("Missing or invalid FC View authorization code.")
    result = _request(TOKEN_URL, method="POST",
                      form={"grant_type": "authorization_code", "code": code.strip()}, basic_auth=True)
    return _validate_token_response(result)


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    if not isinstance(refresh_token, str) or not refresh_token.strip() or len(refresh_token) > 512:
        raise FCVError("Missing or invalid FC View refresh token.")
    result = _request(TOKEN_URL, method="POST",
                      form={"grant_type": "refresh_token", "refresh_token": refresh_token.strip()}, basic_auth=True)
    return _validate_token_response(result)


def _validate_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    access = payload.get("access_token")
    refresh = payload.get("refresh_token")
    expires = payload.get("expires_in", 3600)
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise FCVError("FC View token response did not contain both access_token and refresh_token.")
    try:
        expires = int(expires)
    except (TypeError, ValueError):
        raise FCVError("FC View returned an invalid access-token expiry.")
    if expires < 60 or expires > 86400:
        raise FCVError("FC View returned an unexpected access-token expiry.")
    return {"access_token": access, "refresh_token": refresh,
            "token_type": str(payload.get("token_type", "Bearer")), "expires_in": expires}


def fetch_flights(access_token: str, start_utc: datetime, end_utc: datetime) -> list[dict[str, Any]]:
    if not isinstance(start_utc, datetime) or not isinstance(end_utc, datetime):
        raise FCVError("FC View sync range must use timezone-aware UTC datetimes.")
    if start_utc.tzinfo is None or end_utc.tzinfo is None:
        raise FCVError("FC View sync range must use timezone-aware UTC datetimes.")
    start = start_utc.astimezone(timezone.utc)
    end = end_utc.astimezone(timezone.utc)
    if end <= start:
        raise FCVError("FC View sync end must be later than its start.")
    now = datetime.now(timezone.utc)
    if start < now.replace(microsecond=0) - timedelta(days=62):
        raise FCVError("Automated FC View polling cannot request more than two months of history.")
    query = urllib.parse.urlencode({
        "start_datetime_utc": start.strftime("%Y-%m-%d %H:%M:%S"),
        "end_datetime_utc": end.strftime("%Y-%m-%d %H:%M:%S"),
    })
    payload = _request(f"{FLIGHTS_URL}?{query}", access_token=access_token)
    flights = payload.get("flights")
    if not isinstance(flights, list) or any(not isinstance(item, dict) for item in flights):
        raise FCVError("FC View flights response did not contain a valid flights list.")
    return flights


def revoke_access(refresh_token: str) -> dict[str, Any]:
    if not isinstance(refresh_token, str) or not refresh_token.strip() or len(refresh_token) > 512:
        raise FCVError("Missing or invalid FC View refresh token.")
    # The endpoint requires Basic client authentication and the refreshToken form field.
    request = urllib.request.Request(
        REVOKE_URL,
        data=urllib.parse.urlencode({"refreshToken": refresh_token.strip()}).encode("utf-8"),
        headers={"Authorization": _basic_auth_header(),
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with _open_without_redirects(request, timeout=20) as response:
            result = json.loads(response.read(64 * 1024).decode("utf-8"))
    except Exception as exc:
        raise FCVError("FC View access revocation failed; retain credentials until the result is confirmed.") from exc
    if not isinstance(result, dict) or result.get("success") != "token_revoked":
        raise FCVError("FC View did not confirm token revocation.")
    return result
