"""
Logistics IMS - Database Initialization & Demo Data Seeding Script.

Provides CLI and programmatic entry points for creating relational schemas,
initializing NoSQL collections, and populating a realistic logistics test dataset.
Uses SQLite 3.24+ UPSERT (INSERT ... ON CONFLICT(id) DO UPDATE SET ...) to guarantee
100% idempotent re-seeding on existing databases without foreign key violation crashes.
"""

import os
import sys
import json
import sqlite3
import argparse
import logging
from typing import Optional, Dict, Any
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_schema_sql(schema_path: Optional[str] = None) -> str:
    """Reads the SQL DDL file from disk."""
    path = schema_path or DEFAULT_SCHEMA_PATH
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    raise FileNotFoundError(f"Schema file not found at: {path}")


def seed_demo_dataset(conn: sqlite3.Connection) -> Dict[str, int]:
    """
    Seeds a comprehensive, realistic logistics dataset covering:
    - 5 Roles with granular permission lists.
    - Standard and test users with securely hashed credentials.
    - 4 Warehouse distribution hubs with GPS coordinates and aliases.
    - 16 Physical warehouse storage bins.
    - 16 Products covering electronics, pharmaceuticals, and industrial components.
    - Varied inventory states (Healthy, Low Stock Alert, Out of Stock, Reserved).
    - 10 Directed Network Corridors connecting hubs.
    - Sample Orders, Line Items, Shipments, and Milestone checkpoints.
    - Sample NoSQL Documents (Telemetry streams, audit logs, order snapshots).

    Uses ON CONFLICT(id) DO UPDATE SET ... for complete idempotency with PRAGMA foreign_keys = ON.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA foreign_keys = ON;")

    # -------------------------------------------------------------------------
    # 1. Seed Roles (5 system roles)
    # -------------------------------------------------------------------------
    roles_data = [
        ("role_admin", "Admin", "System Administrator with full operational & analytics permissions", json.dumps(["*"])),
        ("role_wm", "WarehouseManager", "Warehouse Manager overseeing inventory allocation & restock", json.dumps(["inventory:*", "warehouses:read", "shipments:read", "orders:read"])),
        ("role_dispatcher", "Dispatcher", "Logistics Dispatcher managing network routes and freight shipments", json.dumps(["shipments:*", "network:*", "orders:read"])),
        ("role_driver", "Driver", "Transport driver updating shipment status, milestones, and POD uploads", json.dumps(["shipments:read_assigned", "shipments:update_milestone", "pod:upload", "telemetry:write"])),
        ("role_customer", "Customer", "External client placing orders and tracking logistics shipments", json.dumps(["orders:create", "orders:read_own", "shipments:track"])),
    ]
    cur.executemany("""
        INSERT INTO roles (id, name, description, permissions)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name = excluded.name,
            description = excluded.description,
            permissions = excluded.permissions
    """, roles_data)

    # -------------------------------------------------------------------------
    # 2. Seed Default & Test Users (Password: Password123!)
    # -------------------------------------------------------------------------
    pwd_hash = generate_password_hash("Password123!")
    users_data = [
        # Operational personnel
        ("user_admin", "admin", "admin@logistics-ims.internal", pwd_hash, "role_admin", "System Administrator"),
        ("user_wm_chi", "wm_chicago", "marcus.vance@logistics-ims.internal", pwd_hash, "role_wm", "Marcus Vance"),
        ("user_dispatcher", "dispatcher_dan", "danielle.reyes@logistics-ims.internal", pwd_hash, "role_dispatcher", "Danielle Reyes"),
        ("user_driver", "driver_dave", "david.chen@logistics-ims.internal", pwd_hash, "role_driver", "David Chen"),
        ("user_cust_alice", "customer_alice", "alice.smith@example.com", pwd_hash, "role_customer", "Alice Smith"),
        ("user_cust_bob", "customer_bob", "bob.jones@example.com", pwd_hash, "role_customer", "Bob Jones"),
        # Automated test-suite identity fixtures (from tests/conftest.py auth_tokens)
        ("usr-test-admin-01", "test_admin", "test_admin@test.local", pwd_hash, "role_admin", "Test Admin User"),
        ("usr-test-warehousemanager-01", "test_warehousemanager", "test_wm@test.local", pwd_hash, "role_wm", "Test Warehouse Manager"),
        ("usr-test-dispatcher-01", "test_dispatcher", "test_dispatcher@test.local", pwd_hash, "role_dispatcher", "Test Dispatcher"),
        ("usr-test-driver-01", "test_driver", "test_driver@test.local", pwd_hash, "role_driver", "Test Driver"),
        ("usr-test-customer-01", "test_customer", "test_customer@test.local", pwd_hash, "role_customer", "Test Customer"),
    ]
    cur.executemany("""
        INSERT INTO users (id, username, email, password_hash, role_id, full_name)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            username = excluded.username,
            email = excluded.email,
            password_hash = excluded.password_hash,
            role_id = excluded.role_id,
            full_name = excluded.full_name
    """, users_data)

    # -------------------------------------------------------------------------
    # 3. Seed Warehouses (4 Strategic Hubs with Primary & Test IDs)
    # -------------------------------------------------------------------------
    warehouses_data = [
        ("wh-01", "CDC-CHI", "Chicago Central Distribution Center", "CDC", "7100 S Cicero Ave", "Chicago", "IL", "USA", 41.7637, -87.7410, 50000.0),
        ("wh-02", "RDC-DET", "Detroit Regional Distribution Center", "RDC", "2800 E Grand Blvd", "Detroit", "MI", "USA", 42.3740, -83.0450, 25000.0),
        ("wh-03", "HUB-CMH", "Columbus Transit Cross-Dock Hub", "HUB", "4300 International Gateway", "Columbus", "OH", "USA", 39.9980, -82.8919, 35000.0),
        ("wh-04", "LSF-NYC", "New York Last-Mile Sortation Facility", "LSF", "105-02 Northern Blvd", "Corona", "NY", "USA", 40.7589, -73.8648, 15000.0),
        # Explorer aliases for compatibility
        ("wh_cdc_chi", "CDC-CHICAGO", "Chicago Central Distribution Center (Alias)", "CDC", "7100 S Cicero Ave", "Chicago", "IL", "USA", 41.7637, -87.7410, 50000.0),
        ("wh_rdc_det", "RDC-DETROIT", "Detroit Regional Distribution Center (Alias)", "RDC", "2800 E Grand Blvd", "Detroit", "MI", "USA", 42.3740, -83.0450, 25000.0),
        ("wh_hub_cmh", "HUB-COLUMBUS", "Columbus Transit Cross-Dock Hub (Alias)", "HUB", "4300 International Gateway", "Columbus", "OH", "USA", 39.9980, -82.8919, 35000.0),
        ("wh_lsf_nyc", "LSF-NEWYORK", "New York Last-Mile Sortation Facility (Alias)", "LSF", "105-02 Northern Blvd", "Corona", "NY", "USA", 40.7589, -73.8648, 15000.0),
    ]
    cur.executemany("""
        INSERT INTO warehouses (id, code, name, type, address, city, state, country, latitude, longitude, capacity_sqm)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            code = excluded.code,
            name = excluded.name,
            type = excluded.type,
            address = excluded.address,
            city = excluded.city,
            state = excluded.state,
            country = excluded.country,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            capacity_sqm = excluded.capacity_sqm
    """, warehouses_data)

    # -------------------------------------------------------------------------
    # 4. Seed Warehouse Bins
    # -------------------------------------------------------------------------
    bins_data = [
        # wh-01 bins (specifically queried by test_inventory.py)
        ("bin_wh01_a1", "wh-01", "Zone-A", "01", "01", "1", "WH-CHI-ZN-A-AI01-RK01-SH01-BN01", 1000.0),
        ("bin_wh01_a2", "wh-01", "Zone-A", "02", "01", "1", "WH-CHI-ZN-A-AI02-RK01-SH01-BN01", 1000.0),
        ("bin_wh01_c1", "wh-01", "Zone-C", "01", "01", "1", "WH-CHI-COLD-01", 500.0),
        ("bin_wh01_s1", "wh-01", "Zone-S", "01", "01", "1", "WH-CHI-STAGE-01", 2000.0),
        # wh-02 bins
        ("bin_wh02_a1", "wh-02", "Zone-A", "01", "01", "1", "WH-DET-ZN-A-AI01-RK01-SH01-BN01", 1000.0),
        ("bin_wh02_c1", "wh-02", "Zone-C", "01", "01", "1", "WH-DET-COLD-01", 500.0),
        # wh-03 bins
        ("bin_wh03_a1", "wh-03", "Zone-A", "01", "01", "1", "WH-CMH-ZN-A-AI01-RK01-SH01-BN01", 1500.0),
        ("bin_wh03_c1", "wh-03", "Zone-C", "01", "01", "1", "WH-CMH-COLD-01", 600.0),
        # wh-04 bins
        ("bin_wh04_a1", "wh-04", "Zone-A", "01", "01", "1", "WH-NYC-ZN-A-AI01-RK01-SH01-BN01", 800.0),
        ("bin_wh04_c1", "wh-04", "Zone-C", "01", "01", "1", "WH-NYC-COLD-01", 400.0),
        # Explorer alias bins
        ("bin_chi_a1", "wh_cdc_chi", "Zone-A", "01", "01", "1", "CDC-CHI-A1-01", 1000.0),
        ("bin_chi_a2", "wh_cdc_chi", "Zone-A", "02", "01", "1", "CDC-CHI-A2-01", 1000.0),
        ("bin_chi_c1", "wh_cdc_chi", "Zone-C", "01", "01", "1", "CDC-CHI-COLD-01", 500.0),
        ("bin_chi_s1", "wh_cdc_chi", "Zone-S", "01", "01", "1", "CDC-CHI-STAGE-01", 2000.0),
        ("bin_det_a1", "wh_rdc_det", "Zone-A", "01", "01", "1", "RDC-DET-A1-01", 1000.0),
        ("bin_det_a2", "wh_rdc_det", "Zone-A", "02", "01", "1", "RDC-DET-A2-01", 1000.0),
        ("bin_cmh_c1", "wh_hub_cmh", "Zone-C", "01", "01", "1", "HUB-CMH-COLD-01", 500.0),
        ("bin_cmh_a1", "wh_hub_cmh", "Zone-A", "01", "01", "1", "HUB-CMH-A1-01", 1000.0),
        ("bin_nyc_c1", "wh_lsf_nyc", "Zone-C", "01", "01", "1", "LSF-NYC-COLD-01", 500.0),
        ("bin_nyc_a1", "wh_lsf_nyc", "Zone-A", "01", "01", "1", "LSF-NYC-A1-01", 1000.0),
    ]
    cur.executemany("""
        INSERT INTO warehouse_bins (id, warehouse_id, zone, aisle, rack, shelf, bin_code, max_weight_kg)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            warehouse_id = excluded.warehouse_id,
            zone = excluded.zone,
            aisle = excluded.aisle,
            rack = excluded.rack,
            shelf = excluded.shelf,
            bin_code = excluded.bin_code,
            max_weight_kg = excluded.max_weight_kg
    """, bins_data)

    # -------------------------------------------------------------------------
    # 5. Seed Products (Standard Products + Test Specific Products)
    # -------------------------------------------------------------------------
    products_data = [
        # Test fixture products (specifically queried by test suites)
        ("prod-01", "SKU-ELEC-01", "Industrial IoT GPS Telemetry Beacon", "Rugged 4G GPS and environmental sensor tracker", "Electronics", 149.99, 0.35, 25),
        ("prod-02", "SKU-ELEC-02", "Edge AI Telematics Gateway Controller", "High-throughput in-vehicle telematics processor", "Electronics", 499.00, 1.20, 10),
        ("prod-zero", "SKU-ZERO-01", "Zero Stock Out-of-Stock Sensor", "Component specifically with zero available inventory", "Electronics", 99.00, 0.50, 10),
        ("prod-east", "SKU-EAST-01", "East Coast Distribution Component", "Multi-warehouse split item located in East Hub", "Industrial", 75.00, 2.00, 10),
        ("prod-west", "SKU-WEST-01", "West Coast Distribution Component", "Multi-warehouse split item located in West Hub", "Industrial", 85.00, 2.50, 10),
        ("prod-rare", "SKU-RARE-01", "Rare Precision Avionics Component", "Scarce inventory item for concurrency race testing", "Critical", 199.99, 1.00, 5),
        # Explorer catalog products
        ("prod_elec_001", "SKU-IOT-GPS-01", "Industrial IoT GPS Telemetry Beacon Pro", "Rugged 4G GPS and environmental sensor tracker", "Electronics", 149.99, 0.35, 25),
        ("prod_elec_002", "SKU-EDGE-GW-02", "Edge AI Telematics Gateway Pro", "High-throughput in-vehicle telematics processor", "Electronics", 499.00, 1.20, 10),
        ("prod_elec_003", "SKU-SCAN-RFID-03", "Ruggedized Warehouse RFID Scanner", "Long-range UHF RFID inventory handheld scanner", "Electronics", 320.00, 0.65, 15),
        ("prod_elec_004", "SKU-BATT-LION-04", "Lithium-Ion Smart Battery Pack 48V", "High-capacity forklift and AGV power module", "Electronics", 1250.00, 28.50, 5),
        ("prod_phar_001", "SKU-VAX-MRNA-01", "Ultra-Cold mRNA Vaccine Vials (Pack of 25)", "Requires ultra-low temp storage (-25C to -15C)", "Pharmaceuticals", 850.00, 1.50, 20),
        ("prod_phar_002", "SKU-MED-INSL-02", "Recombinant Human Insulin Solution (Box of 10)", "Temperature-controlled 2C to 8C biologics", "Pharmaceuticals", 210.00, 0.80, 30),
        ("prod_phar_003", "SKU-MED-MAB-03", "Monoclonal Antibody Oncology Treatment 100mg", "High-value targeted biologics infusion vial", "Pharmaceuticals", 1850.00, 0.50, 10),
        ("prod_ind_001", "SKU-HYDR-VLV-01", "High-Pressure Hydraulic Control Valve Assembly", "350 Bar precision hydraulic proportional valve", "Industrial", 385.00, 14.20, 12),
        ("prod_ind_002", "SKU-BEAR-150-02", "Precision Tapered Roller Bearing 150mm", "Heavy industrial machinery bearing assembly", "Industrial", 88.50, 4.80, 40),
        ("prod_ind_003", "SKU-PNEU-ACT-03", "Pneumatic Linear Actuator 500mm Stroke", "Double-acting pneumatic automation cylinder", "Industrial", 265.00, 8.75, 15),
    ]
    cur.executemany("""
        INSERT INTO products (id, sku, name, description, category, unit_price, weight_kg, reorder_threshold)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            sku = excluded.sku,
            name = excluded.name,
            description = excluded.description,
            category = excluded.category,
            unit_price = excluded.unit_price,
            weight_kg = excluded.weight_kg,
            reorder_threshold = excluded.reorder_threshold
    """, products_data)

    # -------------------------------------------------------------------------
    # 6. Seed Inventory Allocations (Boundary tests, low stock, healthy stock)
    # -------------------------------------------------------------------------
    inventory_data = [
        # Test fixture inventory in wh-01 and wh-02
        ("inv_prod01_wh01", "wh-01", "prod-01", "bin_wh01_a1", 150, 10, "BATCH-2026-01"), # Healthy: 140 available
        ("inv_prod01_wh02", "wh-02", "prod-01", "bin_wh02_a1", 80, 5, "BATCH-2026-02"),   # Healthy: 75 available
        ("inv_prod02_wh01", "wh-01", "prod-02", "bin_wh01_a2", 50, 0, "BATCH-2026-03"),   # Healthy: 50 available
        ("inv_prod_zero", "wh-01", "prod-zero", "bin_wh01_a1", 0, 0, "BATCH-ZERO-01"),     # Zero stock out-of-stock
        ("inv-east-01", "wh-01", "prod-east", "bin_wh01_a1", 20, 0, "BATCH-EAST-01"),      # Multi-warehouse test
        ("inv-west-01", "wh-02", "prod-west", "bin_wh02_a1", 20, 0, "BATCH-WEST-01"),      # Multi-warehouse test
        ("inv-rare-01", "wh-01", "prod-rare", "bin_wh01_a1", 2, 0, "BATCH-RARE-01"),       # Concurrency test
        # Explorer catalog inventory
        ("inv_001", "wh_cdc_chi", "prod_elec_001", "bin_chi_a1", 150, 10, "BATCH-2026-A1"),
        ("inv_002", "wh_cdc_chi", "prod_elec_002", "bin_chi_a2", 60, 0, "BATCH-2026-A2"),
        ("inv_003", "wh_cdc_chi", "prod_phar_001", "bin_chi_c1", 50, 20, "BATCH-VAX-01"),
        ("inv_004", "wh_cdc_chi", "prod_phar_002", "bin_chi_c1", 100, 5, "BATCH-INS-01"),
        ("inv_005", "wh_cdc_chi", "prod_ind_002", "bin_chi_a1", 200, 0, "BATCH-IND-01"),
        ("inv_006", "wh_rdc_det", "prod_elec_004", "bin_det_a1", 4, 1, "BATCH-BATT-01"),  # Low stock alert
        ("inv_007", "wh_rdc_det", "prod_ind_001", "bin_det_a2", 0, 0, "BATCH-HYD-01"),    # Out of stock
        ("inv_008", "wh_hub_cmh", "prod_phar_001", "bin_cmh_c1", 12, 0, "BATCH-VAX-02"),  # Low stock alert
        ("inv_009", "wh_hub_cmh", "prod_ind_003", "bin_cmh_a1", 30, 15, "BATCH-ACT-01"),
        ("inv_010", "wh_lsf_nyc", "prod_phar_003", "bin_nyc_c1", 0, 0, "BATCH-MAB-01"),   # Out of stock
        ("inv_011", "wh_lsf_nyc", "prod_elec_001", "bin_nyc_a1", 25, 0, "BATCH-IOT-02"),  # At reorder threshold
    ]
    cur.executemany("""
        INSERT INTO inventory (id, warehouse_id, product_id, bin_id, quantity_on_hand, quantity_reserved, batch_number)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            warehouse_id = excluded.warehouse_id,
            product_id = excluded.product_id,
            bin_id = excluded.bin_id,
            quantity_on_hand = excluded.quantity_on_hand,
            quantity_reserved = excluded.quantity_reserved,
            batch_number = excluded.batch_number
    """, inventory_data)

    # -------------------------------------------------------------------------
    # 7. Seed Network Corridors (Graph Topology between Strategic Hubs)
    # -------------------------------------------------------------------------
    corridors_data = [
        # Corridors between standard IDs
        ("corr_wh01_wh02", "wh-01", "wh-02", 455.0, 4.8, 480.00, "ROAD_FREIGHT", 35000.0, 12000.0, "ACTIVE"),
        ("corr_wh02_wh01", "wh-02", "wh-01", 455.0, 4.8, 480.00, "ROAD_FREIGHT", 35000.0, 9000.0, "ACTIVE"),
        ("corr_wh01_wh03", "wh-01", "wh-03", 570.0, 6.0, 600.00, "ROAD_FREIGHT", 40000.0, 28000.0, "ACTIVE"),
        ("corr_wh03_wh01", "wh-03", "wh-01", 570.0, 6.0, 600.00, "ROAD_FREIGHT", 40000.0, 15000.0, "ACTIVE"),
        ("corr_wh02_wh03", "wh-02", "wh-03", 325.0, 3.5, 350.00, "ROAD_FREIGHT", 30000.0, 8000.0, "ACTIVE"),
        ("corr_wh03_wh02", "wh-03", "wh-02", 325.0, 3.5, 350.00, "ROAD_FREIGHT", 30000.0, 7000.0, "ACTIVE"),
        ("corr_wh03_wh04", "wh-03", "wh-04", 865.0, 9.5, 920.00, "ROAD_FREIGHT", 35000.0, 31000.0, "ACTIVE"), # Bottleneck rho = 0.886
        ("corr_wh04_wh03", "wh-04", "wh-03", 865.0, 9.5, 920.00, "ROAD_FREIGHT", 35000.0, 14000.0, "ACTIVE"),
        ("corr_wh02_wh04", "wh-02", "wh-04", 990.0, 10.5, 1150.00, "ROAD_FREIGHT", 25000.0, 10000.0, "ACTIVE"),
        ("corr_wh04_wh02", "wh-04", "wh-02", 990.0, 10.5, 1150.00, "ROAD_FREIGHT", 25000.0, 6000.0, "ACTIVE"),
        # Corridors between alias IDs (supports Explorer test CTE queries)
        ("corr_chi_det", "wh_cdc_chi", "wh_rdc_det", 455.0, 4.8, 480.00, "ROAD_FREIGHT", 35000.0, 12000.0, "ACTIVE"),
        ("corr_det_chi", "wh_rdc_det", "wh_cdc_chi", 455.0, 4.8, 480.00, "ROAD_FREIGHT", 35000.0, 9000.0, "ACTIVE"),
        ("corr_chi_cmh", "wh_cdc_chi", "wh_hub_cmh", 570.0, 6.0, 600.00, "ROAD_FREIGHT", 40000.0, 28000.0, "ACTIVE"),
        ("corr_cmh_chi", "wh_hub_cmh", "wh_cdc_chi", 570.0, 6.0, 600.00, "ROAD_FREIGHT", 40000.0, 15000.0, "ACTIVE"),
        ("corr_det_cmh", "wh_rdc_det", "wh_hub_cmh", 325.0, 3.5, 350.00, "ROAD_FREIGHT", 30000.0, 8000.0, "ACTIVE"),
        ("corr_cmh_det", "wh_hub_cmh", "wh_rdc_det", 325.0, 3.5, 350.00, "ROAD_FREIGHT", 30000.0, 7000.0, "ACTIVE"),
        ("corr_cmh_nyc", "wh_hub_cmh", "wh_lsf_nyc", 865.0, 9.5, 920.00, "ROAD_FREIGHT", 35000.0, 31000.0, "ACTIVE"), # Bottleneck rho = 0.886
        ("corr_nyc_cmh", "wh_lsf_nyc", "wh_hub_cmh", 865.0, 9.5, 920.00, "ROAD_FREIGHT", 35000.0, 14000.0, "ACTIVE"),
        ("corr_det_nyc", "wh_rdc_det", "wh_lsf_nyc", 990.0, 10.5, 1150.00, "ROAD_FREIGHT", 25000.0, 10000.0, "ACTIVE"),
        ("corr_nyc_det", "wh_lsf_nyc", "wh_rdc_det", 990.0, 10.5, 1150.00, "ROAD_FREIGHT", 25000.0, 6000.0, "ACTIVE"),
    ]
    cur.executemany("""
        INSERT INTO network_corridors (id, source_warehouse_id, target_warehouse_id, distance_km, transit_time_hours, base_cost, transport_mode, capacity_max_kg, current_load_kg, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source_warehouse_id = excluded.source_warehouse_id,
            target_warehouse_id = excluded.target_warehouse_id,
            distance_km = excluded.distance_km,
            transit_time_hours = excluded.transit_time_hours,
            base_cost = excluded.base_cost,
            transport_mode = excluded.transport_mode,
            capacity_max_kg = excluded.capacity_max_kg,
            current_load_kg = excluded.current_load_kg,
            status = excluded.status
    """, corridors_data)

    # -------------------------------------------------------------------------
    # 8. Seed Sample Orders & Order Items
    # -------------------------------------------------------------------------
    cur.execute("""
        INSERT INTO orders (id, order_number, customer_id, status, total_amount, shipping_address, destination_city, destination_lat, destination_lon)
        VALUES ('ord_001', 'ORD-2026-1001', 'user_cust_alice', 'DELIVERED', 299.98, '350 5th Ave, New York, NY 10118', 'New York', 40.7484, -73.9857)
        ON CONFLICT(id) DO UPDATE SET
            order_number = excluded.order_number,
            customer_id = excluded.customer_id,
            status = excluded.status,
            total_amount = excluded.total_amount,
            shipping_address = excluded.shipping_address,
            destination_city = excluded.destination_city,
            destination_lat = excluded.destination_lat,
            destination_lon = excluded.destination_lon;
    """)
    cur.execute("""
        INSERT INTO order_items (id, order_id, product_id, quantity, unit_price, allocated_warehouse_id, status)
        VALUES ('oi_001', 'ord_001', 'prod_elec_001', 2, 149.99, 'wh-01', 'PACKED')
        ON CONFLICT(id) DO UPDATE SET
            order_id = excluded.order_id,
            product_id = excluded.product_id,
            quantity = excluded.quantity,
            unit_price = excluded.unit_price,
            allocated_warehouse_id = excluded.allocated_warehouse_id,
            status = excluded.status;
    """)

    cur.execute("""
        INSERT INTO orders (id, order_number, customer_id, status, total_amount, shipping_address, destination_city, destination_lat, destination_lon)
        VALUES ('ord_002', 'ORD-2026-1002', 'user_cust_bob', 'DISPATCHED', 850.00, '400 Renaissance Center, Detroit, MI 48243', 'Detroit', 42.3293, -83.0398)
        ON CONFLICT(id) DO UPDATE SET
            order_number = excluded.order_number,
            customer_id = excluded.customer_id,
            status = excluded.status,
            total_amount = excluded.total_amount,
            shipping_address = excluded.shipping_address,
            destination_city = excluded.destination_city,
            destination_lat = excluded.destination_lat,
            destination_lon = excluded.destination_lon;
    """)
    cur.execute("""
        INSERT INTO order_items (id, order_id, product_id, quantity, unit_price, allocated_warehouse_id, status)
        VALUES ('oi_002', 'ord_002', 'prod_phar_001', 1, 850.00, 'wh-01', 'ALLOCATED')
        ON CONFLICT(id) DO UPDATE SET
            order_id = excluded.order_id,
            product_id = excluded.product_id,
            quantity = excluded.quantity,
            unit_price = excluded.unit_price,
            allocated_warehouse_id = excluded.allocated_warehouse_id,
            status = excluded.status;
    """)

    cur.execute("""
        INSERT INTO orders (id, order_number, customer_id, status, total_amount, shipping_address, destination_city, destination_lat, destination_lon)
        VALUES ('ord-test-sample-01', 'ORD-TEST-0001', 'usr-test-customer-01', 'CONFIRMED', 149.99, '123 Test St, Chicago, IL', 'Chicago', 41.8781, -87.6298)
        ON CONFLICT(id) DO UPDATE SET
            order_number = excluded.order_number,
            customer_id = excluded.customer_id,
            status = excluded.status,
            total_amount = excluded.total_amount,
            shipping_address = excluded.shipping_address,
            destination_city = excluded.destination_city,
            destination_lat = excluded.destination_lat,
            destination_lon = excluded.destination_lon;
    """)
    cur.execute("""
        INSERT INTO order_items (id, order_id, product_id, quantity, unit_price, allocated_warehouse_id, status)
        VALUES ('oi-test-01', 'ord-test-sample-01', 'prod-01', 1, 149.99, 'wh-01', 'ALLOCATED')
        ON CONFLICT(id) DO UPDATE SET
            order_id = excluded.order_id,
            product_id = excluded.product_id,
            quantity = excluded.quantity,
            unit_price = excluded.unit_price,
            allocated_warehouse_id = excluded.allocated_warehouse_id,
            status = excluded.status;
    """)

    # -------------------------------------------------------------------------
    # 9. Seed Shipments & Milestones
    # -------------------------------------------------------------------------
    shipments_data = [
        ("shp_001", "TRK-CHI-NYC-8801", "ord_001", "wh-01", "Apex Freight Express", "EXPRESS", "DELIVERED", "user_driver"),
        ("shp_002", "TRK-CHI-DET-9902", "ord_002", "wh-01", "ColdChain SafeExpress", "PRIORITY", "IN_TRANSIT", "user_driver"),
        # Test fixture tracking numbers
        ("shp_test_trk01", "TRK-20261109-0081", "ord_001", "wh-01", "Apex Freight", "STANDARD", "DELIVERED", "usr-test-driver-01"),
        ("shp_test_pickup", "TRK-TEST-PICKUP-01", "ord_002", "wh-01", "DHL_EXPRESS", "EXPRESS", "CREATED", "usr-test-driver-01"),
        ("shp_test_transit", "TRK-TEST-INTRANSIT-01", "ord_002", "wh-01", "DHL_EXPRESS", "EXPRESS", "PICKED_UP", "usr-test-driver-01"),
        ("shp_test_outfordev", "TRK-TEST-OUTFORDEV-01", "ord_001", "wh-01", "DHL_EXPRESS", "EXPRESS", "IN_TRANSIT", "usr-test-driver-01"),
        ("shp_test_delivered", "TRK-TEST-DELIVERED-01", "ord_001", "wh-01", "DHL_EXPRESS", "EXPRESS", "OUT_FOR_DELIVERY", "usr-test-driver-01"),
        ("shp_cache_verify", "TRK-CACHE-VERIFY-001", "ord_002", "wh-01", "DHL_EXPRESS", "EXPRESS", "PICKED_UP", "usr-test-driver-01"),
    ]
    cur.executemany("""
        INSERT INTO shipments (id, tracking_number, order_id, origin_warehouse_id, carrier, service_level, status, assigned_driver_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            tracking_number = excluded.tracking_number,
            order_id = excluded.order_id,
            origin_warehouse_id = excluded.origin_warehouse_id,
            carrier = excluded.carrier,
            service_level = excluded.service_level,
            status = excluded.status,
            assigned_driver_id = excluded.assigned_driver_id;
    """, shipments_data)

    milestones_data = [
        ("m_001", "shp_001", "MANIFEST_CREATED", "Chicago CDC", 41.7637, -87.7410, "Order manifest created and packaged", "user_wm_chi", "2026-11-09 10:00:00"),
        ("m_002", "shp_001", "PICKUP_COMPLETED", "Chicago CDC Dock 04", 41.7637, -87.7410, "Driver picked up consignment", "user_driver", "2026-11-09 11:30:00"),
        ("m_003", "shp_001", "HUB_ARRIVAL", "Columbus Transit Cross-Dock", 39.9980, -82.8919, "Sortation scan completed at Hub", "user_dispatcher", "2026-11-09 16:00:00"),
        ("m_004", "shp_001", "OUT_FOR_DELIVERY_SCAN", "New York Sortation Facility", 40.7589, -73.8648, "Loaded onto final mile vehicle", "user_driver", "2026-11-09 20:00:00"),
        ("m_005", "shp_001", "DELIVERY_COMPLETED", "Customer Destination NY", 40.7484, -73.9857, "Delivered and signature captured", "user_driver", "2026-11-09 21:15:00"),
        ("m_006", "shp_002", "DEPARTURE_SCAN", "Chicago CDC Gate 2", 41.7637, -87.7410, "Refrigerated linehaul departed toward Detroit", "user_driver", "2026-11-09 14:00:00"),
        ("m_test_01", "shp_test_trk01", "MANIFEST_CREATED", "Chicago CDC", 41.7637, -87.7410, "Manifest created", "usr-test-admin-01", "2026-11-09 08:00:00"),
        ("m_test_02", "shp_test_trk01", "DELIVERY_COMPLETED", "New York", 40.7484, -73.9857, "Delivered successfully", "usr-test-driver-01", "2026-11-09 18:00:00"),
        ("m_cache_verify", "shp_cache_verify", "DEPARTURE_SCAN", "Chicago CDC", 41.7637, -87.7410, "Picked up for cache verification test", "usr-test-driver-01", "2026-11-09 10:00:00"),
    ]
    cur.executemany("""
        INSERT INTO shipment_milestones (id, shipment_id, milestone_type, location_name, latitude, longitude, notes, recorded_by, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            shipment_id = excluded.shipment_id,
            milestone_type = excluded.milestone_type,
            location_name = excluded.location_name,
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            notes = excluded.notes,
            recorded_by = excluded.recorded_by,
            timestamp = excluded.timestamp;
    """, milestones_data)

    # -------------------------------------------------------------------------
    # 10. Seed Sample NoSQL Documents (Telemetry, Audit Logs, Snapshots)
    # -------------------------------------------------------------------------
    nosql_data = [
        # Telemetry ping 1
        ("doc_tel_001", "shipment_telemetry", json.dumps({
            "tracking_number": "TRK-CHI-DET-9902",
            "timestamp": "2026-11-09T14:30:00Z",
            "geo": {"latitude": 41.8781, "longitude": -87.6298, "speed_kmh": 85.4},
            "environmental": {"temperature_celsius": -19.2, "humidity_percent": 35.0, "ambient_temp_celsius": 12.0},
            "cargo_health": {"shock_g_force": 0.12, "tilt_degrees": 1.5, "door_opened": False},
            "telematics": {"battery_pct": 98.5, "cellular_signal_dbm": -68}
        })),
        # Telemetry ping 2
        ("doc_tel_002", "shipment_telemetry", json.dumps({
            "tracking_number": "TRK-CHI-DET-9902",
            "timestamp": "2026-11-09T15:30:00Z",
            "geo": {"latitude": 41.7300, "longitude": -86.2500, "speed_kmh": 88.0},
            "environmental": {"temperature_celsius": -18.8, "humidity_percent": 34.5, "ambient_temp_celsius": 11.5},
            "cargo_health": {"shock_g_force": 0.18, "tilt_degrees": 2.0, "door_opened": False},
            "telematics": {"battery_pct": 96.0, "cellular_signal_dbm": -72}
        })),
        # Telemetry ping 3 (for test tracking number)
        ("doc_tel_003", "shipment_telemetry", json.dumps({
            "tracking_number": "TRK-20261109-0081",
            "timestamp": "2026-11-09T17:00:00Z",
            "geo": {"latitude": 41.8790, "longitude": -87.6310, "speed_kmh": 58.4},
            "environmental": {"temperature_celsius": 4.2, "humidity_percent": 51.0},
            "cargo_health": {"shock_g_force": 0.08, "tilt_degrees": 0.5, "door_opened": False},
            "telematics": {"battery_pct": 94.0, "cellular_signal_dbm": -70}
        })),
        # Audit Event Log
        ("doc_aud_001", "audit_event_logs", json.dumps({
            "event_id": "EVT-2026-0001",
            "action": "DATABASE_INITIALIZED",
            "actor": "user_admin",
            "role": "Admin",
            "timestamp": "2026-11-09T16:00:00Z",
            "details": {"seeded_entities": ["roles", "users", "warehouses", "bins", "products", "inventory", "corridors"]}
        })),
        # Order Snapshot
        ("doc_snp_001", "order_snapshots", json.dumps({
            "order_number": "ORD-2026-1001",
            "snapshot_timestamp": "2026-11-09T12:00:00Z",
            "customer": {"id": "user_cust_alice", "name": "Alice Smith", "email": "alice.smith@example.com"},
            "frozen_pricing": [{"sku": "SKU-IOT-GPS-01", "unit_price": 149.99, "qty": 2, "line_total": 299.98}],
            "tax_amount": 0.00,
            "shipping_terms": "EXPRESS_DELIVERY"
        }))
    ]
    cur.executemany("""
        INSERT INTO nosql_documents (id, collection, data)
        VALUES (?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            collection = excluded.collection,
            data = excluded.data
    """, nosql_data)

    conn.commit()
    logger.info("Successfully seeded demo dataset across all relational and NoSQL collections.")
    return {
        "roles": len(roles_data),
        "users": len(users_data),
        "warehouses": len(warehouses_data),
        "bins": len(bins_data),
        "products": len(products_data),
        "inventory": len(inventory_data),
        "corridors": len(corridors_data),
        "orders": 3,
        "order_items": 3,
        "shipments": len(shipments_data),
        "milestones": len(milestones_data),
        "nosql_documents": len(nosql_data)
    }


def init_database(
    seed_demo_data: bool = True,
    drop_existing: bool = False,
    db_path: Optional[str] = None,
    schema_path: Optional[str] = None
) -> sqlite3.Connection:
    """
    Initializes the SQLite database with full schema DDL and optional demo data.

    Args:
        seed_demo_data: Whether to populate realistic demo data.
        drop_existing: Whether to drop all existing tables before applying DDL.
        db_path: Optional database file path or ':memory:'.
        schema_path: Optional path to schema.sql file.

    Returns:
        Configured sqlite3.Connection instance.
    """
    from app.db.connection import (
        get_db_connection,
        set_default_db_path,
        get_default_db_path,
        _CANONICAL_DB_PATH,
        _LEGACY_DB_PATH,
    )

    target_path = db_path or get_default_db_path()

    if db_path and db_path != ":memory:":
        set_default_db_path(db_path)

    conn = get_db_connection(db_path)

    if drop_existing and (db_path or "") != ":memory:":
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys = OFF;")
        tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        for t in tables:
            cur.execute(f"DROP TABLE IF EXISTS {t[0]};")
        conn.commit()
        cur.execute("PRAGMA foreign_keys = ON;")
        logger.info("Dropped all existing tables.")

    # Execute Schema DDL
    schema_sql = get_schema_sql(schema_path)
    conn.executescript(schema_sql)
    conn.commit()
    logger.info("Executed relational and NoSQL schema DDL.")

    if seed_demo_data:
        counts = seed_demo_dataset(conn)
        logger.info(f"Seeding completed: {counts}")

    # Backwards compatibility: synchronize populated database between canonical and legacy paths
    if target_path != ":memory:":
        try:
            abs_target = os.path.abspath(target_path)
            canonical_abs = os.path.abspath(_CANONICAL_DB_PATH)
            legacy_abs = os.path.abspath(_LEGACY_DB_PATH)
            if abs_target in (canonical_abs, legacy_abs):
                other_path = legacy_abs if abs_target == canonical_abs else canonical_abs
                os.makedirs(os.path.dirname(other_path), exist_ok=True)
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                import shutil
                shutil.copyfile(abs_target, other_path)
                logger.info(f"Synchronized database to compatibility path: {other_path}")
        except Exception as sync_err:
            logger.warning(f"Could not synchronize compatibility database path: {sync_err}")

    return conn


def main():
    """CLI entry point for database initialization."""
    parser = argparse.ArgumentParser(description="Logistics IMS Database Initializer & Seeder")
    parser.add_argument("--seed", action="store_true", default=True, help="Seed realistic demo logistics dataset")
    parser.add_argument("--no-seed", dest="seed", action="store_false", help="Initialize schema without demo data")
    parser.add_argument("--drop", action="store_true", default=False, help="Drop existing tables before creating schema")
    parser.add_argument("--db-path", type=str, default=None, help="Custom SQLite database file path")
    parser.add_argument("--schema-path", type=str, default=None, help="Custom schema.sql path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    target_path = args.db_path or "default"
    print(f"Initializing Logistics IMS database (seed={args.seed}, drop={args.drop}, path={target_path})...")

    conn = init_database(
        seed_demo_data=args.seed,
        drop_existing=args.drop,
        db_path=args.db_path,
        schema_path=args.schema_path
    )
    print("Database initialized successfully!")


if __name__ == "__main__":
    main()
