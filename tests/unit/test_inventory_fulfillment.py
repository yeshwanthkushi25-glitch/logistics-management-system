"""
Logistics IMS - Milestone 2 Empirical Test Prototype Suite.

Comprehensive unit and integration test suite validating:
1. Multi-warehouse stock tracking and mathematical invariant preservation (Q_available = Q_on_hand - Q_reserved >= 0).
2. Atomic inventory allocation and reservation holds.
3. Inventory restock and cycle count adjustments with NoSQL immutable audit logs.
4. Cache-Aside read acceleration with automatic glob pattern invalidation.
5. Reorder Point (ROP = d * L + SS) calculation and automated low-stock alerting.
6. Multi-warehouse atomic order placement with clean ACID rollback on stockouts, payment declines, or process faults.
7. Pre-dispatch order cancellation with complete stock reservation release.
8. High-concurrency race condition handling on scarce inventory.
"""

import concurrent.futures
import os
import sys
import tempfile
import sqlite3
import pytest

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.db.connection import (
    create_connection,
    get_db_connection,
    transaction_scope,
    set_default_db_path,
)
from app.db.init_db import init_database
from app.db.cache import get_cache_manager
from app.db.nosql import DocumentStore

# Import the services from app.services
from app.services.inventory_service import (
    calculate_reorder_point,
    get_inventory_levels,
    allocate_inventory,
    release_inventory,
    restock_inventory,
    adjust_inventory,
    InsufficientStockException,
    ProductNotFoundException,
    InvalidInventoryOperationException,
)
from app.services.fulfillment_service import (
    create_order_atomic,
    cancel_order,
    get_order,
    list_orders,
    PaymentFailedException,
    OrderNotFoundException,
    OrderStateTransitionException,
)


@pytest.fixture
def test_db():
    """Provides a fresh isolated temporary database initialized with demo data."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="m2_test_")
    os.close(fd)
    set_default_db_path(path)

    conn = init_database(seed_demo_data=True, db_path=path)
    cache = get_cache_manager()
    cache.flush()
    cache.reset_stats()

    yield {"path": path, "conn": conn}

    conn.close()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# =============================================================================
# 1. Mathematical Invariant & Allocation Tests
# =============================================================================

def test_inventory_allocation_success(test_db):
    """Verify allocating stock increments quantity_reserved and preserves on_hand."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    initial = cur.fetchone()
    init_on_hand = initial["quantity_on_hand"]
    init_reserved = initial["quantity_reserved"]
    init_avail = initial["quantity_available"]

    success = allocate_inventory(warehouse_id="wh-01", product_id="prod-01", quantity=5, conn=conn)
    assert success is True

    cur.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    after = cur.fetchone()

    assert after["quantity_on_hand"] == init_on_hand
    assert after["quantity_reserved"] == init_reserved + 5
    assert after["quantity_available"] == init_avail - 5
    assert after["quantity_available"] >= 0


def test_inventory_allocation_insufficient_stock(test_db):
    """Verify attempting to allocate more than available stock raises InsufficientStockException and leaves stock unchanged."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    initial = cur.fetchone()
    avail = initial["quantity_available"]

    with pytest.raises(InsufficientStockException) as exc_info:
        allocate_inventory(warehouse_id="wh-01", product_id="prod-01", quantity=avail + 10, conn=conn)

    assert "Insufficient stock" in str(exc_info.value)

    # Invariant assertion: zero change
    cur.execute("SELECT quantity_reserved FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    after = cur.fetchone()
    assert after["quantity_reserved"] == initial["quantity_reserved"]


def test_inventory_release_success(test_db):
    """Verify releasing stock decrements reserved count and increases available count."""
    conn = test_db["conn"]
    cur = conn.cursor()

    allocate_inventory(warehouse_id="wh-01", product_id="prod-01", quantity=10, conn=conn)
    cur.execute("SELECT quantity_reserved FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    mid_reserved = cur.fetchone()["quantity_reserved"]

    release_inventory(warehouse_id="wh-01", product_id="prod-01", quantity=4, conn=conn)
    cur.execute("SELECT quantity_reserved FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    after_reserved = cur.fetchone()["quantity_reserved"]

    assert after_reserved == mid_reserved - 4


def test_inventory_allocation_invalid_quantity(test_db):
    """Verify non-positive allocation and release requests are rejected."""
    conn = test_db["conn"]
    for invalid_q in [0, -1, -50]:
        with pytest.raises(ValueError):
            allocate_inventory(warehouse_id="wh-01", product_id="prod-01", quantity=invalid_q, conn=conn)
        with pytest.raises(ValueError):
            release_inventory(warehouse_id="wh-01", product_id="prod-01", quantity=invalid_q, conn=conn)


# =============================================================================
# 2. Restock & Cycle Count Adjustment Tests
# =============================================================================

def test_restock_inventory_and_audit_logging(test_db):
    """Verify restocking increments on_hand, writes NoSQL audit log, and invalidates cache."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT quantity_on_hand FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    init_on_hand = cur.fetchone()["quantity_on_hand"]

    res = restock_inventory(
        warehouse_id="wh-01",
        product_id="prod-01",
        quantity=30,
        batch_no="BATCH-2026-X1",
        bin_code="WH-CHI-ZN-A-AI01-RK01-SH01-BN01",
        actor_id="usr-test-wm-01",
        conn=conn
    )

    assert res["status"] == "RESTOCKED"
    assert res["new_on_hand"] == init_on_hand + 30

    # Verify DB state
    cur.execute("SELECT quantity_on_hand FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    assert cur.fetchone()["quantity_on_hand"] == init_on_hand + 30

    # Verify NoSQL audit log
    doc_store = DocumentStore(conn=conn)
    audit_logs = doc_store.find("audit_event_logs", {"action": "INVENTORY_RESTOCK", "warehouse_id": "wh-01"})
    assert len(audit_logs) >= 1
    latest = audit_logs[-1]
    assert latest["quantity_added"] == 30
    assert latest["actor_id"] == "usr-test-wm-01"


def test_adjust_inventory_cycle_count(test_db):
    """Verify cycle count adjustments respect invariants and log audit entries."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT quantity_on_hand, quantity_reserved FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    row = cur.fetchone()
    init_on_hand = row["quantity_on_hand"]

    # Positive adjustment (found extra items)
    pos_res = adjust_inventory(warehouse_id="wh-01", product_id="prod-01", delta=5, reason="FOUND_SURPLUS", conn=conn)
    assert pos_res["new_on_hand"] == init_on_hand + 5

    # Negative adjustment (damaged goods)
    neg_res = adjust_inventory(warehouse_id="wh-01", product_id="prod-01", delta=-3, reason="DAMAGED", conn=conn)
    assert neg_res["new_on_hand"] == init_on_hand + 2

    # Negative adjustment violating active reservations
    cur.execute("SELECT quantity_on_hand, quantity_reserved FROM inventory WHERE warehouse_id = 'wh-01' AND product_id = 'prod-01'")
    curr = cur.fetchone()
    excessive_delta = -(curr["quantity_on_hand"] - curr["quantity_reserved"] + 1)

    with pytest.raises(InsufficientStockException):
        adjust_inventory(warehouse_id="wh-01", product_id="prod-01", delta=excessive_delta, reason="SCRAP", conn=conn)


def test_calculate_reorder_point_and_low_stock_filter(test_db):
    """Verify ROP = d * L + SS calculation and low stock filtering."""
    # ROP = (10 * 3) + 5 = 35
    rop = calculate_reorder_point(daily_demand=10.0, lead_time_days=3.0, safety_stock=5.0)
    assert rop == 35

    # Ceiling test: (2.5 * 3) + 1 = 8.5 -> 9
    rop_ceil = calculate_reorder_point(daily_demand=2.5, lead_time_days=3.0, safety_stock=1.0)
    assert rop_ceil == 9

    # Test low_stock_only filter
    levels = get_inventory_levels(low_stock_only=True, conn=test_db["conn"])
    assert isinstance(levels, list)
    for lvl in levels:
        assert lvl["quantity_available"] <= lvl["reorder_threshold"]
        assert lvl["is_low_stock"] is True


# =============================================================================
# 3. Cache-Aside Lifecycle in Inventory Reads
# =============================================================================

def test_inventory_cache_aside_lifecycle(test_db):
    """Verify Cache-Aside pattern: miss -> populate -> hit -> invalidate on restock."""
    cache = get_cache_manager()
    cache_key = "inventory:levels:wh-01:prod-01:0"

    # Step 1: Ensure Miss
    cache.delete(cache_key)
    assert cache.get(cache_key) is None

    # Step 2: First Read -> Miss -> DB Fetch -> Cache Populated
    data1 = get_inventory_levels(warehouse_id="wh-01", product_id="prod-01", low_stock_only=False)
    assert len(data1) == 1
    cached_data = cache.get(cache_key)
    assert cached_data is not None
    assert cached_data == data1

    # Step 3: Repeated Read -> Cache Hit
    data2 = get_inventory_levels(warehouse_id="wh-01", product_id="prod-01", low_stock_only=False)
    assert data2 == data1

    # Step 4: Restock mutation triggers cache invalidation
    restock_inventory(warehouse_id="wh-01", product_id="prod-01", quantity=10, batch_no="BATCH-CACHE-TEST")
    assert cache.get(cache_key) is None

    # Step 5: Subsequent Read fetches fresh updated data
    data3 = get_inventory_levels(warehouse_id="wh-01", product_id="prod-01", low_stock_only=False)
    assert data3[0]["quantity_on_hand"] == data1[0]["quantity_on_hand"] + 10


# =============================================================================
# 4. Transactional Order Fulfillment & Atomicity Tests
# =============================================================================

def test_create_order_atomic_success(test_db):
    """Verify successful order creation creates orders, order_items, shipments, and NoSQL snapshots."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT quantity_available, quantity_reserved FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    init_stock = cur.fetchone()

    payload_items = [{"product_id": "prod-01", "quantity": 2}]
    shipping = "456 Lake Shore Dr, Chicago, IL"

    order = create_order_atomic(
        customer_id="usr-test-customer-01",
        items=payload_items,
        shipping_address=shipping,
        destination_city="Chicago"
    )

    assert "order_id" in order
    assert order["status"] == "CONFIRMED"
    assert len(order["items"]) == 1
    assert len(order["shipments"]) == 1
    assert len(order["tracking_numbers"]) == 1

    # Verify inventory was reserved
    cur.execute("SELECT quantity_available, quantity_reserved FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    post_stock = cur.fetchone()
    assert post_stock["quantity_reserved"] == init_stock["quantity_reserved"] + 2
    assert post_stock["quantity_available"] == init_stock["quantity_available"] - 2

    # Verify NoSQL snapshot was inserted
    doc_store = DocumentStore(conn=conn)
    snapshot = doc_store.find_one("order_snapshots", {"order_id": order["order_id"]})
    assert snapshot is not None
    assert snapshot["customer_id"] == "usr-test-customer-01"


def test_create_order_atomic_multi_warehouse_split(test_db):
    """Verify order sourcing from multiple distinct warehouses creates independent shipment manifests."""
    order = create_order_atomic(
        customer_id="usr-test-customer-01",
        items=[
            {"product_id": "prod-east", "quantity": 2},
            {"product_id": "prod-west", "quantity": 3}
        ],
        shipping_address="555 Split Delivery Way, Toledo, OH",
        destination_city="Toledo"
    )

    assert order["status"] == "CONFIRMED"
    assert len(order["items"]) == 2
    # Should create 2 distinct shipments (wh-01 and wh-02)
    assert len(order["shipments"]) == 2
    origins = {s["origin_warehouse_id"] for s in order["shipments"]}
    assert "wh-01" in origins
    assert "wh-02" in origins


def test_create_order_atomic_rollback_on_stockout(test_db):
    """Verify insufficient stock on any line item rolls back all reservations and leaves zero orphan rows."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT count(*) as cnt FROM orders")
    initial_orders = cur.fetchone()["cnt"]

    cur.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    init_res = cur.fetchone()["quantity_reserved"]

    # Fault injection: prod-01 has ample stock, but prod-zero has 0 stock
    with pytest.raises(InsufficientStockException):
        create_order_atomic(
            customer_id="usr-test-customer-01",
            items=[
                {"product_id": "prod-01", "quantity": 3},
                {"product_id": "prod-zero", "quantity": 1}
            ],
            shipping_address="Rollback Lane, Chicago, IL"
        )

    # Database state verification: 100% rollback
    cur.execute("SELECT count(*) as cnt FROM orders")
    assert cur.fetchone()["cnt"] == initial_orders

    cur.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    assert cur.fetchone()["quantity_reserved"] == init_res


def test_create_order_atomic_rollback_on_payment_failure(test_db):
    """Verify payment decline aborts transaction with zero reservations and zero committed records."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT count(*) as cnt FROM orders")
    initial_orders = cur.fetchone()["cnt"]

    cur.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    init_res = cur.fetchone()["quantity_reserved"]

    with pytest.raises(PaymentFailedException):
        create_order_atomic(
            customer_id="usr-test-customer-01",
            items=[{"product_id": "prod-01", "quantity": 2}],
            shipping_address="Declined Payment Way, Chicago, IL",
            simulate_payment_failure=True
        )

    # Strict rollback verification
    cur.execute("SELECT count(*) as cnt FROM orders")
    assert cur.fetchone()["cnt"] == initial_orders

    cur.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    assert cur.fetchone()["quantity_reserved"] == init_res


def test_create_order_atomic_idempotency(test_db):
    """Verify identical orders with identical idempotency keys return existing order without duplicates."""
    idempotency_key = "IDEMP-KEY-TEST-2026-XYZ"

    order1 = create_order_atomic(
        customer_id="usr-test-customer-01",
        items=[{"product_id": "prod-01", "quantity": 1}],
        shipping_address="100 N LaSalle, Chicago, IL",
        idempotency_key=idempotency_key
    )

    order2 = create_order_atomic(
        customer_id="usr-test-customer-01",
        items=[{"product_id": "prod-01", "quantity": 1}],
        shipping_address="100 N LaSalle, Chicago, IL",
        idempotency_key=idempotency_key
    )

    assert order1["order_id"] == order2["order_id"]

    # Verify single record in DB
    conn = test_db["conn"]
    cur = conn.cursor()
    cur.execute("SELECT count(*) as cnt FROM orders WHERE id = ?", (order1["order_id"],))
    assert cur.fetchone()["cnt"] == 1


def test_order_cancellation_pre_dispatch_restores_inventory(test_db):
    """Verify cancelling an order pre-dispatch transitions status to CANCELLED and releases reserved inventory."""
    conn = test_db["conn"]
    cur = conn.cursor()

    cur.execute("SELECT quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    initial_stock = cur.fetchone()

    # Step 1: Place order
    order = create_order_atomic(
        customer_id="usr-test-customer-01",
        items=[{"product_id": "prod-01", "quantity": 4}],
        shipping_address="200 Randolph St, Chicago, IL"
    )
    order_id = order["order_id"]

    cur.execute("SELECT quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    held_stock = cur.fetchone()
    assert held_stock["quantity_reserved"] == initial_stock["quantity_reserved"] + 4

    # Step 2: Cancel order
    cancel_res = cancel_order(order_id=order_id, actor_id="usr-test-customer-01")
    assert cancel_res["status"] == "CANCELLED"

    # Step 3: Verify DB state
    cur.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
    assert cur.fetchone()["status"] == "CANCELLED"

    cur.execute("SELECT quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    released_stock = cur.fetchone()
    assert released_stock["quantity_reserved"] == initial_stock["quantity_reserved"]
    assert released_stock["quantity_available"] == initial_stock["quantity_available"]


# =============================================================================
# 5. Concurrency & Race Condition on Scarce Inventory
# =============================================================================

def test_concurrency_scarce_inventory(test_db):
    """Verify 10 concurrent threads attempting to reserve 1 unit from a 2-unit stock item: exactly 2 succeed and 8 fail."""
    conn = test_db["conn"]
    cur = conn.cursor()

    # Ensure prod-rare has exactly 2 units available
    cur.execute("UPDATE inventory SET quantity_on_hand = 2, quantity_reserved = 0 WHERE product_id = 'prod-rare' AND warehouse_id = 'wh-01'")
    conn.commit()

    successes = 0
    failures = 0

    def submit_order():
        try:
            return create_order_atomic(
                customer_id="usr-test-customer-01",
                items=[{"product_id": "prod-rare", "quantity": 1}],
                shipping_address="Concurrency Lane, Chicago, IL"
            )
        except InsufficientStockException:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(submit_order) for _ in range(10)]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res is not None:
                successes += 1
            else:
                failures += 1

    assert successes == 2, f"Expected 2 successes, got {successes}"
    assert failures == 8, f"Expected 8 failures, got {failures}"

    # Verify inventory state
    cur.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-rare' AND warehouse_id = 'wh-01'")
    final_stock = cur.fetchone()
    assert final_stock["quantity_available"] == 0
    assert final_stock["quantity_reserved"] == 2
    assert final_stock["quantity_on_hand"] >= final_stock["quantity_reserved"]
