#!/usr/bin/env python3
"""Check the reviewer's JSON output against orchestrator/src/reviewbot/schema.json.

Usage:
  check_output.py SCHEMA RESULT_JSON [--expect-finding PATH:LINE[:TOLERANCE]]
  check_output.py SCHEMA --self-test

RESULT_JSON is the stdout of the review container, i.e. what
"claude -p --output-format json" prints. The script requires a
structured_output field and validates it with reviewbot.validate.schema_errors,
the validator the orchestrator uses before posting, imported from
orchestrator/src in this checkout. That validator supports exactly the
keywords schema.json uses and rejects any other keyword, so an unsupported
schema feature cannot pass unchecked.

--expect-finding requires a comment on PATH whose line is within TOLERANCE
(default 2) of LINE and whose severity is bug or security.

Standard library only (the orchestrator's validate module is too).
"""

import json
import sys
from pathlib import Path

# One validator for the orchestrator and the e2e tests. The path is derived
# from this file's location so the script also works under "python3 -I".
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "orchestrator" / "src"))
from reviewbot.validate import schema_errors  # noqa: E402


class Invalid(Exception):
    pass


def validate(value, schema, where="$"):
    try:
        errors = schema_errors(value, schema, where)
    except ValueError as exc:  # keyword or type the validator does not implement
        raise Invalid(str(exc)) from exc
    if errors:
        raise Invalid("; ".join(errors))


def check_result(schema, result):
    if not isinstance(result, dict):
        raise Invalid("result is not a JSON object")
    if "structured_output" not in result or result["structured_output"] is None:
        raise Invalid("result has no structured_output (subtype=%r, is_error=%r)"
                      % (result.get("subtype"), result.get("is_error")))
    validate(result["structured_output"], schema)
    return result["structured_output"]


def self_test(schema):
    good = {"summary": "ok", "verdict": "comment",
            "comments": [{"path": "a.py", "line": 3, "severity": "bug", "body": "x"}]}
    check_result(schema, {"structured_output": good})
    bad_cases = {
        "missing structured_output": {"result": "text"},
        "null structured_output": {"structured_output": None},
        "missing verdict": {"structured_output": {"summary": "s", "comments": []}},
        "bad verdict": {"structured_output": dict(good, verdict="merge")},
        "line 0": {"structured_output": dict(good, comments=[dict(good["comments"][0], line=0)])},
        "line string": {"structured_output": dict(good, comments=[dict(good["comments"][0], line="3")])},
        "line bool": {"structured_output": dict(good, comments=[dict(good["comments"][0], line=True)])},
        "bad severity": {"structured_output": dict(good, comments=[dict(good["comments"][0], severity="critical")])},
        "summary too long": {"structured_output": dict(good, summary="x" * 4001)},
        "body too long": {"structured_output": dict(good, comments=[dict(good["comments"][0], body="x" * 2001)])},
        "31 comments": {"structured_output": dict(good, comments=[good["comments"][0]] * 31)},
        "comment missing path": {"structured_output": dict(good, comments=[{"line": 1, "severity": "nit", "body": "b"}])},
        "comments not array": {"structured_output": dict(good, comments={})},
    }
    failures = 0
    for name, case in bad_cases.items():
        try:
            check_result(schema, case)
        except Invalid:
            continue
        print("self-test: checker accepted bad input: " + name)
        failures += 1
    try:
        validate("x", {"type": "string", "pattern": "^y$"})
        print("self-test: checker accepted an unsupported keyword")
        failures += 1
    except Invalid:
        pass
    print("self-test: %d bad inputs rejected, %d accepted" % (len(bad_cases) + 1 - failures, failures))
    return failures == 0


def main(argv):
    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        schema = json.load(f)
    if argv[2] == "--self-test":
        return 0 if self_test(schema) else 1
    with open(argv[2]) as f:
        text = f.read()
    try:
        result = json.loads(text)
    except ValueError as exc:
        print("stdout is not JSON: %s" % exc)
        return 1
    try:
        out = check_result(schema, result)
    except Invalid as exc:
        print("INVALID: %s" % exc)
        return 1
    print("structured_output valid against schema: verdict=%s, %d comment(s)"
          % (out["verdict"], len(out["comments"])))
    for c in out["comments"]:
        print("  %s:%d [%s] %s" % (c["path"], c["line"], c["severity"], c["body"][:140].replace("\n", " ")))
    if len(argv) >= 5 and argv[3] == "--expect-finding":
        parts = argv[4].split(":")
        path, line = parts[0], int(parts[1])
        tol = int(parts[2]) if len(parts) > 2 else 2
        hits = [c for c in out["comments"] if c["path"] == path
                and abs(c["line"] - line) <= tol and c["severity"] in ("bug", "security")]
        if not hits:
            print("EXPECTED FINDING MISSING: no bug/security comment on %s near line %d" % (path, line))
            return 1
        print("expected finding present: %s:%d [%s]" % (hits[0]["path"], hits[0]["line"], hits[0]["severity"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
