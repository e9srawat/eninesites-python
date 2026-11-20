"""The eninesites root api: credentials and configuration, the Stripe CLI's way.

Every surface (this noun's cli, an MCP tool, a REST route) calls these functions and
nothing below them. Each verb is one function returning one result type from
``lib.results``, plain JSON. A request the api cannot run raises ``ApiError``.

``login`` stores one credential per profile: a browser login (OAuth tokens, sent as
``Bearer``; ``lib.client.oauth``), or when given a key an API key (the server's
``CustomToken``, sent as ``Token``). Either is checked against the server
(``GET /api/v1/site/``) before it is stored, and storing one removes the other; a browser
login being dropped is revoked at the server first, best effort. ``logout`` removes it the
same way; ``config`` reports what a command would use.

``describe`` reports every command as data, for an agent that would otherwise read 87
``--help`` pages. It is the one api function that reads the cli surface, because the cli is
what it describes: each node's ``parser()``, ``commands()`` and ``output()`` are the one home
of its flags, children and result schemas, so they are read at call time rather than
restated. The import is by name, at call time, so no module imports a surface at load.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import os
from collections.abc import Callable
from typing import Any

from eninesites.errors import ApiError, ErrorResult
from eninesites.lib import dryrun
from eninesites.lib.client import config, credentials, http, oauth
from eninesites.lib.client.http import Client
from eninesites.lib.dryrun import PlannedRequest, writes
from eninesites.lib.jsonvalue import Json
from eninesites.schema import json_schema

from .lib.results import (
    Arg,
    Command,
    ConfigRoot,
    DescribeRoot,
    LoginRoot,
    LogoutRoot,
)

#: What each exit code means, the same in every command (racecar CLI.md O5).
EXIT_CODES = {
    "0": "done, or described",
    "1": "ran, and found something to report",
    "2": "could not run; under --json, stderr holds the refusal as JSON (see error)",
}
#: argparse's own action classes, by the name racecar's CLI audit gives each. argparse names
#: them privately and offers no public alias.
ACTIONS = {
    "store_true": argparse._StoreTrueAction,  # pylint: disable=protected-access
    "store_false": argparse._StoreFalseAction,  # pylint: disable=protected-access
    "count": argparse._CountAction,  # pylint: disable=protected-access
    "append": argparse._AppendAction,  # pylint: disable=protected-access
}


@writes
def login_root(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    project_name: str | None = None,
    dry_run: bool = False,
) -> LoginRoot | PlannedRequest:
    """Check a credential against the server, then store it in the project's profile.

    With no ``api_key`` (None) the credential comes from the browser login
    (``oauth.sign_in``); otherwise it is ``api_key`` (``--api-key``, or with ``--with-token``
    the cli asks for it, hidden, or reads it from stdin), and an empty one is refused. A key
    from the environment or the file is not "logged in" again. ``--base-url``, when given,
    is stored beside it, so a dev-server profile keeps pointing at the dev server. A
    credential the server refuses is not stored.
    """
    browser = api_key is None
    key = (api_key or "").strip()
    resolved = credentials.resolve(base_url=base_url, project_name=project_name)
    keys: dict[str, str] = {}
    if browser:
        if dry_run:
            raise dryrun.Planned("POST", oauth.REGISTER_PATH, body=oauth.registration())
        keys = oauth.sign_in(resolved.base_url, http.transport)
        settings = dataclasses.replace(
            resolved,
            api_key=keys["oauth_access_token"],
            api_key_source="browser login",
            auth="bearer",
        )
        changes: dict[str, str | None] = {**keys, "api_key": None}
    else:
        if not key:
            raise ApiError(
                "login: no API key given. With --with-token, type it at the prompt or "
                "pipe it on stdin; or pass --api-key <key>. `python -m eninesites login` "
                "alone signs in through your browser."
            )
        settings = dataclasses.replace(
            resolved, api_key=key, api_key_source="flag", auth="token"
        )
        changes = {"api_key": key, **oauth.discarded()}
    try:
        sites = Client(settings).get("/api/v1/site/")
    except ApiError:
        if browser:  # the new login works nowhere, so it is not left live on the server
            oauth.revoke(settings.base_url, keys, http.transport)
        raise
    if dry_run:
        stored = {"profile": settings.project_name, "api_key": credentials.redact(key)}
        if base_url:
            stored["base_url"] = settings.base_url
        raise dryrun.Planned("WRITE", str(settings.config_path), body=stored)
    with config.locked(settings.config_path) as path:
        old = config.load(path).get(settings.project_name, {})
        if old.get("oauth_access_token") != changes.get("oauth_access_token"):
            oauth.revoke(old.get("base_url") or settings.base_url, old, http.transport)
        config.update_profile_locked(
            settings.project_name,
            path,
            base_url=settings.base_url if base_url else old.get("base_url"),
            **changes,
        )
    return {
        "project_name": settings.project_name,
        "config_path": str(path),
        "base_url": settings.base_url,
        "auth": "browser" if browser else "api_key",
        "api_key": credentials.redact(settings.api_key),
        "sites": len(sites) if isinstance(sites, list) else 0,
    }


@writes
def logout_root(
    *, project_name: str | None = None, all_: bool = False, dry_run: bool = False
) -> LogoutRoot | PlannedRequest:
    """Remove the stored credential from the project's profile, or every profile with ``--all``.

    A browser login is revoked at the server first, best effort: ``revoked`` names the
    profiles the server confirmed, and the tokens are removed from the file either way. The
    profile's other settings (``base_url``, ``site``) stay, as Stripe's logout keeps non-auth
    fields. A key in ``ENINESITES_API_KEY`` is not the file's to remove; the result says
    when one is still set.
    """
    project = credentials.resolve(project_name=project_name).project_name
    with config.locked() as path:
        profiles = config.load(path)
        names = sorted(profiles) if all_ else [project]
        cleared = [
            n
            for n in names
            if profiles.get(n, {}).get("api_key")
            or profiles.get(n, {}).get("oauth_access_token")
        ]
        if dry_run and cleared:
            raise dryrun.Planned("REMOVE", str(path), body={"api_key": cleared})
        revoked: list[str] = []
        revoke_failed: list[str] = []
        for name in cleared:
            if profiles[name].get("oauth_access_token"):
                base = credentials.resolve(project_name=name).base_url
                ok = oauth.revoke(base, profiles[name], http.transport)
                (revoked if ok else revoke_failed).append(name)
            config.update_profile_locked(name, path, api_key=None, **oauth.discarded())
    return {
        "project_name": project,
        "all": all_,
        "cleared": cleared,
        "revoked": revoked,
        "revoke_failed": revoke_failed,
        "config_path": str(path),
        "env_key_set": bool(os.environ.get(credentials.ENV_API_KEY)),
    }


def config_root(
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    project_name: str | None = None,
) -> ConfigRoot:
    """Each setting a command run with these flags would use, and where it came from.

    Local only: nothing is sent. The key is shown redacted, always.
    """
    settings = credentials.resolve(api_key, base_url, project_name)
    return {
        "project_name": settings.project_name,
        "project_source": settings.project_source,
        "config_path": str(settings.config_path),
        "config_exists": settings.config_path.is_file(),
        "base_url": settings.base_url,
        "base_url_source": settings.base_url_source,
        "auth": _auth_of(settings),
        "token_expires": (
            config.load(settings.config_path)
            .get(settings.project_name, {})
            .get("oauth_expires_at")
            if settings.auth == "bearer"
            else None
        ),
        "api_key": credentials.redact(settings.api_key),
        "api_key_source": settings.api_key_source,
        "site": settings.site,
        "site_source": settings.site_source,
    }


def _auth_of(settings: credentials.Settings) -> str | None:
    """Which kind of credential a command would send: ``api_key``, ``browser`` or none."""
    if not settings.api_key:
        return None
    return "browser" if settings.auth == "bearer" else "api_key"


# argparse keeps a parser's actions, and its action classes, on private names and offers no
# public way to read them; describing the cli means reading them, as lib/cli.py does.
# pylint: disable=protected-access


def describe_root() -> DescribeRoot:
    """Every command as data: what to type, whether it writes, its flags and its output."""
    found: list[Command] = []
    walk("eninesites", "", found)
    return {
        "commands": found,
        "error": json.dumps(json_schema(ErrorResult)),
        "planned": json.dumps(json_schema(dryrun.PlannedRequest)),
        "exit_codes": dict(EXIT_CODES),
    }


def walk(package: str, noun: str, found: list[Command]) -> None:
    """Add ``package``'s verbs to ``found``, then each child's, in ``commands()`` order."""
    node = importlib.import_module(f"{package}.__main__")
    schemas = {params["phase"]: schema for params, schema in node.output()}
    descriptions = dict(node.subcommands())
    for verb, verb_parser in verbs_of(node.parser()).items():
        found.append(
            {
                "command": f"python -m {package} {verb}",
                "noun": noun,
                "verb": verb,
                "description": descriptions[verb],
                "writes": dryrun.is_write(node.api.VERBS[verb]),
                "args": [
                    arg_of(action)
                    for action in verb_parser._actions
                    if not isinstance(action, argparse._HelpAction)
                ],
                "output": json.dumps(schemas[verb]),
            }
        )
    for child, _ in node.commands():
        walk(f"{package}.{child}", f"{noun}.{child}" if noun else child, found)


def verbs_of(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """A node's verb parsers by name; empty for a node with no verbs of its own."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def arg_of(action: argparse.Action) -> Arg:
    """One flag as racecar's CLI audit records it (its ``Arg``)."""
    kind = next(
        (name for name, cls in ACTIONS.items() if isinstance(action, cls)), None
    )
    type_name = getattr(action.type, "__name__", None) if action.type else None
    if kind in ("store_true", "store_false"):
        type_name = "bool"
    return {
        "dest": action.dest,
        "flags": list(action.option_strings),
        "help": None if action.help in (None, argparse.SUPPRESS) else str(action.help),
        "required": bool(action.required),
        "choices": [str(c) for c in action.choices] if action.choices else None,
        "default": json_default(action.default),
        "type": type_name,
        "action": kind,
    }


def json_default(value: Any) -> Json:
    """A flag's default as JSON: a scalar as it is, anything else as its text."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


# pylint: enable=protected-access


# CLI spelling -> the function it binds. A verb spelled like a keyword or a builtin
# (`import`, `list`) is a key here rather than a function name; the lexicon reads this
# literal to find each verb.
VERBS: dict[str, Callable[..., object]] = {
    "login": login_root,
    "logout": logout_root,
    "config": config_root,
    "describe": describe_root,
}
