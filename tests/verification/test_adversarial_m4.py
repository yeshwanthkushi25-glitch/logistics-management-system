"""Adversarial Security, RBAC & Boundary Verification Test Suite for Milestone 4.

Author: challenger_m4_1
Mission: Adversarially challenge:
1. Token forgery, algorithm substitution (`none`, asymmetric mismatch), expired JWTs, corrupted payloads.
2. Privilege escalation across all 5 roles (Driver, Customer, Dispatcher, WarehouseManager, Admin).
3. SQL injection and NoSQL injection attempts via API parameters and payloads.
4. Bulk batch ingestion boundaries (100+ manifests, empty batches, invalid formats).
5. Payload coordinate and inventory capacity boundary violations.
"""

import time
import base64
import json
import pytest
import jwt


# =============================================================================
# 1. JWT Security & Token Forgery Adversarial Tests
# =============================================================================

def test_adversarial_jwt_algorithm_substitution_none(client):
    """Attack: Submit JWT with alg='none' and unsigned payload attempting admin escalation."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode()).decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({
        "sub": "usr-attacker-01",
        "username": "attacker",
        "role": "Admin",
        "permissions": ["*"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }).encode()).decode().rstrip("=")
    none_token = f"{header}.{payload}."

    headers = {"Authorization": f"Bearer {none_token}", "Content-Type": "application/json"}
    res = client.get("/api/v1/admin/users", headers=headers)
    assert res.status_code == 401, f"Expected 401 Unauthorized for 'none' algorithm JWT, got {res.status_code}"


def test_adversarial_jwt_algorithm_substitution_mismatch(client):
    """Attack: Submit token with unsupported algorithm (e.g. HS512 or RS256)."""
    # Forge token using HS512 with random secret
    payload = {
        "sub": "usr-attacker-02",
        "username": "attacker",
        "role": "Admin",
        "permissions": ["*"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    hs512_token = jwt.encode(payload, "forged_secret_key_random_for_testing", algorithm="HS512")
    headers = {"Authorization": f"Bearer {hs512_token}", "Content-Type": "application/json"}

    res = client.get("/api/v1/admin/users", headers=headers)
    assert res.status_code == 401, f"Expected 401 Unauthorized for mismatched algorithm JWT, got {res.status_code}"


def test_adversarial_jwt_expired_token(client):
    """Attack: Submit expired token (exp in past)."""
    now = int(time.time())
    expired_payload = {
        "sub": "usr-expired-01",
        "username": "old_user",
        "role": "Admin",
        "permissions": ["*"],
        "iat": now - 7200,
        "exp": now - 3600  # Expired 1 hour ago
    }
    # Encode with standard secret key
    secret = "test-evaluation-secret-key-super-secure-32-bytes!"
    expired_token = jwt.encode(expired_payload, secret, algorithm="HS256")

    headers = {"Authorization": f"Bearer {expired_token}", "Content-Type": "application/json"}
    res = client.get("/api/v1/orders", headers=headers)
    assert res.status_code == 401, f"Expected 401 Unauthorized for expired JWT, got {res.status_code}"


def test_adversarial_jwt_corrupted_payloads_and_signatures(client):
    """Attack: Submit malformed, truncated, or corrupted JWT tokens."""
    malformed_tokens = [
        "not.a.valid.jwt",
        "Bearer eyJhbGciOiJIUzI1NiJ9",
        "Bearer eyJhbGciOiJIUzI1NiJ9.corrupted_payload",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxIn0.TAMPERED_SIG",
        "Bearer ",
        ""
    ]

    for bad_token in malformed_tokens:
        headers = {"Authorization": f"Bearer {bad_token}" if not bad_token.startswith("Bearer") else bad_token}
        res = client.get("/api/v1/orders", headers=headers)
        assert res.status_code == 401, f"Expected 401 Unauthorized for token '{bad_token}', got {res.status_code}"


def test_adversarial_jwt_wrong_secret_signature(client):
    """Attack: Sign a valid payload containing role Admin with an unauthorized secret key."""
    payload = {
        "sub": "usr-impostor-01",
        "username": "impostor",
        "role": "Admin",
        "permissions": ["*"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600
    }
    forged_token = jwt.encode(payload, "unauthorized-different-signing-key-32b", algorithm="HS256")
    headers = {"Authorization": f"Bearer {forged_token}", "Content-Type": "application/json"}

    res = client.get("/api/v1/admin/users", headers=headers)
    assert res.status_code == 401, f"Expected 401 Unauthorized for forged secret signature, got {res.status_code}"


# =============================================================================
# 2. Privilege Escalation Across the 5 Roles
# =============================================================================

def test_adversarial_privilege_escalation_driver(client, driver_headers):
    """Adversarial stress test: Driver attempting operations restricted to Manager, Dispatcher, Admin."""
    # 1. Driver attempting warehouse restock
    r1 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": 10}, headers=driver_headers)
    assert r1.status_code == 403, f"Driver restock exploit allowed! Got {r1.status_code}"

    # 2. Driver attempting inventory adjust
    r2 = client.post("/api/v1/inventory/adjust", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity_delta": 5}, headers=driver_headers)
    assert r2.status_code == 403, f"Driver inventory adjust exploit allowed! Got {r2.status_code}"

    # 3. Driver attempting customer order placement
    r3 = client.post("/api/v1/orders", json={"items": [{"product_id": "prod-01", "quantity": 1}], "shipping_address": "Test"}, headers=driver_headers)
    assert r3.status_code == 403, f"Driver order creation exploit allowed! Got {r3.status_code}"

    # 4. Driver attempting admin users access
    r4 = client.get("/api/v1/admin/users", headers=driver_headers)
    assert r4.status_code == 403, f"Driver admin directory exploit allowed! Got {r4.status_code}"

    # 5. Driver attempting shipment creation dispatch
    r5 = client.post("/api/v1/shipments", json={"order_id": "ord-01", "origin_warehouse_id": "wh-01"}, headers=driver_headers)
    assert r5.status_code == 403, f"Driver shipment dispatch exploit allowed! Got {r5.status_code}"

    # 6. Driver attempting network routing calculation
    r6 = client.post("/api/v1/network/routes", json={"origin_node": "CDC-CHICAGO", "destination_node": "LSF-NEWYORK"}, headers=driver_headers)
    assert r6.status_code == 403, f"Driver network route exploit allowed! Got {r6.status_code}"

    # 7. Driver attempting audit log inquiry
    r7 = client.get("/api/v1/analytics/audit/logs", headers=driver_headers)
    assert r7.status_code == 403, f"Driver audit log inquiry exploit allowed! Got {r7.status_code}"


def test_adversarial_privilege_escalation_customer(client, customer_headers):
    """Adversarial stress test: Customer attempting administrative, dispatch, or operational operations."""
    # 1. Customer attempting admin users directory
    r1 = client.get("/api/v1/admin/users", headers=customer_headers)
    assert r1.status_code == 403, f"Customer admin access exploit allowed! Got {r1.status_code}"

    # 2. Customer attempting inventory restock
    r2 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": 10}, headers=customer_headers)
    assert r2.status_code == 403, f"Customer restock exploit allowed! Got {r2.status_code}"

    # 3. Customer attempting inventory adjust
    r3 = client.post("/api/v1/inventory/adjust", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity_delta": 5}, headers=customer_headers)
    assert r3.status_code == 403, f"Customer inventory adjust exploit allowed! Got {r3.status_code}"

    # 4. Customer attempting shipment dispatch
    r4 = client.post("/api/v1/shipments", json={"order_id": "ord-01", "origin_warehouse_id": "wh-01"}, headers=customer_headers)
    assert r4.status_code == 403, f"Customer shipment dispatch exploit allowed! Got {r4.status_code}"

    # 5. Customer attempting bulk batch manifest ingestion
    r5 = client.post("/api/v1/shipments/batch", json={"shipments": [{"order_id": "ord-01"}]}, headers=customer_headers)
    assert r5.status_code == 403, f"Customer batch dispatch exploit allowed! Got {r5.status_code}"

    # 6. Customer attempting network route calculation
    r6 = client.post("/api/v1/network/routes", json={"origin_node": "CDC-CHICAGO", "destination_node": "LSF-NEWYORK"}, headers=customer_headers)
    assert r6.status_code == 403, f"Customer route calculation exploit allowed! Got {r6.status_code}"

    # 7. Customer attempting audit logs access
    r7 = client.get("/api/v1/analytics/audit/logs", headers=customer_headers)
    assert r7.status_code == 403, f"Customer audit log exploit allowed! Got {r7.status_code}"


def test_adversarial_privilege_escalation_dispatcher(client, dispatcher_headers):
    """Adversarial stress test: Dispatcher attempting warehouse restock or admin-only routes."""
    # 1. Dispatcher attempting inventory restock
    r1 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": 10}, headers=dispatcher_headers)
    assert r1.status_code == 403, f"Dispatcher restock exploit allowed! Got {r1.status_code}"

    # 2. Dispatcher attempting admin directory
    r2 = client.get("/api/v1/admin/users", headers=dispatcher_headers)
    assert r2.status_code == 403, f"Dispatcher admin access exploit allowed! Got {r2.status_code}"

    # 3. Dispatcher attempting POD upload (restricted to Admin, Driver)
    r3 = client.post("/api/v1/shipments/TRK-20261109-0081/pod", json={"recipient_name": "Bob"}, headers=dispatcher_headers)
    assert r3.status_code == 403, f"Dispatcher POD upload exploit allowed! Got {r3.status_code}"


def test_adversarial_privilege_escalation_warehouse_manager(client, manager_headers):
    """Adversarial stress test: WarehouseManager attempting shipment creation or admin-only actions."""
    # 1. Manager attempting shipment dispatch (restricted to Admin, Dispatcher)
    r1 = client.post("/api/v1/shipments", json={"order_id": "ord-01", "origin_warehouse_id": "wh-01"}, headers=manager_headers)
    assert r1.status_code == 403, f"Manager shipment dispatch exploit allowed! Got {r1.status_code}"

    # 2. Manager attempting bulk batch manifests
    r2 = client.post("/api/v1/shipments/batch", json={"shipments": [{"order_id": "ord-01"}]}, headers=manager_headers)
    assert r2.status_code == 403, f"Manager batch dispatch exploit allowed! Got {r2.status_code}"

    # 3. Manager attempting driver assignment
    r3 = client.post("/api/v1/shipments/TRK-20261109-0081/assign-driver", json={"driver_id": "usr-driver"}, headers=manager_headers)
    assert r3.status_code == 403, f"Manager driver assign exploit allowed! Got {r3.status_code}"

    # 4. Manager attempting admin users directory
    r4 = client.get("/api/v1/admin/users", headers=manager_headers)
    assert r4.status_code == 403, f"Manager admin access exploit allowed! Got {r4.status_code}"


# =============================================================================
# 3. SQL & NoSQL Injection Attacks via API
# =============================================================================

def test_adversarial_sql_injection_inventory_query_parameters(client, manager_headers):
    """Attack: SQL injection payloads in query parameters of inventory endpoints."""
    injection_payloads = [
        "wh-01' OR '1'='1",
        "wh-01'; DROP TABLE inventory; --",
        "wh-01' UNION SELECT * FROM users --",
        "' OR 1=1 --",
        'wh-01" OR ""="'
    ]

    for payload in injection_payloads:
        res = client.get(f"/api/v1/inventory?warehouse_id={payload}", headers=manager_headers)
        # Must execute cleanly via parameterization (returning 200 with 0 items), never 500 DB error
        assert res.status_code == 200, f"SQL injection in warehouse_id caused server failure: {res.status_code}"
        data = res.get_json()
        assert data["total"] == 0 or len(data["items"]) == 0, f"SQL injection payload '{payload}' dumped records!"


def test_adversarial_sql_injection_order_filtering(client, admin_headers):
    """Attack: SQL injection payloads in order listing query filters."""
    injection_filters = [
        ("customer_id", "usr-01' OR 1=1 --"),
        ("status", "CONFIRMED' OR '1'='1"),
        ("status", "'; DELETE FROM orders; --")
    ]

    for param, val in injection_filters:
        res = client.get(f"/api/v1/orders?{param}={val}", headers=admin_headers)
        assert res.status_code == 200, f"SQL injection in {param} caused server failure: {res.status_code}"
        data = res.get_json()
        assert isinstance(data.get("orders"), list)


def test_adversarial_sql_injection_login_endpoint(client):
    """Attack: Classic authentication bypass SQL injection attempts on login endpoint."""
    injection_creds = [
        {"username": "' OR '1'='1", "password": "' OR '1'='1"},
        {"username": "admin' --", "password": "anything"},
        {"username": "admin' /*", "password": "*/"},
        {"username": "' UNION SELECT * FROM users WHERE '1'='1", "password": "foo"}
    ]

    for cred in injection_creds:
        res = client.post("/api/v1/auth/login", json=cred)
        # Must strictly reject with 401 Unauthorized, never bypass authentication
        assert res.status_code == 401, f"SQL injection bypassed login! Payload: {cred}, Status: {res.status_code}"


def test_adversarial_nosql_injection_telemetry_payload(client, driver_headers):
    """Attack: NoSQL injection with complex nested operators or SQL single quotes inside JSON."""
    hostile_telemetry = {
        "tracking_number": "TRK-20261109-0081",
        "device_id": "IOT-771' OR 1=1 --",
        "latitude": 41.8790,
        "longitude": -87.6310,
        "temperature_celsius": 4.2,
        "$where": "this.device_id != null",
        "nested_attack": {
            "key' OR 1=1 --": "injected_value",
            "$gt": 0
        }
    }

    res = client.post("/api/v1/telemetry", json=hostile_telemetry, headers=driver_headers)
    # The JSON1 doc store parameterizes paths with '?' and stores data as valid JSON
    assert res.status_code == 201, f"Expected 201 for hostile JSON document stored safely, got {res.status_code}"


# =============================================================================
# 4. Bulk Batch Ingestion Boundaries & Payload Violations
# =============================================================================

def test_adversarial_bulk_batch_high_throughput_boundary(client, dispatcher_headers):
    """Boundary test: Batch manifest ingestion with 150 shipments in a single atomic transaction."""
    shipments = []
    for i in range(150):
        shipments.append({
            "order_id": f"ord-adv-bulk-{i:04d}",
            "origin_warehouse_id": "wh-01",
            "destination_city": "Chicago",
            "carrier": "APEX_FREIGHT",
            "service_level": "EXPRESS"
        })

    payload = {
        "batch_id": "BATCH-ADV-150-BOUNDARY",
        "shipments": shipments
    }

    start = time.perf_counter()
    res = client.post("/api/v1/shipments/batch", json=payload, headers=dispatcher_headers)
    elapsed = time.perf_counter() - start

    assert res.status_code in (200, 201), f"Bulk batch failed with status {res.status_code}: {res.data}"
    data = res.get_json()
    assert data["processed_count"] == 150
    assert len(data["tracking_numbers"]) == 150
    # Latency budget under 3.0 seconds
    assert elapsed < 3.0, f"Bulk batch of 150 took {elapsed:.2f}s >= 3.0s budget"


def test_adversarial_bulk_batch_malformed_payload_boundaries(client, dispatcher_headers):
    """Boundary test: Malformed bulk batch structures rejected with 400 Bad Request."""
    malformed_batches = [
        {"batch_id": "EMPTY", "shipments": []},       # Empty array
        {"batch_id": "STR", "shipments": "not_list"},  # Non-list type
        {"batch_id": "NONE"},                          # Missing 'shipments' key
        {"shipments": None}                            # Null value
    ]

    for payload in malformed_batches:
        res = client.post("/api/v1/shipments/batch", json=payload, headers=dispatcher_headers)
        assert res.status_code == 400, f"Expected 400 for malformed batch payload {payload}, got {res.status_code}"


def test_adversarial_telemetry_coordinate_boundaries(client, driver_headers):
    """Boundary test: Extreme latitude/longitude coordinate bounds rejected with 400/422."""
    boundary_payloads = [
        # Latitude out of bounds (> 90.0)
        {"tracking_number": "TRK-20261109-0081", "latitude": 90.0001, "longitude": -87.63},
        # Latitude out of bounds (< -90.0)
        {"tracking_number": "TRK-20261109-0081", "latitude": -90.0001, "longitude": -87.63},
        # Longitude out of bounds (> 180.0)
        {"tracking_number": "TRK-20261109-0081", "latitude": 41.87, "longitude": 180.0001},
        # Longitude out of bounds (< -180.0)
        {"tracking_number": "TRK-20261109-0081", "latitude": 41.87, "longitude": -180.0001},
        # Extreme float overflow
        {"tracking_number": "TRK-20261109-0081", "latitude": 1e9, "longitude": -1e9},
        # Non-numeric string coordinates
        {"tracking_number": "TRK-20261109-0081", "latitude": "invalid_latitude", "longitude": -87.63}
    ]

    for payload in boundary_payloads:
        res = client.post("/api/v1/telemetry", json=payload, headers=driver_headers)
        assert res.status_code in (400, 422), f"Expected 400/422 for coordinate boundary violation {payload}, got {res.status_code}"


def test_adversarial_inventory_capacity_boundaries(client, manager_headers):
    """Boundary test: Negative, zero, or warehouse capacity overflow restock attempts rejected."""
    # Negative quantity
    r1 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": -5}, headers=manager_headers)
    assert r1.status_code == 400, f"Expected 400 for negative restock quantity, got {r1.status_code}"

    # Zero quantity
    r2 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": 0}, headers=manager_headers)
    assert r2.status_code == 400, f"Expected 400 for zero restock quantity, got {r2.status_code}"

    # Massive overflow (e.g. 5,000,000 units)
    r3 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": 5000000}, headers=manager_headers)
    assert r3.status_code in (400, 422), f"Expected 400/422 for warehouse capacity overflow, got {r3.status_code}"
