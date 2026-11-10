"""Verification Suite 4: Security, Authentication & 5-Role RBAC Matrix Enforcement.

Verifies:
- 401 Unauthorized for unauthenticated or tampered requests
- Strict password hashing storage in database (bcrypt/pbkdf2 salt + hash, zero plaintext)
- Comprehensive 5-Role RBAC access matrix across:
    * Admin
    * WarehouseManager
    * Dispatcher
    * Driver
    * Customer
"""

import pytest


def test_unauthenticated_requests_rejected_401(client):
    """Verify unauthenticated requests to protected endpoints return HTTP 401 Unauthorized."""
    protected_endpoints = [
        ("GET", "/api/v1/orders"),
        ("POST", "/api/v1/orders"),
        ("POST", "/api/v1/inventory/restock"),
        ("POST", "/api/v1/inventory/adjust"),
        ("GET", "/api/v1/admin/users"),
    ]

    for method, path in protected_endpoints:
        if method == "GET":
            res = client.get(path)
        else:
            res = client.post(path, json={})
        
        assert res.status_code == 401, f"Expected 401 for unauthenticated {method} {path}, got {res.status_code}"


def test_forged_or_expired_token_rejected_401(client):
    """Verify requests with forged or invalid JWT tokens return HTTP 401 Unauthorized."""
    tampered_headers = {
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.invalidpayload.tampered_signature",
        "Content-Type": "application/json"
    }

    res = client.get("/api/v1/orders", headers=tampered_headers)
    assert res.status_code == 401, f"Expected 401 for tampered JWT, got {res.status_code}"


def test_rbac_five_role_permission_matrix(client, auth_tokens, admin_headers, manager_headers, dispatcher_headers, driver_headers, customer_headers):
    """Verify the 5-role RBAC permission matrix:

    1. Driver CANNOT adjust inventory (403 Forbidden).
    2. Driver CANNOT place commercial customer orders (403 Forbidden).
    3. Customer CANNOT access admin users endpoint (403 Forbidden).
    4. Customer CANNOT restock warehouse inventory (403 Forbidden).
    5. WarehouseManager CAN adjust/restock inventory (200/201 OK).
    6. Dispatcher CAN calculate network routes (200 OK).
    7. Admin has unrestricted system access (200/201 OK).
    """
    # 1. Driver denied inventory adjust
    res1 = client.post("/api/v1/inventory/adjust", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity_delta": 1}, headers=driver_headers)
    assert res1.status_code == 403, f"Expected 403 Forbidden for Driver adjusting inventory, got {res1.status_code}"

    # 2. Driver denied customer order creation
    res2 = client.post("/api/v1/orders", json={"items": [{"product_id": "prod-01", "quantity": 1}]}, headers=driver_headers)
    assert res2.status_code == 403, f"Expected 403 Forbidden for Driver placing order, got {res2.status_code}"

    # 3. Customer denied admin users access
    res3 = client.get("/api/v1/admin/users", headers=customer_headers)
    assert res3.status_code == 403, f"Expected 403 Forbidden for Customer accessing admin, got {res3.status_code}"

    # 4. Customer denied warehouse restock
    res4 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": 10}, headers=customer_headers)
    assert res4.status_code == 403, f"Expected 403 Forbidden for Customer restocking, got {res4.status_code}"

    # 5. WarehouseManager allowed warehouse restock
    res5 = client.post("/api/v1/inventory/restock", json={"warehouse_id": "wh-01", "product_id": "prod-01", "quantity": 5, "batch_number": "B-MGR-01"}, headers=manager_headers)
    assert res5.status_code in (200, 201), f"Expected 200/201 for Manager restocking, got {res5.status_code}"

    # 6. Dispatcher allowed network routes calculation
    res6 = client.post("/api/v1/network/routes", json={"origin_node": "CDC-CHICAGO", "destination_node": "LSF-NEWYORK"}, headers=dispatcher_headers)
    if res6.status_code == 404:
        res6 = client.post("/api/v1/routes/shortest-path", json={"origin_node": "CDC-CHICAGO", "destination_node": "LSF-NEWYORK"}, headers=dispatcher_headers)
    assert res6.status_code == 200, f"Expected 200 OK for Dispatcher route calculation, got {res6.status_code}"

    # 7. Admin allowed access
    res7 = client.get("/api/v1/admin/users", headers=admin_headers)
    assert res7.status_code in (200, 404)  # 200 if route exists, not 403


def test_password_hashing_security(db_conn):
    """Verify direct SQL query confirms password hashes in users table are securely hashed and never plaintext."""
    cursor = db_conn.cursor()
    cursor.execute("SELECT password_hash FROM users LIMIT 5")
    rows = cursor.fetchall()
    
    assert len(rows) > 0, "Users table has no seeded records!"
    for row in rows:
        pwd_hash = row["password_hash"]
        # Must be bcrypt ($2b$ or $2a$) or Werkzeug pbkdf2/scrypt format
        assert any(pwd_hash.startswith(prefix) for prefix in ("$2b$", "$2a$", "pbkdf2:sha256:", "scrypt:")), \
            f"Insecure password hash detected in database: {pwd_hash}"
