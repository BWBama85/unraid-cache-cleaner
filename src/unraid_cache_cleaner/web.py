"""Read-only web viewer for the Plex duplicate report (#34, Phase 1).

This is the project's **first inbound listener** — every other surface
(``qbittorrent.py``, ``plex.py``, ``arr.py``) is an *outbound* client. It serves
the on-disk ``plex_duplicate_report_path`` JSON snapshot as a browsable HTML page
and a raw JSON endpoint. It is rigorously read-only:

* it NEVER regenerates the report — no Plex / ``*arr`` / qBittorrent / SQLite
  access happens here; it only reads a file the ``plex-duplicates`` subcommand (or
  a cron) already wrote, so a page load can never fan out to Plex or block on a
  network round-trip;
* it exposes NO mutation path — the delete/action layer that reclaims duplicates
  is the fail-closed Phase 2 follow-up, and deliberately lives outside this
  module. Every non-``GET`` verb is answered ``405``.

Safety envelope for untrusted display data: every Plex-supplied string (titles,
file paths, warnings) is HTML-escaped via :func:`html.escape`; routes are
explicit (no directory serving, no CORS, no external asset fetch), the inline CSS
is the only non-``'none'`` CSP source, and a malformed/truncated/missing report
degrades to an empty-state page rather than a ``500``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from html import escape as _escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, List, Optional
from urllib.parse import urlparse

from .config import Config

LOGGER = logging.getLogger(__name__)

_GIB = 1024 ** 3

#: A provider returns the parsed report payload dict, or ``None`` when no usable
#: report exists yet (missing file, unreadable, malformed, or not an object).
ReportProvider = Callable[[], Optional[dict]]

# A restrictive policy: nothing loads by default, and the only relaxation is the
# page's own inline ``<style>`` block. No scripts, images, fonts, or frames — the
# viewer ships zero external assets, so the browser needs to fetch nothing.
_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def _fmt_gib(num_bytes: object) -> str:
    try:
        return f"{float(num_bytes) / _GIB:.1f} GiB"
    except (TypeError, ValueError):
        return "? GiB"


def file_report_provider(path: Path) -> ReportProvider:
    """A provider that reads and parses the on-disk report at ``path``.

    Returns ``None`` — an empty-state signal, never an exception — when the file
    is missing, unreadable, not valid JSON (e.g. a reader that raced a
    non-atomic writer, though :meth:`PlexDuplicateReporter.write_report` now
    writes atomically), or not a JSON object.
    """

    def provide() -> Optional[dict]:
        try:
            text = path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            LOGGER.warning(
                "Plex duplicate report at %s is not valid JSON; showing empty state",
                path,
            )
            return None
        if not isinstance(data, dict):
            LOGGER.warning(
                "Plex duplicate report at %s is not a JSON object; showing empty state",
                path,
            )
            return None
        return data

    return provide


class DuplicateReportViewer:
    """Renders the current report payload as HTML or a JSON API response.

    Holds only its :data:`ReportProvider`, so it constructs no service clients and
    is trivially unit-testable with an injected fake provider.
    """

    def __init__(self, provider: ReportProvider) -> None:
        self._provider = provider

    def render_html(self) -> str:
        return render_report_html(self._provider())

    def report_api(self) -> dict:
        """The JSON API contract: a stable wrapper around the report snapshot.

        Wrapping (rather than serving the on-disk bytes verbatim) keeps the API
        contract independent of the on-disk file and lets a polling client read a
        steady ``200`` with ``available: false`` before the first report exists.
        """

        payload = self._provider()
        return {"available": payload is not None, "report": payload}

    @staticmethod
    def render_not_found() -> str:
        return _page(
            "Not found",
            "<h1>404</h1><p>No such page. Try <a href=\"/\">the report</a>.</p>",
        )


# --------------------------------------------------------------------------- #
# HTML rendering (pure functions over the payload dict)                        #
# --------------------------------------------------------------------------- #

def _esc(value: object) -> str:
    # ``quote=True`` (the default) also escapes quotes, which matters for any
    # value that lands inside an HTML attribute.
    return _escape(str(value))


_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 1.5rem; font: 15px/1.5 -apple-system, BlinkMacSystemFont,
  "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: #1a1a1a; background: #f6f7f9; }
main { max-width: 1100px; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; }
h2 { font-size: 1.15rem; margin: 2rem 0 .5rem; }
.sub { color: #666; margin: 0 0 1rem; font-size: .9rem; }
.totals { display: flex; flex-wrap: wrap; gap: .75rem; margin: 1rem 0; }
.tile { background: #fff; border: 1px solid #e2e5e9; border-radius: 8px; padding: .6rem .9rem; }
.tile .n { font-size: 1.3rem; font-weight: 600; }
.tile .l { color: #666; font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #e2e5e9;
  border-radius: 8px; overflow: hidden; }
th, td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid #eef0f2; vertical-align: top; }
th { background: #f0f2f4; font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; color: #555; }
tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.parts { margin: .25rem 0 0; padding-left: 1rem; color: #666; font-size: .85rem; }
.tag { display: inline-block; padding: 0 .4rem; border-radius: 4px; font-size: .75rem; font-weight: 600; }
.tag.tracked { background: #fdecec; color: #b3261e; }
.tag.unknown { background: #fff4e5; color: #8a5300; }
.tag.keep { background: #e7f5ec; color: #1b6b39; }
.empty { background: #fff; border: 1px dashed #cfd4da; border-radius: 8px; padding: 1.5rem; color: #555; }
.warn { background: #fff4e5; border: 1px solid #f0d9b0; border-radius: 8px; padding: .6rem .9rem; margin: .4rem 0; }
.err { background: #fdecec; border: 1px solid #f0b3ae; border-radius: 8px; padding: .6rem .9rem; margin: .4rem 0; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
footer { margin: 2rem 0 0; color: #888; font-size: .8rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e6; background: #14161a; }
  .tile, table, .empty { background: #1d2026; border-color: #2c313a; }
  th { background: #23272f; color: #aab; }
  th, td { border-color: #262b33; }
  .sub, .tile .l, .parts { color: #9aa1ab; }
}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f"<style>{_STYLE}</style></head><body><main>{body}"
        '<footer>unraid-cache-cleaner &middot; read-only Plex duplicate viewer '
        "&middot; this page never deletes anything.</footer>"
        "</main></body></html>"
    )


def render_report_html(payload: Optional[dict]) -> str:
    """Render the report payload as a full HTML page (pure)."""

    if payload is None:
        body = (
            "<h1>Plex duplicate report</h1>"
            '<p class="sub">No report available yet.</p>'
            '<div class="empty">Run <code>unraid-cache-cleaner plex-duplicates</code> '
            "(or wait for the scheduled scan) to generate a report, then reload this "
            "page. This viewer only displays an existing report — it never runs a "
            "scan itself.</div>"
        )
        return _page("Plex duplicate report", body)

    groups = payload.get("groups") or []
    totals = payload.get("totals") or {}
    arr_enabled = bool(payload.get("arr_enabled"))

    parts: List[str] = ["<h1>Plex duplicate report</h1>"]
    parts.append(f'<p class="sub">{_render_meta(payload)}</p>')
    parts.append(_render_totals(totals, arr_enabled))
    parts.append(_render_messages(payload.get("warnings"), "warn"))
    parts.append(_render_messages(payload.get("errors"), "err"))

    if not groups:
        parts.append(
            '<div class="empty">No duplicate media found in the scanned sections. '
            "Nothing to reclaim.</div>"
        )
        return _page("Plex duplicate report", "".join(parts))

    reclaimable = [g for g in groups if g.get("classification") != "mismatch"]
    mismatches = [g for g in groups if g.get("classification") == "mismatch"]

    parts.append(_render_reclaimable(reclaimable, arr_enabled))
    parts.append(_render_mismatches(mismatches))
    parts.append(_render_arr_tracked(reclaimable, arr_enabled))
    return _page("Plex duplicate report", "".join(parts))


def _render_meta(payload: dict) -> str:
    bits: List[str] = []
    generated = payload.get("generated_at")
    if isinstance(generated, (int, float)):
        # Rendered as the raw epoch plus a compact UTC stamp; no locale/tz guess.
        stamp = _utc_stamp(generated)
        bits.append(f"Generated {_esc(stamp)}")
    plex_url = payload.get("plex_url")
    if plex_url:
        bits.append(f"Plex: <code>{_esc(plex_url)}</code>")
    sections = payload.get("sections") or []
    if sections:
        names = ", ".join(
            f"{_esc(s.get('title', '?'))} (#{_esc(s.get('key', '?'))})" for s in sections
        )
        bits.append(f"Sections: {names}")
    return " &middot; ".join(bits) or "No scan metadata."


def _utc_stamp(epoch: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(epoch))
    except (OverflowError, OSError, ValueError):
        return str(epoch)


def _render_totals(totals: dict, arr_enabled: bool) -> str:
    tiles = [
        ("Duplicate groups", totals.get("duplicate_group_count", 0)),
        ("Reclaimable", _fmt_gib(totals.get("reclaimable_bytes", 0))),
        ("Reclaimable (keep smallest)", _fmt_gib(totals.get("reclaimable_bytes_keep_smallest", 0))),
        ("Mismatches (excluded)", totals.get("mismatch_count", 0)),
    ]
    if arr_enabled:
        tiles.append(("*arr-tracked reclaimable", totals.get("arr_tracked_reclaimable_count", 0)))
    cells = "".join(
        f'<div class="tile"><div class="n">{_esc(value)}</div>'
        f'<div class="l">{_esc(label)}</div></div>'
        for label, value in tiles
    )
    return f'<div class="totals">{cells}</div>'


def _render_messages(messages: Optional[list], css_class: str) -> str:
    if not messages:
        return ""
    return "".join(f'<div class="{css_class}">{_esc(m)}</div>' for m in messages)


def _copy_label(copy: dict) -> str:
    res = copy.get("resolution") or "?"
    return _esc(res)


def _render_parts(copy: dict) -> str:
    """A <ul> of the copy's physical files at their true sizes (#17)."""

    files = copy.get("parts") or [{"file": copy.get("file"), "size": copy.get("size")}]
    items = "".join(
        f'<li><code>{_esc(p.get("file", "?"))}</code> '
        f'<span class="num">{_esc(_fmt_gib(p.get("size", 0)))}</span></li>'
        for p in files
    )
    return f'<ul class="parts">{items}</ul>'


def _assoc_tag(copy: dict) -> str:
    assoc = copy.get("association")
    if assoc == "tracked":
        svc = copy.get("arr_tracked") or "*arr"
        return f'<span class="tag tracked">{_esc(svc)}</span>'
    if assoc == "unknown":
        return '<span class="tag unknown">unknown</span>'
    return ""


def _render_reclaimable(groups: List[dict], arr_enabled: bool) -> str:
    groups = sorted(groups, key=lambda g: -_as_int(g.get("reclaimable_bytes")))
    total = sum(_as_int(g.get("reclaimable_bytes")) for g in groups)
    header = (
        f"<h2>Reclaimable (safe) &mdash; {_esc(_fmt_gib(total))} "
        f"across {len(groups)} groups</h2>"
    )
    if not groups:
        return header + '<div class="empty">None.</div>'

    rows: List[str] = []
    for group in groups:
        copies = group.get("copies") or []
        keeper = copies[0] if copies else None
        candidates = copies[1:] if copies else []
        keep_res = _copy_label(keeper) if keeper else "?"
        detail = "".join(
            f'<div>{_assoc_tag(c)} {_copy_label(c)}{_render_parts(c)}</div>'
            for c in candidates
        )
        rows.append(
            "<tr>"
            f'<td class="num">{_esc(_fmt_gib(group.get("reclaimable_bytes", 0)))}</td>'
            f'<td>{_esc(group.get("classification", "?"))}</td>'
            f'<td>{_esc(group.get("kind", "?"))}</td>'
            f'<td><span class="tag keep">keep {keep_res}</span></td>'
            f'<td class="num">{len(copies)}</td>'
            f'<td>{_esc(group.get("title", "?"))}{detail}</td>'
            "</tr>"
        )
    return (
        header
        + "<table><thead><tr><th>Reclaimable</th><th>Class</th><th>Kind</th>"
        "<th>Keeper</th><th>Copies</th><th>Title &amp; redundant copies</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_mismatches(groups: List[dict]) -> str:
    header = (
        "<h2>Review &mdash; possible mismatches (never reclaimed) &mdash; "
        f"{len(groups)} groups</h2>"
    )
    if not groups:
        return header + '<div class="empty">None.</div>'
    rows: List[str] = []
    for group in sorted(groups, key=lambda g: (g.get("kind", ""), g.get("title", ""))):
        copies = group.get("copies") or []
        files = "".join(
            f'<li><code>{_esc(c.get("file", "?"))}</code> '
            f'<span class="num">{_esc(_fmt_gib(c.get("size", 0)))}</span></li>'
            for c in copies
        )
        rows.append(
            "<tr>"
            f'<td>{_esc(group.get("kind", "?"))}</td>'
            f'<td>{_esc(group.get("title", "?"))}<ul class="parts">{files}</ul></td>'
            "</tr>"
        )
    return (
        header
        + "<p class=\"sub\">Plex merged different titles under one item; a delete here "
        "would destroy a different film/episode. Check each by hand.</p>"
        "<table><thead><tr><th>Kind</th><th>Title &amp; conflicting files</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_arr_tracked(groups: List[dict], arr_enabled: bool) -> str:
    header = "<h2>&#9888; *arr-tracked (Radarr/Sonarr)</h2>"
    if not arr_enabled:
        return header + (
            '<div class="empty">Not configured. Set <code>RADARR_URL</code>/'
            "<code>RADARR_API_KEY</code> or <code>SONARR_URL</code>/"
            "<code>SONARR_API_KEY</code> to flag redundant copies that re-download "
            "when deleted.</div>"
        )
    rows: List[str] = []
    for group in groups:
        copies = group.get("copies") or []
        for copy in copies[1:]:  # non-keeper reclaim candidates
            if copy.get("association") != "tracked":
                continue
            rows.append(
                "<tr>"
                f'<td>{_esc(group.get("kind", "?"))}</td>'
                f'<td>{_esc(group.get("title", "?"))}</td>'
                f'<td>{_esc(copy.get("arr_tracked") or "*arr")}</td>'
                f'<td>{_render_parts(copy)}</td>'
                "</tr>"
            )
    if not rows:
        return header + (
            '<div class="empty">No reclaimable copy is *arr-tracked &mdash; the safe '
            "copies above are not managed by Radarr/Sonarr.</div>"
        )
    return (
        header
        + "<p class=\"sub\">Delete these via Radarr/Sonarr (or unmonitor first) or they "
        "re-download.</p>"
        "<table><thead><tr><th>Kind</th><th>Title</th><th>Tracked by</th>"
        "<th>File(s)</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# HTTP server                                                                  #
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    """Explicit-route handler: only ``GET`` of a known path is served."""

    server_version = "unraid-cache-cleaner-web"
    # Silence the default per-request stderr spam; route through the module
    # logger at debug instead, keeping the one-line-per-run logging convention.
    protocol_version = "HTTP/1.1"

    @property
    def _viewer(self) -> DuplicateReportViewer:
        return self.server.viewer  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler dispatch name)
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(HTTPStatus.OK, self._viewer.render_html())
        elif path == "/api/report":
            body = json.dumps(self._viewer.report_api(), sort_keys=True).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, body, "application/json; charset=utf-8")
        elif path == "/healthz":
            self._send_bytes(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        else:
            self._send_html(HTTPStatus.NOT_FOUND, self._viewer.render_not_found())

    def _method_not_allowed(self) -> None:
        body = json.dumps(
            {"error": "method not allowed", "detail": "this viewer is read-only (GET only)"}
        ).encode("utf-8")
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self._common_headers(len(body), "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    # Every mutating verb is refused — the action layer is Phase 2 and does not
    # live in this module. HEAD/OPTIONS are refused too, keeping the surface tiny.
    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    def _send_html(self, status: HTTPStatus, html_text: str) -> None:
        self._send_bytes(status, html_text.encode("utf-8"), "text/html; charset=utf-8")

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self._common_headers(len(body), content_type)
        self.end_headers()
        self.wfile.write(body)

    def _common_headers(self, length: int, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("Referrer-Policy", "no-referrer")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        LOGGER.debug("web %s - %s", self.address_string(), format % args)


class DuplicateReportServer:
    """A ``ThreadingHTTPServer`` wrapper serving :class:`DuplicateReportViewer`.

    ``port``/``bind_address`` reflect the *actual* bound socket (so a test can
    pass port ``0`` and read the ephemeral port back). Each request is served on
    its own daemon thread; since the viewer only reads a file, requests are
    independent and need no lock.
    """

    def __init__(self, bind_address: str, port: int, viewer: DuplicateReportViewer) -> None:
        self._httpd = ThreadingHTTPServer((bind_address, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.viewer = viewer  # type: ignore[attr-defined]

    @property
    def bind_address(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def serve_forever(self) -> None:
        """Block, serving requests until :meth:`shutdown` (or KeyboardInterrupt)."""

        self._httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        """Serve on a daemon thread and return it (for folding into ``service``)."""

        thread = threading.Thread(
            target=self._httpd.serve_forever, name="web-viewer", daemon=True
        )
        thread.start()
        return thread

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def build_server(config: Config, *, provider: Optional[ReportProvider] = None) -> DuplicateReportServer:
    """Construct a viewer server from ``config`` (tests inject a fake provider)."""

    if provider is None:
        provider = file_report_provider(config.plex_duplicate_report_path)
    viewer = DuplicateReportViewer(provider)
    return DuplicateReportServer(config.web_bind_address, config.web_port, viewer)
