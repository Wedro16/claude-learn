from learner.attribution import classify, parse_added_lines, record_claude_lines

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
    result = classify(store.conn, ["s1"], DIFF)
    ranges = result["files"]["src/retry.py"]
    assert ranges["claude"] == [[1, 2]]
    assert ranges["claude_modified"] == [[3, 3]]
    assert ranges["human"] == [[4, 4], [6, 6]]
    assert result["totals"] == {"claude": 2, "claude_modified": 1, "human": 2}


def test_lines_from_other_sessions_are_ignored(store):
    record_claude_lines(store.conn, "other", "/repo/src/retry.py", "def retry(fn, attempts=3):")
    assert classify(store.conn, ["s1"], DIFF)["totals"]["claude"] == 0
