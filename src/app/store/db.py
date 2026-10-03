"""SQLite connection settings and numbered migrations."""

import sqlite3
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

MIGRATION_1 = """
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE notes (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  note_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('source', 'source_analysis')),
  stage TEXT NOT NULL CHECK (stage IN ('fetched', 'extracted', 'complete')),
  url TEXT NOT NULL,
  final_url TEXT,
  canonical_url TEXT NOT NULL,
  doi TEXT,
  title TEXT NOT NULL,
  content_type TEXT,
  via TEXT,
  body TEXT NOT NULL,
  pages_json TEXT NOT NULL DEFAULT '[]',
  word_count INTEGER NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  meta_json TEXT NOT NULL DEFAULT '{}',
  source_tier TEXT NOT NULL,
  utility REAL,
  derivative_of TEXT,
  analysis_of TEXT,
  minhash BLOB,
  links_json TEXT NOT NULL DEFAULT '[]',
  extract_failed INTEGER NOT NULL DEFAULT 0,
  claims_kept INTEGER NOT NULL DEFAULT 0,
  claims_dropped INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, note_id),
  UNIQUE (run_id, kind, canonical_url)
);
CREATE INDEX notes_doi ON notes (run_id, doi);
CREATE TABLE claims (
  run_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  note_id TEXT NOT NULL,
  claim TEXT NOT NULL,
  stance TEXT NOT NULL,
  stance_target TEXT NOT NULL,
  evidence_type TEXT NOT NULL,
  scope_conditions TEXT NOT NULL,
  quoted_support TEXT NOT NULL,
  numbers_json TEXT NOT NULL,
  entities_json TEXT NOT NULL,
  time_period TEXT,
  region TEXT,
  confidence TEXT NOT NULL,
  PRIMARY KEY (run_id, claim_id),
  FOREIGN KEY (run_id, note_id) REFERENCES notes (run_id, note_id) ON DELETE CASCADE
);
CREATE TABLE rejected_sources (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  canonical_url TEXT NOT NULL,
  url TEXT NOT NULL,
  reason TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  attempts INTEGER NOT NULL DEFAULT 1,
  retryable INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, canonical_url)
);
CREATE VIRTUAL TABLE notes_fts USING fts5(
  run_id UNINDEXED, note_id UNINDEXED, title, summary, body,
  tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER notes_fts_insert AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts (rowid, run_id, note_id, title, summary, body)
  VALUES (new.rowid, new.run_id, new.note_id, new.title, new.summary, new.body);
END;
CREATE TRIGGER notes_fts_update AFTER UPDATE OF title, summary, body ON notes BEGIN
  DELETE FROM notes_fts WHERE rowid = old.rowid;
  INSERT INTO notes_fts (rowid, run_id, note_id, title, summary, body)
  VALUES (new.rowid, new.run_id, new.note_id, new.title, new.summary, new.body);
END;
CREATE TRIGGER notes_fts_delete AFTER DELETE ON notes BEGIN
  DELETE FROM notes_fts WHERE rowid = old.rowid;
END;
"""

MIGRATION_2 = """
CREATE TABLE sessions (
  session_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  status TEXT NOT NULL
    CHECK (status IN ('interviewing', 'awaiting_decision', 'saved', 'approved')),
  interview_language TEXT NOT NULL,
  report_language TEXT,
  response_format TEXT CHECK (response_format IN ('short', 'structured', 'argumentative')),
  template_id TEXT,
  upload_digest TEXT NOT NULL DEFAULT '',
  digest_notice TEXT NOT NULL DEFAULT '',
  brief_text TEXT,
  brief_sha256 TEXT,
  approved_sha256 TEXT,
  archive_path TEXT,
  run_id TEXT
);
CREATE TABLE uploads (
  session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
  file_id TEXT NOT NULL,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('pdf', 'docx', 'md', 'txt')),
  size INTEGER NOT NULL,
  pages INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  stage TEXT NOT NULL CHECK (stage IN ('stored', 'extracted', 'distilled')),
  pages_json TEXT NOT NULL DEFAULT '[]',
  warnings_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  PRIMARY KEY (session_id, file_id)
);
CREATE TABLE upload_parts (
  session_id TEXT NOT NULL,
  file_id TEXT NOT NULL,
  part INTEGER NOT NULL,
  facts_json TEXT NOT NULL,
  PRIMARY KEY (session_id, file_id, part),
  FOREIGN KEY (session_id, file_id) REFERENCES uploads(session_id, file_id) ON DELETE CASCADE
);
ALTER TABLE runs ADD COLUMN session_id TEXT;
ALTER TABLE runs ADD COLUMN brief_sha256 TEXT;
ALTER TABLE runs ADD COLUMN brief_path TEXT;
ALTER TABLE runs ADD COLUMN tier TEXT;
ALTER TABLE runs ADD COLUMN summarize_model TEXT;
ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'created';
CREATE UNIQUE INDEX runs_session ON runs(session_id) WHERE session_id IS NOT NULL;
"""

MIGRATION_3 = """
CREATE TABLE searches (
  run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
  query_id TEXT NOT NULL,
  source TEXT NOT NULL,
  wave INTEGER NOT NULL,
  results_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, query_id, source)
);
ALTER TABLE runs ADD COLUMN settings_json TEXT;
"""

# Later milestones append their own migrations; never edit one that has shipped.
MIGRATIONS: list[str] = [MIGRATION_1, MIGRATION_2, MIGRATION_3]


WAL_RETRY_S = 5.0


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL. The switch needs an exclusive lock and SQLite may refuse it at once instead
    of waiting, so concurrent first openers retry briefly. The mode then persists in the file."""
    deadline = time.monotonic() + WAL_RETRY_S
    while str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def connect(path: Path | str) -> sqlite3.Connection:
    """Open ``path`` for crash-safe use: WAL, full fsync, foreign keys, 5 s busy timeout.

    The connection is in autocommit mode; writers open `BEGIN IMMEDIATE` themselves.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    _enable_wal(conn)
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, migrations: Sequence[str] = MIGRATIONS) -> None:
    """Apply every migration above the database's `user_version`; each is all-or-nothing.

    If another process applied a migration while we waited for the write lock, the script fails
    with "already exists"; we re-read the version and carry on when it is now up to date.
    """
    for number, ddl in enumerate(migrations, start=1):
        if number <= _version(conn):
            continue
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{ddl}\nPRAGMA user_version={number};\nCOMMIT;")
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            if _version(conn) < number:
                raise


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Database:
    """One migrated connection with a lock and write transactions, shared by the Phase-1 stores."""

    def __init__(self, path: Path | str, now: Callable[[], datetime] = _utcnow) -> None:
        self.path = Path(path)
        self.now = now
        self.lock = threading.RLock()
        self.conn = connect(self.path)
        migrate(self.conn)

    def stamp(self) -> str:
        return self.now().isoformat()

    @contextmanager
    def tx(self) -> Generator[None]:
        """One write transaction; rolled back on any exception, including a crash signal."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def close(self) -> None:
        with self.lock:
            self.conn.close()
