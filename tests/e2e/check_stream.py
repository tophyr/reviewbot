#!/usr/bin/env python3
"""Inspect a "claude -p --output-format stream-json --verbose" transcript.

Usage:
  check_stream.py STREAM.jsonl            check tools offered and used
  check_stream.py STREAM.jsonl --outside  also require that every tool call
                                          naming a path outside /work got an
                                          error result
  check_stream.py STREAM.jsonl --outside --deny P1,P2
                                          also require that each listed path
                                          was attempted and every attempt got
                                          an error result
  check_stream.py --self-test

Checks:
  - the init message exists and offers only Read, Grep, Glob and
    StructuredOutput (the tool claude adds for --json-schema)
  - no forbidden tool (Bash, WebFetch, ...) is offered
  - every tool call the model made used an allowed tool
  - with --outside: at least one call named a path outside /work, and every
    such call's tool_result is an error
"""

import json
import posixpath
import sys

ALLOWED = {"Read", "Grep", "Glob", "StructuredOutput"}
FORBIDDEN = {"Bash", "WebFetch", "WebSearch", "PowerShell", "Write", "Edit",
             "NotebookEdit", "Task", "Agent"}


def _outside_work(path):
    """True when path, resolved against the working directory /work (".."
    included), is not /work or under it. "~" is the home directory."""
    if path.startswith("~"):
        return True
    full = posixpath.normpath(posixpath.join("/work", path))
    return not (full == "/work" or full.startswith("/work/"))


def analyse(lines):
    init_tools = None
    calls = {}  # id -> (name, outside path or None)
    order = []
    results = {}  # tool_use_id -> is_error
    bad_lines = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            bad_lines += 1
            continue
        if m.get("type") == "system" and m.get("subtype") == "init":
            init_tools = m.get("tools")
        msg = m.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if m.get("type") == "assistant" and block.get("type") == "tool_use":
                inp = block.get("input") or {}
                outside = None
                for k in ("file_path", "path"):
                    v = inp.get(k)
                    if isinstance(v, str) and _outside_work(v):
                        outside = v
                named = [inp.get(k) for k in ("file_path", "path") if isinstance(inp.get(k), str)]
                calls[block.get("id")] = (block.get("name"), outside, named)
                order.append(block.get("id"))
            if m.get("type") == "user" and block.get("type") == "tool_result":
                results[block.get("tool_use_id")] = bool(block.get("is_error"))
    return init_tools, calls, order, results, bad_lines


def check(lines, require_outside_denied=False, must_deny=()):
    init_tools, calls, order, results, bad_lines = analyse(lines)
    problems = []
    used = [calls[i][0] for i in order]
    outside = [(calls[i][0], calls[i][1], results.get(i)) for i in order if calls[i][1]]
    report = ["init tools: %s" % init_tools, "tool calls: %s" % used,
              "calls outside /work (tool, path, is_error): %s" % outside]
    if init_tools is None:
        problems.append("no init message")
    else:
        if not init_tools or set(init_tools) - ALLOWED:
            problems.append("unexpected tools offered: %s" % sorted(set(init_tools) - ALLOWED))
        if FORBIDDEN & set(init_tools):
            problems.append("forbidden tools offered: %s" % sorted(FORBIDDEN & set(init_tools)))
    if set(used) - ALLOWED:
        problems.append("tools called outside the allowlist: %s" % sorted(set(used) - ALLOWED))
    if bad_lines:
        problems.append("%d non-JSON lines" % bad_lines)
    for target in must_deny:
        attempts = [results.get(i) for i in order if target in calls[i][2]]
        report.append("calls on %s: is_error=%s" % (target, attempts))
        if not attempts:
            problems.append("no tool call on %s, so its denial was not exercised" % target)
        elif any(a is not True for a in attempts):
            problems.append("a tool call on %s was not denied" % target)
    if require_outside_denied:
        if not outside:
            problems.append("no tool call named a path outside /work, so denial was not exercised")
        for name, path, is_error in outside:
            if is_error is not True:
                problems.append("%s of %s outside /work was not denied (is_error=%r)" % (name, path, is_error))
    return problems, report


def _line(obj):
    return json.dumps(obj)


def self_test():
    init_ok = _line({"type": "system", "subtype": "init", "tools": ["Read", "Grep", "Glob", "StructuredOutput"]})
    init_bash = _line({"type": "system", "subtype": "init", "tools": ["Read", "Bash"]})
    odd = _line({"type": "result", "message": "a plain string"})
    use_bash = _line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "env"}}]}})
    read_proc = _line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t2", "name": "Read", "input": {"file_path": "/proc/self/environ"}}]}})
    proc_ok = _line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "is_error": False, "content": "X=1"}]}})
    proc_denied = _line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": "denied"}]}})
    cases = [
        ("clean stream passes", [init_ok, odd], False, True),
        ("missing init fails", [], False, False),
        ("Bash offered fails", [init_bash], False, False),
        ("Bash called fails", [init_ok, use_bash], False, False),
        ("allowed outside read fails with --outside", [init_ok, read_proc, proc_ok], True, False),
        ("denied outside read passes with --outside", [init_ok, read_proc, proc_denied], True, True),
        ("no outside attempt fails with --outside", [init_ok], True, False),
    ]
    # A path that leaves /work through ".." is outside /work.
    for n, path in enumerate(("/work/../proc/self/environ", "../proc/self/environ")):
        tid = "tdot%d" % n
        use = _line({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": path}}]}})
        ok = _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": False, "content": "X=1"}]}})
        denied = _line({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": True, "content": "denied"}]}})
        cases += [
            ("allowed read of %s fails with --outside, beside a denied one" % path,
             [init_ok, read_proc, proc_denied, use, ok], True, False),
            ("denied read of %s passes with --outside" % path, [init_ok, use, denied], True, True),
        ]
    link_use = _line({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": "/work/repo/link"}}]}})
    link_ok = _line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t3", "is_error": False, "content": "X=1"}]}})
    link_denied = _line({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t3", "is_error": True, "content": "no"}]}})
    deny_cases = [
        ("allowed symlink read fails with --deny", [init_ok, link_use, link_ok], False, False),
        ("denied symlink read passes with --deny", [init_ok, link_use, link_denied], False, True),
        ("missing symlink attempt fails with --deny", [init_ok], False, False),
    ]
    bad = 0
    for path, expect in (("repo/../diff.patch", False), ("/work", False), ("/work/repo/x", False),
                         ("/work/../proc/self/environ", True), ("../proc/self/environ", True),
                         ("/workx/y", True), ("~/.claude.json", True)):
        if _outside_work(path) != expect:
            print("self-test FAILED: _outside_work(%r) is not %r" % (path, expect))
            bad += 1
    for name, lines, outside, expect_ok in cases:
        problems, _ = check(lines, outside)
        if (not problems) != expect_ok:
            print("self-test FAILED: %s (problems=%s)" % (name, problems))
            bad += 1
    for name, lines, outside, expect_ok in deny_cases:
        problems, _ = check(lines, outside, ["/work/repo/link"])
        if (not problems) != expect_ok:
            print("self-test FAILED: %s (problems=%s)" % (name, problems))
            bad += 1
    cases = cases + deny_cases
    print("self-test: %d/%d cases behaved as expected" % (len(cases) - bad, len(cases)))
    return bad == 0


def main(argv):
    if len(argv) == 2 and argv[1] == "--self-test":
        return 0 if self_test() else 1
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(argv[1]) as f:
        lines = f.readlines()
    must_deny = []
    if "--deny" in argv:
        must_deny = [p for p in argv[argv.index("--deny") + 1].split(",") if p]
    problems, report = check(lines, "--outside" in argv[2:], must_deny)
    for r in report:
        print(r)
    for p in problems:
        print("PROBLEM: " + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
