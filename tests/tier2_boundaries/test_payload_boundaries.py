"""Tier 2.4: Boundaries - Payload and Warehouse Physical Capacity Boundaries.

Covers:
- Rejection of malformed / out-of-range telemetry coordinates (latitude > 90, longitude > 180)
- Rejection of restock operations that exceed physical warehouse storage capacity
"""

import pytest


def test_boundary_malformed_telemetry_payload(client, driver_headers):
    """Verify telemetry payload with invalid coordinates (lat > 90, lon < -180) fails schema validation."""
    malformed_payloads = [
        {
            "tracking_number": "TRK-20261109-0081",
            "device_id": "IOT-TRK-771",
            "latitude": 150.0,   # Out of bounds (> 90.0)
            "longitude": -87.63,
            "temperature_celsius": 4.0
        },
        {
            "tracking_number": "TRK-20261109-0081",
            "device_id": "IOT-TRK-771",
            "latitude": 41.87,
            "longitude": -250.0, # Out of bounds (< -180.0)
            "temperature_celsius": 4.0
        }
    ]
    
    for payload in malformed_payloads:
        res = client.post("/api/v1/telemetry", json=payload, headers=driver_headers)
        assert res.status_code in (400, 422), f"Expected 400/422 for malformed coords, got: {res.status_code}"


def test_boundary_max_warehouse_capacity_exceeded(client, manager_headers):
    """Verify attempting to restock an excessive quantity exceeding warehouse capacity returns 400 or 422."""
    massive_payload = {
        "warehouse_id": "wh-01",
        "product_id": "prod-01",
        "quantity": 10000000,  # 10 million units exceeding storage physical limit
        "batch_number": "BATCH-OVERFLOW-999"
    }
    
    res = client.post("/api/v1/inventory/restock", json=massive_payload, headers=manager_headers)
    assert res.status_code in (400, 422), f"Expected 400/422 on capacity overflow, got: {res.status_code}"
    data = res.get_json()
    assert "error" in data or "detail" in data or "message" in data
