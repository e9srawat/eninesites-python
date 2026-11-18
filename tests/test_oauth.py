"""Browser login: registration, PKCE, the loopback listener, Bearer calls, renewal and revoke.

The server is the fake ``server`` fixture. The browser is a function standing in for
``oauth.open_browser``: it reads the approval URL and calls the CLI's own listener on
127.0.0.1 the way the server's redirect would, so the listener is real and nothing else
leaves the process.
"""

from __future__ import annotations

import http.client
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import FakeServer

from eninesites.errors import ApiError
from eninesites.lib import dryrun
from eninesites.lib.client import config, credentials
from eninesites.lib.client import http as client_http
from eninesites.lib.client import oauth
from eninesites.root import api as root
from eninesites.site import api as site
from eninesites.tag import api as tag

TOKENS = {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 36000}
BROWSER_PROFILE = {
    "oauth_access_token": "access-1",
    "oauth_refresh_token": "refresh-1",
    "oauth_expires_at": "2026-01-01T00:00:00+00:00",
    "oauth_client_id": "cid",
}


def browser(answer: dict[str, str] | None = None, *, wrong_state: bool = False) -> Any:
    """A stand-in browser: approves the login by calling the listener's /callback."""

    def approve(url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        redirect = urlsplit(query["redirect_uri"])
        reply = dict(answer or {"code": "the-code"})
        reply.setdefault("state", "forged" if wrong_state else query["state"])
        path = redirect.path + "?" + "&".join(f"{k}={v}" for k, v in reply.items())

        def call() -> None:
            conn = http.client.HTTPConnection("127.0.0.1", redirect.port, timeout=5)
            conn.request("GET", path)
            conn.getresponse().read()

        threading.Thread(target=call, daemon=True).start()

    return approve


def form(call: Any) -> dict[str, str]:
    """A form-encoded request body as a dict."""
    return {k: v[0] for k, v in parse_qs(call.body.decode()).items()}


def store(name: str, **values: str) -> None:
    """Set keys of one profile in the config file."""
    profiles = config.load()
    profiles.setdefault(name, {}).update(values)
    config.save(profiles)


@pytest.fixture(name="signed_in")
def _signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """A default profile holding a browser login, and no API key in the environment."""
    monkeypatch.delenv(credentials.ENV_API_KEY)
    store(config.DEFAULT_PROFILE, site="example.com", **BROWSER_PROFILE)


def test_browser_login_registers_a_public_client_and_stores_the_tokens(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth, "open_browser", browser())
    server.add("POST", "/o/register/", {"client_id": "cid"}, status=201)
    server.add("POST", "/o/token/", TOKENS)
    server.add("GET", "/api/v1/site/", [])
    result = dryrun.done(root.login_root(browser=True))

    register, token, check = server.calls
    assert register.json()["token_endpoint_auth_method"] == "none"
    assert register.json()["redirect_uris"] == ["http://127.0.0.1/callback"]
    sent = form(token)
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "the-code"
    assert sent["client_id"] == "cid" and len(sent["code_verifier"]) >= 43
    assert sent["redirect_uri"].startswith("http://127.0.0.1:")
    assert check.headers["authorization"] == "Bearer access-1"

    profile = config.load()[config.DEFAULT_PROFILE]
    assert profile["oauth_refresh_token"] == "refresh-1"
    assert profile["oauth_client_id"] == "cid" and "api_key" not in profile
    assert result["auth"] == "browser" and "access-1" not in str(result)


def test_a_callback_for_another_login_stores_nothing(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth, "open_browser", browser(wrong_state=True))
    server.add("POST", "/o/register/", {"client_id": "cid"}, status=201)
    with pytest.raises(ApiError, match="state mismatch"):
        root.login_root(browser=True)
    assert [c.path for c in server.calls] == ["/o/register/"]
    assert not config.config_path().exists()


def test_a_refused_approval_is_reported(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = {"error": "access_denied", "error_description": "denied"}
    monkeypatch.setattr(oauth, "open_browser", browser(denied))
    server.add("POST", "/o/register/", {"client_id": "cid"}, status=201)
    with pytest.raises(ApiError, match="refused: denied") as caught:
        root.login_root(browser=True)
    assert caught.value.code == "access_denied"


def test_no_answer_from_the_browser_times_out(
    server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oauth, "open_browser", lambda url: None)
    monkeypatch.setattr(oauth, "LOGIN_TIMEOUT", 0.5)
    server.add("POST", "/o/register/", {"client_id": "cid"}, status=201)
    with pytest.raises(ApiError, match="no answer from the browser") as caught:
        root.login_root(browser=True)
    assert caught.value.code == "timeout"


def test_a_dry_run_shows_the_registration_and_sends_nothing(server: FakeServer) -> None:
    planned = root.login_root(browser=True, dry_run=True)
    assert (planned.get("method"), planned.get("path")) == ("POST", "/o/register/")
    assert not server.calls


def test_a_key_and_browser_together_are_refused() -> None:
    with pytest.raises(ApiError, match="not both"):
        root.login_root(api_key="k" * 40, browser=True)


@pytest.mark.usefixtures("signed_in")
def test_a_refused_token_is_renewed_once_and_the_call_resent(
    server: FakeServer,
) -> None:
    path = "/api/v1/site/example.com/tags/"
    server.add("GET", path, {"detail": "expired"}, status=401)
    server.add("GET", path, {"next": None, "results": []})
    server.add("POST", "/o/token/", {**TOKENS, "access_token": "access-2"})
    tag.list_tag()
    first, renew, again = server.calls
    assert first.headers["authorization"] == "Bearer access-1"
    assert form(renew) == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": "cid",
    }
    assert again.headers["authorization"] == "Bearer access-2"
    assert config.load()["default"]["oauth_access_token"] == "access-2"


@pytest.mark.usefixtures("signed_in")
def test_a_token_another_process_renewed_is_used_without_renewing(
    server: FakeServer,
) -> None:
    settings = credentials.resolve()
    config.update_profile("default", oauth_access_token="access-from-cron")
    assert (
        oauth.renew(settings, "access-1", client_http.transport) == "access-from-cron"
    )
    assert not server.calls


@pytest.mark.usefixtures("signed_in")
def test_an_expired_login_says_to_sign_in_again(server: FakeServer) -> None:
    server.add("GET", "/api/v1/site/", {"detail": "expired"}, status=401)
    server.add(
        "POST",
        "/o/token/",
        {"error": "invalid_grant", "error_description": "Invalid refresh token"},
        status=400,
    )
    with pytest.raises(ApiError, match="login --browser") as caught:
        site.list_site()
    assert caught.value.code == "invalid_grant"


@pytest.mark.usefixtures("signed_in")
def test_too_many_renewals_report_the_wait(server: FakeServer) -> None:
    server.add("GET", "/api/v1/site/", {"detail": "expired"}, status=401)
    server.add(
        "POST",
        "/o/token/",
        {"error": "slow_down", "error_description": "Too many requests"},
        status=429,
        headers={"retry-after": "42"},  # as transport keeps them
    )
    with pytest.raises(ApiError, match="try again in 42 seconds"):
        site.list_site()
    assert len([c for c in server.calls if c.path == "/o/token/"]) == 1


@pytest.mark.usefixtures("signed_in")
def test_a_dry_run_never_renews(server: FakeServer) -> None:
    server.add("GET", "/api/v1/site/", {"detail": "x"}, status=401)
    client = client_http.Client(credentials.resolve(), dry_run=True)
    with pytest.raises(ApiError):
        client.get("/api/v1/site/")
    assert [c.path for c in server.calls] == ["/api/v1/site/"]


def test_an_api_key_in_the_environment_beats_a_browser_login(
    server: FakeServer,
) -> None:
    store("default", **BROWSER_PROFILE)
    site.list_site()
    assert server.last.headers["authorization"].startswith("Token ")


@pytest.mark.usefixtures("signed_in")
def test_logout_revokes_the_browser_login_and_removes_it(server: FakeServer) -> None:
    server.add("POST", "/o/revoke_token/", None)
    result = dryrun.done(root.logout_root())
    assert form(server.last) == {"token": "refresh-1", "client_id": "cid"}
    assert result["revoked"] == ["default"] and not result["revoke_failed"]
    assert not set(config.OAUTH_KEYS) & set(config.load()["default"])


@pytest.mark.usefixtures("signed_in")
def test_logout_removes_the_login_even_when_the_revoke_fails(
    server: FakeServer,
) -> None:
    server.add("POST", "/o/revoke_token/", {"error": "server_error"}, status=500)
    result = dryrun.done(root.logout_root())
    assert result["revoke_failed"] == ["default"] and result["cleared"] == ["default"]
    assert "oauth_access_token" not in config.load()["default"]


@pytest.mark.usefixtures("signed_in")
def test_logout_all_revokes_every_browser_login(server: FakeServer) -> None:
    config.update_profile("dev", api_key="k" * 40)
    store("local", **BROWSER_PROFILE)
    result = dryrun.done(root.logout_root(all_=True))
    assert result["cleared"] == ["default", "dev", "local"]
    assert result["revoked"] == ["default", "local"]
    assert len([c for c in server.calls if c.path == "/o/revoke_token/"]) == 2


@pytest.mark.usefixtures("signed_in")
def test_a_key_login_replaces_and_revokes_a_browser_login(server: FakeServer) -> None:
    dryrun.done(root.login_root(api_key="k" * 40))
    profile = config.load()["default"]
    assert profile["api_key"] == "k" * 40
    assert not set(config.OAUTH_KEYS) & set(profile)
    assert any(c.path == "/o/revoke_token/" for c in server.calls)


@pytest.mark.usefixtures("signed_in")
def test_config_names_the_browser_login_and_its_expiry() -> None:
    result = root.config_root()
    assert result["auth"] == "browser"
    assert result["token_expires"] == BROWSER_PROFILE["oauth_expires_at"]
    assert result["api_key"] and "access-1" not in result["api_key"]


def test_update_profile_holds_the_lock_and_can_be_nested_safely(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    with config.locked(path) as held:
        config.update_profile_locked("default", held, site="a.com")
    config.update_profile("default", path, base_url="https://eninesites.com")
    assert config.load(path)["default"] == {
        "site": "a.com",
        "base_url": "https://eninesites.com",
    }
    assert (tmp_path / "config.toml.lock").exists()
