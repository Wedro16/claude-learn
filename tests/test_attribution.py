from learner.attribution import classify, parse_added_lines, record_claude_lines, unquote_path

SINCE = "2000-01-01T00:00:00+00:00"

DIFF = """\
diff --git a/src/retry.py b/src/retry.py
new file mode 100644
--- /dev/null
+++ b/src/retry.py
@@ -0,0 +1,6 @@
+def retry(fn, attempts=3):
+    for i in range(attempts):
+        delay = min(2 ** i, 30)
+        log.info("retrying after %s seconds", delay)
+
+    raise TimeoutError("gave up")
"""


def test_parse_added_lines_tracks_line_numbers():
    added = parse_added_lines(DIFF)
    assert list(added) == ["src/retry.py"]
    assert added["src/retry.py"][0] == (1, "def retry(fn, attempts=3):")
    assert added["src/retry.py"][5] == (6, '    raise TimeoutError("gave up")')


def test_classify_exact_modified_and_human(store):
    claude_wrote = "\n".join([
        "def retry(fn, attempts=3):",
        "    for i in range(attempts):",
        "        delay = 2 ** i",
    ])
    record_claude_lines(store.conn, "s1", "/repo/src/retry.py", claude_wrote)
    result = classify(store.conn, DIFF, "/repo", SINCE)
    ranges = result["files"]["src/retry.py"]
    assert ranges["claude"] == [[1, 2]]
    assert ranges["claude_modified"] == [[3, 3]]
    assert ranges["human"] == [[4, 4], [6, 6]]
    assert result["totals"] == {"claude": 2, "claude_modified": 1, "human": 2}
    assert result["sessions"] == ["s1"]


def test_lines_claude_wrote_in_another_repository_are_ignored(store):
    record_claude_lines(store.conn, "s1", "/other-repo/src/retry.py", "def retry(fn, attempts=3):")
    assert classify(store.conn, DIFF, "/repo", SINCE)["totals"]["claude"] == 0


def test_any_session_with_recent_writes_counts(store):
    # A resumed session that started long ago still owns lines it wrote just now.
    store.conn.execute("UPDATE sessions SET started_at = '2000-01-01T00:00:00+00:00' WHERE id = 's1'")
    record_claude_lines(store.conn, "s1", "/repo/src/retry.py", "def retry(fn, attempts=3):")
    assert classify(store.conn, DIFF, "/repo", "2026-01-01T00:00:00+00:00")["totals"]["claude"] == 1


def test_old_writes_age_out(store):
    record_claude_lines(store.conn, "s1", "/repo/src/retry.py", "def retry(fn, attempts=3):")
    assert classify(store.conn, DIFF, "/repo", "2999-01-01T00:00:00+00:00")["totals"]["claude"] == 0


def test_git_quoted_paths_are_decoded():
    assert unquote_path('"b/caf\\303\\251.py"') == "b/café.py"
    assert unquote_path('"b/say \\"hi\\".py"') == 'b/say "hi".py'
    assert unquote_path("b/plain.py") == "b/plain.py"
    diff = '+++ "b/caf\\303\\251.py"\n@@ -0,0 +1 @@\n+x = 1\n'
    assert list(parse_added_lines(diff)) == ["café.py"]
