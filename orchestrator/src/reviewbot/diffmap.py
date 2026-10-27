"""Map a unified git diff to the new-file lines a review comment can use.

For every file in diff.patch, collect the line numbers in the new (head)
version that appear in the diff as added ("+") or context (" ") lines. GitHub
accepts inline review comments with side RIGHT only on such lines; any other
comment is moved into the review body by post.py.

Only "+++ " headers and hunks matter here. Files without hunks (pure renames,
mode changes, binary files) and deleted files ("+++ /dev/null") contribute no
usable lines.
"""

from __future__ import annotations

import re

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


def parse_diff(text: str) -> dict[str, set[int]]:
    """Return {path: set of new-file line numbers usable for a comment}."""
    result: dict[str, set[int]] = {}
    current: set[int] | None = None
    new_line = 0
    new_left = 0  # lines of the new side still expected in this hunk
    old_left = 0  # lines of the old side still expected in this hunk

    for line in text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if new_left > 0 or old_left > 0:
            tag = line[:1]
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag == "+" and new_left > 0:
                if current is not None:
                    current.add(new_line)
                new_line += 1
                new_left -= 1
                continue
            if tag == "-" and old_left > 0:
                old_left -= 1
                continue
            if (tag == " " or line == "") and new_left > 0 and old_left > 0:
                if current is not None:
                    current.add(new_line)
                new_line += 1
                new_left -= 1
                old_left -= 1
                continue
            # Line does not fit the hunk header's counts: treat the hunk as
            # finished and parse this line as a header.
            new_left = old_left = 0

        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = _new_path(line)
            current = None if path is None else result.setdefault(path, set())
        else:
            m = HUNK_RE.match(line)
            if m:
                old_left = int(m.group(1)) if m.group(1) is not None else 1
                new_line = int(m.group(2))
                new_left = int(m.group(3)) if m.group(3) is not None else 1
    return result


def can_place(diffmap: dict[str, set[int]], files: set[str], path: str, line: int) -> bool:
    return path in files and line in diffmap.get(path, set())
