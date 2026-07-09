"""Deterministically build the committed ``hello.rar`` test fixture.

`RealBinaryTests` needs a real ``.rar`` to drive the ``unar`` extraction path in
CI (#39). There is no free, script-friendly RAR *creator* — RARLab's ``rar`` is
proprietary and the Homebrew cask ships only ``unrar`` — so rather than depend on
one (or commit an opaque binary of unknown provenance), the fixture is generated
here, byte-for-byte, from this stdlib-only script.

The output is a minimal, uncompressed (store-method) **RAR4** archive holding a
single small file. The format is the classic ``Rar!\\x1a\\x07\\x00`` layout
documented in RARLab's TechNote; store mode keeps it tiny and dependency-free.
The result verifies cleanly with ``lsar -test`` and extracts with ``unar`` (see
the fixture README), which is exactly what the real-binary test exercises.

Regenerate (produces a byte-identical file — the timestamp field is pinned to 0):

    python3 tests/fixtures/make_rar_fixture.py
"""

from __future__ import annotations

import binascii
import struct
from pathlib import Path
from typing import Optional

# The single member the fixture archive contains.
FIXTURE_MEMBER = "hello.txt"
FIXTURE_CONTENT = b"hello from a committed rar fixture\n"
FIXTURE_PATH = Path(__file__).resolve().parent / "hello.rar"

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


def build_rar4(filename: str, data: bytes) -> bytes:
    """Return the bytes of a store-method RAR4 archive holding ``filename``."""

    # MAIN_HEAD: 6 reserved bytes (HighPosAV + PosAV), no flags.
    main = _block(_MAIN_HEAD, 0x0000, struct.pack("<HI", 0, 0))

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
    file_block = _block(_FILE_HEAD, 0x8000, body, tail=data)  # 0x8000: data follows

    end = _block(_END_ARCHIVE, 0x0000, b"")
    return _MARKER + main + file_block + end


def write_fixture(path: Optional[Path] = None) -> Path:
    """Write the canonical fixture archive; return where it landed."""

    target = path or FIXTURE_PATH
    target.write_bytes(build_rar4(FIXTURE_MEMBER, FIXTURE_CONTENT))
    return target


if __name__ == "__main__":
    written = write_fixture()
    print(f"wrote {written} ({written.stat().st_size} bytes)")
