"""Build and post the GitHub review.

Endpoint (checked against https://docs.github.com/en/rest/pulls/reviews on
2026-10-24): POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews with
body fields commit_id, body, event, and comments[] of {path, body, line,
side}. The event is always COMMENT; the model's verdict goes in the body.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import TextIO

from .diffmap import can_place
from .github_auth import GitHubError

log = logging.getLogger(__name__)

VERDICT_TEXT = {
    "approve": "approve",
    "comment": "comment",
    "request_changes": "request changes",
}

# Model output is derived from untrusted PR content. GitHub turns these
# forms in a review into notifications or cross-reference events on other
# issues and pull requests:
#   @user, @org/team, owner/repo@sha, user@sha   (an @ before a name character)
#   #123, owner/repo#123                          (a # before a digit)
#   GH-123                                        (GH, any case, before -digit)
#   owner/repo/issues/1, /pull/1, owner/discussions/1  (any "/" before
#     issues/, pull/ or discussions/ and a digit, also inside paths and URLs)
#   github.com/owner/repo/ followed by issues/, pull/, discussions/, commit/,
#     commits/ or compare/
#   HTML character references (&commat;user, &#x23;1, &sol;) for any of these
# A zero-width space after the "&", "@", "#" or "/" (or between GH and the
# dash) stops GitHub from recognising the form while the text reads the same.
# Ordinary paths such as api/pull/handler.py are left unchanged: the segment
# after issues/, pull/ or discussions/ must start with a digit. GitHub drops a
# backslash before ASCII punctuation first, so every punctuation character a
# pattern needs may be preceded by backslashes (\\*). The forms were found by
# rendering text through GitHub's /markdown API; the cases in tests/test_post.py
# are the list.
ZWSP = "\u200b"
_ENTITY = re.compile(r"&(?=\\*#|[A-Za-z])")
_PATH_REFERENCE = re.compile(
    r"github\\*\.com(?:\\*:[0-9]+)?\\*/(?:[A-Za-z0-9]|\\*-)+\\*/(?:[A-Za-z0-9]|\\*[_.-])+\\*/"
    r"(?=(?:issues|pull|discussions|commits?|compare)\\*/)"
    r"|/(?=(?:issues|pull|discussions)\\*/[0-9])",
    re.IGNORECASE)
_NAME_REFERENCE = re.compile(
    r"@(?=[A-Za-z0-9])"
    r"|#(?=[0-9])"
    r"|(?<![A-Za-z0-9])GH(?=\\*-[0-9])",
    re.IGNORECASE)


# Code is left unchanged so that it can be copied. Only text GitHub certainly
# renders as code counts; doubtful text is prose, at worst adding ZWSPs to code.
_FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)")
# A line that could open a fence inside a list item, block quote or after a
# tab. Fences there are not tracked, so nothing after it is treated as code.
_MAYBE_FENCE = re.compile(r"[ \t>*+0-9.)-]*(?:```|~~~)")
_SPAN_TOKEN = re.compile(r"`+|[\]<$]")
_RUN = re.compile(r"`+")


def _defang_prose(text: str) -> str:
    # Character references first, so that none can decode into a form the
    # passes below look for. Paths next: the last pass may then still break
    # a GH-1 inside one.
    text = _ENTITY.sub(lambda m: m.group(0) + ZWSP, text)
    text = _PATH_REFERENCE.sub(lambda m: m.group(0) + ZWSP, text)
    return _NAME_REFERENCE.sub(lambda m: m.group(0) + ZWSP, text)


def _columns(lead: str) -> int:
    col = 0
    for ch in lead:
        col = col + 4 - col % 4 if ch == "\t" else col + 1
    return col


def _indent(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _fence_end(lines: list[str], i: int, indent: int, marker: str) -> int | None:
    """Index of the line that closes the fence opened on lines[i], or None
    when it is unclosed or the closing line is in doubt."""
    closing = re.compile(rf"[ \t]*{re.escape(marker[0])}{{{len(marker)},}}[ \t]*")
    for j in range(i + 1, len(lines)):
        line = lines[j]
        if not line.strip(" \t"):
            continue
        col = _columns(_indent(line))
        # An indented opener may belong to a list item whose content starts at
        # any column up to `indent`; that item would end or close the fence here.
        if col < indent:
            return None
        if closing.fullmatch(line):
            if col <= 3:
                return j
            if col <= indent + 3:
                return None
    return None


def _code_regions(text: str) -> list[tuple[int, int]]:
    """(start, end) offsets of the fenced code blocks and code spans in a
    whole Markdown document that GitHub renders as code."""
    regions: list[tuple[int, int]] = []
    # A lone CR is a line break to CommonMark and cmark drops a leading BOM,
    # so either would shift the lines GitHub sees.
    if "\r" in text or "\ufeff" in text:
        return regions
    lines = text.split("\n")
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    # Spans are matched within one line, which agrees with GitHub only while
    # every earlier backtick run in the paragraph was matched the same way.
    spans_ok = True
    i = 0
    while i < len(lines):
        line, start = lines[i], starts[i]
        if not line.strip(" \t"):
            spans_ok = True
            i += 1
            continue
        m = _FENCE.fullmatch(line)
        if m and not (m[2][0] == "`" and "`" in m[3]):
            end = _fence_end(lines, i, len(m[1]), m[2])
            if end is None:
                return regions
            # GitHub renders a math fence as text, mentions included. "&" and
            # "\\" are refused because entities and escapes could spell "math".
            info = m[3].lower()
            if "math" not in info and "&" not in info and "\\" not in info:
                regions.append((start, starts[end] + len(lines[end])))
            spans_ok = True
            i = end + 1
            continue
        if _MAYBE_FENCE.match(line):
            return regions
        # A "|" may make the line a table row, split into cells before code
        # spans are found; an indented line may be an indented code block.
        if "|" in line or _columns(_indent(line)) >= 4:
            spans_ok = False
        pos = 0
        while spans_ok:
            tok = _SPAN_TOKEN.search(line, pos)
            if tok is None:
                break
            close = None
            if tok[0][0] == "`":
                before = line[:tok.start()]
                word = re.split(r"[ \t]", before)[-1].lower()
                # An escaping backslash, an autolink ("www.", "scheme:"), or a
                # link destination after "]" can each take the backtick.
                if not before.endswith("\\") and ":" not in word and "www." not in word:
                    close = next((r for r in _RUN.finditer(line, tok.end())
                                  if len(r[0]) == len(tok[0])), None)
            if close is None:
                spans_ok = False
                pos = tok.start()
            else:
                regions.append((start + tok.start(), start + close.end()))
                pos = close.end()
        # "<" may open HTML, and "$" math, that can run past this paragraph.
        if not spans_ok and re.search(r"[<$]", line[pos:]):
            return regions
        i += 1
    return regions


def defang(text: str) -> str:
    """Break every GitHub mention and issue, pull request or commit
    reference in the prose of a whole Markdown document."""
    out: list[str] = []
    pos = 0
    for start, end in _code_regions(text):
        out += [_defang_prose(text[pos:start]), text[start:end]]
        pos = end
    out.append(_defang_prose(text[pos:]))
    return "".join(out)


def _inline_path(path: str) -> str:
    """Render a model-supplied path safely inside backticks on one line."""
    # With its backticks replaced the path holds no code region, so every form
    # is broken; a "|" can still split the span when GitHub reads a table row.
    clean = re.sub(r"[\r\n`]", "?", path)
    return f"`{defang(clean[:300])}`"


def _comment_text(c: dict) -> str:
    return f"**{c['severity']}**: {c['body']}"


def build_payload(structured: dict, head_sha: str, diffmap: dict[str, set[int]],
                  files: set[str], *, relocate_all: bool = False) -> dict:
    inline: list[dict] = []
    moved: list[dict] = []
    for c in structured["comments"]:
        placeable = not relocate_all and can_place(diffmap, files, c["path"], c["line"])
        if placeable:
            inline.append({
                "path": c["path"],
                "line": c["line"],
                "side": "RIGHT",
                "body": defang(_comment_text(c)),
            })
        else:
            moved.append(c)

    verdict = VERDICT_TEXT.get(structured["verdict"], structured["verdict"])
    parts = [f"**reviewbot verdict: {verdict}**", "", structured["summary"]]
    if moved:
        parts += ["", "#### Comments outside the diff", ""]
        for c in moved:
            parts.append(f"- {_inline_path(c['path'])} line {int(c['line'])}: "
                         f"{_comment_text(c)}")
    parts += ["", "<sub>Automated review by reviewbot. It is posted as a comment "
                  "and does not approve or block this pull request.</sub>"]

    payload = {
        "commit_id": head_sha,
        "event": "COMMENT",
        # Code regions depend on the text around them, so the body is
        # defanged as the one document GitHub renders.
        "body": defang("\n".join(parts)),
        "comments": inline,
    }
    return payload


def post_notice(api, token: str, repo: str, number: int, head_sha: str, text: str) -> dict:
    """POST a review that holds only text, about a commit reviewbot will not
    review. text must not hold model output."""
    body = f"{text}\n\n<sub>Automated message from reviewbot.</sub>"
    resp = api.request("POST", f"/repos/{repo}/pulls/{number}/reviews", token,
                       json={"commit_id": head_sha, "event": "COMMENT", "body": body},
                       expected=(200,))
    return resp.json()


def print_payload(repo: str, number: int, payload: dict, out: TextIO | None = None) -> None:
    out = sys.stdout if out is None else out
    doc = {"dry_run": True, "repo": repo, "number": number,
           "endpoint": f"POST /repos/{repo}/pulls/{number}/reviews",
           "payload": payload}
    out.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    out.flush()


def post_review(api, token: str, repo: str, number: int, payload: dict,
                rebuild_without_inline=None) -> dict:
    """POST the review. If GitHub rejects an inline position (422) and a
    rebuild function is given, retry once with every comment in the body."""
    path = f"/repos/{repo}/pulls/{number}/reviews"
    try:
        resp = api.request("POST", path, token, json=payload, expected=(200,))
        return resp.json()
    except GitHubError as exc:
        if exc.status == 422 and payload.get("comments") and rebuild_without_inline:
            log.warning("GitHub rejected inline comments for %s#%d (%s); "
                        "retrying with all comments in the body", repo, number, exc.message)
            retry = rebuild_without_inline()
            resp = api.request("POST", path, token, json=retry, expected=(200,))
            return resp.json()
        raise
