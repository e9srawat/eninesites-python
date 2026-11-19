"""The transport: one authenticated JSON request to the eninesites REST API, and its errors.

Wire conventions are the server's ``docs/API.md``: everything under ``/api/v1/``, served
only on the platform host; ``Authorization: Token <key>`` for an API key, or
``Authorization: Bearer <token>`` for a browser login (``lib.client.oauth``); success is
the resource itself (or 204 with no body); errors are RFC 9457 problem details
(``application/problem+json`` with ``code``, ``detail`` and, on validation, ``errors``);
list endpoints are paginated ``{count, next, previous, results}`` or, on several endpoints,
a bare array.

Every failure becomes the package's one refusal, ``ApiError``, whose message names the HTTP
status, the server's ``code`` and its ``detail``, and which carries the same facts as fields
(``status``, ``code``, ``detail``, ``errors``) for ``--json``. A failure with no server
``code`` gets the client's own (``errors.CLIENT_CODES``). The key never appears in a message.

In dry-run mode (``connect(..., dry_run=True)``) a GET is still sent, because a write often
resolves something first, and the first other request is not sent: ``Client.request``
raises ``dryrun.Planned`` carrying it, which the api function's ``dryrun.writes`` turns into
its result.

A browser login's access token lasts ten hours. When the server refuses it with 401,
``Client.request`` asks ``oauth.renew`` for a working one and resends the request once: a 401
means the server did nothing, so resending a write is safe. A dry run never renews, because
renewing is itself a write.

Every call made with a key or a token counts against the user's rate limits, one per minute
and one per day (the server's ``docs/API.md``, "Rate limits"). The call over a limit gets
``429`` and a ``Retry-After`` header in seconds. The server refuses it before doing anything
and does not count it, so resending it, a write included, is safe. ``Client.request`` waits
the time asked and sends it again, up to ``RATE_RETRIES`` times, when the wait is at most
``RATE_WAIT_LIMIT`` seconds: that covers the per-minute limit, so a long script slows down
instead of stopping. A longer wait means the per-day limit, and the 429 is reported at once.

``transport`` is the only function that touches the network, and ``sleep`` the only one that
waits; tests replace both.
"""

from __future__ import annotations

import json
import mimetypes
import platform
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from email.message import Message
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import quote, urlencode, urlsplit

from eninesites.errors import ApiError
from eninesites.lib import dryrun

from . import credentials, oauth
from .credentials import Settings

TIMEOUT_SECONDS = 60.0
#: The server's page-size cap (``services/manager/api/v1/pagination.py``).
PAGE_SIZE = 200
#: A runaway guard for pagination: 200 pages of 200 is 40,000 rows.
MAX_PAGES = 200
#: The longest ``Retry-After`` the client waits out on a 429, in seconds. A per-minute limit
#: asks for at most 60; a per-day limit asks for hours, and that is reported instead.
RATE_WAIT_LIMIT = 120
#: How many times one request is sent again after a 429 before the 429 is reported.
RATE_RETRIES = 5

sleep = time.sleep


@dataclass(frozen=True)
class Reply:
    """What came back: the status, the headers that matter, and the raw body."""

    status: int
    content_type: str
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class _NoRedirect(request.HTTPRedirectHandler):
    """Refuse every redirect.

    urllib would follow it and send the ``Authorization`` header to wherever it points. The
    API is served from one host, so a redirect means the base URL is wrong, and saying so is
    safer than following it (the Stripe CLI's login refuses redirects for the same reason).
    """

    # urllib's own signature, overridden as it is declared.
    # pylint: disable-next=too-many-positional-arguments
    def redirect_request(
        self,
        req: request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        raise ApiError(
            f"the server redirected {req.get_method()} {req.full_url} to {newurl} "
            f"({code}); not followed, so the API key is not sent there. If that is the "
            "API's real address, pass it as --base-url.",
            status=code,
            code="redirect_refused",
            method=req.get_method(),
            path=urlsplit(req.full_url).path,
        )


_OPENER = request.build_opener(_NoRedirect)


def transport(req: request.Request, timeout: float) -> Reply:
    """Send one request and return the reply, an HTTP error status included."""
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return Reply(
                resp.status,
                resp.headers.get_content_type(),
                resp.read(),
                {k.lower(): v for k, v in resp.headers.items()},
            )
    except error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        content_type = exc.headers.get_content_type() if exc.headers else ""
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        return Reply(exc.code, content_type, body, headers)
    except error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise timed_out(req, timeout) from exc
        raise ApiError(
            f"cannot reach {req.full_url}: {exc.reason}",
            code="unreachable",
            method=req.get_method(),
            path=urlsplit(req.full_url).path,
        ) from exc
    except TimeoutError as exc:
        raise timed_out(req, timeout) from exc


def timed_out(req: request.Request, timeout: float) -> ApiError:
    """The refusal for a server that did not answer within ``timeout`` seconds."""
    return ApiError(
        f"{req.full_url} did not answer within {timeout:.0f}s",
        code="timeout",
        method=req.get_method(),
        path=urlsplit(req.full_url).path,
    )


def _version() -> str:
    try:
        return metadata.version("eninesites")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def user_agent_headers() -> dict[str, str]:
    """The client identification headers, in the Stripe CLI's pair of shapes."""
    version = _version()
    return {
        "User-Agent": f"eninesites-python/{version} python/{platform.python_version()}",
        "X-Eninesites-Client-User-Agent": json.dumps(
            {
                "name": "eninesites-python",
                "version": version,
                "lang": "python",
                "lang_version": platform.python_version(),
                "os": platform.system().lower(),
            },
            sort_keys=True,
        ),
    }


def segment(value: str | int) -> str:
    """One URL path segment: a domain, slug or name, with ``/`` and the rest escaped."""
    return quote(str(value).strip(), safe="")


class Client:
    """An authenticated client bound to one base URL and one key."""

    def __init__(self, settings: Settings, dry_run: bool = False) -> None:
        """Bind to resolved settings; refuses now when there is no key to send."""
        self.settings = settings
        self.dry_run = dry_run
        self._key = credentials.require_key(settings)

    def site_path(self, *parts: str | int) -> str:
        """``/api/v1/site/<site>/<parts...>/`` for the resolved site."""
        site = credentials.require_site(self.settings)
        return "/api/v1/site/" + "/".join(segment(p) for p in (site, *parts)) + "/"

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int] | None = None,
        body: Any = None,
        files: list[Path] | None = None,
        form: dict[str, str] | None = None,
    ) -> Reply:
        """Send one request; return the reply when it succeeded, else raise ``ApiError``.

        In dry-run mode anything but a GET is not sent: ``dryrun.Planned`` is raised instead.
        """
        if self.dry_run and method != "GET":
            raise dryrun.Planned(
                method,
                path,
                query=query,
                body=body,
                files=[str(f) for f in files] if files is not None else None,
                form=form,
            )
        url = self.settings.base_url + path
        if query:
            url += "?" + urlencode(query)
        data: bytes | None = None
        content_type: str | None = None
        if files is not None:
            data, content_type = multipart(files, form or {})
        elif body is not None:
            data, content_type = json.dumps(body).encode("utf-8"), "application/json"
        reply = self._send(method, url, data, content_type)
        if reply.status == 401 and self.settings.auth == "bearer" and not self.dry_run:
            self._key = oauth.renew(self.settings, self._key, transport)
            reply = self._send(method, url, data, content_type)
        for attempt in range(1, RATE_RETRIES + 1):
            wait = retry_after(reply)
            if reply.status != 429 or wait is None or wait > RATE_WAIT_LIMIT:
                break
            notify(
                f"{method} {path}: rate limit reached; waiting {wait}s, then sending it "
                f"again ({attempt} of {RATE_RETRIES})"
            )
            sleep(wait)
            reply = self._send(method, url, data, content_type)
        if reply.status >= 400:
            raise refusal(method, path, reply)
        return reply

    def _send(
        self, method: str, url: str, data: bytes | None, content_type: str | None
    ) -> Reply:
        """One request with this client's credential, as ``Token`` or ``Bearer``."""
        scheme = "Bearer" if self.settings.auth == "bearer" else "Token"
        headers = {
            **user_agent_headers(),
            "Accept": "application/json",
            "Authorization": f"{scheme} {self._key}",
        }
        if content_type:
            headers["Content-Type"] = content_type
        req = request.Request(url, data=data, headers=headers, method=method)
        return transport(req, TIMEOUT_SECONDS)

    def json(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send one request and decode its JSON body; ``None`` for an empty (204) body."""
        reply = self.request(method, path, **kwargs)
        if not reply.body.strip():
            return None
        try:
            return json.loads(reply.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ApiError(
                f"{method} {path}: the server answered {reply.status} with a body that is "
                f"not JSON ({reply.content_type or 'no content type'})",
                status=reply.status,
                code="not_json",
                method=method,
                path=path,
            ) from exc

    def get(self, path: str, query: dict[str, str | int] | None = None) -> Any:
        """GET one resource as decoded JSON."""
        return self.json("GET", path, query=query)

    def list_all(
        self, path: str, query: dict[str, str | int] | None = None
    ) -> list[Any]:
        """Every row of a list endpoint, following pages until ``next`` is null.

        Tolerates both shapes ``docs/API.md`` documents: the paginated envelope and the
        bare array several endpoints return. ``query`` is a filter the endpoint takes; it is
        sent on every page, beside the paging parameters.
        """
        rows: list[Any] = []
        for page in range(1, MAX_PAGES + 1):
            payload = self.get(
                path, {**(query or {}), "page": page, "page_size": PAGE_SIZE}
            )
            if isinstance(payload, list):
                return rows + payload
            if not isinstance(payload, dict):
                return rows
            rows += payload.get("results") or []
            if not payload.get("next"):
                return rows
        raise ApiError(
            f"GET {path}: more than {MAX_PAGES} pages; refusing to continue",
            method="GET",
            path=path,
        )


def retry_after(reply: Reply) -> int | None:
    """The seconds a 429's ``Retry-After`` asks for; None when it gives no number."""
    value = reply.headers.get("retry-after", "").strip()
    return int(value) if value.isdigit() else None


def notify(message: str) -> None:
    """Say on stderr what the client is waiting for, when a person is watching it.

    Only to a terminal: under ``--json`` an agent reads stderr as one JSON document.
    """
    if sys.stderr.isatty():
        print(message, file=sys.stderr, flush=True)


def problem_of(reply: Reply) -> dict[str, Any] | None:
    """The reply's problem details (or any JSON object it carries), else None."""
    try:
        payload = json.loads(reply.body) if reply.body.strip() else None
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def refusal(method: str, path: str, reply: Reply) -> ApiError:
    """An error reply as the one refusal: a line for a person, the fields for ``--json``."""
    payload = problem_of(reply)
    code = detail = None
    errors = None
    if payload is not None:
        code = payload.get("code") or payload.get("error")
        detail = (
            payload.get("detail")
            or payload.get("message")
            or payload.get("error_description")
        )
        errors = payload.get("errors") or None
    message = describe_error(method, path, reply, payload)
    return ApiError(
        message,
        status=reply.status,
        code=str(code) if code else "not_problem",
        detail=str(detail) if detail else message,
        errors=errors if isinstance(errors, (dict, list)) else None,
        method=method,
        path=path,
    )


def describe_error(
    method: str, path: str, reply: Reply, payload: dict[str, Any] | None
) -> str:
    """One line naming what went wrong, from the problem details when the server sent them."""
    head = f"{method} {path}: HTTP {reply.status}"
    if payload is not None:
        code = payload.get("code") or payload.get("error")
        detail = (
            payload.get("detail")
            or payload.get("message")
            or payload.get("error_description")
        )
        if code and code != detail:
            head += f" {code}"
        if detail:
            head += f": {detail}"
        if payload.get("errors"):
            head += f" -- {flatten_errors(payload['errors'])}"
    elif reply.body.strip():
        text = re.sub(r"\s+", " ", reply.body.decode("utf-8", "replace")).strip()
        if reply.content_type in ("text/html", "application/xhtml+xml"):
            text = "an HTML page (is --base-url the eninesites platform host?)"
        head += f": {text[:200]}"
    if reply.status == 401:
        head = head.rstrip(".") + (
            ". Check the API key: `python -m eninesites config` shows which one is used."
        )
    wait = retry_after(reply)
    if reply.status == 429 and wait is not None:
        head = head.rstrip(".") + (
            f". The CLI waits up to {RATE_WAIT_LIMIT}s and this limit lasts longer:"
            f" run the command again in {in_words(wait)}."
            if wait > RATE_WAIT_LIMIT
            else f". Still limited after waiting {RATE_RETRIES} times; run it again later."
        )
    return head


def in_words(seconds: int) -> str:
    """A wait in hours and minutes, rounded up to the minute: ``3 h 20 min``, ``5 min``."""
    hours, minutes = divmod(-(-seconds // 60), 60)
    if not hours:
        return f"{minutes} min"
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def flatten_errors(errors: Any, prefix: str = "") -> str:
    """DRF field errors (``{"field": ["msg"]}``, nested, or a flat list) as one line."""
    if isinstance(errors, dict):
        return "; ".join(
            flatten_errors(v, f"{prefix}.{k}" if prefix else str(k))
            for k, v in errors.items()
        )
    if isinstance(errors, list):
        if any(isinstance(e, (dict, list)) for e in errors):
            return "; ".join(flatten_errors(e, prefix) for e in errors)
        text = " ".join(str(e) for e in errors)
        return f"{prefix}: {text}" if prefix else text
    return f"{prefix}: {errors}" if prefix else str(errors)


def multipart(files: list[Path], form: dict[str, str]) -> tuple[bytes, str]:
    """A ``multipart/form-data`` body: each file under the field name ``file``."""
    boundary = f"eninesites-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in form.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            + value.encode("utf-8")
            + b"\r\n"
        )
    for path in files:
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ApiError(f"cannot read {path}: {exc.strerror}") from exc
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        filename = path.name.replace('"', "_")
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: {kind}\r\n\r\n'
            ).encode()
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def connect(
    api_key: str | None = None,
    base_url: str | None = None,
    project_name: str | None = None,
    domain: str | None = None,
    *,
    dry_run: bool = False,
) -> Client:
    """Resolve the settings for one command and return a client bound to them.

    ``dry_run`` sends GETs only; see ``Client.request``.
    """
    return Client(credentials.resolve(api_key, base_url, project_name, domain), dry_run)


def filename_from(reply: Reply) -> str | None:
    """The ``filename`` a ``Content-Disposition: attachment`` header names, if any."""
    disposition = reply.headers.get("content-disposition", "")
    match = re.search(r'filename="?([^";]+)"?', disposition)
    return Path(match.group(1)).name if match else None
