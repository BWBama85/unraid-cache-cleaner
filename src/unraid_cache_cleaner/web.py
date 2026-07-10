"""Web viewer + action layer for the Plex duplicate report (#34).

This is the project's **first inbound listener** — every other surface
(``qbittorrent.py``, ``plex.py``, ``arr.py``) is an *outbound* client. It serves
the on-disk ``plex_duplicate_report_path`` JSON snapshot as a browsable HTML page
and a raw JSON endpoint (Phase 1), and — only when explicitly enabled — a
fail-closed **action layer** that reclaims selected redundant copies (Phase 2).

Read path (always on):

* it NEVER regenerates the report — no Plex / ``*arr`` / qBittorrent / SQLite
  access happens on a GET; it only reads a file the ``plex-duplicates`` subcommand
  (or a cron) already wrote, so a page load can never fan out to Plex or block on a
  network round-trip.

Action path (Phase 2, off unless ``WEB_ENABLE_ACTIONS=true``):

* a ``POST /api/reclaim`` (JSON) and a no-JS ``POST /actions/reclaim`` (browser
  form) hand a ``{rating_key, part_id}`` selection to :class:`~unraid_cache_cleaner.web_actions.ReclaimService`,
  which owns the entire safety envelope (token gate, fresh-snapshot resolution,
  keeper/mismatch/unknown refusals, TOCTOU re-validation, audit). This module only
  parses the request and renders the result — it never deletes anything itself.
* when no reclaim service is attached (the plain viewer, or actions disabled),
  every non-``GET`` verb is answered ``405``, exactly as in Phase 1.

Safety envelope for untrusted display data: every Plex-supplied string (titles,
file paths, warnings) is HTML-escaped via :func:`html.escape`; routes are
explicit (no directory serving, no CORS, no external asset fetch), the inline CSS
is the only non-``'none'`` CSP source (with ``form-action 'self'`` added only when
the action form is served), and a malformed/truncated/missing report degrades to
an empty-state page rather than a ``500``.
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
from urllib.parse import parse_qsl, urlparse

from .config import Config
from .web_actions import ReclaimResponse, ReclaimService, ReclaimTarget

LOGGER = logging.getLogger(__name__)

_GIB = 1024 ** 3

#: Cap on a reclaim request body — a duplicate report has at most a few thousand
#: groups, so a legitimate selection is small; anything larger is refused unread.
_MAX_ACTION_BODY_BYTES = 256 * 1024

#: A provider returns the parsed report payload dict, or ``None`` when no usable
#: report exists yet (missing file, unreadable, malformed, or not an object).
ReportProvider = Callable[[], Optional[dict]]


def _csp(actions_enabled: bool) -> str:
    """The Content-Security-Policy. Nothing loads by default; the only relaxations
    are the page's own inline ``<style>`` and — only when the action form is
    served — a same-origin ``form-action`` so the reclaim POST is permitted. No
    scripts, images, fonts, or frames: the page ships zero external assets."""

    form_action = "'self'" if actions_enabled else "'none'"
    return (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        f"form-action {form_action}"
    )


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

    Parses lazily: the result is memoized against the file's ``(mtime, size)`` so
    a page refresh or a polling ``/api/report`` client does not re-read and
    re-parse an unchanged report (which can be large) on every request; the
    atomic writer bumps ``mtime`` on publish, so a fresh report is picked up on
    the next request.
    """

    cache: dict = {}  # {"entry": ((mtime, size), Optional[dict])}

    def provide() -> Optional[dict]:
        try:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return None
        # The (key, value) pair is stored and read as ONE tuple assignment, so a
        # reader that races the writer (the viewer and the action layer share this
        # provider across threads) can never observe a new key paired with the old
        # value — it sees either the whole old snapshot or the whole new one.
        entry = cache.get("entry")
        if entry is not None and entry[0] == key:
            return entry[1]

        value = _read_report(path)
        cache["entry"] = (key, value)
        return value

    return provide


def _read_report(path: Path) -> Optional[dict]:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        LOGGER.warning(
            "Plex duplicate report at %s is not valid JSON; showing empty state", path
        )
        return None
    if not isinstance(data, dict):
        LOGGER.warning(
            "Plex duplicate report at %s is not a JSON object; showing empty state", path
        )
        return None
    return data


class DuplicateReportViewer:
    """Renders the current report payload as HTML or a JSON API response.

    Holds only its :data:`ReportProvider`, so it constructs no service clients and
    is trivially unit-testable with an injected fake provider.
    """

    def __init__(self, provider: ReportProvider, *, actions_enabled: bool = False) -> None:
        self._provider = provider
        # When True the page renders the no-JS reclaim form (checkboxes + a token
        # field). Off for the plain read-only viewer, so the form and its relaxed
        # ``form-action`` CSP never appear unless an operator opted into actions.
        self._actions_enabled = actions_enabled

    def render_html(self) -> str:
        """Render the current report, degrading a corrupt one to the empty state.

        The provider only guarantees the payload is a JSON object; a report whose
        *nested* shape is wrong (a hand-edited or schema-drifted file where, say,
        ``groups`` is not a list of dicts) would raise inside the renderer. Catch
        that here so the documented "malformed report → empty-state page, never a
        500" guarantee holds for structural corruption, not just a bad top level.
        """

        try:
            return render_report_html(self._provider(), actions_enabled=self._actions_enabled)
        except Exception:  # noqa: BLE001 — any structural corruption degrades gracefully
            LOGGER.warning("rendering the duplicate report failed; showing empty state", exc_info=True)
            return render_report_html(None, actions_enabled=self._actions_enabled)

    def report_api(self) -> dict:
        """The JSON API contract: a stable wrapper around the report snapshot.

        Wrapping (rather than serving the on-disk bytes verbatim) keeps the API
        contract independent of the on-disk file and lets a polling client read a
        steady ``200`` with ``available: false`` before the first report exists.
        A provider failure degrades to ``available: false`` rather than raising.
        """

        try:
            payload = self._provider()
        except Exception:  # noqa: BLE001
            LOGGER.warning("reading the duplicate report failed; reporting unavailable", exc_info=True)
            payload = None
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
.controls { margin: 1rem 0; display: flex; flex-wrap: wrap; gap: .75rem; align-items: center; }
.controls input[type="password"] { padding: .3rem .4rem; }
.controls button { padding: .4rem .9rem; border-radius: 6px; border: 1px solid #b3261e;
  background: #b3261e; color: #fff; font-weight: 600; cursor: pointer; }
footer { margin: 2rem 0 0; color: #888; font-size: .8rem; }
@media (prefers-color-scheme: dark) {
  body { color: #e6e6e6; background: #14161a; }
  .tile, table, .empty { background: #1d2026; border-color: #2c313a; }
  th { background: #23272f; color: #aab; }
  th, td { border-color: #262b33; }
  .sub, .tile .l, .parts { color: #9aa1ab; }
}
"""


_READONLY_FOOTER = "read-only Plex duplicate viewer &middot; this page never deletes anything."
_ACTION_FOOTER = (
    "action layer enabled &middot; deletes are gated by WEB_ACTION_TOKEN and "
    "WEB_ACTIONS_DRY_RUN."
)


def _page(title: str, body: str, *, footer_note: str = _READONLY_FOOTER) -> str:
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title>"
        f"<style>{_STYLE}</style></head><body><main>{body}"
        f"<footer>unraid-cache-cleaner &middot; {footer_note}</footer>"
        "</main></body></html>"
    )


def render_report_html(payload: Optional[dict], *, actions_enabled: bool = False) -> str:
    """Render the report payload as a full HTML page (pure).

    When ``actions_enabled`` the reclaimable section is wrapped in a no-JS form
    (a checkbox per redundant copy + a token field) that POSTs to
    ``/actions/reclaim``; otherwise the page is the Phase 1 read-only viewer.
    """

    footer = _ACTION_FOOTER if actions_enabled else _READONLY_FOOTER

    if payload is None:
        body = (
            "<h1>Plex duplicate report</h1>"
            '<p class="sub">No report available yet.</p>'
            '<div class="empty">Run <code>unraid-cache-cleaner plex-duplicates</code> '
            "(or wait for the scheduled scan) to generate a report, then reload this "
            "page. This viewer only displays an existing report — it never runs a "
            "scan itself.</div>"
        )
        return _page("Plex duplicate report", body, footer_note=footer)

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
        return _page("Plex duplicate report", "".join(parts), footer_note=footer)

    reclaimable = [g for g in groups if g.get("classification") != "mismatch"]
    mismatches = [g for g in groups if g.get("classification") == "mismatch"]

    reclaimable_html = _render_reclaimable(reclaimable, actions_enabled=actions_enabled)
    if actions_enabled:
        reclaimable_html = _wrap_reclaim_form(reclaimable_html, payload.get("generated_at"))
    parts.append(reclaimable_html)
    parts.append(_render_mismatches(mismatches))
    parts.append(_render_arr_tracked(reclaimable, arr_enabled))
    return _page("Plex duplicate report", "".join(parts), footer_note=footer)


def _wrap_reclaim_form(reclaimable_html: str, generated_at: object) -> str:
    """Wrap the reclaimable table in the no-JS action form: the checkboxes rendered
    inside it, plus the token field, the hidden ``generated_at`` freshness token,
    and the submit button. No JavaScript — a native form POST keeps the strict CSP
    (only ``form-action 'self'`` is relaxed)."""

    gen = _esc("" if generated_at is None else generated_at)
    return (
        '<form method="post" action="/actions/reclaim" class="reclaim">'
        + reclaimable_html
        + f'<input type="hidden" name="report_generated_at" value="{gen}">'
        + '<div class="controls"><label>Action token '
        + '<input type="password" name="token" autocomplete="off" required></label> '
        + '<button type="submit">Reclaim selected</button></div>'
        + '<p class="sub">Deletes the selected redundant copies — an *arr-tracked '
        + "copy is removed via Radarr/Sonarr so it does not re-download. Refuses the "
        + "keeper, mismatch groups, and unconfirmed copies. Honors "
        + "<code>WEB_ACTIONS_DRY_RUN</code>.</p>"
        + "</form>"
    )


def render_reclaim_result_html(response: ReclaimResponse) -> str:
    """Render a reclaim outcome as an HTML result page (for the browser form)."""

    rows: List[str] = []
    for result in response.results:
        rows.append(
            "<tr>"
            f'<td>{_esc(result.status)}</td>'
            f'<td>{_esc(result.backend or "-")}</td>'
            f'<td>{_esc(result.rating_key)}:{_esc(result.part_id)}</td>'
            f'<td class="num">{_esc(_fmt_gib(result.reclaimed_bytes))}</td>'
            f'<td>{_esc(result.message)}</td>'
            "</tr>"
        )
    mode = "DRY-RUN (nothing was deleted)" if response.dry_run else "LIVE"
    body = [f"<h1>Reclaim result &mdash; {_esc(mode)}</h1>"]
    if response.message:
        body.append(f'<div class="warn">{_esc(response.message)}</div>')
    if rows:
        body.append(
            "<table><thead><tr><th>Status</th><th>Backend</th><th>Target</th>"
            "<th>Reclaimed</th><th>Detail</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>"
        )
    elif not response.message:
        body.append('<div class="empty">No targets were selected.</div>')
    body.append('<p class="sub"><a href="/">&larr; Back to the report</a></p>')
    return _page("Reclaim result", "".join(body), footer_note=_ACTION_FOOTER)


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


def _copy_label(copy: Optional[dict]) -> str:
    res = (copy or {}).get("resolution") or "?"
    return _esc(res)


def _file_li(entry: dict) -> str:
    """One physical-file list item: escaped path + right-aligned true size."""

    return (
        f'<li><code>{_esc(entry.get("file", "?"))}</code> '
        f'<span class="num">{_esc(_fmt_gib(entry.get("size", 0)))}</span></li>'
    )


def _render_parts(copy: dict) -> str:
    """A <ul> of the copy's physical files at their true sizes (#17)."""

    files = copy.get("parts") or [{"file": copy.get("file"), "size": copy.get("size")}]
    return f'<ul class="parts">{"".join(_file_li(p) for p in files)}</ul>'


def _is_keeper(copy: dict, keeper: Optional[dict]) -> bool:
    """Identity match against the report's authoritative ``keeper`` copy.

    A logical copy is uniquely identified by its first-part ``file`` and its Plex
    ``media_id``; the serialized keeper carries the same two, so this holds even
    if ``copies`` were ever reordered for display.
    """

    if keeper is None:
        return False
    return copy.get("file") == keeper.get("file") and copy.get("media_id") == keeper.get(
        "media_id"
    )


def _reclaim_candidates(group: dict) -> List[dict]:
    """The redundant copies a reclaim would delete: every copy but the keeper.

    Uses the report's explicit ``keeper`` field rather than assuming ``copies`` is
    keeper-first, so the view can never mislabel the wrong copy as the keeper.
    Falls back to "all but the first" only when no keeper is serialized.
    """

    copies = group.get("copies") or []
    keeper = group.get("keeper")
    if keeper is None:
        return copies[1:]
    return [c for c in copies if not _is_keeper(c, keeper)]


def _assoc_tag(copy: dict) -> str:
    assoc = copy.get("association")
    if assoc == "tracked":
        svc = copy.get("arr_tracked") or "*arr"
        return f'<span class="tag tracked">{_esc(svc)}</span>'
    if assoc == "unknown":
        return '<span class="tag unknown">unknown</span>'
    return ""


def _checkbox(group: dict, copy: dict, actions_enabled: bool) -> str:
    """A reclaim checkbox for one redundant copy, valued ``{rating_key}:{part_id}``
    on the copy's first physical part. Targeting any one part reclaims the whole
    logical copy (all its parts), so one checkbox per copy is enough. Omitted when
    the copy is unaddressable (no rating_key or a zero/absent first part_id) so a
    row can never post an id the action layer would refuse anyway."""

    if not actions_enabled:
        return ""
    rating_key = group.get("rating_key")
    parts = copy.get("parts") or []
    part_id = parts[0].get("part_id") if parts and isinstance(parts[0], dict) else None
    if not rating_key or not part_id:
        return '<span class="tag unknown">n/a</span> '
    value = f"{rating_key}:{part_id}"
    return f'<input type="checkbox" name="target" value="{_esc(value)}"> '


def _render_reclaimable(groups: List[dict], *, actions_enabled: bool = False) -> str:
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
        candidates = _reclaim_candidates(group)
        keep_res = _copy_label(group.get("keeper"))
        detail = "".join(
            f'<div>{_checkbox(group, c, actions_enabled)}{_assoc_tag(c)} '
            f'{_copy_label(c)}{_render_parts(c)}</div>'
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
        files = "".join(_file_li(c) for c in copies)
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
    unknown_count = 0
    for group in groups:
        for copy in _reclaim_candidates(group):  # non-keeper reclaim candidates
            assoc = copy.get("association")
            if assoc == "unknown":
                unknown_count += 1
            if assoc != "tracked":
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
        # "safe" is only honest when nothing is unconfirmed: an *arr outage, or a
        # TV copy whose filename didn't match, leaves reclaim candidates `unknown`,
        # which must never be presented as safe (mirrors the CLI's caveat).
        if unknown_count:
            return header + (
                f'<div class="empty">No reclaimable copy is *arr-tracked, but '
                f"{unknown_count} could not be confirmed (<code>unknown</code>) "
                "&mdash; verify those before deleting.</div>"
            )
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
# Request parsing (report-selection only; no trusted attributes of the delete) #
# --------------------------------------------------------------------------- #

def _parse_targets_json(raw: object) -> List[ReclaimTarget]:
    """Parse the JSON ``targets`` array into :class:`ReclaimTarget`s leniently — a
    malformed id becomes an invalid target the action layer refuses, never a parse
    error that drops the whole (possibly valid) batch."""

    if not isinstance(raw, list):
        return []
    targets: List[ReclaimTarget] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        targets.append(ReclaimTarget(str(item.get("rating_key") or ""), _as_int(item.get("part_id"))))
    return targets


def _parse_form_reclaim(raw: bytes) -> tuple:
    """Parse a form-encoded reclaim body into ``(token, report_generated_at,
    targets)``. Each ``target`` field is ``rating_key:part_id`` (split on the last
    ``:`` so a rating_key containing a colon still parses)."""

    token: Optional[str] = None
    generated_at: Optional[str] = None
    targets: List[ReclaimTarget] = []
    for key, value in parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True):
        if key == "token":
            token = value
        elif key == "report_generated_at":
            generated_at = value
        elif key == "target":
            rating_key, sep, part_id = value.rpartition(":")
            if sep:
                targets.append(ReclaimTarget(rating_key, _as_int(part_id)))
    return token, generated_at, targets


# --------------------------------------------------------------------------- #
# HTTP server                                                                  #
# --------------------------------------------------------------------------- #

class _Handler(BaseHTTPRequestHandler):
    """Explicit-route handler: only ``GET``/``HEAD`` of a known path is served.

    Left at the default HTTP/1.0 (no keep-alive) on purpose: every response then
    closes the connection, so a refused verb's body or a HEAD's absent body can
    never desync a persistent connection. ``timeout`` drops a slow/idle client so
    a single connection cannot pin a worker thread indefinitely, and
    ``sys_version = ""`` keeps the interpreter version out of the ``Server``
    header.
    """

    server_version = "unraid-cache-cleaner-web"
    sys_version = ""
    timeout = 15

    @property
    def _viewer(self) -> DuplicateReportViewer:
        return self.server.viewer  # type: ignore[attr-defined]

    @property
    def _reclaim(self) -> Optional[ReclaimService]:
        return getattr(self.server, "reclaim_service", None)

    @property
    def _actions_enabled(self) -> bool:
        service = self._reclaim
        return bool(service is not None and service.enabled)

    def _resolve(self, path: str) -> tuple:
        """Return ``(status, content_type, body_bytes)`` for a route.

        A last-resort guard: even if a renderer raises on some unforeseen input,
        the client gets a clean error response instead of a dropped connection.
        (Malformed reports are already degraded to the empty state by the viewer.)
        """

        try:
            if path in ("/", "/index.html"):
                return (
                    HTTPStatus.OK,
                    "text/html; charset=utf-8",
                    self._viewer.render_html().encode("utf-8"),
                )
            if path == "/api/report":
                body = json.dumps(self._viewer.report_api(), sort_keys=True).encode("utf-8")
                return HTTPStatus.OK, "application/json; charset=utf-8", body
            if path == "/healthz":
                return HTTPStatus.OK, "text/plain; charset=utf-8", b"ok\n"
            return (
                HTTPStatus.NOT_FOUND,
                "text/html; charset=utf-8",
                self._viewer.render_not_found().encode("utf-8"),
            )
        except Exception:  # noqa: BLE001 — never drop the connection with no reply
            LOGGER.exception("web handler failed for %s", path)
            return (
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "text/html; charset=utf-8",
                _page("Error", "<h1>500</h1><p>The report could not be rendered.</p>").encode("utf-8"),
            )

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler dispatch name)
        status, content_type, body = self._resolve(urlparse(self.path).path)
        self._respond(status, content_type, len(body))
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        # Same headers as the equivalent GET, but no body (RFC 9110) — so a health
        # check or proxy can probe any GET route with a cheap HEAD.
        status, content_type, body = self._resolve(urlparse(self.path).path)
        self._respond(status, content_type, len(body))

    def do_POST(self) -> None:  # noqa: N802
        """Dispatch the reclaim endpoints when an action service is attached and
        actions are enabled; otherwise every POST is ``405`` (Phase 1 behavior).

        Routing on ``_actions_enabled`` (not merely on the service's presence)
        means a server built with actions disabled stays a read-only viewer whose
        every mutating verb is refused, exactly like the plain viewer."""

        service = self._reclaim
        if service is None or not service.enabled:
            self._method_not_allowed()
            return
        path = urlparse(self.path).path
        if path == "/api/reclaim":
            self._handle_reclaim_json(service)
        elif path == "/actions/reclaim":
            self._handle_reclaim_form(service)
        else:
            self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        # Accurate whether actions are on or off: the reclaim POST routes are
        # matched before this fires, so a request reaching here is a verb/route this
        # server does not serve (every GET route is GET/HEAD only).
        body = json.dumps(
            {"error": "method not allowed", "detail": "no such method for this route"}
        ).encode("utf-8")
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, HEAD")
        self._common_headers(len(body), "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    # PUT/PATCH/OPTIONS are never used; DELETE (as an HTTP verb) is not the reclaim
    # channel — reclaim is a POST — so every one of these stays a flat 405.
    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_OPTIONS = _method_not_allowed

    # -- reclaim endpoints --------------------------------------------------- #

    def _handle_reclaim_json(self, service: ReclaimService) -> None:
        """``POST /api/reclaim``: a JSON ``{report_generated_at, targets:[{rating_key,
        part_id}], token?}`` body. The token may also arrive as an ``X-Action-Token``
        header. Returns the reclaim result as JSON with the service's status code."""

        if not self._origin_ok():
            self._write_json(HTTPStatus.FORBIDDEN, {"message": "cross-origin request refused"})
            return
        raw = self._read_body()
        if raw is None:  # a 413/400 was already written
            return
        try:
            data = json.loads(raw or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"message": "request body is not valid JSON"})
            return
        if not isinstance(data, dict):
            self._write_json(HTTPStatus.BAD_REQUEST, {"message": "request body must be a JSON object"})
            return
        token = self.headers.get("X-Action-Token") or data.get("token")
        response = service.reclaim(
            _parse_targets_json(data.get("targets")),
            token=token if isinstance(token, str) else None,
            report_generated_at=data.get("report_generated_at"),
        )
        self._write_json(response.status_code, response.as_dict())

    def _handle_reclaim_form(self, service: ReclaimService) -> None:
        """``POST /actions/reclaim``: the no-JS browser form (form-encoded ``token``,
        ``report_generated_at``, and repeated ``target=rating_key:part_id``). Renders
        an HTML result page."""

        if not self._origin_ok():
            self._write_html(
                HTTPStatus.FORBIDDEN,
                _page(
                    "Reclaim refused",
                    '<h1>Cross-origin request refused</h1>'
                    '<p class="sub"><a href="/">&larr; Back to the report</a></p>',
                    footer_note=_ACTION_FOOTER,
                ),
            )
            return
        raw = self._read_body()
        if raw is None:
            return
        token, generated_at, targets = _parse_form_reclaim(raw)
        response = service.reclaim(
            targets, token=token, report_generated_at=generated_at
        )
        self._write_html(response.status_code, render_reclaim_result_html(response))

    def _origin_ok(self) -> bool:
        """Reject a cross-origin POST (a CSRF defense on top of the token): if an
        ``Origin`` header is present it must match the request ``Host``. A request
        with no ``Origin`` (curl, the JSON API) is allowed — the token still gates
        it."""

        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return urlparse(origin).netloc == host

    def _read_body(self) -> Optional[bytes]:
        """Read the request body, enforcing the size cap. Returns ``None`` (after
        writing a 413/400) when the body is too large or the length is malformed."""

        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError):
            self._write_json(HTTPStatus.BAD_REQUEST, {"message": "invalid Content-Length"})
            return None
        if length < 0 or length > _MAX_ACTION_BODY_BYTES:
            # Drain the oversized body (bounded memory, discarded in chunks) BEFORE
            # replying, so the client reads a clean 413 rather than a connection
            # reset from a half-sent request; the socket timeout bounds a client
            # that keeps sending forever.
            self._drain(length)
            self._write_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"message": "request body too large"}
            )
            return None
        return self.rfile.read(length) if length else b""

    def _drain(self, length: int) -> None:
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _write_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self._common_headers(len(body), "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, status: int, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self._common_headers(len(body), "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _respond(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self._common_headers(length, content_type)
        self.end_headers()

    def _common_headers(self, length: int, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", _csp(self._actions_enabled))
        self.send_header("Referrer-Policy", "no-referrer")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        LOGGER.debug("web %s - %s", self.address_string(), format % args)


class DuplicateReportServer:
    """A ``ThreadingHTTPServer`` wrapper serving :class:`DuplicateReportViewer`.

    ``port``/``bind_address`` reflect the *actual* bound socket (so a test can
    pass port ``0`` and read the ephemeral port back). Each request is served on
    its own daemon thread; a GET only reads a file so it needs no lock, and the
    optional :class:`ReclaimService` serializes its own mutations internally.
    """

    def __init__(
        self,
        bind_address: str,
        port: int,
        viewer: DuplicateReportViewer,
        *,
        reclaim_service: Optional[ReclaimService] = None,
    ) -> None:
        self._httpd = ThreadingHTTPServer((bind_address, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.viewer = viewer  # type: ignore[attr-defined]
        # ``None`` (or a service whose actions are disabled) keeps the server a
        # read-only viewer — every POST is 405.
        self._httpd.reclaim_service = reclaim_service  # type: ignore[attr-defined]
        self._started = False

    @property
    def bind_address(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def serve_forever(self) -> None:
        """Block, serving requests until :meth:`shutdown` (or KeyboardInterrupt)."""

        self._started = True
        self._httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        """Serve on a daemon thread and return it (for folding into ``service``)."""

        self._started = True
        thread = threading.Thread(
            target=self._httpd.serve_forever, name="web-viewer", daemon=True
        )
        thread.start()
        return thread

    def shutdown(self) -> None:
        # ``BaseServer.shutdown`` blocks on an event that only ``serve_forever``
        # sets, so calling it on a server that never started serving would hang
        # forever. Guard on the started flag and always release the socket.
        if self._started:
            self._httpd.shutdown()
        self._httpd.server_close()


def build_server(
    config: Config,
    *,
    provider: Optional[ReportProvider] = None,
    reclaim_service: Optional[ReclaimService] = None,
) -> DuplicateReportServer:
    """Construct a viewer server from ``config`` (tests inject a fake provider).

    ``reclaim_service`` is assembled by the CLI (it owns the ``*arr`` clients and
    the audit store) and passed in; when it is ``None`` — or its actions are
    disabled — the server is the Phase 1 read-only viewer. The page renders the
    action form only when the attached service actually has actions enabled.
    """

    if provider is None:
        provider = file_report_provider(config.plex_duplicate_report_path)
    actions_enabled = reclaim_service is not None and reclaim_service.enabled
    viewer = DuplicateReportViewer(provider, actions_enabled=actions_enabled)
    return DuplicateReportServer(
        config.web_bind_address, config.web_port, viewer, reclaim_service=reclaim_service
    )
