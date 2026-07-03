"""Tests for the pure duplicate-analysis engine (no I/O)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import dedupe
from unraid_cache_cleaner.models import DuplicateGroup, MediaCopy

GB = 1_000_000_000


def _copy(
    part_id: int,
    file: str,
    size: int,
    resolution: str = "",
    bitrate: int = 0,
    media_id: int = 0,
) -> MediaCopy:
    return MediaCopy(
        part_id=part_id,
        file=Path(file),
        size=size,
        resolution=resolution,
        bitrate=bitrate,
        media_id=media_id,
    )


def _group(kind: str, title: str, *copies: MediaCopy, rating_key: str = "rk") -> DuplicateGroup:
    return DuplicateGroup(rating_key=rating_key, kind=kind, title=title, copies=tuple(copies))


class ResolutionRankTests(unittest.TestCase):
    def test_known_labels_rank_in_order(self) -> None:
        ranks = [dedupe.resolution_rank(r) for r in ("4k", "1080", "720", "480", "sd")]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertTrue(all(rank > 0 for rank in ranks))

    def test_case_and_trailing_p_normalized(self) -> None:
        self.assertEqual(dedupe.resolution_rank("4K"), dedupe.resolution_rank("4k"))
        self.assertEqual(dedupe.resolution_rank("1080p"), dedupe.resolution_rank("1080"))

    def test_unknown_or_missing_ranks_zero(self) -> None:
        self.assertEqual(dedupe.resolution_rank(""), 0)
        self.assertEqual(dedupe.resolution_rank("potato"), 0)


class RankingTests(unittest.TestCase):
    def test_1080_x265_beats_larger_720(self) -> None:
        # The 2 Broke Girls case: a 1080p x265 copy is *smaller* than the 720p
        # one, so size-only sorting would keep the wrong file.
        group = _group(
            "episode",
            "2 Broke Girls S02E18",
            _copy(1, "/tv/2 Broke Girls {imdb-tt1}/720.mkv", int(0.76 * GB), "720", 3_000),
            _copy(2, "/tv/2 Broke Girls {imdb-tt1}/1080.mkv", int(0.57 * GB), "1080", 5_000),
        )
        keeper = dedupe.rank_copies(group)[0]
        self.assertEqual(keeper.resolution, "1080")
        self.assertEqual(dedupe.analyze_group(group).keeper, keeper)

    def test_bitrate_then_size_break_resolution_ties(self) -> None:
        group = _group(
            "movie",
            "Same res, higher bitrate wins",
            _copy(1, "/m/a {imdb-tt1}/lo.mkv", 900, "1080", 2_000),
            _copy(2, "/m/a {imdb-tt1}/hi.mkv", 800, "1080", 8_000),
        )
        self.assertEqual(dedupe.rank_copies(group)[0].part_id, 2)


class ClassifyTests(unittest.TestCase):
    def test_identical_same_res_and_size(self) -> None:
        group = _group(
            "episode",
            "Redundant copies",
            _copy(1, "/tv/show {imdb-tt9}/a.mkv", 700 * 1_000_000, "1080"),
            _copy(2, "/tv/show {imdb-tt9}/b.mkv", 700 * 1_000_000, "1080"),
        )
        self.assertEqual(dedupe.classify(group), dedupe.IDENTICAL)
        # keep one copy: reclaimable == a single copy's size
        self.assertEqual(dedupe.reclaimable_bytes(group), 700 * 1_000_000)

    def test_upgrade_4k_over_1080(self) -> None:
        group = _group(
            "movie",
            "4k supersedes 1080",
            _copy(1, "/m/film {imdb-tt2}/1080.mkv", 8 * GB, "1080"),
            _copy(2, "/m/film {imdb-tt2}/4k.mkv", 40 * GB, "4k"),
        )
        analyzed = dedupe.analyze_group(group)
        self.assertEqual(analyzed.classification, dedupe.UPGRADE)
        self.assertEqual(analyzed.keeper.resolution, "4k")
        # reclaim the 1080 copy
        self.assertEqual(analyzed.reclaimable_bytes, 8 * GB)

    def test_mismatch_distinct_imdb_ids(self) -> None:
        # TMNT: Plex merged the 1990 and 2014 films into one duplicate group.
        group = _group(
            "movie",
            "Teenage Mutant Ninja Turtles",
            _copy(1, "/m/TMNT (1990) {imdb-tt0100758}/x.mkv", 5 * GB, "1080"),
            _copy(2, "/m/TMNT (2014) {imdb-tt1291150}/y.mkv", 9 * GB, "1080"),
        )
        self.assertEqual(dedupe.classify(group), dedupe.MISMATCH)
        self.assertEqual(dedupe.reclaimable_bytes(group), 0)
        self.assertEqual(dedupe.reclaimable_keep_smallest(group), 0)

    def test_mismatch_across_id_namespaces(self) -> None:
        group = _group(
            "movie",
            "Cross-namespace ids differ",
            _copy(1, "/m/a {imdb-tt5}/x.mkv", 5 * GB, "1080"),
            _copy(2, "/m/b {tmdb-777}/y.mkv", 9 * GB, "1080"),
        )
        self.assertEqual(dedupe.classify(group), dedupe.MISMATCH)

    def test_same_single_id_is_not_mismatch(self) -> None:
        group = _group(
            "movie",
            "Same id on both copies",
            _copy(1, "/m/film {imdb-tt3}/1080.mkv", 8 * GB, "1080"),
            _copy(2, "/m/film {imdb-tt3}/4k.mkv", 40 * GB, "4k"),
        )
        self.assertNotEqual(dedupe.classify(group), dedupe.MISMATCH)

    def test_absent_ids_are_not_mismatch(self) -> None:
        group = _group(
            "movie",
            "No id tags anywhere",
            _copy(1, "/m/film/1080.mkv", 8 * GB, "1080"),
            _copy(2, "/m/film/4k.mkv", 40 * GB, "4k"),
        )
        self.assertEqual(dedupe.classify(group), dedupe.UPGRADE)


class StackTests(unittest.TestCase):
    def test_stacked_single_copy_is_not_a_duplicate(self) -> None:
        # One Media split across two parts (cd1/cd2) — a single logical copy.
        group = _group(
            "movie",
            "Stacked film",
            _copy(1, "/m/film {imdb-tt4}/cd1.mkv", 3 * GB, "1080", media_id=100),
            _copy(2, "/m/film {imdb-tt4}/cd2.mkv", 3 * GB, "1080", media_id=100),
        )
        self.assertEqual(len(dedupe.analyze([group])), 0)

    def test_stack_merges_size_then_ranks_against_a_real_copy(self) -> None:
        group = _group(
            "movie",
            "Stack vs single",
            _copy(1, "/m/film {imdb-tt4}/cd1.mkv", 3 * GB, "1080", media_id=100),
            _copy(2, "/m/film {imdb-tt4}/cd2.mkv", 3 * GB, "1080", media_id=100),
            _copy(3, "/m/film {imdb-tt4}/single.mkv", 5 * GB, "1080", media_id=200),
        )
        # Two logical copies: the 6 GB stack and the 5 GB single.
        ranked = dedupe.rank_copies(group)
        self.assertEqual(len(ranked), 2)
        # Same resolution but different sizes (6 GB stack vs 5 GB single) => upgrade,
        # and the merged stack is the keeper.
        self.assertEqual(dedupe.classify(group), dedupe.UPGRADE)
        self.assertEqual(ranked[0].size, 6 * GB)
        self.assertEqual(dedupe.reclaimable_bytes(group), 5 * GB)


class SummarizeTests(unittest.TestCase):
    def _probe_fixture(self) -> list:
        """A small fixture mirroring the 2026-07-01 probe's shape."""

        groups = []
        # Movies: 5 upgrades + 2 mismatches (TMNT, WALL-E).
        for i in range(5):
            groups.append(
                _group(
                    "movie",
                    f"Movie upgrade {i}",
                    _copy(1, f"/m/mv{i} {{imdb-tt{i}}}/1080.mkv", 8 * GB, "1080"),
                    _copy(2, f"/m/mv{i} {{imdb-tt{i}}}/4k.mkv", 40 * GB, "4k"),
                    rating_key=f"mv{i}",
                )
            )
        for name, a, b in (("TMNT", "tt0100758", "tt1291150"), ("WALL-E", "tt0910970", "tt6017060")):
            groups.append(
                _group(
                    "movie",
                    name,
                    _copy(1, f"/m/{name} {{imdb-{a}}}/x.mkv", 5 * GB, "1080"),
                    _copy(2, f"/m/{name} {{imdb-{b}}}/y.mkv", 9 * GB, "1080"),
                    rating_key=name,
                )
            )
        # TV: 3 identical + 4 upgrades (scaled-down stand-ins for 50 + 181).
        for i in range(3):
            groups.append(
                _group(
                    "episode",
                    f"Identical ep {i}",
                    _copy(1, f"/tv/ep{i} {{imdb-tt{i}}}/a.mkv", 2 * GB, "1080"),
                    _copy(2, f"/tv/ep{i} {{imdb-tt{i}}}/b.mkv", 2 * GB, "1080"),
                    rating_key=f"idep{i}",
                )
            )
        for i in range(4):
            groups.append(
                _group(
                    "episode",
                    f"Upgrade ep {i}",
                    _copy(1, f"/tv/up{i} {{imdb-tt{i}}}/720.mkv", 1 * GB, "720"),
                    _copy(2, f"/tv/up{i} {{imdb-tt{i}}}/1080.mkv", 3 * GB, "1080"),
                    rating_key=f"upep{i}",
                )
            )
        return groups

    def test_summary_excludes_mismatch_and_splits_by_section(self) -> None:
        summary = dedupe.summarize(self._probe_fixture())

        by_kind = {section.kind: section for section in summary.sections}
        movies = by_kind["movie"]
        tv = by_kind["episode"]

        # Movies: 7 groups, 5 upgrade + 2 mismatch, mismatch bytes excluded.
        self.assertEqual(movies.group_count, 7)
        self.assertEqual(movies.upgrade_count, 5)
        self.assertEqual(movies.mismatch_count, 2)
        self.assertEqual(movies.reclaimable_bytes, 5 * (8 * GB))  # 5 * 1080 copy

        # TV: 3 identical + 4 upgrade, no mismatches.
        self.assertEqual(tv.identical_count, 3)
        self.assertEqual(tv.upgrade_count, 4)
        self.assertEqual(tv.mismatch_count, 0)
        self.assertEqual(tv.reclaimable_bytes, 3 * (2 * GB) + 4 * (1 * GB))

        # Overall totals are the section sums; mismatch reclaim is zero.
        self.assertEqual(summary.group_count, 14)
        self.assertEqual(summary.mismatch_count, 2)
        self.assertEqual(
            summary.reclaimable_bytes, movies.reclaimable_bytes + tv.reclaimable_bytes
        )
        # keep-smallest never dips below keep-best, and still excludes mismatch.
        self.assertGreaterEqual(
            summary.reclaimable_keep_smallest, summary.reclaimable_bytes
        )

    def test_summarize_drops_non_duplicates(self) -> None:
        stacked_single = _group(
            "movie",
            "Stacked single",
            _copy(1, "/m/film {imdb-tt8}/cd1.mkv", 3 * GB, "1080", media_id=1),
            _copy(2, "/m/film {imdb-tt8}/cd2.mkv", 3 * GB, "1080", media_id=1),
        )
        summary = dedupe.summarize([stacked_single])
        self.assertEqual(summary.group_count, 0)


class ImmutabilityTests(unittest.TestCase):
    def test_analyze_does_not_mutate_input(self) -> None:
        group = _group(
            "movie",
            "Untouched",
            _copy(1, "/m/film {imdb-tt2}/1080.mkv", 8 * GB, "1080"),
            _copy(2, "/m/film {imdb-tt2}/4k.mkv", 40 * GB, "4k"),
        )
        dedupe.analyze_group(group)
        self.assertEqual(group.keeper, None)
        self.assertEqual(group.classification, "")
        self.assertEqual(group.reclaimable_bytes, 0)


if __name__ == "__main__":
    unittest.main()
