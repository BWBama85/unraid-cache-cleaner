"""Tests for the Plex duplicate parser and report orchestrator."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import arr, dedupe
from unraid_cache_cleaner.arr import ArrClientError
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

# A reclaimable movie whose keeper is a stacked 1080p release (cd1 + cd2 under a
# single Plex media_id) worth 14 GiB, superseding a 720p single-file copy (#17).
_MOVIE_STACKED = _movie(
    "900",
    "Stacked Movie",
    [
        _media(
            20,
            "1080",
            9000,
            [
                _part(201, "/movies/Stacked Movie (2020)/cd1.mkv", 5 * GiB),
                _part(202, "/movies/Stacked Movie (2020)/cd2.mkv", 9 * GiB),
            ],
        ),
        _media(21, "720", 4000, [_part(211, "/movies/Stacked Movie (2020)/single.720.mkv", 4 * GiB)]),
    ],
)

# Two DIFFERENT films mis-stacked under one media_id (conflicting {imdb-…} ids)
# -> mismatch. The review must show BOTH physical parts at their true sizes, not
# one 14 GiB merged row (#25).
_MOVIE_STACKED_MISMATCH = _movie(
    "950",
    "Mis-stacked",
    [
        _media(
            30,
            "1080",
            9000,
            [
                _part(301, "/movies/A (1990) {imdb-tt0100758}/cd1.mkv", 5 * GiB),
                _part(302, "/movies/B (2014) {imdb-tt1291150}/cd2.mkv", 9 * GiB),
            ],
        ),
    ],
)

# A movie whose reclaim candidate is a stacked 1080p copy Radarr tracks; the 4k
# single-file copy is the keeper. Both share {tmdb-970} so it is not a mismatch.
_MOVIE_ARR_STACKED = _movie(
    "970",
    "Arr Stacked",
    [
        _media(40, "4k", 20000, [_part(401, "/movies/Arr Stacked {tmdb-970}/best.4k.mkv", 20 * GiB)]),
        _media(
            41,
            "1080",
            9000,
            [
                _part(411, "/movies/Arr Stacked {tmdb-970}/cd1.1080.mkv", 4 * GiB),
                _part(412, "/movies/Arr Stacked {tmdb-970}/cd2.1080.mkv", 5 * GiB),
            ],
        ),
    ],
    guids=["tmdb://970"],
)

# A reclaimable movie whose RECLAIM CANDIDATE is a stacked 1080p release (cd1 +
# cd2 under one media_id); the 4k single-file copy is the keeper. For a Plex-only
# run the reclaimable-safe table must surface both candidate parts at their true
# sizes (#48), and the keeper's file is never listed as reclaimable.
_MOVIE_STACKED_RECLAIM = _movie(
    "800",
    "Reclaim Stacked",
    [
        _media(50, "4k", 20000, [_part(501, "/movies/Reclaim Stacked (2020)/best.4k.mkv", 20 * GiB)]),
        _media(
            51,
            "1080",
            9000,
            [
                _part(511, "/movies/Reclaim Stacked (2020)/cd1.1080.mkv", 5 * GiB),
                _part(512, "/movies/Reclaim Stacked (2020)/cd2.1080.mkv", 6 * GiB),
            ],
        ),
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


class FakeArrClient:
    """Returns a canned tracked index, or raises to simulate an outage."""

    def __init__(self, index, *, raises=None) -> None:
        self._index = index
        self._raises = raises
        self.calls = 0

    def fetch_tracked_index(self):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        # {tmdbId: {basename: id}} for Radarr, {basename: [ids]} for Sonarr (#61) —
        # copy so callers can't mutate ours
        return type(self._index)(self._index)


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


def _reclaimable_section(table: str) -> str:
    """Isolate the 'Reclaimable (safe)' block so an assertion targets only it,
    not the mismatch or arr-tracked sections that follow."""

    return table.split("Reclaimable (safe)", 1)[1].split("Review - possible")[0]


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

    def test_write_report_is_atomic(self) -> None:
        # #34: the report is written via a temp file + os.replace so the web
        # viewer never reads a truncated file. After a write the target is valid
        # JSON and no scratch .tmp sibling is left behind.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._full_client())
            reporter.write_report(reporter.generate())

            target = reporter.config.plex_duplicate_report_path
            json.loads(target.read_text())  # parses cleanly
            leftovers = list(target.parent.glob(f"{target.name}.*.tmp"))
            self.assertEqual(leftovers, [])

    def test_write_report_publishes_readable_mode(self) -> None:
        # The atomic writer uses mkstemp (0600); the published report must be
        # readable (not owner-only) so a separate web-container/host reader can
        # consume it, and must preserve an operator's existing mode on rewrite.
        import stat as _stat

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._full_client())
            target = reporter.config.plex_duplicate_report_path

            reporter.write_report(reporter.generate())  # first write, no prior file
            first_mode = _stat.S_IMODE(target.stat().st_mode)
            self.assertTrue(first_mode & 0o044, oct(first_mode))  # group/other readable

            target.chmod(0o640)  # operator narrows the mode
            reporter.write_report(reporter.generate())  # rewrite must preserve it
            self.assertEqual(_stat.S_IMODE(target.stat().st_mode), 0o640)

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


# --------------------------------------------------------------------------- #
# Radarr/Sonarr association (#8)                                                #
# --------------------------------------------------------------------------- #

# A reclaimable movie carrying a tmdb guid so Radarr can id-anchor it. Both
# paths share {tmdb-700}, so it is not a mismatch.
_MOVIE_ARR = _movie(
    "700",
    "Arr Movie",
    [
        _media(1, "4k", 20000, [_part(11, "/movies/Arr Movie {tmdb-700}/arr.4k.mkv", 20 * GiB)]),
        _media(2, "1080", 9000, [_part(12, "/movies/Arr Movie {tmdb-700}/arr.1080.mkv", 8 * GiB)]),
    ],
    guids=["tmdb://700"],
)


def _arr_reporter(tmp, client, *, radarr=None, sonarr=None, config=None):
    return PlexDuplicateReporter(
        config or _config(tmp),
        client,
        radarr_client=radarr,
        sonarr_client=sonarr,
        clock=lambda: 1234.5,
    )


class ArrAssociationTests(unittest.TestCase):
    def _movie_client(self):
        return FakePlexClient(
            [PlexSection(key="1", type="movie", title="Movies")], {("1", 1): [_MOVIE_ARR]}
        )

    def test_json_carries_association_and_keeper_tracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({"700": {"arr.4k.mkv": 704}})
            reporter = _arr_reporter(tmp, self._movie_client(), radarr=radarr)

            payload = reporter.build_payload(reporter.generate())

            self.assertTrue(payload["arr_enabled"])
            self.assertEqual(radarr.calls, 1)
            group = payload["groups"][0]
            copies = {c["file"].split("/")[-1]: c for c in group["copies"]}
            self.assertEqual(copies["arr.4k.mkv"]["association"], "tracked")
            self.assertEqual(copies["arr.4k.mkv"]["arr_tracked"], "radarr")
            self.assertEqual(copies["arr.1080.mkv"]["association"], "untracked")
            self.assertIsNone(copies["arr.1080.mkv"]["arr_tracked"])
            # each part carries its *arr file id (#61) on an arr-enabled report: the
            # tracked file's movieFile id, null for the untracked sibling.
            self.assertEqual(copies["arr.4k.mkv"]["parts"][0]["arr_file_id"], 704)
            self.assertIsNone(copies["arr.1080.mkv"]["parts"][0]["arr_file_id"])
            self.assertEqual(group["keeper"]["parts"][0]["arr_file_id"], 704)
            # arr-enabled part shape: exactly the base keys plus arr_file_id
            for copy in group["copies"]:
                for part in copy["parts"]:
                    self.assertEqual(set(part), {"part_id", "file", "size", "arr_file_id"})
            # the keeper (best copy) is the tracked 4k one
            self.assertEqual(group["keeper"]["association"], "tracked")
            # reclaim candidate (1080) is untracked -> nothing at re-download risk
            self.assertEqual(payload["totals"]["arr_tracked_reclaimable_count"], 0)

    def test_tracked_reclaim_candidate_flagged_in_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Radarr tracks the worse (1080) copy — the one a reclaim would delete.
            radarr = FakeArrClient({"700": {"arr.1080.mkv": 710}})
            reporter = _arr_reporter(tmp, self._movie_client(), radarr=radarr)

            report = reporter.generate()
            payload = reporter.build_payload(report)
            table = reporter.render_table(report)

            self.assertEqual(payload["totals"]["arr_tracked_reclaimable_count"], 1)
            self.assertIn("[arr:tracked]", table)
            self.assertIn("tracked by radarr", table)
            self.assertIn("re-download", table)
            table.encode("ascii")  # stays ASCII

    def test_unconfigured_is_byte_identical_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _arr_reporter(tmp, self._movie_client())  # no arr clients

            payload = reporter.build_payload(reporter.generate())

            self.assertNotIn("arr_enabled", payload)
            self.assertNotIn("arr_tracked_reclaimable_count", payload["totals"])
            for group in payload["groups"]:
                for copy in group["copies"]:
                    # `parts` (the per-file breakdown, #17) and the `media_id` /
                    # per-part `part_id` delete-target keys (#34) are always
                    # present; a Plex-only run must still carry no arr fields.
                    self.assertEqual(
                        set(copy),
                        {"file", "size", "resolution", "bitrate", "media_id", "parts"},
                    )
                    for part in copy["parts"]:
                        self.assertEqual(set(part), {"part_id", "file", "size"})
            # table shows the not-configured hint, not a stale placeholder
            table = reporter.render_table(reporter.generate())
            self.assertIn("Not configured", table)

    def test_unreachable_arr_warns_and_still_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({}, raises=ArrClientError("boom", status_code=500))
            reporter = _arr_reporter(tmp, self._movie_client(), radarr=radarr)

            report = reporter.generate()
            reporter.write_report(report)
            payload = json.loads(reporter.config.plex_duplicate_report_path.read_text())

            self.assertTrue(payload["arr_enabled"])
            self.assertTrue(any("Radarr association skipped" in w for w in report.warnings))
            # an outage never crashes the report; the copies fall back to unknown
            group = payload["groups"][0]
            self.assertTrue(all(c["association"] == "unknown" for c in group["copies"]))

    def test_episode_tracked_by_sonarr_basename(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = FakePlexClient(
                [PlexSection(key="2", type="show", title="TV")],
                {("2", 4): [_EPISODE_IDENTICAL]},
            )
            # _EPISODE_IDENTICAL copies are /tv/Some Show/S02/a.mkv and b.mkv
            sonarr = FakeArrClient({"a.mkv": [900]})
            reporter = _arr_reporter(tmp, client, sonarr=sonarr)

            payload = reporter.build_payload(reporter.generate())

            copies = {c["file"].split("/")[-1]: c for c in payload["groups"][0]["copies"]}
            self.assertEqual(copies["a.mkv"]["association"], "tracked")
            self.assertEqual(copies["a.mkv"]["arr_tracked"], "sonarr")
            # the extra TV copy is unknown, never falsely labeled untracked/safe
            self.assertEqual(copies["b.mkv"]["association"], "unknown")

    def test_mismatch_copies_never_labeled_untracked_in_json(self) -> None:
        # Radarr tracks the first film's file, but the group is a Plex mismatch
        # (two tmdb ids). No copy may be serialized as untracked/safe.
        mismatch = _movie(
            "800",
            "Mismatch",
            [
                _media(1, "1080", 9000, [_part(81, "/movies/A {tmdb-111}/a.mkv", 5 * GiB)]),
                _media(2, "1080", 9000, [_part(82, "/movies/B {tmdb-222}/b.mkv", 6 * GiB)]),
            ],
            guids=["tmdb://111"],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            client = FakePlexClient(
                [PlexSection(key="1", type="movie", title="Movies")], {("1", 1): [mismatch]}
            )
            radarr = FakeArrClient({"111": {"a.mkv": 111}})
            reporter = _arr_reporter(tmp, client, radarr=radarr)

            payload = reporter.build_payload(reporter.generate())

            group = payload["groups"][0]
            self.assertEqual(group["classification"], "mismatch")
            self.assertTrue(
                all(c["association"] == "unknown" for c in group["copies"]),
                group["copies"],
            )

    def test_no_tracked_reclaim_reports_all_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({"700": {"arr.4k.mkv": 704}})
            reporter = _arr_reporter(tmp, self._movie_client(), radarr=radarr)

            table = reporter.render_table(reporter.generate())
            self.assertIn("all safe to delete", table)

    def test_unknown_reclaim_candidate_not_called_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # tmdb 700 isn't in the index -> every copy unknown, none tracked.
            radarr = FakeArrClient({"999": {"other.mkv": 999}})
            reporter = _arr_reporter(tmp, self._movie_client(), radarr=radarr)

            table = reporter.render_table(reporter.generate())
            self.assertNotIn("all safe to delete", table)
            self.assertIn("verify those before deleting", table)
            self.assertIn("[arr:?]", table)


# --------------------------------------------------------------------------- #
# Stacked multi-part representation (#17 / #25)                                 #
# --------------------------------------------------------------------------- #

class StackedRepresentationTests(unittest.TestCase):
    def _client(self, item) -> FakePlexClient:
        return FakePlexClient(
            [PlexSection(key="1", type="movie", title="Movies")], {("1", 1): [item]}
        )

    def test_stacked_reclaimable_copy_lists_both_parts_in_json(self) -> None:
        # #17: a stacked logical copy exposes each physical file at its true size
        # via `parts`, while the merged size, classification, reclaimable bytes,
        # and logical copy count are unchanged.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED))
            group = reporter.build_payload(reporter.generate())["groups"][0]

            self.assertEqual(group["classification"], "upgrade")
            self.assertEqual(group["reclaimable_bytes"], 4 * GiB)  # reclaim the 720p single
            copies = group["copies"]
            self.assertEqual(len(copies), 2)  # logical count: the stack + the single

            stack = next(c for c in copies if c["file"].endswith("cd1.mkv"))
            self.assertEqual(stack["size"], 14 * GiB)  # summed logical size, unchanged
            self.assertEqual(
                {Path(p["file"]).name: p["size"] for p in stack["parts"]},
                {"cd1.mkv": 5 * GiB, "cd2.mkv": 9 * GiB},
            )

            single = next(c for c in copies if c["file"].endswith("single.720.mkv"))
            self.assertEqual([Path(p["file"]).name for p in single["parts"]], ["single.720.mkv"])
            self.assertEqual(single["parts"][0]["size"], 4 * GiB)

            keeper = group["keeper"]
            self.assertEqual(keeper["size"], 14 * GiB)
            self.assertEqual(
                {Path(p["file"]).name for p in keeper["parts"]}, {"cd1.mkv", "cd2.mkv"}
            )

    def test_delete_target_keys_surfaced(self) -> None:
        # #34 Phase 1 prep: each copy carries its Plex `media_id`, and each part
        # its `part_id`, so the web action layer (Phase 2) has a stable
        # {rating_key, part_id} delete target — including every part of a stack.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED))
            group = reporter.build_payload(reporter.generate())["groups"][0]

            self.assertEqual(group["rating_key"], "900")
            stack = next(c for c in group["copies"] if c["file"].endswith("cd1.mkv"))
            self.assertEqual(stack["media_id"], 20)
            self.assertEqual(
                {p["part_id"]: Path(p["file"]).name for p in stack["parts"]},
                {201: "cd1.mkv", 202: "cd2.mkv"},
            )

            single = next(c for c in group["copies"] if c["file"].endswith("single.720.mkv"))
            self.assertEqual(single["media_id"], 21)
            self.assertEqual([p["part_id"] for p in single["parts"]], [211])

    def test_stacked_json_is_byte_identical_across_runs(self) -> None:
        # #17 acceptance: the richer per-part shape stays deterministic.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED))
            first = json.dumps(reporter.build_payload(reporter.generate()), indent=2, sort_keys=True)
            second = json.dumps(reporter.build_payload(reporter.generate()), indent=2, sort_keys=True)
            self.assertEqual(first, second)

    def test_stacked_mismatch_shows_both_physical_parts_in_json(self) -> None:
        # #25: a mis-stacked pair is serialized as two physical copies at their
        # individual sizes, not one 14 GiB stack-merged copy.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED_MISMATCH))
            group = reporter.build_payload(reporter.generate())["groups"][0]

            self.assertEqual(group["classification"], "mismatch")
            self.assertEqual(group["reclaimable_bytes"], 0)  # reclaim math unchanged
            copies = group["copies"]
            self.assertEqual(len(copies), 2)
            self.assertEqual(
                {Path(c["file"]).name: c["size"] for c in copies},
                {"cd1.mkv": 5 * GiB, "cd2.mkv": 9 * GiB},
            )
            self.assertNotIn(14 * GiB, [c["size"] for c in copies])  # never the merged sum
            for copy in copies:  # each physical copy is its own single part
                self.assertEqual(len(copy["parts"]), 1)
                self.assertEqual(copy["parts"][0]["size"], copy["size"])

    def test_stacked_mismatch_shows_both_physical_parts_in_table(self) -> None:
        # #25: the "Review - possible mismatches" table lists both conflicting
        # files at their true sizes, not a single 14 GiB summed row.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED_MISMATCH))
            table = reporter.render_table(reporter.generate())

            self.assertIn("/movies/A (1990) {imdb-tt0100758}/cd1.mkv", table)
            self.assertIn("/movies/B (2014) {imdb-tt1291150}/cd2.mkv", table)
            self.assertIn("5.0 GiB", table)
            self.assertIn("9.0 GiB", table)
            self.assertNotIn("14.0 GiB", table)  # the collapse bug this fixes
            table.encode("ascii")  # stays ASCII

    def test_stacked_tracked_copy_lists_each_part_in_arr_section(self) -> None:
        # #17 (table) + arr: a stacked reclaim candidate Radarr tracks is listed
        # as each of its physical parts, the count stays per-logical-copy, and the
        # per-copy association survives the richer serialization.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({"970": {"cd1.1080.mkv": 970}})
            reporter = _arr_reporter(tmp, self._client(_MOVIE_ARR_STACKED), radarr=radarr)
            report = reporter.generate()
            payload = reporter.build_payload(report)
            table = reporter.render_table(report)

            # one logical tracked reclaim candidate (the stack), not two parts
            self.assertEqual(payload["totals"]["arr_tracked_reclaimable_count"], 1)

            stack = next(
                c for c in payload["groups"][0]["copies"] if c["file"].endswith("cd1.1080.mkv")
            )
            self.assertEqual(stack["association"], "tracked")
            self.assertEqual(stack["arr_tracked"], "radarr")
            self.assertEqual(
                {Path(p["file"]).name: p["size"] for p in stack["parts"]},
                {"cd1.1080.mkv": 4 * GiB, "cd2.1080.mkv": 5 * GiB},
            )

            # the arr-tracked table lists BOTH parts at their individual sizes
            self.assertIn("cd1.1080.mkv", table)
            self.assertIn("cd2.1080.mkv", table)
            self.assertIn("4.0 GiB", table)
            self.assertIn("5.0 GiB", table)
            self.assertIn("tracked by radarr", table)
            table.encode("ascii")

    def test_stacked_reclaim_candidate_lists_parts_in_reclaimable_table(self) -> None:
        # #48: a Plex-only run surfaces a stacked reclaim candidate's physical
        # parts (path + true per-file size) in the Reclaimable (safe) section,
        # matching the fidelity the JSON already provides.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED_RECLAIM))
            table = reporter.render_table(reporter.generate())
            safe = _reclaimable_section(table)

            self.assertIn("/movies/Reclaim Stacked (2020)/cd1.1080.mkv", safe)
            self.assertIn("/movies/Reclaim Stacked (2020)/cd2.1080.mkv", safe)
            self.assertIn("5.0 GiB", safe)  # cd1 at its true size
            self.assertIn("6.0 GiB", safe)  # cd2 at its true size
            self.assertIn("11.0 GiB", safe)  # summary line reclaimable total unchanged
            # the keeper is kept — its file is never listed as a reclaimable part
            self.assertNotIn("best.4k.mkv", safe)
            table.encode("ascii")  # stays ASCII

    def test_single_file_reclaimable_rows_unchanged(self) -> None:
        # #48: the common single-file case adds no part sub-rows — the reclaimable
        # section stays one summary line per group (no indented file paths).
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_UPGRADE))
            table = reporter.render_table(reporter.generate())
            safe = _reclaimable_section(table)

            self.assertIn("Big Movie", safe)  # summary line present
            self.assertNotIn("big.1080.mkv", safe)  # no per-part sub-row
            self.assertNotIn("/movies/Big Movie", safe)

    def test_arr_enabled_tracked_stacked_candidate_omits_reclaimable_subrows(self) -> None:
        # #56: a *tracked* stacked candidate stays out of Reclaimable (safe) — its
        # parts already appear (with the same fidelity) in the arr-tracked section,
        # so listing them again would double-print the copy. Radarr tracks cd1, so
        # the stacked reclaim candidate is tracked.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({"970": {"cd1.1080.mkv": 970}})
            reporter = _arr_reporter(tmp, self._client(_MOVIE_ARR_STACKED), radarr=radarr)
            table = reporter.render_table(reporter.generate())
            safe = _reclaimable_section(table)

            # compact summary — no indented part paths in THIS section
            self.assertNotIn("cd1.1080.mkv", safe)
            self.assertNotIn("cd2.1080.mkv", safe)
            # they still appear elsewhere (the arr-tracked section)
            self.assertIn("cd1.1080.mkv", table)

    def test_arr_enabled_untracked_stacked_candidate_lists_reclaimable_subrows(self) -> None:
        # #56: with *arr on, an UNTRACKED stacked reclaim candidate must still show
        # its physical parts (path + true per-file size) in Reclaimable (safe), so
        # enabling *arr no longer strips the breakdown for copies it does not track.
        # Radarr tracks the KEEPER (best.4k.mkv), so the cd1/cd2 candidate is untracked.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({"970": {"best.4k.mkv": 704}})
            reporter = _arr_reporter(tmp, self._client(_MOVIE_ARR_STACKED), radarr=radarr)
            report = reporter.generate()
            table = reporter.render_table(report)
            safe = _reclaimable_section(table)

            # the untracked candidate's parts now appear in the safe section
            self.assertIn("cd1.1080.mkv", safe)
            self.assertIn("cd2.1080.mkv", safe)
            self.assertIn("4.0 GiB", safe)  # cd1 at its true size
            self.assertIn("5.0 GiB", safe)  # cd2 at its true size
            # the keeper's file is never listed as a reclaimable part
            self.assertNotIn("best.4k.mkv", safe)
            table.encode("ascii")  # stays ASCII

    def test_arr_enabled_unknown_stacked_candidate_lists_reclaimable_subrows(self) -> None:
        # #56: an *unknown* stacked candidate (here the group's tmdb id is not in
        # Radarr, so every copy falls back to unknown) is also not tracked, so its
        # parts belong in Reclaimable (safe) too — only tracked copies are withheld.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({})  # tmdb 970 absent -> all copies unknown
            reporter = _arr_reporter(tmp, self._client(_MOVIE_ARR_STACKED), radarr=radarr)
            report = reporter.generate()
            table = reporter.render_table(report)
            safe = _reclaimable_section(table)

            self.assertIn("cd1.1080.mkv", safe)
            self.assertIn("cd2.1080.mkv", safe)

    def test_arr_enabled_single_file_reclaimable_rows_unchanged(self) -> None:
        # #56 only adds sub-rows for STACKED candidates; a single-file untracked
        # reclaim candidate stays one summary line (no indented path) on an *arr run.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({"700": {"arr.4k.mkv": 704}})  # keeper tracked
            reporter = _arr_reporter(tmp, self._client(_MOVIE_ARR), radarr=radarr)
            safe = _reclaimable_section(reporter.render_table(reporter.generate()))

            self.assertIn("Arr Movie", safe)  # summary line present
            self.assertNotIn("arr.1080.mkv", safe)  # untracked single-file: no sub-row


# --------------------------------------------------------------------------- #
# rank-once memoization (#19)                                                  #
# --------------------------------------------------------------------------- #

class RankOnceTests(unittest.TestCase):
    """The render path must rank each group at most once per render (#19)."""

    def _client(self, *items) -> FakePlexClient:
        return FakePlexClient(
            [PlexSection(key="1", type="movie", title="Movies")],
            {("1", 1): list(items)},
        )

    def _assert_ranked_once(self, render, *, expected_groups: int) -> None:
        # rank_copies_with_parts is called only by the reporter's per-render memo
        # (summarize/analyze use rank_copies), so spying on it counts exactly the
        # render's ranking work. One id per group and no repeats => once per group.
        real = dedupe.rank_copies_with_parts
        seen: list = []

        def spy(group):
            seen.append(id(group))
            return real(group)

        with mock.patch.object(dedupe, "rank_copies_with_parts", side_effect=spy):
            render()

        self.assertEqual(len(seen), len(set(seen)), "a group was ranked more than once")
        self.assertEqual(len(set(seen)), expected_groups)

    @staticmethod
    def _full_command(reporter, report):
        # Mirror cli.run_plex_duplicates: JSON, then log line, then table — all
        # for the same report, the sequence the memo must rank each group once
        # across (not once per method).
        reporter.write_report(report)
        reporter.log_report(report)
        reporter.render_table(report)

    def test_full_command_ranks_each_group_once_total(self) -> None:
        # #19 (P2): the whole write_report -> log_report -> render_table sequence
        # ranks each group exactly once TOTAL, not once per output method.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED_RECLAIM, _MOVIE_UPGRADE))
            report = reporter.generate()

            self._assert_ranked_once(
                lambda: self._full_command(reporter, report), expected_groups=2
            )

    def test_arr_full_command_ranks_each_group_once_total(self) -> None:
        # The strongest case: with *arr on, one group's ranking feeds the JSON
        # copies, the reclaimable count, the arr tag, the arr-tracked count, and
        # the arr-tracked rows — across three output methods. Still once each.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            radarr = FakeArrClient({"970": {"cd1.1080.mkv": 970}})
            reporter = _arr_reporter(
                tmp, self._client(_MOVIE_ARR_STACKED, _MOVIE_UPGRADE), radarr=radarr
            )
            report = reporter.generate()

            self._assert_ranked_once(
                lambda: self._full_command(reporter, report), expected_groups=2
            )

    def test_new_report_invalidates_memo(self) -> None:
        # A second, independent report must not reuse the first's ranking — the
        # memo is scoped to the report object, and (P1) two groups with an empty
        # Plex rating_key must never collide onto one cache entry.
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(_MOVIE_STACKED_RECLAIM, _MOVIE_UPGRADE))
            reporter.render_table(reporter.generate())  # warm the memo on report 1

            report2 = reporter.generate()  # fresh report, fresh group objects
            self._assert_ranked_once(
                lambda: reporter.render_table(report2), expected_groups=2
            )

    def test_empty_rating_key_groups_do_not_collide(self) -> None:
        # #19 (P1): build_duplicate_group stores a missing Plex ratingKey as "",
        # so two such groups must still rank independently — identity keying keeps
        # group B from rendering group A's file paths.
        # _movie's first arg is the ratingKey; "" reproduces a Plex item that
        # omits it (build_duplicate_group stores the missing key as "").
        blank_a = _movie(
            "", "Alpha",
            [
                _media(1, "4k", 20000, [_part(1, "/m/Alpha/a.4k.mkv", 20 * GiB)]),
                _media(2, "1080", 9000, [_part(2, "/m/Alpha/a.1080.mkv", 8 * GiB)]),
            ],
        )
        blank_b = _movie(
            "", "Beta",
            [
                _media(3, "4k", 20000, [_part(3, "/m/Beta/b.4k.mkv", 10 * GiB)]),
                _media(4, "1080", 9000, [_part(4, "/m/Beta/b.1080.mkv", 4 * GiB)]),
            ],
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            reporter = _reporter(tmp, self._client(blank_a, blank_b))
            groups = {g["title"]: g for g in reporter.build_payload(reporter.generate())["groups"]}

            # each group's copies reference its OWN files, not the other's
            alpha_files = {Path(c["file"]).name for c in groups["Alpha"]["copies"]}
            beta_files = {Path(c["file"]).name for c in groups["Beta"]["copies"]}
            self.assertEqual(alpha_files, {"a.4k.mkv", "a.1080.mkv"})
            self.assertEqual(beta_files, {"b.4k.mkv", "b.1080.mkv"})
            self.assertEqual(groups["Beta"]["reclaimable_bytes"], 4 * GiB)


# --------------------------------------------------------------------------- #
# Content-hash confirmation pass integration (#9)                             #
# --------------------------------------------------------------------------- #

class HashPassIntegrationTests(unittest.TestCase):
    """End-to-end: the reporter runs the hash pass and surfaces it in JSON + table."""

    def _client(self, *files_and_sizes) -> FakePlexClient:
        # One movie with two same-size single-part copies under /plex, so it
        # classifies ``identical`` and the hash pass decides its fate.
        medias = [
            _media(i + 1, "1080", 1000, [_part(10 + i, f"/plex/{name}", size)])
            for i, (name, size) in enumerate(files_and_sizes)
        ]
        movie = _movie("100", "Dup", medias)
        return FakePlexClient(
            [PlexSection(key="1", type="movie", title="Movies")],
            {("1", 1): [movie]},
        )

    def _hash_config(self, tmp: Path, media: Path, mode: str) -> Config:
        return _config(
            tmp,
            hash_mode=mode,
            web_media_path_map=((Path("/plex"), media),),
        )

    def _run(self, tmp: Path, media: Path, mode: str, *files):
        for name, data in files:
            (media / name).write_bytes(data)
        client = self._client(*[(name, len(data)) for name, data in files])
        reporter = PlexDuplicateReporter(
            self._hash_config(tmp, media, mode), client, clock=lambda: 1234.5
        )
        report = reporter.generate()
        return reporter, report

    def test_off_keeps_report_shape_and_reads_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            media = tmp / "media"
            media.mkdir()
            # Files intentionally NOT written: an ``off`` run must not touch them.
            client = self._client(("a.mkv", 100), ("b.mkv", 100))
            reporter = PlexDuplicateReporter(
                self._hash_config(tmp, media, "off"), client, clock=lambda: 1234.5
            )
            payload = reporter.build_payload(reporter.generate())
            self.assertNotIn("hash_enabled", payload)
            self.assertNotIn("hash_mode", payload)
            self.assertNotIn("hash_confirmed_count", payload["totals"])
            self.assertNotIn("different_content_count", payload["totals"])
            for group in payload["groups"]:
                self.assertNotIn("hash_status", group)

    def test_full_confirms_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            media = tmp / "media"
            media.mkdir()
            reporter, report = self._run(
                tmp, media, "full", ("a.mkv", b"X" * 100), ("b.mkv", b"X" * 100)
            )
            payload = reporter.build_payload(report)
            self.assertTrue(payload["hash_enabled"])
            self.assertEqual(payload["hash_mode"], "full")
            self.assertEqual(payload["totals"]["hash_confirmed_count"], 1)
            self.assertEqual(payload["totals"]["different_content_count"], 0)
            group = payload["groups"][0]
            self.assertEqual(group["classification"], "identical")
            self.assertEqual(group["hash_status"], "confirmed")
            self.assertGreater(group["reclaimable_bytes"], 0)
            self.assertIn("[hash:confirmed]", reporter.render_table(report))

    def test_partial_reports_sample_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            media = tmp / "media"
            media.mkdir()
            reporter, report = self._run(
                tmp, media, "partial", ("a.mkv", b"X" * 100), ("b.mkv", b"X" * 100)
            )
            group = reporter.build_payload(report)["groups"][0]
            self.assertEqual(group["hash_status"], "sample-match")

    def test_different_content_excluded_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            media = tmp / "media"
            media.mkdir()
            reporter, report = self._run(
                tmp, media, "full", ("a.mkv", b"X" * 100), ("b.mkv", b"Y" * 100)
            )
            payload = reporter.build_payload(report)
            group = payload["groups"][0]
            self.assertEqual(group["classification"], "different-content")
            self.assertEqual(group["hash_status"], "different")
            self.assertEqual(group["reclaimable_bytes"], 0)
            self.assertEqual(payload["totals"]["different_content_count"], 1)
            self.assertEqual(payload["totals"]["reclaimable_bytes"], 0)
            # Excluded from the reclaimable section, surfaced under the review section.
            table = reporter.render_table(report)
            self.assertIn("different content (hash mismatch, excluded)", table)

    def test_unhashable_stays_size_only_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            media = tmp / "media"
            media.mkdir()
            # Only a.mkv written; b.mkv missing => unhashable.
            reporter, report = self._run(tmp, media, "full", ("a.mkv", b"X" * 100))
            # Re-run with b.mkv referenced but absent: build a two-copy client manually.
            (media / "a.mkv").write_bytes(b"X" * 100)
            client = self._client(("a.mkv", 100), ("b.mkv", 100))
            reporter = PlexDuplicateReporter(
                self._hash_config(tmp, media, "full"), client, clock=lambda: 1234.5
            )
            report = reporter.generate()
            payload = reporter.build_payload(report)
            group = payload["groups"][0]
            self.assertEqual(group["classification"], "identical")
            self.assertEqual(group["hash_status"], "unhashable")
            self.assertGreater(group["reclaimable_bytes"], 0)  # unchanged, size-only
            self.assertEqual(payload["totals"]["hash_unhashable_count"], 1)
            self.assertTrue(any("unhashable" in w for w in report.warnings))


if __name__ == "__main__":
    unittest.main()
