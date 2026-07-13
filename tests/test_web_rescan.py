"""Tests for the report-regeneration service + cross-process lock (#77)."""

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.web_rescan import (  # noqa: E402
    RESCAN_ALREADY_RUNNING,
    RESCAN_STARTED,
    RESULT_FAILED,
    RESULT_SKIPPED,
    RESULT_SUCCEEDED,
    ReportRescanService,
    report_generation_lock,
    report_generation_lock_path,
)

_SYNC = lambda target: target()  # noqa: E731 — run the worker inline for deterministic tests


class RescanServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self._tmp.name) / "report.json.rescan.lock"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _service(self, regenerate, *, spawn=_SYNC, clock=None):
        kw = {"spawn": spawn}
        if clock is not None:
            kw["clock"] = clock
        return ReportRescanService(regenerate, self.lock_path, **kw)

    def test_idle_status_before_any_run(self) -> None:
        svc = self._service(lambda: None)
        status = svc.status()
        self.assertFalse(status.running)
        self.assertIsNone(status.last_status)
        self.assertIsNone(status.finished_at)

    def test_successful_run_records_succeeded(self) -> None:
        calls = []
        svc = self._service(lambda: calls.append("ran"))
        self.assertEqual(svc.trigger(), RESCAN_STARTED)  # _SYNC runs it inline
        self.assertEqual(calls, ["ran"])
        status = svc.status()
        self.assertFalse(status.running)
        self.assertEqual(status.last_status, RESULT_SUCCEEDED)
        self.assertIsNotNone(status.finished_at)

    def test_failed_run_records_failed_and_message(self) -> None:
        def boom():
            raise RuntimeError("plex unreachable")

        svc = self._service(boom)
        svc.trigger()
        status = svc.status()
        self.assertEqual(status.last_status, RESULT_FAILED)
        self.assertIn("plex unreachable", status.last_message)
        self.assertFalse(status.running)

    def test_in_process_single_flight(self) -> None:
        # A run in flight makes a second trigger report already-running, not a 2nd run.
        started = threading.Event()
        release = threading.Event()
        ran = []

        def regen():
            ran.append("x")
            started.set()
            self.assertTrue(release.wait(5))

        threads = []
        svc = self._service(
            regen,
            spawn=lambda t: threads.append(threading.Thread(target=t, daemon=True)) or threads[-1].start(),
        )
        self.assertEqual(svc.trigger(), RESCAN_STARTED)
        self.assertTrue(started.wait(2))
        self.assertTrue(svc.status().running)
        self.assertEqual(svc.trigger(), RESCAN_ALREADY_RUNNING)  # single-flight
        release.set()
        threads[0].join(5)
        self.assertEqual(ran, ["x"])  # regenerate ran exactly once
        self.assertEqual(svc.status().last_status, RESULT_SUCCEEDED)

    def test_trigger_is_non_blocking(self) -> None:
        # trigger() returns before a slow regeneration finishes (it does NOT run inline).
        release = threading.Event()
        finished = threading.Event()

        def regen():
            self.assertTrue(release.wait(5))
            finished.set()

        threads = []
        svc = self._service(
            regen,
            spawn=lambda t: threads.append(threading.Thread(target=t, daemon=True)) or threads[-1].start(),
        )
        self.assertEqual(svc.trigger(), RESCAN_STARTED)
        # The worker is still blocked in regenerate; trigger already returned.
        self.assertFalse(finished.is_set())
        self.assertTrue(svc.status().running)
        release.set()
        threads[0].join(5)
        self.assertFalse(svc.status().running)

    def test_spawn_failure_resets_running_flag(self) -> None:
        # A worker-spawn failure must not wedge the service "running" forever.
        def bad_spawn(_target):
            raise RuntimeError("cannot start thread")

        svc = self._service(lambda: None, spawn=bad_spawn)
        with self.assertRaises(RuntimeError):
            svc.trigger()
        self.assertFalse(svc.status().running)  # reset, not wedged
        # A later trigger with a working spawn succeeds.
        svc._spawn = _SYNC
        self.assertEqual(svc.trigger(), RESCAN_STARTED)

    def test_failed_regeneration_retains_prior_report(self) -> None:
        # The prior report on disk must survive a failed regeneration untouched — the
        # production regenerate generates THEN atomically writes, so a generate failure
        # never reaches the write. Modeled here by a regenerate that raises before writing.
        report = Path(self._tmp.name) / "report.json"
        report.write_text('{"generated_at": 1}', encoding="utf-8")

        def regen():
            raise RuntimeError("plex down")  # nothing written to `report`

        svc = self._service(regen)
        svc.trigger()
        self.assertEqual(svc.status().last_status, RESULT_FAILED)
        self.assertEqual(report.read_text(encoding="utf-8"), '{"generated_at": 1}')

    def test_cross_process_lock_busy_records_skipped(self) -> None:
        # While another holder has the cross-process lock, the run skips (never a 2nd
        # fan-out) and records SKIPPED rather than regenerating.
        ran = []
        svc = self._service(lambda: ran.append("x"))
        with report_generation_lock(self.lock_path) as acquired:
            self.assertTrue(acquired)  # the "other process" holds it
            svc.trigger()  # _SYNC runs the worker inline while the lock is held
        self.assertEqual(ran, [])  # regeneration skipped, not run
        self.assertEqual(svc.status().last_status, RESULT_SKIPPED)


class ReportGenerationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_path = Path(self._tmp.name) / "report.json.rescan.lock"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lock_path_is_a_stable_sidecar(self) -> None:
        report = Path("/config/plex-duplicates.json")
        self.assertEqual(
            report_generation_lock_path(report),
            Path("/config/plex-duplicates.json.rescan.lock"),
        )

    def test_second_holder_sees_busy_then_released(self) -> None:
        with report_generation_lock(self.lock_path) as first:
            self.assertTrue(first)
            with report_generation_lock(self.lock_path) as second:
                self.assertFalse(second)  # busy while the first holds it
        # Released now: a fresh acquire succeeds.
        with report_generation_lock(self.lock_path) as third:
            self.assertTrue(third)


if __name__ == "__main__":
    unittest.main()
