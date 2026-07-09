# Test fixtures

## `hello.rar`

A minimal, uncompressed (store-method) **RAR4** archive containing one file,
`hello.txt`, whose contents are `hello from a committed rar fixture\n`.

`tests/test_extractor.py::RealBinaryTests` uses it to drive the real `unar`
extraction path (and `lsar` member listing) in CI, so those paths are actually
exercised rather than skipped. The test still skips gracefully where `unar` is
not installed (e.g. local macOS/Windows dev).

### Provenance — why it is generated, not created with `rar`

There is no free, scriptable RAR *creator*: RARLab's `rar` is proprietary and the
Homebrew `rar` cask ships only `unrar`. Committing an opaque binary of unknown
origin would be worse. So the fixture is emitted, byte-for-byte, by the
stdlib-only generator `make_rar_fixture.py`, and verified against the real tools:

```console
$ lsar -test tests/fixtures/hello.rar
tests/fixtures/hello.rar: RAR
hello.txt... OK.
1 passed, 0 failed.
```

### Regenerating

The output is deterministic (the archive timestamp field is pinned to 0), so
regenerating produces a byte-identical file:

```console
$ python3 tests/fixtures/make_rar_fixture.py
```

If `hello.rar` is ever missing, the real-binary test regenerates it in a temp dir
via the same generator — it never shells out to a `rar` binary.
