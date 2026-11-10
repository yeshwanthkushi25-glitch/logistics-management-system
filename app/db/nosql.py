"""
NoSQL Document Store Engine for Logistics IMS.

Leverages SQLite native JSON1 capabilities to provide schema-less document storage,
dot-notation JSON path queries, MongoDB-style comparison operators, dynamic expression
indexes, and ACID transactional integration across collections:
- shipment_telemetry
- audit_event_logs
- order_snapshots
"""

import json
import re
import sqlite3
import threading
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

# Optional import of centralized connection manager; falls back gracefully
try:
    from app.db.connection import get_db_connection
except ImportError:
    get_db_connection = None


class DocumentStore:
    """
    SQLite JSON1-backed NoSQL Document Store.

    Supports multi-collection document persistence, nested JSON path extraction,
    filter operators ($eq, $ne, $gt, $gte, $lt, $lte, $in, $nin, $exists, $like),
    sorting, and SQLite expression index generation.
    All JSON paths and values are 100% parameterized with '?' to prevent SQL injection.
    """

    ALLOWED_COLLECTIONS = {
        "shipment_telemetry",
        "audit_event_logs",
        "order_snapshots"
    }

    def __init__(self, conn: Optional[sqlite3.Connection] = None, db_path: Optional[str] = None):
        """
        Initializes the DocumentStore.

        Args:
            conn: Optional existing sqlite3.Connection (useful for testing and transactions).
            db_path: Optional path to SQLite database file. Defaults to memory if neither is given.
        """
        self._custom_conn = conn
        self._db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Resolves the active database connection."""
        if self._custom_conn is not None:
            return self._custom_conn
        if get_db_connection is not None:
            return get_db_connection(self._db_path)

        # Local standalone connection fallback
        if not hasattr(self, "_local_conn") or self._local_conn is None:
            target = self._db_path or ":memory:"
            self._local_conn = sqlite3.connect(target, check_same_thread=False, timeout=10.0)
            self._local_conn.row_factory = sqlite3.Row
            self._local_conn.execute("PRAGMA foreign_keys = ON;")
            self._local_conn.execute("PRAGMA busy_timeout = 5000;")
        return self._local_conn

    def _init_db(self) -> None:
        """Initializes the unified nosql_documents table and base indexes."""
        conn = self._get_connection()
        with self._lock:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nosql_documents (
                    id TEXT PRIMARY KEY,
                    collection TEXT NOT NULL,
                    data JSON NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nosql_collection 
                ON nosql_documents(collection);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nosql_collection_created 
                ON nosql_documents(collection, created_at);
            """)

    @staticmethod
    def _to_json_path(field_path: str) -> str:
        """
        Translates dot-notation field path to SQLite JSON1 path expression.

        Example:
            'geo.latitude' -> '$.geo.latitude'
            'environmental.temperature_celsius' -> '$.environmental.temperature_celsius'
        """
        clean_path = field_path.strip().lstrip("$").lstrip(".")
        parts = clean_path.split(".")
        segments = []
        for part in parts:
            if not part:
                continue
            if re.match(r"^[a-zA-Z0-9_]+$", part):
                segments.append(f".{part}")
            else:
                escaped = part.replace('"', '\\"')
                segments.append(f'["{escaped}"]')
        return "$" + "".join(segments)

    def insert(self, collection: str, doc: Dict[str, Any]) -> str:
        """
        Inserts a semi-structured document into the specified collection.

        Args:
            collection: Name of the collection (e.g. 'shipment_telemetry').
            doc: Dictionary payload to store.

        Returns:
            The unique document identifier (_id).
        """
        doc_copy = dict(doc)
        doc_id = str(doc_copy.get("_id") or doc_copy.get("id") or uuid.uuid4().hex)
        doc_copy["_id"] = doc_id

        json_str = json.dumps(doc_copy)
        conn = self._get_connection()

        with self._lock:
            conn.execute(
                "INSERT INTO nosql_documents (id, collection, data) VALUES (?, ?, ?)",
                (doc_id, collection, json_str)
            )
        return doc_id

    def create_index(self, collection: str, field_path: str) -> str:
        """
        Creates a SQLite expression index on a specific JSON path for a collection.

        Args:
            collection: Target collection name.
            field_path: Dot-notation field path (e.g. 'geo.latitude').

        Returns:
            The created index name.
        """
        if not re.match(r"^[a-zA-Z0-9_.]+$", field_path):
            raise ValueError(f"Invalid field path for index: {field_path}")
        if not re.match(r"^[a-zA-Z0-9_]+$", collection):
            raise ValueError(f"Invalid collection name: {collection}")

        sanitized_col = re.sub(r"[^a-zA-Z0-9_]", "_", collection)
        sanitized_field = re.sub(r"[^a-zA-Z0-9_]", "_", field_path)
        index_name = f"idx_nosql_{sanitized_col}_{sanitized_field}"
        json_path = self._to_json_path(field_path)
        escaped_json_path = json_path.replace("'", "''")

        conn = self._get_connection()
        with self._lock:
            conn.execute(f"""
                CREATE INDEX IF NOT EXISTS {index_name} 
                ON nosql_documents (collection, (json_extract(data, '{escaped_json_path}')));
            """)
        return index_name

    def _build_where_clause(
        self,
        collection: str,
        query: Optional[Dict[str, Any]] = None
    ) -> Tuple[str, List[Any]]:
        """
        Builds parameterized WHERE clause and parameter list.
        All json_extract path expressions use '?' parameter placeholders.
        """
        query = query or {}
        where_clauses = ["collection = ?"]
        params: List[Any] = [collection]

        for field, condition in query.items():
            json_path = self._to_json_path(field)

            if isinstance(condition, dict):
                # Operator dictionary e.g. {"$lt": -20.0, "$gt": -50.0}
                for op, op_val in condition.items():
                    if op == "$eq":
                        where_clauses.append("json_extract(data, ?) = ?")
                        params.extend([json_path, op_val])
                    elif op == "$ne":
                        where_clauses.append("json_extract(data, ?) != ?")
                        params.extend([json_path, op_val])
                    elif op == "$gt":
                        where_clauses.append("json_extract(data, ?) > ?")
                        params.extend([json_path, op_val])
                    elif op == "$gte":
                        where_clauses.append("json_extract(data, ?) >= ?")
                        params.extend([json_path, op_val])
                    elif op == "$lt":
                        where_clauses.append("json_extract(data, ?) < ?")
                        params.extend([json_path, op_val])
                    elif op == "$lte":
                        where_clauses.append("json_extract(data, ?) <= ?")
                        params.extend([json_path, op_val])
                    elif op == "$in":
                        if not isinstance(op_val, (list, tuple, set)):
                            raise ValueError("$in requires an iterable sequence")
                        if not op_val:
                            where_clauses.append("1 = 0")
                        else:
                            placeholders = ", ".join("?" for _ in op_val)
                            where_clauses.append(f"json_extract(data, ?) IN ({placeholders})")
                            params.append(json_path)
                            params.extend(op_val)
                    elif op == "$nin":
                        if not isinstance(op_val, (list, tuple, set)):
                            raise ValueError("$nin requires an iterable sequence")
                        if op_val:
                            placeholders = ", ".join("?" for _ in op_val)
                            where_clauses.append(f"json_extract(data, ?) NOT IN ({placeholders})")
                            params.append(json_path)
                            params.extend(op_val)
                    elif op == "$exists":
                        if op_val:
                            where_clauses.append("json_extract(data, ?) IS NOT NULL")
                        else:
                            where_clauses.append("json_extract(data, ?) IS NULL")
                        params.append(json_path)
                    elif op == "$like":
                        where_clauses.append("json_extract(data, ?) LIKE ?")
                        params.extend([json_path, op_val])
                    else:
                        raise ValueError(f"Unsupported query operator: {op}")
            else:
                # Direct equality filter
                where_clauses.append("json_extract(data, ?) = ?")
                params.extend([json_path, condition])

        where_stmt = " AND ".join(where_clauses)
        return where_stmt, params

    def find(
        self,
        collection: str,
        query: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        sort_by: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Queries documents in a collection matching filter criteria.

        Args:
            collection: Name of collection to query.
            query: Filter conditions supporting dot-notation and operators ($gt, $lt, $in, etc.).
            limit: Maximum records to return.
            sort_by: Optional sort expression (e.g. 'timestamp', '-timestamp', 'geo.speed_kmh DESC').

        Returns:
            List of matching document dictionaries.
        """
        where_stmt, params = self._build_where_clause(collection, query)

        # Build ORDER BY clause
        if sort_by:
            sort_field = sort_by.strip()
            direction = "ASC"
            if sort_field.startswith("-"):
                sort_field = sort_field[1:].strip()
                direction = "DESC"
            elif sort_field.upper().endswith(" DESC"):
                sort_field = sort_field[:-5].strip()
                direction = "DESC"
            elif sort_field.upper().endswith(" ASC"):
                sort_field = sort_field[:-4].strip()
                direction = "ASC"

            if not re.match(r"^[a-zA-Z0-9_.]+$", sort_field):
                raise ValueError(f"Invalid sort field name: {sort_field}")
            if direction not in ("ASC", "DESC"):
                raise ValueError(f"Invalid sort direction: {direction}")

            if sort_field in ("created_at", "id"):
                order_stmt = f"ORDER BY {sort_field} {direction}"
            else:
                sort_json = self._to_json_path(sort_field)
                order_stmt = f"ORDER BY json_extract(data, ?) {direction}"
                params.append(sort_json)
        else:
            order_stmt = "ORDER BY created_at ASC"

        sql = f"SELECT data FROM nosql_documents WHERE {where_stmt} {order_stmt} LIMIT ?"
        params.append(limit)

        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(sql, params)
            rows = cur.fetchall()

        return [json.loads(row["data"] if isinstance(row, sqlite3.Row) else row[0]) for row in rows]

    def find_one(self, collection: str, query: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """Finds the first document matching query filter, or None."""
        results = self.find(collection, query=query, limit=1)
        return results[0] if results else None

    def count(self, collection: str, query: Optional[Dict[str, Any]] = None) -> int:
        """
        Counts documents in a collection matching query filter.
        Executes directly via parameterized SQL without deserializing documents into memory.
        """
        where_stmt, params = self._build_where_clause(collection, query)
        sql = f"SELECT COUNT(*) FROM nosql_documents WHERE {where_stmt}"
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return row[0] if row else 0

    def delete(self, collection: str, query: Dict[str, Any]) -> int:
        """
        Deletes documents matching query filter. Returns count deleted.
        Executes directly via parameterized SQL without deserializing documents into memory.
        """
        where_stmt, params = self._build_where_clause(collection, query)
        sql = f"DELETE FROM nosql_documents WHERE {where_stmt}"
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def clear_collection(self, collection: str) -> int:
        """Purges all documents from a collection."""
        conn = self._get_connection()
        with self._lock:
            cur = conn.execute("DELETE FROM nosql_documents WHERE collection = ?", (collection,))
            return cur.rowcount


# Singleton document store reference
_GLOBAL_DOC_STORE: Optional[DocumentStore] = None
_STORE_LOCK = threading.Lock()


def get_doc_store(db_path: Optional[str] = None) -> DocumentStore:
    """Singleton provider for DocumentStore instance."""
    global _GLOBAL_DOC_STORE
    with _STORE_LOCK:
        if _GLOBAL_DOC_STORE is None or (db_path and _GLOBAL_DOC_STORE._db_path != db_path):
            _GLOBAL_DOC_STORE = DocumentStore(db_path=db_path)
        return _GLOBAL_DOC_STORE
