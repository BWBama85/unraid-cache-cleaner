"""Tests for the optional content-hash confirmation pass (#9)."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import dedupe, hasher
from unraid_cache_cleaner.models import DuplicateGroup, MediaCopy

_MIB = 1024 * 1024


def _copy(part_id: int, plex_name: str, size: int, *, media_id: int = 0) -> MediaCopy:
    # A distinct media_id per part keeps each copy a standalone logical copy; a
    # shared non-zero media_id merges parts into one stacked copy.
    return MediaCopy(
        part_id=part_id,
        file=Path("/plex") / plex_name,
        size=size,
        resolution="1080",
        bitrate=1000,
        media_id=media_id,
    )


def _identical_group(*copies: MediaCopy, title: str = "Movie", rating_key: str = "rk") -> DuplicateGroup:
    """Analyze a group whose copies share resolution+size => classified identical."""

    return dedupe.analyze_group(
        DuplicateGroup(rating_key=rating_key, kind="movie", title=title, copies=tuple(copies))
    )


class HashRegionsTests(unittest.TestCase):
    """The pure region planner: `partial` must never plan the whole large file."""

    def test_full_reads_whole_extent(self) -> None:
        self.assertEqual(hasher.hash_regions(100, hasher.HASH_FULL), ((0, 100),))
        self.assertEqual(hasher.hash_regions(60 * 1024 ** 3, hasher.HASH_FULL), ((0, 60 * 1024 ** 3),))

    def test_zero_byte_reads_nothing(self) -> None:
        self.assertEqual(hasher.hash_regions(0, hasher.HASH_FULL), ())
        self.assertEqual(hasher.hash_regions(0, hasher.HASH_PARTIAL), ())

    def test_partial_small_file_reads_whole(self) -> None:
        # <= 2 * 4 MiB is read whole (head and tail would overlap).
        for size in (1, 4 * _MIB, 8 * _MIB):
            self.assertEqual(
                hasher.hash_regions(size, hasher.HASH_PARTIAL), ((0, size),), f"size={size}"
            )

    def test_partial_large_file_reads_only_head_and_tail(self) -> None:
        size = 8 * _MIB + 1
        regions = hasher.hash_regions(size, hasher.HASH_PARTIAL)
        self.assertEqual(regions, ((0, 4 * _MIB), (size - 4 * _MIB, 4 * _MIB)))
        # No overlap, and total bytes read is constant (8 MiB) regardless of size.
        self.assertEqual(sum(length for _off, length in regions), 8 * _MIB)

    def test_partial_60gib_reads_8mib_not_whole(self) -> None:
        size = 60 * 1024 ** 3
        read = sum(length for _off, length in hasher.hash_regions(size, hasher.HASH_PARTIAL))
        self.assertEqual(read, 8 * _MIB)
        self.assertLess(read, size)


class _MediaFixture:
    """Temp media dir + a path map translating /plex/<name> to it."""

    def __init__(self, tmp: Path) -> None:
        self.root = tmp / "media"
        self.root.mkdir()
        self.path_map = ((Path("/plex"), self.root),)

    def write(self, name: str, data: bytes) -> None:
        (self.root / name).write_bytes(data)


class HashCopyTests(unittest.TestCase):
    def test_full_single_part_digest_equals_plain_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            data = b"hello world" * 1000
            fx.write("a.mkv", data)
            result = hasher._hash_copy((_copy(1, "a.mkv", len(data)),), fx.path_map, hasher.HASH_FULL)
            self.assertIsNone(result.error)
            self.assertEqual(result.digest, hashlib.sha256(data).hexdigest())

    def test_partial_ignores_middle_difference(self) -> None:
        # Two files identical in head+tail but differing only in the middle: partial
        # reports the same digest (a false "same"), full reports different digests.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            size = 12 * _MIB
            base = bytearray(os.urandom(size))
            other = bytearray(base)
            other[6 * _MIB] ^= 0xFF  # flip one middle byte only
            fx.write("a.mkv", bytes(base))
            fx.write("b.mkv", bytes(other))
            a, b = _copy(1, "a.mkv", size), _copy(2, "b.mkv", size)
            self.assertEqual(
                hasher._hash_copy((a,), fx.path_map, hasher.HASH_PARTIAL).digest,
                hasher._hash_copy((b,), fx.path_map, hasher.HASH_PARTIAL).digest,
            )
            self.assertNotEqual(
                hasher._hash_copy((a,), fx.path_map, hasher.HASH_FULL).digest,
                hasher._hash_copy((b,), fx.path_map, hasher.HASH_FULL).digest,
            )

    def test_partial_detects_tail_difference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            size = 12 * _MIB
            base = bytearray(os.urandom(size))
            other = bytearray(base)
            other[-1] ^= 0xFF  # flip a byte inside the tail sample
            fx.write("a.mkv", bytes(base))
            fx.write("b.mkv", bytes(other))
            self.assertNotEqual(
                hasher._hash_copy((_copy(1, "a.mkv", size),), fx.path_map, hasher.HASH_PARTIAL).digest,
                hasher._hash_copy((_copy(2, "b.mkv", size),), fx.path_map, hasher.HASH_PARTIAL).digest,
            )

    def test_unmapped_path_is_unhashable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            result = hasher._hash_copy(
                (MediaCopy(part_id=1, file=Path("/elsewhere/a.mkv"), size=10),), fx.path_map, hasher.HASH_FULL
            )
            self.assertIsNone(result.digest)
            self.assertIn("not mapped", result.error)

    def test_missing_file_is_unhashable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            result = hasher._hash_copy((_copy(1, "gone.mkv", 10),), fx.path_map, hasher.HASH_FULL)
            self.assertIsNone(result.digest)
            self.assertIn("not readable", result.error)

    def test_directory_is_unhashable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            (fx.root / "d.mkv").mkdir()
            result = hasher._hash_copy((_copy(1, "d.mkv", 10),), fx.path_map, hasher.HASH_FULL)
            self.assertIsNone(result.digest)
            self.assertIn("not a regular file", result.error)

    def test_symlink_is_unhashable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("real.mkv", b"x" * 10)
            os.symlink(fx.root / "real.mkv", fx.root / "link.mkv")
            result = hasher._hash_copy((_copy(1, "link.mkv", 10),), fx.path_map, hasher.HASH_FULL)
            self.assertIsNone(result.digest)
            self.assertIn("not a regular file", result.error)

    def test_size_drift_is_unhashable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"x" * 10)
            result = hasher._hash_copy((_copy(1, "a.mkv", 999),), fx.path_map, hasher.HASH_FULL)
            self.assertIsNone(result.digest)
            self.assertIn("size changed", result.error)

    def test_symlinked_parent_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            fx = _MediaFixture(tmp)
            outside = tmp / "outside"
            outside.mkdir()
            (outside / "secret.mkv").write_bytes(b"x" * 10)
            # A symlinked directory *inside* the root pointing outside: the real path
            # escapes the mapped root and must be refused.
            os.symlink(outside, fx.root / "sub")
            result = hasher._hash_copy((_copy(1, "sub/secret.mkv", 10),), fx.path_map, hasher.HASH_FULL)
            self.assertIsNone(result.digest)
            self.assertTrue(
                "escapes" in (result.error or "") or "not a regular file" in (result.error or ""),
                result.error,
            )


class ConfirmGroupsTests(unittest.TestCase):
    def _fixture(self, tmpdir: str) -> _MediaFixture:
        return _MediaFixture(Path(tmpdir))

    def test_off_returns_groups_unchanged_and_no_io(self) -> None:
        # HASH_MODE=off must not touch the filesystem: a group whose files do not
        # even exist is returned untouched with no warning.
        group = _identical_group(_copy(1, "a.mkv", 10), _copy(2, "b.mkv", 10))
        out, warnings = hasher.confirm_groups([group], (), hasher.HASH_OFF)
        self.assertEqual(out, [group])
        self.assertEqual(warnings, [])

    def test_confirmed_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].classification, dedupe.IDENTICAL)
            self.assertEqual(out[0].hash_status, hasher.CONFIRMED)
            self.assertEqual(out[0].reclaimable_bytes, group.reclaimable_bytes)
            self.assertEqual(warnings, [])

    def test_partial_match_is_sample_match_not_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_PARTIAL)
            self.assertEqual(out[0].hash_status, hasher.SAMPLE_MATCH)

    def test_different_content_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"Y" * 100)  # same size, different bytes
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))
            self.assertGreater(group.reclaimable_bytes, 0)
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].classification, dedupe.DIFFERENT)
            self.assertEqual(out[0].hash_status, hasher.DIFFERENT)
            self.assertEqual(out[0].reclaimable_bytes, 0)
            self.assertEqual(out[0].reclaimable_keep_smallest, 0)
            self.assertFalse(dedupe.is_reclaimable(out[0].classification))
            self.assertTrue(warnings)

    def test_three_copies_one_differs_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            fx.write("c.mkv", b"Z" * 100)
            group = _identical_group(
                _copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100), _copy(3, "c.mkv", 100)
            )
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].classification, dedupe.DIFFERENT)

    def test_one_unhashable_member_leaves_group_size_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)  # b.mkv missing
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].classification, dedupe.IDENTICAL)
            self.assertEqual(out[0].hash_status, hasher.UNHASHABLE)
            self.assertEqual(out[0].reclaimable_bytes, group.reclaimable_bytes)  # unchanged
            self.assertTrue(any("unhashable" in w for w in warnings))

    def test_one_bad_group_does_not_abort_others(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)  # good group confirms
            fx.write("c.mkv", b"X" * 100)  # d.mkv missing -> unhashable group
            good = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100), rating_key="g")
            bad = _identical_group(_copy(3, "c.mkv", 100), _copy(4, "d.mkv", 100), rating_key="b")
            out, _ = hasher.confirm_groups([good, bad], fx.path_map, hasher.HASH_FULL)
            by_key = {g.rating_key: g for g in out}
            self.assertEqual(by_key["g"].hash_status, hasher.CONFIRMED)
            self.assertEqual(by_key["b"].hash_status, hasher.UNHASHABLE)

    def test_upgrade_and_mismatch_groups_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            # An upgrade group (different sizes) is never hashed.
            fx.write("big.mkv", b"X" * 200)
            fx.write("small.mkv", b"X" * 100)
            upgrade = dedupe.analyze_group(
                DuplicateGroup(
                    rating_key="u", kind="movie", title="Up",
                    copies=(_copy(1, "big.mkv", 200), _copy(2, "small.mkv", 100)),
                )
            )
            self.assertEqual(upgrade.classification, dedupe.UPGRADE)
            out, warnings = hasher.confirm_groups([upgrade], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].hash_status, "")
            self.assertEqual(out[0].classification, dedupe.UPGRADE)
            self.assertEqual(warnings, [])

    def test_stacked_copies_confirm(self) -> None:
        # Two logical copies each split into two parts (same topology + same bytes).
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            for name in ("a1.mkv", "a2.mkv", "b1.mkv", "b2.mkv"):
                fx.write(name, b"X" * 50 if name.endswith("1.mkv") else b"Y" * 70)
            copy_a = (_copy(1, "a1.mkv", 50, media_id=10), _copy(2, "a2.mkv", 70, media_id=10))
            copy_b = (_copy(3, "b1.mkv", 50, media_id=20), _copy(4, "b2.mkv", 70, media_id=20))
            group = _identical_group(*copy_a, *copy_b)
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].hash_status, hasher.CONFIRMED)
            self.assertEqual(warnings, [])

    def test_different_part_topology_is_unhashable(self) -> None:
        # Same total size, different split => not comparable => size-only, never a
        # false different-content.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a1.mkv", b"X" * 60)
            fx.write("a2.mkv", b"Y" * 60)
            fx.write("b.mkv", b"X" * 60 + b"Y" * 60)
            copy_a = (_copy(1, "a1.mkv", 60, media_id=10), _copy(2, "a2.mkv", 60, media_id=10))
            copy_b = (_copy(3, "b.mkv", 120, media_id=20),)
            group = _identical_group(*copy_a, *copy_b)
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].hash_status, hasher.UNHASHABLE)
            self.assertEqual(out[0].classification, dedupe.IDENTICAL)
            self.assertTrue(any("part layouts" in w for w in warnings))

    def test_empty_path_map_skips_with_warning(self) -> None:
        group = _identical_group(_copy(1, "a.mkv", 10), _copy(2, "b.mkv", 10))
        out, warnings = hasher.confirm_groups([group], (), hasher.HASH_FULL)
        self.assertEqual(out[0].hash_status, "")  # untouched
        self.assertTrue(any("WEB_MEDIA_PATH_MAP is not set" in w for w in warnings))

    def test_unmounted_root_skips_with_warning(self) -> None:
        # A path map whose container root does not exist => skip the pass, warn.
        path_map = ((Path("/plex"), Path("/nonexistent-root-xyz")),)
        group = _identical_group(_copy(1, "a.mkv", 10), _copy(2, "b.mkv", 10))
        out, warnings = hasher.confirm_groups([group], path_map, hasher.HASH_FULL)
        self.assertEqual(out[0].hash_status, "")
        self.assertTrue(any("mounted here" in w for w in warnings))

    def test_zero_byte_copies_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"")
            fx.write("b.mkv", b"")
            group = _identical_group(_copy(1, "a.mkv", 0), _copy(2, "b.mkv", 0))
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].hash_status, hasher.CONFIRMED)


if __name__ == "__main__":
    unittest.main()
