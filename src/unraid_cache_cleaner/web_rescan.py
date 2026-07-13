"""Report regeneration for the web action layer (#77).

The read-only viewer (``web.py``) never fans out to Plex on a GET — a page load only
reads the on-disk snapshot the ``plex-duplicates`` subcommand (or a cron) already
wrote. Rescan is the deliberate exception: an authorized, POST-only trigger that
regenerates *our* duplicate report from the browser instead of waiting for the CLI.

This module owns the regeneration envelope, kept small and fail-safe:

* **Non-blocking.** Report generation fans out to Plex/``*arr`` and can take minutes, so
  :meth:`ReportRescanService.trigger` never runs it inline — it hands the work to a
  background daemon thread and returns immediately, so an HTTP worker thread is never
  pinned and the operator is not left staring at a spinner. The page polls
  :meth:`ReportRescanService.status` (a pure in-memory read — never Plex) for progress.
* **Single-flight — two layers.** An in-process lock/flag makes concurrent browser
  clicks (or two request threads) collapse to one run — a second trigger returns
  ``already-running`` rather than fanning out twice. Across *processes* (the cron
  ``plex-duplicates`` path is a separate process, even a separate container), an
  advisory ``flock`` on a stable sidecar lock file (:func:`report_generation_lock`,
  reused by the CLI) means a web rescan and an overlapping cron run never both
  regenerate — the loser records ``skipped`` instead.
* **Failure-retention.** The injected ``regenerate`` reuses the reporter's
  generate→atomic-write sequence, which only ``os.replace``\\s the report once the new
  one is fully written, so a failed regeneration leaves the previous snapshot exactly in
  place (never an empty or half-written report). A raised regeneration is recorded as a
  ``failed`` run; the report on disk is untouched.
* **Lazy Plex.** The ``regenerate`` callable constructs its Plex/``*arr`` clients per run
  (built by the CLI), so no client is created until a rescan actually runs — preserving
  the credential-less read-only viewer deployment and the "a GET never reaches Plex"
  guarantee.

The service holds no report state and no reclaim lock — it is deliberately independent
of :class:`~unraid_cache_cleaner.web_actions.ReclaimService`, so a minutes-long scan
never blocks a reclaim request thread (authorization is still the same shared token/
unlock gate, applied by the web layer before ``trigger`` is called).
"""

from __future__ import annotations

import errno
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Optional

try:  # POSIX only (Linux deploy target + macOS dev); absent on Windows.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

#: The work a rescan performs: generate the duplicate report and atomically publish it
#: (and log a summary). Injected so a test drives it without a real Plex socket; the CLI
#: wires the production closure that builds fresh Plex/``*arr`` clients per run.
Regenerate = Callable[[], None]

#: How :meth:`ReportRescanService.trigger` runs the regeneration. The default launches a
#: daemon thread (non-blocking); a test may inject a synchronous runner for determinism.
Spawn = Callable[[Callable[[], None]], None]

#: :meth:`ReportRescanService.trigger` outcomes.
RESCAN_STARTED = "started"
RESCAN_ALREADY_RUNNING = "already-running"

#: :attr:`RescanStatus.last_status` values — the result of the most recent finished run.
RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"
RESULT_SKIPPED = "skipped"  # another process held the cross-process regeneration lock

#: Suffix of the sidecar lock file, colocated with the report but a *distinct*, stable
#: inode — the report itself is swapped by ``os.replace`` on every publish, so an
#: ``flock`` on it would be lost the moment a report is written.
_LOCK_SUFFIX = ".rescan.lock"


def report_generation_lock_path(report_path: Path) -> Path:
    """The sidecar lock file guarding report regeneration for ``report_path``."""

    return report_path.with_name(report_path.name + _LOCK_SUFFIX)


@contextmanager
def report_generation_lock(lock_path: Path) -> Iterator[bool]:
    """Hold the cross-process report-regeneration lock for the ``with`` body.

    Yields ``True`` when this process acquired the exclusive advisory lock (or when
    cross-process locking is unavailable — see below — so the body still runs), and
    ``False`` when another process already holds it (the caller should skip, not
    regenerate). Non-blocking: it never waits on the lock.

    The lock is an ``flock`` on a stable sidecar file, so it is released automatically
    when the fd closes — including on process crash — which means there is never a stale
    lock to clear (unlike a pid file). If ``flock`` is unavailable (no ``fcntl`` module,
    or a mount that rejects it — some network filesystems do), this degrades to
    *acquired* with a debug note rather than blocking regeneration: the in-process
    single-flight still holds, and the worst case is a rare duplicated fan-out that the
    atomic writer makes harmless (no partial/corrupt report).
    """

    if fcntl is None:  # pragma: no cover - non-POSIX
        LOGGER.debug("fcntl unavailable; skipping cross-process rescan lock")
        yield True
        return
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        # Can't even create the sidecar (unwritable dir): degrade rather than block.
        LOGGER.debug("could not open rescan lock %s (%s); proceeding unlocked", lock_path, exc)
        yield True
        return
    acquired = False
    degraded = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                yield False  # another process holds it — caller skips
                return
            # ENOTSUP/EINVAL etc.: the mount doesn't support flock — degrade to acquired.
            LOGGER.debug("flock unsupported on %s (%s); proceeding unlocked", lock_path, exc)
            degraded = True
            yield True
            return
        yield True
    finally:
        if acquired and not degraded:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - closing the fd releases the lock anyway
                pass
        os.close(fd)


class RescanStatus:
    """An immutable snapshot of the rescan state, safe to serialize to JSON."""

    __slots__ = ("running", "last_status", "last_message", "started_at", "finished_at")

    def __init__(
        self,
        *,
        running: bool,
        last_status: Optional[str],
        last_message: str,
        started_at: Optional[float],
        finished_at: Optional[float],
    ) -> None:
        self.running = running
        self.last_status = last_status
        self.last_message = last_message
        self.started_at = started_at
        self.finished_at = finished_at

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "last_status": self.last_status,
            "last_message": self.last_message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class ReportRescanService:
    """Single-flight, non-blocking, failure-retaining report regeneration (#77)."""

    def __init__(
        self,
        regenerate: Regenerate,
        lock_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        spawn: Optional[Spawn] = None,
    ) -> None:
        self._regenerate = regenerate
        self._lock_path = lock_path
        self._clock = clock
        self._spawn = spawn or _daemon_spawn
        self._lock = threading.Lock()
        self._running = False
        self._last_status: Optional[str] = None
        self._last_message = ""
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None

    def trigger(self) -> str:
        """Start a regeneration in the background, or report one already in flight.

        Returns :data:`RESCAN_STARTED` when a run was launched, or
        :data:`RESCAN_ALREADY_RUNNING` when this process is already regenerating (the
        in-process single-flight). Never blocks on the scan itself — the work runs on a
        background thread; a cross-process collision (an overlapping cron run) is detected
        inside that thread and recorded as :data:`RESULT_SKIPPED`."""

        with self._lock:
            if self._running:
                return RESCAN_ALREADY_RUNNING
            self._running = True
            self._started_at = self._clock()
        self._spawn(self._run)
        return RESCAN_STARTED

    def status(self) -> RescanStatus:
        """A snapshot of the current state (pure in-memory read — never touches Plex)."""

        with self._lock:
            return RescanStatus(
                running=self._running,
                last_status=self._last_status,
                last_message=self._last_message,
                started_at=self._started_at,
                finished_at=self._finished_at,
            )

    def _run(self) -> None:
        status = RESULT_SUCCEEDED
        message = "report regenerated"
        try:
            with report_generation_lock(self._lock_path) as acquired:
                if not acquired:
                    status = RESULT_SKIPPED
                    message = "another report regeneration is already in progress"
                    LOGGER.info("web rescan: %s; skipping", message)
                else:
                    self._regenerate()
                    LOGGER.info("web rescan: report regenerated")
        except Exception as exc:  # noqa: BLE001 — a failed scan must retain the prior report
            status = RESULT_FAILED
            message = f"report regeneration failed: {exc}"
            LOGGER.warning("web rescan: report regeneration failed", exc_info=True)
        finally:
            with self._lock:
                self._running = False
                self._last_status = status
                self._last_message = message
                self._finished_at = self._clock()


def _daemon_spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="web-rescan", daemon=True).start()
