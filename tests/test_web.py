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
    build_server,
    file_report_provider,
    render_report_html,
)

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
                self.assertEqual(ctx.exception.headers.get("Allow"), "GET")

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

        server = build_server(_Cfg())
        server.start_background()
        try:
            base = f"http://127.0.0.1:{server.port}"
            status, _, body = _get(base + "/")
        finally:
            server.shutdown()
        self.assertEqual(status, 200)
        self.assertIn("No report available", body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
