"""
Logistics IMS - Test Harness for Security & RBAC Layer.

Verifies:
1. PBKDF2 HMAC-SHA256 password hashing with 100,000+ iterations and timing-safe verification.
2. Stateless HMAC-SHA256 JWT generation, decoding, expiry, and tampering rejection.
3. Database integration: user authentication, lookup, and registration against SQLite schema.
4. RBAC 5-role permission matrix with domain wildcard matching.
5. Flask decorators (@require_auth, @require_role, @require_permission) with Bearer token extraction,
   JSON 401 Unauthorized / 403 Forbidden status codes, and request context holder (flask.g).
"""

import os
import sys
import time
import json
import sqlite3
import tempfile
import pytest
from flask import Flask, jsonify, g

from app.auth.security import (
    hash_password,
    verify_password,
    generate_token,
    decode_token,
    verify_token,
    authenticate_user,
    register_user,
    get_user_by_id,
    get_user_by_username,
    AuthenticationError,
    InvalidTokenError,
    TokenExpiredError,
    DEFAULT_PBKDF2_ITERATIONS,
)
from app.auth.rbac import (
    ROLE_ADMIN,
    ROLE_WAREHOUSE_MANAGER,
    ROLE_DISPATCHER,
    ROLE_DRIVER,
    ROLE_CUSTOMER,
    ALL_ROLES,
    ROLE_PERMISSIONS_MATRIX,
    permission_matches,
    get_effective_permissions,
    check_user_has_permission,
    extract_bearer_token,
    require_auth,
    require_role,
    require_permission,
    get_current_user,
    get_current_role,
)
from app.db.init_db import init_database
from app.db.connection import get_db_connection, set_default_db_path, close_all_connections


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def test_db():
    """Initializes a fresh isolated SQLite database with schema and demo users."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_auth_rbac_")
    os.close(fd)
    set_default_db_path(path)

    conn = init_database(db_path=path, seed_demo_data=True, drop_existing=True)
    yield conn, path

    close_all_connections()
    for ext in ["", "-wal", "-shm"]:
        p = path + ext
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


@pytest.fixture
def auth_flask_app():
    """Builds a test Flask application with protected endpoints."""
    app = Flask("test_auth_app")
    app.config["TESTING"] = True
    app.config["JWT_SECRET"] = "super-secret-evaluation-key-for-testing-at-least-32b"
    app.config["JWT_EXPIRATION_SECONDS"] = 3600

    @app.route("/api/v1/protected/auth-only", methods=["GET"])
    @require_auth
    def auth_only():
        return jsonify({
            "status": "ok",
            "user": get_current_user()["username"],
            "role": get_current_role()
        })

    @app.route("/api/v1/orders", methods=["GET", "POST"])
    @require_role(ROLE_CUSTOMER, ROLE_ADMIN)
    def orders_endpoint():
        return jsonify({"status": "orders_success", "user": g.current_user["username"]}), 201

    @app.route("/api/v1/inventory/restock", methods=["POST"])
    @require_role(ROLE_WAREHOUSE_MANAGER, ROLE_ADMIN)
    def restock_endpoint():
        return jsonify({"status": "restock_success", "user": g.current_user["username"]}), 200

    @app.route("/api/v1/inventory/adjust", methods=["POST"])
    @require_permission("inventory:adjust")
    def adjust_endpoint():
        return jsonify({"status": "adjust_success", "user": g.current_user["username"]}), 200

    @app.route("/api/v1/network/routes", methods=["POST"])
    @require_role(ROLE_DISPATCHER, ROLE_ADMIN)
    def routes_endpoint():
        return jsonify({"status": "routes_success"}), 200

    @app.route("/api/v1/admin/users", methods=["GET"])
    @require_role(ROLE_ADMIN)
    def admin_users_endpoint():
        return jsonify({"status": "admin_access_granted"}), 200

    @app.route("/api/v1/shipments/status", methods=["POST"])
    @require_permission("shipments:update_status")
    def shipment_status_endpoint():
        return jsonify({"status": "status_updated"}), 200

    return app


@pytest.fixture
def test_client(auth_flask_app):
    return auth_flask_app.test_client()


# =============================================================================
# 1. Password Hashing & Verification Tests
# =============================================================================

def test_hash_password_format_and_iterations():
    """Verify password hashing generates secure PBKDF2 HMAC-SHA256 hash with >= 100,000 iterations."""
    pwd = "SecureLogisticsPassword123!"
    h1 = hash_password(pwd)
    
    assert h1.startswith("pbkdf2:sha256:100000$"), f"Expected PBKDF2 with 100000 iterations, got: {h1}"
    
    # Test random salt: two hashes of the same password MUST be distinct
    h2 = hash_password(pwd)
    assert h1 != h2, "Password hash must use a unique random salt per invocation!"

    # Empty password rejection
    with pytest.raises(ValueError):
        hash_password("")


def test_verify_password_timing_safe():
    """Verify password matching and rejection with timing-safe comparison."""
    pwd = "ValidPassword456$"
    hashed = hash_password(pwd)

    # Correct password succeeds
    assert verify_password(pwd, hashed) is True

    # Incorrect passwords fail
    assert verify_password("WrongPassword", hashed) is False
    assert verify_password("validpassword456$", hashed) is False  # Case sensitive
    assert verify_password("", hashed) is False
    assert verify_password(pwd, "") is False
    assert verify_password(pwd, "invalid_hash_format") is False


# =============================================================================
# 2. JWT Generation & Validation Tests
# =============================================================================

def test_generate_and_decode_token():
    """Verify JWT encodes all required claims and decodes cleanly."""
    payload = {
        "sub": "usr_test_123",
        "username": "tester",
        "role": ROLE_WAREHOUSE_MANAGER,
        "permissions": ["inventory:read", "inventory:restock"]
    }
    secret = "test-secret-at-least-32-bytes-long-for-hmac-sha256"
    token = generate_token(payload, secret=secret, expires_in=1800)
    
    assert isinstance(token, str)
    assert len(token.split(".")) == 3

    decoded = decode_token(token, secret=secret)
    assert decoded["sub"] == "usr_test_123"
    assert decoded["user_id"] == "usr_test_123"
    assert decoded["username"] == "tester"
    assert decoded["role"] == ROLE_WAREHOUSE_MANAGER
    assert "inventory:restock" in decoded["permissions"]
    assert decoded["exp"] > decoded["iat"]


def test_token_expiration_rejection():
    """Verify expired token raises TokenExpiredError."""
    secret = "test-secret-at-least-32-bytes-long-for-hmac-sha256"
    now = int(time.time())
    payload = {
        "sub": "usr_expired",
        "username": "expired_user",
        "role": ROLE_CUSTOMER,
        "iat": now - 3600,
        "exp": now - 60  # expired 1 minute ago
    }
    expired_token = generate_token(payload, secret=secret, expires_in=-60)
    
    with pytest.raises(TokenExpiredError):
        decode_token(expired_token, secret=secret)


def test_token_tampering_rejection():
    """Verify tampered token signature or payload raises InvalidTokenError."""
    secret = "test-secret-at-least-32-bytes-long-for-hmac-sha256"
    token = generate_token({"sub": "usr_1", "role": ROLE_ADMIN}, secret=secret)
    
    parts = token.split(".")
    tampered_token = f"{parts[0]}.{parts[1]}.bad_signature_here"

    with pytest.raises(InvalidTokenError):
        decode_token(tampered_token, secret=secret)

    # Wrong secret key rejection
    with pytest.raises(InvalidTokenError):
        decode_token(token, secret="completely-different-secret-key-32b-length")


# =============================================================================
# 3. Database Integration & User Management Tests
# =============================================================================

def test_authenticate_user_with_seeded_db(test_db):
    """Verify database authentication with seeded users and credentials."""
    conn, _ = test_db

    # 1. Admin login with Password123!
    admin_user = authenticate_user("admin", "Password123!", db_conn=conn)
    assert admin_user is not None
    assert admin_user["username"] == "admin"
    assert admin_user["role"] == ROLE_ADMIN
    assert "*" in admin_user["permissions"]
    assert "password_hash" not in admin_user  # Never leak password hash in return

    # 2. Wrong password rejection
    bad_login = authenticate_user("admin", "WrongPassword!", db_conn=conn)
    assert bad_login is None

    # 3. Nonexistent user rejection
    none_user = authenticate_user("nonexistent_user", "Password123!", db_conn=conn)
    assert none_user is None


def test_user_registration_and_login(test_db):
    """Verify registering a new user, hashing password, and authenticating."""
    conn, _ = test_db

    new_user = register_user(
        username="new_operator",
        email="operator@logistics-ims.internal",
        password="NewOperatorPass2026!",
        role="WarehouseManager",
        full_name="New Warehouse Operator",
        db_conn=conn
    )

    assert new_user["id"].startswith("usr_")
    assert new_user["username"] == "new_operator"
    assert new_user["role"] == "WarehouseManager"

    # Authenticate new user
    auth_res = authenticate_user("new_operator", "NewOperatorPass2026!", db_conn=conn)
    assert auth_res is not None
    assert auth_res["id"] == new_user["id"]

    # Prevent duplicate username registration
    with pytest.raises(ValueError, match="already exists"):
        register_user("new_operator", "other@example.com", "Password123!", db_conn=conn)


# =============================================================================
# 4. RBAC Permission Matrix & Wildcard Tests
# =============================================================================

def test_wildcard_permission_matching():
    """Verify exact, domain prefix, and superuser wildcard matching."""
    # Global wildcard
    assert permission_matches("*", "orders:create") is True
    assert permission_matches("*", "anything:at:all") is True

    # Exact match
    assert permission_matches("orders:create", "orders:create") is True
    assert permission_matches("orders:create", "orders:cancel") is False

    # Domain prefix wildcard
    assert permission_matches("inventory:*", "inventory:restock") is True
    assert permission_matches("inventory:*", "inventory:adjust") is True
    assert permission_matches("inventory:*", "inventory:read") is True
    assert permission_matches("inventory:*", "orders:create") is False
    assert permission_matches("shipments:*", "shipments:update_status") is True


def test_five_roles_coverage():
    """Verify all 5 logistics roles are present in the matrix."""
    for role in ALL_ROLES:
        assert role in ROLE_PERMISSIONS_MATRIX
        assert len(ROLE_PERMISSIONS_MATRIX[role]) > 0


# =============================================================================
# 5. Flask Decorators & Bearer Token Enforcement Tests
# =============================================================================

def test_unauthenticated_request_rejected_401(test_client):
    """Verify missing Authorization header returns HTTP 401 Unauthorized."""
    res = test_client.get("/api/v1/protected/auth-only")
    assert res.status_code == 401
    data = res.get_json()
    assert data["error"] == "Unauthorized"
    assert "Missing Authorization" in data["message"]


def test_malformed_auth_header_rejected_401(test_client):
    """Verify malformed headers (not 'Bearer <token>') return HTTP 401."""
    # 1. Basic auth format
    res1 = test_client.get("/api/v1/protected/auth-only", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert res1.status_code == 401

    # 2. Empty token after Bearer
    res2 = test_client.get("/api/v1/protected/auth-only", headers={"Authorization": "Bearer "})
    assert res2.status_code == 401

    # 3. Forged signature
    res3 = test_client.get("/api/v1/protected/auth-only", headers={"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.fake.signature"})
    assert res3.status_code == 401


def test_rbac_matrix_matrix_enforcement(auth_flask_app, test_client):
    """
    Verifies RBAC enforcement across all 5 logistics roles:
    1. Driver CANNOT restock inventory (403 Forbidden).
    2. Driver CANNOT place customer orders (403 Forbidden).
    3. Customer CANNOT access admin users (403 Forbidden).
    4. Customer CANNOT restock inventory (403 Forbidden).
    5. WarehouseManager CAN restock and adjust inventory (200 OK).
    6. Dispatcher CAN calculate routes (200 OK).
    7. Driver CAN update shipment status (200 OK).
    8. Customer CAN place orders (201 OK).
    9. Admin has unrestricted system access to all endpoints (200/201 OK).
    """
    secret = auth_flask_app.config["JWT_SECRET"]

    def make_headers(role: str, user_id: str = "usr_test") -> dict:
        token = generate_token({
            "sub": user_id,
            "username": f"user_{role.lower()}",
            "role": role,
            "permissions": sorted(list(ROLE_PERMISSIONS_MATRIX.get(role, set())))
        }, secret=secret)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    driver_headers = make_headers(ROLE_DRIVER)
    customer_headers = make_headers(ROLE_CUSTOMER)
    manager_headers = make_headers(ROLE_WAREHOUSE_MANAGER)
    dispatcher_headers = make_headers(ROLE_DISPATCHER)
    admin_headers = make_headers(ROLE_ADMIN)

    # 1. Driver denied inventory restock (403)
    r1 = test_client.post("/api/v1/inventory/restock", json={}, headers=driver_headers)
    assert r1.status_code == 403, f"Expected 403 for Driver restock, got {r1.status_code}"

    # 2. Driver denied order placement (403)
    r2 = test_client.post("/api/v1/orders", json={}, headers=driver_headers)
    assert r2.status_code == 403, f"Expected 403 for Driver placing order, got {r2.status_code}"

    # 3. Customer denied admin users access (403)
    r3 = test_client.get("/api/v1/admin/users", headers=customer_headers)
    assert r3.status_code == 403, f"Expected 403 for Customer accessing admin, got {r3.status_code}"

    # 4. Customer denied inventory restock (403)
    r4 = test_client.post("/api/v1/inventory/restock", json={}, headers=customer_headers)
    assert r4.status_code == 403, f"Expected 403 for Customer restocking, got {r4.status_code}"

    # 5. WarehouseManager allowed inventory restock (200) and adjust (200 via permission wildcard)
    r5a = test_client.post("/api/v1/inventory/restock", json={}, headers=manager_headers)
    assert r5a.status_code == 200, f"Expected 200 for Manager restock, got {r5a.status_code}"

    r5b = test_client.post("/api/v1/inventory/adjust", json={}, headers=manager_headers)
    assert r5b.status_code == 200, f"Expected 200 for Manager adjust, got {r5b.status_code}"

    # 6. Dispatcher allowed network routes calculation (200)
    r6 = test_client.post("/api/v1/network/routes", json={}, headers=dispatcher_headers)
    assert r6.status_code == 200, f"Expected 200 for Dispatcher routes, got {r6.status_code}"

    # 7. Driver allowed shipment status update (200 via permission)
    r7 = test_client.post("/api/v1/shipments/status", json={}, headers=driver_headers)
    assert r7.status_code == 200, f"Expected 200 for Driver shipment status update, got {r7.status_code}"

    # 8. Customer allowed order placement (201)
    r8 = test_client.post("/api/v1/orders", json={}, headers=customer_headers)
    assert r8.status_code == 201, f"Expected 201 for Customer placing order, got {r8.status_code}"

    # 9. Admin allowed access everywhere (200 / 201)
    assert test_client.get("/api/v1/admin/users", headers=admin_headers).status_code == 200
    assert test_client.post("/api/v1/inventory/restock", json={}, headers=admin_headers).status_code == 200
    assert test_client.post("/api/v1/orders", json={}, headers=admin_headers).status_code == 201
    assert test_client.post("/api/v1/network/routes", json={}, headers=admin_headers).status_code == 200


if __name__ == "__main__":
    pytest.main(["-v", __file__])
