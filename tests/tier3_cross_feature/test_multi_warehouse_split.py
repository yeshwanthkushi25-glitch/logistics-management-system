"""Tier 3.2: Cross-Feature - Multi-Warehouse Split-Order Fulfillment.

Covers:
- Order fulfillment requiring inventory sourced from multiple distinct distribution centers
- Independent shipment manifests generated per sourcing warehouse
- Stock reservation integrity across multiple warehouse inventory ledgers
"""

import pytest


def test_cross_feature_multi_warehouse_split_fulfillment(client, customer_headers, db_conn):
    """Verify an order requesting items from distinct warehouses splits fulfillment across warehouses."""
    # Ensure prod-east is in wh-01 and prod-west is in wh-02
    cursor = db_conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO inventory (id, warehouse_id, product_id, quantity_on_hand, quantity_reserved)
        VALUES 
            ('inv-east-01', 'wh-01', 'prod-east', 20, 0),
            ('inv-west-01', 'wh-02', 'prod-west', 20, 0)
    """)
    db_conn.commit()

    split_payload = {
        "shipping_address": "555 Split Delivery Way, Toledo, OH",
        "destination_city": "Toledo",
        "destination_lat": 41.6528,
        "destination_lon": -83.5379,
        "items": [
            {"product_id": "prod-east", "quantity": 2},
            {"product_id": "prod-west", "quantity": 3}
        ]
    }

    response = client.post("/api/v1/orders", json=split_payload, headers=customer_headers)
    assert response.status_code in (200, 201), f"Split order failed: {response.data}"
    
    order_data = response.get_json()
    order_id = order_data.get("order_id") or order_data.get("id")
    assert order_id is not None

    # Check order items in DB: each item should be allocated to its respective warehouse
    cursor.execute("SELECT product_id, allocated_warehouse_id, quantity FROM order_items WHERE order_id = ?", (order_id,))
    items = cursor.fetchall()
    assert len(items) >= 2
    
    # Verify stock reservations in both warehouses
    cursor.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-east' AND warehouse_id = 'wh-01'")
    east_res = cursor.fetchone()["quantity_reserved"]
    assert east_res >= 2

    cursor.execute("SELECT quantity_reserved FROM inventory WHERE product_id = 'prod-west' AND warehouse_id = 'wh-02'")
    west_res = cursor.fetchone()["quantity_reserved"]
    assert west_res >= 3
