"""Planner and scanner safety tests."""

from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.models import TorrentRecord
from unraid_cache_cleaner.planner import (
    build_protection_plan,
    find_orphan_candidates,
    map_media_path,
    normalize_path,
    with_protected_files,
)
from unraid_cache_cleaner.scanner import scan_filesystem


class MapMediaPathTests(unittest.TestCase):
    """The shared Plex->container mapper reused by the reclaim path and hash pass."""

    def test_maps_under_prefix(self) -> None:
        path_map = ((Path("/mnt/user/Media"), Path("/media")),)
        result = map_media_path(Path("/mnt/user/Media/Movies/x.mkv"), path_map)
        self.assertEqual(result, (Path("/media/Movies/x.mkv"), Path("/media")))

    def test_unmapped_returns_none(self) -> None:
        path_map = ((Path("/mnt/user/Media"), Path("/media")),)
        self.assertIsNone(map_media_path(Path("/elsewhere/x.mkv"), path_map))

    def test_component_aware_prefix_not_substring(self) -> None:
        # /mnt/user/Media must not match /mnt/user/Media2 (a substring prefix).
        path_map = ((Path("/mnt/user/Media"), Path("/media")),)
        self.assertIsNone(map_media_path(Path("/mnt/user/Media2/x.mkv"), path_map))

    def test_longest_prefix_wins(self) -> None:
        path_map = (
            (Path("/mnt/user"), Path("/all")),
            (Path("/mnt/user/Media"), Path("/media")),
        )
        result = map_media_path(Path("/mnt/user/Media/x.mkv"), path_map)
        self.assertEqual(result, (Path("/media/x.mkv"), Path("/media")))

    def test_traversal_in_remainder_refused(self) -> None:
        path_map = ((Path("/mnt/user/Media"), Path("/media")),)
        self.assertIsNone(map_media_path(Path("/mnt/user/Media/../etc/x"), path_map))


class PlannerTests(unittest.TestCase):
    def test_multifile_torrent_directory_is_fully_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            watch_root = Path(tempdir) / "data"
            torrent_root = watch_root / "active-release"
            torrent_root.mkdir(parents=True)
            (torrent_root / "part01.rar").write_text("tracked")
            (torrent_root / "movie.mkv").write_text("extracted")
            (watch_root / "orphan.txt").write_text("orphan")

            torrent = TorrentRecord(
                torrent_hash="abc",
                name="active-release",
                state="uploading",
                save_path=watch_root,
                content_path=torrent_root,
            )

            plan = build_protection_plan(
                [torrent],
                (watch_root,),
                protect_single_file_parent_dirs=True,
            )
            scanned = scan_filesystem((watch_root,), (), protected_dirs=plan.protected_dirs)
            candidates = find_orphan_candidates(scanned, plan)

            self.assertIn(watch_root / "orphan.txt", candidates)
            self.assertNotIn(torrent_root / "movie.mkv", candidates)

    def test_single_file_parent_directory_can_be_protected(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            watch_root = Path(tempdir) / "data"
            job_dir = watch_root / "job-001"
            job_dir.mkdir(parents=True)
            archive = job_dir / "release.rar"
            extracted = job_dir / "release.mkv"
            archive.write_text("tracked")
            extracted.write_text("extracted")

            torrent = TorrentRecord(
                torrent_hash="abc",
                name="release.rar",
                state="stalledUP",
                save_path=job_dir,
                content_path=archive,
            )

            plan = build_protection_plan(
                [torrent],
                (watch_root,),
                protect_single_file_parent_dirs=True,
            )
            scanned = scan_filesystem((watch_root,), (), protected_dirs=plan.protected_dirs)
            candidates = find_orphan_candidates(scanned, plan)

            self.assertEqual(candidates, {})

    def test_single_file_in_watch_root_only_protects_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            watch_root = Path(tempdir) / "data"
            watch_root.mkdir(parents=True)
            archive = watch_root / "release.rar"
            orphan = watch_root / "orphan.txt"
            archive.write_text("tracked")
            orphan.write_text("orphan")

            torrent = TorrentRecord(
                torrent_hash="abc",
                name="release.rar",
                state="uploading",
                save_path=watch_root,
                content_path=archive,
            )

            plan = build_protection_plan(
                [torrent],
                (watch_root,),
                protect_single_file_parent_dirs=True,
            )
            scanned = scan_filesystem((watch_root,), (), protected_dirs=plan.protected_dirs)
            candidates = find_orphan_candidates(scanned, plan)

            self.assertIn(orphan, candidates)
            self.assertNotIn(archive, candidates)


class ExtractedOutputProtectionTests(unittest.TestCase):
    """with_protected_files keeps extracted media safe (Child C, #36)."""

    def test_extracted_output_survives_beside_unprotected_single_file_rar(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            watch_root = Path(tempdir) / "data"
            job_dir = watch_root / "job-001"
            job_dir.mkdir(parents=True)
            archive = job_dir / "release.rar"
            extracted = job_dir / "release.mkv"
            archive.write_text("tracked")
            extracted.write_text("extracted")

            torrent = TorrentRecord(
                torrent_hash="abc",
                name="release.rar",
                state="stalledUP",
                save_path=job_dir,
                content_path=archive,
            )
            # With single-file parent protection OFF, the extracted .mkv is exposed.
            plan = build_protection_plan(
                [torrent], (watch_root,), protect_single_file_parent_dirs=False
            )
            scanned = scan_filesystem((watch_root,), (), protected_dirs=plan.protected_dirs)
            self.assertIn(normalize_path(extracted), find_orphan_candidates(scanned, plan))

            # Injecting the extracted output as a protected file closes the hole.
            protected = with_protected_files(plan, [extracted])
            candidates = find_orphan_candidates(scanned, protected)
            self.assertNotIn(normalize_path(extracted), candidates)

    def test_flat_watch_root_protects_output_but_still_cleans_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            watch_root = Path(tempdir) / "data"
            watch_root.mkdir(parents=True)
            archive = watch_root / "release.rar"
            extracted = watch_root / "release.mkv"
            orphan = watch_root / "orphan.txt"
            for path, text in ((archive, "tracked"), (extracted, "extracted"), (orphan, "orphan")):
                path.write_text(text)

            torrent = TorrentRecord(
                torrent_hash="abc",
                name="release.rar",
                state="uploading",
                save_path=watch_root,
                content_path=archive,
            )
            plan = build_protection_plan(
                [torrent], (watch_root,), protect_single_file_parent_dirs=True
            )
            # Protecting the extracted file must NOT protect the whole watch root.
            protected = with_protected_files(plan, [extracted])
            scanned = scan_filesystem((watch_root,), (), protected_dirs=protected.protected_dirs)
            candidates = find_orphan_candidates(scanned, protected)

            self.assertNotIn(normalize_path(extracted), candidates)  # kept
            self.assertNotIn(normalize_path(archive), candidates)  # tracked by torrent
            self.assertIn(normalize_path(orphan), candidates)  # cleanup still works


if __name__ == "__main__":
    unittest.main()
