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
    render_report_html,
)
from unraid_cache_cleaner.web_actions import (
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
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[Path] = []
        self._fail = fail

    def __call__(self, path: Path) -> None:
        self.calls.append(Path(path))
        if self._fail:
            raise OSError("permission denied")


class _FakeArr:
    """A fake Radarr/Sonarr client recording index/delete calls.

    ``ids`` is the basename -> [file id] index; ``index_calls`` proves the action
    layer builds the index at most once per request rather than per target.
    """

    def __init__(self, ids=None, *, fail_delete=False, fail_index=False) -> None:
        self._ids = ids or {}
        self._fail_delete = fail_delete
        self._fail_index = fail_index
        self.deleted: list[int] = []
        self.index_calls = 0

    def fetch_file_index(self):
        self.index_calls += 1
        if self._fail_index:
            raise ArrClientError("arr index fetch failed")
        return {name: list(ids) for name, ids in self._ids.items()}

    def delete_movie_file(self, file_id: int) -> None:
        if self._fail_delete:
            raise ArrClientError("arr delete failed")
        self.deleted.append(file_id)

    delete_episode_file = delete_movie_file


class _FakeAudit:
    def __init__(self) -> None:
        self.records = []

    def __call__(self, records, now) -> None:
        self.records.extend(records)


def _keeper(file="/lib/keep.4k.mkv", size=20 * GiB, media_id=20, part_id=1):
    return {
        "file": file,
        "size": size,
        "resolution": "4k",
        "media_id": media_id,
        "parts": [{"part_id": part_id, "file": file, "size": size}],
        "association": "untracked",
        "arr_tracked": None,
    }


def _copy(file, size, *, media_id, association, arr_tracked=None, parts=None, resolution="1080"):
    parts = parts or [{"part_id": 2, "file": file, "size": size}]
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
            self.assertEqual(deleter.calls, [real])
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
            self.assertEqual(deleter.calls, [target])

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


# --------------------------------------------------------------------------- #
# *arr backend                                                                 #
# --------------------------------------------------------------------------- #

class ArrRoutingTests(unittest.TestCase):
    def _tracked_group(self, backend="radarr", file="/lib/old.mkv"):
        keeper = _keeper()
        copy = _copy(file, 8, media_id=21, association="tracked", arr_tracked=backend)
        return _group([keeper, copy], keeper=keeper)

    def test_tracked_radarr_hits_delete_movie_file(self) -> None:
        radarr = _FakeArr({"old.mkv": [55]})
        audit = _FakeAudit()
        service = _service(
            _report([self._tracked_group("radarr")]), radarr=radarr, audit=audit
        )
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "deleted")
        self.assertEqual(response.results[0].backend, "radarr")
        self.assertEqual(radarr.deleted, [55])
        self.assertEqual(len(audit.records), 1)

    def test_tracked_sonarr_hits_delete_episode_file(self) -> None:
        sonarr = _FakeArr({"old.mkv": [77]})
        group = self._tracked_group("sonarr")
        group["kind"] = "episode"
        service = _service(_report([group]), sonarr=sonarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "deleted")
        self.assertEqual(sonarr.deleted, [77])

    def test_dry_run_makes_no_arr_delete_calls(self) -> None:
        radarr = _FakeArr({"old.mkv": [55]})
        service = _service(
            _report([self._tracked_group("radarr")]),
            config=_config(web_actions_dry_run=True),
            radarr=radarr,
        )
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "would-delete")
        self.assertEqual(radarr.deleted, [])

    def test_ambiguous_arr_resolution_refused(self) -> None:
        radarr = _FakeArr({"old.mkv": [1, 2]})  # two files share the basename
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("ambiguous", response.results[0].message)
        self.assertEqual(radarr.deleted, [])

    def test_not_found_arr_resolution_refused(self) -> None:
        radarr = _FakeArr({})  # basename no longer tracked
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("no radarr file", response.results[0].message)

    def test_missing_client_refused(self) -> None:
        # Tracked-by-radarr but no Radarr client wired.
        service = _service(_report([self._tracked_group("radarr")]), radarr=None)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
        self.assertIn("not configured", response.results[0].message)

    def test_arr_delete_failure_is_error_and_audited(self) -> None:
        radarr = _FakeArr({"old.mkv": [55]}, fail_delete=True)
        audit = _FakeAudit()
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr, audit=audit)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "error")
        self.assertEqual(len(audit.records), 1)
        self.assertEqual(audit.records[0].status, "error")

    def test_arr_index_built_once_for_multiple_targets(self) -> None:
        # Two tracked copies in one request must share a single index fetch, not
        # one full-library fan-out per target.
        keeper = _keeper()
        g = _group(
            [
                keeper,
                _copy("/lib/a.mkv", 8, media_id=21, association="tracked", arr_tracked="radarr",
                      parts=[{"part_id": 2, "file": "/lib/a.mkv", "size": 8}]),
                _copy("/lib/b.mkv", 8, media_id=22, association="tracked", arr_tracked="radarr",
                      parts=[{"part_id": 3, "file": "/lib/b.mkv", "size": 8}]),
            ],
            keeper=keeper,
        )
        radarr = _FakeArr({"a.mkv": [1], "b.mkv": [2]})
        service = _service(_report([g]), radarr=radarr)
        response = service.reclaim(
            [ReclaimTarget("900", 2), ReclaimTarget("900", 3)], token="tok", report_generated_at=GEN
        )
        self.assertEqual([r.status for r in response.results], ["deleted", "deleted"])
        self.assertEqual(sorted(radarr.deleted), [1, 2])
        self.assertEqual(radarr.index_calls, 1)  # one fetch, not one per target

    def test_arr_index_fetch_failure_refused(self) -> None:
        radarr = _FakeArr({"old.mkv": [55]}, fail_index=True)
        service = _service(_report([self._tracked_group("radarr")]), radarr=radarr)
        response = _reclaim(service)
        self.assertEqual(response.results[0].status, "refused")
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
