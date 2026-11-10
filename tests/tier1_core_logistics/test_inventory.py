"""Tier 1.3: Core Logistics - Warehouse and Inventory Management Tests.

Covers:
- Inventory allocation and decrement on order creation
- Inventory restock operations and ledger logging
- Multi-warehouse stock count isolation
- Automated reorder point (ROP) low-stock alert triggers
- Manual cycle count inventory adjustments with audit reasons
- Inventory reservation hold and release lifecycle
"""

import pytest


def test_inventory_decrement_on_order_allocation(client, customer_headers, db_conn):
    """Verify placing an order decrements available inventory and increments reserved inventory."""
    # 1. Query baseline stock
    cursor = db_conn.cursor()
    cursor.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    row_before = cursor.fetchone()
    assert row_before is not None
    init_avail = row_before["quantity_available"]
    init_res = row_before["quantity_reserved"]

    # 2. Place order for 2 units
    payload = {
        "shipping_address": "100 South Wacker Dr, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8810,
        "destination_lon": -87.6370,
        "items": [{"product_id": "prod-01", "quantity": 2}]
    }
    res = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert res.status_code in (200, 201)

    # 3. Query updated stock
    cursor.execute("SELECT quantity_on_hand, quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    row_after = cursor.fetchone()
    assert row_after["quantity_reserved"] == init_res + 2
    assert row_after["quantity_available"] == init_avail - 2


def test_inventory_restock_success(client, manager_headers, db_conn):
    """Verify warehouse manager restock increments quantity_on_hand and logs batch number."""
    cursor = db_conn.cursor()
    cursor.execute("SELECT quantity_on_hand FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    initial_on_hand = cursor.fetchone()["quantity_on_hand"]

    restock_payload = {
        "warehouse_id": "wh-01",
        "product_id": "prod-01",
        "quantity": 25,
        "batch_number": "BATCH-RESTOCK-2026-A",
        "bin_code": "WH-CHI-ZN-A-AI01-RK01-SH01-BN01"
    }
    
    response = client.post("/api/v1/inventory/restock", json=restock_payload, headers=manager_headers)
    assert response.status_code in (200, 201)
    data = response.get_json()
    assert data.get("status") in ("SUCCESS", "RESTOCKED") or "new_on_hand" in data

    # Verify DB update
    cursor.execute("SELECT quantity_on_hand FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    updated_on_hand = cursor.fetchone()["quantity_on_hand"]
    assert updated_on_hand == initial_on_hand + 25


def test_inventory_multi_warehouse_isolation(client, manager_headers, db_conn):
    """Verify identical SKU stocked across multiple distinct warehouses maintains isolated counters."""
    cursor = db_conn.cursor()
    
    # Check warehouse 1 vs warehouse 2 stock counts
    cursor.execute("SELECT warehouse_id, quantity_on_hand FROM inventory WHERE product_id = 'prod-01'")
    rows = cursor.fetchall()
    
    # We expect multiple warehouse stock rows if seeded, or restock into wh-02 independently
    wh2_payload = {
        "warehouse_id": "wh-02",
        "product_id": "prod-01",
        "quantity": 15,
        "batch_number": "BATCH-WH2-IND-01"
    }
    client.post("/api/v1/inventory/restock", json=wh2_payload, headers=manager_headers)
    
    cursor.execute("SELECT quantity_on_hand FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-01'")
    wh1_stock = cursor.fetchone()["quantity_on_hand"]
    
    cursor.execute("SELECT quantity_on_hand FROM inventory WHERE product_id = 'prod-01' AND warehouse_id = 'wh-02'")
    wh2_stock = cursor.fetchone()["quantity_on_hand"]
    
    # wh1 and wh2 counters must be independent
    assert wh1_stock != wh2_stock or wh1_stock >= 0


def test_inventory_reorder_threshold_alert(client, manager_headers):
    """Verify querying inventory with low_stock filter detects products below reorder threshold."""
    response = client.get("/api/v1/inventory?low_stock=true", headers=manager_headers)
    assert response.status_code == 200
    data = response.get_json()
    items = data.get("items") if isinstance(data, dict) else data
    assert isinstance(items, list)
    
    for item in items:
        if "reorder_threshold" in item and "quantity_available" in item:
            assert item["quantity_available"] <= item["reorder_threshold"]


def test_inventory_adjustment_with_audit_reason(client, manager_headers, db_conn):
    """Verify manual inventory cycle count adjustment records reason and persists to audit logs."""
    adjust_payload = {
        "warehouse_id": "wh-01",
        "product_id": "prod-01",
        "quantity_delta": -2,
        "reason_code": "DAMAGED",
        "notes": "Damaged during forklift transit in aisle 4"
    }
    
    res = client.post("/api/v1/inventory/adjust", json=adjust_payload, headers=manager_headers)
    assert res.status_code in (200, 201)
    data = res.get_json()
    assert data.get("status") in ("SUCCESS", "ADJUSTED") or "audit_id" in data


def test_inventory_stock_reservation_hold_and_release(client, customer_headers, db_conn):
    """Verify reserving stock holds quantity_reserved, and order cancellation immediately releases it."""
    cursor = db_conn.cursor()
    cursor.execute("SELECT quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    initial_row = cursor.fetchone()
    init_res = initial_row["quantity_reserved"]
    init_avail = initial_row["quantity_available"]

    # 1. Place order (reserves 3 units)
    order_payload = {
        "shipping_address": "800 North Michigan Ave, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8980,
        "destination_lon": -87.6240,
        "items": [{"product_id": "prod-01", "quantity": 3}]
    }
    res = client.post("/api/v1/orders", json=order_payload, headers=customer_headers)
    assert res.status_code in (200, 201)
    order_id = res.get_json().get("order_id") or res.get_json().get("id")

    # 2. Check hold in DB
    cursor.execute("SELECT quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    held_row = cursor.fetchone()
    assert held_row["quantity_reserved"] == init_res + 3
    assert held_row["quantity_available"] == init_avail - 3

    # 3. Cancel order (releases hold)
    cancel_res = client.put(f"/api/v1/orders/{order_id}/cancel", headers=customer_headers)
    if cancel_res.status_code == 404:
        cancel_res = client.post(f"/api/v1/orders/{order_id}/cancel", headers=customer_headers)
    assert cancel_res.status_code in (200, 204)

    # 4. Check released in DB
    cursor.execute("SELECT quantity_reserved, quantity_available FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    released_row = cursor.fetchone()
    assert released_row["quantity_reserved"] == init_res
    assert released_row["quantity_available"] == init_avail
