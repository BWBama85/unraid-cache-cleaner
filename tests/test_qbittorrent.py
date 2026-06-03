"""Tests for the qBittorrent client login handling."""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unraid_cache_cleaner.qbittorrent import QbittorrentClient, QbittorrentClientError


class LoginTests(unittest.TestCase):
    def _client(self) -> QbittorrentClient:
        return QbittorrentClient("http://qbt:8080", "admin", "secret")

    def test_login_accepts_ok(self) -> None:
        client = self._client()
        with mock.patch.object(client, "_request", return_value="Ok."):
            client.login()
        self.assertTrue(client._authenticated)

    def test_login_accepts_empty_bypass_response(self) -> None:
        # qBittorrent returns an empty 204 body when the client is auth-bypassed
        # (whitelisted subnet or localhost). That must count as success.
        client = self._client()
        with mock.patch.object(client, "_request", return_value=""):
            client.login()
        self.assertTrue(client._authenticated)

    def test_login_rejects_fails(self) -> None:
        client = self._client()
        with mock.patch.object(client, "_request", return_value="Fails."):
            with self.assertRaises(QbittorrentClientError):
                client.login()
        self.assertFalse(client._authenticated)


if __name__ == "__main__":
    unittest.main()
