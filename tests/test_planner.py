"""Planner and scanner safety tests."""

from __future__ import annotations

import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.models import TorrentRecord
from unraid_cache_cleaner.planner import build_protection_plan, find_orphan_candidates
from unraid_cache_cleaner.scanner import scan_filesystem


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


if __name__ == "__main__":
    unittest.main()
