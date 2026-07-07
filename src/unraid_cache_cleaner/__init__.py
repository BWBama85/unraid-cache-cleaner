"""Unraid cache cleaner package."""

__all__ = ["__version__", "USER_AGENT"]

__version__ = "0.1.0"

# Single source of truth for the HTTP User-Agent every client sends. Deriving it
# from __version__ keeps a release bump to two files (this one + pyproject.toml).
USER_AGENT = f"unraid-cache-cleaner/{__version__}"
