"""Browser login: the OAuth flow the eninesites MCP connector uses, run from the terminal.

The server (django-oauth-toolkit with dynamic client registration) offers the
authorization-code grant with PKCE (S256 only) and the refresh-token grant; nothing else, so
there is no device flow and a login needs a browser on this machine. The steps:

1. Register a public client (``token_endpoint_auth_method: none``, no secret) at
   ``/o/register/`` with the redirect URI ``http://127.0.0.1/callback``. The server matches a
   registered ``127.0.0.1`` URI at any port (RFC 8252 section 7.3), so one registration serves
   whatever port the listener gets. A client is registered at every login.
2. Listen on ``127.0.0.1:<free port>`` for exactly one ``/callback``, open the browser at
   ``/o/authorize/`` with a PKCE challenge and a random ``state``, and wait (``LOGIN_TIMEOUT``).
3. Swap the code for an access token (10 hours) and a refresh token at ``/o/token/``.

The endpoints are fixed paths under the base URL. The server's discovery document builds them
from its own configured domain, not the address the CLI talks to, so it is not read.

``renew`` replaces an expired access token, under the config file's lock: when another process
has already renewed it, the stored token is used as it is. The server rotates the refresh token
on every use and, two minutes after a rotation, revokes the whole login if the old one is used
again, so two processes must never both renew from the same refresh token.

Nothing here touches the network itself: each function takes ``send``, which is
``lib.client.http.transport`` (``http`` imports this module, so this module cannot import it).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol
from urllib import request
from urllib.parse import parse_qs, urlencode, urlsplit

from eninesites.errors import ApiError

from . import config
from .credentials import Settings

REGISTER_PATH = "/o/register/"
AUTHORIZE_PATH = "/o/authorize/"
TOKEN_PATH = "/o/token/"
#: django-oauth-toolkit's own path (``oauth2_provider/urls.py``), with an underscore. The
#: server's discovery document advertises ``/o/revoke-token/``, which answers 404.
REVOKE_PATH = "/o/revoke_token/"
#: What the client registers; the listener's port is added at authorize time.
REDIRECT_URI = "http://127.0.0.1/callback"
#: The server's default scope is ``read``; the CLI writes, so it asks for both.
SCOPE = "read write"
CLIENT_NAME = "eninesites CLI"
#: How long ``sign_in`` waits for the browser before giving up, in seconds.
LOGIN_TIMEOUT = 300.0
TIMEOUT_SECONDS = 60.0
LOGIN_AGAIN = "Run `python -m eninesites login` to sign in again."


class Reply(Protocol):  # pylint: disable=too-few-public-methods  # a shape, not a class
    """The parts of ``http.Reply`` read here, read-only as ``http.Reply`` is frozen."""

    @property
    def status(self) -> int:
        """The HTTP status."""

    @property
    def body(self) -> bytes:
        """The raw body."""

    @property
    def headers(self) -> dict[str, str]:
        """The headers, names in lower case."""


Send = Callable[[request.Request, float], Reply]


def registration() -> dict[str, Any]:
    """The client metadata ``/o/register/`` receives (RFC 7591)."""
    return {
        "client_name": CLIENT_NAME,
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": SCOPE,
    }


def register(base_url: str, send: Send) -> str:
    """Register a public client; its ``client_id``."""
    payload = _post(base_url, REGISTER_PATH, send, body=registration())
    client_id = payload.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise ApiError(f"POST {REGISTER_PATH}: the server returned no client_id")
    return client_id


def pkce_pair() -> tuple[str, str]:
    """A PKCE ``(code_verifier, code_challenge)``, S256 (RFC 7636)."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(  # pylint: disable=too-many-arguments
    base_url: str, client_id: str, redirect_uri: str, state: str, challenge: str
) -> str:
    """The page the person approves the login on."""
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{base_url}{AUTHORIZE_PATH}?{urlencode(query)}"


class Listener:
    """A one-shot HTTP server on ``127.0.0.1`` that waits for the browser's ``/callback``."""

    def __init__(self) -> None:
        self.answer: dict[str, str] | None = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            """Takes the one ``/callback`` and tells the person they can close the tab."""

            # http.server dispatches a GET to a method of exactly this name.
            def do_GET(self) -> None:  # pylint: disable=invalid-name
                """Record the callback's query and answer the browser."""
                parts = urlsplit(self.path)
                if parts.path != "/callback":
                    self.send_error(404)
                    return
                owner.answer = {k: v[0] for k, v in parse_qs(parts.query).items()}
                failed = "error" in owner.answer
                page = (
                    "Login failed; see the terminal."
                    if failed
                    else "You are logged in to the eninesites CLI. You can close this tab."
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(  # pylint: disable=arguments-differ
                self, *_args: Any
            ) -> None:
                """Say nothing: the request line would print the code."""

        self._server = HTTPServer(("127.0.0.1", 0), Handler)

    @property
    def redirect_uri(self) -> str:
        """The registered redirect URI with this listener's port."""
        return f"http://127.0.0.1:{self._server.server_address[1]}/callback"

    def wait(self, timeout: float) -> dict[str, str]:
        """The callback's query, or a refusal once ``timeout`` seconds pass without one."""
        deadline = time.monotonic() + timeout
        self._server.timeout = 0.5
        while self.answer is None and time.monotonic() < deadline:
            self._server.handle_request()
        if self.answer is None:
            raise ApiError(
                f"no answer from the browser within {timeout:.0f}s. If the page said your "
                "plan has no API access, a plan that includes it is needed; otherwise run "
                "the login again.",
                code="timeout",
            )
        return self.answer

    def close(self) -> None:
        """Stop listening."""
        self._server.server_close()


def open_browser(url: str) -> None:
    """Show the approval page: open the browser, and print the link in case it does not."""
    print(
        f"Opening your browser to sign in. If it does not open, visit:\n\n  {url}\n",
        file=sys.stderr,
    )
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()


def sign_in(base_url: str, send: Send) -> dict[str, str]:
    """Run the whole browser login; the profile keys to store (``config.OAUTH_KEYS``)."""
    client_id = register(base_url, send)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    listener = Listener()
    try:
        open_browser(
            authorize_url(base_url, client_id, listener.redirect_uri, state, challenge)
        )
        answer = listener.wait(LOGIN_TIMEOUT)
    finally:
        listener.close()
    if "error" in answer:
        detail = answer.get("error_description") or answer["error"]
        raise ApiError(f"the login was refused: {detail}", code=answer["error"])
    if answer.get("state") != state or not answer.get("code"):
        raise ApiError(
            "the browser's answer does not belong to this login (state mismatch); "
            "nothing was stored. Run the login again.",
            code="state_mismatch",
        )
    form = {
        "grant_type": "authorization_code",
        "code": answer["code"],
        "redirect_uri": listener.redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    return stored(client_id, _post(base_url, TOKEN_PATH, send, form=form))


def stored(client_id: str, tokens: dict[str, Any]) -> dict[str, str]:
    """A token response as the profile keys that hold it."""
    access, refresh = tokens.get("access_token"), tokens.get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise ApiError(
            f"POST {TOKEN_PATH}: the server returned no access or refresh token"
        )
    lifetime = tokens.get("expires_in")
    seconds = lifetime if isinstance(lifetime, int) else 0
    expires = datetime.now(UTC) + timedelta(seconds=seconds)
    return {
        "oauth_access_token": access,
        "oauth_refresh_token": refresh,
        "oauth_expires_at": expires.isoformat(timespec="seconds"),
        "oauth_client_id": client_id,
    }


def renew(settings: Settings, sent: str, send: Send) -> str:
    """A working access token after ``sent`` was refused with 401.

    Under the config lock: a token another process stored since ``sent`` is returned as it
    is; otherwise the refresh token is used once and the new pair stored.
    """
    with config.locked(settings.config_path) as path:
        profile = config.load(path).get(settings.project_name, {})
        current = profile.get("oauth_access_token")
        if current and current != sent:
            return current
        refresh, client_id = profile.get("oauth_refresh_token"), profile.get(
            "oauth_client_id"
        )
        if not refresh or not client_id:
            raise ApiError(f"your browser login has expired. {LOGIN_AGAIN}", status=401)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
        try:
            tokens = _post(settings.base_url, TOKEN_PATH, send, form=form)
        except ApiError as exc:
            if exc.code in ("invalid_grant", "invalid_client"):
                raise ApiError(
                    f"your browser login has expired or was revoked ({exc.code}). "
                    f"{LOGIN_AGAIN}",
                    status=exc.status,
                    code=exc.code,
                ) from exc
            raise
        keys = stored(client_id, tokens)
        config.update_profile_locked(settings.project_name, path, **keys)
        return keys["oauth_access_token"]


def revoke(base_url: str, profile: dict[str, str], send: Send) -> bool:
    """Revoke a stored login at the server; whether the server confirmed it.

    Best effort: the caller discards the tokens either way. Revoking the refresh token
    also ends its access token.
    """
    token = profile.get("oauth_refresh_token") or profile.get("oauth_access_token")
    client_id = profile.get("oauth_client_id")
    if not token or not client_id:
        return False
    form = {"token": token, "client_id": client_id}
    try:
        _post(base_url, REVOKE_PATH, send, form=form)
    except ApiError:
        return False
    return True


def discarded() -> dict[str, str | None]:
    """The ``update_profile`` changes that remove a browser login."""
    return {key: None for key in config.OAUTH_KEYS}


def _post(
    base_url: str,
    path: str,
    send: Send,
    *,
    body: dict[str, Any] | None = None,
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST JSON or a form to an OAuth endpoint; the JSON reply, or an ``ApiError``."""
    if body is not None:
        data, content_type = json.dumps(body).encode(), "application/json"
    else:
        data, content_type = (
            urlencode(form or {}).encode(),
            "application/x-www-form-urlencoded",
        )
    req = request.Request(
        base_url + path,
        data=data,
        headers={"Content-Type": content_type, "Accept": "application/json"},
        method="POST",
    )
    reply = send(req, TIMEOUT_SECONDS)
    try:
        payload = json.loads(reply.body) if reply.body.strip() else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    payload = payload if isinstance(payload, dict) else {}
    if reply.status < 400:
        return payload
    code = str(payload.get("error") or payload.get("code") or "not_problem")
    detail = str(
        payload.get("error_description")
        or payload.get("detail")
        or f"HTTP {reply.status}"
    )
    message = f"POST {path}: HTTP {reply.status} {code}: {detail}"
    if reply.status == 429:
        wait = reply.headers.get("retry-after")
        message += f". Too many requests; try again in {wait} seconds." if wait else "."
    raise ApiError(
        message, status=reply.status, code=code, detail=detail, method="POST", path=path
    )
