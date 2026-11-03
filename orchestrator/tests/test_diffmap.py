import os
import re

from conftest import fixture_text

from reviewbot import pipeline
from reviewbot.diffmap import (_new_path, can_place, diff_positions, number_diff, parse_diff,
                               unquote_git_path)


def numbered_text(text: str) -> str:
    return "".join(number_diff(text))

# tests/fixtures/small.diff was produced by `git diff --no-color -M BASE HEAD`
# on a scratch repository with an edit, a rename with an edit, a deletion,
# two new files, a path with a space and a non-ASCII path.
EXPECTED = {
    "café.txt": {1},
    "docs/new_name.txt": set(range(2, 9)),
    "src/calc.py": set(range(3, 19)),
    "src/new.py": {1, 2},
    "with space.txt": {1, 2},
}


def files():
    return {x for x in fixture_text("small.files.txt").split("\n") if x}


def test_fixture_map_is_exact():
    assert parse_diff(fixture_text("small.diff")) == EXPECTED


def test_files_list_unquotes_git_paths():
    assert "café.txt" in files()
    assert "obsolete.txt" in files()


def test_places_added_and_context_lines():
    m, f = parse_diff(fixture_text("small.diff")), files()
    assert can_place(m, f, "src/calc.py", 6)       # added (changed) line
    assert can_place(m, f, "src/calc.py", 17)      # added at end
    assert can_place(m, f, "src/calc.py", 9)       # context line
    assert can_place(m, f, "docs/new_name.txt", 5)  # added line in a renamed file
    assert can_place(m, f, "docs/new_name.txt", 2)  # first context line of the hunk
    assert can_place(m, f, "with space.txt", 2)
    assert can_place(m, f, "café.txt", 1)


def test_relocates_lines_outside_the_diff():
    m, f = parse_diff(fixture_text("small.diff")), files()
    assert not can_place(m, f, "src/calc.py", 1)        # untouched, before the hunk
    assert not can_place(m, f, "src/calc.py", 19)       # past the end of the hunk
    assert not can_place(m, f, "docs/new_name.txt", 1)  # untouched
    assert not can_place(m, f, "docs/new_name.txt", 9)  # untouched, after the hunk
    assert not can_place(m, f, "docs/old_name.txt", 5)  # old name of a renamed file
    assert not can_place(m, f, "obsolete.txt", 1)       # deleted file
    assert not can_place(m, f, "README.md", 1)          # not in the PR


def test_path_must_be_in_files_list():
    m = parse_diff(fixture_text("small.diff"))
    assert not can_place(m, set(), "src/calc.py", 6)


def test_removed_lines_do_not_advance_new_numbering():
    diff = (
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        "@@ -10,4 +10,3 @@\n ctx10\n-gone11\n-gone12\n+new11\n ctx12\n"
        "@@ -40 +39,2 @@\n-old\n+n39\n+n40\n"
    )
    assert parse_diff(diff) == {"f": {10, 11, 12, 39, 40}}


def test_no_newline_marker_and_hunk_lines_that_look_like_headers():
    diff = (
        "diff --git a/f b/f\n--- a/f\n+++ b/f\n"
        "@@ -1,2 +1,3 @@\n a\n-b\n\\ No newline at end of file\n"
        "+++ b/not-a-header\n+b\n+--- also content\n"
    )
    assert parse_diff(diff) == {"f": {1, 2, 3}}


def test_binary_and_mode_only_changes_have_no_lines():
    diff = (
        "diff --git a/img.png b/img.png\nnew file mode 100644\nindex 0000000..1111111\n"
        "Binary files /dev/null and b/img.png differ\n"
        "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
    )
    assert parse_diff(diff) == {}


def test_unquote():
    assert unquote_git_path('"caf\\303\\251.txt"') == "café.txt"
    assert unquote_git_path('"a\\tb\\"c\\\\d"') == 'a\tb"c\\d'
    assert unquote_git_path("plain.txt") == "plain.txt"


# tests/fixtures/descent3_625.diff is `gh pr diff 625 -R DescentDevelopers/Descent3`
# at head ab48e06e: three files, the third (ddio/file.cpp) one hunk from line
# 214. A review of it cited ddio/file.cpp lines 49, 53 and 56, which are lines
# of the diff text; the lines meant were 223, 227 and 230 of the file.
D3_FILES = {"Descent3/init.cpp", "ddio/ddio.h", "ddio/file.cpp"}


def test_descent3_625_map_counts_each_file_from_its_own_hunk_header():
    assert parse_diff(fixture_text("descent3_625.diff")) == {
        "Descent3/init.cpp": set(range(1985, 1992)),
        "ddio/ddio.h": set(range(426, 436)),
        "ddio/file.cpp": set(range(214, 244)),
    }


def test_descent3_625_diff_text_positions_are_not_file_lines():
    text = fixture_text("descent3_625.diff")
    m = parse_diff(text)
    for wrong, right in ((49, 223), (53, 227), (56, 230)):
        assert not can_place(m, D3_FILES, "ddio/file.cpp", wrong)
        assert can_place(m, D3_FILES, "ddio/file.cpp", right)
        assert diff_positions(text, {("ddio/file.cpp", wrong)}) == {("ddio/file.cpp", wrong): right}


def test_descent3_625_numbered_diff_shows_file_lines():
    text = fixture_text("descent3_625.diff")
    numbered = numbered_text(text).split("\n")
    assert numbered[-1] == ""  # ends with a newline, like the input
    numbered.pop()
    assert len(numbered) == len(text.split("\n")) - 1
    # line 49 of the diff text, which the Read tool shows as line 49
    assert numbered[48] == "223:+  if (envr) {"
    assert numbered[52] == "227:+    if (envr) {"
    assert numbered[55] == "230:+#endif"
    assert numbered[4] == "@@ -1985,13 +1985,7 @@ void SetupTempDirectory(void) {"
    assert numbered[5] == "1985:   if (t_arg) {"
    assert numbered[8] == "-:-    std::error_code ec;"
    assert numbered[15] == "1988:+    Descent3_temp_directory = ddio_GetTempPath();"
    assert numbered[23] == "@@ -426,4 +426,10 @@ bool ddio_CreateLockFile(const std::filesystem::path &dir);"
    assert numbered[24] == "426:  */"


def numbered_pairs(numbered: str) -> dict[str, set[int]]:
    """{path: numbers} read back from number_diff output the way rules.md
    tells the reviewer to: each number belongs to the file named by the
    nearest "+++ " header above it."""
    out: dict[str, set[int]] = {}
    path = None
    for line in numbered.split("\n"):
        if line.startswith("+++ "):
            path = _new_path(line)
            continue
        number, sep, _ = line.partition(":")
        if sep and number.isdigit():
            out.setdefault(path, set()).add(int(number))
    return out


def test_numbered_diff_agrees_with_the_placement_map():
    for name in ("descent3_625.diff", "small.diff"):
        text = fixture_text(name)
        m = {p: s for p, s in parse_diff(text).items() if s}
        assert numbered_pairs(numbered_text(text)) == m, name


MULTI = (
    "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n"
    "@@ -1,2 +1,2 @@\n-one\n+ONE\n two\n"
    "diff --git a/b.c b/b.c\n--- a/b.c\n+++ b/b.c\n"
    "@@ -10,3 +10,4 @@ int f(void)\n ctx10\n+add11\n ctx12\n ctx13\n"
    "@@ -50,3 +51,2 @@ int g(void)\n ctx51\n-gone\n ctx52\n"
    "@@ -90 +90,2 @@\n ctx90\n+add91\n"
)


def test_multi_file_multi_hunk_numbers_restart_at_each_hunk_header():
    assert parse_diff(MULTI) == {"a.txt": {1, 2}, "b.c": {10, 11, 12, 13, 51, 52, 90, 91}}
    lines = numbered_text(MULTI).split("\n")
    assert lines[4] == "-:-one"
    assert lines[5] == "1:+ONE"
    assert lines[12] == "11:+add11"
    assert lines[15] == "@@ -50,3 +51,2 @@ int g(void)"
    assert lines[16] == "51: ctx51"
    assert lines[17] == "-:-gone"
    assert lines[18] == "52: ctx52"
    assert lines[21] == "91:+add91"
    # 1-based positions in the diff text; only those asked for are returned
    assert diff_positions(MULTI, {("b.c", 13), ("b.c", 22), ("a.txt", 22), ("b.c", 5)}) == {
        ("b.c", 13): 11, ("b.c", 22): 91}
    assert diff_positions(MULTI, set()) == {}
    # a position in the diff text is not a line of b.c that the diff shows
    m = parse_diff(MULTI)
    assert not can_place(m, {"a.txt", "b.c"}, "b.c", 22)
    assert can_place(m, {"a.txt", "b.c"}, "b.c", 91)


def test_numbered_diff_copies_deleted_files_and_headers_unchanged():
    text = fixture_text("small.diff")
    lines = numbered_text(text).split("\n")
    src = text.split("\n")
    assert len(lines) == len(src)
    for got, want in zip(lines, src):
        if not re.match(r"(\d+|-):", got):
            assert got == want
    assert "-gone" in lines  # the deleted file's line has no head path
    assert "6:+    return a - b  # subtract" in lines
    assert "-:-    return a - b" in lines
    assert "2:+y" in lines


def test_reviewer_rules_describe_the_numbered_diff_as_written():
    """reviewer/rules.md names the files the pipeline writes and shows their
    line format; a rename or format change must update both."""
    rules_path = os.path.join(os.path.dirname(__file__), "..", "..", "reviewer", "rules.md")
    with open(rules_path, encoding="utf-8") as fh:
        rules = fh.read()
    assert f"`/work/{pipeline.NUMBERED_DIFF}`" in rules
    assert f"`/work/{pipeline.NUMBERED_INTERDIFF}`" in rules
    example = re.search(r"`(120:\+[^`]*)` under `\+\+\+ b/src/app\.c`", rules).group(1)
    text = ("diff --git a/src/app.c b/src/app.c\n--- a/src/app.c\n+++ b/src/app.c\n"
            "@@ -119,0 +120 @@\n+" + example.split(":+", 1)[1] + "\n")
    assert numbered_text(text).split("\n")[4] == example
    assert "`-:`" in rules
