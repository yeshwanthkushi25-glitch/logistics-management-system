"""
Logistics IMS - Milestone 1 Adversarial Security & Boundary Test Suite.

Author: Challenger M1-2 (Security & Boundary Exploit Challenger)
Mission: Stress-test security boundaries, injection resilience, path traversal defenses,
and relational constraint invariants.
"""

import os
import json
import sqlite3
import tempfile
import pytest

from app.db.connection import get_db_connection, set_default_db_path, close_all_connections
from app.db.nosql import DocumentStore
from app.db.storage import LocalStorageClient
from app.db.init_db import get_schema_sql


@pytest.fixture
def isolated_db():
    """Provides a fresh isolated database with relational schema initialized."""
    temp_file = tempfile.NamedTemporaryFile(suffix=".db", prefix="test_adv_sec_", delete=False)
    db_path = temp_file.name
    temp_file.close()

    set_default_db_path(db_path)
    conn = get_db_connection(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(get_schema_sql())
    conn.commit()

    yield conn, db_path

    close_all_connections()
    for ext in ["", "-wal", "-shm"]:
        p = db_path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# =============================================================================
# 1. NoSQL Injection & Boundary Tests
# =============================================================================

def test_nosql_sql_injection_in_values(isolated_db):
    """Verifies that query values containing SQL injection payloads are properly parameterized."""
    conn, _ = isolated_db
    store = DocumentStore(conn=conn)
    store.insert("public_docs", {"name": "normal_doc", "status": "active"})

    # Injection payloads in value field
    injection_values = [
        "' OR '1'='1",
        "active' OR 1=1 --",
        "'; DROP TABLE nosql_documents; --",
        '" OR ""="',
        "admin' UNION SELECT * FROM users --"
    ]

    for val in injection_values:
        res = store.find("public_docs", {"status": val})
        assert len(res) == 0, f"Value injection '{val}' unexpectedly matched records!"


def test_nosql_sql_injection_in_collection(isolated_db):
    """Verifies that collection names are parameterized with '?' and do not allow SQL injection."""
    conn, _ = isolated_db
    store = DocumentStore(conn=conn)
    store.insert("secret_col", {"secret": "confidential_data"})

    # Injection payloads in collection name
    res = store.find("secret_col' OR 1=1 --", {})
    assert len(res) == 0, "Collection name SQL injection bypassed parameterization!"


def test_nosql_sql_injection_vulnerability_in_keys(isolated_db):
    """
    SECURITY AUDIT TEST:
    Tests whether query keys containing unescaped single quotes can break out of
    json_extract string literal and inject arbitrary SQL.

    VULNERABILITY DEMONSTRATION:
    f"json_extract(data, '{json_path}') = ?"
    When key contains single quotes without SQLite path parameterization:
    {"a') OR 1=1 OR json_extract(data, '$": "dummy"}
    evaluates to:
    WHERE collection = 'public_docs' AND json_extract(data, '$["a"]') OR 1=1 OR json_extract(data, '$"]') = 'dummy'
    leaking cross-collection documents!
    """
    conn, _ = isolated_db
    store = DocumentStore(conn=conn)

    # Insert document in private collection
    store.insert("private_vault", {"secret_key": "CLASSIFIED_TOP_SECRET_COORDINATES"})
    # Insert document in public collection
    store.insert("public_docs", {"title": "Public Guide"})

    exploit_key = "a') OR 1=1 OR json_extract(data, '$"
    
    # Query ONLY the public collection with the exploit key
    results = store.find("public_docs", {exploit_key: "dummy"})

    # If vulnerable: results will contain documents from private_vault!
    is_vulnerable = any("CLASSIFIED_TOP_SECRET_COORDINATES" in str(doc) for doc in results)
    
    # We assert False if vulnerable to signal required change
    assert not is_vulnerable, (
        "CRITICAL SECURITY VULNERABILITY CONFIRMED: "
        "SQL injection in DocumentStore.find() keys broke collection isolation "
        "and leaked documents from 'private_vault' into 'public_docs' query results!"
    )


def test_nosql_deeply_nested_json(isolated_db):
    """Stress-tests DocumentStore with a 50-level deeply nested JSON document."""
    conn, _ = isolated_db
    store = DocumentStore(conn=conn)

    nested_doc = {"target_leaf": "found_at_depth_50"}
    for i in range(50):
        nested_doc = {f"nest_{50-i}": nested_doc}

    doc_id = store.insert("shipment_telemetry", nested_doc)
    assert doc_id is not None

    deep_query_path = ".".join([f"nest_{i+1}" for i in range(50)]) + ".target_leaf"
    res = store.find_one("shipment_telemetry", {deep_query_path: "found_at_depth_50"})
    assert res is not None, "Failed to locate leaf value in 50-level deeply nested JSON document!"


def test_nosql_malformed_dot_notation_paths(isolated_db):
    """Tests resilience against malformed dot-notation paths."""
    conn, _ = isolated_db
    store = DocumentStore(conn=conn)
    store.insert("shipment_telemetry", {"geo": {"lat": 41.88}})

    malformed_paths = [
        ".", "..", "...", ".leading.dot", "trailing.dot.",
        "middle..empty", "", "items.0.sku", "special@chars#here"
    ]
    for p in malformed_paths:
        try:
            res = store.find("shipment_telemetry", {p: "probe"})
            assert isinstance(res, list)
        except Exception as e:
            # Must not crash with unhandled database corruption
            assert isinstance(e, (sqlite3.OperationalError, ValueError))


# =============================================================================
# 2. Storage Client Path Traversal Exploit Tests
# =============================================================================

def test_local_storage_path_traversal_rejection(tmp_path):
    """Verifies that directory traversal escape vectors are rejected."""
    client = LocalStorageClient(storage_dir=str(tmp_path))

    traversal_vectors = [
        "../escape.txt",
        "../../etc/passwd",
        "..\\escape.txt",
        "..\\..\\windows\\system32\\cmd.exe",
        "/etc/shadow",
        "\\windows\\win.ini",
        "\\\\server\\share\\exploit.exe",
        "nested/../../outside.txt",
        "....//....//etc/passwd",
        "a/b/../../../../root.key"
    ]

    for vector in traversal_vectors:
        with pytest.raises(ValueError, match="Security error"):
            client.upload_file(b"malicious_bytes", vector)

    for vector in traversal_vectors:
        with pytest.raises(ValueError, match="Security error"):
            client.download_file(vector)


def test_local_storage_null_byte_rejection(tmp_path):
    """Verifies that null-byte injection attempts are safely rejected."""
    client = LocalStorageClient(storage_dir=str(tmp_path))
    with pytest.raises(ValueError):
        client.upload_file(b"payload", "harmless.txt\x00.exe")


def test_local_storage_no_escape_outside_directory(tmp_path):
    """Verifies that no file can be written outside the designated storage root."""
    client = LocalStorageClient(storage_dir=str(tmp_path))

    # Test file uploads with various clean or sanitizable names
    names = ["pod_123.pdf", "scan_image.png", "sub_file.jpg"]
    for name in names:
        client.upload_file(b"test_data", name)
        assert client.file_exists(name)

    # Verify all files reside strictly inside tmp_path
    for root, dirs, files in os.walk(str(tmp_path)):
        for f in files:
            full_path = os.path.join(root, f)
            assert full_path.startswith(str(tmp_path))


# =============================================================================
# 3. Relational Schema Constraint Boundary Tests
# =============================================================================

def test_schema_negative_prices_rejected(isolated_db):
    """Verifies CHECK constraints reject negative prices across all entities."""
    conn, _ = isolated_db
    conn.execute("INSERT INTO roles VALUES ('r_test', 'RTest', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO users VALUES ('u_test', 'utest', 'u@ex.com', 'h', 'r_test', 'Test', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO warehouses VALUES ('w_test', 'WT', 'Warehouse', 'CDC', 'Addr', 'City', 'ST', 'US', 40.0, -75.0, 1000.0, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO orders VALUES ('o_test', 'ORD-OK', 'u_test', 'PENDING', 100.0, 'Addr', 'City', 40.0, -75.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    # 1. Product unit_price < 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO products VALUES ('p_neg1', 'SKU-N1', 'P', 'd', 'c', -0.01, 1.0, 5, CURRENT_TIMESTAMP)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO products VALUES ('p_neg2', 'SKU-N2', 'P', 'd', 'c', -50.0, 1.0, 5, CURRENT_TIMESTAMP)")

    # 2. Order total_amount < 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO orders VALUES ('o_neg', 'ORD-NEG', 'u_test', 'PENDING', -1.0, 'Addr', 'City', 40.0, -75.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    # 3. Order item unit_price < 0
    conn.execute("INSERT INTO products VALUES ('p_ok', 'SKU-OK', 'P', 'd', 'c', 10.0, 1.0, 5, CURRENT_TIMESTAMP)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO order_items VALUES ('oi_neg', 'o_test', 'p_ok', 1, -0.50, 'w_test', 'PENDING')")

    # 4. Network corridor base_cost < 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO network_corridors VALUES ('c_neg', 'w_test', 'w_test', 100.0, 2.0, -10.0, 'ROAD_FREIGHT', 5000.0, 0.0, 'ACTIVE', CURRENT_TIMESTAMP)")


def test_schema_negative_and_zero_weights_rejected(isolated_db):
    """Verifies CHECK constraints reject negative and zero weights."""
    conn, _ = isolated_db
    conn.execute("INSERT INTO warehouses VALUES ('w_test', 'WT', 'Warehouse', 'CDC', 'Addr', 'City', 'ST', 'US', 40.0, -75.0, 1000.0, 1, CURRENT_TIMESTAMP)")

    # 1. Product weight_kg <= 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO products VALUES ('p_w0', 'SKU-W0', 'P', 'd', 'c', 10.0, 0.0, 5, CURRENT_TIMESTAMP)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO products VALUES ('p_wneg', 'SKU-WNEG', 'P', 'd', 'c', 10.0, -2.5, 5, CURRENT_TIMESTAMP)")

    # 2. Warehouse bin max_weight_kg <= 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO warehouse_bins VALUES ('b_0', 'w_test', 'Z', 'A', 'R', 'S', 'B0', 0.0, 0, CURRENT_TIMESTAMP)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO warehouse_bins VALUES ('b_neg', 'w_test', 'Z', 'A', 'R', 'S', 'BNEG', -100.0, 0, CURRENT_TIMESTAMP)")

    # 3. Corridor capacity_max_kg <= 0
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO network_corridors VALUES ('c_cap0', 'w_test', 'w_test', 50.0, 1.0, 10.0, 'ROAD_FREIGHT', 0.0, 0.0, 'ACTIVE', CURRENT_TIMESTAMP)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO network_corridors VALUES ('c_capneg', 'w_test', 'w_test', 50.0, 1.0, 10.0, 'ROAD_FREIGHT', -500.0, 0.0, 'ACTIVE', CURRENT_TIMESTAMP)")


def test_schema_invalid_enum_strings_rejected(isolated_db):
    """Verifies CHECK constraints reject invalid enum strings."""
    conn, _ = isolated_db
    conn.execute("INSERT INTO roles VALUES ('r_test', 'RTest', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO users VALUES ('u_test', 'utest', 'u@ex.com', 'h', 'r_test', 'Test', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO warehouses VALUES ('w_test', 'WT', 'Warehouse', 'CDC', 'Addr', 'City', 'ST', 'US', 40.0, -75.0, 1000.0, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO orders VALUES ('o_test', 'ORD-OK', 'u_test', 'PENDING', 100.0, 'Addr', 'City', 40.0, -75.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO shipments VALUES ('s_test', 'TRK-OK', 'o_test', 'w_test', 'Carrier', 'STANDARD', 'CREATED', NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    # 1. Invalid warehouse type
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO warehouses VALUES ('w_bad', 'WBAD', 'W', 'MEGA_STORE', 'Addr', 'City', 'ST', 'US', 40.0, -75.0, 1000.0, 1, CURRENT_TIMESTAMP)")

    # 2. Invalid order status
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO orders VALUES ('o_bad', 'ORD-BAD', 'u_test', 'COMPLETED_INVALID', 10.0, 'Addr', 'City', 40.0, -75.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    # 3. Invalid shipment service level
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO shipments VALUES ('s_bad1', 'TRK-BAD1', 'o_test', 'w_test', 'Carrier', 'HYPERSONIC', 'CREATED', NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    # 4. Invalid shipment status
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO shipments VALUES ('s_bad2', 'TRK-BAD2', 'o_test', 'w_test', 'Carrier', 'STANDARD', 'VANISHED', NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    # 5. Invalid milestone type
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO shipment_milestones VALUES ('m_bad', 's_test', 'PACKAGE_DESTROYED', 'City', 40.0, -75.0, 'notes', 'u_test', CURRENT_TIMESTAMP)")

    # 6. Invalid transport mode
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO network_corridors VALUES ('c_bad', 'w_test', 'w_test', 10.0, 1.0, 5.0, 'TELEPORT', 1000.0, 0.0, 'ACTIVE', CURRENT_TIMESTAMP)")


def test_schema_inventory_mathematical_invariant(isolated_db):
    """
    Verifies strict mathematical invariant enforcement:
    Q_on_hand >= Q_reserved >= 0 and Q_avail = Q_on_hand - Q_reserved.
    """
    conn, _ = isolated_db
    conn.execute("INSERT INTO warehouses VALUES ('w_inv', 'WINV', 'Warehouse', 'CDC', 'Addr', 'City', 'ST', 'US', 40.0, -75.0, 1000.0, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO products VALUES ('p_inv', 'SKU-INV', 'P', 'd', 'c', 10.0, 1.0, 5, CURRENT_TIMESTAMP)")

    # 1. Attempt Q_reserved > Q_on_hand on INSERT (10 < 15)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
            ("inv-fail-1", "w_inv", "p_inv", 10, 15)
        )

    # 2. Attempt negative quantity_on_hand (-5)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
            ("inv-fail-2", "w_inv", "p_inv", -5, 0)
        )

    # 3. Attempt negative quantity_reserved (-2)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
            ("inv-fail-3", "w_inv", "p_inv", 10, -2)
        )

    # 4. Insert valid inventory: 30 on hand, 10 reserved
    conn.execute(
        "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
        ("inv-ok", "w_inv", "p_inv", 30, 10)
    )
    row = conn.execute("SELECT quantity_available FROM inventory WHERE id = 'inv-ok'").fetchone()
    assert row["quantity_available"] == 20

    # 5. Attempt UPDATE violating invariant by increasing reserved above on_hand (30 < 35)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE inventory SET quantity_reserved = 35 WHERE id = 'inv-ok'")

    # 6. Attempt UPDATE violating invariant by decreasing on_hand below reserved (5 < 10)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE inventory SET quantity_on_hand = 5 WHERE id = 'inv-ok'")
