"""The eninesites root views as terminal text.

Every line this noun's cli prints is written here. Each ``print_<verb>`` takes that verb's
result and writes it; it reads no store and decides nothing. No line here can show a key
in full: the results carry only its redacted form.
"""

from __future__ import annotations

import sys

from eninesites.lib.renderer import text

from ..results import ConfigRoot, DescribeRoot, LoginRoot, LogoutRoot


def print_login(result: LoginRoot) -> None:
    """``login`` as text, in the Stripe CLI's "Done!" form."""
    text.line(
        f"> Done! The eninesites CLI is configured for project {result['project_name']} "
        f"at {result['base_url']} ({result['sites']} site(s) reachable)."
    )
    what = "Browser login" if result["auth"] == "browser" else "Key"
    text.line(f"{what} {result['api_key']} saved to {result['config_path']}")


def print_logout(result: LogoutRoot) -> None:
    """``logout`` as text: what was cleared, or that nothing was stored."""
    if not result["cleared"]:
        text.line("You are already logged out.")
    elif result["all"]:
        text.line("Credentials have been cleared for all projects.")
    elif result["project_name"] == "default":
        text.line("Credentials have been cleared for the default project.")
    else:
        text.line(f"Credentials have been cleared for {result['project_name']}.")
    if result["revoked"]:
        text.line(
            f"Browser login revoked at the server: {', '.join(result['revoked'])}."
        )
    if result["revoke_failed"]:
        text.line(
            "Note: the server did not confirm revoking the browser login of "
            f"{', '.join(result['revoke_failed'])}; it was removed from this machine anyway."
        )
    if result["env_key_set"]:
        text.line(
            "Note: ENINESITES_API_KEY is still set in this environment and still used."
        )


def print_config(result: ConfigRoot) -> None:
    """``config`` as text: each setting and the tier that answered."""

    def shown(value: object, source: object) -> str:
        if value is None:
            return "(not set)"
        return f"{value}  [{source}]"

    exists = "" if result["config_exists"] else "  (does not exist yet)"
    text.record(
        {
            "project": shown(result["project_name"], result["project_source"]),
            "config file": f"{result['config_path']}{exists}",
            "base url": shown(result["base_url"], result["base_url_source"]),
            "login": {"api_key": "API key", "browser": "browser login"}.get(
                result["auth"] or "", "(not logged in)"
            ),
            "api key": shown(result["api_key"], result["api_key_source"]),
            "token expires": result["token_expires"] or "-",
            "site": shown(result["site"], result["site_source"]),
        }
    )


def print_describe(result: DescribeRoot) -> None:
    """``describe`` as text: one line per command; ``--json`` carries the flags and schemas."""
    text.table(
        [
            {**c, "writes": "write" if c["writes"] else "read"}
            for c in result["commands"]
        ],
        ["command", "writes", "description"],
    )


def error(message: str) -> None:
    """A refusal the api raised, on stderr, so stdout stays empty."""
    print(message, file=sys.stderr)
