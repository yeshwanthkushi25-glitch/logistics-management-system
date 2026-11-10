"""
Logistics IMS - Multi-Warehouse Inventory Management Service.

Enforces:
1. Mathematical invariants: Q_available = Q_on_hand - Q_reserved >= 0 at both application and database layers.
2. Atomic inventory allocation and reservation holds.
3. Inventory restock and cycle count adjustments with NoSQL immutable audit event logging.
4. Cache-Aside read acceleration with automatic glob pattern invalidation.
5. Reorder Point (ROP = d * L + SS) calculation and automated low-stock alerting.
"""

import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from app.db.connection import get_db_connection, transaction_scope
from app.db.cache import get_cache_manager
from app.db.nosql import DocumentStore


# =============================================================================
# Custom Domain Exceptions
# =============================================================================

class InventoryException(Exception):
    """Base exception for inventory domain errors."""
    pass


class InsufficientStockException(InventoryException):
    """Raised when available stock is insufficient to fulfill allocation or adjustment."""
    pass


class ProductNotFoundException(InventoryException):
    """Raised when a product record does not exist in catalog or warehouse."""
    pass


class WarehouseNotFoundException(InventoryException):
    """Raised when a warehouse record does not exist."""
    pass


class InvalidInventoryOperationException(InventoryException):
    """Raised when an operation would violate invariants or quantity parameters."""
    pass


# =============================================================================
# Mathematical Formulas & Business Logic
# =============================================================================

def calculate_reorder_point(
    daily_demand: float,
    lead_time_days: float,
    safety_stock: float = 0.0
) -> int:
    """
    Calculates the automated Reorder Point (ROP) using the standard supply chain formula:
        ROP = (d * L) + SS
    where:
        d = average daily demand / usage rate
        L = replenishment lead time in days
        SS = safety stock threshold for demand volatility buffer

    Returns:
        Integer reorder point (ceiling rounded).
    """
    if daily_demand < 0 or lead_time_days < 0 or safety_stock < 0:
        raise ValueError("Demand, lead time, and safety stock parameters must be non-negative.")
    
    rop_val = (daily_demand * lead_time_days) + safety_stock
    return int(math.ceil(rop_val))


# =============================================================================
# Core Inventory Service Operations
# =============================================================================

def get_inventory_levels(
    warehouse_id: Optional[str] = None,
    product_id: Optional[str] = None,
    low_stock_only: bool = False,
    conn: Optional[sqlite3.Connection] = None
) -> List[Dict[str, Any]]:
    """
    Retrieves current stock levels across warehouses using the Cache-Aside pattern.

    Args:
        warehouse_id: Optional warehouse ID filter.
        product_id: Optional product ID filter.
        low_stock_only: When True, filters records where quantity_available <= reorder_threshold.
        conn: Optional active database connection (skips cache if in an active transaction).

    Returns:
        List of inventory records with product and warehouse metadata.
    """
    cache = get_cache_manager()
    cache_key = f"inventory:levels:{warehouse_id or 'all'}:{product_id or 'all'}:{1 if low_stock_only else 0}"

    # Cache read (only when not bound to an active mutation connection)
    if conn is None:
        cached_result = cache.get(cache_key)
        if cached_result is not None:
            return cached_result

    active_conn = conn or get_db_connection()

    sql = """
        SELECT 
            i.id,
            i.warehouse_id,
            w.code AS warehouse_code,
            w.name AS warehouse_name,
            i.product_id,
            p.sku AS product_sku,
            p.name AS product_name,
            p.category AS product_category,
            p.unit_price,
            p.reorder_threshold,
            i.bin_id,
            b.bin_code,
            i.quantity_on_hand,
            i.quantity_reserved,
            i.quantity_available,
            i.batch_number,
            i.expiry_date,
            i.version,
            i.updated_at
        FROM inventory i
        JOIN products p ON i.product_id = p.id
        JOIN warehouses w ON i.warehouse_id = w.id
        LEFT JOIN warehouse_bins b ON i.bin_id = b.id
        WHERE 1 = 1
    """
    params: List[Any] = []

    if warehouse_id:
        sql += " AND i.warehouse_id = ?"
        params.append(warehouse_id)

    if product_id:
        sql += " AND i.product_id = ?"
        params.append(product_id)

    if low_stock_only:
        sql += " AND i.quantity_available <= p.reorder_threshold"

    sql += " ORDER BY i.warehouse_id, p.name"

    cur = active_conn.execute(sql, params)
    rows = cur.fetchall()

    result: List[Dict[str, Any]] = []
    for row in rows:
        item = {
            "id": row["id"],
            "warehouse_id": row["warehouse_id"],
            "warehouse_code": row["warehouse_code"],
            "warehouse_name": row["warehouse_name"],
            "product_id": row["product_id"],
            "product_sku": row["product_sku"],
            "product_name": row["product_name"],
            "product_category": row["product_category"],
            "unit_price": float(row["unit_price"]),
            "reorder_threshold": row["reorder_threshold"],
            "bin_id": row["bin_id"],
            "bin_code": row["bin_code"],
            "quantity_on_hand": row["quantity_on_hand"],
            "quantity_reserved": row["quantity_reserved"],
            "quantity_available": row["quantity_available"],
            "is_low_stock": (row["quantity_available"] <= row["reorder_threshold"]),
            "batch_number": row["batch_number"],
            "expiry_date": row["expiry_date"],
            "version": row["version"],
            "updated_at": row["updated_at"]
        }
        result.append(item)

    if conn is None:
        cache.set(cache_key, result, ttl_seconds=300)

    return result


def allocate_inventory(
    warehouse_id: str,
    product_id: str,
    quantity: int,
    conn: Optional[sqlite3.Connection] = None
) -> bool:
    """
    Atomically reserves inventory for an order fulfillment hold.

    Increments quantity_reserved while enforcing the mathematical invariant:
        quantity_on_hand - quantity_reserved >= quantity

    Args:
        warehouse_id: Sourcing warehouse ID.
        product_id: SKU product ID.
        quantity: Units to reserve (must be > 0).
        conn: Optional active transaction connection.

    Raises:
        ValueError: If quantity <= 0.
        InsufficientStockException: If available stock is less than requested quantity.

    Returns:
        True upon successful reservation.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError(f"Reservation quantity must be a positive integer, got {quantity}")

    def _execute_allocation(c: sqlite3.Connection) -> bool:
        # Atomic conditional update enforcing available stock invariant
        cur = c.execute("""
            UPDATE inventory
            SET quantity_reserved = quantity_reserved + ?,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (
                SELECT id FROM inventory 
                WHERE warehouse_id = ? 
                  AND product_id = ? 
                  AND (quantity_on_hand - quantity_reserved) >= ?
                ORDER BY quantity_available DESC 
                LIMIT 1
            )
        """, (quantity, warehouse_id, product_id, quantity))

        if cur.rowcount == 0:
            # Query existing row to provide precise exception detail
            check_cur = c.execute("""
                SELECT quantity_on_hand, quantity_reserved, quantity_available 
                FROM inventory 
                WHERE warehouse_id = ? AND product_id = ?
            """, (warehouse_id, product_id))
            row = check_cur.fetchone()

            if not row:
                raise InsufficientStockException(
                    f"No inventory record found for product '{product_id}' in warehouse '{warehouse_id}'."
                )
            
            avail = row["quantity_available"]
            raise InsufficientStockException(
                f"Insufficient stock for product '{product_id}' in warehouse '{warehouse_id}': "
                f"requested {quantity}, available {avail} (on_hand={row['quantity_on_hand']}, reserved={row['quantity_reserved']})."
            )

        # Invalidate inventory caches
        cache = get_cache_manager()
        cache.invalidate_pattern("inventory:*")
        return True

    if conn is not None:
        return _execute_allocation(conn)
    else:
        with transaction_scope(isolation="IMMEDIATE") as auto_conn:
            return _execute_allocation(auto_conn)


def release_inventory(
    warehouse_id: str,
    product_id: str,
    quantity: int,
    conn: Optional[sqlite3.Connection] = None
) -> None:
    """
    Atomically releases a previously held inventory reservation.

    Decrements quantity_reserved (bounded at zero).

    Args:
        warehouse_id: Warehouse ID.
        product_id: SKU product ID.
        quantity: Units to release (must be > 0).
        conn: Optional active transaction connection.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError(f"Release quantity must be a positive integer, got {quantity}")

    def _execute_release(c: sqlite3.Connection) -> None:
        cur = c.execute("""
            UPDATE inventory
            SET quantity_reserved = MAX(0, quantity_reserved - ?),
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE warehouse_id = ? AND product_id = ?
        """, (quantity, warehouse_id, product_id))

        if cur.rowcount == 0:
            raise ProductNotFoundException(
                f"No inventory record found to release for product '{product_id}' in warehouse '{warehouse_id}'."
            )

        cache = get_cache_manager()
        cache.invalidate_pattern("inventory:*")

    if conn is not None:
        _execute_release(conn)
    else:
        with transaction_scope(isolation="IMMEDIATE") as auto_conn:
            _execute_release(auto_conn)


def restock_inventory(
    warehouse_id: str,
    product_id: str,
    quantity: int,
    batch_no: Optional[str] = None,
    bin_code: Optional[str] = None,
    actor_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Atomically increments on-hand inventory, updates location/batch, writes an immutable
    NoSQL audit event log, and invalidates inventory cache keys.

    Args:
        warehouse_id: Target warehouse ID.
        product_id: SKU product ID.
        quantity: Units to add (must be > 0).
        batch_no: Optional replenishment batch / lot number.
        bin_code: Optional physical warehouse storage bin code.
        actor_id: User ID of warehouse manager executing restock.
        conn: Optional active transaction connection.

    Returns:
        Summary dictionary including updated on_hand and audit_event_id.
    """
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        raise ValueError(f"Restock quantity must be a positive integer, got {quantity}")

    def _execute_restock(c: sqlite3.Connection) -> Dict[str, Any]:
        # Resolve bin_id if bin_code provided
        resolved_bin_id = None
        if bin_code:
            bin_cur = c.execute("""
                SELECT id FROM warehouse_bins 
                WHERE warehouse_id = ? AND bin_code = ?
            """, (warehouse_id, bin_code))
            bin_row = bin_cur.fetchone()
            if bin_row:
                resolved_bin_id = bin_row["id"]

        # Check existing inventory row for warehouse and product
        cur = c.execute("""
            SELECT id, quantity_on_hand, quantity_reserved, quantity_available, batch_number, bin_id
            FROM inventory
            WHERE warehouse_id = ? AND product_id = ?
            LIMIT 1
        """, (warehouse_id, product_id))
        row = cur.fetchone()

        if row:
            inv_id = row["id"]
            new_on_hand = row["quantity_on_hand"] + quantity
            c.execute("""
                UPDATE inventory
                SET quantity_on_hand = quantity_on_hand + ?,
                    batch_number = COALESCE(?, batch_number),
                    bin_id = COALESCE(?, bin_id),
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (quantity, batch_no, resolved_bin_id, inv_id))
        else:
            inv_id = f"inv_{uuid.uuid4().hex[:12]}"
            new_on_hand = quantity
            c.execute("""
                INSERT INTO inventory (
                    id, warehouse_id, product_id, bin_id, 
                    quantity_on_hand, quantity_reserved, batch_number, version
                ) VALUES (?, ?, ?, ?, ?, 0, ?, 1)
            """, (inv_id, warehouse_id, product_id, resolved_bin_id, quantity, batch_no))

        # Write immutable audit log to NoSQL document store
        doc_store = DocumentStore(conn=c)
        audit_event_id = f"EVT-RESTOCK-{uuid.uuid4().hex[:8].upper()}"
        audit_doc = {
            "event_id": audit_event_id,
            "action": "INVENTORY_RESTOCK",
            "actor_id": actor_id or "system",
            "warehouse_id": warehouse_id,
            "product_id": product_id,
            "quantity_added": quantity,
            "new_on_hand": new_on_hand,
            "batch_number": batch_no,
            "bin_code": bin_code,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        doc_store.insert("audit_event_logs", audit_doc)

        # Invalidate inventory caches
        cache = get_cache_manager()
        cache.invalidate_pattern("inventory:*")

        return {
            "status": "RESTOCKED",
            "inventory_id": inv_id,
            "warehouse_id": warehouse_id,
            "product_id": product_id,
            "quantity_added": quantity,
            "new_on_hand": new_on_hand,
            "batch_number": batch_no,
            "bin_code": bin_code,
            "audit_event_id": audit_event_id
        }

    if conn is not None:
        return _execute_restock(conn)
    else:
        with transaction_scope(isolation="IMMEDIATE") as auto_conn:
            return _execute_restock(auto_conn)


def adjust_inventory(
    warehouse_id: str,
    product_id: str,
    delta: int,
    reason: str,
    actor_id: Optional[str] = None,
    notes: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Performs a manual cycle count or variance inventory adjustment.

    Guarantees:
        new_quantity_on_hand >= quantity_reserved
    so that quantity_available never drops below zero.

    Args:
        warehouse_id: Warehouse ID.
        product_id: SKU product ID.
        delta: Adjustment delta (negative for loss/damage, positive for found).
        reason: Standard reason code (e.g. 'DAMAGED', 'CYCLE_COUNT', 'THEFT', 'EXPIRED').
        actor_id: ID of user executing adjustment.
        notes: Contextual explanation.
        conn: Optional active transaction connection.

    Returns:
        Summary dictionary with audit details.
    """
    if not isinstance(delta, int) or isinstance(delta, bool) or delta == 0:
        raise ValueError(f"Adjustment delta must be a non-zero integer, got {delta}")

    def _execute_adjust(c: sqlite3.Connection) -> Dict[str, Any]:
        cur = c.execute("""
            SELECT id, quantity_on_hand, quantity_reserved, quantity_available
            FROM inventory
            WHERE warehouse_id = ? AND product_id = ?
            LIMIT 1
        """, (warehouse_id, product_id))
        row = cur.fetchone()

        if not row:
            raise ProductNotFoundException(
                f"Cannot adjust inventory: No record found for product '{product_id}' in warehouse '{warehouse_id}'."
            )

        curr_on_hand = row["quantity_on_hand"]
        curr_reserved = row["quantity_reserved"]
        new_on_hand = curr_on_hand + delta

        if new_on_hand < curr_reserved:
            raise InsufficientStockException(
                f"Adjustment of {delta} violates stock invariant: "
                f"new on_hand ({new_on_hand}) would be less than active reservations ({curr_reserved})."
            )

        if new_on_hand < 0:
            raise InvalidInventoryOperationException(
                f"Adjustment of {delta} would result in negative on_hand ({new_on_hand})."
            )

        c.execute("""
            UPDATE inventory
            SET quantity_on_hand = ?,
                version = version + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_on_hand, row["id"]))

        # Write immutable audit log
        doc_store = DocumentStore(conn=c)
        audit_id = f"EVT-ADJ-{uuid.uuid4().hex[:8].upper()}"
        audit_doc = {
            "event_id": audit_id,
            "action": "INVENTORY_ADJUSTMENT",
            "actor_id": actor_id or "system",
            "warehouse_id": warehouse_id,
            "product_id": product_id,
            "quantity_delta": delta,
            "reason_code": reason,
            "notes": notes,
            "old_on_hand": curr_on_hand,
            "new_on_hand": new_on_hand,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        doc_store.insert("audit_event_logs", audit_doc)

        # Invalidate inventory caches
        cache = get_cache_manager()
        cache.invalidate_pattern("inventory:*")

        return {
            "status": "ADJUSTED",
            "inventory_id": row["id"],
            "warehouse_id": warehouse_id,
            "product_id": product_id,
            "quantity_delta": delta,
            "new_on_hand": new_on_hand,
            "reason_code": reason,
            "audit_id": audit_id,
            "audit_event_id": audit_id
        }

    if conn is not None:
        return _execute_adjust(conn)
    else:
        with transaction_scope(isolation="IMMEDIATE") as auto_conn:
            return _execute_adjust(auto_conn)
