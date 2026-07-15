"""Deterministically build the committed ``hello.rar`` / ``nested.rar`` fixtures.

`RealBinaryTests` needs a real ``.rar`` to drive the ``unar`` extraction path in
CI (#39). There is no free, script-friendly RAR *creator* — RARLab's ``rar`` is
proprietary and the Homebrew cask ships only ``unrar`` — so rather than depend on
one (or commit an opaque binary of unknown provenance), the fixtures are generated
here, byte-for-byte, from this stdlib-only script.

The output is a minimal, uncompressed (store-method) **RAR4** archive. The format
is the classic ``Rar!\\x1a\\x07\\x00`` layout documented in RARLab's TechNote;
store mode keeps it tiny and dependency-free. The results verify cleanly with
``lsar -test`` and extract with ``unar`` (see the fixture README), which is
exactly what the real-binary tests exercise.

``hello.rar`` holds one root-level file; ``nested.rar`` adds a member inside a
subdirectory so the nested produced-output path is covered too (#54).

Regenerate (produces byte-identical files — the timestamp field is pinned to 0):

    python3 tests/fixtures/make_rar_fixture.py
"""

from __future__ import annotations

import binascii
import struct
from pathlib import Path
from typing import Iterable, Optional, Tuple

# The single member the fixture archive contains.
FIXTURE_MEMBER = "hello.txt"
FIXTURE_CONTENT = b"hello from a committed rar fixture\n"
FIXTURE_PATH = Path(__file__).resolve().parent / "hello.rar"

# RAR stores member paths with a DOS-style **backslash** separator regardless of
# the host OS, and ``unar`` splits on it to recreate the directory tree. This is
# what makes nested members exactly mappable (#54): ``lsar`` reports the member as
# ``sub/deep.txt`` and ``unar`` writes it to ``<dest>/sub/deep.txt``. A member
# holding a literal ``/`` is *not* a path — ``unar`` sanitizes it into a single
# flat name (``sub_deep.txt`` on Linux, ``sub:deep.txt`` on macOS) — so fixtures
# must use this separator to mirror what real archives contain.
RAR_PATH_SEPARATOR = "\\"

# The nested fixture: one root-level member plus one inside a subdirectory.
NESTED_FIXTURE_PATH = Path(__file__).resolve().parent / "nested.rar"
NESTED_ROOT_MEMBER = "root.txt"
NESTED_ROOT_CONTENT = b"root member of a nested rar fixture\n"
NESTED_DEEP_MEMBER = RAR_PATH_SEPARATOR.join(("sub", "deep.txt"))
NESTED_DEEP_CONTENT = b"nested member of a nested rar fixture\n"
# Where the nested member lands on disk (and how ``lsar`` names it).
NESTED_DEEP_EXTRACTED = "sub/deep.txt"
NESTED_MEMBERS: Tuple[Tuple[str, bytes], ...] = (
    (NESTED_ROOT_MEMBER, NESTED_ROOT_CONTENT),
    (NESTED_DEEP_MEMBER, NESTED_DEEP_CONTENT),
)

# RAR4 block types (RARLab TechNote).
_MAIN_HEAD = 0x73
_FILE_HEAD = 0x74
_END_ARCHIVE = 0x7B

_MARKER = bytes([0x52, 0x61, 0x72, 0x21, 0x1A, 0x07, 0x00])  # "Rar!\x1a\x07\x00"


def _crc16(data: bytes) -> int:
    """RAR4 header checksum: the low 16 bits of the header's CRC32."""

    return binascii.crc32(data) & 0xFFFF


def _block(head_type: int, head_flags: int, body: bytes, *, tail: bytes = b"") -> bytes:
    """Assemble one RAR4 block: ``HEAD_CRC HEAD_TYPE HEAD_FLAGS HEAD_SIZE body [tail]``.

    ``HEAD_SIZE`` covers the header only (not ``tail`` — the file data that follows
    a file header). ``HEAD_CRC`` checksums every header byte after itself.
    """

    head_size = 2 + 1 + 2 + 2 + len(body)  # crc + type + flags + size + body
    header_after_crc = struct.pack("<BHH", head_type, head_flags, head_size) + body
    return struct.pack("<H", _crc16(header_after_crc)) + header_after_crc + tail


def _file_block(filename: str, data: bytes) -> bytes:
    """One store-method FILE_HEAD block plus its file data.

    ``filename`` is the raw archived name: a subdirectory member spells its path
    with :data:`RAR_PATH_SEPARATOR`, not ``/``.
    """

    name = filename.encode("ascii")
    body = (
        struct.pack("<II", len(data), len(data))  # PACK_SIZE, UNP_SIZE (store: equal)
        + struct.pack("<B", 3)  # HOST_OS = Unix
        + struct.pack("<I", binascii.crc32(data) & 0xFFFFFFFF)  # FILE_CRC
        + struct.pack("<I", 0)  # FTIME (pinned to 0 → deterministic output)
        + struct.pack("<BB", 20, 0x30)  # UNP_VER = 2.0, METHOD = 0x30 (store)
        + struct.pack("<H", len(name))  # NAME_SIZE
        + struct.pack("<I", 0)  # ATTR
        + name
    )
    return _block(_FILE_HEAD, 0x8000, body, tail=data)  # 0x8000: data follows


def build_rar4_multi(entries: Iterable[Tuple[str, bytes]]) -> bytes:
    """Return the bytes of a store-method RAR4 archive holding ``entries``.

    Members are emitted in order. Subdirectories need no explicit entry — ``unar``
    creates them from the member path.
    """

    # MAIN_HEAD: 6 reserved bytes (HighPosAV + PosAV), no flags.
    main = _block(_MAIN_HEAD, 0x0000, struct.pack("<HI", 0, 0))
    blocks = b"".join(_file_block(filename, data) for filename, data in entries)
    end = _block(_END_ARCHIVE, 0x0000, b"")
    return _MARKER + main + blocks + end


def build_rar4(filename: str, data: bytes) -> bytes:
    """Return the bytes of a store-method RAR4 archive holding one file."""

    return build_rar4_multi([(filename, data)])


def write_fixture(path: Optional[Path] = None) -> Path:
    """Write the canonical single-member fixture archive; return where it landed."""

    target = path or FIXTURE_PATH
    target.write_bytes(build_rar4(FIXTURE_MEMBER, FIXTURE_CONTENT))
    return target


def write_nested_fixture(path: Optional[Path] = None) -> Path:
    """Write the nested-member fixture archive; return where it landed."""

    target = path or NESTED_FIXTURE_PATH
    target.write_bytes(build_rar4_multi(NESTED_MEMBERS))
    return target


if __name__ == "__main__":
    for written in (write_fixture(), write_nested_fixture()):
        print(f"wrote {written} ({written.stat().st_size} bytes)")
