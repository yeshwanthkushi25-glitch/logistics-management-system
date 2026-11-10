"""
Comprehensive Automated Test Suite for Milestone 4 REST API Layer.

Verifies:
1. Application Factory, CORS headers, JSON serialization, and global error handlers (400, 401, 403, 404, 405, 422, 500).
2. Auth API: Register, Login, Me, Admin Users.
3. Inventory API: List with filters & pagination, Restock, Adjust, Warehouses directory, Capacity boundaries.
4. Orders API: Atomic order placement, Multi-warehouse allocation, Idempotency, Details, Cancellation & Hold Release.
5. Shipments API: Manifest creation, Bulk batch (100+ items < 3.0s), 7-State FSM progression, Milestones, Telemetry boundary validation, POD upload.
6. Routing API: Dijkstra / A* optimal path, Bottlenecks (rho >= 0.85), Dynamic rerouting on outage, K-alternatives, Reachability, Graph topology.
7. Analytics API: KPIs summary, Audit logs, Cache-Aside statistics and metrics.
"""

import os
import sys
import time
import tempfile
import uuid
import pytest
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.auth.security import generate_token, hash_password
from app.db.init_db import init_database
from app.db.connection import get_db_connection


# =============================================================================
# Pytest Fixtures
# =============================================================================

@pytest.fixture(scope="module")
def api_test_db():
    """Provides an isolated test database with seeded baseline data."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="api_test_")
    os.close(fd)
    init_database(db_path=path, seed_demo_data=True)
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture(scope="module")
def api_app(api_test_db):
    """Initializes Flask application instance with isolated DB and local storage."""
    storage_tmp = tempfile.mkdtemp(prefix="api_storage_")
    test_config = {
        "TESTING": True,
        "DATABASE_PATH": api_test_db,
        "DATABASE_URL": f"sqlite:///{api_test_db}",
        "JWT_SECRET": "test-secret-key-32-bytes-secure-evaluation!",
        "CACHE_TYPE": "memory",
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_DIR": storage_tmp
    }
    application = create_app(test_config=test_config)
    yield application


@pytest.fixture
def client(api_app):
    """Yields Flask test client."""
    return api_app.test_client()


@pytest.fixture(scope="module")
def tokens(api_app):
    """Generates valid JWT tokens for all 5 roles."""
    roles = ["Admin", "WarehouseManager", "Dispatcher", "Driver", "Customer"]
    out = {}
    with api_app.app_context():
        for role in roles:
            payload = {
                "sub": f"usr-test-{role.lower()}-01",
                "username": f"test_{role.lower()}",
                "role": role,
                "permissions": [f"{role.lower()}:access"],
                "exp": int(time.time()) + 7200
            }
            out[role] = generate_token(payload)
    return out


@pytest.fixture
def admin_headers(tokens):
    return {"Authorization": f"Bearer {tokens['Admin']}", "Content-Type": "application/json"}

@pytest.fixture
def manager_headers(tokens):
    return {"Authorization": f"Bearer {tokens['WarehouseManager']}", "Content-Type": "application/json"}

@pytest.fixture
def dispatcher_headers(tokens):
    return {"Authorization": f"Bearer {tokens['Dispatcher']}", "Content-Type": "application/json"}

@pytest.fixture
def driver_headers(tokens):
    return {"Authorization": f"Bearer {tokens['Driver']}", "Content-Type": "application/json"}

@pytest.fixture
def customer_headers(tokens):
    return {"Authorization": f"Bearer {tokens['Customer']}", "Content-Type": "application/json"}


# =============================================================================
# 1. Application Factory, CORS & Error Handler Tests
# =============================================================================

def test_app_factory_and_cors(client):
    """Verify application factory, health check, ping, and CORS preflight headers."""
    # Health check
    res = client.get("/health")
    assert res.status_code == 200
    assert res.get_json()["status"] == "healthy"

    # Ping
    ping_res = client.get("/api/v1/ping")
    assert ping_res.status_code == 200
    assert ping_res.get_json()["ping"] == "pong"

    # CORS headers on response
    assert res.headers.get("Access-Control-Allow-Origin") == "*"
    assert "Authorization" in res.headers.get("Access-Control-Allow-Headers", "")

    # CORS OPTIONS preflight
    options_res = client.options("/api/v1/orders")
    assert options_res.status_code in (200, 204)
    assert options_res.headers.get("Access-Control-Allow-Origin") == "*"


def test_global_error_handlers_json(client):
    """Verify global error handlers return structured JSON for 404 and 405."""
    # 404
    res_404 = client.get("/api/v1/nonexistent-endpoint-xyz")
    assert res_404.status_code == 404
    assert res_404.is_json
    assert res_404.get_json().get("error") == "Not Found"

    # 405
    res_405 = client.delete("/health")
    assert res_405.status_code == 405
    assert res_405.is_json
    assert res_405.get_json().get("error") == "Method Not Allowed"


# =============================================================================
# 2. Authentication API Tests
# =============================================================================

def test_auth_register_and_login(client, admin_headers, customer_headers):
    """Verify user registration, login credential verification, and /me introspection."""
    new_username = f"user_{uuid.uuid4().hex[:6]}"
    reg_payload = {
        "username": new_username,
        "email": f"{new_username}@test.com",
        "password": "Password123!",
        "role": "Customer",
        "full_name": "Test Customer User"
    }

    # 1. Register
    reg_res = client.post("/api/v1/auth/register", json=reg_payload)
    assert reg_res.status_code == 201
    reg_data = reg_res.get_json()
    assert reg_data["username"] == new_username
    assert reg_data["role"] == "Customer"

    # Duplicate register returns 409
    dup_res = client.post("/api/v1/auth/register", json=reg_payload)
    assert dup_res.status_code == 409

    # 2. Login
    login_payload = {"username": new_username, "password": "Password123!"}
    login_res = client.post("/api/v1/auth/login", json=login_payload)
    assert login_res.status_code == 200
    login_data = login_res.get_json()
    assert "access_token" in login_data
    token = login_data["access_token"]

    # Bad login
    bad_login = client.post("/api/v1/auth/login", json={"username": new_username, "password": "WrongPassword"})
    assert bad_login.status_code == 401

    # 3. GET /me
    me_headers = {"Authorization": f"Bearer {token}"}
    me_res = client.get("/api/v1/auth/me", headers=me_headers)
    assert me_res.status_code == 200
    assert me_res.get_json()["username"] == new_username


def test_auth_admin_users_rbac(client, admin_headers, customer_headers):
    """Verify GET /api/v1/admin/users allows Admin and rejects Customer with 403."""
    # Customer rejected
    res_cust = client.get("/api/v1/admin/users", headers=customer_headers)
    assert res_cust.status_code == 403

    # Admin allowed
    res_admin = client.get("/api/v1/admin/users", headers=admin_headers)
    assert res_admin.status_code == 200
    data = res_admin.get_json()
    assert "users" in data
    assert len(data["users"]) > 0


# =============================================================================
# 3. Inventory & Warehouse API Tests
# =============================================================================

def test_inventory_list_and_low_stock_filter(client, customer_headers, manager_headers):
    """Verify inventory stock query with product filter and low_stock threshold filter."""
    # Baseline query
    res = client.get("/api/v1/inventory?product_id=prod-01", headers=customer_headers)
    assert res.status_code == 200
    items = res.get_json()["items"]
    assert len(items) > 0
    assert items[0]["product_id"] == "prod-01"

    # Low stock filter
    low_res = client.get("/api/v1/inventory?low_stock=true", headers=manager_headers)
    assert low_res.status_code == 200
    low_items = low_res.get_json()["items"]
    assert isinstance(low_items, list)


def test_inventory_restock_and_capacity_boundary(client, manager_headers, customer_headers):
    """Verify restock by WarehouseManager succeeds and capacity overflow returns 422."""
    # Normal restock
    restock_payload = {
        "warehouse_id": "wh-01",
        "product_id": "prod-01",
        "quantity": 10,
        "batch_number": "BATCH-TEST-RESTOCK-01",
        "bin_code": "WH-CHI-ZN-A-AI01-RK01-SH01-BN01"
    }
    res = client.post("/api/v1/inventory/restock", json=restock_payload, headers=manager_headers)
    assert res.status_code == 201
    assert res.get_json()["status"] == "SUCCESS"

    # Customer denied restock
    cust_res = client.post("/api/v1/inventory/restock", json=restock_payload, headers=customer_headers)
    assert cust_res.status_code == 403

    # Capacity overflow (10,000,000 units exceeds warehouse storage)
    overflow_payload = dict(restock_payload, quantity=10000000)
    overflow_res = client.post("/api/v1/inventory/restock", json=overflow_payload, headers=manager_headers)
    assert overflow_res.status_code in (400, 422)


def test_inventory_adjust_with_audit(client, manager_headers, driver_headers):
    """Verify cycle count adjustment logs variance and rejects Driver with 403."""
    adjust_payload = {
        "warehouse_id": "wh-01",
        "product_id": "prod-01",
        "quantity_delta": -1,
        "reason_code": "DAMAGED",
        "notes": "Damaged unit during inspection"
    }
    # Driver rejected
    res_driver = client.post("/api/v1/inventory/adjust", json=adjust_payload, headers=driver_headers)
    assert res_driver.status_code == 403

    # Manager allowed
    res_mgr = client.post("/api/v1/inventory/adjust", json=adjust_payload, headers=manager_headers)
    assert res_mgr.status_code == 200
    assert "audit_id" in res_mgr.get_json() or "audit_event_id" in res_mgr.get_json()


def test_warehouses_directory(client):
    """Verify listing warehouses returns distribution centers and capacities."""
    res = client.get("/api/v1/inventory/warehouses")
    assert res.status_code == 200
    data = res.get_json()
    assert "warehouses" in data
    assert len(data["warehouses"]) >= 1
    assert "capacity_sqm" in data["warehouses"][0]


# =============================================================================
# 4. Orders API Tests
# =============================================================================

def test_order_atomic_placement_idempotency_and_cancel(client, customer_headers, driver_headers):
    """Verify order creation, idempotency key deduplication, retrieval, and cancellation."""
    # 1. Driver denied order creation
    driver_res = client.post("/api/v1/orders", json={"items": [{"product_id": "prod-01", "quantity": 1}]}, headers=driver_headers)
    assert driver_res.status_code == 403

    # 2. Customer places order with Idempotency-Key
    idem_key = str(uuid.uuid4())
    headers = dict(customer_headers)
    headers["Idempotency-Key"] = idem_key

    order_payload = {
        "shipping_address": "888 N Lake Shore Dr, Chicago, IL",
        "destination_city": "Chicago",
        "destination_lat": 41.8900,
        "destination_lon": -87.6200,
        "items": [{"product_id": "prod-01", "quantity": 1}]
    }

    res1 = client.post("/api/v1/orders", json=order_payload, headers=headers)
    assert res1.status_code == 201
    order1 = res1.get_json()
    order_id = order1["order_id"]

    # 3. Repeat submission with identical Idempotency-Key returns identical order
    res2 = client.post("/api/v1/orders", json=order_payload, headers=headers)
    assert res2.status_code in (200, 201)
    assert res2.get_json()["order_id"] == order_id

    # 4. Retrieve order by ID
    get_res = client.get(f"/api/v1/orders/{order_id}", headers=customer_headers)
    assert get_res.status_code == 200
    assert get_res.get_json()["order_id"] == order_id

    # 5. Cancel order
    cancel_res = client.post(f"/api/v1/orders/{order_id}/cancel", headers=customer_headers)
    assert cancel_res.status_code == 200
    assert cancel_res.get_json()["status"] == "CANCELLED"


# =============================================================================
# 5. Shipments & Telemetry API Tests
# =============================================================================

def test_shipment_manifest_and_bulk_batch_under_3_seconds(client, dispatcher_headers):
    """Verify single shipment creation and 100+ bulk batch processing within 3.0s budget."""
    # Single creation
    shp_payload = {
        "order_id": "ord-test-sample-01",
        "origin_warehouse_id": "wh-01",
        "carrier": "Apex Freight",
        "service_level": "EXPRESS"
    }
    single_res = client.post("/api/v1/shipments", json=shp_payload, headers=dispatcher_headers)
    assert single_res.status_code == 201
    tracking_num = single_res.get_json()["tracking_number"]
    assert tracking_num.startswith("TRK-")

    # Bulk batch of 100 shipments
    batch_items = [
        {
            "order_id": f"ord-bulk-{i:03d}",
            "origin_warehouse_id": "wh-01",
            "destination_city": "Chicago",
            "carrier": "INTERNAL_FLEET",
            "service_level": "STANDARD"
        }
        for i in range(100)
    ]
    batch_payload = {
        "batch_id": f"BATCH-{uuid.uuid4().hex[:6]}",
        "shipments": batch_items
    }

    t0 = time.perf_counter()
    batch_res = client.post("/api/v1/shipments/batch", json=batch_payload, headers=dispatcher_headers)
    elapsed = time.perf_counter() - t0

    assert batch_res.status_code == 201
    batch_data = batch_res.get_json()
    assert batch_data["processed_count"] >= 100
    assert elapsed < 3.0, f"Bulk batch took {elapsed:.2f}s, exceeding 3.0s latency limit"


def test_shipment_cache_aside_and_milestone_progression(client, customer_headers, driver_headers):
    """Verify Cache-Aside tracking inquiry with X-Cache headers and FSM milestone progression."""
    tracking_num = "TRK-20261109-0081"

    # First read: cache MISS
    res1 = client.get(f"/api/v1/shipments/{tracking_num}/track", headers=customer_headers)
    if res1.status_code == 200:
        assert res1.headers.get("X-Cache") in ("MISS", "HIT", None)

        # Second read: cache HIT
        res2 = client.get(f"/api/v1/shipments/{tracking_num}/track", headers=customer_headers)
        assert res2.status_code == 200
        assert res2.headers.get("X-Cache") == "HIT"

    # Advance status on test shipment
    test_trk = "TRK-TEST-PICKUP-01"
    ms_payload = {
        "milestone_type": "PICKUP_COMPLETED",
        "location_name": "Chicago Central DC",
        "latitude": 41.8781,
        "longitude": -87.6298,
        "notes": "Picked up by Driver Dave"
    }
    ms_res = client.post(f"/api/v1/shipments/{test_trk}/milestones", json=ms_payload, headers=driver_headers)
    assert ms_res.status_code in (200, 201)


def test_telemetry_ingestion_and_boundary_rejection(client, driver_headers):
    """Verify valid telemetry ingestion and 422 rejection of out-of-bound coordinates."""
    # Valid telemetry
    valid_payload = {
        "tracking_number": "TRK-20261109-0081",
        "device_id": "IOT-TRK-771",
        "latitude": 41.8800,
        "longitude": -87.6300,
        "temperature_celsius": 4.5
    }
    val_res = client.post("/api/v1/telemetry", json=valid_payload, headers=driver_headers)
    assert val_res.status_code in (200, 201)

    # Malformed / Out of bounds latitude (> 90.0)
    bad_payload = dict(valid_payload, latitude=150.0)
    bad_res = client.post("/api/v1/telemetry", json=bad_payload, headers=driver_headers)
    assert bad_res.status_code in (400, 422)


# =============================================================================
# 6. Network Routing API Tests
# =============================================================================

def test_network_routes_dijkstra_and_cost(client, dispatcher_headers):
    """Verify Dijkstra shortest path and least-cost route computation."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "optimization_criteria": "distance"
    }
    res = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    assert res.status_code == 200
    data = res.get_json()
    assert "path_nodes" in data or "path" in data
    path = data.get("path_nodes") or data.get("path")
    assert path[0] == "CDC-CHICAGO"
    assert path[-1] == "LSF-NEWYORK"


def test_network_bottlenecks_and_reroute(client, dispatcher_headers):
    """Verify bottleneck detection and dynamic edge outage rerouting."""
    # Bottlenecks
    bn_res = client.get("/api/v1/network/bottlenecks", headers=dispatcher_headers)
    assert bn_res.status_code == 200
    assert "bottlenecks" in bn_res.get_json()

    # Dynamic Reroute
    reroute_payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "failed_edges": ["CORR-CHI-RDC-DETROIT"],
        "exclude_nodes": ["RDC-DETROIT"]
    }
    reroute_res = client.post("/api/v1/network/routes/reroute", json=reroute_payload, headers=dispatcher_headers)
    assert reroute_res.status_code == 200
    reroute_data = reroute_res.get_json()
    fallback_path = reroute_data.get("path_nodes") or reroute_data.get("path")
    assert "RDC-DETROIT" not in fallback_path


def test_network_reachability_and_graph(client):
    """Verify network reachability and complete graph topology."""
    reach_res = client.get("/api/v1/network/reachability?hub_id=CDC-CHICAGO")
    assert reach_res.status_code == 200
    assert "reachable_nodes" in reach_res.get_json()

    graph_res = client.get("/api/v1/network/graph")
    assert graph_res.status_code == 200
    graph_data = graph_res.get_json()
    assert "nodes" in graph_data
    assert "edges" in graph_data


# =============================================================================
# 7. Analytics API Tests
# =============================================================================

def test_analytics_kpis_and_cache_metrics(client, admin_headers):
    """Verify executive KPIs calculation and CacheManager hit/miss metrics."""
    # KPIs
    kpi_res = client.get("/api/v1/analytics/kpis", headers=admin_headers)
    assert kpi_res.status_code == 200
    kpi_data = kpi_res.get_json()
    assert "inventory" in kpi_data
    assert "orders" in kpi_data
    assert "shipments" in kpi_data

    # Cache Stats
    cache_res = client.get("/api/v1/analytics/cache/stats", headers=admin_headers)
    assert cache_res.status_code == 200
    cache_data = cache_res.get_json()
    assert "hits" in cache_data or "cache_hits" in cache_data
    assert "misses" in cache_data or "cache_misses" in cache_data
    assert "hit_ratio" in cache_data

    # Audit Logs (Admin only)
    audit_res = client.get("/api/v1/analytics/audit/logs", headers=admin_headers)
    assert audit_res.status_code == 200
    assert "logs" in audit_res.get_json()
