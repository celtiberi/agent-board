"""SQLite connection management, WAL configuration, migrations, and BEGIN IMMEDIATE transactions."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agent_board.ids import utc_now_iso


class Database:
    """Thread-safe SQLite database wrapper with WAL mode and BEGIN IMMEDIATE writes."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._write_conn: sqlite3.Connection | None = None
        self._write_depth = 0
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._read_conns_lock = threading.Lock()

    def _configure_conn(self, conn: sqlite3.Connection, *, read_only: bool = False) -> sqlite3.Connection:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        if read_only:
            conn.execute("PRAGMA query_only = ON;")
        else:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _get_write_conn(self) -> sqlite3.Connection:
        if self._write_conn is None:
            conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,
                check_same_thread=False,
            )
            self._write_conn = self._configure_conn(conn, read_only=False)
        return self._write_conn

    def read_conn(self) -> sqlite3.Connection:
        """Return a thread-local read-only SQLite connection in autocommit mode."""
        conn: sqlite3.Connection | None = getattr(self._local, "read_conn", None)
        if conn is None:
            # Ensure DB file and WAL exist before opening read_only connection
            if not self.db_path.exists():
                with self._write_lock:
                    self._get_write_conn()
            conn = sqlite3.connect(
                str(self.db_path),
                isolation_level=None,
                check_same_thread=False,
            )
            self._configure_conn(conn, read_only=True)
            self._local.read_conn = conn
            with self._read_conns_lock:
                self._read_conns.append(conn)
        return conn

    @contextmanager
    def read_snapshot(self) -> Iterator[sqlite3.Connection]:
        """Execute multiple SELECTs against a consistent WAL snapshot (re-entrant)."""
        conn = self.read_conn()
        depth = getattr(self._local, "read_depth", 0)
        is_outer = depth == 0
        if is_outer:
            conn.execute("BEGIN DEFERRED;")
        self._local.read_depth = depth + 1
        try:
            yield conn
        finally:
            self._local.read_depth = depth
            if is_outer and conn.in_transaction:
                conn.execute("ROLLBACK;")

    @contextmanager
    def write_tx(self) -> Iterator[sqlite3.Connection]:
        """Acquire the writer lock and execute inside a BEGIN IMMEDIATE transaction."""
        with self._write_lock:
            conn = self._get_write_conn()
            is_outer = self._write_depth == 0
            if is_outer:
                conn.execute("BEGIN IMMEDIATE;")
            else:
                conn.execute(f"SAVEPOINT sp_{self._write_depth};")
            self._write_depth += 1
            try:
                yield conn
            except BaseException:
                self._write_depth -= 1
                if is_outer:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK;")
                else:
                    conn.execute(f"ROLLBACK TO SAVEPOINT sp_{self._write_depth};")
                    conn.execute(f"RELEASE SAVEPOINT sp_{self._write_depth};")
                raise
            else:
                try:
                    if is_outer:
                        conn.execute("COMMIT;")
                    else:
                        conn.execute(f"RELEASE SAVEPOINT sp_{self._write_depth - 1};")
                except BaseException:
                    if is_outer and conn.in_transaction:
                        conn.execute("ROLLBACK;")
                    raise
                finally:
                    self._write_depth -= 1

    def apply_migrations(self, migrations_dir: Path) -> list[str]:
        """Apply any unapplied .sql files in migrations_dir in lexical order."""
        applied: list[str] = []
        files = sorted(migrations_dir.glob("*.sql"))
        with self.write_tx() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """
            )
            existing = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            for path in files:
                version = path.name
                if version in existing:
                    continue
                sql = path.read_text(encoding="utf-8")
                conn.executescript("BEGIN IMMEDIATE;\n" + sql) if False else None
                # executescript issues implicit COMMIT; execute statement-by-statement inside write_tx
                for stmt in _split_sql_statements(sql):
                    conn.execute(stmt)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now_iso()),
                )
                applied.append(version)
        return applied

    def checkpoint_wal(self) -> None:
        """Checkpoint WAL cleanly when needed."""
        with self._write_lock:
            conn = self._get_write_conn()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")

    def close(self) -> None:
        """Close writer and all thread-local reader connections."""
        with self._read_conns_lock:
            for conn in self._read_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()
        with self._write_lock:
            if self._write_conn is not None:
                try:
                    self._write_conn.close()
                except sqlite3.Error:
                    pass
                self._write_conn = None


def _split_sql_statements(sql: str) -> list[str]:
    """Split a migration script into complete SQLite statements cleanly."""
    statements: list[str] = []
    buffer: list[str] = []
    for line in sql.splitlines(keepends=True):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("--")):
            continue
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            cleaned = candidate.strip()
            if cleaned:
                statements.append(cleaned)
            buffer.clear()
    remainder = "".join(buffer).strip()
    if remainder:
        statements.append(remainder)
    return statements
