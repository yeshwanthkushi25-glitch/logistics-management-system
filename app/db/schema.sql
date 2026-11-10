-- ============================================================================
-- Logistics Information Management System (Logistics IMS)
-- Relational & Multi-Model Database DDL Schema (SQLite 3.53+ Engine Specification)
-- 10 Relational Tables + Network Corridors + NoSQL Documents
-- ============================================================================

PRAGMA foreign_keys = ON;

-- ----------------------------------------------------------------------------
-- 1. Roles & Permissions Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    permissions TEXT NOT NULL, -- JSON-encoded array of permissions e.g. '["orders:create", "inventory:read"]'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ----------------------------------------------------------------------------
-- 2. Users Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role_id TEXT NOT NULL,
    full_name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE RESTRICT
);

-- ----------------------------------------------------------------------------
-- 3. Warehouses Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warehouses (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('CDC', 'RDC', 'LSF', 'HUB')),
    address TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT,
    country TEXT NOT NULL,
    latitude REAL NOT NULL CHECK (latitude BETWEEN -90.0 AND 90.0),
    longitude REAL NOT NULL CHECK (longitude BETWEEN -180.0 AND 180.0),
    capacity_sqm REAL NOT NULL CHECK (capacity_sqm > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ----------------------------------------------------------------------------
-- 4. Warehouse Physical Bins Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS warehouse_bins (
    id TEXT PRIMARY KEY,
    warehouse_id TEXT NOT NULL,
    zone TEXT NOT NULL,
    aisle TEXT NOT NULL,
    rack TEXT NOT NULL,
    shelf TEXT NOT NULL,
    bin_code TEXT NOT NULL,
    max_weight_kg REAL NOT NULL CHECK (max_weight_kg > 0),
    is_occupied INTEGER NOT NULL DEFAULT 0 CHECK (is_occupied IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (warehouse_id) REFERENCES warehouses(id) ON DELETE CASCADE,
    UNIQUE (warehouse_id, bin_code)
);

-- ----------------------------------------------------------------------------
-- 5. Products Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY,
    sku TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    category TEXT NOT NULL,
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    weight_kg REAL NOT NULL CHECK (weight_kg > 0),
    reorder_threshold INTEGER NOT NULL DEFAULT 10 CHECK (reorder_threshold >= 0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ----------------------------------------------------------------------------
-- 6. Inventory Table (Strict Mathematical Invariants & Generated Stored Column)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory (
    id TEXT PRIMARY KEY,
    warehouse_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    bin_id TEXT,
    quantity_on_hand INTEGER NOT NULL DEFAULT 0 CHECK (quantity_on_hand >= 0),
    quantity_reserved INTEGER NOT NULL DEFAULT 0 CHECK (quantity_reserved >= 0),
    quantity_available INTEGER GENERATED ALWAYS AS (quantity_on_hand - quantity_reserved) STORED,
    batch_number TEXT,
    expiry_date TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (warehouse_id) REFERENCES warehouses(id) ON DELETE RESTRICT,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT,
    FOREIGN KEY (bin_id) REFERENCES warehouse_bins(id) ON DELETE SET NULL,
    CHECK (quantity_on_hand >= quantity_reserved),
    UNIQUE (warehouse_id, product_id, batch_number)
);

-- ----------------------------------------------------------------------------
-- 7. Orders Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    order_number TEXT UNIQUE NOT NULL,
    customer_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'CONFIRMED', 'ALLOCATED', 'DISPATCHED', 'DELIVERED', 'CANCELLED', 'REFUNDED'
    )),
    total_amount REAL NOT NULL CHECK (total_amount >= 0),
    shipping_address TEXT NOT NULL,
    destination_city TEXT NOT NULL,
    destination_lat REAL NOT NULL CHECK (destination_lat BETWEEN -90.0 AND 90.0),
    destination_lon REAL NOT NULL CHECK (destination_lon BETWEEN -180.0 AND 180.0),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (customer_id) REFERENCES users(id) ON DELETE RESTRICT
);

-- ----------------------------------------------------------------------------
-- 8. Order Items Table
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_items (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    allocated_warehouse_id TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'ALLOCATED', 'PICKED', 'PACKED', 'CANCELLED'
    )),
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE RESTRICT,
    FOREIGN KEY (allocated_warehouse_id) REFERENCES warehouses(id) ON DELETE RESTRICT
);

-- ----------------------------------------------------------------------------
-- 9. Shipments Table (7-State FSM)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shipments (
    id TEXT PRIMARY KEY,
    tracking_number TEXT UNIQUE NOT NULL,
    order_id TEXT NOT NULL,
    origin_warehouse_id TEXT NOT NULL,
    carrier TEXT NOT NULL,
    service_level TEXT NOT NULL DEFAULT 'STANDARD' CHECK (service_level IN (
        'STANDARD', 'EXPRESS', 'PRIORITY', 'SAME_DAY'
    )),
    status TEXT NOT NULL DEFAULT 'CREATED' CHECK (status IN (
        'CREATED', 'PICKED_UP', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION', 'RETURNED'
    )),
    estimated_delivery TIMESTAMP,
    actual_delivery TIMESTAMP,
    dispatched_at TIMESTAMP,
    current_lat REAL CHECK (current_lat IS NULL OR current_lat BETWEEN -90.0 AND 90.0),
    current_lon REAL CHECK (current_lon IS NULL OR current_lon BETWEEN -180.0 AND 180.0),
    assigned_driver_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE RESTRICT,
    FOREIGN KEY (origin_warehouse_id) REFERENCES warehouses(id) ON DELETE RESTRICT,
    FOREIGN KEY (assigned_driver_id) REFERENCES users(id) ON DELETE SET NULL
);

-- ----------------------------------------------------------------------------
-- 10. Shipment Milestones Table (10 Milestone Types)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shipment_milestones (
    id TEXT PRIMARY KEY,
    shipment_id TEXT NOT NULL,
    milestone_type TEXT NOT NULL CHECK (milestone_type IN (
        'MANIFEST_CREATED', 'PICKUP_COMPLETED', 'DEPARTURE_SCAN', 'HUB_ARRIVAL',
        'CUSTOMS_CLEARED', 'SORTATION_COMPLETED', 'OUT_FOR_DELIVERY_SCAN',
        'DELIVERY_ATTEMPTED', 'DELIVERY_COMPLETED', 'EXCEPTION'
    )),
    location_name TEXT NOT NULL,
    latitude REAL CHECK (latitude IS NULL OR latitude BETWEEN -90.0 AND 90.0),
    longitude REAL CHECK (longitude IS NULL OR longitude BETWEEN -180.0 AND 180.0),
    notes TEXT,
    recorded_by TEXT,
    timestamp TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (shipment_id) REFERENCES shipments(id) ON DELETE CASCADE,
    FOREIGN KEY (recorded_by) REFERENCES users(id) ON DELETE SET NULL
);

-- ----------------------------------------------------------------------------
-- 11. Network Corridors Table (Graph Database Topology)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS network_corridors (
    id TEXT PRIMARY KEY,
    source_warehouse_id TEXT NOT NULL,
    target_warehouse_id TEXT NOT NULL,
    distance_km REAL NOT NULL CHECK (distance_km > 0),
    transit_time_hours REAL NOT NULL CHECK (transit_time_hours > 0),
    base_cost REAL NOT NULL CHECK (base_cost >= 0),
    transport_mode TEXT NOT NULL DEFAULT 'ROAD_FREIGHT' CHECK (transport_mode IN (
        'ROAD_FREIGHT', 'AIR_FREIGHT', 'RAIL_FREIGHT', 'OCEAN_FREIGHT'
    )),
    capacity_max_kg REAL NOT NULL CHECK (capacity_max_kg > 0),
    current_load_kg REAL NOT NULL DEFAULT 0.0 CHECK (current_load_kg >= 0),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
        'ACTIVE', 'CONGESTED', 'CLOSED', 'MAINTENANCE'
    )),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_warehouse_id) REFERENCES warehouses(id) ON DELETE RESTRICT,
    FOREIGN KEY (target_warehouse_id) REFERENCES warehouses(id) ON DELETE RESTRICT
);

-- ----------------------------------------------------------------------------
-- 12. NoSQL Documents Table (SQLite JSON1 Document Store)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS nosql_documents (
    id TEXT PRIMARY KEY,
    collection TEXT NOT NULL,
    data JSON NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Explicit Performance Indexes
-- ============================================================================
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE INDEX IF NOT EXISTS idx_warehouse_bins_wh ON warehouse_bins(warehouse_id);

CREATE INDEX IF NOT EXISTS idx_inventory_lookup ON inventory(warehouse_id, product_id);
CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory(product_id);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_order_items_product ON order_items(product_id);

CREATE INDEX IF NOT EXISTS idx_shipments_tracking ON shipments(tracking_number);
CREATE INDEX IF NOT EXISTS idx_shipments_order ON shipments(order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(status);

CREATE INDEX IF NOT EXISTS idx_milestones_shipment_time ON shipment_milestones(shipment_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_corridors_source ON network_corridors(source_warehouse_id);
CREATE INDEX IF NOT EXISTS idx_corridors_target ON network_corridors(target_warehouse_id);

CREATE INDEX IF NOT EXISTS idx_nosql_collection ON nosql_documents(collection);
CREATE INDEX IF NOT EXISTS idx_nosql_collection_created ON nosql_documents(collection, created_at);
