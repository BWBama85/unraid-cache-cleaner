"""Tests for the optional content-hash confirmation pass (#9)."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import dedupe, hasher
from unraid_cache_cleaner.models import DuplicateGroup, HashBucket, MediaCopy
from unraid_cache_cleaner.state import HashCache

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


def _res_copy(
    part_id: int, plex_name: str, size: int, resolution: str, *, media_id: int = 0
) -> MediaCopy:
    """A copy at an explicit resolution — what makes a group an ``upgrade`` (#93)."""

    return MediaCopy(
        part_id=part_id,
        file=Path("/plex") / plex_name,
        size=size,
        resolution=resolution,
        bitrate=1000,
        media_id=media_id,
    )


def _identical_group(*copies: MediaCopy, title: str = "Movie", rating_key: str = "rk") -> DuplicateGroup:
    """Analyze a group whose copies share resolution+size => classified identical."""

    return dedupe.analyze_group(
        DuplicateGroup(rating_key=rating_key, kind="movie", title=title, copies=tuple(copies))
    )


def _upgrade_group(
    *copies: MediaCopy, title: str = "Movie", rating_key: str = "rk"
) -> DuplicateGroup:
    """Analyze a group whose copies differ in resolution/size => classified upgrade."""

    group = dedupe.analyze_group(
        DuplicateGroup(rating_key=rating_key, kind="movie", title=title, copies=tuple(copies))
    )
    assert group.classification == dedupe.UPGRADE, group.classification
    return group


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

    def test_short_read_is_unhashable(self) -> None:
        # A file that passes the size check but yields fewer bytes than expected
        # (truncated/replaced mid-hash) must fail closed as unhashable, never return
        # a digest over the prefix. Force it by planning a region past EOF.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            with mock.patch.object(hasher, "hash_regions", return_value=((0, 200),)):
                result = hasher._hash_copy((_copy(1, "a.mkv", 100),), fx.path_map, hasher.HASH_FULL)
            self.assertIsNone(result.digest)
            self.assertIn("short read", result.error)

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
            # An upgrade group never earns a group-wide verdict. This one holds no
            # same-size bucket either, so the bucket pass (#93) leaves it completely
            # alone — see UpgradeBucketTests for the case where it does have one.
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


class UpgradeBucketTests(unittest.TestCase):
    """Same-size bucket confirmation inside ``upgrade`` groups (#93).

    The invariant every test here re-checks: an ``upgrade`` is **annotated, never
    re-decided**. Its classification, keeper, and both reclaim figures must come out
    exactly as ``dedupe.analyze_group`` computed them, whatever the bytes say.
    """

    def _fixture(self, tmpdir: str) -> _MediaFixture:
        return _MediaFixture(Path(tmpdir))

    def _assert_untouched(self, out: DuplicateGroup, original: DuplicateGroup) -> None:
        """The reclaim contract: hashing an upgrade moves nothing but ``hash_buckets``."""

        self.assertEqual(out.classification, dedupe.UPGRADE)
        self.assertEqual(out.keeper, original.keeper)
        self.assertEqual(out.reclaimable_bytes, original.reclaimable_bytes)
        self.assertEqual(out.reclaimable_keep_smallest, original.reclaimable_keep_smallest)
        self.assertEqual(out.hash_status, "")  # the group-wide verdict stays unset
        self.assertTrue(dedupe.is_reclaimable(out.classification))

    def test_same_size_pair_confirmed_and_keeper_untouched(self) -> None:
        # 1080p x2 at an identical size (the redundant pair) + a 720p at another size.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            fx.write("old.mkv", b"Y" * 50)
            group = _upgrade_group(
                _res_copy(1, "a.mkv", 100, "1080"),
                _res_copy(2, "b.mkv", 100, "1080"),
                _res_copy(3, "old.mkv", 50, "720"),
            )
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(
                out[0].hash_buckets,
                (
                    HashBucket(
                        size=100,
                        status=hasher.CONFIRMED,
                        copy_count=2,
                        redundant_count=2,
                        part_ids=(1, 2),
                    ),
                ),
            )
            self._assert_untouched(out[0], group)
            # Informational only: an upgrade bucket never adds to the warning list,
            # which is reserved for findings that bear on reclaim safety.
            self.assertEqual(warnings, [])

    def test_singleton_size_is_never_read(self) -> None:
        # The cost guarantee: a size unique within the group can hold no redundancy, so
        # its bytes must never be touched. ``hash_regions`` runs once per part on the
        # read path, so the sizes it sees are exactly the files that were read.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            fx.write("old.mkv", b"Y" * 50)
            group = _upgrade_group(
                _res_copy(1, "a.mkv", 100, "1080"),
                _res_copy(2, "b.mkv", 100, "1080"),
                _res_copy(3, "old.mkv", 50, "720"),
            )
            reads: list[int] = []
            real = hasher.hash_regions

            def counting(size, mode):
                reads.append(size)
                return real(size, mode)

            with mock.patch.object(hasher, "hash_regions", counting):
                hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(reads, [100, 100])
            self.assertNotIn(50, reads)

    def test_upgrade_with_no_same_size_bucket_reads_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("big.mkv", b"X" * 200)
            fx.write("small.mkv", b"X" * 100)
            group = _upgrade_group(
                _res_copy(1, "big.mkv", 200, "1080"),
                _res_copy(2, "small.mkv", 100, "720"),
            )
            reads: list[int] = []
            real = hasher.hash_regions

            def counting(size, mode):
                reads.append(size)
                return real(size, mode)

            with mock.patch.object(hasher, "hash_regions", counting):
                out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(reads, [])
            self.assertEqual(out[0].hash_buckets, ())
            self.assertEqual(warnings, [])
            self._assert_untouched(out[0], group)

    def test_partial_mode_pair_is_sample_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            fx.write("old.mkv", b"Y" * 50)
            group = _upgrade_group(
                _res_copy(1, "a.mkv", 100, "1080"),
                _res_copy(2, "b.mkv", 100, "1080"),
                _res_copy(3, "old.mkv", 50, "720"),
            )
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_PARTIAL)
            self.assertEqual(out[0].hash_buckets[0].status, hasher.SAMPLE_MATCH)
            self.assertEqual(out[0].hash_buckets[0].redundant_count, 2)

    def test_same_size_different_bytes_is_informational_not_protective(self) -> None:
        # The deliberate asymmetry with an ``identical`` group: differing bytes here are
        # expected (a 720p and a 1080p sharing a size), so the group must NOT be
        # downgraded to different-content or dropped from the reclaimable set.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("hd.mkv", b"X" * 100)
            fx.write("sd.mkv", b"Y" * 100)  # same size, different bytes, worse res
            group = _upgrade_group(
                _res_copy(1, "hd.mkv", 100, "1080"),
                _res_copy(2, "sd.mkv", 100, "720"),
            )
            self.assertGreater(group.reclaimable_bytes, 0)
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].hash_buckets[0].status, hasher.DIFFERENT)
            self.assertEqual(out[0].hash_buckets[0].redundant_count, 0)
            self.assertNotEqual(out[0].classification, dedupe.DIFFERENT)
            self._assert_untouched(out[0], group)
            self.assertEqual(warnings, [])

    def test_mixed_cluster_reports_the_redundant_pair(self) -> None:
        # Three copies at one size: two identical + one odd. The bucket does not all
        # agree (=> different), but two copies are still provably redundant — the case a
        # single all-or-nothing verdict would hide.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            fx.write("c.mkv", b"Z" * 100)
            group = _upgrade_group(
                _res_copy(1, "a.mkv", 100, "1080"),
                _res_copy(2, "b.mkv", 100, "1080"),
                _res_copy(3, "c.mkv", 100, "720"),
            )
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            bucket = out[0].hash_buckets[0]
            self.assertEqual(bucket.status, hasher.DIFFERENT)
            self.assertEqual(bucket.redundant_count, 2)
            self.assertEqual(bucket.copy_count, 3)
            self.assertEqual(dedupe.redundant_bucket_copies(out[0]), 2)
            self._assert_untouched(out[0], group)

    def test_several_buckets_in_one_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)  # bucket 100: redundant pair
            fx.write("c.mkv", b"Y" * 50)
            fx.write("d.mkv", b"Z" * 50)  # bucket 50: same size, different bytes
            group = _upgrade_group(
                _res_copy(1, "a.mkv", 100, "1080"),
                _res_copy(2, "b.mkv", 100, "1080"),
                _res_copy(3, "c.mkv", 50, "720"),
                _res_copy(4, "d.mkv", 50, "720"),
            )
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(
                [(b.size, b.status, b.redundant_count) for b in out[0].hash_buckets],
                [(100, hasher.CONFIRMED, 2), (50, hasher.DIFFERENT, 0)],
            )
            # Buckets are ordered best-first, matching the group's copy ranking.
            self.assertEqual(dedupe.redundant_bucket_copies(out[0]), 2)
            self._assert_untouched(out[0], group)

    def test_unreadable_member_is_unhashable_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)  # b.mkv missing
            fx.write("old.mkv", b"Y" * 50)
            group = _upgrade_group(
                _res_copy(1, "a.mkv", 100, "1080"),
                _res_copy(2, "b.mkv", 100, "1080"),
                _res_copy(3, "old.mkv", 50, "720"),
            )
            out, warnings = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            bucket = out[0].hash_buckets[0]
            self.assertEqual(bucket.status, hasher.UNHASHABLE)
            self.assertEqual(bucket.redundant_count, 0)  # nothing was proven
            self._assert_untouched(out[0], group)
            self.assertEqual(warnings, [])

    def test_incompatible_part_topology_is_unhashable_bucket(self) -> None:
        # Two stacked copies with the same total size but different splits: not
        # comparable, so no verdict — never a false "redundant".
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a1.mkv", b"X" * 50)
            fx.write("a2.mkv", b"Y" * 70)
            fx.write("b1.mkv", b"X" * 60)
            fx.write("b2.mkv", b"Y" * 60)
            fx.write("old.mkv", b"Z" * 40)
            group = _upgrade_group(
                _res_copy(1, "a1.mkv", 50, "1080", media_id=10),
                _res_copy(2, "a2.mkv", 70, "1080", media_id=10),
                _res_copy(3, "b1.mkv", 60, "1080", media_id=20),
                _res_copy(4, "b2.mkv", 60, "1080", media_id=20),
                _res_copy(5, "old.mkv", 40, "720"),
            )
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            bucket = out[0].hash_buckets[0]
            self.assertEqual(bucket.size, 120)  # both stacks merge to one logical size
            self.assertEqual(bucket.status, hasher.UNHASHABLE)
            self.assertEqual(bucket.redundant_count, 0)
            self._assert_untouched(out[0], group)

    def test_stacked_copies_with_same_topology_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            for name in ("a1.mkv", "b1.mkv"):
                fx.write(name, b"X" * 50)
            for name in ("a2.mkv", "b2.mkv"):
                fx.write(name, b"Y" * 70)
            fx.write("old.mkv", b"Z" * 40)
            group = _upgrade_group(
                _res_copy(1, "a1.mkv", 50, "1080", media_id=10),
                _res_copy(2, "a2.mkv", 70, "1080", media_id=10),
                _res_copy(3, "b1.mkv", 50, "1080", media_id=20),
                _res_copy(4, "b2.mkv", 70, "1080", media_id=20),
                _res_copy(5, "old.mkv", 40, "720"),
            )
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            bucket = out[0].hash_buckets[0]
            self.assertEqual(bucket.size, 120)
            self.assertEqual(bucket.status, hasher.CONFIRMED)
            self.assertEqual(bucket.redundant_count, 2)
            # Members are addressed by each logical copy's part_id (a stack's first part).
            self.assertEqual(bucket.part_ids, (1, 3))

    def test_off_mode_leaves_upgrade_unbucketed(self) -> None:
        group = _upgrade_group(
            _res_copy(1, "a.mkv", 100, "1080"),
            _res_copy(2, "b.mkv", 100, "1080"),
            _res_copy(3, "old.mkv", 50, "720"),
        )
        out, warnings = hasher.confirm_groups([group], (), hasher.HASH_OFF)
        self.assertEqual(out, [group])
        self.assertEqual(out[0].hash_buckets, ())
        self.assertEqual(warnings, [])

    def test_mismatch_group_is_never_bucketed(self) -> None:
        # A mismatch is protected on identity grounds; no hash verdict could make it
        # safer, so its bytes are never read.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            group = dedupe.analyze_group(
                DuplicateGroup(
                    rating_key="m",
                    kind="movie",
                    title="Mixed",
                    copies=(
                        _res_copy(1, "{imdb-tt1} a.mkv", 100, "1080"),
                        _res_copy(2, "{imdb-tt2} b.mkv", 100, "720"),
                    ),
                )
            )
            self.assertEqual(group.classification, dedupe.MISMATCH)
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL)
            self.assertEqual(out[0].hash_buckets, ())
            self.assertEqual(out[0].classification, dedupe.MISMATCH)

    def test_bucket_digests_are_cached_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = self._fixture(tmpdir)
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            fx.write("old.mkv", b"Y" * 50)
            group = _upgrade_group(
                _res_copy(1, "a.mkv", 100, "1080"),
                _res_copy(2, "b.mkv", 100, "1080"),
                _res_copy(3, "old.mkv", 50, "720"),
            )
            cache_path = Path(tmpdir) / "hc.sqlite3"
            real = hasher.hash_regions

            def run() -> tuple:
                reads: list[int] = []

                def counting(size, mode):
                    reads.append(size)
                    return real(size, mode)

                cache = HashCache(cache_path)
                with mock.patch.object(hasher, "hash_regions", counting):
                    out, _ = hasher.confirm_groups(
                        [group], fx.path_map, hasher.HASH_FULL, cache=cache
                    )
                cache.close()
                return out, reads

            out1, reads1 = run()
            self.assertEqual(reads1, [100, 100])  # cold: the bucket members are read
            out2, reads2 = run()
            self.assertEqual(reads2, [])  # warm: served from cache
            self.assertEqual(out1[0].hash_buckets, out2[0].hash_buckets)
            self.assertEqual(out2[0].hash_buckets[0].status, hasher.CONFIRMED)


class CacheIntegrationTests(unittest.TestCase):
    """The persistent hash cache (#92) wired through confirm_groups.

    ``hasher.hash_regions`` is invoked once per part **only on the read path**, so
    counting its calls is a direct, root-proof proof of whether a copy's bytes were
    actually read or served from cache. (A warm hit still ``open()``s each part for the
    fail-closed readability re-check, so counting ``open`` no longer distinguishes a hit
    from a miss — counting reads does.)
    """

    def _confirm_counting_reads(self, groups, path_map, mode, cache):
        reads: list[int] = []
        real = hasher.hash_regions

        def counting(size, m):
            reads.append(size)
            return real(size, m)

        with mock.patch.object(hasher, "hash_regions", counting):
            out, _ = hasher.confirm_groups(groups, path_map, mode, cache=cache)
        return out, reads

    def test_second_run_reuses_cache_and_skips_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            cache_path = Path(tmpdir) / "hc.sqlite3"
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))

            c1 = HashCache(cache_path)
            out1, reads1 = self._confirm_counting_reads([group], fx.path_map, hasher.HASH_FULL, c1)
            c1.close()
            self.assertEqual(out1[0].hash_status, hasher.CONFIRMED)
            self.assertTrue(reads1)  # cold run reads the media

            # A fresh cache instance models a later report run: same files, every copy
            # served from cache, so not a single content byte is read.
            c2 = HashCache(cache_path)
            out2, reads2 = self._confirm_counting_reads([group], fx.path_map, hasher.HASH_FULL, c2)
            c2.close()
            self.assertEqual(out2[0].hash_status, hasher.CONFIRMED)
            self.assertEqual(reads2, [])

    def test_changed_bytes_invalidate_and_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            cache_path = Path(tmpdir) / "hc.sqlite3"
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))

            c1 = HashCache(cache_path)
            out1, _ = self._confirm_counting_reads([group], fx.path_map, hasher.HASH_FULL, c1)
            c1.close()
            self.assertEqual(out1[0].hash_status, hasher.CONFIRMED)

            # a.mkv is overwritten with different bytes (its mtime naturally moves): the
            # stale cached digest must be discarded and the file re-read, catching the
            # difference and downgrading the group — never a stale 'confirmed'.
            (fx.root / "a.mkv").write_bytes(b"Y" * 100)
            c2 = HashCache(cache_path)
            out2, _ = self._confirm_counting_reads([group], fx.path_map, hasher.HASH_FULL, c2)
            c2.close()
            self.assertEqual(out2[0].classification, dedupe.DIFFERENT)
            self.assertEqual(out2[0].hash_status, hasher.DIFFERENT)

    def test_same_size_and_mtime_changed_bytes_is_reread(self) -> None:
        # The P1 case a (size, mtime) fingerprint alone misses: an overwrite (cp -p /
        # rsync -t / coarse-mtime FS) that preserves size AND mtime but changes bytes.
        # ctime (which userspace cannot forge) still moves, so the group is re-read and
        # downgraded rather than served a stale 'confirmed'.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            cache_path = Path(tmpdir) / "hc.sqlite3"
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))

            c1 = HashCache(cache_path)
            out1, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=c1)
            c1.close()
            self.assertEqual(out1[0].hash_status, hasher.CONFIRMED)

            orig = os.stat(fx.root / "a.mkv")
            (fx.root / "a.mkv").write_bytes(b"Y" * 100)  # different bytes, same size
            os.utime(fx.root / "a.mkv", ns=(orig.st_atime_ns, orig.st_mtime_ns))  # restore mtime
            after = os.stat(fx.root / "a.mkv")
            self.assertEqual(after.st_size, orig.st_size)          # size unchanged
            self.assertEqual(after.st_mtime_ns, orig.st_mtime_ns)  # mtime restored

            c2 = HashCache(cache_path)
            out2, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=c2)
            c2.close()
            self.assertEqual(out2[0].classification, dedupe.DIFFERENT)
            self.assertEqual(out2[0].hash_status, hasher.DIFFERENT)

    def test_cache_hit_reverifies_readability(self) -> None:
        # The P2 case: a warm hit must not skip the readability check. If the media
        # became unopenable while its size/mtime/ctime stayed intact (an ancestor-dir,
        # ACL, or LSM change), the hit fails closed to unhashable, never confirmed.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            cache_path = Path(tmpdir) / "hc.sqlite3"
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))

            c1 = HashCache(cache_path)
            out1, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=c1)
            c1.close()
            self.assertEqual(out1[0].hash_status, hasher.CONFIRMED)

            c2 = HashCache(cache_path)
            real_open = open

            def deny_media(file, *args, **kwargs):
                if str(fx.root) in str(file):
                    raise PermissionError(13, "Permission denied")
                return real_open(file, *args, **kwargs)

            with mock.patch("builtins.open", deny_media):
                out2, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=c2)
            c2.close()
            self.assertEqual(out2[0].hash_status, hasher.UNHASHABLE)

    def test_full_then_partial_does_not_reuse_full_digest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            cache_path = Path(tmpdir) / "hc.sqlite3"
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))

            c1 = HashCache(cache_path)
            hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=c1)
            c1.close()

            # A partial run has a distinct mode key: it must not be served the full
            # digest, so it re-reads (reads > 0) and reports sample-match.
            c2 = HashCache(cache_path)
            out, reads = self._confirm_counting_reads([group], fx.path_map, hasher.HASH_PARTIAL, c2)
            c2.close()
            self.assertEqual(out[0].hash_status, hasher.SAMPLE_MATCH)
            self.assertTrue(reads)

    def test_disabled_cache_still_hashes_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            corrupt = Path(tmpdir) / "hc.sqlite3"
            corrupt.write_bytes(b"not a database" * 5)
            cache = HashCache(corrupt)
            self.assertTrue(cache._disabled)
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))
            out, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=cache)
            cache.close()
            self.assertEqual(out[0].hash_status, hasher.CONFIRMED)

    def test_cache_hit_still_enforces_safety(self) -> None:
        # Even with a warm cache, resolution (path map, regular-file, symlink,
        # root-escape, size) runs first: a copy swapped for a symlink is refused as
        # unhashable, never served a cache-confirmed verdict on an unsafe path.
        with tempfile.TemporaryDirectory() as tmpdir:
            fx = _MediaFixture(Path(tmpdir))
            fx.write("a.mkv", b"X" * 100)
            fx.write("b.mkv", b"X" * 100)
            cache_path = Path(tmpdir) / "hc.sqlite3"
            group = _identical_group(_copy(1, "a.mkv", 100), _copy(2, "b.mkv", 100))
            c1 = HashCache(cache_path)
            out1, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=c1)
            c1.close()
            self.assertEqual(out1[0].hash_status, hasher.CONFIRMED)

            outside = Path(tmpdir) / "outside"
            outside.mkdir()
            (outside / "evil.mkv").write_bytes(b"Z" * 100)  # same size as expected
            (fx.root / "a.mkv").unlink()
            os.symlink(outside / "evil.mkv", fx.root / "a.mkv")
            c2 = HashCache(cache_path)
            out2, _ = hasher.confirm_groups([group], fx.path_map, hasher.HASH_FULL, cache=c2)
            c2.close()
            self.assertEqual(out2[0].hash_status, hasher.UNHASHABLE)


if __name__ == "__main__":
    unittest.main()
