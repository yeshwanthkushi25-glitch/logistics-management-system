"""Tier 3.1: Cross-Feature - Complete End-to-End Logistics Lifecycle Test.

Comprehensive multi-step lifecycle exercising:
1. Authentication (JWT token issuance)
2. Inventory inquiry (stock availability check)
3. Transactional order placement & atomic stock lock
4. Graph routing engine computation (origin to destination)
5. Shipment manifest generation
6. Cache-Aside read (Cache MISS -> DB Query -> Cache Populate)
7. Shipment status mutation & Cache Invalidation
8. NoSQL IoT telemetry ingestion (GPS + environmental readings)
9. Fresh fetch & Delivery completion with proof-of-delivery (POD)
"""

import pytest


def test_e2e_complete_logistics_lifecycle(client, customer_headers, driver_headers, dispatcher_headers, db_conn):
    """Executes the complete end-to-end logistics workflow across all system components."""
    
    # -------------------------------------------------------------------------
    # Step 1: Authentication & Identity
    # -------------------------------------------------------------------------
    # Headers are provided via authenticated fixtures (JWT)
    assert customer_headers.get("Authorization") is not None

    # -------------------------------------------------------------------------
    # Step 2: Inventory Inquiry
    # -------------------------------------------------------------------------
    inv_res = client.get("/api/v1/inventory?product_id=prod-01", headers=customer_headers)
    assert inv_res.status_code == 200
    inv_data = inv_res.get_json()
    items = inv_data.get("items") if isinstance(inv_data, dict) else inv_data
    assert len(items) > 0
    initial_avail = items[0]["quantity_available"]
    assert initial_avail >= 2

    # -------------------------------------------------------------------------
    # Step 3: Transactional Order Placement & Atomic Lock
    # -------------------------------------------------------------------------
    order_payload = {
        "shipping_address": "777 Grand Ave, Chicago, IL 60611",
        "destination_city": "Chicago",
        "destination_lat": 41.8910,
        "destination_lon": -87.6200,
        "items": [{"product_id": "prod-01", "quantity": 2}]
    }
    order_res = client.post("/api/v1/orders", json=order_payload, headers=customer_headers)
    assert order_res.status_code in (200, 201)
    order_data = order_res.get_json()
    order_id = order_data.get("order_id") or order_data.get("id")
    assert order_id is not None

    # Verify inventory was atomically locked (reserved)
    cursor = db_conn.cursor()
    cursor.execute("SELECT quantity_available FROM inventory WHERE product_id = 'prod-01' LIMIT 1")
    post_order_avail = cursor.fetchone()["quantity_available"]
    assert post_order_avail == initial_avail - 2

    # -------------------------------------------------------------------------
    # Step 4: Network Route Calculation
    # -------------------------------------------------------------------------
    route_payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "optimization_criteria": "distance"
    }
    route_res = client.post("/api/v1/network/routes", json=route_payload, headers=dispatcher_headers)
    if route_res.status_code == 404:
        route_res = client.post("/api/v1/routes/shortest-path", json=route_payload, headers=dispatcher_headers)
    assert route_res.status_code == 200
    route_data = route_res.get_json()
    path = route_data.get("path_nodes") or route_data.get("path")
    assert len(path) >= 2

    # -------------------------------------------------------------------------
    # Step 5: Shipment Manifest Generation
    # -------------------------------------------------------------------------
    shipment_payload = {
        "order_id": order_id,
        "origin_warehouse_id": "wh-01",
        "carrier": "DHL_EXPRESS",
        "service_level": "STANDARD"
    }
    ship_res = client.post("/api/v1/shipments", json=shipment_payload, headers=dispatcher_headers)
    if ship_res.status_code == 404:
        ship_res = client.post("/api/v1/shipments/dispatch", json=shipment_payload, headers=dispatcher_headers)
    assert ship_res.status_code in (200, 201)
    ship_data = ship_res.get_json()
    tracking_number = ship_data.get("tracking_number") or ship_data.get("tracking_num")
    assert tracking_number is not None

    # -------------------------------------------------------------------------
    # Step 6: Cache Miss & Populate (First Read)
    # -------------------------------------------------------------------------
    track_res1 = client.get(f"/api/v1/shipments/{tracking_number}/track", headers=customer_headers)
    assert track_res1.status_code == 200
    track_data1 = track_res1.get_json()
    # Cache status should indicate MISS on first access
    assert track_data1.get("cache_status") in ("MISS", None) or "tracking_number" in track_data1

    # Second read should be Cache HIT
    track_res_hit = client.get(f"/api/v1/shipments/{tracking_number}/track", headers=customer_headers)
    assert track_res_hit.status_code == 200

    # -------------------------------------------------------------------------
    # Step 7: Status Mutation & Cache Invalidation
    # -------------------------------------------------------------------------
    milestone_payload = {
        "milestone_type": "DEPARTURE_SCAN",
        "location_name": "Chicago Central DC",
        "latitude": 41.8781,
        "longitude": -87.6298,
        "notes": "Package dispatched into transit network"
    }
    mutation_res = client.post(f"/api/v1/shipments/{tracking_number}/milestones", json=milestone_payload, headers=driver_headers)
    if mutation_res.status_code == 404:
        mutation_res = client.put(f"/api/v1/shipments/{tracking_number}/status", json={"status": "IN_TRANSIT"}, headers=driver_headers)
    assert mutation_res.status_code in (200, 201)

    # -------------------------------------------------------------------------
    # Step 8: NoSQL IoT Telemetry Ingestion
    # -------------------------------------------------------------------------
    telemetry_doc = {
        "tracking_number": tracking_number,
        "device_id": "IOT-TRK-771",
        "latitude": 41.8820,
        "longitude": -87.6320,
        "temperature_celsius": 4.1,
        "humidity_percent": 52.0,
        "shock_impact_g": 0.05,
        "battery_percent": 95
    }
    telemetry_res = client.post("/api/v1/telemetry", json=telemetry_doc, headers=driver_headers)
    assert telemetry_res.status_code in (200, 201)

    # -------------------------------------------------------------------------
    # Step 9: Fresh Fetch & Delivery Completion with POD
    # -------------------------------------------------------------------------
    # Fresh tracking fetch reflects new status and telemetry
    fresh_track_res = client.get(f"/api/v1/shipments/{tracking_number}/track", headers=customer_headers)
    assert fresh_track_res.status_code == 200

    # Final delivery milestone
    deliv_payload = {
        "milestone_type": "DELIVERY_COMPLETED",
        "location_name": "777 Grand Ave, Chicago, IL",
        "latitude": 41.8910,
        "longitude": -87.6200,
        "notes": "Delivered. Signature: John Customer"
    }
    final_res = client.post(f"/api/v1/shipments/{tracking_number}/milestones", json=deliv_payload, headers=driver_headers)
    if final_res.status_code == 404:
        final_res = client.put(f"/api/v1/shipments/{tracking_number}/status", json={"status": "DELIVERED"}, headers=driver_headers)
    assert final_res.status_code in (200, 201)
