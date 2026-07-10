"""Tests for the Plex duplicate web action layer (#34, Phase 2).

The reclaim path is the project's first outside-triggered mutation of media, so
these tests exercise the whole safety envelope with injected fakes (report
provider, filesystem deleter, Radarr/Sonarr clients, audit sink) and — for the
HTTP surface — the real ``ThreadingHTTPServer`` driven in-process over ``urllib``.
No real Plex/``*arr``/qBittorrent client is ever constructed; the only real disk
writes are ``tempfile`` sentinels used to prove the deleter did (or did not) run.
"""

from __future__ import annotations

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

from unraid_cache_cleaner.arr import ArrClientError
from unraid_cache_cleaner.config import Config
from unraid_cache_cleaner.web import (
    DuplicateReportServer,
    DuplicateReportViewer,
    build_server,
    render_report_html,
)
from unraid_cache_cleaner.web_actions import (
    STAGING_SUFFIX,
    ReclaimService,
    ReclaimTarget,
    build_action_index,
)

GiB = 1024 ** 3
GEN = 1_720_000_000.0


# --------------------------------------------------------------------------- #
# Fixtures / fakes                                                             #
# --------------------------------------------------------------------------- #

def _config(**overrides) -> Config:
    base = dict(
        qbittorrent_url="http://qbt:8080",
        qbittorrent_username="u",
        qbittorrent_password="p",
        qbittorrent_timeout_seconds=15,
        qbittorrent_verify_tls=True,
        watch_paths=(),
        poll_interval_seconds=300,
        orphan_grace_seconds=0,
        min_file_age_seconds=0,
        dry_run=False,
        delete_empty_dirs=True,
        protect_single_file_parent_dirs=True,
        excluded_globs=(),
        state_db_path=Path("/tmp/uc/state.sqlite3"),
        report_path=Path("/tmp/uc/last.json"),
        log_level="INFO",
        web_actions_enabled=True,
        web_actions_dry_run=False,
        web_action_token="tok",
        web_media_path_map=(),
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class _FakeDeleter:
    def __init__(self, *, fail: bool = False, fail_on_call: int | None = None) -> None:
        self.calls: list[Path] = []
        self._fail = fail
        self._fail_on_call = fail_on_call  # 1-based index of the call that raises

    def __call__(self, path: Path) -> None:
        self.calls.append(Path(path))
        if self._fail or self._fail_on_call == len(self.calls):
            raise OSError("permission denied")


class _PartialMover:
    """``os.rename`` for every call except the configured ones, which raise ``OSError``
    — to drive #64's stage-phase rollback. Rollback renames (which run after a forward
    failure) go through the same seam and are recorded, so ``fail_on_calls={2}`` fails
    only the 2nd forward rename while the later rollback rename succeeds."""

    def __init__(self, fail_on_calls) -> None:
        self.calls: list[tuple[Path, Path]] = []
        self._fail_on = set(fail_on_calls)
        self._n = 0

    def __call__(self, src: Path, dst: Path) -> None:
        self._n += 1
        self.calls.append((Path(src), Path(dst)))
        if self._n in self._fail_on:
            raise OSError("simulated rename failure")
        os.rename(src, dst)


class _FakeArr:
    """A fake Radarr/Sonarr client recording by-id lookups and delete calls.

    ``files`` maps ``{file id: current path}`` — the live ``*arr`` state a reclaim
    re-validates a report-serialized id against (#61). A ``get_*`` for an id absent
    from ``files`` raises ``ArrClientError(status_code=404)`` (the id is gone);
    ``get_calls`` proves each part costs exactly one by-id GET (no full-library
    fan-out).
    """

    def __init__(
        self, files=None, *, sizes=None, fail_delete=False, fail_delete_ids=None,
        fail_get=False,
    ) -> None:
        self._files = files or {}
        self._sizes = sizes or {}
        self._fail_delete = fail_delete
        self._fail_delete_ids = set(fail_delete_ids or ())
        self._fail_get = fail_get
        self.deleted: list[int] = []
        self.get_calls: list[int] = []

    def get_movie_file(self, file_id: int) -> dict:
        self.get_calls.append(file_id)
        if self._fail_get:
            raise ArrClientError("arr get failed", status_code=500)
        if file_id not in self._files:
            raise ArrClientError("not found", status_code=404)
        record = {"id": file_id, "path": self._files[file_id]}
        # A size is included only when the test supplies one, so existing cases
        # (no size) exercise basename-only drift and the size cross-check is opt-in.
        if file_id in self._sizes:
            record["size"] = self._sizes[file_id]
        return record

    get_episode_file = get_movie_file

    def delete_movie_file(self, file_id: int) -> None:
        if self._fail_delete or file_id in self._fail_delete_ids:
            raise ArrClientError("arr delete failed")
        self.deleted.append(file_id)

    delete_episode_file = delete_movie_file


class _FakeAudit:
    def __init__(self) -> None:
        self.records = []
        self.batches = []  # one entry per flush call — proves flush timing/batching

    def __call__(self, records, now) -> None:
        self.batches.append(list(records))
        self.records.extend(records)


def _keeper(file="/lib/keep.4k.mkv", size=20 * GiB, media_id=20, part_id=1):
    return {
        "file": file,
        "size": size,
        "resolution": "4k",
        "media_id": media_id,
        "parts": [{"part_id": part_id, "file": file, "size": size, "arr_file_id": None}],
        "association": "untracked",
        "arr_tracked": None,
    }


def _copy(
    file, size, *, media_id, association, arr_tracked=None, parts=None,
    resolution="1080", arr_file_id=None,
):
    parts = parts or [
        {"part_id": 2, "file": file, "size": size, "arr_file_id": arr_file_id}
    ]
    return {
        "file": file,
        "size": size,
        "resolution": resolution,
        "media_id": media_id,
        "parts": parts,
        "association": association,
        "arr_tracked": arr_tracked,
    }


def _group(copies, *, keeper, rating_key="900", kind="movie", classification="upgrade"):
    return {
        "rating_key": rating_key,
        "title": "A Movie",
        "kind": kind,
        "classification": classification,
        "reclaimable_bytes": 8 * GiB,
        "keeper": keeper,
        "copies": copies,
    }


def _report(groups, generated_at=GEN):
    return {"generated_at": generated_at, "arr_enabled": True, "groups": groups}


def _service(payload, **overrides):
    config = overrides.pop("config", None) or _config()
    return ReclaimService(
        config,
        lambda: payload,
        filesystem_deleter=overrides.pop("deleter", lambda p: None),
        filesystem_mover=overrides.pop("mover", os.rename),
        radarr=overrides.pop("radarr", None),
        sonarr=overrides.pop("sonarr", None),
        audit=overrides.pop("audit", None),
        clock=lambda: 123.0,
    )


def _reclaim(service, rating_key="900", part_id=2, *, token="tok", gen=GEN):
    return service.reclaim(
        [ReclaimTarget(rating_key, part_id)], token=token, report_generated_at=gen
    )


# --------------------------------------------------------------------------- #
# Gate refusals (disabled / token / stale)                                    #
# --------------------------------------------------------------------------- #

class GateTests(unittest.TestCase):
    def test_disabled_is_403_and_touches_nothing(self) -> None:
        deleter = _FakeDeleter()
        keeper = _keeper()
        payload = _report([_group([keeper, _copy("/lib/old.mkv", 8 * GiB, media_id=21, association="untracked")], keeper=keeper)])
        service = _service(payload, config=_config(web_actions_enabled=False), deleter=deleter)
        response = _reclaim(service)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.results, [])
        self.assertEqual(deleter.calls, [])

    def test_enabled_without_token_configured_refuses(self) -> None:
        payload = _report([])
        service = _service(payload, config=_config(web_action_token=""))
        response = _reclaim(service, token="anything")
        self.assertEqual(response.status_code, 403)
        self.assertIn("WEB_ACTION_TOKEN", response.message)

    def test_wrong_token_refuses(self) -> None:
        service = _service(_report([]))
        self.assertEqual(_reclaim(service, token="nope").status_code, 403)

    def test_missing_token_refuses(self) -> None:
        service = _service(_report([]))
        self.assertEqual(_reclaim(service, token=None).status_code, 403)

    def test_no_report_is_409(self) -> None:
        service = _service(None)
        self.assertEqual(_reclaim(service).status_code, 409)

    def test_stale_generation_is_409(self) -> None:
        keeper = _keeper()
        payload = _report([_group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="untracked")], keeper=keeper)])
        service = _service(payload)
        self.assertEqual(_reclaim(service, gen=GEN + 5).status_code, 409)

    def test_missing_generation_is_409(self) -> None:
        service = _service(_report([]))
        self.assertEqual(_reclaim(service, gen=None).status_code, 409)


# --------------------------------------------------------------------------- #
# Per-target safety refusals                                                   #
# --------------------------------------------------------------------------- #

class SafetyRefusalTests(unittest.TestCase):
    def _run(self, group, rating_key="900", part_id=2):
        deleter = _FakeDeleter()
        service = _service(_report([group]), deleter=deleter)
        response = _reclaim(service, rating_key, part_id)
        return response, deleter

    def test_refuses_keeper(self) -> None:
        keeper = _keeper(part_id=1)
        group = _group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="untracked")], keeper=keeper)
        response, deleter = self._run(group, part_id=1)  # part_id 1 is the keeper
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("keeper", response.results[0].message)
        self.assertEqual(deleter.calls, [])

    def test_refuses_mismatch_group(self) -> None:
        keeper = _keeper()
        group = _group(
            [keeper, _copy("/lib/old.mkv", 8, media_id=21, association="untracked")],
            keeper=keeper,
            classification="mismatch",
        )
        response, deleter = self._run(group)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("mismatch", response.results[0].message)
        self.assertEqual(deleter.calls, [])

    def test_refuses_unknown_association(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="unknown")], keeper=keeper)
        response, deleter = self._run(group)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("unknown", response.results[0].message)
        self.assertEqual(deleter.calls, [])

    def test_refuses_target_sharing_keeper_file_path(self) -> None:
        # Plex reports one physical file under two Media/Part ids: a non-keeper
        # sibling shares the keeper's path. It is not the keeper by identity, but
        # deleting it would destroy the keeper's file, so it must be refused.
        keeper = _keeper(file="/lib/same.mkv", size=20 * GiB, media_id=20, part_id=1)
        sibling = _copy(
            "/lib/same.mkv", 20 * GiB, media_id=99, association="untracked",
            parts=[{"part_id": 2, "file": "/lib/same.mkv", "size": 20 * GiB}],
        )
        group = _group([keeper, sibling], keeper=keeper)
        response, deleter = self._run(group, part_id=2)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("shares a file with the keeper", response.results[0].message)
        self.assertEqual(deleter.calls, [])

    def test_refuses_group_without_keeper(self) -> None:
        group = _group([_copy("/lib/old.mkv", 8, media_id=21, association="untracked")], keeper=None)
        response, deleter = self._run(group)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("keeper", response.results[0].message)

    def test_refuses_zero_part_id(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="untracked")], keeper=keeper)
        response, _ = self._run(group, part_id=0)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("invalid target id", response.results[0].message)

    def test_refuses_empty_rating_key(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="untracked")], keeper=keeper)
        response, _ = self._run(group, rating_key="")
        self.assertEqual(response.results[0].status, "refused")

    def test_refuses_unknown_target(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="untracked")], keeper=keeper)
        response, _ = self._run(group, part_id=999)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("not found", response.results[0].message)

    def test_duplicate_identity_in_report_is_ambiguous(self) -> None:
        # Two copies claim the same {rating_key, part_id} -> the identity is
        # ambiguous and refused rather than acting on the wrong copy.
        keeper = _keeper()
        dup_a = _copy("/lib/a.mkv", 8, media_id=21, association="untracked", parts=[{"part_id": 2, "file": "/lib/a.mkv", "size": 8}])
        dup_b = _copy("/lib/b.mkv", 8, media_id=22, association="untracked", parts=[{"part_id": 2, "file": "/lib/b.mkv", "size": 8}])
        group = _group([keeper, dup_a, dup_b], keeper=keeper)
        response, deleter = self._run(group)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("ambiguous", response.results[0].message)
        self.assertEqual(deleter.calls, [])

    def test_duplicate_targets_deduped(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="unknown")], keeper=keeper)
        service = _service(_report([group]))
        response = service.reclaim(
            [ReclaimTarget("900", 2), ReclaimTarget("900", 2)], token="tok", report_generated_at=GEN
        )
        self.assertEqual(len(response.results), 1)  # collapsed to one


# --------------------------------------------------------------------------- #
# Filesystem backend                                                          #
# --------------------------------------------------------------------------- #

class FilesystemTests(unittest.TestCase):
    def _fixture(self, tmp, rel="movie/old.mkv", size=5, plex_prefix="/plex"):
        media_root = Path(tmp) / "media"
        real = media_root / rel
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(b"x" * size)
        plex_path = f"{plex_prefix}/{rel}"
        keeper = _keeper()
        group = _group(
            [keeper, _copy(plex_path, size, media_id=21, association="untracked")],
            keeper=keeper,
        )
        config = _config(web_media_path_map=((Path(plex_prefix), media_root),))
        return real, group, config

    def test_untracked_routes_to_filesystem_and_audits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real, group, config = self._fixture(tmp)
            deleter = _FakeDeleter()
            audit = _FakeAudit()
            service = _service(_report([group]), config=config, deleter=deleter, audit=audit)
            response = _reclaim(service)
            self.assertEqual(response.results[0].status, "deleted")
            self.assertEqual(response.results[0].backend, "filesystem")
            # #64: the file is staged (renamed to a sibling) and the STAGED path is
            # unlinked; the audit still records the original media path.
            staged = real.with_name(real.name + STAGING_SUFFIX)
            self.assertEqual(deleter.calls, [staged])
            self.assertFalse(real.exists())  # renamed away from its media path
            self.assertEqual(len(audit.records), 1)
            self.assertEqual(audit.records[0].status, "deleted")
            self.assertEqual(Path(audit.records[0].path), real)

    def test_dry_run_reports_would_delete_and_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real, group, config = self._fixture(tmp)
            config = _config(
                web_media_path_map=config.web_media_path_map, web_actions_dry_run=True
            )
            deleter = _FakeDeleter()
            audit = _FakeAudit()
            service = _service(_report([group]), config=config, deleter=deleter, audit=audit)
            response = _reclaim(service)
            self.assertEqual(response.results[0].status, "would-delete")
            self.assertEqual(deleter.calls, [])       # nothing deleted
            self.assertEqual(audit.records, [])        # nothing audited
            self.assertTrue(real.exists())             # sentinel survives

    def test_real_unlink_removes_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real, group, config = self._fixture(tmp)
            service = _service(_report([group]), config=config, deleter=os.unlink)
            response = _reclaim(service)
            self.assertEqual(response.results[0].status, "deleted")
            self.assertFalse(real.exists())

    def test_unmapped_path_refused(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/plex/x.mkv", 5, media_id=21, association="untracked")], keeper=keeper)
        service = _service(_report([group]), config=_config(web_media_path_map=()))
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("WEB_MEDIA_PATH_MAP", response.results[0].message)

    def test_unmounted_root_refused(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/plex/x.mkv", 5, media_id=21, association="untracked")], keeper=keeper)
        config = _config(web_media_path_map=((Path("/plex"), Path("/no/such/root")),))
        service = _service(_report([group]), config=config)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("not mounted", response.results[0].message)

    def test_missing_file_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp) / "media"
            media_root.mkdir()
            keeper = _keeper()
            group = _group([keeper, _copy("/plex/gone.mkv", 5, media_id=21, association="untracked")], keeper=keeper)
            config = _config(web_media_path_map=((Path("/plex"), media_root),))
            response = _reclaim(_service(_report([group]), config=config))
            self.assertEqual(response.results[0].status, "refused")
            self.assertIn("not present", response.results[0].message)

    def test_size_mismatch_refused_toctou(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real, group, config = self._fixture(tmp, size=5)
            # Report claims a different size than what's on disk now.
            group["copies"][1]["size"] = 999
            group["copies"][1]["parts"][0]["size"] = 999
            deleter = _FakeDeleter()
            response = _reclaim(_service(_report([group]), config=config, deleter=deleter))
            self.assertEqual(response.results[0].status, "refused")
            self.assertIn("size changed", response.results[0].message)
            self.assertEqual(deleter.calls, [])
            self.assertTrue(real.exists())

    def test_symlink_target_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp) / "media"
            media_root.mkdir()
            outside = Path(tmp) / "outside.mkv"
            outside.write_bytes(b"xxxxx")
            link = media_root / "link.mkv"
            os.symlink(outside, link)
            keeper = _keeper()
            group = _group([keeper, _copy("/plex/link.mkv", 5, media_id=21, association="untracked")], keeper=keeper)
            config = _config(web_media_path_map=((Path("/plex"), media_root),))
            deleter = _FakeDeleter()
            response = _reclaim(_service(_report([group]), config=config, deleter=deleter))
            self.assertEqual(response.results[0].status, "refused")
            self.assertEqual(deleter.calls, [])
            self.assertTrue(outside.exists())  # the symlink target is untouched

    def test_symlinked_parent_escape_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp) / "media"
            media_root.mkdir()
            outside_dir = Path(tmp) / "outside"
            outside_dir.mkdir()
            (outside_dir / "old.mkv").write_bytes(b"xxxxx")
            os.symlink(outside_dir, media_root / "movie")  # media/movie -> ../outside
            keeper = _keeper()
            group = _group([keeper, _copy("/plex/movie/old.mkv", 5, media_id=21, association="untracked")], keeper=keeper)
            config = _config(web_media_path_map=((Path("/plex"), media_root),))
            deleter = _FakeDeleter()
            response = _reclaim(_service(_report([group]), config=config, deleter=deleter))
            self.assertEqual(response.results[0].status, "refused")
            self.assertIn("escapes", response.results[0].message)
            self.assertEqual(deleter.calls, [])

    def test_longest_prefix_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            specific = Path(tmp) / "specific"
            generic = Path(tmp) / "generic"
            (specific / "old.mkv").parent.mkdir(parents=True, exist_ok=True)
            target = specific / "old.mkv"
            target.write_bytes(b"xxxxx")
            keeper = _keeper()
            group = _group([keeper, _copy("/plex/tv/old.mkv", 5, media_id=21, association="untracked")], keeper=keeper)
            # /plex/tv (2 components after root) is more specific than /plex.
            config = _config(
                web_media_path_map=(
                    (Path("/plex"), generic),
                    (Path("/plex/tv"), specific),
                )
            )
            deleter = _FakeDeleter()
            response = _reclaim(_service(_report([group]), config=config, deleter=deleter))
            self.assertEqual(response.results[0].status, "deleted")
            self.assertEqual(deleter.calls, [target.with_name(target.name + STAGING_SUFFIX)])

    def test_component_aware_prefix_no_false_match(self) -> None:
        # /plex should NOT match a Plex path under /plexextra.
        keeper = _keeper()
        group = _group([keeper, _copy("/plexextra/old.mkv", 5, media_id=21, association="untracked")], keeper=keeper)
        config = _config(web_media_path_map=((Path("/plex"), Path("/media")),))
        response = _reclaim(_service(_report([group]), config=config))
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("WEB_MEDIA_PATH_MAP", response.results[0].message)

    def test_stacked_copy_all_parts_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp) / "media"
            (media_root / "movie").mkdir(parents=True)
            cd1 = media_root / "movie" / "cd1.mkv"
            cd2 = media_root / "movie" / "cd2.mkv"
            cd1.write_bytes(b"aaaaa")
            cd2.write_bytes(b"bbb")
            keeper = _keeper()
            stacked = _copy(
                "/plex/movie/cd1.mkv", 8, media_id=21, association="untracked",
                parts=[
                    {"part_id": 2, "file": "/plex/movie/cd1.mkv", "size": 5},
                    {"part_id": 3, "file": "/plex/movie/cd2.mkv", "size": 3},
                ],
            )
            group = _group([keeper, stacked], keeper=keeper)
            config = _config(web_media_path_map=((Path("/plex"), media_root),))
            audit = _FakeAudit()
            service = _service(_report([group]), config=config, deleter=os.unlink, audit=audit)
            response = _reclaim(service, part_id=2)
            self.assertEqual(response.results[0].status, "deleted")
            self.assertFalse(cd1.exists())
            self.assertFalse(cd2.exists())
            self.assertEqual(len(audit.records), 2)  # one audit row per part

    def test_stacked_copy_refused_atomically_if_one_part_bad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp) / "media"
            (media_root / "movie").mkdir(parents=True)
            cd1 = media_root / "movie" / "cd1.mkv"
            cd1.write_bytes(b"aaaaa")  # cd2 deliberately absent
            keeper = _keeper()
            stacked = _copy(
                "/plex/movie/cd1.mkv", 8, media_id=21, association="untracked",
                parts=[
                    {"part_id": 2, "file": "/plex/movie/cd1.mkv", "size": 5},
                    {"part_id": 3, "file": "/plex/movie/cd2.mkv", "size": 3},
                ],
            )
            group = _group([keeper, stacked], keeper=keeper)
            config = _config(web_media_path_map=((Path("/plex"), media_root),))
            service = _service(_report([group]), config=config, deleter=os.unlink)
            response = _reclaim(service, part_id=2)
            self.assertEqual(response.results[0].status, "refused")
            self.assertTrue(cd1.exists())  # nothing deleted — refused whole

    def _two_part_stacked(self, tmp):
        """A two-part untracked stacked copy on disk (cd1=5B, cd2=3B) + report."""
        media_root = Path(tmp) / "media"
        (media_root / "movie").mkdir(parents=True)
        cd1 = media_root / "movie" / "cd1.mkv"
        cd2 = media_root / "movie" / "cd2.mkv"
        cd1.write_bytes(b"aaaaa")
        cd2.write_bytes(b"bbb")
        keeper = _keeper()
        stacked = _copy(
            "/plex/movie/cd1.mkv", 8, media_id=21, association="untracked",
            parts=[
                {"part_id": 2, "file": "/plex/movie/cd1.mkv", "size": 5},
                {"part_id": 3, "file": "/plex/movie/cd2.mkv", "size": 3},
            ],
        )
        group = _group([keeper, stacked], keeper=keeper)
        config = _config(web_media_path_map=((Path("/plex"), media_root),))
        return media_root, cd1, cd2, group, config

    def test_stacked_stage_failure_rolls_back_leaving_all_parts_intact(self) -> None:
        # #64 core guarantee: if the SECOND part fails to stage, the first part's
        # staging rename is rolled back — both parts stay intact at their original
        # paths, nothing is unlinked, and the result is an error with zero bytes freed.
        with tempfile.TemporaryDirectory() as tmp:
            media_root, cd1, cd2, group, config = self._two_part_stacked(tmp)
            mover = _PartialMover(fail_on_calls={2})  # 2nd forward rename (cd2) fails
            deleter = _FakeDeleter()
            audit = _FakeAudit()
            service = _service(
                _report([group]), config=config, deleter=deleter, mover=mover, audit=audit
            )
            result = _reclaim(service, part_id=2).results[0]
            self.assertEqual(result.status, "error")
            self.assertEqual(result.reclaimed_bytes, 0)   # nothing freed
            self.assertIn("rolled back, nothing deleted", result.message)
            self.assertEqual(deleter.calls, [])           # no unlink ran
            self.assertTrue(cd1.exists())
            self.assertEqual(cd1.read_bytes(), b"aaaaa")  # restored, intact
            self.assertTrue(cd2.exists())
            self.assertEqual(cd2.read_bytes(), b"bbb")
            self.assertEqual(audit.records, [])           # clean rollback → nothing audited
            self.assertFalse(cd1.with_name(cd1.name + STAGING_SUFFIX).exists())  # no leftover

    def test_stacked_stage_failure_with_failed_rollback_audits_orphan(self) -> None:
        # If rollback ITSELF fails (the staged part can't be moved back), that part is
        # orphaned at its staging path and audited as an error naming the original
        # media path; still nothing is unlinked and zero bytes are freed.
        with tempfile.TemporaryDirectory() as tmp:
            media_root, cd1, cd2, group, config = self._two_part_stacked(tmp)
            # call 1 stages cd1; call 2 (stage cd2) fails; call 3 (rollback cd1) fails
            mover = _PartialMover(fail_on_calls={2, 3})
            deleter = _FakeDeleter()
            audit = _FakeAudit()
            service = _service(
                _report([group]), config=config, deleter=deleter, mover=mover, audit=audit
            )
            result = _reclaim(service, part_id=2).results[0]
            self.assertEqual(result.status, "error")
            self.assertEqual(result.reclaimed_bytes, 0)
            self.assertIn("could not be rolled back", result.message)
            self.assertEqual(deleter.calls, [])
            staged_cd1 = cd1.with_name(cd1.name + STAGING_SUFFIX)
            self.assertFalse(cd1.exists())    # orphaned at its staging path
            self.assertTrue(staged_cd1.exists())
            self.assertTrue(cd2.exists())     # never staged
            self.assertEqual([r.status for r in audit.records], ["error"])
            self.assertEqual(Path(audit.records[0].path), cd1)  # audit names the media path
            self.assertIn("left staged", audit.records[0].message)

    def test_purge_failure_after_staging_is_partial_error(self) -> None:
        # Once every part has staged, the unlink pass runs; if the SECOND unlink fails
        # (a purge-phase failure, post-commit), the first part is deleted + audited,
        # the second is audited as an error, and the result is a partial error carrying
        # only the first part's freed bytes — the shared #70 loop's protocol, run over
        # staged paths but auditing the original media paths.
        with tempfile.TemporaryDirectory() as tmp:
            media_root, cd1, cd2, group, config = self._two_part_stacked(tmp)
            deleter = _FakeDeleter(fail_on_call=2)  # 2nd unlink fails (real os.rename mover)
            audit = _FakeAudit()
            service = _service(_report([group]), config=config, deleter=deleter, audit=audit)
            result = _reclaim(service, part_id=2).results[0]
            self.assertEqual(result.status, "error")
            self.assertEqual(result.reclaimed_bytes, 5)   # only cd1's bytes freed
            self.assertIn("partial", result.message)
            self.assertFalse(cd1.exists())  # both parts staged (renamed away) before any unlink
            self.assertFalse(cd2.exists())
            self.assertEqual(len(deleter.calls), 2)
            self.assertTrue(all(str(c).endswith(STAGING_SUFFIX) for c in deleter.calls))
            self.assertEqual([r.status for r in audit.records], ["deleted", "error"])
            self.assertEqual(
                [Path(r.path) for r in audit.records],
                [media_root / "movie" / "cd1.mkv", media_root / "movie" / "cd2.mkv"],
            )
            self.assertEqual(len(audit.batches), 1)       # one flush batch

    def test_purge_failure_before_any_commit_rolls_back(self) -> None:
        # #71 (Codex): an unlink failing BEFORE any delete commits is recoverable —
        # every part is still staged, so all are rolled back and the reclaim is refused
        # with nothing deleted (vs. leaving originals renamed away and the later,
        # un-attempted staged parts orphaned and un-audited).
        with tempfile.TemporaryDirectory() as tmp:
            media_root, cd1, cd2, group, config = self._two_part_stacked(tmp)
            deleter = _FakeDeleter(fail_on_call=1)  # 1st unlink fails (nothing committed)
            audit = _FakeAudit()
            service = _service(_report([group]), config=config, deleter=deleter, audit=audit)
            result = _reclaim(service, part_id=2).results[0]
            self.assertEqual(result.status, "error")
            self.assertEqual(result.reclaimed_bytes, 0)
            self.assertIn("rolled back, nothing deleted", result.message)
            self.assertEqual(len(deleter.calls), 1)       # stopped at the first failure
            self.assertTrue(cd1.exists())
            self.assertEqual(cd1.read_bytes(), b"aaaaa")  # both restored
            self.assertTrue(cd2.exists())
            self.assertEqual(cd2.read_bytes(), b"bbb")
            self.assertEqual(audit.records, [])           # clean rollback → nothing audited
            self.assertFalse(cd1.with_name(cd1.name + STAGING_SUFFIX).exists())
            self.assertFalse(cd2.with_name(cd2.name + STAGING_SUFFIX).exists())

    def test_long_basename_stages_with_bounded_name(self) -> None:
        # #71 (Codex): a basename near NAME_MAX must still stage. Appending the suffix
        # to the full name would overflow the component limit (ENAMETOOLONG) and refuse
        # a reclaim a direct unlink would have handled; the staging name is bounded.
        with tempfile.TemporaryDirectory() as tmp:
            media_root = Path(tmp) / "media"
            media_root.mkdir()
            long_name = "x" * 250 + ".mkv"  # 254 bytes, near the 255-byte NAME_MAX
            real = media_root / long_name
            real.write_bytes(b"xxxxx")
            keeper = _keeper()
            group = _group(
                [keeper, _copy(f"/plex/{long_name}", 5, media_id=21, association="untracked")],
                keeper=keeper,
            )
            config = _config(web_media_path_map=((Path("/plex"), media_root),))
            service = _service(_report([group]), config=config, deleter=os.unlink)
            result = _reclaim(service).results[0]
            self.assertEqual(result.status, "deleted")  # not ENAMETOOLONG-refused
            self.assertFalse(real.exists())

    def test_rollback_skips_recreated_original_leaving_orphan(self) -> None:
        # #71 (Codex): if the original path reappears during the staging window,
        # rollback must NOT clobber the new file (os.rename replaces on POSIX) — it
        # leaves the staged copy as an audited orphan and preserves the new file.
        with tempfile.TemporaryDirectory() as tmp:
            media_root, cd1, cd2, group, config = self._two_part_stacked(tmp)
            calls = []

            def mover(src, dst):
                calls.append((Path(src), Path(dst)))
                if len(calls) == 1:
                    os.rename(src, dst)               # stage cd1
                    cd1.write_bytes(b"NEW-CONTENT")   # another process recreates cd1
                else:
                    raise OSError("stage cd2 failed")  # forces rollback of cd1

            deleter = _FakeDeleter()
            audit = _FakeAudit()
            service = _service(
                _report([group]), config=config, deleter=deleter, mover=mover, audit=audit
            )
            result = _reclaim(service, part_id=2).results[0]
            self.assertEqual(result.status, "error")
            self.assertEqual(deleter.calls, [])           # nothing unlinked
            self.assertIn("could not be rolled back", result.message)
            self.assertEqual(cd1.read_bytes(), b"NEW-CONTENT")  # new file NOT clobbered
            self.assertTrue(cd1.with_name(cd1.name + STAGING_SUFFIX).exists())  # orphan kept
            self.assertEqual([r.status for r in audit.records], ["error"])
            self.assertEqual(Path(audit.records[0].path), cd1)
            self.assertIn("left staged", audit.records[0].message)


# --------------------------------------------------------------------------- #
# *arr backend                                                                 #
# --------------------------------------------------------------------------- #

class ArrRoutingTests(unittest.TestCase):
    def _tracked_group(self, backend="radarr", file="/lib/old.mkv", arr_file_id=55):
        keeper = _keeper()
        copy = _copy(
            file, 8, media_id=21, association="tracked", arr_tracked=backend,
            arr_file_id=arr_file_id,
        )
        return _group([keeper, copy], keeper=keeper)

    def test_tracked_radarr_deletes_by_id_after_one_get(self) -> None:
        # The report-serialized id (55) is re-validated by a single by-id GET whose
        # current basename still matches, then deleted — no full-library fan-out.
        radarr = _FakeArr({55: "/data/old.mkv"})
        audit = _FakeAudit()
        service = _service(
            _report([self._tracked_group("radarr")]), radarr=radarr, audit=audit
        )
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "deleted")
        self.assertEqual(response.results[0].backend, "radarr")
        self.assertEqual(radarr.get_calls, [55])  # exactly one by-id GET
        self.assertEqual(radarr.deleted, [55])
        self.assertEqual(len(audit.records), 1)

    def test_tracked_sonarr_deletes_by_id(self) -> None:
        sonarr = _FakeArr({77: "/data/tv/old.mkv"})
        group = self._tracked_group("sonarr", arr_file_id=77)
        group["kind"] = "episode"
        service = _service(_report([group]), sonarr=sonarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "deleted")
        self.assertEqual(sonarr.get_calls, [77])
        self.assertEqual(sonarr.deleted, [77])

    def test_dry_run_validates_by_id_but_makes_no_delete(self) -> None:
        # Dry-run still runs the by-id GET validation (so a stale id previews as a
        # refusal), but issues no DELETE and writes no audit record.
        radarr = _FakeArr({55: "/data/old.mkv"})
        audit = _FakeAudit()
        service = _service(
            _report([self._tracked_group("radarr")]),
            config=_config(web_actions_dry_run=True),
            radarr=radarr,
            audit=audit,
        )
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "would-delete")
        self.assertEqual(radarr.get_calls, [55])  # validated
        self.assertEqual(radarr.deleted, [])  # but not deleted
        self.assertEqual(audit.records, [])  # and not audited

    def test_drift_basename_mismatch_refused(self) -> None:
        # The id still resolves, but now points at a DIFFERENT file (the id was
        # reused after the report) — a drift guard refuses rather than delete it.
        radarr = _FakeArr({55: "/data/some-other-movie.mkv"})
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("drift", response.results[0].message)
        self.assertEqual(radarr.deleted, [])

    def test_drift_size_mismatch_refused(self) -> None:
        # The id still resolves to a same-basename file, but its size differs from
        # the report — the id was reused for a DIFFERENT file that happens to share
        # the basename (generic names collide across series). Size catches what
        # basename cannot; refuse rather than delete the wrong file.
        radarr = _FakeArr({55: "/data/old.mkv"}, sizes={55: 999})  # report part.size is 8
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("size changed", response.results[0].message)
        self.assertEqual(radarr.deleted, [])

    def test_matching_size_and_basename_deletes(self) -> None:
        # The size cross-check must not false-refuse the correct file: a current
        # record whose basename AND size both match the report is deleted.
        radarr = _FakeArr({55: "/data/old.mkv"}, sizes={55: 8})  # matches report part.size
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "deleted")
        self.assertEqual(radarr.deleted, [55])

    def test_id_gone_404_refused(self) -> None:
        radarr = _FakeArr({})  # id 55 no longer exists
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("no longer exists", response.results[0].message)
        self.assertEqual(radarr.deleted, [])

    def test_missing_id_in_report_refused_with_regenerate_hint(self) -> None:
        # A tracked copy from a report predating #61 has no arr_file_id key at all;
        # it is refused (fail-closed) with a regenerate hint rather than resolved
        # live via a full-library fetch (the fallback #61 removed).
        keeper = _keeper()
        old_copy = _copy(
            "/lib/old.mkv", 8, media_id=21, association="tracked", arr_tracked="radarr",
            parts=[{"part_id": 2, "file": "/lib/old.mkv", "size": 8}],  # no arr_file_id
        )
        radarr = _FakeArr({55: "/data/old.mkv"})
        service = _service(_report([_group([keeper, old_copy], keeper=keeper)]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("regenerate", response.results[0].message)
        self.assertEqual(radarr.get_calls, [])  # no id to look up
        self.assertEqual(radarr.deleted, [])

    def test_missing_client_refused(self) -> None:
        # Tracked-by-radarr but no Radarr client wired.
        service = _service(_report([self._tracked_group("radarr")]), radarr=None)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("not configured", response.results[0].message)

    def test_arr_delete_failure_is_error_and_audited(self) -> None:
        radarr = _FakeArr({55: "/data/old.mkv"}, fail_delete=True)
        audit = _FakeAudit()
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr, audit=audit)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "error")
        self.assertEqual(len(audit.records), 1)
        self.assertEqual(audit.records[0].status, "error")

    def test_stacked_partial_delete_failure_audits_deleted_then_error(self) -> None:
        # #70: the shared delete-and-audit loop's whole-or-refused protocol. A
        # two-part tracked copy whose SECOND delete fails: part one is deleted and
        # audited, part two is audited as an error, both in ONE flush batch, and the
        # result is a partial `error` carrying only part one's freed bytes. This is
        # the mid-loop failure the old per-backend loops had no test for (the fakes
        # could only fail EVERY delete).
        keeper = _keeper()
        stacked = _copy(
            "/lib/cd1.mkv", 8, media_id=21, association="tracked", arr_tracked="radarr",
            parts=[
                {"part_id": 2, "file": "/lib/cd1.mkv", "size": 5, "arr_file_id": 1},
                {"part_id": 3, "file": "/lib/cd2.mkv", "size": 3, "arr_file_id": 2},
            ],
        )
        radarr = _FakeArr({1: "/data/cd1.mkv", 2: "/data/cd2.mkv"}, fail_delete_ids={2})
        audit = _FakeAudit()
        service = _service(
            _report([_group([keeper, stacked], keeper=keeper)]), radarr=radarr, audit=audit
        )
        response = _reclaim(service, part_id=2)
        result = response.results[0]
        self.assertEqual(result.status, "error")
        self.assertEqual(result.reclaimed_bytes, 5)          # only cd1's bytes freed
        self.assertIn("partial", result.message)
        self.assertIn("cd2.mkv", result.message)
        self.assertEqual(radarr.deleted, [1])                # cd1 gone; cd2 attempted, failed
        self.assertEqual([r.status for r in audit.records], ["deleted", "error"])
        self.assertEqual(
            [Path(r.path) for r in audit.records],
            [Path("/lib/cd1.mkv"), Path("/lib/cd2.mkv")],   # audit uses media paths, in order
        )
        self.assertEqual(audit.records[0].message, "id=1 rating_key=900 part_id=2")
        self.assertTrue(audit.records[1].message.startswith("id=2 rating_key=900:"))
        self.assertEqual(len(audit.batches), 1)              # one flush batch, not per-part

    def test_stacked_tracked_copy_validates_all_parts_before_delete(self) -> None:
        # A two-part tracked copy: each part is re-validated by its own by-id GET
        # before any DELETE, and both are deleted together (removed whole).
        keeper = _keeper()
        stacked = _copy(
            "/lib/cd1.mkv", 5, media_id=21, association="tracked", arr_tracked="radarr",
            parts=[
                {"part_id": 2, "file": "/lib/cd1.mkv", "size": 5, "arr_file_id": 1},
                {"part_id": 3, "file": "/lib/cd2.mkv", "size": 3, "arr_file_id": 2},
            ],
        )
        radarr = _FakeArr({1: "/data/cd1.mkv", 2: "/data/cd2.mkv"})
        service = _service(_report([_group([keeper, stacked], keeper=keeper)]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "deleted")
        self.assertEqual(response.results[0].reclaimed_bytes, 8)
        self.assertEqual(sorted(radarr.get_calls), [1, 2])  # one GET per part
        self.assertEqual(sorted(radarr.deleted), [1, 2])

    def test_stacked_one_bad_part_refuses_whole_copy_before_delete(self) -> None:
        # If any part fails re-validation (here part 2's id is gone), the whole
        # stacked copy is refused and NOTHING is deleted.
        keeper = _keeper()
        stacked = _copy(
            "/lib/cd1.mkv", 5, media_id=21, association="tracked", arr_tracked="radarr",
            parts=[
                {"part_id": 2, "file": "/lib/cd1.mkv", "size": 5, "arr_file_id": 1},
                {"part_id": 3, "file": "/lib/cd2.mkv", "size": 3, "arr_file_id": 2},
            ],
        )
        radarr = _FakeArr({1: "/data/cd1.mkv"})  # id 2 missing -> 404
        service = _service(_report([_group([keeper, stacked], keeper=keeper)]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertEqual(radarr.deleted, [])  # no part deleted

    def test_arr_get_failure_non_404_refused(self) -> None:
        radarr = _FakeArr({55: "/data/old.mkv"}, fail_get=True)
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("re-validate", response.results[0].message)
        self.assertEqual(radarr.deleted, [])

    def test_non_numeric_token_is_refused_not_crash(self) -> None:
        # A hostile non-ASCII token must refuse (403), never raise TypeError.
        service = _service(_report([]))
        response = _reclaim(service, token="café")
        self.assertEqual(response.status_code, 403)

    def test_sibling_stacked_parts_deduped_to_one_result(self) -> None:
        # Selecting two parts of ONE stacked copy collapses to a single result,
        # so a dry-run preview never double-counts the copy's bytes.
        keeper = _keeper()
        stacked = _copy(
            "/lib/cd1.mkv", 8, media_id=21, association="unknown",  # unknown -> refused, but deduped first
            parts=[
                {"part_id": 2, "file": "/lib/cd1.mkv", "size": 5},
                {"part_id": 3, "file": "/lib/cd2.mkv", "size": 3},
            ],
        )
        service = _service(_report([_group([keeper, stacked], keeper=keeper)]))
        response = service.reclaim(
            [ReclaimTarget("900", 2), ReclaimTarget("900", 3)], token="tok", report_generated_at=GEN
        )
        self.assertEqual(len(response.results), 1)  # one copy, one result


# --------------------------------------------------------------------------- #
# Action index unit checks                                                     #
# --------------------------------------------------------------------------- #

class ActionIndexTests(unittest.TestCase):
    def test_skips_group_without_rating_key(self) -> None:
        keeper = _keeper()
        group = _group([keeper, _copy("/lib/old.mkv", 8, media_id=21, association="untracked")], keeper=keeper, rating_key="")
        index = build_action_index(_report([group]))
        self.assertEqual(index.entries, {})

    def test_zero_part_id_not_indexed(self) -> None:
        keeper = _keeper()
        copy = _copy("/lib/old.mkv", 8, media_id=21, association="untracked", parts=[{"part_id": 0, "file": "/lib/old.mkv", "size": 8}])
        index = build_action_index(_report([_group([keeper, copy], keeper=keeper)]))
        self.assertNotIn(("900", 0), index.entries)


# --------------------------------------------------------------------------- #
# HTTP endpoints                                                               #
# --------------------------------------------------------------------------- #

@contextmanager
def _serve(payload, service, *, require_browser_origin=False, allowed_origins=()):
    viewer = DuplicateReportViewer(lambda: payload, actions_enabled=service.enabled)
    server = DuplicateReportServer(
        "127.0.0.1",
        0,
        viewer,
        reclaim_service=service,
        require_browser_origin=require_browser_origin,
        allowed_origins=allowed_origins,
    )
    server.start_background()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()


def _post(url, data, headers, method="POST"):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _untracked_payload(size=5):
    keeper = _keeper()
    return _report([_group([keeper, _copy("/plex/x.mkv", size, media_id=21, association="untracked")], keeper=keeper)])


class HttpEndpointTests(unittest.TestCase):
    def test_post_when_actions_disabled_is_405(self) -> None:
        payload = _untracked_payload()
        service = _service(payload, config=_config(web_actions_enabled=False))
        with _serve(payload, service) as base:
            status, _ = _post(
                base + "/api/reclaim", b"{}", {"Content-Type": "application/json"}
            )
        self.assertEqual(status, 405)

    def test_json_dry_run_reports_would_delete(self) -> None:
        payload = _untracked_payload()
        # dry-run so no real path map is needed to get a would-delete/refused result
        service = _service(payload, config=_config(web_actions_dry_run=True))
        body = json.dumps({"report_generated_at": GEN, "targets": [{"rating_key": "900", "part_id": 2}]}).encode()
        with _serve(payload, service) as base:
            status, raw = _post(
                base + "/api/reclaim", body, {"Content-Type": "application/json", "X-Action-Token": "tok"}
            )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertTrue(data["dry_run"])
        # unmapped path in dry-run surfaces as a refusal (still no delete)
        self.assertEqual(len(data["results"]), 1)

    def test_json_missing_token_is_403(self) -> None:
        payload = _untracked_payload()
        service = _service(payload)
        body = json.dumps({"report_generated_at": GEN, "targets": []}).encode()
        with _serve(payload, service) as base:
            status, _ = _post(base + "/api/reclaim", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 403)

    def test_json_cross_origin_is_403(self) -> None:
        payload = _untracked_payload()
        service = _service(payload)
        body = json.dumps({"report_generated_at": GEN, "targets": []}).encode()
        with _serve(payload, service) as base:
            status, _ = _post(
                base + "/api/reclaim",
                body,
                {"Content-Type": "application/json", "X-Action-Token": "tok", "Origin": "http://evil.example"},
            )
        self.assertEqual(status, 403)

    def test_json_body_too_large_is_413(self) -> None:
        payload = _untracked_payload()
        service = _service(payload)
        big = b'{"targets":[' + b'{"rating_key":"900","part_id":2},' * 20000 + b'{}]}'
        with _serve(payload, service) as base:
            status, _ = _post(
                base + "/api/reclaim", big, {"Content-Type": "application/json", "X-Action-Token": "tok"}
            )
        self.assertEqual(status, 413)

    def test_json_invalid_body_is_400(self) -> None:
        payload = _untracked_payload()
        service = _service(payload)
        with _serve(payload, service) as base:
            status, _ = _post(
                base + "/api/reclaim", b"{not json", {"Content-Type": "application/json", "X-Action-Token": "tok"}
            )
        self.assertEqual(status, 400)

    def test_form_endpoint_renders_result_page(self) -> None:
        payload = _untracked_payload()
        service = _service(payload, config=_config(web_actions_dry_run=True))
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(payload, service) as base:
            status, raw = _post(
                base + "/actions/reclaim", form, {"Content-Type": "application/x-www-form-urlencoded"}
            )
        self.assertEqual(status, 200)
        self.assertIn(b"Reclaim result", raw)
        self.assertIn(b"DRY-RUN", raw)


class CsrfHardeningHttpTests(unittest.TestCase):
    """#63: the browser-form origin gate on a non-loopback bind, driven over HTTP."""

    def _dry_service(self):
        payload = _untracked_payload()
        return payload, _service(payload, config=_config(web_actions_dry_run=True))

    def test_loopback_form_without_origin_still_works(self) -> None:
        # The default (loopback) posture is unchanged: a form POST with no Origin
        # is accepted (the token still gates it).
        payload, service = self._dry_service()
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(payload, service, require_browser_origin=False) as base:
            status, raw = _post(
                base + "/actions/reclaim", form, {"Content-Type": "application/x-www-form-urlencoded"}
            )
        self.assertEqual(status, 200)
        self.assertIn(b"Reclaim result", raw)

    def test_nonloopback_form_without_origin_is_403(self) -> None:
        payload, service = self._dry_service()
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(payload, service, require_browser_origin=True) as base:
            status, raw = _post(
                base + "/actions/reclaim", form, {"Content-Type": "application/x-www-form-urlencoded"}
            )
        self.assertEqual(status, 403)
        self.assertIn(b"Cross-origin request refused", raw)

    def test_nonloopback_form_with_matching_origin_ok(self) -> None:
        payload, service = self._dry_service()
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(payload, service, require_browser_origin=True) as base:
            status, raw = _post(
                base + "/actions/reclaim",
                form,
                {"Content-Type": "application/x-www-form-urlencoded", "Origin": base},
            )
        self.assertEqual(status, 200)
        self.assertIn(b"Reclaim result", raw)

    def test_nonloopback_form_with_cross_origin_is_403(self) -> None:
        payload, service = self._dry_service()
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(payload, service, require_browser_origin=True) as base:
            status, _ = _post(
                base + "/actions/reclaim",
                form,
                {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://evil.example"},
            )
        self.assertEqual(status, 403)

    def test_nonloopback_form_same_origin_referer_fallback_ok(self) -> None:
        payload, service = self._dry_service()
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(payload, service, require_browser_origin=True) as base:
            status, _ = _post(
                base + "/actions/reclaim",
                form,
                {"Content-Type": "application/x-www-form-urlencoded", "Referer": base + "/"},
            )
        self.assertEqual(status, 200)

    def test_allowlist_accepts_external_proxy_origin(self) -> None:
        # Behind a TLS proxy the browser Origin is the external https origin, which
        # only the allow-list can vouch for (the server itself is plain http).
        payload, service = self._dry_service()
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(
            payload, service, require_browser_origin=True,
            allowed_origins=("https://media.example.com",),
        ) as base:
            status, _ = _post(
                base + "/actions/reclaim",
                form,
                {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://media.example.com"},
            )
        self.assertEqual(status, 200)

    def test_nonloopback_json_without_origin_still_token_only(self) -> None:
        # The JSON API is unaffected by the browser-form requirement: no Origin +
        # a valid token succeeds even on a non-loopback bind.
        payload = _untracked_payload()
        service = _service(payload, config=_config(web_actions_dry_run=True))
        body = json.dumps({"report_generated_at": GEN, "targets": [{"rating_key": "900", "part_id": 2}]}).encode()
        with _serve(payload, service, require_browser_origin=True) as base:
            status, _ = _post(
                base + "/api/reclaim", body, {"Content-Type": "application/json", "X-Action-Token": "tok"}
            )
        self.assertEqual(status, 200)

    def test_allowlist_on_loopback_bind_still_requires_browser_origin(self) -> None:
        # A reverse proxy can forward to a LOOPBACK bind, so configuring an allow-list
        # must flip on the browser-origin requirement there too — otherwise an
        # origin-less cross-site form POST slips through before the list is consulted.
        payload = _untracked_payload()
        config = _config(
            web_actions_dry_run=True,
            web_bind_address="127.0.0.1",  # loopback
            web_allowed_origins=("https://ext.example",),
        )
        service = _service(payload, config=config)
        server = build_server(config, provider=lambda: payload, reclaim_service=service)
        server.start_background()
        try:
            base = f"http://127.0.0.1:{server.port}"
            form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
            no_origin, _ = _post(
                base + "/actions/reclaim", form, {"Content-Type": "application/x-www-form-urlencoded"}
            )
            listed, _ = _post(
                base + "/actions/reclaim",
                form,
                {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://ext.example"},
            )
        finally:
            server.shutdown()
        self.assertEqual(no_origin, 403)  # origin-less form POST refused despite loopback
        self.assertEqual(listed, 200)     # the allow-listed proxy origin is accepted

    def test_malformed_origin_is_clean_403_not_dropped_connection(self) -> None:
        # A hostile bad-port Origin must yield a clean 403, never crash the request
        # thread (urlparse(...).port raises ValueError) and drop the connection.
        payload, service = self._dry_service()
        form = f"token=tok&report_generated_at={GEN}&target=900:2".encode()
        with _serve(payload, service, require_browser_origin=True) as base:
            status, _ = _post(
                base + "/actions/reclaim",
                form,
                {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://evil:999999"},
            )
        self.assertEqual(status, 403)

    def test_referrer_policy_same_origin_when_actions_enabled(self) -> None:
        payload = _untracked_payload()
        service = _service(payload)
        with _serve(payload, service) as base:
            with urllib.request.urlopen(base + "/", timeout=5) as resp:
                self.assertEqual(resp.headers.get("Referrer-Policy"), "same-origin")


class ActionFormRenderTests(unittest.TestCase):
    def test_form_and_relaxed_csp_when_actions_enabled(self) -> None:
        payload = _untracked_payload()
        service = _service(payload)
        with _serve(payload, service) as base:
            with urllib.request.urlopen(base + "/", timeout=5) as resp:
                headers = resp.headers
                html = resp.read().decode("utf-8")
        self.assertIn('action="/actions/reclaim"', html)
        self.assertIn('name="target"', html)
        self.assertIn('name="token"', html)
        self.assertIn("form-action 'self'", headers.get("Content-Security-Policy", ""))

    def test_no_form_and_strict_csp_when_disabled(self) -> None:
        payload = _untracked_payload()
        service = _service(payload, config=_config(web_actions_enabled=False))
        with _serve(payload, service) as base:
            with urllib.request.urlopen(base + "/", timeout=5) as resp:
                headers = resp.headers
                html = resp.read().decode("utf-8")
        self.assertNotIn("/actions/reclaim", html)
        self.assertIn("form-action 'none'", headers.get("Content-Security-Policy", ""))

    def test_render_report_html_actions_disabled_has_no_form(self) -> None:
        html = render_report_html(_untracked_payload(), actions_enabled=False)
        self.assertNotIn("/actions/reclaim", html)


if __name__ == "__main__":
    unittest.main()
