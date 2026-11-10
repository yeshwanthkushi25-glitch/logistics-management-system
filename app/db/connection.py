"""
Database Connection & Transaction Manager for Logistics IMS.

Provides thread-local SQLite connection pooling with WAL mode, strict foreign key
enforcement, 5000ms busy timeout, row factory (sqlite3.Row), and an atomic ACID
transaction context manager supporting nested savepoints.
"""

import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from typing import Generator, Optional, Set

# Thread-local storage for non-Flask threads, CLI scripts, and pytest fixtures
_thread_local = threading.local()
_global_lock = threading.Lock()
_active_connections: Set[sqlite3.Connection] = set()

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CANONICAL_DB_PATH = os.path.join(_PROJECT_ROOT, "data", "logistics.db")
_LEGACY_DB_PATH = os.path.join(_PROJECT_ROOT, "app", "data", "logistics.db")

_DEFAULT_DB_PATH = os.environ.get("DATABASE_PATH", _CANONICAL_DB_PATH)


def set_default_db_path(path: str) -> None:
    """Sets the global default database file path."""
    global _DEFAULT_DB_PATH
    _DEFAULT_DB_PATH = path


def get_default_db_path() -> str:
    """Returns the current default database file path."""
    return _DEFAULT_DB_PATH


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Factory function that instantiates and configures a new SQLite connection.

    Configured Pragmas:
    - PRAGMA foreign_keys = ON; (Mandatory foreign key constraint enforcement)
    - PRAGMA journal_mode = WAL; (Write-Ahead Logging for non-blocking concurrent reads)
    - PRAGMA busy_timeout = 5000; (5-second lock acquisition timeout)
    - PRAGMA synchronous = NORMAL; (High-performance durability in WAL mode)
    - PRAGMA cache_size = -64000; (64MB memory page cache)
    - isolation_level = None (Enables explicit transaction control via BEGIN/COMMIT/ROLLBACK)
    """
    target_path = db_path or get_default_db_path()

    if target_path != ":memory:":
        parent_dir = os.path.dirname(os.path.abspath(target_path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        # Backwards compatibility synchronization:
        # If accessing canonical or legacy path, and requested file is unpopulated (<= 4096 bytes)
        # while the alternate file is populated (> 4096 bytes), synchronize from the populated database.
        abs_target = os.path.abspath(target_path)
        canonical_abs = os.path.abspath(_CANONICAL_DB_PATH)
        legacy_abs = os.path.abspath(_LEGACY_DB_PATH)

        if abs_target == canonical_abs:
            if (not os.path.exists(canonical_abs) or os.path.getsize(canonical_abs) <= 4096) and os.path.exists(legacy_abs) and os.path.getsize(legacy_abs) > 4096:
                import shutil
                shutil.copyfile(legacy_abs, canonical_abs)
        elif abs_target == legacy_abs:
            if (not os.path.exists(legacy_abs) or os.path.getsize(legacy_abs) <= 4096) and os.path.exists(canonical_abs) and os.path.getsize(canonical_abs) > 4096:
                import shutil
                shutil.copyfile(canonical_abs, legacy_abs)

    conn = sqlite3.connect(
        target_path,
        timeout=10.0,
        isolation_level=None,  # Manual autocommit mode allowing explicit BEGIN statements
        check_same_thread=False
    )
    conn.row_factory = sqlite3.Row

    # Execute mandatory pragmas
    conn.execute("PRAGMA foreign_keys = ON;")
    if target_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA cache_size = -64000;")

    with _global_lock:
        _active_connections.add(conn)

    return conn


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Retrieves or initializes a thread-safe / context-bound SQLite database connection.

    If running within a Flask application context, binds connection to flask.g.
    Otherwise, retrieves the connection from thread-local storage.
    """
    # 1. Flask application context check
    try:
        from flask import g, has_app_context
        if has_app_context():
            key = f"_db_connection_{db_path or get_default_db_path()}"
            if not hasattr(g, key):
                setattr(g, key, create_connection(db_path))
            return getattr(g, key)
    except ImportError:
        pass

    # 2. Thread-local storage fallback
    thread_connections = getattr(_thread_local, "connections", None)
    if thread_connections is None:
        thread_connections = {}
        _thread_local.connections = thread_connections

    key = db_path or get_default_db_path()
    conn = thread_connections.get(key)
    if conn is None:
        conn = create_connection(key)
        thread_connections[key] = conn
    return conn


def close_db_connection(db_path: Optional[str] = None) -> None:
    """Closes the current thread/context connection for the specified path."""
    key = db_path or get_default_db_path()

    # Flask context teardown
    try:
        from flask import g, has_app_context
        if has_app_context():
            flask_key = f"_db_connection_{key}"
            conn = getattr(g, flask_key, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
                with _global_lock:
                    _active_connections.discard(conn)
                delattr(g, flask_key)
            return
    except ImportError:
        pass

    # Thread-local teardown
    thread_connections = getattr(_thread_local, "connections", None)
    if thread_connections and key in thread_connections:
        conn = thread_connections.pop(key)
        try:
            conn.close()
        except Exception:
            pass
        with _global_lock:
            _active_connections.discard(conn)


def close_all_connections() -> None:
    """Closes all tracked SQLite connections across all threads (used in test teardown)."""
    with _global_lock:
        for conn in list(_active_connections):
            try:
                conn.close()
            except Exception:
                pass
        _active_connections.clear()
    _thread_local.connections = {}


@contextmanager
def transaction_scope(
    conn: Optional[sqlite3.Connection] = None,
    isolation: str = "IMMEDIATE"
) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager providing strict ACID transaction boundaries.

    - Default isolation: 'IMMEDIATE' (acquires reserved lock immediately, preventing write deadlocks).
    - Auto-commits on clean exit of the 'with' block.
    - Executes clean rollback (conn.rollback()) on any unhandled exception.
    - Supports nested transaction calls via SQLite SAVEPOINTS (releases on success, rollbacks on error).
    - Leaves zero orphaned locks or uncommitted mutations.
    """
    active_conn = conn or get_db_connection()
    is_nested = active_conn.in_transaction

    if is_nested:
        # Re-entrant / nested transaction scope: use SQLite SAVEPOINT
        savepoint_name = f"sp_{uuid.uuid4().hex[:8]}"
        active_conn.execute(f"SAVEPOINT {savepoint_name};")
        try:
            yield active_conn
            active_conn.execute(f"RELEASE SAVEPOINT {savepoint_name};")
        except Exception:
            active_conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name};")
            active_conn.execute(f"RELEASE SAVEPOINT {savepoint_name};")
            raise
    else:
        # Top-level transaction: explicit BEGIN <isolation>
        active_conn.execute(f"BEGIN {isolation};")
        try:
            yield active_conn
            active_conn.commit()
        except Exception:
            active_conn.rollback()
            raise
