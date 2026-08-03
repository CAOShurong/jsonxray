"""Reading the file without holding it.

The whole promise of this tool is that it works on the file you actually have,
which is bigger than memory. That rules out ``json.load`` on an array, and it
rules out reading all the lines. Both modes here yield one record at a time and
never retain more than the record in hand.

Two input shapes are supported, because both turn up constantly:

* **JSON Lines** -- one JSON value per line. The common case, and trivially
  streamable.
* **A single JSON array** -- ``[{...}, {...}, ...]``, often pretty-printed
  across many lines, which is what most "export to JSON" buttons produce.
  ``json.load`` on one of these is exactly the out-of-memory failure people
  hit, so it is streamed with an incremental decoder instead.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterator
from typing import IO

from .profile import Profile

__all__ = ["ScanError", "detect_format", "scan"]

#: Chunk size for the array reader. Big enough that the syscall overhead
#: disappears, small enough that a pathological single record does not spike
#: memory by more than a few megabytes.
CHUNK = 1 << 16

#: A single JSON value larger than this aborts the array reader. Without a
#: cap, one unterminated bracket turns "constant memory" into "read the whole
#: file into a buffer", which is the failure this module exists to avoid.
MAX_VALUE_BYTES = 64 << 20


class ScanError(Exception):
    """The input could not be read as JSON at all."""


def detect_format(head: str) -> str:
    """Guess ``jsonl`` or ``array`` from the first bytes of the file.

    A leading ``[`` means a JSON array *unless* the whole first line parses on
    its own, which is the case for a JSON Lines file whose records happen to
    be arrays. Getting that backwards would read a valid JSONL file as one
    enormous malformed array, so the ambiguous case is resolved by trying.
    """
    stripped = head.lstrip()
    if not stripped:
        return "jsonl"
    if not stripped.startswith("["):
        return "jsonl"
    first_line = stripped.split("\n", 1)[0].rstrip()
    try:
        json.loads(first_line)
    except ValueError:
        return "array"
    return "jsonl"


def scan(
    stream: IO[str],
    profile: Profile,
    *,
    fmt: str = "auto",
    limit: int | None = None,
) -> Profile:
    """Fill ``profile`` from ``stream``, one record at a time.

    ``limit`` stops after that many records and marks the profile truncated,
    which matters for honesty downstream: a percentage from the first thousand
    records of a sorted file is not a percentage of the file, and the renderer
    says so.
    """
    if fmt == "auto":
        fmt, stream = _sniff(stream)
    if fmt == "array":
        records = _iter_array(stream, profile)
    elif fmt == "jsonl":
        records = _iter_lines(stream, profile)
    else:
        raise ScanError(f"unknown format: {fmt}")

    for lineno, record in records:
        profile.add_record(record, lineno)
        if limit is not None and profile.records >= limit:
            profile.truncated_at = limit
            break
    return profile


def _sniff(stream: IO[str]) -> tuple[str, IO[str]]:
    """Read a little, decide, and push it back.

    The peeked text is always pushed in front of the stream rather than
    rewound, and the caller must use the returned wrapper. Discarding it
    silently drops the first 64 KB of the input.

    Rewinding is not attempted at all, because there is no way to ask a
    stream whether it worked. A pipe on Windows answers ``seekable()`` with
    True, accepts ``seek(0)`` without raising, and then reports ``tell() ==
    0`` -- while the data is gone and every subsequent read returns "". Every
    indicator lies, so trusting any of them made ``jsonxray -`` report "no
    JSON records" for input that was perfectly good. Pushing the text back is
    correct on every stream and costs one branch per read until the head is
    consumed.
    """
    head = stream.read(CHUNK)
    if head == "":
        return "jsonl", stream
    return detect_format(head), _prepend(stream, head)


class _Prepended(io.TextIOBase):
    """A text stream with some already-read text pushed back on the front."""

    def __init__(self, head: str, rest: IO[str]) -> None:
        self._head = head
        self._rest = rest

    def read(self, size: int = -1) -> str:
        if self._head:
            if size is None or size < 0:
                out, self._head = self._head, ""
                return out + self._rest.read()
            out, self._head = self._head[:size], self._head[size:]
            if len(out) < size:
                out += self._rest.read(size - len(out))
            return out
        return self._rest.read(size)

    def readline(self, size: int = -1) -> str:  # type: ignore[override]
        if not self._head:
            return self._rest.readline(size)
        index = self._head.find("\n")
        if index >= 0:
            out, self._head = self._head[: index + 1], self._head[index + 1 :]
            return out
        # The pushed-back text runs past the end of the buffer without a
        # newline, so the rest of this line is still in the underlying stream.
        out, self._head = self._head, ""
        return out + self._rest.readline()

    def __iter__(self) -> Iterator[str]:
        while True:
            line = self.readline()
            if not line:
                return
            yield line


def _prepend(stream: IO[str], head: str) -> IO[str]:
    return _Prepended(head, stream)  # type: ignore[return-value]


def _iter_lines(stream: IO[str], profile: Profile) -> Iterator[tuple[int, object]]:
    """One JSON value per line, skipping blanks, surviving bad lines.

    A malformed line is recorded and stepped over rather than raised. A 40 GB
    export with nine broken lines in it is still worth profiling, and finding
    out *which* nine is usually the reason for running this at all.
    """
    for lineno, line in enumerate(stream, start=1):
        profile.bytes_read += len(line)
        stripped = line.strip()
        if not stripped:
            profile.blank_lines += 1
            continue
        try:
            yield lineno, json.loads(stripped)
        except ValueError as exc:
            profile.add_malformed(lineno, str(exc))


def _iter_array(stream: IO[str], profile: Profile) -> Iterator[tuple[int, object]]:
    """Values from a single top-level JSON array, decoded incrementally.

    ``raw_decode`` parses one value from the front of a buffer and reports
    where it stopped, which is enough to walk an arbitrarily long array while
    only ever holding one element plus whatever has been read but not yet
    consumed.

    Line numbers are tracked by counting newlines through the consumed text,
    so a reported position still points at somewhere the user can open.
    """
    decoder = json.JSONDecoder()
    buffer = ""
    lineno = 1
    started = False
    finished = False

    while True:
        chunk = stream.read(CHUNK)
        if chunk:
            profile.bytes_read += len(chunk)
            buffer += chunk
        elif not buffer.strip():
            break

        if not started:
            index = _skip_space(buffer, 0)
            if index >= len(buffer):
                if not chunk:
                    break
                continue
            if buffer[index] != "[":
                raise ScanError("expected a JSON array at the start of the input")
            lineno += buffer.count("\n", 0, index + 1)
            buffer = buffer[index + 1 :]
            started = True

        while True:
            index = _skip_space(buffer, 0)
            if index >= len(buffer):
                lineno += buffer.count("\n")
                buffer = ""
                break
            if buffer[index] in ",":
                lineno += buffer.count("\n", 0, index + 1)
                buffer = buffer[index + 1 :]
                continue
            if buffer[index] == "]":
                finished = True
                break
            try:
                value, end = decoder.raw_decode(buffer, index)
            except ValueError:
                # Either the element is genuinely broken, or it is merely
                # split across the chunk boundary. Only the absence of more
                # input can tell the two apart.
                if not chunk:
                    profile.add_malformed(
                        lineno + buffer.count("\n", 0, index),
                        "unterminated or invalid array element",
                    )
                    finished = True
                if len(buffer) > MAX_VALUE_BYTES:
                    raise ScanError(
                        "a single JSON value exceeded "
                        f"{MAX_VALUE_BYTES // (1 << 20)} MB; this does not look "
                        "like an array of records"
                    ) from None
                break
            yield lineno + buffer.count("\n", 0, index), value
            lineno += buffer.count("\n", 0, end)
            buffer = buffer[end:]

        if finished or not chunk:
            break


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def open_source(path: str) -> tuple[IO[str], str]:
    """Open ``path`` for scanning, or stdin when it is ``-``.

    Decoding is UTF-8 with replacement. A byte that is not valid UTF-8 in a
    JSON file is a real defect, but refusing to read the file is a worse
    answer than profiling it and having the surrounding line show up as
    malformed.
    """
    if path == "-":
        import sys

        return sys.stdin, "-"
    if not os.path.exists(path):
        raise ScanError(f"no such file: {path}")
    # Deliberately returned open: the caller streams from it and closes it.
    # A context manager here would close the file before a single record
    # had been read.
    handle = open(  # noqa: SIM115
        path, encoding="utf-8", errors="replace", newline=""
    )
    return handle, path
