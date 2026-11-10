"""
Logistics IMS - Comprehensive Database Core & Storage Infrastructure Unit Tests.

Validates:
1. SQLite Pragmas & Concurrency (foreign_keys, WAL mode, busy_timeout, row_factory)
2. ACID Transaction Atomicity, Commit, Rollback, and Nested SAVEPOINT scopes
3. Relational Schema, Generated Stored Columns, Mathematical Invariants, and Foreign Keys
4. SQLite JSON1 NoSQL Document Store (nested path extraction, operators, expression indexes, transactions)
5. Thread-safe CacheManager (6-phase lifecycle, LRU eviction, TTL expiration, pattern invalidation, metrics)
6. Dual-Mode Storage Client (LocalStorageClient with traversal prevention, S3 client, transparent fallback)
7. Database Initialization & Seeding (Relational entities, network graph corridors, NoSQL collections, CTE routing)
"""

import os
import time
import json
import sqlite3
import tempfile
import threading
from unittest.mock import MagicMock
import pytest

from app.db.connection import (
    create_connection,
    get_db_connection,
    close_db_connection,
    close_all_connections,
    transaction_scope,
    set_default_db_path,
    get_default_db_path,
)
from app.db.nosql import DocumentStore, get_doc_store
from app.db.cache import CacheManager, get_cache_manager
from app.db.storage import (
    BaseStorageClient,
    LocalStorageClient,
    S3StorageClient,
    DualModeStorageClient,
    get_storage_client,
)
from app.db.init_db import init_database, seed_demo_dataset, get_schema_sql


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def temp_db():
    """Provides a fresh isolated temporary database file for each test."""
    temp_file = tempfile.NamedTemporaryFile(suffix=".db", prefix="test_db_core_", delete=False)
    db_path = temp_file.name
    temp_file.close()

    set_default_db_path(db_path)

    # Initialize schema DDL
    conn = get_db_connection(db_path)
    ddl = get_schema_sql()
    conn.executescript(ddl)
    conn.commit()

    yield db_path

    # Clean teardown
    close_all_connections()
    for ext in ["", "-wal", "-shm"]:
        p = db_path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# -----------------------------------------------------------------------------
# 1. Connection & Pragma Verification Tests
# -----------------------------------------------------------------------------

def test_pragmas_enabled(temp_db):
    conn = get_db_connection(temp_db)

    # 1. Foreign keys must be ON
    fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
    assert fk == 1, "PRAGMA foreign_keys is not enabled!"

    # 2. Journal mode must be WAL (for file-based database)
    jm = conn.execute("PRAGMA journal_mode;").fetchone()[0]
    assert jm.lower() == "wal", f"PRAGMA journal_mode is not WAL, got {jm}"

    # 3. Busy timeout must be 5000ms
    bt = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    assert bt == 5000, f"PRAGMA busy_timeout is {bt}, expected 5000"

    # 4. Row factory must be sqlite3.Row
    assert conn.row_factory == sqlite3.Row


def test_thread_local_connection_isolation(temp_db):
    """Verifies that separate threads receive distinct thread-local connections."""
    connections = []

    def worker():
        c = get_db_connection(temp_db)
        connections.append(c)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(connections) == 2
    assert connections[0] is not connections[1], "Separate threads must maintain distinct SQLite connections!"


# -----------------------------------------------------------------------------
# 2. ACID Transaction Atomicity & Rollback Tests
# -----------------------------------------------------------------------------

def test_transaction_clean_commit(temp_db):
    conn = get_db_connection(temp_db)
    with transaction_scope(conn) as tx:
        tx.execute(
            "INSERT INTO roles (id, name, description, permissions) VALUES (?, ?, ?, ?)",
            ("role_tx_test", "TxTestRole", "Description", '["test:all"]')
        )

    row = conn.execute("SELECT * FROM roles WHERE id='role_tx_test'").fetchone()
    assert row is not None
    assert row["name"] == "TxTestRole"


def test_transaction_atomic_rollback_on_exception(temp_db):
    conn = get_db_connection(temp_db)

    with pytest.raises(RuntimeError, match="Simulated transaction fault"):
        with transaction_scope(conn) as tx:
            tx.execute(
                "INSERT INTO roles (id, name, description, permissions) VALUES (?, ?, ?, ?)",
                ("role_rollback", "RollbackRole", "Desc", '[]')
            )
            raise RuntimeError("Simulated transaction fault")

    row = conn.execute("SELECT * FROM roles WHERE id='role_rollback'").fetchone()
    assert row is None, "Failed transaction was not rolled back; dirty read observed!"


def test_nested_transaction_savepoint(temp_db):
    """Verifies re-entrant savepoint release and inner-only rollback."""
    conn = get_db_connection(temp_db)

    with transaction_scope(conn) as tx_outer:
        tx_outer.execute(
            "INSERT INTO roles (id, name, description, permissions) VALUES (?, ?, ?, ?)",
            ("role_outer_1", "OuterRole1", "Desc", '[]')
        )

        # Nested transaction fails and is caught
        try:
            with transaction_scope(tx_outer) as tx_inner:
                tx_inner.execute(
                    "INSERT INTO roles (id, name, description, permissions) VALUES (?, ?, ?, ?)",
                    ("role_inner_fail", "InnerRoleFail", "Desc", '[]')
                )
                raise ValueError("Inner failure")
        except ValueError:
            pass

        # Outer transaction proceeds
        tx_outer.execute(
            "INSERT INTO roles (id, name, description, permissions) VALUES (?, ?, ?, ?)",
            ("role_outer_2", "OuterRole2", "Desc", '[]')
        )

    # Outer records committed, inner record rolled back
    assert conn.execute("SELECT * FROM roles WHERE id='role_outer_1'").fetchone() is not None
    assert conn.execute("SELECT * FROM roles WHERE id='role_outer_2'").fetchone() is not None
    assert conn.execute("SELECT * FROM roles WHERE id='role_inner_fail'").fetchone() is None


# -----------------------------------------------------------------------------
# 3. Schema Constraints, Invariants & Indexes Tests
# -----------------------------------------------------------------------------

def test_all_12_tables_exist(temp_db):
    conn = get_db_connection(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = {r[0] for r in cursor.fetchall()}

    expected = {
        "roles", "users", "warehouses", "warehouse_bins", "products",
        "inventory", "orders", "order_items", "shipments", "shipment_milestones",
        "network_corridors", "nosql_documents"
    }
    assert expected.issubset(tables), f"Missing tables: {expected - tables}"


def test_inventory_generated_column_and_invariant(temp_db):
    conn = get_db_connection(temp_db)

    # Insert prerequisites
    conn.execute("INSERT INTO roles VALUES ('r1', 'Admin', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO warehouses VALUES ('w1', 'WH-01', 'Chicago Hub', 'CDC', '123 St', 'Chicago', 'IL', 'USA', 41.87, -87.62, 5000, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO products VALUES ('p1', 'SKU-1', 'Product 1', 'desc', 'Cat', 10.0, 1.5, 5, CURRENT_TIMESTAMP)")

    # 1. Valid inventory record: 50 on-hand, 15 reserved -> 35 available computed automatically
    conn.execute(
        "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
        ("inv-1", "w1", "p1", 50, 15)
    )
    row = conn.execute("SELECT quantity_available FROM inventory WHERE id='inv-1'").fetchone()
    assert row["quantity_available"] == 35, "Generated column failed to compute correct available quantity!"

    # 2. Check constraint violation: reserved > on_hand
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
            ("inv-fail", "w1", "p1", 10, 20)
        )


def test_foreign_key_cascades_and_restricts(temp_db):
    conn = get_db_connection(temp_db)

    # 1. Restrict delete on roles when referenced by user
    conn.execute("INSERT INTO roles VALUES ('r1', 'Manager', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO users VALUES ('u1', 'mgr', 'mgr@example.com', 'hash', 'r1', 'Manager User', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM roles WHERE id='r1'")

    # 2. Cascade delete on shipment -> shipment_milestones
    conn.execute("INSERT INTO warehouses VALUES ('w1', 'WH-01', 'Chicago Hub', 'CDC', '123 St', 'Chicago', 'IL', 'USA', 41.87, -87.62, 5000, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO orders VALUES ('o1', 'ORD-1', 'u1', 'CONFIRMED', 100.0, '456 Ave', 'Detroit', 42.33, -83.04, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO shipments VALUES ('s1', 'TRK-001', 'o1', 'w1', 'FedEx', 'EXPRESS', 'CREATED', NULL, NULL, NULL, NULL, NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO shipment_milestones VALUES ('m1', 's1', 'MANIFEST_CREATED', 'Chicago', 41.87, -87.62, 'Created', 'u1', CURRENT_TIMESTAMP)")

    conn.execute("DELETE FROM shipments WHERE id='s1'")
    count = conn.execute("SELECT count(*) FROM shipment_milestones WHERE id='m1'").fetchone()[0]
    assert count == 0, "Milestone was not cascade-deleted with parent shipment!"


def test_indexes_exist(temp_db):
    conn = get_db_connection(temp_db)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
    indexes = {r[0] for r in cursor.fetchall()}

    expected_indexes = {
        "idx_users_role", "idx_users_username", "idx_users_email",
        "idx_warehouse_bins_wh", "idx_inventory_lookup", "idx_inventory_product",
        "idx_orders_customer", "idx_orders_status", "idx_order_items_order",
        "idx_order_items_product", "idx_shipments_tracking", "idx_shipments_order",
        "idx_shipments_status", "idx_milestones_shipment_time",
        "idx_corridors_source", "idx_corridors_target",
        "idx_nosql_collection", "idx_nosql_collection_created"
    }
    assert expected_indexes.issubset(indexes), f"Missing indexes: {expected_indexes - indexes}"


# -----------------------------------------------------------------------------
# 4. NoSQL DocumentStore Tests
# -----------------------------------------------------------------------------

def test_nosql_insert_and_find(temp_db):
    store = DocumentStore(conn=get_db_connection(temp_db))

    telemetry = {
        "tracking_number": "TRK-GPS-001",
        "geo": {"latitude": 41.8781, "longitude": -87.6298, "speed_kmh": 65.4},
        "environmental": {"temperature_celsius": 4.2, "humidity_percent": 55.0}
    }
    doc_id = store.insert("shipment_telemetry", telemetry)
    assert doc_id is not None

    found = store.find_one("shipment_telemetry", {"tracking_number": "TRK-GPS-001"})
    assert found is not None
    assert found["geo"]["latitude"] == 41.8781
    assert found["environmental"]["temperature_celsius"] == 4.2


def test_nosql_operators_and_indexes(temp_db):
    store = DocumentStore(conn=get_db_connection(temp_db))

    store.insert("shipment_telemetry", {
        "tracking_number": "TRK-COLD-1",
        "geo": {"latitude": 40.71, "longitude": -74.01},
        "environmental": {"temperature_celsius": -25.5}
    })
    store.insert("shipment_telemetry", {
        "tracking_number": "TRK-WARM-1",
        "geo": {"latitude": 40.71, "longitude": -74.01},
        "environmental": {"temperature_celsius": 15.0}
    })

    # Query with $lt operator and dot-notation path
    cold_docs = store.find("shipment_telemetry", {
        "geo.latitude": 40.71,
        "environmental.temperature_celsius": {"$lt": -20.0}
    })
    assert len(cold_docs) == 1
    assert cold_docs[0]["tracking_number"] == "TRK-COLD-1"

    # Create expression index
    idx_name = store.create_index("shipment_telemetry", "environmental.temperature_celsius")
    assert "idx_nosql" in idx_name

    # Check index exists in sqlite_master
    cur = get_db_connection(temp_db).execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (idx_name,))
    assert cur.fetchone() is not None


def test_nosql_transactional_rollback(temp_db):
    conn = get_db_connection(temp_db)
    store = DocumentStore(conn=conn)

    with pytest.raises(ValueError):
        with transaction_scope(conn):
            store.insert("order_snapshots", {"order_id": "ord-fail", "total": 99.0})
            assert store.find_one("order_snapshots", {"order_id": "ord-fail"}) is not None
            raise ValueError("Rollback NoSQL transaction")

    assert store.find_one("order_snapshots", {"order_id": "ord-fail"}) is None


# -----------------------------------------------------------------------------
# 5. High-Throughput CacheManager Tests
# -----------------------------------------------------------------------------

def test_cache_six_phase_lifecycle():
    cache = CacheManager(max_entries=3, default_ttl=60)

    # Phase 1: Cold start miss
    assert cache.get("key1") is None
    stats = cache.get_stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 0

    # Phase 2: Cache Hit
    cache.set("key1", "val1", ttl_seconds=1)
    assert cache.get("key1") == "val1"
    assert cache.get_stats()["hits"] == 1

    # Phase 3: TTL Expiration on read
    time.sleep(1.05)
    assert cache.get("key1") is None
    assert cache.get_stats()["invalidations"] >= 1

    # Phase 4: Mutation single-key deletion
    cache.set("itemA", 100)
    assert cache.delete("itemA") is True
    assert cache.get("itemA") is None

    # Phase 5: Pattern invalidation
    cache.set("inv:wh1:p1", 10)
    cache.set("inv:wh1:p2", 20)
    cache.set("inv:wh2:p1", 30)
    invalidated = cache.invalidate_pattern("inv:wh1:*")
    assert invalidated == 2
    assert cache.get("inv:wh1:p1") is None
    assert cache.get("inv:wh2:p1") == 30

    # Phase 6: LRU Eviction under capacity limit
    cache.flush()
    cache.reset_stats()
    cache.set("k1", 1)
    cache.set("k2", 2)
    cache.set("k3", 3)

    # Access k1 to make it most recently used (LRU order: k2, k3, k1)
    assert cache.get("k1") == 1

    # Adding k4 must evict k2
    cache.set("k4", 4)
    assert cache.get("k2") is None
    assert cache.get("k1") == 1
    assert cache.get("k3") == 3
    assert cache.get("k4") == 4
    assert cache.get_stats()["evictions"] == 1


def test_cache_concurrency():
    cache = CacheManager(max_entries=200, default_ttl=30)

    def worker(wid):
        for i in range(50):
            cache.set(f"thread:{wid}:{i}", i)
            val = cache.get(f"thread:{wid}:{i}")
            assert val == i
            if i % 10 == 0:
                cache.invalidate_pattern(f"thread:{wid}:*")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    stats = cache.get_stats()
    assert stats["hits"] > 0
    assert stats["invalidations"] > 0


# -----------------------------------------------------------------------------
# 6. Storage Client Tests
# -----------------------------------------------------------------------------

def test_local_storage_crud(tmp_path):
    client = LocalStorageClient(storage_dir=str(tmp_path))
    content = b"Sample Proof-of-Delivery PDF Content"

    # Upload
    url = client.upload_file(content, "pod_order_1001.pdf", content_type="application/pdf")
    assert "/static/uploads/pod_order_1001.pdf" in url
    assert client.file_exists("pod_order_1001.pdf") is True

    # Download
    downloaded = client.download_file("pod_order_1001.pdf")
    assert downloaded == content

    # Delete
    assert client.delete_file("pod_order_1001.pdf") is True
    assert client.file_exists("pod_order_1001.pdf") is False


def test_local_storage_path_traversal_prevention(tmp_path):
    client = LocalStorageClient(storage_dir=str(tmp_path))
    for evil_path in ["../../etc/passwd", "/root/secret.key", "\\windows\\system32", "../test.txt"]:
        with pytest.raises(ValueError, match="Security error"):
            client.upload_file(b"malicious", evil_path)


def test_dual_mode_storage_fallback():
    # When no AWS credentials, should fall back cleanly to local
    client = DualModeStorageClient(force_backend="auto", bucket_name="")
    assert client.active_mode == "local"
    url = client.upload_file(b"test file", "test.txt")
    assert "/static/uploads/test.txt" in url


def test_mocked_s3_storage_operations():
    mock_s3 = MagicMock()
    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: b"s3 data")}

    client = S3StorageClient(bucket_name="my-logistics-bucket", s3_client=mock_s3)
    url = client.upload_file(b"s3 data", "doc.pdf")
    assert "https://my-logistics-bucket.s3.us-east-1.amazonaws.com/doc.pdf" in url
    mock_s3.put_object.assert_called_once()

    data = client.download_file("doc.pdf")
    assert data == b"s3 data"

    assert client.delete_file("doc.pdf") is True
    mock_s3.delete_object.assert_called_once()


# -----------------------------------------------------------------------------
# 7. Database Initialization & Seeding Tests
# -----------------------------------------------------------------------------

def test_init_database_seeding(temp_db):
    conn = get_db_connection(temp_db)
    counts = seed_demo_dataset(conn)

    assert counts["roles"] == 5
    assert counts["users"] >= 6
    assert counts["warehouses"] >= 4
    assert counts["products"] >= 10
    assert counts["corridors"] >= 10
    assert counts["inventory"] >= 11

    # Verify low-stock items exist
    cur = conn.cursor()
    low_stock = cur.execute("""
        SELECT count(*) FROM inventory i 
        JOIN products p ON i.product_id = p.id 
        WHERE i.quantity_available <= p.reorder_threshold
    """).fetchone()[0]
    assert low_stock >= 4


def test_seeded_network_cte_routing(temp_db):
    conn = get_db_connection(temp_db)
    seed_demo_dataset(conn)

    # Recursive CTE route query from Chicago CDC to New York LSF
    cur = conn.cursor()
    row = cur.execute("""
        WITH RECURSIVE path_finder(current_wh, full_path, total_distance, hops) AS (
            SELECT 'wh_cdc_chi', 'wh_cdc_chi', 0.0, 0
            UNION ALL
            SELECT c.target_warehouse_id, pf.full_path || ' -> ' || c.target_warehouse_id, pf.total_distance + c.distance_km, pf.hops + 1
            FROM network_corridors c JOIN path_finder pf ON c.source_warehouse_id = pf.current_wh
            WHERE pf.hops < 3 AND pf.full_path NOT LIKE '%' || c.target_warehouse_id || '%'
        )
        SELECT full_path, total_distance FROM path_finder WHERE current_wh = 'wh_lsf_nyc' ORDER BY total_distance ASC LIMIT 1
    """).fetchone()

    assert row is not None
    assert "wh_hub_cmh" in row["full_path"]
    assert row["total_distance"] == 1435.0


def test_seeded_corridor_bottleneck(temp_db):
    conn = get_db_connection(temp_db)
    seed_demo_dataset(conn)

    cur = conn.cursor()
    bottlenecks = cur.execute("""
        SELECT id, source_warehouse_id, target_warehouse_id, (current_load_kg / capacity_max_kg) as load_ratio
        FROM network_corridors
        WHERE (current_load_kg / capacity_max_kg) >= 0.85
    """).fetchall()

    assert len(bottlenecks) >= 1
    # Verify the designated bottleneck corridor
    ids = [b["id"] for b in bottlenecks]
    assert any("cmh" in i and "nyc" in i for i in ids)
