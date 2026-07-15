# Test fixtures

## `hello.rar`

A minimal, uncompressed (store-method) **RAR4** archive containing one file,
`hello.txt`, whose contents are `hello from a committed rar fixture\n`.

`tests/test_extractor.py::RealBinaryTests` uses it to drive the real `unar`
extraction path (and `lsar` member listing) in CI, so those paths are actually
exercised rather than skipped. The test still skips gracefully where `unar` is
not installed (e.g. local macOS/Windows dev).

## `nested.rar`

The same format, holding two members: `root.txt` at the archive root and one
inside a subdirectory. It backs the nested produced-output tests (#54) — in
particular that a nested member re-extracted to byte-identical content with a
restored mtime is still recorded as a protected output.

### Why the nested member is archived as `sub\deep.txt`

RAR stores member paths with a DOS-style **backslash** separator regardless of the
host OS, and `unar` splits on it to recreate the directory tree. That is what makes
nested members exactly mappable: `lsar` reports the member normalized to
`sub/deep.txt`, and `unar` writes it to `<dest>/sub/deep.txt` — the same relative
path, on Linux and macOS alike.

A member holding a **literal** `/` is a different thing entirely: it is not a path
to `unar`, but a single name it sanitizes flat — `sub_deep.txt` on Linux,
`sub:deep.txt` on macOS. Since `lsar` normalizes both spellings to `sub/deep.txt`,
the two are indistinguishable from the listing, so such a member cannot be mapped
and falls back to the `(mtime, size)` diff. Fixtures therefore use the backslash,
which is what real archives contain.

### Provenance — why they are generated, not created with `rar`

There is no free, scriptable RAR *creator*: RARLab's `rar` is proprietary and the
Homebrew `rar` cask ships only `unrar`. Committing an opaque binary of unknown
origin would be worse. So the fixtures are emitted, byte-for-byte, by the
stdlib-only generator `make_rar_fixture.py`, and verified against the real tools:

```console
$ lsar -test tests/fixtures/hello.rar
tests/fixtures/hello.rar: RAR
hello.txt... OK.
1 passed, 0 failed.

$ lsar -test tests/fixtures/nested.rar
tests/fixtures/nested.rar: RAR
root.txt... OK.
sub/deep.txt... OK.
2 passed, 0 failed.
```

### Regenerating

The output is deterministic (the archive timestamp field is pinned to 0), so
regenerating produces byte-identical files:

```console
$ python3 tests/fixtures/make_rar_fixture.py
```

If either archive is ever missing, the real-binary tests regenerate it in a temp
dir via the same generator — they never shell out to a `rar` binary.
