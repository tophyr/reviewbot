"""Map a unified git diff to the new-file lines a review comment can use.

For every file in diff.patch, collect the line numbers in the new (head)
version that appear in the diff as added ("+") or context (" ") lines. GitHub
accepts inline review comments with side RIGHT only on such lines; any other
comment is moved into the review body by post.py.

Only "+++ " headers and hunks matter here. Files without hunks (pure renames,
mode changes, binary files) and deleted files ("+++ /dev/null") contribute no
usable lines.

The same walk over the diff (iter_diff) also writes the numbered diff that
the reviewer reads (number_diff), so the line numbers the model is shown are
the ones a comment is checked against. A line number of diff.patch itself
(its position in the file, as the reviewer's Read tool shows it) is not a
line number of any source file; diff_positions maps such positions back, so
that a comment citing one can be recognised.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13,
            '"': 34, "\\": 92}
_OCTAL = re.compile(r"[0-7]{3}")


def unquote_git_path(raw: str) -> str:
    """Undo git's C-style quoting ("b/t\\303\\251st") of unusual paths."""
    if not (len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"'):
        return raw
    body = raw[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            if _OCTAL.fullmatch(body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            nxt = body[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
            else:
                out.extend(("\\" + nxt).encode("utf-8"))
            i += 2
            continue
        out.extend(c.encode("utf-8"))
        i += 1
    return out.decode("utf-8", errors="replace")


def _new_path(header: str) -> str | None:
    """Path from a "+++ " header line, without the b/ prefix; None for /dev/null."""
    raw = header[4:]
    # git ends a path that contains a space with a tab, after the closing
    # quote when the path is quoted (a quoted path has no literal tab inside)
    if raw.startswith('"'):
        raw = raw.rstrip("\t")
    elif "\t" in raw:
        raw = raw.split("\t", 1)[0]
    path = unquote_git_path(raw)
    if path == "/dev/null":
        return None
    return path[2:] if path.startswith("b/") else path


@dataclass(frozen=True)
class DiffLine:
    """One line of a diff. kind is "add", "context", "remove" (lines of a
    hunk) or "other" (headers and anything outside a hunk). path is the
    new-side path of the file the line belongs to, None before the first
    file and for a deleted file. new_line is the head line number of an add
    or context line, None otherwise."""
    kind: str
    path: str | None
    new_line: int | None
    text: str


def _split_lines(text: str) -> list[str]:
    """The lines of text as a file viewer numbers them: split at "\n", with
    the empty string after a final newline dropped and a trailing "\r" removed."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def iter_diff(text: str) -> Iterator[DiffLine]:
    """Yield one DiffLine per line of text, in order, so that the n-th item
    describes line n of the diff file."""
    path: str | None = None
    new_line = 0
    new_left = 0  # lines of the new side still expected in this hunk
    old_left = 0  # lines of the old side still expected in this hunk

    for line in _split_lines(text):
        if new_left > 0 or old_left > 0:
            tag = line[:1]
            if tag == "\\":  # "\ No newline at end of file"
                yield DiffLine("other", path, None, line)
                continue
            if tag == "+" and new_left > 0:
                yield DiffLine("add", path, new_line if path is not None else None, line)
                new_line += 1
                new_left -= 1
                continue
            if tag == "-" and old_left > 0:
                yield DiffLine("remove", path, None, line)
                old_left -= 1
                continue
            if (tag == " " or line == "") and new_left > 0 and old_left > 0:
                yield DiffLine("context", path, new_line if path is not None else None, line)
                new_line += 1
                new_left -= 1
                old_left -= 1
                continue
            # Line does not fit the hunk header's counts: treat the hunk as
            # finished and parse this line as a header.
            new_left = old_left = 0

        if line.startswith("diff --git "):
            path = None
        elif line.startswith("+++ "):
            path = _new_path(line)
        else:
            m = HUNK_RE.match(line)
            if m:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                new_line = int(m.group(2))
                new_left = int(m.group(3)) if m.group(3) is not None else 1
        yield DiffLine("other", path, None, line)


def parse_diff(text: str) -> dict[str, set[int]]:
    """Return {path: set of new-file line numbers usable for a comment}."""
    result: dict[str, set[int]] = {}
    for dl in iter_diff(text):
        if dl.path is None:
            continue
        lines = result.setdefault(dl.path, set())
        if dl.new_line is not None:
            lines.add(dl.new_line)
    return result


def _shown_path(path: str) -> str:
    # The fetcher refuses a path with a newline, but git's quoting can spell
    # one; keep the numbered diff one line per line of the diff even so.
    return path.replace("\n", "?").replace("\r", "?")


def number_diff(text: str) -> str:
    """The diff with the head line number written on each line of a hunk.

    An added or context line of a file becomes "<path>:<line>:<diff line>",
    where <line> is its line number in the head version of <path>. A removed
    line becomes "<path>:-:<diff line>": it has no number in the head
    version. Every other line (file and hunk headers, the lines of a deleted
    file) is copied unchanged. The output has as many lines as the input.
    """
    out: list[str] = []
    for dl in iter_diff(text):
        if dl.path is not None and dl.new_line is not None:
            out.append(f"{_shown_path(dl.path)}:{dl.new_line}:{dl.text}")
        elif dl.path is not None and dl.kind == "remove":
            out.append(f"{_shown_path(dl.path)}:-:{dl.text}")
        else:
            out.append(dl.text)
    return "".join(line + "\n" for line in out)


def diff_positions(text: str) -> dict[str, dict[int, int]]:
    """{path: {n: head line}} for every added or context line, where n is
    that line's 1-based position in the diff file itself."""
    result: dict[str, dict[int, int]] = {}
    for n, dl in enumerate(iter_diff(text), start=1):
        if dl.path is not None and dl.new_line is not None:
            result.setdefault(dl.path, {})[n] = dl.new_line
    return result


def can_place(diffmap: dict[str, set[int]], files: set[str], path: str, line: int) -> bool:
    return path in files and line in diffmap.get(path, set())
