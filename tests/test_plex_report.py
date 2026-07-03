"""Tests for the Plex duplicate parser and report orchestrator."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import dedupe
from unraid_cache_cleaner.config import Config
from unraid_cache_cleaner.models import PlexSection
from unraid_cache_cleaner.plex import build_duplicate_group
from unraid_cache_cleaner.plex_report import PlexDuplicateReporter


# --------------------------------------------------------------------------- #
# Raw-Plex payload builders                                                    #
# --------------------------------------------------------------------------- #

def _part(part_id: int, file: str, size: int) -> dict:
    part: dict = {"id": part_id, "file": file}
    if size is not None:
        part["size"] = size
    return part


def _media(media_id: int, resolution: str, bitrate: int, parts: list) -> dict:
    media: dict = {"id": media_id, "Part": parts}
    if resolution is not None:
        media["videoResolution"] = resolution
    if bitrate is not None:
        media["bitrate"] = bitrate
        media["videoCodec"] = "h264"
        media["container"] = "mkv"
    return media


def _movie(rating_key: str, title: str, medias: list, *, year: int = 2020, guids=None) -> dict:
    item: dict = {"ratingKey": rating_key, "type": "movie", "title": title, "year": year, "Media": medias}
    if guids:
        item["Guid"] = [{"id": g} for g in guids]
    return item


def _episode(rating_key: str, show: str, season: int, episode: int, title: str, medias: list) -> dict:
    return {
        "ratingKey": rating_key,
        "type": "episode",
        "title": title,
        "grandparentTitle": show,
        "parentIndex": season,
        "index": episode,
        "Media": medias,
    }


# A real reclaimable movie: keep the 4k copy (20 GiB), reclaim the 1080p (8 GiB).
GiB = 1024 ** 3
_MOVIE_UPGRADE = _movie(
    "100",
    "Big Movie",
    [
        _media(1, "4k", 20000, [_part(11, "/movies/Big Movie (2020)/big.4k.mkv", 20 * GiB)]),
        _media(2, "1080", 9000, [_part(12, "/movies/Big Movie (2020)/big.1080.mkv", 8 * GiB)]),
    ],
)

# Plex merged two different films (different imdb ids in the paths) -> mismatch.
_MOVIE_MISMATCH = _movie(
    "200",
    "TMNT",
    [
        _media(3, "1080", 9000, [_part(31, "/movies/TMNT (1990) {imdb-tt0100758}/a.mkv", 5 * GiB)]),
        _media(4, "1080", 9000, [_part(41, "/movies/TMNT (2014) {imdb-tt1291150}/b.mkv", 6 * GiB)]),
    ],
)

# A TV episode with two identical copies -> reclaim one.
_EPISODE_IDENTICAL = _episode(
    "300",
    "Some Show",
    2,
    18,
    "The Episode",
    [
        _media(5, "1080", 9000, [_part(51, "/tv/Some Show/S02/a.mkv", 3 * GiB)]),
        _media(6, "1080", 9000, [_part(61, "/tv/Some Show/S02/b.mkv", 3 * GiB)]),
    ],
)


# --------------------------------------------------------------------------- #
# Fake client + config helpers                                                 #
# --------------------------------------------------------------------------- #

class FakePlexClient:
    """Records fetch calls; returns canned sections and duplicates."""

    def __init__(self, sections, duplicates) -> None:
        self._sections = sections
        self._duplicates = duplicates
        self.duplicate_calls: list = []

    def fetch_sections(self):
        return list(self._sections)

    def fetch_duplicates(self, section_id, item_type, *, page_size=200):
        self.duplicate_calls.append((section_id, item_type))
        return list(self._duplicates.get((section_id, item_type), []))


def _config(tmp: Path, **overrides) -> Config:
    base = dict(
        qbittorrent_url="http://qbt:8080",
        qbittorrent_username="",
        qbittorrent_password="",
        qbittorrent_timeout_seconds=15,
        qbittorrent_verify_tls=True,
        watch_paths=(),
        poll_interval_seconds=300,
        orphan_grace_seconds=0,
        min_file_age_seconds=0,
        dry_run=True,
        delete_empty_dirs=True,
        protect_single_file_parent_dirs=True,
        excluded_globs=(),
        state_db_path=tmp / "state.sqlite3",
        report_path=tmp / "last-run.json",
        log_level="INFO",
        plex_url="http://plex:32400",
        plex_token="TOKEN",
        plex_sections=(),
        plex_timeout_seconds=30,
        plex_verify_tls=True,
        plex_duplicate_report_path=tmp / "plex-duplicates.json",
    )
    base.update(overrides)
    return Config(**base)


def _reporter(tmp: Path, client: FakePlexClient, *, config=None) -> PlexDuplicateReporter:
    return PlexDuplicateReporter(config or _config(tmp), client, clock=lambda: 1234.5)


# --------------------------------------------------------------------------- #
# Parser tests                                                                 #
# --------------------------------------------------------------------------- #

class ParserTests(unittest.TestCase):
    def test_movie_two_copies(self) -> None:
        group = build_duplicate_group(_MOVIE_UPGRADE, "movie")
        self.assertIsNotNone(group)
        self.assertEqual(group.kind, "movie")
        self.assertEqual(group.title, "Big Movie")
        self.assertEqual(group.year, 2020)
        self.assertEqual(len(group.copies), 2)
        self.assertEqual({c.media_id for c in group.copies}, {1, 2})

    def test_episode_title_and_indices(self) -> None:
        group = build_duplicate_group(_EPISODE_IDENTICAL, "episode")
        self.assertEqual(group.title, "Some Show - S02E18 - The Episode")
        self.assertEqual(group.season, 2)
        self.assertEqual(group.episode, 18)

    def test_guids_become_external_ids(self) -> None:
        item = _movie("1", "M", [_media(1, "1080", 9000, [_part(1, "/m/a.mkv", GiB)])],
                      guids=["imdb://tt123", "tmdb://456"])
        group = build_duplicate_group(item, "movie")
        self.assertEqual(group.external_ids, {"imdb": "tt123", "tmdb": "456"})

    def test_stacked_media_shares_media_id(self) -> None:
        # One Media split across two Parts -> two copies sharing media_id, which
        # the dedupe engine merges into a single logical copy (not a duplicate).
        item = _movie(
            "1",
            "Stacked",
            [_media(7, "1080", 9000, [_part(1, "/m/cd1.mkv", GiB), _part(2, "/m/cd2.mkv", GiB)])],
        )
        group = build_duplicate_group(item, "movie")
        self.assertEqual([c.media_id for c in group.copies], [7, 7])
        self.assertEqual(dedupe.analyze([group]), [])

    def test_idless_media_parts_still_merge(self) -> None:
        # Plex omits the Media id: the two parts must share a synthesized id so
        # they merge into one logical copy (a stacked movie), not two duplicates.
        item = {
            "ratingKey": "1",
            "type": "movie",
            "title": "Stacked",
            "Media": [
                {"videoResolution": "1080", "Part": [
                    {"id": 1, "file": "/m/cd1.mkv", "size": GiB},
                    {"id": 2, "file": "/m/cd2.mkv", "size": GiB},
                ]},
            ],
        }
        group = build_duplicate_group(item, "movie")
        self.assertEqual(len(group.copies), 2)
        self.assertEqual(group.copies[0].media_id, group.copies[1].media_id)
        self.assertNotEqual(group.copies[0].media_id, 0)
        self.assertEqual(dedupe.analyze([group]), [])

    def test_part_without_file_is_skipped(self) -> None:
        item = _movie(
            "1",
            "M",
            [_media(1, "1080", 9000, [_part(1, "", GiB), _part(2, "/m/b.mkv", GiB)])],
        )
        group = build_duplicate_group(item, "movie")
        self.assertEqual(len(group.copies), 1)
        self.assertEqual(str(group.copies[0].file), "/m/b.mkv")

    def test_item_without_parts_returns_none(self) -> None:
        self.assertIsNone(build_duplicate_group(_movie("1", "M", []), "movie"))
        self.assertIsNone(
            build_duplicate_group(_movie("1", "M", [_media(1, "1080", 9000, [])]), "movie")
        )

    def test_missing_numeric_fields_default_to_zero(self) -> None:
        item = {
            "ratingKey": "1",
            "type": "movie",
            "title": "M",
            "Media": [{"id": 1, "Part": [{"id": 1, "file": "/m/a.mkv"}]}],
        }
        group = build_duplicate_group(item, "movie")
        copy = group.copies[0]
        self.assertEqual(copy.size, 0)
        self.assertEqual(copy.bitrate, 0)
        self.assertEqual(copy.resolution, "")
        self.assertIsNone(group.year)


# --------------------------------------------------------------------------- #
# Reporter / rendering tests                                                   #
# --------------------------------------------------------------------------- #

class ReporterTests(unittest.TestCase):
    def _full_client(self) -> FakePlexClient:
        sections = [
            PlexSection(key="1", type="movie", title="Movies"),
            PlexSection(key="2", type="show", title="TV Shows"),
        ]
        duplicates = {
            ("1", 1): [_MOVIE_UPGRADE, _MOVIE_MISMATCH],
            ("2", 4): [_EPISODE_IDENTICAL],
        }
        return FakePlexClient(sections, duplicates)

    def test_generate_and_json_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = self._full_client()
            reporter = _reporter(tmp, client)

            report = reporter.generate()
            reporter.write_report(report)
            payload = json.loads(reporter.config.plex_duplicate_report_path.read_text())

            self.assertEqual(payload["totals"]["duplicate_group_count"], 3)
            self.assertEqual(payload["totals"]["mismatch_count"], 1)
            # 8 GiB (movie upgrade) + 3 GiB (identical episode); mismatch excluded.
            self.assertEqual(payload["totals"]["reclaimable_bytes"], 11 * GiB)
            self.assertEqual(payload["plex_url"], "http://plex:32400")
            self.assertEqual([s["key"] for s in payload["sections"]], ["1", "2"])
            # video sections queried with the right Plex item types
            self.assertEqual(client.duplicate_calls, [("1", 1), ("2", 4)])

    def test_table_has_three_section_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._full_client())
            table = reporter.render_table(reporter.generate())

            self.assertIn("Reclaimable (safe)", table)
            self.assertIn("Review - possible mismatches", table)
            self.assertIn("arr-tracked", table)
            # the rendered table stays ASCII so a non-UTF-8 stdout can't crash print()
            table.encode("ascii")
            # mismatch title appears under review, never as reclaimable
            self.assertIn("TMNT", table)

    def test_mismatch_never_reclaimable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._full_client())
            payload = reporter.build_payload(reporter.generate())

            mismatch = next(g for g in payload["groups"] if g["classification"] == "mismatch")
            self.assertEqual(mismatch["reclaimable_bytes"], 0)

    def test_json_is_byte_identical_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._full_client())
            first = json.dumps(reporter.build_payload(reporter.generate()), indent=2, sort_keys=True)
            second = json.dumps(reporter.build_payload(reporter.generate()), indent=2, sort_keys=True)
            self.assertEqual(first, second)

    def test_limit_caps_table_rows_not_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._full_client())
            report = reporter.generate()

            table = reporter.render_table(report, limit=1)
            self.assertIn("and 1 more", table)  # 2 reclaimable groups, 1 shown
            # JSON keeps every group regardless of the printed limit
            payload = reporter.build_payload(report)
            self.assertEqual(payload["totals"]["duplicate_group_count"], 3)

    def test_zero_duplicates_valid_empty_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = FakePlexClient(
                [PlexSection(key="1", type="movie", title="Movies")], {}
            )
            reporter = _reporter(tmp, client)

            report = reporter.generate()
            reporter.write_report(report)
            payload = json.loads(reporter.config.plex_duplicate_report_path.read_text())

            self.assertEqual(payload["totals"]["duplicate_group_count"], 0)
            self.assertEqual(payload["groups"], [])
            self.assertIn("No duplicate media found", reporter.render_table(report))

    def test_explicit_sections_override_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = self._full_client()
            # config asks for section 2, but the --section override wins
            config = _config(tmp, plex_sections=("2",))
            reporter = _reporter(tmp, client, config=config)

            reporter.generate(section_overrides=["1"])
            self.assertEqual(client.duplicate_calls, [("1", 1)])

    def test_config_sections_used_when_no_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = self._full_client()
            config = _config(tmp, plex_sections=("2",))
            reporter = _reporter(tmp, client, config=config)

            reporter.generate()
            self.assertEqual(client.duplicate_calls, [("2", 4)])

    def test_autodetect_skips_non_video_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sections = [
                PlexSection(key="1", type="movie", title="Movies"),
                PlexSection(key="2", type="show", title="TV Shows"),
                PlexSection(key="3", type="artist", title="Music"),
            ]
            client = FakePlexClient(sections, {})
            reporter = _reporter(tmp, client)

            report = reporter.generate()
            self.assertEqual([s.key for s in report.sections], ["1", "2"])

    def test_invalid_and_non_video_sections_warn_and_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            sections = [
                PlexSection(key="1", type="movie", title="Movies"),
                PlexSection(key="3", type="artist", title="Music"),
            ]
            client = FakePlexClient(sections, self._full_client()._duplicates)
            reporter = _reporter(tmp, client)

            report = reporter.generate(section_overrides=["99", "3"])
            self.assertEqual(client.duplicate_calls, [])
            self.assertEqual(len(report.warnings), 2)
            self.assertTrue(any("99" in w for w in report.warnings))
            self.assertTrue(any("Music" in w for w in report.warnings))

    def test_repeated_section_id_scanned_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = self._full_client()
            reporter = _reporter(tmp, client)

            report = reporter.generate(section_overrides=["1", "1"])
            # scanned once, not twice — no double-counted reclaimable bytes
            self.assertEqual(client.duplicate_calls, [("1", 1)])
            self.assertEqual(report.summary.reclaimable_bytes, 8 * GiB)

    def test_zero_reclaimable_group_is_listed(self) -> None:
        # Two same-resolution copies Plex reports without a size -> identical,
        # reclaimable 0. It must still appear in the table body, not just inflate
        # the header count.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            item = _movie(
                "500",
                "Sizeless",
                [
                    _media(8, "1080", 9000, [_part(81, "/m/a.mkv", 0)]),
                    _media(9, "1080", 9000, [_part(91, "/m/b.mkv", 0)]),
                ],
            )
            client = FakePlexClient(
                [PlexSection(key="1", type="movie", title="Movies")], {("1", 1): [item]}
            )
            reporter = _reporter(tmp, client)

            report = reporter.generate()
            self.assertEqual(report.summary.group_count, 1)
            table = reporter.render_table(report)
            self.assertIn("Sizeless", table)

    def test_warnings_surface_when_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = FakePlexClient([PlexSection(key="1", type="movie", title="Movies")], {})
            reporter = _reporter(tmp, client)

            report = reporter.generate(section_overrides=["99"])
            with self.assertLogs("unraid_cache_cleaner.plex_report", level="WARNING"):
                reporter.log_report(report)
            self.assertIn("warning:", reporter.render_table(report))

    def test_8k_outranks_1080_as_keeper(self) -> None:
        # A non-numeric resolution label ("8k") must still rank above 1080p, or
        # the report would recommend deleting the best copy.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            item = _movie(
                "600",
                "Huge",
                [
                    _media(10, "8k", 80000, [_part(101, "/m/8k.mkv", 40 * GiB)]),
                    _media(11, "1080", 9000, [_part(111, "/m/1080.mkv", 6 * GiB)]),
                ],
            )
            client = FakePlexClient(
                [PlexSection(key="1", type="movie", title="Movies")], {("1", 1): [item]}
            )
            reporter = _reporter(tmp, client)

            group = reporter.generate().groups[0]
            self.assertEqual(group.keeper.resolution, "8k")
            self.assertEqual(group.reclaimable_bytes, 6 * GiB)


if __name__ == "__main__":
    unittest.main()
