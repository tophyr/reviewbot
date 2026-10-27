import copy
import json
import secrets as pysecrets
import string

import pytest

from reviewbot import validate
from reviewbot.validate import Secrets, validate_review

ALNUM = string.ascii_letters + string.digits


def rand(n, alphabet=ALNUM + "-_"):
    return "".join(pysecrets.choice(alphabet) for _ in range(n))


REAL = "sk-ant-oat01-" + rand(95)
GH_IN_USE = "ghs_" + rand(36, ALNUM)


def clean_review():
    return {
        "summary": "Looks reasonable. One possible off-by-one.",
        "verdict": "comment",
        "comments": [
            {"path": "src/calc.py", "line": 6, "severity": "bug", "body": "Check the sign here."},
            {"path": "src/new.py", "line": 1, "severity": "nit", "body": "Typo."},
        ],
    }


def stdout_for(structured, **extra):
    doc = {"type": "result", "subtype": "success", "is_error": False, "result": "done",
           "structured_output": structured}
    doc.update(extra)
    return json.dumps(doc)


def secrets():
    return Secrets(real_token=REAL, github_tokens=[GH_IN_USE])


def run(structured, **kw):
    return validate_review(stdout_for(structured), secrets(), **kw)


def test_clean_review_is_accepted():
    r = run(clean_review())
    assert r.ok, str(r)
    assert r.structured == clean_review()
    assert r.payload["body"]


def with_text(text, where="summary"):
    s = clean_review()
    if where == "summary":
        s["summary"] = s["summary"] + " " + text
    elif where == "comment":
        s["comments"][1]["body"] = text
    elif where == "path":
        s["comments"][0]["path"] = text
    return s


@pytest.mark.parametrize("where", ["summary", "comment", "path"])
def test_refuses_the_real_claude_token(where):
    r = run(with_text(REAL, where))
    assert (r.ok, r.step, r.code) == (False, 2, "claude_token"), str(r)
    assert REAL not in r.reason


def test_refuses_the_real_claude_token_in_a_key():
    # The schema lets the model add keys to a comment; they are scanned too.
    s = clean_review()
    s["comments"][0][REAL] = 1
    r = run(s)
    assert (r.ok, r.step, r.code) == (False, 2, "claude_token"), str(r)


@pytest.mark.parametrize("where", ["summary", "comment", "path"])
def test_refuses_a_github_token_in_use(where):
    r = run(with_text(GH_IN_USE, where))
    assert (r.ok, r.step, r.code) == (False, 3, "github_token"), str(r)
    assert GH_IN_USE not in r.reason


def test_claude_check_runs_before_github_check():
    assert run(with_text(REAL + " " + GH_IN_USE)).step == 2


# Only the secrets the orchestrator holds are refused. Other token-shaped text,
# such as a key quoted from the pull request or the reviewer image's dummy
# token, is posted.
@pytest.mark.parametrize("text", [
    "sk-ant-api03-" + rand(93),
    "ghp_" + rand(36, ALNUM),
    "sk-ant-oat01-dummy",
], ids=["claude-shaped", "github-shaped", "dummy"])
def test_tokens_the_orchestrator_does_not_hold_are_allowed(text):
    r = run(with_text(text))
    assert r.ok, str(r)


def test_read_real_token_strips_whitespace(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", f" {REAL}\n")
    assert validate.read_real_token() == REAL


def _maximal_review(body):
    """The schema's maximum: a 4000-character summary and 30 comments of the
    longest allowed body, each relocated into the review body with a
    300-character path (the length post._inline_path keeps) and a 10-digit
    line, the longest framing build_payload adds."""
    return {"summary": "a" * 4000, "verdict": "request_changes",
            "comments": [{"path": "p" * 300, "line": 9_999_999_999, "severity": "suggestion",
                          "body": body} for _ in range(30)]}


def _build(st):
    from reviewbot import post
    return post.build_payload(st, "0" * 40, {}, set())


def test_schema_maximum_fits_under_the_limit():
    # The 1,500-character comment limit keeps the largest schema-valid review
    # (4,000-character summary, 30 relocated comments with 300-character paths)
    # under MAX_TOTAL_BODY.
    schema = validate.load_schema()
    body_max = schema["properties"]["comments"]["items"]["properties"]["body"]["maxLength"]
    assert body_max == 1500
    s = _maximal_review("b" * body_max)
    r = run(s, build_payload=_build)
    assert r.ok, str(r)
    assert validate.total_body_length(r.payload) < validate.MAX_TOTAL_BODY
    # one character more is a schema error
    s["comments"][0]["body"] += "b"
    assert run(s, build_payload=_build).step == 1


def test_too_long_is_refused():
    # Schema-valid text can still exceed the limit once defang adds its
    # zero-width spaces ("#1" grows by half).
    s = _maximal_review("#1" * 750)
    r = run(s, build_payload=_build)
    assert (r.ok, r.step, r.code) == (False, 4, "too_long")
    # the same review without reference forms passes, so the length is what failed
    for c in s["comments"]:
        c["body"] = "b" * 1500
    assert run(s, build_payload=_build).ok


def test_length_counts_the_payload_that_would_be_posted():
    s = clean_review()
    r = run(s, build_payload=lambda st: {"body": "x" * 60000, "comments": []})
    assert (r.ok, r.step) == (False, 4)


SCHEMA_BAD = {
    "not json": "this is not json",
    "is_error": stdout_for(clean_review(), is_error=True, subtype="error_max_turns"),
    "missing structured_output": json.dumps({"type": "result", "is_error": False, "result": "x"}),
}


@pytest.mark.parametrize("name", list(SCHEMA_BAD))
def test_refuses_missing_output(name):
    r = validate_review(SCHEMA_BAD[name], secrets())
    assert (r.ok, r.step, r.code) == (False, 1, "no_structured_output")


def _mutations():
    def m(fn):
        s = clean_review()
        fn(s)
        return s
    return {
        "missing verdict": m(lambda s: s.pop("verdict")),
        "bad verdict": m(lambda s: s.update(verdict="APPROVE")),
        "line zero": m(lambda s: s["comments"][0].update(line=0)),
        "line bool": m(lambda s: s["comments"][0].update(line=True)),
        "line string": m(lambda s: s["comments"][0].update(line="6")),
        "bad severity": m(lambda s: s["comments"][0].update(severity="critical")),
        "summary too long": m(lambda s: s.update(summary="x" * 4001)),
        "body too long": m(lambda s: s["comments"][0].update(body="x" * 2001)),
        "too many comments": m(lambda s: s.update(comments=s["comments"] * 16)),
        "comment missing body": m(lambda s: s["comments"][0].pop("body")),
        "comments not array": m(lambda s: s.update(comments={})),
    }


@pytest.mark.parametrize("name", list(_mutations()))
def test_refuses_schema_violations(name):
    r = run(_mutations()[name])
    assert (r.ok, r.step, r.code) == (False, 1, "schema"), str(r)


def test_schema_error_does_not_echo_model_text():
    s = clean_review()
    s["verdict"] = "sk-ant-api03-" + rand(40)
    r = run(s)
    assert r.step == 1
    assert s["verdict"] not in r.reason


def test_array_form_of_stdout():
    doc = [{"type": "system"}, {"type": "result", "is_error": False,
                                "structured_output": clean_review()}]
    assert validate_review(json.dumps(doc), secrets()).ok


def test_package_schema_is_a_regular_file_the_validator_can_use():
    # The reviewer image is built from this file (build-images.sh passes its
    # directory as a build context), so it must be a real file in the package.
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    package = root / "orchestrator" / "src" / "reviewbot" / "schema.json"
    assert package.is_file() and not package.is_symlink()
    assert not (root / "reviewer" / "schema.json").exists()
    schema = validate.load_schema()
    assert schema == json.loads(package.read_text())
    # the keys schema_errors reads, for the review object and for a comment
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"summary", "verdict", "comments"}
    props = schema["properties"]
    assert props["summary"]["maxLength"] > 0
    assert props["verdict"]["enum"] == ["approve", "comment", "request_changes"]
    assert props["comments"]["maxItems"] > 0
    item = props["comments"]["items"]
    assert set(item["required"]) == {"path", "line", "severity", "body"}
    assert item["properties"]["line"]["minimum"] == 1
    assert item["properties"]["body"]["maxLength"] > 0
    assert validate.schema_errors(clean_review(), schema) == []
    assert validate.schema_errors(dict(clean_review(), verdict="merge"), schema)


def test_unknown_schema_keyword_is_an_error():
    with pytest.raises(ValueError):
        validate.schema_errors("x", {"type": "string", "pattern": "^x$"})


def test_e2e_checker_uses_this_validator():
    # tests/e2e/check_output.py must validate with schema_errors, not a copy.
    import runpy
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / "tests" / "e2e" / "check_output.py"
    g = runpy.run_path(str(script))
    assert g["schema_errors"] is validate.schema_errors
    schema = validate.load_schema()
    # A keyword the orchestrator's validator accepts must be accepted there too,
    # and a value it refuses must be refused there too.
    described = dict(schema, description="reviewer output")
    g["validate"](clean_review(), described)
    bad = dict(clean_review(), extra="x")
    with pytest.raises(g["Invalid"], match="unexpected field"):
        g["validate"](bad, dict(schema, additionalProperties=False))
