import io
import json
import re
import string

import pytest
from conftest import fixture_text

from reviewbot import post
from reviewbot.diffmap import parse_diff
from reviewbot.github_auth import GitHubError

HEAD = "f" * 40


def ctx():
    m = parse_diff(fixture_text("small.diff"))
    f = {x for x in fixture_text("small.files.txt").split("\n") if x}
    return m, f


def review(verdict="request_changes"):
    return {
        "summary": "Summary text.",
        "verdict": verdict,
        "comments": [
            {"path": "src/calc.py", "line": 6, "severity": "bug", "body": "Inline one."},
            {"path": "src/calc.py", "line": 1, "severity": "nit", "body": "Untouched line."},
            {"path": "obsolete.txt", "line": 1, "severity": "suggestion", "body": "Deleted file."},
            {"path": "nowhere.py", "line": 3, "severity": "security", "body": "Not in PR."},
            {"path": "docs/new_name.txt", "line": 5, "severity": "nit", "body": "Inline two."},
        ],
    }


def test_payload_shape():
    m, f = ctx()
    p = post.build_payload(review(), HEAD, m, f)
    assert set(p) == {"commit_id", "event", "body", "comments"}
    assert p["commit_id"] == HEAD
    assert p["event"] == "COMMENT"
    assert p["comments"] == [
        {"path": "src/calc.py", "line": 6, "side": "RIGHT", "body": "**bug**: Inline one."},
        {"path": "docs/new_name.txt", "line": 5, "side": "RIGHT", "body": "**nit**: Inline two."},
    ]


def test_unplaceable_comments_move_to_body():
    m, f = ctx()
    body = post.build_payload(review(), HEAD, m, f)["body"]
    assert body.startswith("**reviewbot verdict: request changes**")
    assert "Summary text." in body
    assert "- `src/calc.py` line 1: **nit**: Untouched line." in body
    assert "- `obsolete.txt` line 1: **suggestion**: Deleted file." in body
    assert "- `nowhere.py` line 3: **security**: Not in PR." in body
    assert "Inline one." not in body
    total = len(review()["comments"])
    assert body.count("\n- `") + len(post.build_payload(review(), HEAD, m, f)["comments"]) == total


@pytest.mark.parametrize("verdict", ["approve", "comment", "request_changes"])
def test_event_is_always_comment(verdict):
    m, f = ctx()
    assert post.build_payload(review(verdict), HEAD, m, f)["event"] == "COMMENT"


def test_relocate_all():
    m, f = ctx()
    p = post.build_payload(review(), HEAD, m, f, relocate_all=True)
    assert p["comments"] == []
    assert "- `src/calc.py` line 6: **bug**: Inline one." in p["body"]


def test_mentions_are_defanged_and_paths_sanitised():
    m, f = ctx()
    r = review()
    r["summary"] = "cc @octocat and @org/team, email a@b.c"
    r["comments"][3]["path"] = "evil`\n# heading.py"
    p = post.build_payload(r, HEAD, m, f)
    assert "@octocat" not in p["body"] and "@​octocat" in p["body"]
    assert "@org/team" not in p["body"]
    assert "`evil??# heading.py`" in p["body"]


Z = "\u200b"
SHA = "4f2cfe92e3499f8581bf39db77e0a1431fa7b55c"
# (text the model might repeat, the text that must reach GitHub instead).
# Each plain form below was rendered as a link by GitHub's POST /markdown on
# 2026-10-25 and each expected form was not.
REFERENCES = [
    ("cc @octocat", f"cc @{Z}octocat"),
    ("team @org/team", f"team @{Z}org/team"),
    ("see #123", f"see #{Z}123"),
    ("see owner/repo#123", f"see owner/repo#{Z}123"),
    ("see GH-123", f"see GH{Z}-123"),
    ("see gh-123", f"see gh{Z}-123"),
    (f"commit owner/repo@{SHA}", f"commit owner/repo@{Z}{SHA}"),
    (f"commit user@{SHA[:12]}", f"commit user@{Z}{SHA[:12]}"),
    ("path owner/repo/issues/7", f"path owner/repo/{Z}issues/7"),
    ("path owner/repo/Pull/7", f"path owner/repo/{Z}Pull/7"),
    ("path owner/repo/discussions/1", f"path owner/repo/{Z}discussions/1"),
    ("url https://github.com/o/r/issues/7#issuecomment-1",
     f"url https://github.com/o/r/{Z}issues/7#issuecomment-1"),
    ("link [x](https://github.com/o/r/pull/7)", f"link [x](https://github.com/o/r/{Z}pull/7)"),
    (f"url https://github.com/o/r/commit/{SHA}", f"url https://github.com/o/r/{Z}commit/{SHA}"),
    ("url https://github.com/o/r/compare/a...b", f"url https://github.com/o/r/{Z}compare/a...b"),
    # with a port (rendered as a commit link on 2026-10-25)
    (f"url https://github.com:443/o/r/commit/{SHA}", f"url https://github.com:443/o/r/{Z}commit/{SHA}"),
    # GitHub also finds the shorthand inside a longer path, and in a URL
    # without a scheme
    ("path x/owner/repo/issues/7", f"path x/owner/repo/{Z}issues/7"),
    ("path owner/repo/pull/7/files", f"path owner/repo/{Z}pull/7/files"),
    ("url github.com/o/r/issues/7", f"url github.com/o/r/{Z}issues/7"),
    ("url www.github.com/o/r/pull/7", f"url www.github.com/o/r/{Z}pull/7"),
    # HTML character references, which GitHub decodes before it finds
    # mentions and references (rendered on 2026-10-25)
    ("cc &#x40;octocat", f"cc &{Z}#x40;octocat"),
    ("cc &commat;octocat", f"cc &{Z}commat;octocat"),
    ("see &#x23;123", f"see &{Z}#x23;123"),
    ("see owner/repo&num;123", f"see owner/repo&{Z}num;123"),
    ("see owner/repo&#x23;123", f"see owner/repo&{Z}#x23;123"),
    ("path owner/repo/issues&sol;7", f"path owner/repo/issues&{Z}sol;7"),
    ("path owner&sol;repo&sol;issues&sol;7", f"path owner&{Z}sol;repo&{Z}sol;issues&{Z}sol;7"),
    # a bare /issues/N, /pull/N or /discussions/N links to the same repository
    ("see /issues/7", f"see /{Z}issues/7"),
    ("see /pull/7", f"see /{Z}pull/7"),
    ("see /discussions/7", f"see /{Z}discussions/7"),
    ("see (/pull/2)", f"see (/{Z}pull/2)"),
    ("see:/issues/3", f"see:/{Z}issues/3"),
    # <owner>/issues/N links when <owner> owns the repository; any "/" before
    # issues/N, pull/N or discussions/N is broken, whatever precedes it
    ("see octocat/issues/1", f"see octocat/{Z}issues/1"),
    ("see x/issues/1", f"see x/{Z}issues/1"),
]


@pytest.mark.parametrize("plain,expected", REFERENCES)
def test_references_are_neutralised_in_all_model_text(plain, expected):
    m, f = ctx()
    r = review()
    r["summary"] = plain
    for c in r["comments"]:
        c["body"] = plain
    r["comments"][1]["path"] = "moved " + plain  # relocated, path shown in the body
    p = post.build_payload(r, HEAD, m, f)
    inline = [c["body"] for c in p["comments"]]
    assert inline == [f"**bug**: {expected}", f"**nit**: {expected}"]
    body = p["body"]
    assert plain not in body
    assert f"\n{expected}\n" in body  # summary
    assert body.count(f"**: {expected}") == 3  # three relocated comment bodies
    assert f"`moved {expected}`" in body  # relocated path
    # the 422 retry puts every comment in the body; none may escape there either
    again = post.build_payload(r, HEAD, m, f, relocate_all=True)["body"]
    assert plain not in again and again.count(f"**: {expected}") == 5


# GitHub removes a backslash before any ASCII punctuation character before it
# finds references ("owner/repo/issues\\/1" and "GH\\-1" rendered as links on
# 2026-10-25). Every row of REFERENCES is repeated with a backslash before
# each punctuation character, one at a time and all at once. After defang,
# removing the backslashes the way GitHub does must give the row's expected
# text, so the reference is still broken.
def _github_unescape(text):
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", text)


def _escaped_variants(plain):
    positions = [i for i, ch in enumerate(plain) if ch in string.punctuation]
    variants = [plain[:i] + "\\" + plain[i:] for i in positions]
    if len(positions) > 1:
        variants.append("".join("\\" + ch if ch in string.punctuation else ch for ch in plain))
    return variants


ESCAPED_REFERENCES = [(v, expected) for plain, expected in REFERENCES
                      for v in _escaped_variants(plain)]


def test_escaped_variants_cover_every_row():
    assert all(_escaped_variants(plain) for plain, _ in REFERENCES)
    assert _github_unescape("a\\/b\\-c\\#1\\\\x") == "a/b-c#1\\x"


@pytest.mark.parametrize("escaped,expected", ESCAPED_REFERENCES)
def test_backslash_escaped_references_are_neutralised(escaped, expected):
    out = post.defang(escaped)
    assert _github_unescape(out) == expected, out
    m, f = ctx()
    r = review()
    r["summary"] = escaped
    r["comments"][0]["body"] = escaped
    p = post.build_payload(r, HEAD, m, f)
    assert _github_unescape(p["comments"][0]["body"]) == f"**bug**: {expected}"
    assert f"\n{expected}\n" in _github_unescape(p["body"])


def test_plain_text_is_unchanged():
    text = "a/b.py line 3, 100% #define x, src/pull.py, email-like a.b"
    assert post.defang(text) == text


# Repository paths that contain issues/ or pull/ but are not references.
# GitHub's POST /markdown renders each of these as plain text.
ORDINARY_PATHS = ["api/pull/handler.py", "tests/issues/test_close.py",
                  "src/api/pull/handler.py", "docs/discussions/index.md",
                  "lib/commit/compare/x.py", "owner/repo/pull/abc"]


@pytest.mark.parametrize("path", ORDINARY_PATHS)
def test_ordinary_paths_are_unchanged(path):
    assert post.defang(path) == path
    m, f = ctx()
    r = review()
    r["summary"] = f"See {path}."
    r["comments"][3]["path"] = path  # not in the PR, so relocated into the body
    r["comments"][3]["body"] = f"In {path} the handler is wrong."
    body = post.build_payload(r, HEAD, m, f)["body"]
    assert f"See {path}." in body
    assert f"- `{path}` line 3: **security**: In {path} the handler is wrong." in body
    assert Z not in body


# Text GitHub renders as code is left unchanged, so code can be copied.
CODE = [
    "```python\n@property\ndef x(self):\n    return 1\n```",
    "~~~\n@octocat #1\n~~~",
    "use `@property` here",
    "x `` a`@octocat `` y",
    # a fence in a numbered list item, rendered as code on 2026-10-27
    "1. a\n   ```\n   @octocat\n   ```",
    # a blank line ends the paragraph holding an unmatched backtick
    "x `a\n\n`@octocat`",
]


@pytest.mark.parametrize("text", CODE)
def test_code_is_unchanged(text):
    assert post.defang(text) == text


def test_unclosed_fence_is_prose():
    # GitHub runs an unclosed fence to the end; treating it as prose is safe.
    assert post.defang("```\n@octocat") == f"```\n@{Z}octocat"


@pytest.mark.parametrize("text", ["x `@octocat y", "x ``@octocat` y"])
def test_unmatched_backticks_are_prose(text):
    assert post.defang(text) == text.replace("@", f"@{Z}")


def test_prose_around_fences_is_defanged():
    text = "cc @a\n```\n@b\n```\nand @c\n~~~\n#1\n~~~\nthen @d"
    assert post.defang(text) == (f"cc @{Z}a\n```\n@b\n```\nand @{Z}c\n"
                                 f"~~~\n#1\n~~~\nthen @{Z}d")


def test_shorter_closing_fence_does_not_close():
    assert post.defang("````\n@octocat\n```") == f"````\n@{Z}octocat\n```"


def test_closing_fence_indented_four_does_not_close():
    assert post.defang("```\n@octocat\n    ```") == f"```\n@{Z}octocat\n    ```"


def test_tilde_fence_is_not_closed_by_backticks():
    assert post.defang("~~~\n#1\n```\nafter #2") == f"~~~\n#{Z}1\n```\nafter #{Z}2"


def test_reference_next_to_code_span():
    out = post.defang("#123 and `#456`")
    assert out == f"#{Z}123 and `#456`" and out.count(Z) == 1


# Each text was rendered by GitHub's POST /markdown on 2026-10-27 with the
# @cli mention linked, although a naive splitter would call it code.
GITHUB_PROSE = [
    "# `a\n` @cli `@cli`",                         # spans do not cross a heading
    "- a\n  ```\n@cli\n  ```",                     # outdent ends the list item
    "- a\n  ```\n  b\n     ```\n  @cli\n  ```",    # closes at column 5 in the item
    "```math\n@cli\n```",                          # math fences are text
    "x $`@cli`$ y",                                # inline math
    "www.a.co/` @cli `",                           # autolink takes the backtick
    "http://a.co/` @cli `",
    "[a](`) @cli `",                               # link destination
    "\\` @cli `",                                  # escaped backtick
    "    `\n@cli `",                               # indented code block
    "<div>\n```\n@cli\n```\n</div>",               # HTML block
    "`a|@cli`\n|-|-|",                             # table cells split the span
    "**bug**: ```\n@cli\n```",                     # not at the start of a line
    "<?x>\n\n` @cli `",                            # HTML blocks pass blank lines
    "<textarea>\n\n` @cli `\n",
    "- ```\n  ~~~\n  ```\n  @cli\n  ~~~",           # fence opened in a list item
    "> ```\n> ~~~\n> ```\n> @cli\n> ~~~",          # fence opened in a block quote
]


@pytest.mark.parametrize("text", GITHUB_PROSE)
def test_github_prose_is_defanged(text):
    assert post.defang(text) == text.replace("@", f"@{Z}")


def test_code_is_kept_in_payload():
    m, f = ctx()
    r = review()
    code = "see\n```python\n@property\ndef x(self): ...\n```\ncc @octocat"
    expected = f"see\n```python\n@property\ndef x(self): ...\n```\ncc @{Z}octocat"
    r["summary"] = code
    for c in r["comments"]:
        c["body"] = code
    p = post.build_payload(r, HEAD, m, f)
    assert [c["body"] for c in p["comments"]] == [f"**bug**: {expected}", f"**nit**: {expected}"]
    assert p["body"].count(expected) == 4  # summary and three relocated bodies


def test_fence_on_first_line_of_comment_is_prose():
    # The severity prefix means the body's first line is not a fence.
    m, f = ctx()
    r = review()
    r["comments"][0]["body"] = "```\n@octocat\n```"
    r["comments"][1]["body"] = "```\n@octocat\n```"
    p = post.build_payload(r, HEAD, m, f)
    assert p["comments"][0]["body"] == f"**bug**: ```\n@{Z}octocat\n```"
    assert f"**nit**: ```\n@{Z}octocat\n```" in p["body"]


def test_print_payload_is_json():
    m, f = ctx()
    buf = io.StringIO()
    post.print_payload("o/r", 7, post.build_payload(review(), HEAD, m, f), out=buf)
    doc = json.loads(buf.getvalue())
    assert doc["endpoint"] == "POST /repos/o/r/pulls/7/reviews"
    assert doc["payload"]["event"] == "COMMENT"


class FakeAPI:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def request(self, method, path, token, json=None, expected=(200,), **kw):
        self.calls.append((method, path, json))
        status = self.statuses.pop(0)
        if status not in expected:
            raise GitHubError(status, "Unprocessable Entity")

        class R:
            def json(self_inner):
                return {"id": 99}
        return R()


def test_post_retries_422_with_comments_in_body():
    m, f = ctx()
    payload = post.build_payload(review(), HEAD, m, f)
    api = FakeAPI([422, 200])
    resp = post.post_review(api, "tok", "o/r", 7, payload,
                            rebuild_without_inline=lambda: post.build_payload(
                                review(), HEAD, m, f, relocate_all=True))
    assert resp == {"id": 99}
    assert [c[1] for c in api.calls] == ["/repos/o/r/pulls/7/reviews"] * 2
    assert api.calls[0][2]["comments"] and api.calls[1][2]["comments"] == []


def test_post_other_errors_propagate():
    m, f = ctx()
    api = FakeAPI([403])
    with pytest.raises(GitHubError):
        post.post_review(api, "tok", "o/r", 7, post.build_payload(review(), HEAD, m, f),
                         rebuild_without_inline=lambda: {})
    assert len(api.calls) == 1
