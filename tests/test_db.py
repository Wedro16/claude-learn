import os
import stat

from learner.db import connect


def test_learner_files_are_owner_only(home):
    os.umask(0o022)
    connect()
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "learner.db").st_mode) == 0o600


def test_existing_wider_files_are_tightened(home):
    home.mkdir(mode=0o755)
    (home / "learner.db").touch(mode=0o644)
    os.chmod(home / "learner.db", 0o644)
    connect()
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(home / "learner.db").st_mode) == 0o600


def test_older_databases_gain_new_columns(home, tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, started_at TEXT NOT NULL)")
    old.commit()
    old.close()
    conn = connect(path)
    assert "repo" in {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
