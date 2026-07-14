#!/usr/bin/env python3
"""One-time local OAuth setup for Lark Mail draft access.

This script starts a tiny HTTP server on localhost:9876, prints a Lark OAuth
authorization URL, captures the redirected authorization code, exchanges it for
user tokens, and persists the refresh token for lark_mail_draft.py.

It intentionally stores only the refresh token and expiry metadata, not the
app secret.
"""

from __future__ import annotations

import importlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

APP_ID_EXPECTED = "cli_aa94690b12b81cde"
REDIRECT_URI = "http://localhost:9876/oauth/callback"
SCOPE = "sheets:spreadsheet:readonly sheets:spreadsheet mail:user_mailbox.message:modify"
TOKEN_URL = "https://open.larksuite.com/open-apis/authen/v1/oidc/access_token"
OAUTH_URL = "https://accounts.larksuite.com/open-apis/authen/v1/authorize"
TOKEN_FILE = ROOT / "runtime" / "lark_oauth_secret.json"


def _first_attr(module: Any, names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = getattr(module, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_app_credentials() -> Tuple[str, str, str]:
    """Load app_id/app_secret from project config modules.

    The preferred locations are copyright_alert.lark_auth and
    copyright_alert.config. A final fallback to copyright_alert.run_alert is kept
    because this project currently stores the app credentials there.
    """
    candidates = (
        "copyright_alert.lark_auth",
        "copyright_alert.config",
        "copyright_alert.run_alert",
    )
    app_id_names = ("APP_ID", "APPID", "LARK_APP_ID", "BOT_APP_ID", "app_id")
    secret_names = ("APP_SECRET", "LARK_APP_SECRET", "BOT_SECRET", "app_secret")

    errors: list[str] = []
    for module_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            errors.append(f"{module_name}: {exc!r}")
            continue

        app_id = _first_attr(module, app_id_names)
        app_secret = _first_attr(module, secret_names)
        if app_id and app_secret:
            if app_id != APP_ID_EXPECTED:
                raise RuntimeError(
                    f"Loaded app_id {app_id!r} from {module_name}, expected {APP_ID_EXPECTED!r}."
                )
            return app_id, app_secret, module_name

        errors.append(f"{module_name}: app_id/app_secret attributes not found")

    raise RuntimeError("Could not load Lark app credentials. Tried: " + "; ".join(errors))


def post_json(url: str, payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {text[:1000]}") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Non-JSON response from {url}: {text[:1000]}") from exc

    code = data.get("code", 0)
    if code not in (0, "0", None):
        raise RuntimeError(f"Lark API error from {url}: {json.dumps(data, ensure_ascii=False)}")
    return data


def _response_page(title: str, body: str) -> bytes:
    html = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>{title}</title></head>
<body style=\"font-family: -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif; line-height: 1.5;\">
<h2>{title}</h2>
<p>{body}</p>
</body></html>"""
    return html.encode("utf-8")


def capture_code(expected_state: str) -> str:
    """Run a one-shot localhost callback server and return the OAuth code."""

    class OAuthCallbackHandler(BaseHTTPRequestHandler):
        server_version = "LarkOAuthSetup/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # keep output clean
            return

        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/oauth/callback":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found")
                return

            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [""])[0]
            error = (qs.get("error") or [""])[0]
            error_description = (qs.get("error_description") or [""])[0]

            if error:
                self.server.oauth_error = f"OAuth error: {error} {error_description}"  # type: ignore[attr-defined]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(_response_page("OAuth failed", "You can close this tab and check the terminal."))
                return
            if not code:
                self.server.oauth_error = "OAuth callback did not include a code."  # type: ignore[attr-defined]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(_response_page("OAuth failed", "Missing authorization code."))
                return
            if state != expected_state:
                self.server.oauth_error = "OAuth state mismatch; refusing to use this callback."  # type: ignore[attr-defined]
                self.send_response(400)
                self.end_headers()
                self.wfile.write(_response_page("OAuth failed", "State mismatch."))
                return

            self.server.oauth_code = code  # type: ignore[attr-defined]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(_response_page("OAuth complete", "Authorization succeeded. You can close this tab."))

    server = HTTPServer(("127.0.0.1", 9876), OAuthCallbackHandler)
    server.oauth_code = ""  # type: ignore[attr-defined]
    server.oauth_error = ""  # type: ignore[attr-defined]
    try:
        while not getattr(server, "oauth_code") and not getattr(server, "oauth_error"):
            server.handle_request()
    finally:
        server.server_close()

    if getattr(server, "oauth_error"):
        raise RuntimeError(getattr(server, "oauth_error"))
    return getattr(server, "oauth_code")


def exchange_code(app_id: str, app_secret: str, code: str) -> Dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "client_id": app_id,
        "client_secret": app_secret,
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    return post_json(TOKEN_URL, payload)


def save_refresh_token(token_response: Dict[str, Any], source_module: str) -> Dict[str, Any]:
    data = token_response.get("data") if isinstance(token_response.get("data"), dict) else token_response
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        raise RuntimeError(f"Token response did not contain refresh_token: {json.dumps(token_response, ensure_ascii=False)}")

    now = int(time.time())
    record = {
        "app_id": APP_ID_EXPECTED,
        "source_module": source_module,
        "refresh_token": refresh_token,
        "refresh_token_expires_in": data.get("refresh_token_expires_in"),
        "refresh_token_expires_at": now + int(data.get("refresh_token_expires_in") or 0) if data.get("refresh_token_expires_in") else None,
        "access_token_expires_in": data.get("expires_in"),
        "created_at": now,
        "updated_at": now,
        "scope": SCOPE,
        "redirect_uri": REDIRECT_URI,
    }
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def main() -> int:
    app_id, app_secret, source_module = load_app_credentials()
    state = secrets.token_urlsafe(24)
    auth_params = {
        "app_id": app_id,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
    }
    auth_url = OAUTH_URL + "?" + urllib.parse.urlencode(auth_params)

    print(f"Loaded app credentials from {source_module}.")
    print("\nOpen this URL in your browser and approve the requested scope:\n")
    print(auth_url)
    print("\nWaiting for callback on http://localhost:9876/oauth/callback ...", flush=True)

    code = capture_code(state)
    print("Authorization code captured. Exchanging for tokens ...", flush=True)
    token_response = exchange_code(app_id, app_secret, code)
    record = save_refresh_token(token_response, source_module)

    expiry = record.get("refresh_token_expires_at")
    expiry_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expiry)) if expiry else "not provided"
    print(f"Success. Refresh token saved to {TOKEN_FILE}.")
    print(f"Refresh token expiry: {expiry_text}")
    print("Local OAuth server shut down.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
