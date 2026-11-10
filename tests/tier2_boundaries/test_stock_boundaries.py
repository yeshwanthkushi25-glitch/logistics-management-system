"""Tier 2.1: Boundaries - Inventory and Stock Boundary Conditions.

Covers:
- Order rejection when available stock is exactly 0
- Payload validation failure on negative or zero order quantity
- Exact stock depletion down to 0 without underflowing
"""

import pytest


def test_boundary_zero_stock_order_rejection(client, customer_headers, db_conn):
    """Verify attempting to order a product with 0 available stock is rejected with 409 or 422."""
    # Ensure prod-zero has quantity_available = 0
    cursor = db_conn.cursor()
    cursor.execute("SELECT quantity_available FROM inventory WHERE product_id = 'prod-zero' LIMIT 1")
    row = cursor.fetchone()
    if row is None:
        # Insert a zero stock item if not seeded
        cursor.execute("""
            INSERT OR IGNORE INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved)
            VALUES ('inv-test-zero', 'wh-01', 'prod-zero', 0, 0)
        """)
        db_conn.commit()

    payload = {
        "shipping_address": "123 State St, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8800,
        "destination_lon": -87.6200,
        "items": [{"product_id": "prod-zero", "quantity": 1}]
    }

    response = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert response.status_code in (400, 409, 422), f"Expected 409/422/400 for 0 stock, got: {response.status_code}"
    data = response.get_json()
    assert "error" in data or "detail" in data or "message" in data


def test_boundary_negative_quantity_order_rejection(client, customer_headers):
    """Verify negative or zero quantities fail schema validation with HTTP 400 or 422."""
    for invalid_qty in [-5, 0]:
        payload = {
            "shipping_address": "123 State St, Chicago, IL",
            "destination_city": "Chicago",
            "destination_lat": 41.8800,
            "destination_lon": -87.6200,
            "items": [{"product_id": "prod-01", "quantity": invalid_qty}]
        }
        res = client.post("/api/v1/orders", json=payload, headers=customer_headers)
        assert res.status_code in (400, 422), f"Expected 400/422 for qty={invalid_qty}, got: {res.status_code}"


def test_boundary_exact_stock_depletion_to_zero(client, customer_headers, db_conn):
    """Verify ordering the exact remaining stock units reduces available stock to exactly 0 without negative underflow."""
    cursor = db_conn.cursor()
    # Find a product with known stock
    cursor.execute("SELECT product_id, quantity_available FROM inventory WHERE quantity_available > 0 LIMIT 1")
    row = cursor.fetchone()
    assert row is not None
    prod_id = row["product_id"]
    available_qty = row["quantity_available"]

    payload = {
        "shipping_address": "999 Exact Depletion Blvd, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8800,
        "destination_lon": -87.6200,
        "items": [{"product_id": prod_id, "quantity": available_qty}]
    }

    res = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert res.status_code in (200, 201), f"Exact order failed: {res.data}"

    # Verify inventory in DB: quantity_available must be exactly 0, never negative
    cursor.execute("SELECT quantity_available FROM inventory WHERE product_id = ?", (prod_id,))
    updated_row = cursor.fetchone()
    assert updated_row["quantity_available"] == 0
    assert updated_row["quantity_available"] >= 0
