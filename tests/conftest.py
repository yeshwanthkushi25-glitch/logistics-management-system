"""Shared pytest configuration, fixtures, and test infrastructure for Logistics IMS."""

import os
import sys
import tempfile
import sqlite3
import time
import pytest
from typing import Dict, Any, Generator

# Add project root to sys.path so 'app' and 'tests' are importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Attempt to import core application modules; if not yet implemented, allow graceful collection
try:
    from app import create_app
    from app.db.connection import get_db_connection, transaction_scope
    from app.db.init_db import init_database
    from app.db.cache import get_cache_manager
    from app.db.nosql import get_doc_store
    from app.db.storage import get_storage_client
    from app.auth.security import generate_token, hash_password
    APP_AVAILABLE = True
except ImportError as err:
    APP_AVAILABLE = False
    APP_IMPORT_ERROR = str(err)


# ---------------------------------------------------------------------------
# Test Application & Database Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_db_path() -> Generator[str, None, None]:
    """Provides a temporary SQLite database file for testing."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="logistics_test_")
    os.close(fd)
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture
def app(test_db_path: str):
    """Initializes and yields a Flask test application instance with isolated DB."""
    if not APP_AVAILABLE:
        pytest.skip(f"App modules not yet implemented: {APP_IMPORT_ERROR}")

    test_config = {
        "TESTING": True,
        "DATABASE_PATH": test_db_path,
        "DATABASE_URL": f"sqlite:///{test_db_path}",
        "JWT_SECRET": "test-evaluation-secret-key-super-secure-32-bytes!",
        "CACHE_TYPE": "memory",
        "STORAGE_BACKEND": "local",
        "STORAGE_LOCAL_DIR": tempfile.mkdtemp(prefix="logistics_storage_test_"),
    }
    
    application = create_app(test_config)
    
    with application.app_context():
        # Initialize schema and seed minimal baseline data
        init_database(db_path=test_db_path, seed_demo_data=True)
        yield application


@pytest.fixture
def client(app):
    """Yields a Flask test client for making HTTP requests."""
    return app.test_client()


@pytest.fixture
def db_conn(app, test_db_path: str) -> Generator[sqlite3.Connection, None, None]:
    """Yields a direct raw SQLite connection to the test database with foreign keys enabled."""
    conn = sqlite3.connect(test_db_path, timeout=10.0)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Authentication & Role Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def auth_tokens(app) -> Dict[str, str]:
    """Generates valid JWT tokens for all 5 system roles."""
    if not APP_AVAILABLE:
        pytest.skip(f"App modules not yet implemented: {APP_IMPORT_ERROR}")

    roles = ["Admin", "WarehouseManager", "Dispatcher", "Driver", "Customer"]
    tokens = {}
    for role in roles:
        user_payload = {
            "sub": f"usr-test-{role.lower()}-01",
            "username": f"test_{role.lower()}",
            "role": role,
            "permissions": [f"{role.lower()}:access"],
            "exp": int(time.time()) + 3600
        }
        tokens[role] = generate_token(user_payload)
    return tokens


@pytest.fixture
def admin_headers(auth_tokens) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {auth_tokens['Admin']}",
        "Content-Type": "application/json"
    }


@pytest.fixture
def manager_headers(auth_tokens) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {auth_tokens['WarehouseManager']}",
        "Content-Type": "application/json"
    }


@pytest.fixture
def dispatcher_headers(auth_tokens) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {auth_tokens['Dispatcher']}",
        "Content-Type": "application/json"
    }


@pytest.fixture
def driver_headers(auth_tokens) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {auth_tokens['Driver']}",
        "Content-Type": "application/json"
    }


@pytest.fixture
def customer_headers(auth_tokens) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {auth_tokens['Customer']}",
        "Content-Type": "application/json"
    }


# ---------------------------------------------------------------------------
# Test Data Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_telemetry_payload() -> Dict[str, Any]:
    """Generates a valid IoT GPS and cold-chain sensor payload."""
    return {
        "tracking_number": "TRK-20261109-0081",
        "device_id": "IOT-TRK-771",
        "timestamp": "2026-11-09T17:00:00Z",
        "latitude": 41.8790,
        "longitude": -87.6310,
        "altitude_m": 182.5,
        "speed_kmh": 58.4,
        "temperature_celsius": 4.2,
        "humidity_percent": 51.0,
        "shock_impact_g": 0.08,
        "battery_percent": 94
    }


@pytest.fixture
def sample_cold_chain_breach_telemetry() -> Dict[str, Any]:
    """Generates an anomalous IoT payload breaching cold-chain thresholds."""
    return {
        "tracking_number": "TRK-20261109-0081",
        "device_id": "IOT-TRK-771",
        "timestamp": "2026-11-09T17:05:00Z",
        "latitude": 41.8820,
        "longitude": -87.6350,
        "altitude_m": 183.0,
        "speed_kmh": 62.0,
        "temperature_celsius": -12.5,  # Breaches standard +2C to +8C vaccine constraint
        "humidity_percent": 75.0,
        "shock_impact_g": 3.4,         # High impact shock
        "battery_percent": 92
    }


@pytest.fixture
def sample_order_payload() -> Dict[str, Any]:
    """Standard customer order creation payload."""
    return {
        "shipping_address": "123 Michigan Ave, Chicago, IL 60601",
        "destination_city": "Chicago",
        "destination_lat": 41.8781,
        "destination_lon": -87.6298,
        "items": [
            {"product_id": "prod-01", "quantity": 2},
            {"product_id": "prod-02", "quantity": 1}
        ]
    }
