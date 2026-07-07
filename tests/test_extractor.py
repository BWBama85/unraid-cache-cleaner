"""Extractor tests (fake tool + one gated real-binary roundtrip)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.config import Config
from unraid_cache_cleaner.extractor import (
    Extractor,
    ExtractorError,
    UnarArchiveTool,
    _derive_list_tool,
    _parse_owner,
    summarize,
)
from unraid_cache_cleaner.planner import normalize_path


class FakeTool:
    """Stand-in for the injected archive tool; records calls, no subprocess."""

    def __init__(
        self,
        *,
        available: bool = True,
        test_result: bool = True,
        test_raises: Exception | None = None,
        extract_raises: Exception | None = None,
        fail_names: set[str] | None = None,
    ) -> None:
        self.available = available
        self.test_result = test_result
        self.test_raises = test_raises
        self.extract_raises = extract_raises
        self.fail_names = fail_names or set()
        self.test_calls: list[Path] = []
        self.extract_calls: list[tuple[Path, Path]] = []

    def is_available(self) -> bool:
        return self.available

    def test(self, archive: Path) -> bool:
        self.test_calls.append(archive)
        if self.test_raises is not None:
            raise self.test_raises
        return self.test_result

    def extract(self, archive: Path, dest_dir: Path) -> None:
        self.extract_calls.append((archive, dest_dir))
        if self.extract_raises is not None:
            raise self.extract_raises
        if archive.name in self.fail_names:
            raise ExtractorError(f"boom: {archive.name}")
        # Simulate a real extraction so ownership/os.walk paths have something.
        (dest_dir / (Path(archive.name).stem + ".mkv")).write_text("extracted")


def _make_config(
    *,
    watch_root: Path,
    config_root: Path,
    dry_run: bool = False,
    extract_enabled: bool = True,
    extract_owner: str = "",
    extract_min_age_seconds: int = 0,
    excluded_globs: tuple[str, ...] = (),
) -> Config:
    return Config(
        qbittorrent_url="http://qbt:8080",
        qbittorrent_username="admin",
        qbittorrent_password="secret",
        qbittorrent_timeout_seconds=15,
        qbittorrent_verify_tls=True,
        watch_paths=(watch_root,),
        poll_interval_seconds=300,
        orphan_grace_seconds=0,
        min_file_age_seconds=0,
        dry_run=dry_run,
        delete_empty_dirs=True,
        protect_single_file_parent_dirs=True,
        excluded_globs=excluded_globs,
        state_db_path=config_root / "state.sqlite3",
        report_path=config_root / "last-run.json",
        log_level="INFO",
        plex_duplicate_report_path=config_root / "plex-duplicates.json",
        extract_enabled=extract_enabled,
        extract_owner=extract_owner,
        extract_min_age_seconds=extract_min_age_seconds,
    )


class _Fixture:
    """Temp dir with data/config roots; use as a context manager."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.watch_root = root / "data"
        self.config_root = root / "config"
        self.watch_root.mkdir()
        self.config_root.mkdir()

    def __enter__(self) -> "_Fixture":
        return self

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()

    def write_rar(self, relative: str, content: str = "rar") -> Path:
        path = self.watch_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def config(self, **overrides: object) -> Config:
        return _make_config(
            watch_root=self.watch_root, config_root=self.config_root, **overrides
        )


class ExtractorTests(unittest.TestCase):
    def test_extracts_single_archive(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("release/movie.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertEqual(len(tool.extract_calls), 1)
            self.assertTrue((fx.watch_root / "release" / "movie.mkv").exists())

    def test_multivolume_extracts_first_volume_only(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/show.part01.rar")
            fx.write_rar("rel/show.part02.rar")
            fx.write_rar("rel/show.part03.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual(len(results), 1)
            self.assertEqual(len(tool.extract_calls), 1)
            self.assertEqual(tool.extract_calls[0][0].name, "show.part01.rar")

    def test_legacy_rNN_volumes_are_not_scanned(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            fx.write_rar("rel/movie.r00")
            fx.write_rar("rel/movie.r01")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([c[0].name for c in tool.extract_calls], ["movie.rar"])

    def test_dry_run_reports_would_extract_without_writing(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(dry_run=True), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=True)

            self.assertEqual([r.status for r in results], ["would_extract"])
            self.assertEqual(tool.extract_calls, [])
            self.assertEqual(len(tool.test_calls), 1)
            self.assertFalse((fx.watch_root / "rel" / "movie.mkv").exists())

    def test_integrity_failure_defers(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            tool = FakeTool(test_result=False)
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["deferred_incomplete"])
            self.assertEqual(tool.extract_calls, [])

    def test_extraction_failure_marks_failed(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            tool = FakeTool(extract_raises=ExtractorError("unar exited 1"))
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["failed"])
            self.assertEqual(len(tool.extract_calls), 1)

    def test_settle_guard_defers_recent_archive(self) -> None:
        with _Fixture() as fx:
            archive = fx.write_rar("rel/movie.rar")
            now = 1_000_000.0
            os.utime(archive, (now - 10, now - 10))  # 10s old, younger than min age
            tool = FakeTool()
            extractor = Extractor(
                fx.config(extract_min_age_seconds=3600),
                tool=tool,
                clock=lambda: now,
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["deferred_incomplete"])
            self.assertEqual(tool.test_calls, [])  # deferred before the integrity test
            self.assertEqual(tool.extract_calls, [])

    def test_missing_binary_raises(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            extractor = Extractor(fx.config(), tool=FakeTool(available=False))

            with self.assertRaises(ExtractorError):
                extractor.extract_all((fx.watch_root,), dry_run=False)

    def test_symlinked_archive_is_skipped(self) -> None:
        with _Fixture() as fx:
            real = fx.write_rar("real/movie.rar")
            os.symlink(real, fx.watch_root / "alias.rar")
            tool = FakeTool()
            extractor = Extractor(fx.config(), tool=tool)

            extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual(
                [c[0] for c in tool.extract_calls], [normalize_path(real)]
            )

    def test_per_archive_error_isolation(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("a/good.rar")
            fx.write_rar("b/bad.rar")
            tool = FakeTool(fail_names={"bad.rar"})
            extractor = Extractor(fx.config(), tool=tool)

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            by_name = {r.archive.name: r.status for r in results}
            self.assertEqual(by_name, {"good.rar": "extracted", "bad.rar": "failed"})

    def test_ownership_applied_only_to_new_files(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")  # pre-existing; must NOT be chowned
            chown_calls: list[tuple[str, int, int]] = []
            extractor = Extractor(
                fx.config(extract_owner="99:100"),
                tool=FakeTool(),
                chown=lambda path, uid, gid: chown_calls.append((path, uid, gid)),
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertTrue(all((uid, gid) == (99, 100) for _, uid, gid in chown_calls))
            chowned = {path for path, _, _ in chown_calls}
            self.assertIn(str(fx.watch_root / "rel" / "movie.mkv"), chowned)  # extracted
            self.assertNotIn(str(fx.watch_root / "rel" / "movie.rar"), chowned)  # source

    def test_ownership_scoped_to_extracted_output_at_watch_root(self) -> None:
        # A loose .rar at the watch root must not turn chown into an
        # entire-mount ownership rewrite of unrelated siblings.
        with _Fixture() as fx:
            fx.write_rar("loose.rar")
            (fx.watch_root / "unrelated.mkv").write_text("keep")
            chown_calls: list[str] = []
            extractor = Extractor(
                fx.config(extract_owner="99:100"),
                tool=FakeTool(),
                chown=lambda path, uid, gid: chown_calls.append(path),
            )

            extractor.extract_all((fx.watch_root,), dry_run=False)

            chowned = set(chown_calls)
            self.assertIn(str(fx.watch_root / "loose.mkv"), chowned)  # extracted output
            self.assertNotIn(str(fx.watch_root / "unrelated.mkv"), chowned)
            self.assertNotIn(str(fx.watch_root / "loose.rar"), chowned)

    def test_ownership_never_follows_symlinks(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            external = Path(fx._tmp.name) / "external.txt"  # out of the watch tree
            external.write_text("external")

            class LinkTool(FakeTool):
                def extract(self, archive: Path, dest_dir: Path) -> None:
                    super().extract(archive, dest_dir)
                    os.symlink(external, dest_dir / "link.txt")

            chown_calls: list[str] = []
            extractor = Extractor(
                fx.config(extract_owner="99:100"),
                tool=LinkTool(),
                chown=lambda path, uid, gid: chown_calls.append(path),
            )

            extractor.extract_all((fx.watch_root,), dry_run=False)

            chowned = set(chown_calls)
            self.assertIn(str(fx.watch_root / "rel" / "movie.mkv"), chowned)
            self.assertNotIn(str(fx.watch_root / "rel" / "link.txt"), chowned)

    def test_chown_failure_is_non_fatal(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")

            def _boom(path: str, uid: int, gid: int) -> None:
                raise PermissionError("operation not permitted")

            extractor = Extractor(
                fx.config(extract_owner="99:100"), tool=FakeTool(), chown=_boom
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])

    def test_invalid_owner_skips_chown(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("rel/movie.rar")
            chown_calls: list[object] = []
            extractor = Extractor(
                fx.config(extract_owner="nobody:users"),
                tool=FakeTool(),
                chown=lambda *a: chown_calls.append(a),
            )

            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertEqual(chown_calls, [])

    def test_summarize_counts_statuses(self) -> None:
        with _Fixture() as fx:
            fx.write_rar("a/good.rar")
            fx.write_rar("b/bad.rar")
            tool = FakeTool(fail_names={"bad.rar"})
            extractor = Extractor(fx.config(), tool=tool)

            counts = summarize(extractor.extract_all((fx.watch_root,), dry_run=False))

            self.assertEqual(counts["extracted"], 1)
            self.assertEqual(counts["failed"], 1)


class HelperTests(unittest.TestCase):
    def test_derive_list_tool(self) -> None:
        self.assertEqual(_derive_list_tool("unar"), "lsar")
        self.assertEqual(_derive_list_tool("/usr/bin/unar"), "/usr/bin/lsar")
        self.assertIsNone(_derive_list_tool("p7zip"))

    def test_parse_owner(self) -> None:
        self.assertEqual(_parse_owner("99:100"), (99, 100))
        with self.assertRaises(ValueError):
            _parse_owner("99")
        with self.assertRaises(ValueError):
            _parse_owner("nobody:users")


class UnarArchiveToolTests(unittest.TestCase):
    """Exercise command construction and error mapping without the real binary."""

    def _runner(self, returncode: int, *, stderr: str = "", stdout: str = ""):
        calls: list[list[str]] = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

        return runner, calls

    def test_extract_builds_command_and_succeeds(self) -> None:
        runner, calls = self._runner(0)
        tool = UnarArchiveTool("unar", runner=runner)

        tool.extract(Path("/data/rel/movie.rar"), Path("/data/rel"))

        self.assertEqual(len(calls), 1)
        cmd = calls[0]
        self.assertEqual(cmd[0], "unar")
        self.assertIn("-output-directory", cmd)
        self.assertEqual(cmd[cmd.index("-output-directory") + 1], "/data/rel")
        self.assertEqual(cmd[-1], "/data/rel/movie.rar")

    def test_extract_raises_on_nonzero_exit(self) -> None:
        runner, _ = self._runner(2, stderr="Couldn't open archive")
        tool = UnarArchiveTool("unar", runner=runner)

        with self.assertRaises(ExtractorError) as ctx:
            tool.extract(Path("/data/rel/movie.rar"), Path("/data/rel"))
        self.assertIn("Couldn't open archive", str(ctx.exception))

    def test_test_skipped_when_no_list_tool(self) -> None:
        runner, calls = self._runner(1)
        # A non-unar binary yields no derived lsar, so the pre-test is skipped.
        tool = UnarArchiveTool("p7zip", runner=runner)

        self.assertTrue(tool.test(Path("/data/rel/movie.rar")))
        self.assertEqual(calls, [])


class RealBinaryTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("unar"), "unar not installed")
    def test_real_unar_extraction_roundtrip(self) -> None:
        rar = shutil.which("rar")
        if rar is None:
            self.skipTest("no `rar` creator available to build a fixture at runtime")

        with _Fixture() as fx:
            rel = fx.watch_root / "release"
            rel.mkdir()
            payload = rel / "hello.txt"
            payload.write_text("hello from a real rar")
            archive = rel / "sample.rar"
            subprocess.run(
                [rar, "a", "-ep", str(archive), "hello.txt"],
                cwd=str(rel),
                check=True,
                capture_output=True,
            )
            payload.unlink()  # force extraction to re-create it

            extractor = Extractor(
                fx.config(extract_min_age_seconds=0),
                clock=lambda: archive.stat().st_mtime + 10_000,
            )
            results = extractor.extract_all((fx.watch_root,), dry_run=False)

            self.assertEqual([r.status for r in results], ["extracted"])
            self.assertTrue(payload.exists())
            self.assertEqual(payload.read_text(), "hello from a real rar")


if __name__ == "__main__":
    unittest.main()
