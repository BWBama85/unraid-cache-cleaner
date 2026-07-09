"""Structural guards for the release image signing wiring (issue #30).

There is no YAML tooling in this stdlib-only repo and the publish workflow does
not run on PRs, so these substring checks are the regression net that keeps the
cosign keyless-signing steps from silently regressing: the versioned GHCR image
must stay signed, gated to tag builds, and free of new secrets.
"""

from __future__ import annotations

import unittest
from pathlib import Path

_WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml"
)
_TAG_GUARD = "startsWith(github.ref, 'refs/tags/v')"


class PublishWorkflowSigningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _WORKFLOW.read_text(encoding="utf-8")

    def test_grants_oidc_id_token(self) -> None:
        # Keyless cosign signing needs the OIDC id-token permission.
        self.assertIn("id-token: write", self.text)

    def test_build_step_is_addressable(self) -> None:
        # The sign step references the pushed digest via the build step id.
        self.assertIn("id: build", self.text)

    def test_signs_the_image(self) -> None:
        self.assertIn("cosign sign", self.text)

    def test_signs_by_digest(self) -> None:
        # Signing the digest (not a mutable tag) is what makes every tag verify.
        self.assertIn("steps.build.outputs.digest", self.text)

    def test_uses_lowercase_repository(self) -> None:
        # GHCR rejects an uppercase repository path.
        self.assertIn("GITHUB_REPOSITORY,,", self.text)

    def test_signing_is_gated_to_tag_builds(self) -> None:
        # Both the installer and the sign step must be tag-gated so :latest and
        # :sha builds from main are never signed.
        self.assertGreaterEqual(self.text.count(_TAG_GUARD), 2)

    def test_no_signing_without_the_tag_guard(self) -> None:
        # Every step that actually installs or runs cosign must sit under the
        # tag guard (a passing mention in a comment does not count).
        for block in _step_blocks(self.text):
            if "cosign sign --yes" in block or "sigstore/cosign-installer" in block:
                self.assertIn(_TAG_GUARD, block, msg=f"unguarded cosign step:\n{block}")

    def test_no_new_secrets_beyond_github_token(self) -> None:
        # AC #3: no new required secrets beyond GITHUB_TOKEN / OIDC.
        secrets = {
            token.split("}}")[0].strip().rstrip(".")
            for token in self.text.split("secrets.")[1:]
        }
        self.assertEqual(secrets, {"GITHUB_TOKEN"})


def _step_blocks(text: str) -> list:
    """Split the workflow into per-step chunks keyed on ``- name:`` lines."""
    blocks: list = []
    current: list = []
    for line in text.splitlines():
        if line.lstrip().startswith("- name:") and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


if __name__ == "__main__":
    unittest.main()
