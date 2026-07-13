"""Tests for the read-only Plex duplicate report web viewer (#34, Phase 1).

Each test starts the real ``ThreadingHTTPServer`` on an ephemeral port (``0``)
and drives it in-process over ``urllib`` — the same transport an operator's
browser uses — so routing, headers, status codes, and HTML escaping are all
exercised end to end. The report is supplied by an injected fake provider, so no
Plex / ``*arr`` / qBittorrent / SQLite client is ever constructed.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.web import (
    DuplicateReportServer,
    DuplicateReportViewer,
    _effective_allowed_hosts,
    _host_allowed,
    _host_only,
    _is_loopback_bind,
    _normalize_origin,
    _normalized_allowed_origins,
    _request_origin_ok,
    build_server,
    file_report_provider,
    render_actions_html,
    render_reclaim_confirm_html,
    render_reclaim_notice_html,
    render_report_html,
)
from unraid_cache_cleaner.web_actions import ReclaimResponse, ReclaimResult

GiB = 1024 ** 3


def _payload(**overrides) -> dict:
    """A representative arr-enabled report: one reclaimable group + one mismatch."""

    payload = {
        "generated_at": 1_720_000_000.0,
        "plex_url": "http://plex:32400",
        "sections": [{"key": "1", "type": "movie", "title": "Movies"}],
        "totals": {
            "duplicate_group_count": 2,
            "reclaimable_bytes": 8 * GiB,
            "reclaimable_bytes_keep_smallest": 4 * GiB,
            "mismatch_count": 1,
            "arr_tracked_reclaimable_count": 1,
        },
        "arr_enabled": True,
        "warnings": [],
        "errors": [],
        "groups": [
            {
                "rating_key": "900",
                "title": "Stacked Movie",
                "kind": "movie",
                "classification": "upgrade",
                "reclaimable_bytes": 8 * GiB,
                "keeper": {
                    "file": "/movies/keep.4k.mkv",
                    "size": 20 * GiB,
                    "resolution": "4k",
                    "media_id": 20,
                    "parts": [{"part_id": 1, "file": "/movies/keep.4k.mkv", "size": 20 * GiB}],
                },
                "copies": [
                    {
                        "file": "/movies/keep.4k.mkv",
                        "size": 20 * GiB,
                        "resolution": "4k",
                        "media_id": 20,
                        "parts": [{"part_id": 1, "file": "/movies/keep.4k.mkv", "size": 20 * GiB}],
                        "association": "untracked",
                        "arr_tracked": None,
                    },
                    {
                        "file": "/movies/old.1080.mkv",
                        "size": 8 * GiB,
                        "resolution": "1080",
                        "media_id": 21,
                        "parts": [{"part_id": 2, "file": "/movies/old.1080.mkv", "size": 8 * GiB}],
                        "association": "tracked",
                        "arr_tracked": "radarr",
                    },
                ],
            },
            {
                "rating_key": "200",
                "title": "TMNT",
                "kind": "movie",
                "classification": "mismatch",
                "reclaimable_bytes": 0,
                "keeper": None,
                "copies": [
                    {
                        "file": "/movies/TMNT (1990)/a.mkv",
                        "size": 5 * GiB,
                        "resolution": "1080",
                        "media_id": 3,
                        "parts": [{"part_id": 31, "file": "/movies/TMNT (1990)/a.mkv", "size": 5 * GiB}],
                    },
                    {
                        "file": "/movies/TMNT (2014)/b.mkv",
                        "size": 6 * GiB,
                        "resolution": "1080",
                        "media_id": 4,
                        "parts": [{"part_id": 41, "file": "/movies/TMNT (2014)/b.mkv", "size": 6 * GiB}],
                    },
                ],
            },
        ],
    }
    payload.update(overrides)
    return payload


@contextmanager
def _serve(provider):
    server = DuplicateReportServer("127.0.0.1", 0, DuplicateReportViewer(provider))
    server.start_background()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers, resp.read()


class ReportViewerTests(unittest.TestCase):
    def test_renders_report_sections(self) -> None:
        with _serve(lambda: _payload()) as base:
            status, headers, body = _get(base + "/")
            html = body.decode("utf-8")

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        # totals
        self.assertIn("Duplicate groups", html)
        self.assertIn("8.0 GiB", html)
        # reclaimable section with the group + its tracked redundant copy
        self.assertIn("Reclaimable (safe)", html)
        self.assertIn("Stacked Movie", html)
        self.assertIn("radarr", html)  # arr tag on the tracked copy
        # mismatch review section
        self.assertIn("possible mismatches", html)
        self.assertIn("TMNT", html)
        # arr-tracked section is populated (not the "Not configured" hint)
        self.assertIn("*arr-tracked", html)
        self.assertNotIn("Not configured", html)

    def test_generated_at_stamp_shown(self) -> None:
        with _serve(lambda: _payload()) as base:
            _, _, body = _get(base + "/")
        # 1_720_000_000 -> 2024-07-03 ... UTC; assert the date + UTC label render.
        self.assertIn("2024-07-03", body.decode("utf-8"))
        self.assertIn("UTC", body.decode("utf-8"))

    def test_api_returns_wrapped_report_with_delete_target_keys(self) -> None:
        with _serve(lambda: _payload()) as base:
            status, headers, body = _get(base + "/api/report")
            data = json.loads(body)

        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        self.assertTrue(data["available"])
        self.assertEqual(len(data["report"]["groups"]), 2)
        # the Phase-2 delete-target keys survive the round trip
        copy = data["report"]["groups"][0]["copies"][1]
        self.assertEqual(copy["media_id"], 21)
        self.assertEqual(copy["parts"][0]["part_id"], 2)

    def test_arr_not_configured_hint(self) -> None:
        payload = _payload(arr_enabled=False)
        # strip association fields to mimic a Plex-only report
        for group in payload["groups"]:
            for copy in group["copies"]:
                copy.pop("association", None)
                copy.pop("arr_tracked", None)
        with _serve(lambda: payload) as base:
            _, _, body = _get(base + "/")
        self.assertIn("Not configured", body.decode("utf-8"))

    def test_arr_unknown_candidate_is_not_called_safe(self) -> None:
        # arr ran but a reclaim candidate is `unknown` (an *arr outage, or a TV
        # copy whose filename didn't match). No copy is `tracked`, so the section
        # has no rows — but it must warn to verify, never call the copies "safe".
        payload = _payload()
        payload["totals"]["arr_tracked_reclaimable_count"] = 0
        payload["groups"][0]["copies"][1]["association"] = "unknown"
        payload["groups"][0]["copies"][1]["arr_tracked"] = None
        with _serve(lambda: payload) as base:
            _, _, body = _get(base + "/")
        html = body.decode("utf-8")
        self.assertIn("verify those before deleting", html)
        self.assertNotIn("not managed by Radarr/Sonarr", html)

    def test_keeper_derived_from_payload_not_copy_order(self) -> None:
        # The keeper is read from the authoritative `keeper` field, so even if
        # `copies` were not keeper-first the view labels the right keeper and never
        # lists it among the redundant copies.
        keeper = {
            "file": "/m/keep.4k.mkv", "size": 20 * GiB, "resolution": "4k",
            "media_id": 20, "parts": [{"part_id": 1, "file": "/m/keep.4k.mkv", "size": 20 * GiB}],
            "association": "untracked", "arr_tracked": None,
        }
        redundant = {
            "file": "/m/old.1080.mkv", "size": 8 * GiB, "resolution": "1080",
            "media_id": 21, "parts": [{"part_id": 2, "file": "/m/old.1080.mkv", "size": 8 * GiB}],
            "association": "untracked", "arr_tracked": None,
        }
        payload = _payload(groups=[{
            "rating_key": "900", "title": "Reordered", "kind": "movie",
            "classification": "upgrade", "reclaimable_bytes": 8 * GiB,
            "keeper": keeper,
            "copies": [redundant, keeper],  # deliberately NOT keeper-first
        }], arr_enabled=False)
        with _serve(lambda: payload) as base:
            _, _, body = _get(base + "/")
        html = body.decode("utf-8")
        self.assertIn("keep 4k", html)  # keeper resolution from the keeper field
        # the keeper file is not listed as a redundant copy in the reclaimable row
        reclaim_section = html.split("Review")[0]
        self.assertNotIn("keep.4k.mkv", reclaim_section)
        self.assertIn("old.1080.mkv", reclaim_section)


class EmptyAndDegradedStateTests(unittest.TestCase):
    def test_no_report_is_empty_state_not_500(self) -> None:
        with _serve(lambda: None) as base:
            status, _, body = _get(base + "/")
            api_status, _, api_body = _get(base + "/api/report")

        self.assertEqual(status, 200)
        self.assertIn("No report available", body.decode("utf-8"))
        self.assertEqual(api_status, 200)
        data = json.loads(api_body)
        self.assertFalse(data["available"])
        self.assertIsNone(data["report"])

    def test_zero_groups_renders_nothing_to_reclaim(self) -> None:
        payload = _payload(groups=[], totals={"duplicate_group_count": 0})
        with _serve(lambda: payload) as base:
            status, _, body = _get(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("No duplicate media found", body.decode("utf-8"))

    def test_report_without_delete_target_keys_still_renders(self) -> None:
        # A report written before #34 lacks media_id/part_id; the viewer must
        # tolerate that (read via .get) and still render, never 500.
        payload = _payload()
        for group in payload["groups"]:
            for copy in group["copies"]:
                copy.pop("media_id", None)
                for part in copy["parts"]:
                    part.pop("part_id", None)
        with _serve(lambda: payload) as base:
            status, _, body = _get(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("Stacked Movie", body.decode("utf-8"))

    def test_file_provider_degrades_on_missing_and_malformed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "nope.json"
            self.assertIsNone(file_report_provider(missing)())

            garbage = Path(tmpdir) / "garbage.json"
            garbage.write_text("{not valid json", encoding="utf-8")
            self.assertIsNone(file_report_provider(garbage)())

            not_object = Path(tmpdir) / "list.json"
            not_object.write_text("[1, 2, 3]", encoding="utf-8")
            self.assertIsNone(file_report_provider(not_object)())

            valid = Path(tmpdir) / "ok.json"
            valid.write_text(json.dumps({"groups": []}), encoding="utf-8")
            self.assertEqual(file_report_provider(valid)(), {"groups": []})

    def test_truncated_report_file_shows_empty_state(self) -> None:
        # Simulates a reader that raced a non-atomic writer: half a JSON doc.
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "plex-duplicates.json"
            path.write_text('{"groups": [{"title": "half', encoding="utf-8")
            with _serve(file_report_provider(path)) as base:
                status, _, body = _get(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("No report available", body.decode("utf-8"))

    def test_structurally_malformed_report_degrades_not_500(self) -> None:
        # Valid JSON object at the top level but wrong nested shapes. The
        # guarantee is a normal 200 response — never a dropped connection or a
        # 500 — for both the HTML and JSON surfaces.
        for bad in ({"groups": ["oops"]}, {"groups": {"a": 1}}, {"groups": [{"copies": "x"}]}):
            with _serve(lambda bad=bad: bad) as base:
                status, _, _ = _get(base + "/")
                api_status, _, _ = _get(base + "/api/report")
            self.assertEqual(status, 200, bad)
            self.assertEqual(api_status, 200, bad)

    def test_renderer_crash_falls_back_to_empty_state(self) -> None:
        # A report whose shape makes the renderer raise (a group that is a string)
        # degrades to the empty-state page rather than dropping the connection.
        with _serve(lambda: {"groups": ["oops"]}) as base:
            status, _, body = _get(base + "/")
        self.assertEqual(status, 200)
        self.assertIn("No report available", body.decode("utf-8"))

    def test_provider_exception_degrades(self) -> None:
        def boom():
            raise RuntimeError("disk on fire")

        with _serve(boom) as base:
            status, _, body = _get(base + "/")
            api_status, _, api_body = _get(base + "/api/report")
        self.assertEqual(status, 200)
        self.assertIn("No report available", body.decode("utf-8"))
        self.assertFalse(json.loads(api_body)["available"])

    def test_file_provider_caches_until_mtime_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "r.json"
            path.write_text(json.dumps({"groups": [], "totals": {"a": 1}}), encoding="utf-8")
            provider = file_report_provider(path)
            self.assertEqual(provider()["totals"], {"a": 1})
            # Overwrite with new content and bump mtime; the cache must refresh.
            os.utime(path, None)
            path.write_text(json.dumps({"groups": [], "totals": {"a": 2}}), encoding="utf-8")
            future = path.stat().st_mtime + 5
            os.utime(path, (future, future))
            self.assertEqual(provider()["totals"], {"a": 2})


class SecurityAndRoutingTests(unittest.TestCase):
    def test_escapes_hostile_report_strings(self) -> None:
        payload = _payload(
            warnings=["<script>alert('xss')</script>"],
        )
        payload["groups"][0]["title"] = 'Evil <img src=x onerror="alert(1)">'
        payload["groups"][0]["copies"][1]["file"] = "/m/<b>bad</b>.mkv"
        with _serve(lambda: payload) as base:
            _, _, body = _get(base + "/")
        html = body.decode("utf-8")

        # no raw injection survives
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<b>bad</b>.mkv", html)
        # the escaped forms are present instead
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img src=x", html)

    def test_non_ascii_titles_render_as_utf8(self) -> None:
        payload = _payload()
        payload["groups"][0]["title"] = "Amélie 日本語"
        with _serve(lambda: payload) as base:
            _, headers, body = _get(base + "/")
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn("Amélie 日本語", body.decode("utf-8"))

    def test_security_headers_present(self) -> None:
        with _serve(lambda: _payload()) as base:
            _, headers, _ = _get(base + "/")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_unknown_path_is_404(self) -> None:
        with _serve(lambda: _payload()) as base:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                _get(base + "/secret")
        self.assertEqual(ctx.exception.code, 404)

    def test_mutating_verbs_are_405(self) -> None:
        with _serve(lambda: _payload()) as base:
            for method in ("POST", "PUT", "DELETE", "PATCH"):
                req = urllib.request.Request(base + "/", method=method, data=b"{}")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 405, method)
                self.assertEqual(ctx.exception.headers.get("Allow"), "GET, HEAD")

    def test_head_returns_headers_without_body(self) -> None:
        with _serve(lambda: _payload()) as base:
            for path in ("/", "/healthz"):
                req = urllib.request.Request(base + path, method="HEAD")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200, path)
                    self.assertEqual(resp.read(), b"", path)  # no body on HEAD
                    self.assertIn("Content-Type", resp.headers)

    def test_healthz(self) -> None:
        with _serve(lambda: None) as base:
            status, headers, body = _get(base + "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok\n")

    def test_concurrent_gets_all_succeed(self) -> None:
        with _serve(lambda: _payload()) as base:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: _get(base + "/")[0], range(24)))
        self.assertTrue(all(status == 200 for status in results))


class OriginPolicyTests(unittest.TestCase):
    """The pure CSRF/origin decision (#63), exercised as a full matrix."""

    def test_normalize_origin_drops_default_ports_and_lowercases(self) -> None:
        self.assertEqual(_normalize_origin("http://Host.Example:80"), "http://host.example")
        self.assertEqual(_normalize_origin("https://Host.Example:443"), "https://host.example")
        self.assertEqual(_normalize_origin("http://host:8080"), "http://host:8080")
        self.assertEqual(_normalize_origin("http://[::1]:8080"), "http://[::1]:8080")

    def test_normalize_origin_rejects_opaque_and_malformed(self) -> None:
        for bad in (None, "", "null", "not-a-url", "/relative/path"):
            self.assertIsNone(_normalize_origin(bad), bad)

    def test_normalize_origin_rejects_bad_port_without_raising(self) -> None:
        # urlparse(...).port raises ValueError on a bad port; a client controls this
        # header, so it must refuse (None), never crash the request thread.
        for bad in ("http://h:999999", "http://h:abc", "http://h:-1"):
            self.assertIsNone(_normalize_origin(bad), bad)

    def test_is_loopback_bind(self) -> None:
        for addr in ("127.0.0.1", "::1", "[::1]", "localhost", "127.5.5.5"):
            self.assertTrue(_is_loopback_bind(addr), addr)
        for addr in ("0.0.0.0", "::", "", "192.168.1.5", "media.example.com"):
            self.assertFalse(_is_loopback_bind(addr), addr)

    def _ok(self, **kw):
        base = dict(
            origin=None, referer=None, host="127.0.0.1:8080",
            allowed_origins=(), browser_path=True, require_browser_origin=False,
        )
        base.update(kw)
        return _request_origin_ok(**base)

    # -- JSON API (token-only friendly) -------------------------------------- #
    def test_json_no_origin_allowed(self) -> None:
        self.assertTrue(self._ok(browser_path=False, origin=None))

    def test_json_matching_origin_allowed(self) -> None:
        self.assertTrue(self._ok(browser_path=False, origin="http://127.0.0.1:8080"))

    def test_json_mismatched_origin_refused(self) -> None:
        self.assertFalse(self._ok(browser_path=False, origin="http://evil.example"))

    def test_json_no_origin_allowed_even_with_allowlist(self) -> None:
        # The JSON API stays token-only when it sends no Origin (curl), regardless of
        # a configured allow-list.
        self.assertTrue(self._ok(browser_path=False, origin=None,
                                 allowed_origins=("https://media.example.com",)))

    # -- Browser form on a loopback bind (unchanged default) ----------------- #
    def test_form_loopback_no_headers_allowed(self) -> None:
        self.assertTrue(self._ok(require_browser_origin=False, origin=None, referer=None))

    def test_form_loopback_mismatched_origin_still_refused(self) -> None:
        self.assertFalse(self._ok(require_browser_origin=False, origin="http://evil.example"))

    # -- Browser form on a non-loopback bind (hardened) ---------------------- #
    def test_form_nonloopback_missing_headers_refused(self) -> None:
        self.assertFalse(self._ok(require_browser_origin=True, origin=None, referer=None))

    def test_form_nonloopback_matching_origin_allowed(self) -> None:
        self.assertTrue(self._ok(require_browser_origin=True, origin="http://127.0.0.1:8080"))

    def test_form_nonloopback_referer_fallback(self) -> None:
        # No Origin, but a same-origin Referer satisfies the check; a cross-site
        # Referer does not.
        self.assertTrue(self._ok(require_browser_origin=True, origin=None,
                                 referer="http://127.0.0.1:8080/"))
        self.assertFalse(self._ok(require_browser_origin=True, origin=None,
                                  referer="http://evil.example/x"))

    def test_form_nonloopback_https_needs_allowlist(self) -> None:
        # Behind a TLS proxy the browser Origin is https but the server is http, so a
        # bare Host comparison is scheme-mismatched (refused) until the operator lists
        # the external origin.
        self.assertFalse(self._ok(require_browser_origin=True, host="media.example.com",
                                  origin="https://media.example.com"))
        self.assertTrue(self._ok(require_browser_origin=True, host="media.example.com",
                                 origin="https://media.example.com",
                                 allowed_origins=("https://media.example.com",)))

    def test_allowlist_rejects_unlisted_origin(self) -> None:
        self.assertFalse(self._ok(require_browser_origin=True,
                                  origin="http://127.0.0.1:8080",
                                  allowed_origins=("https://media.example.com",)))

    def test_normalized_allowlist_drops_and_warns_on_bad_entry(self) -> None:
        # A scheme-less entry can't be honored; it is dropped (collapsing toward the
        # weaker Host fallback) and the misconfiguration is logged, not silent.
        with self.assertLogs("unraid_cache_cleaner.web", level="WARNING") as logs:
            result = _normalized_allowed_origins(("media.example.com", "https://ok.example:443"))
        self.assertEqual(result, ("https://ok.example",))  # default https port normalized away
        self.assertTrue(any("WEB_ALLOWED_ORIGINS" in line for line in logs.output))

    def test_bad_port_origin_over_http_is_clean_refusal(self) -> None:
        # End-to-end: a hostile bad-port Origin refuses, never raising.
        self.assertFalse(self._ok(require_browser_origin=True, origin="http://h:999999"))


class HostPolicyTests(unittest.TestCase):
    """#67: the pure DNS-rebinding Host allow-list, so the whole matrix is unit-tested."""

    def test_host_only_strips_port_and_brackets(self) -> None:
        self.assertEqual(_host_only("192.168.1.5:8080"), "192.168.1.5")
        self.assertEqual(_host_only("media.example.com"), "media.example.com")
        self.assertEqual(_host_only("media.example.com:8443"), "media.example.com")
        self.assertEqual(_host_only("[::1]:8080"), "::1")
        self.assertEqual(_host_only("[fe80::1]"), "fe80::1")
        self.assertEqual(_host_only("::1"), "::1")          # bare IPv6 passed through whole
        self.assertIsNone(_host_only("[::1"))               # malformed → None (fail closed)

    def test_missing_or_blank_host_is_allowed(self) -> None:
        # A non-browser client (curl / HTTP-1.0 probe) is not a rebinding vector.
        self.assertTrue(_host_allowed(None, ()))
        self.assertTrue(_host_allowed("", ()))
        self.assertTrue(_host_allowed("   ", ()))

    def test_ip_literals_and_localhost_always_allowed(self) -> None:
        # An IP is reached with no DNS lookup, so it cannot be rebound — this is the
        # config-free direct-LAN-by-IP case. localhost resolves to loopback.
        for host in ("127.0.0.1:8080", "192.168.1.218:8080", "10.0.0.5", "[::1]:8080", "::1"):
            self.assertTrue(_host_allowed(host, ()), host)
        self.assertTrue(_host_allowed("localhost", ()))
        self.assertTrue(_host_allowed("LOCALHOST:8080", ()))

    def test_unlisted_hostname_is_refused(self) -> None:
        # The rebinding case: an unknown hostname is refused with no allow-list...
        self.assertFalse(_host_allowed("evil.example", ()))
        self.assertFalse(_host_allowed("tower.local:8080", ()))
        # ...and permitted only once listed (case-insensitive, port- and trailing-dot-agnostic).
        self.assertTrue(_host_allowed("evil.example", ("evil.example",)))
        self.assertTrue(_host_allowed("Media.Example:8443", ("media.example",)))
        self.assertTrue(_host_allowed("host.local.", ("host.local",)))

    def test_malformed_or_empty_parsed_host_fails_closed(self) -> None:
        self.assertFalse(_host_allowed("[::1", ()))     # unclosed bracket
        self.assertFalse(_host_allowed(":8080", ()))    # empty host part
        self.assertFalse(_host_allowed("user@evil.example", ("evil.example",)))  # userinfo

    def test_effective_allowed_hosts_normalizes_and_folds_origins(self) -> None:
        # WEB_ALLOWED_HOSTS entries (port stripped, lowercased, de-duped) plus the
        # hostnames of the normalized WEB_ALLOWED_ORIGINS — so an existing #63 proxy
        # setup is not locked out by the new Host check.
        result = _effective_allowed_hosts(
            ("Media.Example:8443", "host.local.", "media.example"),
            ("https://proxy.example", "http://media.example"),
        )
        self.assertEqual(result, ("media.example", "host.local", "proxy.example"))


def _get_host(url: str, host: str, *, method: str = "GET"):
    """A request that forwards ``url``'s socket but forges the ``Host`` header — the
    exact shape of a DNS-rebinding request (the browser reaches the LAN IP but sends the
    attacker's rebound hostname)."""

    req = urllib.request.Request(url, method=method, headers={"Host": host})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@contextmanager
def _serve_hosts(provider, allowed_hosts=()):
    server = DuplicateReportServer(
        "127.0.0.1", 0, DuplicateReportViewer(provider), allowed_hosts=allowed_hosts
    )
    server.start_background()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()


class HostHeaderHttpTests(unittest.TestCase):
    """#67: the Host gate driven over real HTTP against the read surface (every route
    trusts the client Host for nothing but this allow-list, applied before routing)."""

    def test_ip_host_is_served(self) -> None:
        # urllib's default Host is 127.0.0.1:<port> (an IP literal) → served.
        with _serve_hosts(lambda: _payload()) as base:
            status, _, body = _get(base + "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Plex duplicate report", body)

    def test_rebinding_hostname_is_refused_even_with_matching_origin(self) -> None:
        # The classic rebind: Origin AND Host are the same forged hostname, so the
        # origin check would pass — the Host allow-list is what refuses it.
        with _serve_hosts(lambda: _payload()) as base:
            status, body = _get_host(base + "/", "evil.example")
            self.assertEqual(status, 403)
            self.assertIn(b"host is not recognized", body)
            # The read APIs (which expose deleted paths) are refused the same way.
            self.assertEqual(_get_host(base + "/api/report", "evil.example")[0], 403)
            self.assertEqual(_get_host(base + "/api/actions", "evil.example")[0], 403)

    def test_head_with_bad_host_is_403_without_body(self) -> None:
        with _serve_hosts(lambda: _payload()) as base:
            status, body = _get_host(base + "/", "evil.example", method="HEAD")
        self.assertEqual(status, 403)
        self.assertEqual(body, b"")

    def test_listed_hostname_is_served(self) -> None:
        with _serve_hosts(lambda: _payload(), allowed_hosts=("media.example",)) as base:
            status, body = _get_host(base + "/", "media.example:8443")
        self.assertEqual(status, 200)
        self.assertIn(b"Plex duplicate report", body)

    def test_x_forwarded_host_never_grants_access(self) -> None:
        # Only the real Host header is consulted; a spoofable X-Forwarded-* is ignored.
        with _serve_hosts(lambda: _payload()) as base:
            req = urllib.request.Request(
                base + "/",
                headers={"Host": "evil.example", "X-Forwarded-Host": "127.0.0.1"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
        self.assertEqual(status, 403)

    def test_mutating_verb_with_bad_host_is_403_not_405(self) -> None:
        # The Host gate precedes the 405 for unsupported verbs on the read-only viewer.
        with _serve_hosts(lambda: _payload()) as base:
            status, _ = _get_host(base + "/", "evil.example", method="DELETE")
        self.assertEqual(status, 403)


@contextmanager
def _serve_with_history(report, history):
    viewer = DuplicateReportViewer(lambda: report, action_history=history)
    server = DuplicateReportServer("127.0.0.1", 0, viewer)
    server.start_background()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()


def _action_row(**overrides):
    row = {
        "path": "/lib/old.mkv",
        "action": "web-reclaim:filesystem",
        "status": "deleted",
        "size": 5 * GiB,
        "message": "rating_key=900 part_id=2",
        "occurred_at": 1_720_000_000.0,
    }
    row.update(overrides)
    return row


class ActionHistoryViewerTests(unittest.TestCase):
    """The read-only ``/actions`` + ``/api/actions`` history views (#62)."""

    def test_render_none_is_unavailable_state(self) -> None:
        html = render_actions_html(None)
        self.assertIn("No action history is available", html)
        self.assertIn("WEB_ENABLE_ACTIONS", html)

    def test_render_empty_is_no_actions_state(self) -> None:
        html = render_actions_html([])
        self.assertIn("No reclaim actions have been recorded", html)

    def test_render_rows_are_a_newest_first_table(self) -> None:
        html = render_actions_html([_action_row(), _action_row(action="web-reclaim:radarr")])
        self.assertIn("Reclaim action history", html)
        self.assertIn("filesystem", html)
        self.assertIn("radarr", html)
        self.assertIn("5.0 GiB", html)
        self.assertIn("2024-07-03", html)  # occurred_at stamped as UTC
        self.assertIn("/lib/old.mkv", html)

    def test_error_row_does_not_claim_reclaimed_bytes(self) -> None:
        # An error row audits the target size, but nothing was freed — the Reclaimed
        # column must not show "N GiB" next to a failed delete.
        html = render_actions_html([
            _action_row(status="error", size=5 * GiB, message="permission denied")
        ])
        self.assertNotIn("5.0 GiB", html)
        self.assertIn("—", html)
        # A successful delete still shows its reclaimed size.
        ok = render_actions_html([_action_row(status="deleted", size=5 * GiB)])
        self.assertIn("5.0 GiB", ok)

    def test_render_escapes_hostile_path_and_message(self) -> None:
        html = render_actions_html([
            _action_row(path="/m/<script>x</script>.mkv", message='<img src=x onerror="a()">')
        ])
        self.assertNotIn("<script>x", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)

    def test_api_reports_unavailable_when_history_is_none(self) -> None:
        with _serve_with_history(None, lambda: None) as base:
            status, headers, body = _get(base + "/api/actions")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "application/json; charset=utf-8")
        data = json.loads(body)
        self.assertFalse(data["available"])
        self.assertEqual(data["actions"], [])

    def test_api_reports_rows_when_available(self) -> None:
        rows = [_action_row()]
        with _serve_with_history(None, lambda: rows) as base:
            status, _, body = _get(base + "/api/actions")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["available"])
        self.assertEqual(len(data["actions"]), 1)
        self.assertEqual(data["actions"][0]["action"], "web-reclaim:filesystem")

    def test_actions_page_renders(self) -> None:
        with _serve_with_history(None, lambda: [_action_row()]) as base:
            status, headers, body = _get(base + "/actions")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/html; charset=utf-8")
        self.assertIn(b"Reclaim action history", body)

    def test_report_page_links_to_history_when_wired(self) -> None:
        with _serve_with_history(_payload(), lambda: []) as base:
            _, _, body = _get(base + "/")
        self.assertIn('href="/actions"', body.decode("utf-8"))

    def test_report_page_has_no_history_link_without_provider(self) -> None:
        # The plain viewer (no action_history) does not advertise the history page.
        with _serve(lambda: _payload()) as base:
            _, _, report_body = _get(base + "/")
            api_status, _, api_body = _get(base + "/api/actions")
        self.assertNotIn('href="/actions"', report_body.decode("utf-8"))
        # And the routes still answer read-only, reporting unavailable.
        self.assertEqual(api_status, 200)
        self.assertFalse(json.loads(api_body)["available"])

    def test_provider_exception_degrades_to_unavailable(self) -> None:
        def boom():
            raise RuntimeError("db locked")

        with _serve_with_history(None, boom) as base:
            page_status, _, page_body = _get(base + "/actions")
            api_status, _, api_body = _get(base + "/api/actions")
        self.assertEqual(page_status, 200)
        self.assertIn("No action history is available", page_body.decode("utf-8"))
        self.assertEqual(api_status, 200)
        self.assertFalse(json.loads(api_body)["available"])


class ConfirmPageRenderTests(unittest.TestCase):
    """The #62 confirmation page + the action-layer notice page (pure renderers)."""

    def _resp(self, results, *, dry_run=False):
        return ReclaimResponse(200, True, dry_run, "", results)

    def test_confirm_page_summarizes_and_carries_targets_without_a_token(self) -> None:
        results = [
            ReclaimResult("900", 2, "would-delete", "filesystem", "1 file(s) via filesystem", 5 * GiB),
        ]
        html = render_reclaim_confirm_html(self._resp(results), 1_720_000_000.0, dry_run=False)
        self.assertIn("Confirm reclaim", html)
        self.assertIn("You are about to delete", html)
        self.assertIn("5.0 GiB", html)
        # The confirm form posts to the destructive route, carrying the selection…
        self.assertIn('action="/actions/reclaim"', html)
        self.assertIn('name="target" value="900:2"', html)
        self.assertIn('name="report_generated_at" value="1720000000.0"', html)
        # …but never a token field: the browser proves auth with the session cookie.
        self.assertNotIn('name="token"', html)

    def test_confirm_page_shows_refusals_and_no_form_when_nothing_deletable(self) -> None:
        results = [ReclaimResult("900", 1, "refused", "", "target is the group keeper; never deleted")]
        html = render_reclaim_confirm_html(self._resp(results), 1_720_000_000.0, dry_run=False)
        self.assertIn("Nothing selected would be deleted", html)
        self.assertIn("keeper", html)
        self.assertNotIn('action="/actions/reclaim"', html)  # no confirm form to submit

    def test_confirm_page_flags_dry_run(self) -> None:
        results = [ReclaimResult("900", 2, "would-delete", "filesystem", "x", 5 * GiB)]
        html = render_reclaim_confirm_html(self._resp(results, dry_run=True), 1_720_000_000.0, dry_run=True)
        self.assertIn("Dry-run mode", html)
        self.assertIn("removes nothing", html)

    def test_confirm_page_reflects_configured_mode_not_forced_preview(self) -> None:
        # The preview response is ALWAYS force-dry-run (response.dry_run == True), but
        # the page must reflect the *configured* reclaim mode: in live mode it must not
        # claim Confirm "removes nothing" when Confirm performs a real delete.
        results = [ReclaimResult("900", 2, "would-delete", "filesystem", "x", 5 * GiB)]
        forced_preview = self._resp(results, dry_run=True)  # what preview() always returns
        html = render_reclaim_confirm_html(forced_preview, 1_720_000_000.0, dry_run=False)
        self.assertNotIn("removes nothing", html)
        self.assertNotIn("Dry-run mode", html)
        self.assertIn("permanently deletes", html)
        self.assertIn("Live mode", html)

    def test_confirm_page_escapes_hostile_message(self) -> None:
        results = [ReclaimResult("900", 2, "refused", "", "<script>alert(1)</script>")]
        html = render_reclaim_confirm_html(self._resp(results), 1_720_000_000.0, dry_run=False)
        self.assertNotIn("<script>alert", html)
        self.assertIn("&lt;script&gt;", html)

    def test_notice_page_escapes_message_and_links_back(self) -> None:
        html = render_reclaim_notice_html("Reclaim refused", "invalid or missing action token")
        self.assertIn("Reclaim refused", html)
        self.assertIn("invalid or missing action token", html)
        self.assertIn('href="/"', html)


class RenderPureFunctionTests(unittest.TestCase):
    def test_render_none_is_empty_state(self) -> None:
        html = render_report_html(None)
        self.assertIn("No report available", html)
        self.assertIn("plex-duplicates", html)

    def test_build_server_uses_config_report_path_provider(self) -> None:
        # build_server with no injected provider wires the file provider at the
        # config's report path; a missing file yields the empty state (no crash).
        class _Cfg:
            web_bind_address = "127.0.0.1"
            web_port = 0
            plex_duplicate_report_path = Path("/nonexistent/does-not-exist.json")
            state_db_path = Path("/nonexistent/state.sqlite3")
            web_allowed_origins = ()
            web_allowed_hosts = ()
            web_action_history_auth = False

        server = build_server(_Cfg())
        server.start_background()
        try:
            base = f"http://127.0.0.1:{server.port}"
            status, _, body = _get(base + "/")
        finally:
            server.shutdown()
        self.assertEqual(status, 200)
        self.assertIn("No report available", body.decode("utf-8"))


class BindFailureTests(unittest.TestCase):
    def test_run_web_returns_clean_code_on_bind_error(self) -> None:
        # A bind failure (port in use, bad address) must surface as a clean exit
        # code, not a raw socket traceback (fail-closed, CLAUDE.md).
        from unittest import mock

        from unraid_cache_cleaner import cli

        class _Cfg:
            web_bind_address = "127.0.0.1"
            web_port = 9
            plex_duplicate_report_path = Path("/nonexistent/report.json")
            web_actions_enabled = False  # actions off → no reclaim service is built

        with mock.patch.object(cli.web, "build_server", side_effect=OSError("in use")):
            self.assertEqual(cli.run_web(_Cfg()), 3)

    def test_second_bind_on_same_port_raises_oserror(self) -> None:
        # Documents that DuplicateReportServer surfaces a bind conflict as OSError
        # (which run_web/run_cleaner catch), rather than hanging.
        first = DuplicateReportServer("127.0.0.1", 0, DuplicateReportViewer(lambda: None))
        try:
            with self.assertRaises(OSError):
                DuplicateReportServer("127.0.0.1", first.port, DuplicateReportViewer(lambda: None))
        finally:
            first.shutdown()


if __name__ == "__main__":
    unittest.main()
