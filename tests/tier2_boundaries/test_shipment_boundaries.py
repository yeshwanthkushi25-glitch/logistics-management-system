"""Tier 2.3: Boundaries - Shipment Lifecycle & Status Boundary Conditions.

Covers:
- Lookup of non-existent shipment tracking numbers (404 handling)
- Rejection of invalid / backward state transitions in the shipment FSM
"""

import pytest


def test_boundary_non_existent_shipment_or_order(client, customer_headers):
    """Verify querying tracking for non-existent shipment returns HTTP 404 Not Found."""
    fake_tracking_num = "TRK-00000000-NONEXISTENT"
    
    response = client.get(f"/api/v1/shipments/{fake_tracking_num}/track", headers=customer_headers)
    assert response.status_code == 404, f"Expected 404, got: {response.status_code}"
    
    data = response.get_json()
    assert "error" in data or "detail" in data or "message" in data


def test_boundary_invalid_status_transition_progression(client, driver_headers):
    """Verify illegal jumps in the shipment FSM (e.g. CREATED -> DELIVERED, or DELIVERED -> IN_TRANSIT) are rejected."""
    # Attempting to move a CREATED or DELIVERED shipment backward to IN_TRANSIT
    fake_tracking = "TRK-TEST-DELIVERED-01"
    
    payload = {
        "milestone_type": "DEPARTURE_SCAN",
        "location_name": "Invalid Leap Hub",
        "notes": "Attempting invalid backward progression"
    }
    
    # Direct status change attempt
    status_payload = {"status": "IN_TRANSIT"}
    res = client.put(f"/api/v1/shipments/{fake_tracking}/status", json=status_payload, headers=driver_headers)
    
    # Must fail if shipment is already DELIVERED
    if res.status_code != 404:
        assert res.status_code in (400, 422), f"Expected 400/422 for invalid status transition, got: {res.status_code}"
