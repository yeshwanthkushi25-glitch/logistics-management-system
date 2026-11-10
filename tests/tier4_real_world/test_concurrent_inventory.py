"""Tier 4.1: Real-World - Concurrency and Race Condition Testing on Scarce Inventory.

Covers:
- High-concurrency race condition: 10 concurrent threads attempting to reserve 1 unit each when only 2 units exist.
- Assertion that exactly 2 succeed and 8 fail cleanly with HTTP 409 / 422.
- Invariant assertion: Available inventory depletes to exactly 0 with zero negative balances or lost updates.
"""

import concurrent.futures
import pytest


def test_concurrency_scarce_inventory_race_condition(client, customer_headers, db_conn):
    """Verify concurrent orders for scarce inventory prevent overselling and maintain strict invariants."""
    # 1. Setup product with stock = exactly 2 units
    cursor = db_conn.cursor()
    cursor.execute("DELETE FROM inventory WHERE product_id = 'prod-rare'")
    cursor.execute("""
        INSERT OR REPLACE INTO products (id, sku, name, unit_price, weight_kg, category)
        VALUES ('prod-rare', 'SKU-RARE-01', 'Rare Component', 199.99, 1.0, 'Critical')
    """)
    cursor.execute("""
        INSERT OR REPLACE INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved)
        VALUES ('inv-rare-01', 'wh-01', 'prod-rare', 2, 0)
    """)
    db_conn.commit()

    order_payload = {
        "shipping_address": "Race Condition Lane, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8800,
        "destination_lon": -87.6200,
        "items": [{"product_id": "prod-rare", "quantity": 1}]
    }

    # 2. Worker function for concurrent submission
    def submit_order():
        return client.post("/api/v1/orders", json=order_payload, headers=customer_headers)

    # 3. Spawn 10 concurrent threads
    successes = 0
    failures = 0
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(submit_order) for _ in range(10)]
        for f in concurrent.futures.as_completed(futures):
            res = f.result()
            if res.status_code in (200, 201):
                successes += 1
            elif res.status_code in (400, 409, 422):
                failures += 1

    # 4. Assert exactly 2 succeeded and 8 failed
    assert successes == 2, f"Expected exactly 2 successes, got {successes}"
    assert failures == 8, f"Expected exactly 8 failures, got {failures}"

    # 5. Invariant check: Final available stock must be exactly 0
    cursor.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-rare'")
    row = cursor.fetchone()
    assert row["quantity_available"] == 0
    assert row["quantity_reserved"] == 2
    assert row["quantity_on_hand"] >= row["quantity_reserved"]
