#!/usr/bin/env python3
"""Static checks over the repository's shell scripts.

Usage: tests/lint_shell.py [ROOT]      (default: the checkout holding this file)
       tests/lint_shell.py --self-test  (each check must fire on a known-bad line)

Every *.sh file under ROOT is checked, except under tests/e2e/out (test output,
which holds third-party checkouts) and orchestrator/.venv:

1. "bash -n" ("sh -n" for a #!/bin/sh script).
2. "${" followed by a character that cannot start a parameter expansion, such
   as "${...}" inside double quotes. bash -n accepts it; at run time bash
   fails with "bad substitution" and the command never runs.
3. An assignment from a command substitution with no "||" on the line, in
   two forms:
   a. the command substitution runs grep, head, awk or find (any script).
      Under set -e (and pipefail, when the command is not last in a
      pipeline) a non-match or a find error makes the assignment fail and
      the script exits without printing its own message.
   b. any command, in a script that runs "set -e", when the assigned
      variable is then checked: a later line within CHECK_WINDOW logical
      lines names the variable and calls die, fail, config_error or exit,
      or names it in an if, elif or case line whose block does so within
      CHECK_SPAN lines. If the command fails,
      set -e exits at the assignment and that message is never printed.
      Assignments with "local", "export", "readonly" or "declare" are not
      covered by 3b: those builtins return their own status, so the script
      does not exit there.
   A variable that is assigned and never checked by a die/fail line is not
   reported by 3b: a failure there exits with the command's own error, and
   no message of the script's is skipped. Every assignment from a command
   substitution cannot be required to have "||" without reporting lines
   such as SCRIPT=$(readlink -f ...) or TMP=$(mktemp -d), where there is no
   better message to print.
4. A bare pipeline into grep, head or awk outside a condition, with no "||"
   or "&&" after it. The same silent exit happens when grep matches nothing.

Checks 3 and 4 read one logical line at a time (backslash continuations
joined) and skip text inside multi-line single-quoted strings, such as
inline python or the bash wrappers passed to "bash -c". Those wrappers must
be checked by reading them. 3b finds the check by the variable's name in
the next CHECK_WINDOW logical lines only, so a check further away, or one
made through another variable, is not seen. Exit status 1 if anything is
reported.
"""

import os
import re
import subprocess
import sys

SKIP_DIRS = ("tests/e2e/out", "orchestrator/.venv")
BAD_SUBST = re.compile(r"(?<!\\)\$\{(?=[^A-Za-z0-9_#!@*?$-])")
CMD = r"(?:grep|head|awk|find)"
ASSIGN = re.compile(r"^\s*(?:local\s+|export\s+|readonly\s+)?[A-Za-z_][A-Za-z0-9_]*=")
CMD_IN_SUBST = re.compile(r"(?:\$\(|\||&&|;)\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*" + CMD + r"\b")
PIPE_INTO = re.compile(r"(?<!\|)\|(?!\|)\s*(?:grep|head|awk)\b")
CONDITION = re.compile(r"^\s*(?:if|elif|while|until|!)\s")
PLAIN_SUBST_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=[^\s;]*\$\(")
SET_E = re.compile(r"^\s*set\s+(?:-[A-Za-z]*e|-o\s+errexit)", re.M)
REPORTS = re.compile(r"\b(?:die|fail|config_error|exit)\b(?!\s*\(\))")  # not a definition
BLOCK_OPENER = re.compile(r"^\s*(?:if|elif|case)\s")
CHECK_WINDOW = 10
CHECK_SPAN = 3


def scripts(root):
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(d for d in dirnames if d != ".git"
                             and os.path.normpath(os.path.join(rel, d)) not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(".sh"):
                yield os.path.join(dirpath, name)


def strip_substitutions(line):
    """The line with every $(...) removed (nested ones too)."""
    out, depth, i = [], 0, 0
    while i < len(line):
        if line.startswith("$(", i):
            depth += 1
            i += 2
            continue
        if depth and line[i] == "(":
            depth += 1
        elif depth and line[i] == ")":
            depth -= 1
        elif not depth:
            out.append(line[i])
        i += 1
    return "".join(out)


def logical_lines(text):
    """(line number, logical line) pairs. Backslash continuations are joined,
    comment lines are skipped, and a single-quoted string that spans lines
    is cut out: the opening line is joined to what follows the closing quote,
    so text such as "|| x=" after the string is part of the logical line.
    Quote tracking is rough: it follows ', " and $( ... ) nesting (a ' inside
    "$( ... )" starts a quoted string, as in bash)."""
    in_single = False
    stack = []  # in_double of each open $( ... )
    pending, start = "", 0
    for n, raw in enumerate(text.split("\n"), 1):
        starts_quoted = in_single
        in_double = False
        if not starts_quoted:
            stack = []
        close_at = None
        prev = ""
        for idx, ch in enumerate(raw):
            if in_single:
                if ch == "'":
                    in_single = False
                    if close_at is None and starts_quoted:
                        close_at = idx
            elif ch == "'" and not in_double and prev != "\\":
                in_single = True
            elif ch == '"' and prev != "\\":
                in_double = not in_double
            elif ch == "(" and prev == "$":
                stack.append(in_double)
                in_double = False
            elif ch == ")" and stack and not in_double:
                in_double = stack.pop()
            elif ch == "#" and not in_double and prev in ("", " ", "\t"):
                break
            prev = ch
        if starts_quoted:
            if close_at is None or not pending:
                continue  # inside the string, or a string not opened by a kept line
            raw = raw[close_at:]
        if not pending:
            start = n
        if in_single:
            pending += raw  # the string continues on the next line
            continue
        if raw.rstrip().endswith("\\"):
            pending += raw.rstrip()[:-1] + " "
            continue
        line = pending + raw
        pending = ""
        if line.lstrip().startswith("#"):
            continue
        yield start, line


def check_file(path):
    problems = []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    shell = "sh" if text.startswith("#!/bin/sh") else "bash"
    r = subprocess.run([shell, "-n", path], capture_output=True, text=True)
    if r.returncode != 0:
        problems.append(f"{path}: {shell} -n failed: {r.stderr.strip()}")
    for n, raw in enumerate(text.split("\n"), 1):
        if BAD_SUBST.search(raw) and not raw.lstrip().startswith("#"):
            problems.append(f"{path}:{n}: '${{' not followed by a parameter name (bad substitution at run time)")
    errexit = bool(SET_E.search(text))
    lines = list(logical_lines(text))
    for i, (n, line) in enumerate(lines):
        if CONDITION.match(line):
            continue
        if ASSIGN.match(line) and CMD_IN_SUBST.search(line) and "||" not in line:
            problems.append(f"{path}:{n}: assignment from grep/head/awk/find with no '||': "
                            "a non-match or error exits a set -e script silently")
            continue
        m = PLAIN_SUBST_ASSIGN.match(line)
        if errexit and m and "||" not in line:
            checked = checked_by_report(lines, i, m.group(1))
            if checked:
                problems.append(f"{path}:{n}: assignment from a command substitution with no '||', "
                                f"and line {checked} reports on ${m.group(1)}: if the command fails, "
                                "set -e exits here and that message is never printed")
                continue
        top = strip_substitutions(line)
        m = PIPE_INTO.search(top)
        if m and not ASSIGN.match(line):
            rest = top[m.end():]
            if "||" not in rest and "&&" not in rest:
                problems.append(f"{path}:{n}: bare pipeline into grep/head/awk with no '||': "
                                "a non-match exits a set -e script silently")
    return problems


def checked_by_report(lines, i, var):
    """Line number of the first of the next CHECK_WINDOW logical lines that
    names var and either calls die, fail, config_error or exit itself, or
    opens an if, elif or case block whose next CHECK_SPAN lines do; None if
    there is none. Stops at a new assignment to var."""
    ref = re.compile(r"\$\{?#?" + re.escape(var) + r"\b")
    reassign = re.compile(r"^\s*(?:local\s+)?" + re.escape(var) + r"=")
    for j in range(i + 1, min(i + 1 + CHECK_WINDOW, len(lines))):
        n, line = lines[j]
        if reassign.match(line):
            return None
        if not ref.search(line):
            continue
        if REPORTS.search(line):
            return n
        if BLOCK_OPENER.match(line) and any(REPORTS.search(l) for _, l in lines[j + 1:j + 1 + CHECK_SPAN]):
            return n
    return None


SELF_TEST_BAD = {
    "bad substitution": 'die "expanding ${...} in it"',
    "grep|cut assignment": 'line="$(grep -n x f | cut -d: -f1)"',
    "plain grep assignment": 'n=$(grep -c x f)',
    "find|wc after cd &&": 'n="$(cd d && find . -type f | wc -l)"',
    "local head assignment": 'local first="$(head -c 3 f)"',
    "bare pipe into grep": "printf '%s\\n' \"$out\" | grep -F 'x'",
    "continued pipe into grep": "printf '%s\\n' \"$out\" \\\n  | grep -F 'x'",
    "checked capture": 'deps=$(python -c "import x; print(1)")\n[ "$deps" = 1 ] || die "deps missing"',
    "checked capture, case": 'f=$(cmd)\ncase "$f" in\n  /venv/*) ;;\n  *) die "bad $f" ;;\nesac',
    "checked capture, multi-line quote": "tok=\"$(python3 -c '\nimport sys\nsys.exit(1)\n')\"\nif [ -z \"$tok\" ]; then\n  exit 2\nfi",
    "checked capture, if": 'tok="$(cmd)"\nlog "read"\nif [ "${#tok}" -lt 20 ]; then\n  log short\n  exit 2\nfi',
}
SELF_TEST_GOOD = [
    'line="$(grep -n x f | cut -d: -f1)" || line=none',
    "printf '%s' \"$x\" | grep -q y || die no",
    "if printf '%s' \"$x\" | grep -q y; then :; fi",
    'echo "${HOME} ${#x} ${x:-y} \\${...}"',
    'pass "ran ($(grep -E x f))"',
    "python3 -c '\nimport sys\nx = 1 | 2 | head\n' arg",
    'deps=$(cmd) || deps=""\n[ "$deps" = 1 ] || die "deps missing"',
    'dir=$(mktemp -d)\necho "$dir"\ncd "$dir"',
    'if v=$(cmd); then :; fi\n[ -n "$v" ] || die "empty"',
    'v=$(cmd)\nv=other\n[ -n "$v" ] || die "empty"',
    "tok=\"$(python3 -c '\nimport sys\nsys.exit(1)\n')\" || tok=\"\"\nif [ -z \"$tok\" ]; then\n  exit 2\nfi",
    'out="$(date)"\nlog() { echo "$out"; }\nfail() { log "FAIL"; }',
    'd=$(mktemp -d)\ntrap \'rm -rf "$d"\' EXIT\n[ -n "$x" ] || exit 1',
]


def self_test():
    import tempfile
    failures = 0
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.sh")
        for name, line in SELF_TEST_BAD.items():
            with open(path, "w") as fh:
                fh.write("#!/bin/bash\nset -euo pipefail\n" + line + "\n")
            if not check_file(path):
                print(f"self-test: not reported: {name}")
                failures += 1
        for line in SELF_TEST_GOOD:
            with open(path, "w") as fh:
                fh.write("#!/bin/bash\nset -euo pipefail\ndie() { :; }; pass() { :; }\n" + line + "\n")
            found = check_file(path)
            if found:
                print(f"self-test: false report for {line!r}: {found}")
                failures += 1
    print(f"lint_shell self-test: {len(SELF_TEST_BAD)} bad lines, {len(SELF_TEST_GOOD)} good lines, "
          f"{failures} wrong")
    return failures == 0


def main(argv):
    if argv[1:] == ["--self-test"]:
        return 0 if self_test() else 1
    root = os.path.abspath(argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(__file__), ".."))
    files = list(scripts(root))
    problems = [p for f in files for p in check_file(f)]
    for p in problems:
        print(p)
    print(f"lint_shell: {len(files)} scripts checked, {len(problems)} problems")
    return 1 if problems or not files else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
