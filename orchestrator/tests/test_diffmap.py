from conftest import fixture_text

from reviewbot.diffmap import can_place, parse_diff, unquote_git_path

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
