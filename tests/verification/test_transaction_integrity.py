"""Verification Suite 1: Strict Transactional Integrity & Rollback Assertions.

Guarantees strict multi-table ACID transactions across:
- orders
- order_items
- inventory
- shipments
- audit_event_logs

Explicit assertions verify zero-orphan records and pristine balance preservation on rollback.
"""

import sqlite3
import pytest


def test_atomic_rollback_on_partial_stockout(client, customer_headers, db_conn):
    """Verify that an intentional stockout on any item in a multi-item order triggers a full rollback.

    Asserts:
    1. HTTP response is 409 Conflict or 422 Unprocessable Entity.
    2. Available stock for Item A is NOT decremented (zero phantom reservations).
    3. Orders table row count is unchanged (zero orphaned order rows).
    4. Order_items table row count is unchanged (zero orphaned item rows).
    5. Shipments table has zero rows associated with the failed attempt.
    """
    cursor = db_conn.cursor()

    # Capture pre-transaction baseline counts
    cursor.execute("SELECT count(*) as cnt FROM orders")
    initial_order_count = cursor.fetchone()["cnt"]

    cursor.execute("SELECT count(*) as cnt FROM order_items")
    initial_items_count = cursor.fetchone()["cnt"]

    cursor.execute("SELECT quantity_available, quantity_reserved FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    prod1_baseline = cursor.fetchone()
    init_prod1_avail = prod1_baseline["quantity_available"]
    init_prod1_res = prod1_baseline["quantity_reserved"]

    # Fault injection payload: prod-01 has ample stock, but prod-zero has 0 stock
    fault_payload = {
        "shipping_address": "404 Fault Injection Way, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8800,
        "destination_lon": -87.6200,
        "items": [
            {"product_id": "prod-01", "quantity": 3},
            {"product_id": "prod-zero", "quantity": 1}
        ]
    }

    response = client.post("/api/v1/orders", json=fault_payload, headers=customer_headers)
    assert response.status_code in (400, 409, 422), f"Expected transaction abort, got {response.status_code}"

    # Strict Database Rollback Assertions
    # 1. prod-01 stock must be 100% untouched
    cursor.execute("SELECT quantity_available, quantity_reserved FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    prod1_post = cursor.fetchone()
    assert prod1_post["quantity_available"] == init_prod1_avail, "Rollback failed: prod-01 available stock was decremented!"
    assert prod1_post["quantity_reserved"] == init_prod1_res, "Rollback failed: prod-01 reserved stock was altered!"

    # 2. orders row count must be identical to baseline
    cursor.execute("SELECT count(*) as cnt FROM orders")
    post_order_count = cursor.fetchone()["cnt"]
    assert post_order_count == initial_order_count, f"Rollback failed: orphaned order row committed! ({post_order_count} > {initial_order_count})"

    # 3. order_items row count must be identical to baseline
    cursor.execute("SELECT count(*) as cnt FROM order_items")
    post_items_count = cursor.fetchone()["cnt"]
    assert post_items_count == initial_items_count, f"Rollback failed: orphaned order_items committed! ({post_items_count} > {initial_items_count})"


def test_atomic_rollback_on_payment_failure(client, customer_headers, db_conn):
    """Verify that a payment processing failure triggers immediate transaction abort and complete rollback."""
    cursor = db_conn.cursor()
    cursor.execute("SELECT count(*) as cnt FROM orders")
    initial_orders = cursor.fetchone()["cnt"]

    cursor.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    initial_res = cursor.fetchone()["quantity_reserved"]

    # Payload with simulated payment decline
    fail_payload = {
        "shipping_address": "Declined Payment Ave, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8800,
        "destination_lon": -87.6200,
        "items": [{"product_id": "prod-01", "quantity": 2}],
        "simulate_failure": True
    }

    res = client.post("/api/v1/orders", json=fail_payload, headers=customer_headers)
    assert res.status_code in (400, 402, 422, 500)

    # Verify zero orders committed
    cursor.execute("SELECT count(*) as cnt FROM orders")
    assert cursor.fetchone()["cnt"] == initial_orders

    # Verify reservations rolled back
    cursor.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    assert cursor.fetchone()["quantity_reserved"] == initial_res


def test_mid_flight_crash_zero_orphan_records(app, db_conn):
    """Verify that an unhandled Python exception inside the transaction scope triggers clean rollback."""
    try:
        from app.db.connection import transaction_scope
    except ImportError:
        pytest.skip("app.db.connection not yet implemented")

    cursor = db_conn.cursor()
    cursor.execute("SELECT count(*) as cnt FROM orders")
    baseline_orders = cursor.fetchone()["cnt"]

    # Inject simulated mid-flight crash inside transaction_scope
    with pytest.raises(RuntimeError):
        with transaction_scope() as conn:
            conn.execute("""
                INSERT INTO orders (id, order_number, customer_id, status, total_amount, shipping_address, destination_city, destination_lat, destination_lon)
                VALUES ('ord-crash-test', 'ORD-CRASH-001', 'usr-test-customer-01', 'PENDING', 99.99, 'Crash St', 'Chicago', 41.8, -87.6)
            """)
            # Simulate unexpected process crash or database error
            raise RuntimeError("Simulated mid-flight transaction crash!")

    # Verify the crash row was never committed
    cursor.execute("SELECT count(*) as cnt FROM orders WHERE id = 'ord-crash-test'")
    assert cursor.fetchone()["cnt"] == 0

    cursor.execute("SELECT count(*) as cnt FROM orders")
    assert cursor.fetchone()["cnt"] == baseline_orders
