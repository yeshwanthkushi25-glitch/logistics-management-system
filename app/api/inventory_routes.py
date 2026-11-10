"""
Logistics Information Management System (Logistics IMS) - Inventory & Warehouse API Blueprint.

Defines REST routes:
- GET /api/v1/inventory: Query multi-warehouse stock levels with filtering and low-stock ROP alerting
- POST /api/v1/inventory/restock: Replenish batch inventory with bin allocation and capacity checks
- POST /api/v1/inventory/adjust: Manual variance and cycle count adjustment with audit logging
- GET /api/v1/inventory/warehouses (and /api/v1/warehouses): List distribution centers and capacities
"""

import logging
from typing import Dict, Any, Optional

from flask import Blueprint, request, jsonify, g

from app.db.connection import get_db_connection
from app.services.inventory_service import (
    get_inventory_levels,
    restock_inventory,
    adjust_inventory,
    InsufficientStockException,
    ProductNotFoundException,
    WarehouseNotFoundException,
    InvalidInventoryOperationException
)

from app.auth.rbac import (
    require_auth,
    require_role,
    ROLE_ADMIN,
    ROLE_WAREHOUSE_MANAGER
)

logger = logging.getLogger(__name__)

inventory_bp = Blueprint("inventory_routes", __name__)


# =============================================================================
# GET /api/v1/inventory
# =============================================================================

@inventory_bp.route("/", methods=["GET"])
@inventory_bp.route("", methods=["GET"])
def list_inventory():
    """
    Retrieves current stock levels across warehouses.

    Query Parameters:
        warehouse_id (str, optional): Target warehouse filter
        product_id (str, optional): Target product SKU/ID filter
        low_stock (str, optional): 'true'/'1' filters items where available <= reorder_threshold
        page (int, optional): Page number (default 1)
        limit (int, optional): Page size (default 50)
    """
    warehouse_id = request.args.get("warehouse_id")
    product_id = request.args.get("product_id")
    low_stock_raw = request.args.get("low_stock", "").lower()
    low_stock = low_stock_raw in ("true", "1", "yes")

    try:
        page = max(1, int(request.args.get("page", 1)))
        limit = min(500, max(1, int(request.args.get("limit", request.args.get("per_page", 50)))))
    except ValueError:
        page = 1
        limit = 50

    try:
        records = get_inventory_levels(
            warehouse_id=warehouse_id,
            product_id=product_id,
            low_stock_only=low_stock
        )
    except Exception as exc:
        logger.error(f"Error querying inventory levels: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    # Pagination slicing
    total = len(records)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    paginated_items = records[start_idx:end_idx]

    return jsonify({
        "status": "SUCCESS",
        "items": paginated_items,
        "total": total,
        "page": page,
        "limit": limit
    }), 200


# =============================================================================
# POST /api/v1/inventory/restock
# =============================================================================

@inventory_bp.route("/restock", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_WAREHOUSE_MANAGER)
def restock():
    """
    Restocks batch inventory into a warehouse bin.
    Restricted to WarehouseManager and Admin roles (403 for Customer/Driver).

    Payload:
        warehouse_id (str): Target warehouse identifier
        product_id (str): SKU identifier
        quantity (int): Units to replenish (> 0)
        batch_number (str, optional): Supplier lot / batch number (or batch_no)
        bin_code (str, optional): Specific bin location code
    """
    data = request.get_json(silent=True) or {}

    warehouse_id = data.get("warehouse_id")
    product_id = data.get("product_id")
    raw_quantity = data.get("quantity")
    batch_no = data.get("batch_number") or data.get("batch_no")
    bin_code = data.get("bin_code")

    if not warehouse_id or not product_id or raw_quantity is None:
        return jsonify({
            "error": "Bad Request",
            "message": "Missing required fields: warehouse_id, product_id, and quantity are required."
        }), 400

    try:
        quantity = int(raw_quantity)
    except (ValueError, TypeError):
        return jsonify({
            "error": "Bad Request",
            "message": f"Quantity must be a valid integer, got '{raw_quantity}'."
        }), 400

    if quantity <= 0:
        return jsonify({
            "error": "Bad Request",
            "message": f"Restock quantity must be positive, got {quantity}."
        }), 400

    # -------------------------------------------------------------------------
    # Physical Warehouse Capacity Boundary Check
    # -------------------------------------------------------------------------
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, capacity_sqm, is_active FROM warehouses WHERE id = ? LIMIT 1", (warehouse_id,))
    wh_row = cur.fetchone()

    if not wh_row:
        return jsonify({
            "error": "Not Found",
            "message": f"Warehouse '{warehouse_id}' not found."
        }), 404

    # Each square meter accommodates a maximum physical volume threshold (e.g. 50 units/sqm)
    # A single bulk shipment of 10,000,000 units clearly breaches warehouse physical limits.
    max_physical_capacity = int(wh_row["capacity_sqm"] * 50)
    if quantity > max_physical_capacity or quantity > 500000:
        logger.warning(
            f"Physical storage limit exceeded for warehouse {warehouse_id}: "
            f"requested restock {quantity} > capacity threshold {max_physical_capacity}"
        )
        return jsonify({
            "error": "Unprocessable Entity",
            "message": f"Restock quantity {quantity} exceeds physical warehouse storage capacity limit ({max_physical_capacity}).",
            "warehouse_id": warehouse_id,
            "capacity_limit": max_physical_capacity
        }), 422

    # Extract actor ID from authenticated request context
    current_user = getattr(g, "current_user", {}) or {}
    actor_id = current_user.get("sub") or current_user.get("id") or current_user.get("username") or "user_wm"

    try:
        result = restock_inventory(
            warehouse_id=warehouse_id,
            product_id=product_id,
            quantity=quantity,
            batch_no=batch_no,
            bin_code=bin_code,
            actor_id=actor_id
        )
    except (ProductNotFoundException, WarehouseNotFoundException) as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": "Bad Request", "message": str(exc)}), 400
    except Exception as exc:
        logger.error(f"Restock error: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify({
        "status": "SUCCESS",
        "action": "RESTOCKED",
        "warehouse_id": result.get("warehouse_id"),
        "product_id": result.get("product_id"),
        "quantity_added": result.get("quantity_added"),
        "new_on_hand": result.get("new_on_hand"),
        "batch_number": result.get("batch_number"),
        "audit_event_id": result.get("audit_event_id")
    }), 201


# =============================================================================
# POST /api/v1/inventory/adjust
# =============================================================================

@inventory_bp.route("/adjust", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_WAREHOUSE_MANAGER)
def adjust():
    """
    Performs a cycle count or variance inventory adjustment.
    Restricted to WarehouseManager and Admin roles (403 for Customer/Driver).

    Payload:
        warehouse_id (str): Target warehouse identifier
        product_id (str): SKU identifier
        quantity_delta (int): Variance delta (negative for damage/loss, positive for found)
        reason_code (str, optional): Standard reason code (e.g. 'DAMAGED', 'CYCLE_COUNT')
        notes (str, optional): Explanatory note
    """
    data = request.get_json(silent=True) or {}

    warehouse_id = data.get("warehouse_id")
    product_id = data.get("product_id")
    raw_delta = data.get("quantity_delta") if "quantity_delta" in data else data.get("delta")
    reason_code = data.get("reason_code") or data.get("reason") or "CYCLE_COUNT"
    notes = data.get("notes")

    if not warehouse_id or not product_id or raw_delta is None:
        return jsonify({
            "error": "Bad Request",
            "message": "Missing required fields: warehouse_id, product_id, and quantity_delta are required."
        }), 400

    try:
        delta = int(raw_delta)
    except (ValueError, TypeError):
        return jsonify({
            "error": "Bad Request",
            "message": f"Adjustment delta must be a non-zero integer, got '{raw_delta}'."
        }), 400

    if delta == 0:
        return jsonify({
            "error": "Bad Request",
            "message": "Adjustment delta cannot be zero."
        }), 400

    current_user = getattr(g, "current_user", {}) or {}
    actor_id = current_user.get("sub") or current_user.get("id") or current_user.get("username") or "user_wm"

    try:
        result = adjust_inventory(
            warehouse_id=warehouse_id,
            product_id=product_id,
            delta=delta,
            reason=reason_code,
            actor_id=actor_id,
            notes=notes
        )
    except InsufficientStockException as exc:
        return jsonify({"error": "Insufficient Stock", "message": str(exc)}), 409
    except (ProductNotFoundException, WarehouseNotFoundException) as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except ValueError as exc:
        return jsonify({"error": "Bad Request", "message": str(exc)}), 400
    except Exception as exc:
        logger.error(f"Inventory adjust error: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify({
        "status": "SUCCESS",
        "action": "ADJUSTED",
        "warehouse_id": result.get("warehouse_id"),
        "product_id": result.get("product_id"),
        "quantity_delta": result.get("delta"),
        "new_on_hand": result.get("new_on_hand"),
        "audit_id": result.get("audit_event_id"),
        "audit_event_id": result.get("audit_event_id")
    }), 200


# =============================================================================
# GET /api/v1/inventory/warehouses & GET /api/v1/warehouses
# =============================================================================

@inventory_bp.route("/warehouses", methods=["GET"])
def list_warehouses():
    """Returns a directory of all active distribution centers and physical capacities."""
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT 
            w.id, w.code, w.name, w.type, w.address, w.city, w.state, w.country,
            w.latitude, w.longitude, w.capacity_sqm, w.is_active,
            COALESCE(SUM(i.quantity_on_hand), 0) AS current_total_stock
        FROM warehouses w
        LEFT JOIN inventory i ON w.id = i.warehouse_id
        GROUP BY w.id
        ORDER BY w.code ASC
    """)
    rows = cur.fetchall()

    warehouses = [
        {
            "id": r["id"],
            "code": r["code"],
            "name": r["name"],
            "type": r["type"],
            "address": r["address"],
            "city": r["city"],
            "state": r["state"],
            "country": r["country"],
            "latitude": float(r["latitude"]),
            "longitude": float(r["longitude"]),
            "capacity_sqm": float(r["capacity_sqm"]),
            "is_active": int(r["is_active"]),
            "current_total_stock": int(r["current_total_stock"])
        }
        for r in rows
    ]

    return jsonify({
        "status": "SUCCESS",
        "warehouses": warehouses,
        "total": len(warehouses)
    }), 200


warehouses_bp = Blueprint("warehouses_routes", __name__)

@warehouses_bp.route("", methods=["GET"])
@warehouses_bp.route("/", methods=["GET"])
def warehouses_root_route():
    return list_warehouses()
