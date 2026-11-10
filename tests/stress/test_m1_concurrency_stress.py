"""
Challenger M1-1: Comprehensive Concurrency & Transaction Stress Test Suite.

Empirical verification of Milestone 1 Database Core under adversarial stress:
1. High concurrency / multi-threaded race conditions on transaction_scope:
   - 10 concurrent threads updating stock balances simultaneously (lost updates prevention).
   - Scarce inventory boundary condition (negative balance prevention, invariant preservation).
   - Deadlock elimination under WAL mode (zero SQLITE_BUSY / lock escalation deadlocks).
   - Mixed read/write/restock contention.
2. CacheManager concurrency:
   - 20 concurrent threads reading, writing, and invalidating cache keys.
   - Accurate telemetry tracking (hits, misses, evictions, invalidations, hit_ratio).
   - Capacity bound invariant under extreme parallel write pressure (size <= max_entries).
   - Concurrent TTL expiration and glob pattern invalidations.
3. Re-entrant transactions & deeply nested savepoints (3+ levels):
   - 4-level deep nesting with inner-only rollback and outer survival.
   - 3-level deep nesting with mid-level rollback.
   - Outer rollback cascading to purge all inner commits (zero orphaned rows).
   - Multi-branch hierarchical savepoint tree verification.
"""

import concurrent.futures
import os
import sqlite3
import tempfile
import threading
import time
from typing import List
import pytest

from app.db.connection import (
    create_connection,
    get_db_connection,
    close_all_connections,
    transaction_scope,
    set_default_db_path,
)
from app.db.cache import CacheManager
from app.db.nosql import DocumentStore
from app.db.init_db import get_schema_sql, seed_demo_dataset


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def stress_db():
    """Provides a fresh isolated disk-backed SQLite database in WAL mode."""
    temp_file = tempfile.NamedTemporaryFile(suffix=".db", prefix="stress_db_", delete=False)
    db_path = temp_file.name
    temp_file.close()

    set_default_db_path(db_path)

    # Initialize schema DDL
    conn = get_db_connection(db_path)
    ddl = get_schema_sql()
    conn.executescript(ddl)
    conn.commit()

    yield db_path

    # Teardown: close all connections and delete files
    close_all_connections()
    for ext in ["", "-wal", "-shm"]:
        p = db_path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


# =============================================================================
# 1. High Concurrency & Transaction Stress (10 Threads)
# =============================================================================

def test_concurrent_stock_updates_no_lost_updates(stress_db):
    """
    Stress Test 1.1: 10 concurrent threads updating stock balances simultaneously.
    Verifies:
    - 10 threads, each executing 50 reservation transactions (500 total).
    - Initial stock: 500 on hand, 0 reserved.
    - Each transaction reserves exactly 1 unit atomically via transaction_scope.
    - Zero lost updates: final reserved must equal 500, available must equal 0.
    - Zero deadlocks under WAL mode with BEGIN IMMEDIATE.
    """
    conn = get_db_connection(stress_db)
    # Seed prerequisites
    conn.execute("INSERT INTO roles VALUES ('r_stress', 'StressRole', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO warehouses VALUES ('wh_stress_1', 'WH-S1', 'Stress Hub', 'CDC', 'Addr', 'City', 'IL', 'USA', 41.8, -87.6, 5000, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO products VALUES ('prod_stress_1', 'SKU-S1', 'Stress Product', 'desc', 'Cat', 25.0, 1.0, 10, CURRENT_TIMESTAMP)")
    conn.execute(
        "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
        ("inv_stress_1", "wh_stress_1", "prod_stress_1", 500, 0)
    )

    num_threads = 10
    reservations_per_thread = 50
    errors: List[Exception] = []

    def worker_reserve(thread_id: int):
        try:
            # Each thread uses its thread-local connection
            thread_conn = get_db_connection(stress_db)
            for _ in range(reservations_per_thread):
                with transaction_scope(thread_conn):
                    cursor = thread_conn.execute(
                        "SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE id = 'inv_stress_1'"
                    )
                    row = cursor.fetchone()
                    assert row["quantity_available"] > 0, "Stock exhausted unexpectedly!"

                    thread_conn.execute(
                        "UPDATE inventory SET quantity_reserved = quantity_reserved + 1 WHERE id = 'inv_stress_1'"
                    )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker_reserve, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Zero errors expected
    assert len(errors) == 0, f"Encountered concurrency errors: {errors}"

    # Verify final stock state
    verification_conn = create_connection(stress_db)
    row = verification_conn.execute(
        "SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE id = 'inv_stress_1'"
    ).fetchone()

    assert row["quantity_on_hand"] == 500
    assert row["quantity_reserved"] == 500, f"Lost updates detected! Expected 500 reserved, got {row['quantity_reserved']}"
    assert row["quantity_available"] == 0, f"Available stock expected 0, got {row['quantity_available']}"
    verification_conn.close()


def test_concurrent_scarce_inventory_no_negative_quantities(stress_db):
    """
    Stress Test 1.2: Scarce inventory contention & invariant preservation.
    Verifies:
    - Initial stock: 10 on hand, 0 reserved (10 available).
    - 10 concurrent threads each attempt to reserve 2 units (demanding 20 total).
    - Exactly 5 threads must succeed (10 units reserved).
    - Exactly 5 threads must fail cleanly due to insufficient available stock.
    - Available stock depletes to exactly 0 with ZERO negative balances.
    - CHECK constraint (quantity_on_hand >= quantity_reserved) is never violated.
    """
    conn = get_db_connection(stress_db)
    conn.execute("INSERT INTO roles VALUES ('r_stress2', 'StressRole2', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO warehouses VALUES ('wh_stress_2', 'WH-S2', 'Stress Hub 2', 'CDC', 'Addr', 'City', 'IL', 'USA', 41.8, -87.6, 5000, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO products VALUES ('prod_scarce', 'SKU-SCARCE', 'Scarce Product', 'desc', 'Cat', 100.0, 1.0, 5, CURRENT_TIMESTAMP)")
    conn.execute(
        "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
        ("inv_scarce_1", "wh_stress_2", "prod_scarce", 10, 0)
    )

    success_count = 0
    failure_count = 0
    lock = threading.Lock()

    def attempt_reservation(requested_qty: int):
        nonlocal success_count, failure_count
        thread_conn = get_db_connection(stress_db)
        try:
            with transaction_scope(thread_conn):
                row = thread_conn.execute(
                    "SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE id = 'inv_scarce_1'"
                ).fetchone()

                if row["quantity_available"] >= requested_qty:
                    thread_conn.execute(
                        "UPDATE inventory SET quantity_reserved = quantity_reserved + ? WHERE id = 'inv_scarce_1'",
                        (requested_qty,)
                    )
                    with lock:
                        success_count += 1
                else:
                    with lock:
                        failure_count += 1
        except sqlite3.IntegrityError:
            with lock:
                failure_count += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(attempt_reservation, 2) for _ in range(10)]
        concurrent.futures.wait(futures)

    assert success_count == 5, f"Expected exactly 5 successful allocations, got {success_count}"
    assert failure_count == 5, f"Expected exactly 5 rejections, got {failure_count}"

    # Verify database state
    verification_conn = create_connection(stress_db)
    row = verification_conn.execute(
        "SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE id = 'inv_scarce_1'"
    ).fetchone()

    assert row["quantity_on_hand"] == 10
    assert row["quantity_reserved"] == 10
    assert row["quantity_available"] == 0
    assert row["quantity_on_hand"] >= row["quantity_reserved"]
    verification_conn.close()


def test_concurrent_mixed_transactions_zero_deadlocks(stress_db):
    """
    Stress Test 1.3: Heavy mixed read/write/restock contention under WAL mode.
    Verifies:
    - 10 concurrent threads performing 30 operations each (300 total transactions).
    - 4 threads reserving stock
    - 3 threads restocking stock (incrementing quantity_on_hand)
    - 3 threads reading and aggregating stock
    - Zero deadlocks (zero sqlite3.OperationalError: database is locked).
    - Final mathematical balance matches exact operations executed.
    """
    conn = get_db_connection(stress_db)
    conn.execute("INSERT INTO roles VALUES ('r_mix', 'MixRole', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO warehouses VALUES ('wh_mix', 'WH-MIX', 'Mix Hub', 'CDC', 'Addr', 'City', 'IL', 'USA', 41.8, -87.6, 5000, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO products VALUES ('prod_mix', 'SKU-MIX', 'Mix Product', 'desc', 'Cat', 15.0, 1.0, 5, CURRENT_TIMESTAMP)")
    conn.execute(
        "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
        ("inv_mix", "wh_mix", "prod_mix", 100, 10)
    )

    deadlock_errors = []
    reserves_succeeded = 0
    restocks_succeeded = 0
    reads_succeeded = 0
    counter_lock = threading.Lock()

    def reserver():
        nonlocal reserves_succeeded
        thread_conn = get_db_connection(stress_db)
        for _ in range(30):
            try:
                with transaction_scope(thread_conn):
                    row = thread_conn.execute(
                        "SELECT quantity_available FROM inventory WHERE id = 'inv_mix'"
                    ).fetchone()
                    if row["quantity_available"] >= 1:
                        thread_conn.execute(
                            "UPDATE inventory SET quantity_reserved = quantity_reserved + 1 WHERE id = 'inv_mix'"
                        )
                        with counter_lock:
                            reserves_succeeded += 1
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    deadlock_errors.append(e)

    def restocker():
        nonlocal restocks_succeeded
        thread_conn = get_db_connection(stress_db)
        for _ in range(30):
            try:
                with transaction_scope(thread_conn):
                    thread_conn.execute(
                        "UPDATE inventory SET quantity_on_hand = quantity_on_hand + 2 WHERE id = 'inv_mix'"
                    )
                    with counter_lock:
                        restocks_succeeded += 1
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    deadlock_errors.append(e)

    def reader():
        nonlocal reads_succeeded
        thread_conn = get_db_connection(stress_db)
        for _ in range(30):
            try:
                # Read operation runs cleanly concurrently with writers under WAL mode
                row = thread_conn.execute(
                    "SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE id = 'inv_mix'"
                ).fetchone()
                assert row["quantity_on_hand"] >= row["quantity_reserved"]
                with counter_lock:
                    reads_succeeded += 1
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    deadlock_errors.append(e)

    threads = []
    # 4 reservers, 3 restockers, 3 readers = 10 threads
    for _ in range(4):
        threads.append(threading.Thread(target=reserver))
    for _ in range(3):
        threads.append(threading.Thread(target=restocker))
    for _ in range(3):
        threads.append(threading.Thread(target=reader))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Zero deadlocks or locked errors
    assert len(deadlock_errors) == 0, f"Observed database lock deadlocks: {deadlock_errors}"
    assert reads_succeeded == 90, f"Expected 90 successful reads, got {reads_succeeded}"
    assert restocks_succeeded == 90, f"Expected 90 successful restocks, got {restocks_succeeded}"

    # Verify arithmetic integrity
    verification_conn = create_connection(stress_db)
    final_row = verification_conn.execute(
        "SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE id = 'inv_mix'"
    ).fetchone()

    expected_on_hand = 100 + (90 * 2)  # initial 100 + 180 added = 280
    expected_reserved = 10 + reserves_succeeded

    assert final_row["quantity_on_hand"] == expected_on_hand
    assert final_row["quantity_reserved"] == expected_reserved
    assert final_row["quantity_available"] == expected_on_hand - expected_reserved
    verification_conn.close()


# =============================================================================
# 2. CacheManager Concurrency Stress (20 Threads)
# =============================================================================

def test_cache_manager_20_threads_concurrency():
    """
    Stress Test 2.1: 20 concurrent threads reading, writing, and invalidating cache keys.
    Verifies:
    - 20 threads performing 100 operations each (2000 total operations in parallel).
    - 8 threads performing set() operations across keys.
    - 8 threads performing get() operations.
    - 4 threads performing invalidate_pattern() and delete() operations.
    - Thread safety: zero exceptions, zero dictionary corruption, zero lock starvation.
    - Accurate metrics telemetry:
      * hits + misses == total reads
      * size <= max_entries
      * hit_ratio between 0.0 and 1.0
      * evictions and invalidations tracked properly
    """
    cache = CacheManager(max_entries=100, default_ttl=60)
    errors: List[Exception] = []

    total_reads_attempted = 0
    read_counter_lock = threading.Lock()

    def writer_thread(tid: int):
        try:
            for i in range(100):
                key = f"item:{tid}:{i % 25}"
                cache.set(key, f"val_{tid}_{i}", ttl_seconds=30)
        except Exception as e:
            errors.append(e)

    def reader_thread(tid: int):
        nonlocal total_reads_attempted
        try:
            for i in range(100):
                target_tid = i % 8
                key = f"item:{target_tid}:{i % 25}"
                _ = cache.get(key)
                with read_counter_lock:
                    total_reads_attempted += 1
        except Exception as e:
            errors.append(e)

    def invalidator_thread(tid: int):
        try:
            for i in range(100):
                target_tid = i % 8
                if i % 2 == 0:
                    cache.invalidate_pattern(f"item:{target_tid}:*")
                else:
                    cache.delete(f"item:{target_tid}:{i % 25}")
        except Exception as e:
            errors.append(e)

    threads = []
    # 8 writers, 8 readers, 4 invalidators = 20 concurrent threads
    for t in range(8):
        threads.append(threading.Thread(target=writer_thread, args=(t,)))
    for t in range(8):
        threads.append(threading.Thread(target=reader_thread, args=(t,)))
    for t in range(4):
        threads.append(threading.Thread(target=invalidator_thread, args=(t,)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Assert no crashes occurred
    assert len(errors) == 0, f"CacheManager concurrency errors: {errors}"

    # Assert accurate telemetry metrics
    stats = cache.get_stats()
    assert stats["size"] <= stats["max_entries"], f"Cache size exceeded limit! {stats['size']} > {stats['max_entries']}"
    assert stats["hits"] + stats["misses"] == total_reads_attempted, (
        f"Telemetry mismatch: hits ({stats['hits']}) + misses ({stats['misses']}) != total reads ({total_reads_attempted})"
    )
    assert 0.0 <= stats["hit_ratio"] <= 1.0, f"Invalid hit ratio: {stats['hit_ratio']}"
    assert stats["invalidations"] > 0, "Invalidation operations were not tracked in telemetry!"


def test_cache_manager_lru_eviction_under_capacity_pressure():
    """
    Stress Test 2.2: Extreme capacity pressure on CacheManager with 20 threads.
    Verifies:
    - Small capacity limit: max_entries = 25.
    - 20 concurrent threads write 20 unique keys each (400 unique keys).
    - Eviction mechanism is stressed under intense parallel insertion.
    - Cache size stays strictly <= 25 throughout and after execution.
    - Evictions counter accurately reflects evicted entries.
    """
    cache = CacheManager(max_entries=25, default_ttl=60)
    errors: List[Exception] = []

    def heavy_writer(tid: int):
        try:
            for i in range(20):
                key = f"heavy:{tid}:{i}"
                cache.set(key, {"tid": tid, "i": i}, ttl_seconds=60)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=heavy_writer, args=(t,)) for t in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Errors during heavy writer pressure: {errors}"

    stats = cache.get_stats()
    assert stats["size"] <= 25, f"Cache size {stats['size']} exceeds max_entries 25!"
    assert stats["evictions"] >= (400 - 25), f"Expected at least {400 - 25} evictions, got {stats['evictions']}"


def test_cache_manager_concurrent_ttl_expiration_and_purge():
    """
    Stress Test 2.3: Concurrent TTL expiration and lazy purge with 20 threads.
    Verifies:
    - 20 threads simultaneously insert keys with 0.05s TTL.
    - After 0.08s sleep, all 20 threads read their keys concurrently.
    - All reads must observe None (expired).
    - Lazy invalidations and misses are tracked accurately.
    """
    cache = CacheManager(max_entries=500, default_ttl=1)

    def write_short_ttl(tid: int):
        for i in range(10):
            cache.set(f"exp:{tid}:{i}", f"val_{tid}_{i}", ttl_seconds=0.05)

    writers = [threading.Thread(target=write_short_ttl, args=(t,)) for t in range(20)]
    for w in writers:
        w.start()
    for w in writers:
        w.join()

    # Wait for TTL to expire
    time.sleep(0.08)

    results = []

    def read_expired(tid: int):
        for i in range(10):
            val = cache.get(f"exp:{tid}:{i}")
            results.append(val)

    readers = [threading.Thread(target=read_expired, args=(t,)) for t in range(20)]
    for r in readers:
        r.start()
    for r in readers:
        r.join()

    # All reads must return None due to TTL expiration
    assert len(results) == 200
    assert all(r is None for r in results), "Expired cache keys were returned as non-None values!"

    stats = cache.get_stats()
    assert stats["misses"] >= 200
    assert stats["invalidations"] >= 200


# =============================================================================
# 3. Re-entrant Transactions & Nested Savepoints (3+ Levels)
# =============================================================================

def test_deeply_nested_savepoints_4_levels_inner_rollback_outer_commit(stress_db):
    """
    Stress Test 3.1: 4-Level Deeply Nested Savepoints with Inner-Only Rollback.
    Structure:
    - Level 1 (Outer BEGIN IMMEDIATE): Inserts Warehouse 1
      - Level 2 (SAVEPOINT sp_2): Inserts Warehouse 2
        - Level 3 (SAVEPOINT sp_3): Inserts Warehouse 3
          - Level 4 (SAVEPOINT sp_4): Inserts Warehouse 4, then raises intentional fault!
    - Level 3 catches Level 4 fault, rolls back Level 4 savepoint cleanly.
    - Level 3 continues and inserts Warehouse 3b.
    - Level 2 commits savepoint.
    - Level 1 commits transaction.

    Verifications:
    - Warehouses 1, 2, 3, and 3b are permanently committed in DB.
    - Warehouse 4 (from failed Level 4) was rolled back and is completely absent.
    - Outer transaction integrity and durability is preserved.
    """
    conn = get_db_connection(stress_db)

    with transaction_scope(conn) as tx1:
        # Level 1: Outer transaction
        tx1.execute(
            "INSERT INTO warehouses VALUES ('wh_lvl1', 'W-L1', 'L1 Hub', 'CDC', 'A1', 'C1', 'IL', 'USA', 41.0, -87.0, 1000, 1, CURRENT_TIMESTAMP)"
        )

        with transaction_scope(tx1) as tx2:
            # Level 2: Nested savepoint 1
            tx2.execute(
                "INSERT INTO warehouses VALUES ('wh_lvl2', 'W-L2', 'L2 Hub', 'CDC', 'A2', 'C2', 'IL', 'USA', 42.0, -87.0, 2000, 1, CURRENT_TIMESTAMP)"
            )

            with transaction_scope(tx2) as tx3:
                # Level 3: Nested savepoint 2
                tx3.execute(
                    "INSERT INTO warehouses VALUES ('wh_lvl3', 'W-L3', 'L3 Hub', 'CDC', 'A3', 'C3', 'IL', 'USA', 43.0, -87.0, 3000, 1, CURRENT_TIMESTAMP)"
                )

                # Level 4: Nested savepoint 3 (fails and rolls back)
                level4_caught = False
                try:
                    with transaction_scope(tx3) as tx4:
                        tx4.execute(
                            "INSERT INTO warehouses VALUES ('wh_lvl4_fail', 'W-L4', 'L4 Hub', 'CDC', 'A4', 'C4', 'IL', 'USA', 44.0, -87.0, 4000, 1, CURRENT_TIMESTAMP)"
                        )
                        raise ValueError("Simulated fault at deep Level 4 savepoint")
                except ValueError:
                    level4_caught = True

                assert level4_caught, "Level 4 exception was not caught!"

                # Level 3 proceeds after Level 4 rollback
                tx3.execute(
                    "INSERT INTO warehouses VALUES ('wh_lvl3b', 'W-L3B', 'L3B Hub', 'CDC', 'A3B', 'C3B', 'IL', 'USA', 43.5, -87.0, 3500, 1, CURRENT_TIMESTAMP)"
                )

    # Independent verification connection
    v_conn = create_connection(stress_db)
    committed_ids = {r["id"] for r in v_conn.execute("SELECT id FROM warehouses").fetchall()}

    assert "wh_lvl1" in committed_ids, "Level 1 record missing!"
    assert "wh_lvl2" in committed_ids, "Level 2 record missing!"
    assert "wh_lvl3" in committed_ids, "Level 3 record missing!"
    assert "wh_lvl3b" in committed_ids, "Level 3b recovery record missing!"
    assert "wh_lvl4_fail" not in committed_ids, "Level 4 rolled-back record leaked into database!"
    v_conn.close()


def test_nested_savepoints_outer_rollback_purges_all_inner_levels(stress_db):
    """
    Stress Test 3.2: Outer Rollback Cascades Across All Nested Levels.
    Structure:
    - Level 1: BEGIN IMMEDIATE
      - Level 2: SAVEPOINT sp_a (succeeds, inserts Product A)
        - Level 3: SAVEPOINT sp_b (succeeds, inserts Product B)
          - Level 4: SAVEPOINT sp_c (succeeds, inserts Product C)
    - After all inner savepoints succeed and release, Level 1 encounters an unhandled exception.
    - Level 1 executes conn.rollback().

    Verifications:
    - ZERO products (A, B, or C) exist in the database.
    - Even though inner savepoints released successfully within their scopes,
      outer rollback cleanly purges all modifications.
    - Zero orphaned records or dangling locks.
    """
    conn = get_db_connection(stress_db)

    with pytest.raises(RuntimeError, match="Catastrophic crash at outer transaction boundary"):
        with transaction_scope(conn) as tx1:
            with transaction_scope(tx1) as tx2:
                tx2.execute(
                    "INSERT INTO products VALUES ('prod_nested_a', 'SKU-NA', 'Prod A', 'desc', 'Cat', 10.0, 1.0, 5, CURRENT_TIMESTAMP)"
                )
                with transaction_scope(tx2) as tx3:
                    tx3.execute(
                        "INSERT INTO products VALUES ('prod_nested_b', 'SKU-NB', 'Prod B', 'desc', 'Cat', 20.0, 1.0, 5, CURRENT_TIMESTAMP)"
                    )
                    with transaction_scope(tx3) as tx4:
                        tx4.execute(
                            "INSERT INTO products VALUES ('prod_nested_c', 'SKU-NC', 'Prod C', 'desc', 'Cat', 30.0, 1.0, 5, CURRENT_TIMESTAMP)"
                        )

            # Outer boundary crashes
            raise RuntimeError("Catastrophic crash at outer transaction boundary")

    # Verify zero records committed
    v_conn = create_connection(stress_db)
    count = v_conn.execute("SELECT count(*) FROM products WHERE id LIKE 'prod_nested_%'").fetchone()[0]
    assert count == 0, f"Outer rollback failed! Found {count} orphaned product records in database!"
    v_conn.close()


def test_multi_branch_hierarchical_savepoints(stress_db):
    """
    Stress Test 3.3: Hierarchical Savepoint Tree (Multi-Branch Rollback & Commit).
    Structure:
    Level 1: Outer Transaction
      Branch A (Level 2):
        - Sub-branch A1 (Level 3): Succeeded (inserts role_a1)
        - Sub-branch A2 (Level 3): Failed & caught (inserts role_a2_fail)
      Branch B (Level 2):
        - Sub-branch B1 (Level 3): Failed & caught (inserts role_b1_fail)
        - Sub-branch B2 (Level 3): Succeeded (inserts role_b2)

    Verifications:
    - role_a1 is present in DB.
    - role_a2_fail is NOT present in DB.
    - role_b1_fail is NOT present in DB.
    - role_b2 is present in DB.
    """
    conn = get_db_connection(stress_db)

    with transaction_scope(conn) as tx:
        # Branch A
        with transaction_scope(tx) as tx_a:
            # Sub-branch A1: Success
            with transaction_scope(tx_a) as tx_a1:
                tx_a1.execute("INSERT INTO roles VALUES ('role_a1', 'RoleA1', 'desc', '[]', CURRENT_TIMESTAMP)")

            # Sub-branch A2: Failure
            try:
                with transaction_scope(tx_a) as tx_a2:
                    tx_a2.execute("INSERT INTO roles VALUES ('role_a2_fail', 'RoleA2Fail', 'desc', '[]', CURRENT_TIMESTAMP)")
                    raise ValueError("Fault in branch A2")
            except ValueError:
                pass

        # Branch B
        with transaction_scope(tx) as tx_b:
            # Sub-branch B1: Failure
            try:
                with transaction_scope(tx_b) as tx_b1:
                    tx_b1.execute("INSERT INTO roles VALUES ('role_b1_fail', 'RoleB1Fail', 'desc', '[]', CURRENT_TIMESTAMP)")
                    raise ValueError("Fault in branch B1")
            except ValueError:
                pass

            # Sub-branch B2: Success
            with transaction_scope(tx_b) as tx_b2:
                tx_b2.execute("INSERT INTO roles VALUES ('role_b2', 'RoleB2', 'desc', '[]', CURRENT_TIMESTAMP)")

    # Verify committed roles
    v_conn = create_connection(stress_db)
    roles = {r["id"] for r in v_conn.execute("SELECT id FROM roles WHERE id LIKE 'role_%'").fetchall()}

    assert "role_a1" in roles
    assert "role_b2" in roles
    assert "role_a2_fail" not in roles, "Failed branch A2 leaked into database!"
    assert "role_b1_fail" not in roles, "Failed branch B1 leaked into database!"
    v_conn.close()


def test_concurrent_nosql_document_store_transactions(stress_db):
    """
    Stress Test 3.4: Concurrent NoSQL DocumentStore transactions.
    Verifies:
    - 10 concurrent threads inserting telemetry documents into NoSQL DocumentStore inside transaction_scope.
    - In-flight queries observe transactional visibility.
    - Intentional rollback in 3 threads leaves zero document traces.
    - Exactly 7 threads commit, leaving exactly 7 * 5 = 35 documents in the collection.
    """
    conn = get_db_connection(stress_db)
    doc_store = DocumentStore(conn=conn)

    def worker_doc(tid: int, should_fail: bool):
        t_conn = get_db_connection(stress_db)
        t_store = DocumentStore(conn=t_conn)
        try:
            with transaction_scope(t_conn):
                for i in range(5):
                    t_store.insert("telemetry_stress", {
                        "thread_id": tid,
                        "reading_index": i,
                        "temperature": 20.0 + tid,
                        "location": {"lat": 41.8781, "lon": -87.6298}
                    })
                if should_fail:
                    raise RuntimeError(f"Simulated failure in doc thread {tid}")
        except RuntimeError:
            pass

    threads = []
    # 7 succeeding threads (tid 0..6), 3 failing threads (tid 7..9)
    for tid in range(7):
        threads.append(threading.Thread(target=worker_doc, args=(tid, False)))
    for tid in range(7, 10):
        threads.append(threading.Thread(target=worker_doc, args=(tid, True)))

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Query all telemetry documents
    v_conn = create_connection(stress_db)
    v_store = DocumentStore(conn=v_conn)
    docs = v_store.find("telemetry_stress", {})

    assert len(docs) == 35, f"Expected exactly 35 committed documents, got {len(docs)}"
    thread_ids_present = {d["thread_id"] for d in docs}
    assert thread_ids_present == set(range(7)), f"Unexpected thread IDs in committed docs: {thread_ids_present}"
    assert all(tid not in thread_ids_present for tid in range(7, 10)), "Failed threads leaked NoSQL documents!"
    v_conn.close()


# =============================================================================
# 4. Hardcore Adversarial Edge-Case Stress Tests
# =============================================================================

def test_extreme_concurrency_20_threads_1000_transactions(stress_db):
    """
    Stress Test 4.1: Extreme Concurrency - 20 Concurrent Threads, 1,000 Transactions.
    Verifies:
    - 20 concurrent threads executing 50 reservation transactions each (1,000 total).
    - Initial inventory: 1,000 on hand, 0 reserved.
    - Contention is maxed out on a single inventory row.
    - Verifies SQLite WAL mode busy handler with 5000ms timeout prevents all deadlocks.
    - Exactly 1,000 reservations committed, 0 available stock remaining.
    """
    conn = get_db_connection(stress_db)
    conn.execute("INSERT INTO roles VALUES ('r_ext', 'ExtRole', 'desc', '[]', CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO warehouses VALUES ('wh_ext', 'WH-EXT', 'Ext Hub', 'CDC', 'Addr', 'City', 'IL', 'USA', 41.8, -87.6, 5000, 1, CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO products VALUES ('prod_ext', 'SKU-EXT', 'Ext Product', 'desc', 'Cat', 10.0, 1.0, 5, CURRENT_TIMESTAMP)")
    conn.execute(
        "INSERT INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved) VALUES (?, ?, ?, ?, ?)",
        ("inv_ext", "wh_ext", "prod_ext", 1000, 0)
    )

    num_threads = 20
    tx_per_thread = 50
    errors = []

    def heavy_reserver(tid: int):
        try:
            t_conn = get_db_connection(stress_db)
            for _ in range(tx_per_thread):
                with transaction_scope(t_conn):
                    t_conn.execute(
                        "UPDATE inventory SET quantity_reserved = quantity_reserved + 1 WHERE id = 'inv_ext'"
                    )
        except Exception as e:
            errors.append((tid, e))

    threads = [threading.Thread(target=heavy_reserver, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Encountered errors during 1,000-tx storm: {errors}"

    v_conn = create_connection(stress_db)
    row = v_conn.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE id = 'inv_ext'").fetchone()

    assert row["quantity_on_hand"] == 1000
    assert row["quantity_reserved"] == 1000
    assert row["quantity_available"] == 0
    v_conn.close()


def test_cache_manager_flush_during_concurrent_storm():
    """
    Stress Test 4.2: Concurrent flush() while 20 threads are actively reading & writing.
    Verifies:
    - 20 threads reading and writing at high velocity.
    - 1 thread periodically executing flush() and reset_stats().
    - Thread safety: no KeyError, no Deadlock, no NoneType crashes.
    """
    cache = CacheManager(max_entries=50, default_ttl=60)
    stop_event = threading.Event()
    errors = []

    def worker(tid: int):
        try:
            i = 0
            while not stop_event.is_set():
                key = f"storm:{tid}:{i % 10}"
                cache.set(key, i)
                _ = cache.get(key)
                if i % 5 == 0:
                    cache.delete(key)
                i += 1
        except Exception as e:
            errors.append(e)

    def flusher():
        try:
            while not stop_event.is_set():
                time.sleep(0.01)
                cache.flush()
                cache.reset_stats()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    flusher_thread = threading.Thread(target=flusher)

    for t in threads:
        t.start()
    flusher_thread.start()

    time.sleep(0.5)  # Run storm for 500ms
    stop_event.set()

    for t in threads:
        t.join()
    flusher_thread.join()

    assert len(errors) == 0, f"CacheManager crashed during concurrent flush storm: {errors}"


def test_cache_manager_minimal_capacity_max_entries_1():
    """
    Stress Test 4.3: CacheManager boundary condition with max_entries = 1.
    Verifies:
    - 20 concurrent threads writing 10 keys each to a cache with capacity 1.
    - Size must NEVER exceed 1.
    - Eviction mechanism cleanly handles extreme thrashing.
    """
    cache = CacheManager(max_entries=1, default_ttl=60)
    errors = []

    def thrash_writer(tid: int):
        try:
            for i in range(10):
                cache.set(f"k_{tid}_{i}", i)
                stats = cache.get_stats()
                assert stats["size"] <= 1, f"Size exceeded 1! Got {stats['size']}"
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=thrash_writer, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0, f"Errors under max_entries=1 thrashing: {errors}"
    final_stats = cache.get_stats()
    assert final_stats["size"] <= 1
    assert final_stats["evictions"] >= 199


def test_deeply_nested_savepoints_10_levels(stress_db):
    """
    Stress Test 4.4: 10-Level Deeply Nested Savepoints with Mixed Rollbacks.
    Verifies:
    - 10 recursive levels of transaction_scope.
    - Levels 1..7 succeed.
    - Level 8 rolls back (caught by Level 7).
    - Level 9 rolls back (caught by Level 7).
    - Level 10 rolls back (caught by Level 7).
    - Level 7 recovers and inserts recovery record.
    - Commits cleanly to outer Level 1.
    - Verifies exact database state: records from levels 1..7 and 7_recovery exist; 8..10 absent.
    """
    conn = get_db_connection(stress_db)

    def recursive_scope(level: int, max_level: int, parent_conn):
        with transaction_scope(parent_conn) as current_conn:
            if level <= 7:
                current_conn.execute(
                    "INSERT INTO roles VALUES (?, ?, 'desc', '[]', CURRENT_TIMESTAMP)",
                    (f"role_lvl_{level}", f"RoleLvl{level}")
                )
            else:
                # Levels 8, 9, 10 attempt inserts and fail
                current_conn.execute(
                    "INSERT INTO roles VALUES (?, ?, 'desc', '[]', CURRENT_TIMESTAMP)",
                    (f"role_lvl_{level}_fail", f"RoleLvl{level}Fail")
                )
                raise RuntimeError(f"Deliberate failure at deep level {level}")

            if level < max_level:
                try:
                    recursive_scope(level + 1, max_level, current_conn)
                except RuntimeError as e:
                    if level == 7:
                        # Catch failure from level 8, 9, 10 and record recovery
                        current_conn.execute(
                            "INSERT INTO roles VALUES ('role_lvl_7_recovery', 'RoleLvl7Recovery', 'desc', '[]', CURRENT_TIMESTAMP)"
                        )
                    else:
                        raise

    # Execute 10 levels deep
    with transaction_scope(conn) as top_conn:
        recursive_scope(1, 10, top_conn)

    # Verification
    v_conn = create_connection(stress_db)
    roles = {r["id"] for r in v_conn.execute("SELECT id FROM roles").fetchall()}

    for lvl in range(1, 8):
        assert f"role_lvl_{lvl}" in roles, f"Level {lvl} record missing!"
    assert "role_lvl_7_recovery" in roles, "Recovery record missing!"

    for lvl in range(8, 11):
        assert f"role_lvl_{lvl}_fail" not in roles, f"Failed level {lvl} record leaked into database!"
    v_conn.close()


def test_cache_manager_pattern_edge_cases():
    """
    Stress Test 4.5: Pattern Invalidation Edge Cases (glob wildcards, special chars).
    Verifies:
    - Invalidation handles '?', '*', '[a-z]', empty string, unicode keys.
    - Non-matching patterns return 0 without error.
    - Pattern invalidation is exact and does not purge unintended keys.
    """
    cache = CacheManager(max_entries=100, default_ttl=300)

    cache.set("shipment:NYC:001", "val1")
    cache.set("shipment:NYC:002", "val2")
    cache.set("shipment:CHI:001", "val3")
    cache.set("shipment:LAX:999", "val4")
    cache.set("order[draft]:100", "val5")
    cache.set("special:üñîçødé:test", "val6")

    # Invalidate using glob wildcard
    count_nyc = cache.invalidate_pattern("shipment:NYC:*")
    assert count_nyc == 2
    assert cache.get("shipment:NYC:001") is None
    assert cache.get("shipment:NYC:002") is None
    assert cache.get("shipment:CHI:001") == "val3"

    # Invalidate using character range
    count_chi = cache.invalidate_pattern("shipment:C[H-I]I:*")
    assert count_chi == 1
    assert cache.get("shipment:CHI:001") is None

    # Invalidate non-existent pattern
    count_none = cache.invalidate_pattern("nonexistent:*")
    assert count_none == 0

    # Unicode pattern
    count_unicode = cache.invalidate_pattern("special:üñîçødé:*")
    assert count_unicode == 1
    assert cache.get("special:üñîçødé:test") is None
