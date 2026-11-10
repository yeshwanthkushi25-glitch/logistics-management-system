"""
Logistics IMS - Transactional Order Fulfillment Service.

Enforces:
1. Strict ACID transactional boundaries across orders, order_items, inventory, shipments, and NoSQL snapshots.
2. Multi-warehouse stock allocation and atomic reservation holds.
3. Clean rollback semantics on stockouts, payment declines, or unexpected process faults with zero orphaned records.
4. Idempotency enforcement via cache and NoSQL document snapshots.
5. Pre-dispatch order cancellation with complete stock reservation release.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from app.db.connection import get_db_connection, transaction_scope
from app.db.cache import get_cache_manager
from app.db.nosql import DocumentStore
from app.services.inventory_service import (
    allocate_inventory,
    release_inventory,
    InsufficientStockException,
)


# =============================================================================
# Domain Exceptions
# =============================================================================

class FulfillmentException(Exception):
    """Base exception for order fulfillment errors."""
    pass


class PaymentFailedException(FulfillmentException):
    """Raised when payment gateway declines or simulated payment failure is requested."""
    pass


class OrderNotFoundException(FulfillmentException):
    """Raised when an order cannot be found by ID or order number."""
    pass


class OrderStateTransitionException(FulfillmentException):
    """Raised when an illegal order lifecycle transition is attempted."""
    pass


# =============================================================================
# Core Fulfillment Service Operations
# =============================================================================

def create_order_atomic(
    customer_id: str,
    items: List[Dict[str, Any]],
    shipping_address: Union[str, Dict[str, Any]],
    destination_city: Optional[str] = None,
    destination_lat: Optional[float] = None,
    destination_lon: Optional[float] = None,
    simulate_failure: bool = False,
    simulate_payment_failure: bool = False,
    idempotency_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Executes atomic, multi-warehouse order placement within an ACID transaction scope.

    Lifecycle Steps:
    1. Check Idempotency: If key exists in cache or NoSQL snapshot store, return existing order.
    2. Input Validation: Validate customer, items, and shipping destination parameters.
    3. Begin ACID Transaction: Acquire IMMEDIATE exclusive lock via transaction_scope.
    4. Multi-Warehouse Stock Allocation: Identify optimal stocking warehouses and atomically reserve inventory.
    5. Insufficient Stock Abort: If any item is unavailable, raise InsufficientStockException (clean rollback).
    6. Payment Processing: If simulated failure is requested, raise PaymentFailedException (clean rollback).
    7. Orders Record: Insert relational order record with status 'CONFIRMED'.
    8. Order Items Records: Insert relational line items linked to allocated warehouses with status 'ALLOCATED'.
    9. Shipment Stubs: Create independent shipment manifests with status 'CREATED' per sourcing warehouse.
    10. NoSQL Snapshot: Store point-in-time frozen checkout document in 'order_snapshots' collection.
    11. Transaction Commit: Auto-commit on clean context exit.
    12. Cache Invalidation & Return: Purge affected inventory caches, cache idempotency response, and return.

    Args:
        customer_id: Authenticated customer user ID.
        items: List of line items [{"product_id": str, "quantity": int, ...}].
        shipping_address: String or dict destination address.
        destination_city: Destination city name.
        destination_lat: Destination latitude (-90 to 90).
        destination_lon: Destination longitude (-180 to 180).
        simulate_failure: When True, aborts after allocation to test payment rollback.
        simulate_payment_failure: Explicit alias for simulated payment failure.
        idempotency_key: Client-provided idempotency token (e.g. UUID).

    Returns:
        Structured order payload with order_id, status, items, and shipment tracking numbers.
    """
    cache = get_cache_manager()

    # -------------------------------------------------------------------------
    # 1. Idempotency Check
    # -------------------------------------------------------------------------
    if idempotency_key:
        cached_order = cache.get(f"idempotency:{idempotency_key}")
        if cached_order is not None:
            return cached_order

        # Fallback check against NoSQL order snapshots collection
        doc_store = DocumentStore()
        existing_snap = doc_store.find_one("order_snapshots", {"idempotency_key": idempotency_key})
        if existing_snap:
            order_id = existing_snap.get("order_id")
            if order_id:
                existing_order = get_order(order_id)
                cache.set(f"idempotency:{idempotency_key}", existing_order, ttl_seconds=86400)
                return existing_order

    # -------------------------------------------------------------------------
    # 2. Input Validation
    # -------------------------------------------------------------------------
    if not items or not isinstance(items, list):
        raise ValueError("Order must contain at least one line item.")

    for itm in items:
        if not itm.get("product_id"):
            raise ValueError("Each order item must specify a product_id.")
        qty = itm.get("quantity", 0)
        if not isinstance(qty, int) or qty <= 0:
            raise ValueError(f"Item quantity must be a positive integer, got {qty}")

    # Normalize destination address
    if isinstance(shipping_address, dict):
        addr_str = shipping_address.get("street") or shipping_address.get("address") or json.dumps(shipping_address)
        city = destination_city or shipping_address.get("city") or "Chicago"
        lat = destination_lat if destination_lat is not None else float(shipping_address.get("lat", 41.8781))
        lon = destination_lon if destination_lon is not None else float(shipping_address.get("lon", -87.6298))
    else:
        addr_str = str(shipping_address).strip()
        city = destination_city or "Chicago"
        lat = destination_lat if destination_lat is not None else 41.8781
        lon = destination_lon if destination_lon is not None else -87.6298

    # -------------------------------------------------------------------------
    # 3. ACID Transaction Execution
    # -------------------------------------------------------------------------
    with transaction_scope(isolation="IMMEDIATE") as conn:
        cur = conn.cursor()

        # Step 3.0: Double-Checked Idempotency Lock inside BEGIN IMMEDIATE
        if idempotency_key:
            doc_store_tx = DocumentStore(conn=conn)
            existing_snap = doc_store_tx.find_one("order_snapshots", {"idempotency_key": idempotency_key})
            if existing_snap:
                order_id = existing_snap.get("order_id")
                if order_id:
                    existing_order = get_order(order_id, conn=conn)
                    cache.set(f"idempotency:{idempotency_key}", existing_order, ttl_seconds=86400)
                    return existing_order

        # Step 3a. Validate customer exists in users table
        cur.execute("SELECT id FROM users WHERE id = ?", (customer_id,))
        cust_row = cur.fetchone()
        if not cust_row:
            raise ValueError(f"Customer with ID '{customer_id}' does not exist.")

        # Step 3b. Multi-Warehouse Stock Allocation & Reservation
        allocated_items: List[Dict[str, Any]] = []
        total_amount: float = 0.0
        distinct_warehouses: set[str] = set()

        for itm in items:
            prod_id = itm["product_id"]
            req_qty = itm["quantity"]
            preferred_wh = itm.get("warehouse_id") or itm.get("allocated_warehouse_id")

            # Fetch product metadata from catalog
            cur.execute("""
                SELECT id, name, sku, unit_price 
                FROM products 
                WHERE id = ?
            """, (prod_id,))
            prod_row = cur.fetchone()

            if not prod_row:
                raise InsufficientStockException(f"Product '{prod_id}' not found in catalog.")

            unit_price = float(itm.get("unit_price") or prod_row["unit_price"])
            line_total = round(unit_price * req_qty, 2)
            total_amount += line_total

            # Sourcing Warehouse Selection
            if preferred_wh:
                # Sourcing constrained to specific warehouse
                cur.execute("""
                    SELECT warehouse_id, quantity_available 
                    FROM inventory 
                    WHERE warehouse_id = ? AND product_id = ?
                """, (preferred_wh, prod_id))
                wh_stock = cur.fetchone()
                if not wh_stock or wh_stock["quantity_available"] < req_qty:
                    avail = wh_stock["quantity_available"] if wh_stock else 0
                    raise InsufficientStockException(
                        f"Warehouse '{preferred_wh}' has insufficient stock for '{prod_id}': "
                        f"requested {req_qty}, available {avail}"
                    )
                chosen_wh = preferred_wh
            else:
                # Sourcing candidate: warehouse with highest available stock >= req_qty
                cur.execute("""
                    SELECT warehouse_id, quantity_available 
                    FROM inventory 
                    WHERE product_id = ? AND quantity_available >= ?
                    ORDER BY quantity_available DESC
                    LIMIT 1
                """, (prod_id, req_qty))
                candidate = cur.fetchone()

                if not candidate:
                    # Check aggregate stock across all warehouses for descriptive diagnostics
                    cur.execute("""
                        SELECT SUM(quantity_available) AS total_avail 
                        FROM inventory 
                        WHERE product_id = ?
                    """, (prod_id,))
                    tot_row = cur.fetchone()
                    tot_avail = tot_row["total_avail"] if (tot_row and tot_row["total_avail"] is not None) else 0
                    raise InsufficientStockException(
                        f"Insufficient available stock for product '{prod_id}': "
                        f"requested {req_qty}, total across all warehouses is {tot_avail}."
                    )
                chosen_wh = candidate["warehouse_id"]

            # Step 3c. Atomically reserve inventory hold
            allocate_inventory(
                warehouse_id=chosen_wh,
                product_id=prod_id,
                quantity=req_qty,
                conn=conn
            )

            distinct_warehouses.add(chosen_wh)
            allocated_items.append({
                "id": f"oi_{uuid.uuid4().hex[:12]}",
                "product_id": prod_id,
                "product_name": prod_row["name"],
                "product_sku": prod_row["sku"],
                "quantity": req_qty,
                "unit_price": unit_price,
                "line_total": line_total,
                "allocated_warehouse_id": chosen_wh,
                "status": "ALLOCATED"
            })

        total_amount = round(total_amount, 2)

        # Step 3d. Simulated Payment Processing / Transaction Rollback Check
        if simulate_failure or simulate_payment_failure:
            raise PaymentFailedException("Payment processing failed: Card declined or payment gateway unavailable.")

        # Step 3e. Insert orders record
        order_id = f"ord_{uuid.uuid4().hex[:12]}"
        order_number = f"ORD-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        order_status = "CONFIRMED"

        cur.execute("""
            INSERT INTO orders (
                id, order_number, customer_id, status, total_amount, 
                shipping_address, destination_city, destination_lat, destination_lon
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id, order_number, customer_id, order_status, total_amount,
            addr_str, city, lat, lon
        ))

        # Step 3f. Insert order_items records
        for ai in allocated_items:
            cur.execute("""
                INSERT INTO order_items (
                    id, order_id, product_id, quantity, unit_price, allocated_warehouse_id, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                ai["id"], order_id, ai["product_id"], ai["quantity"],
                ai["unit_price"], ai["allocated_warehouse_id"], ai["status"]
            ))

        # Step 3g. Insert shipment manifest stubs (one per distinct sourcing warehouse)
        shipments_list: List[Dict[str, Any]] = []
        for wh_id in sorted(distinct_warehouses):
            shipment_id = f"shp_{uuid.uuid4().hex[:12]}"
            tracking_number = f"TRK-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
            carrier = "Apex Freight Express"
            service_level = "STANDARD"
            shipment_status = "CREATED"

            cur.execute("""
                INSERT INTO shipments (
                    id, tracking_number, order_id, origin_warehouse_id, carrier, service_level, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                shipment_id, tracking_number, order_id, wh_id, carrier, service_level, shipment_status
            ))

            # Record initial milestone checkpoint
            milestone_id = f"ms_{uuid.uuid4().hex[:12]}"
            cur.execute("""
                INSERT INTO shipment_milestones (
                    id, shipment_id, milestone_type, location_name, latitude, longitude, notes, recorded_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                milestone_id, shipment_id, "MANIFEST_CREATED",
                f"Warehouse {wh_id} Outbound Cross-Dock", lat, lon,
                "Shipment manifest generated upon order confirmation", customer_id
            ))

            shipments_list.append({
                "id": shipment_id,
                "tracking_number": tracking_number,
                "origin_warehouse_id": wh_id,
                "carrier": carrier,
                "service_level": service_level,
                "status": shipment_status
            })

        # Step 3h. Insert NoSQL order_snapshots document (bound to current transaction conn)
        doc_store = DocumentStore(conn=conn)
        snapshot_payload = {
            "_id": f"snp_{order_id}",
            "order_id": order_id,
            "order_number": order_number,
            "idempotency_key": idempotency_key,
            "customer_id": customer_id,
            "status": order_status,
            "total_amount": total_amount,
            "shipping_address": addr_str,
            "destination": {"city": city, "lat": lat, "lon": lon},
            "items": [
                {
                    "item_id": ai["id"],
                    "product_id": ai["product_id"],
                    "sku": ai["product_sku"],
                    "name": ai["product_name"],
                    "quantity": ai["quantity"],
                    "unit_price": ai["unit_price"],
                    "line_total": ai["line_total"],
                    "allocated_warehouse_id": ai["allocated_warehouse_id"]
                }
                for ai in allocated_items
            ],
            "shipments": [s["tracking_number"] for s in shipments_list],
            "snapshot_timestamp": datetime.now(timezone.utc).isoformat()
        }
        doc_store.insert("order_snapshots", snapshot_payload)

    # -------------------------------------------------------------------------
    # 4. Post-Commit Cache Invalidation & Idempotency Storage
    # -------------------------------------------------------------------------
    cache.invalidate_pattern("inventory:*")
    cache.invalidate_pattern("orders:*")
    cache.invalidate_pattern("order:*")

    result_payload: Dict[str, Any] = {
        "order_id": order_id,
        "id": order_id,
        "order_number": order_number,
        "customer_id": customer_id,
        "status": order_status,
        "total_amount": total_amount,
        "shipping_address": addr_str,
        "destination_city": city,
        "destination_lat": lat,
        "destination_lon": lon,
        "items": allocated_items,
        "shipments": shipments_list,
        "tracking_numbers": [s["tracking_number"] for s in shipments_list],
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    if idempotency_key:
        cache.set(f"idempotency:{idempotency_key}", result_payload, ttl_seconds=86400)

    return result_payload


def cancel_order(
    order_id: str,
    actor_id: Optional[str] = None,
    reason: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Cancels an order pre-dispatch and restores reserved inventory holds.

    Guarantees:
    - Only pre-dispatch orders ('PENDING', 'CONFIRMED', 'ALLOCATED') can be cancelled.
    - Releases all held reservations across all line items back to available inventory.
    - Transitions order status to 'CANCELLED'.
    - Updates shipments status to 'EXCEPTION'.
    - Writes immutable audit log to 'audit_event_logs'.

    Args:
        order_id: Unique order ID.
        actor_id: User ID requesting cancellation.
        reason: Optional explanation.
        conn: Optional active database connection.

    Returns:
        Cancellation confirmation dictionary.
    """
    def _execute_cancel(c: sqlite3.Connection) -> Dict[str, Any]:
        cur = c.execute("""
            SELECT id, order_number, customer_id, status 
            FROM orders 
            WHERE id = ?
        """, (order_id,))
        order_row = cur.fetchone()

        if not order_row:
            raise OrderNotFoundException(f"Order '{order_id}' does not exist.")

        current_status = order_row["status"]
        if current_status not in ("PENDING", "CONFIRMED", "ALLOCATED"):
            raise OrderStateTransitionException(
                f"Cannot cancel order in state '{current_status}'. Only pre-dispatch orders can be cancelled."
            )

        # Retrieve order items to release reservations
        cur.execute("""
            SELECT id, product_id, quantity, allocated_warehouse_id, status 
            FROM order_items 
            WHERE order_id = ?
        """, (order_id,))
        items = cur.fetchall()

        for item in items:
            wh_id = item["allocated_warehouse_id"]
            prod_id = item["product_id"]
            qty = item["quantity"]
            if wh_id and item["status"] != "CANCELLED":
                release_inventory(
                    warehouse_id=wh_id,
                    product_id=prod_id,
                    quantity=qty,
                    conn=c
                )
                c.execute("UPDATE order_items SET status = 'CANCELLED' WHERE id = ?", (item["id"],))

        # Update order status
        c.execute("""
            UPDATE orders 
            SET status = 'CANCELLED', updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        """, (order_id,))

        # Update associated shipments
        c.execute("""
            UPDATE shipments 
            SET status = 'EXCEPTION', updated_at = CURRENT_TIMESTAMP 
            WHERE order_id = ?
        """, (order_id,))

        # Audit event log
        doc_store = DocumentStore(conn=c)
        audit_doc = {
            "event_id": f"EVT-ORD-CANCEL-{uuid.uuid4().hex[:8].upper()}",
            "action": "ORDER_CANCELLED",
            "actor_id": actor_id or order_row["customer_id"],
            "order_id": order_id,
            "order_number": order_row["order_number"],
            "reason": reason or "Customer requested pre-dispatch cancellation",
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        doc_store.insert("audit_event_logs", audit_doc)

        # Cache invalidation
        cache = get_cache_manager()
        cache.invalidate_pattern("inventory:*")
        cache.invalidate_pattern("orders:*")
        cache.invalidate_pattern("order:*")
        cache.delete(f"orders:{order_id}")
        cache.delete(f"order:{order_id}")

        return {
            "status": "CANCELLED",
            "order_id": order_id,
            "order_number": order_row["order_number"],
            "released_items_count": len(items)
        }

    if conn is not None:
        return _execute_cancel(conn)
    else:
        with transaction_scope(isolation="IMMEDIATE") as auto_conn:
            return _execute_cancel(auto_conn)


def get_order(
    order_id: str,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Retrieves full details for a single order including items and shipments.
    Employs Cache-Aside caching with TTL 60 seconds.
    """
    cache = get_cache_manager()
    cache_key = f"orders:{order_id}"

    if conn is None:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    active_conn = conn or get_db_connection()
    cur = active_conn.cursor()

    cur.execute("""
        SELECT 
            id, order_number, customer_id, status, total_amount,
            shipping_address, destination_city, destination_lat, destination_lon,
            created_at, updated_at
        FROM orders 
        WHERE id = ?
    """, (order_id,))
    order_row = cur.fetchone()

    if not order_row:
        raise OrderNotFoundException(f"Order '{order_id}' not found.")

    cur.execute("""
        SELECT 
            oi.id, oi.product_id, p.name AS product_name, p.sku AS product_sku,
            oi.quantity, oi.unit_price, oi.allocated_warehouse_id, oi.status
        FROM order_items oi
        JOIN products p ON oi.product_id = p.id
        WHERE oi.order_id = ?
    """, (order_id,))
    items_rows = cur.fetchall()

    cur.execute("""
        SELECT 
            id, tracking_number, origin_warehouse_id, carrier, service_level, status
        FROM shipments 
        WHERE order_id = ?
    """, (order_id,))
    shipment_rows = cur.fetchall()

    payload: Dict[str, Any] = {
        "id": order_row["id"],
        "order_id": order_row["id"],
        "order_number": order_row["order_number"],
        "customer_id": order_row["customer_id"],
        "status": order_row["status"],
        "total_amount": float(order_row["total_amount"]),
        "shipping_address": order_row["shipping_address"],
        "destination_city": order_row["destination_city"],
        "destination_lat": order_row["destination_lat"],
        "destination_lon": order_row["destination_lon"],
        "created_at": order_row["created_at"],
        "updated_at": order_row["updated_at"],
        "items": [
            {
                "id": r["id"],
                "product_id": r["product_id"],
                "product_name": r["product_name"],
                "product_sku": r["product_sku"],
                "quantity": r["quantity"],
                "unit_price": float(r["unit_price"]),
                "allocated_warehouse_id": r["allocated_warehouse_id"],
                "status": r["status"]
            }
            for r in items_rows
        ],
        "shipments": [
            {
                "id": s["id"],
                "tracking_number": s["tracking_number"],
                "origin_warehouse_id": s["origin_warehouse_id"],
                "carrier": s["carrier"],
                "service_level": s["service_level"],
                "status": s["status"]
            }
            for s in shipment_rows
        ]
    }

    if conn is None:
        cache.set(cache_key, payload, ttl_seconds=60)

    return payload


def list_orders(
    customer_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    conn: Optional[sqlite3.Connection] = None
) -> List[Dict[str, Any]]:
    """Lists orders matching optional customer or status filters."""
    active_conn = conn or get_db_connection()
    sql = """
        SELECT id, order_number, customer_id, status, total_amount, destination_city, created_at
        FROM orders
        WHERE 1 = 1
    """
    params: List[Any] = []

    if customer_id:
        sql += " AND customer_id = ?"
        params.append(customer_id)

    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cur = active_conn.execute(sql, params)
    rows = cur.fetchall()

    return [
        {
            "id": r["id"],
            "order_id": r["id"],
            "order_number": r["order_number"],
            "customer_id": r["customer_id"],
            "status": r["status"],
            "total_amount": float(r["total_amount"]),
            "destination_city": r["destination_city"],
            "created_at": r["created_at"]
        }
        for r in rows
    ]
