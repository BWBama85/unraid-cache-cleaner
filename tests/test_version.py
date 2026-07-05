"""Version single-source-of-truth guards.

The release flow (``/release``) bumps two files — ``pyproject.toml`` and
``src/unraid_cache_cleaner/__init__.py`` — and every client's ``User-Agent`` is
derived from ``__version__`` so a bump can never leave a stale ``0.x`` string in
one client. These tests fail loudly if that invariant regresses.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner import USER_AGENT, __version__
from unraid_cache_cleaner.arr import RadarrClient, SonarrClient
from unraid_cache_cleaner.plex import PlexClient
from unraid_cache_cleaner.qbittorrent import QbittorrentClient

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    """Read the ``[project]`` version without tomllib (absent on Python 3.9)."""
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.match(r'version\s*=\s*"([^"]+)"', stripped)
            if match:
                return match.group(1)
    raise AssertionError("no version found under [project] in pyproject.toml")


def _user_agent_header(opener) -> str:
    for name, value in opener.addheaders:
        if name.lower() == "user-agent":
            return value
    raise AssertionError("no User-Agent header on opener")


class VersionConsistencyTests(unittest.TestCase):
    def test_pyproject_matches_package_version(self) -> None:
        self.assertEqual(_pyproject_version(), __version__)

    def test_user_agent_derived_from_version(self) -> None:
        self.assertEqual(USER_AGENT, f"unraid-cache-cleaner/{__version__}")


class ClientUserAgentTests(unittest.TestCase):
    def test_qbittorrent_sends_shared_user_agent(self) -> None:
        client = QbittorrentClient("http://qbt:8080", "admin", "secret")
        self.assertEqual(_user_agent_header(client._opener), USER_AGENT)

    def test_plex_sends_shared_user_agent(self) -> None:
        client = PlexClient("http://plex:32400", "token")
        self.assertEqual(_user_agent_header(client._opener), USER_AGENT)

    def test_radarr_sends_shared_user_agent(self) -> None:
        client = RadarrClient("http://radarr:7878", "key")
        self.assertEqual(_user_agent_header(client._opener), USER_AGENT)

    def test_sonarr_sends_shared_user_agent(self) -> None:
        client = SonarrClient("http://sonarr:8989", "key")
        self.assertEqual(_user_agent_header(client._opener), USER_AGENT)


if __name__ == "__main__":
    unittest.main()
