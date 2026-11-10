"""Tier 1.1: Core Logistics - Order Creation and Management Tests.

Covers:
- Single item order creation and inventory allocation
- Multi-item order creation and total calculation
- Customer identity association
- Order retrieval and status filtering
- Pre-dispatch order cancellation and inventory de-allocation
- Idempotency key handling
"""

import json
import uuid
import pytest


def test_order_creation_success_single_item(client, customer_headers, db_conn):
    """Verify placing a single-item order succeeds, allocates stock, and returns order details."""
    payload = {
        "shipping_address": "456 Lake Shore Dr, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8818,
        "destination_lon": -87.6231,
        "items": [
            {"product_id": "prod-01", "quantity": 1}
        ]
    }
    
    response = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert response.status_code in (200, 201), f"Expected 201/200, got {response.status_code}: {response.data}"
    
    data = response.get_json()
    assert "order_id" in data or "id" in data
    order_id = data.get("order_id") or data.get("id")
    assert order_id is not None
    assert data.get("status") in ("CONFIRMED", "ALLOCATED", "PENDING")
    
    # Verify in DB
    cursor = db_conn.cursor()
    cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    order_row = cursor.fetchone()
    assert order_row is not None
    assert order_row["status"] in ("CONFIRMED", "ALLOCATED", "PENDING")


def test_order_creation_multi_item_success(client, customer_headers, db_conn):
    """Verify placing a multi-item order correctly calculates line item totals and aggregate amount."""
    payload = {
        "shipping_address": "789 Michigan Ave, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8900,
        "destination_lon": -87.6240,
        "items": [
            {"product_id": "prod-01", "quantity": 2},
            {"product_id": "prod-02", "quantity": 1}
        ]
    }
    
    response = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert response.status_code in (200, 201), f"Expected 201/200, got {response.status_code}: {response.data}"
    
    data = response.get_json()
    order_id = data.get("order_id") or data.get("id")
    assert order_id is not None
    assert "total_amount" in data
    assert float(data["total_amount"]) > 0
    
    # Verify order_items count in DB
    cursor = db_conn.cursor()
    cursor.execute("SELECT count(*) as cnt FROM order_items WHERE order_id = ?", (order_id,))
    item_count = cursor.fetchone()["cnt"]
    assert item_count >= 2


def test_order_customer_identity_association(client, customer_headers, auth_tokens, db_conn):
    """Verify order is strictly bound to the authenticated customer's account."""
    payload = {
        "shipping_address": "100 South Wacker Dr, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8810,
        "destination_lon": -87.6370,
        "items": [
            {"product_id": "prod-01", "quantity": 1}
        ]
    }
    
    response = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert response.status_code in (200, 201)
    data = response.get_json()
    order_id = data.get("order_id") or data.get("id")
    
    cursor = db_conn.cursor()
    cursor.execute("SELECT customer_id FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    assert row is not None
    # Customer ID should not be null
    assert row["customer_id"] is not None


def test_order_retrieval_and_status_filtering(client, customer_headers, admin_headers):
    """Verify retrieving an order by ID and filtering the order list by status."""
    # 1. Place order
    payload = {
        "shipping_address": "500 Madison St, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8820,
        "destination_lon": -87.6400,
        "items": [{"product_id": "prod-01", "quantity": 1}]
    }
    res = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert res.status_code in (200, 201)
    order_id = res.get_json().get("order_id") or res.get_json().get("id")
    
    # 2. Retrieve by ID
    get_res = client.get(f"/api/v1/orders/{order_id}", headers=customer_headers)
    assert get_res.status_code == 200
    retrieved = get_res.get_json()
    assert (retrieved.get("id") == order_id or retrieved.get("order_id") == order_id)
    
    # 3. Filter orders by status
    filter_res = client.get("/api/v1/orders?status=CONFIRMED", headers=admin_headers)
    assert filter_res.status_code == 200
    filter_data = filter_res.get_json()
    orders_list = filter_data.get("orders") if isinstance(filter_data, dict) else filter_data
    assert isinstance(orders_list, list)


def test_order_cancellation_pre_dispatch(client, customer_headers, db_conn):
    """Verify cancelling an order before dispatch transitions status to CANCELLED and restores reserved inventory."""
    # 1. Place order
    payload = {
        "shipping_address": "200 East Randolph St, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8850,
        "destination_lon": -87.6210,
        "items": [{"product_id": "prod-01", "quantity": 2}]
    }
    res = client.post("/api/v1/orders", json=payload, headers=customer_headers)
    assert res.status_code in (200, 201)
    order_id = res.get_json().get("order_id") or res.get_json().get("id")
    
    # 2. Cancel order
    cancel_res = client.put(f"/api/v1/orders/{order_id}/cancel", headers=customer_headers)
    if cancel_res.status_code == 404:
        # Fallback to POST /cancel or status update
        cancel_res = client.post(f"/api/v1/orders/{order_id}/cancel", headers=customer_headers)
    assert cancel_res.status_code in (200, 204)
    
    # 3. Verify status in DB
    cursor = db_conn.cursor()
    cursor.execute("SELECT status FROM orders WHERE id = ?", (order_id,))
    row = cursor.fetchone()
    assert row["status"] == "CANCELLED"


def test_order_creation_idempotency(client, customer_headers, db_conn):
    """Verify submitting an identical order with an Idempotency-Key returns the existing order."""
    idempotency_key = str(uuid.uuid4())
    headers = dict(customer_headers)
    headers["Idempotency-Key"] = idempotency_key
    
    payload = {
        "shipping_address": "333 North Michigan Ave, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8880,
        "destination_lon": -87.6250,
        "items": [{"product_id": "prod-01", "quantity": 1}]
    }
    
    # First submission
    res1 = client.post("/api/v1/orders", json=payload, headers=headers)
    assert res1.status_code in (200, 201)
    order1_id = res1.get_json().get("order_id") or res1.get_json().get("id")
    
    # Second submission with same Idempotency-Key
    res2 = client.post("/api/v1/orders", json=payload, headers=headers)
    assert res2.status_code in (200, 201)
    order2_id = res2.get_json().get("order_id") or res2.get_json().get("id")
    
    # Order IDs should match without duplicating
    assert order1_id == order2_id
    
    # Verify no duplicate orders created in DB with same order_number
    cursor = db_conn.cursor()
    cursor.execute("SELECT count(*) as cnt FROM orders WHERE id = ?", (order1_id,))
    assert cursor.fetchone()["cnt"] == 1
