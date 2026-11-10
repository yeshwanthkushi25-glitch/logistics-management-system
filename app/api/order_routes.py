"""
Logistics Information Management System (Logistics IMS) - Orders & Fulfillment API Blueprint.

Defines REST routes:
- POST /api/v1/orders: Atomic multi-warehouse order placement with idempotency key handling
- GET /api/v1/orders: Order history inquiry with status and customer filtering
- GET /api/v1/orders/<order_id>: Detailed order snapshot, line items, and shipment manifests
- POST/PUT /api/v1/orders/<order_id>/cancel: Pre-dispatch order cancellation with stock hold release
"""

import logging
from typing import Dict, Any, Optional

from flask import Blueprint, request, jsonify, g

from app.services.fulfillment_service import (
    create_order_atomic,
    cancel_order,
    get_order,
    list_orders,
    OrderNotFoundException,
    OrderStateTransitionException,
    PaymentFailedException
)
from app.services.inventory_service import InsufficientStockException

from app.auth.rbac import (
    require_auth,
    require_role,
    ROLE_ADMIN,
    ROLE_WAREHOUSE_MANAGER,
    ROLE_CUSTOMER,
    ROLE_DRIVER
)

logger = logging.getLogger(__name__)

order_bp = Blueprint("order_routes", __name__)


# =============================================================================
# POST /api/v1/orders
# =============================================================================

@order_bp.route("/", methods=["POST"])
@order_bp.route("", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_WAREHOUSE_MANAGER, ROLE_CUSTOMER)
def place_order():
    """
    Submits an atomic customer order across one or more products.
    Enforces ACID multi-warehouse stock reservation and automatic rollback.
    Restricted to Customer, WarehouseManager, and Admin roles (403 for Driver).

    Headers:
        Idempotency-Key (str, optional): Client-supplied token ensuring exactly-once execution.

    Payload:
        items (list): Array of items [{"product_id": str, "quantity": int}]
        shipping_address (str or dict): Destination street address
        destination_city (str, optional): Destination city
        destination_lat (float, optional): Latitude coordinate
        destination_lon (float, optional): Longitude coordinate
        simulate_failure (bool, optional): Force rollback to test ACID atomicity
        simulate_payment_failure (bool, optional): Force payment gateway decline
    """
    data = request.get_json(silent=True) or {}
    items = data.get("items")
    shipping_address = data.get("shipping_address")
    destination_city = data.get("destination_city") or "Chicago"
    destination_lat = data.get("destination_lat")
    destination_lon = data.get("destination_lon")
    simulate_failure = bool(data.get("simulate_failure", False))
    simulate_payment_failure = bool(data.get("simulate_payment_failure", False))

    # Read idempotency key from HTTP header or request payload
    idempotency_key = request.headers.get("Idempotency-Key") or data.get("idempotency_key")

    if not items or not isinstance(items, list):
        return jsonify({
            "error": "Bad Request",
            "message": "Field 'items' must be a non-empty list of product order items."
        }), 400

    if not shipping_address:
        return jsonify({
            "error": "Bad Request",
            "message": "Field 'shipping_address' is required."
        }), 400

    # Resolve customer ID from authenticated context
    current_user = getattr(g, "current_user", {}) or {}
    customer_id = (
        current_user.get("sub") or
        current_user.get("id") or
        current_user.get("user_id") or
        "usr-test-customer-01"
    )

    try:
        order_record = create_order_atomic(
            customer_id=customer_id,
            items=items,
            shipping_address=shipping_address,
            destination_city=destination_city,
            destination_lat=destination_lat,
            destination_lon=destination_lon,
            simulate_failure=simulate_failure,
            simulate_payment_failure=simulate_payment_failure,
            idempotency_key=idempotency_key
        )
    except InsufficientStockException as exc:
        logger.info(f"Order rejected due to stockout: {exc}")
        return jsonify({
            "error": "Conflict",
            "message": "Insufficient available inventory to fulfill the requested items.",
            "details": str(exc)
        }), 409
    except PaymentFailedException as exc:
        logger.info(f"Order rolled back due to simulated payment failure: {exc}")
        return jsonify({
            "error": "Unprocessable Entity",
            "message": "Payment processing declined.",
            "details": str(exc)
        }), 422
    except ValueError as exc:
        return jsonify({"error": "Bad Request", "message": str(exc)}), 400
    except Exception as exc:
        logger.error(f"Unexpected error during order creation: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    order_id = order_record.get("order_id") or order_record.get("id")
    order_record["order_id"] = order_id
    order_record["id"] = order_id

    return jsonify(order_record), 201


# =============================================================================
# GET /api/v1/orders
# =============================================================================

@order_bp.route("/", methods=["GET"])
@order_bp.route("", methods=["GET"])
@require_auth
def get_orders():
    """
    Lists orders matching optional status or customer filters.
    Regular customers are scoped to their own order records.
    """
    status_filter = request.args.get("status")
    customer_filter = request.args.get("customer_id")

    current_user = getattr(g, "current_user", {}) or {}
    user_role = current_user.get("role")
    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id")

    # Scope customer to their own orders unless privileged role
    if user_role == ROLE_CUSTOMER:
        target_customer_id = user_id
    else:
        target_customer_id = customer_filter

    try:
        limit = min(200, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50

    try:
        orders_list = list_orders(
            customer_id=target_customer_id,
            status=status_filter,
            limit=limit
        )
    except Exception as exc:
        logger.error(f"Error querying orders: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify({
        "status": "SUCCESS",
        "orders": orders_list,
        "total": len(orders_list)
    }), 200


# =============================================================================
# GET /api/v1/orders/<order_id>
# =============================================================================

@order_bp.route("/<order_id>", methods=["GET"])
@require_auth
def get_order_by_id(order_id: str):
    """Retrieves full details for an order including items and shipment manifests."""
    try:
        order_details = get_order(order_id)
    except OrderNotFoundException as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except Exception as exc:
        logger.error(f"Error fetching order {order_id}: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    # Ensure both id and order_id keys are present
    order_details["id"] = order_details.get("id") or order_id
    order_details["order_id"] = order_details.get("order_id") or order_id

    return jsonify(order_details), 200


# =============================================================================
# POST / PUT /api/v1/orders/<order_id>/cancel
# =============================================================================

@order_bp.route("/<order_id>/cancel", methods=["POST", "PUT"])
@require_auth
def cancel_order_by_id(order_id: str):
    """
    Cancels an order before dispatch and releases all held stock reservations.
    """
    current_user = getattr(g, "current_user", {}) or {}
    actor_id = current_user.get("sub") or current_user.get("id") or "user"

    data = request.get_json(silent=True) or {}
    reason = data.get("reason") or "Customer requested pre-dispatch cancellation"

    try:
        result = cancel_order(order_id=order_id, actor_id=actor_id, reason=reason)
    except OrderNotFoundException as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except OrderStateTransitionException as exc:
        return jsonify({"error": "Unprocessable Entity", "message": str(exc)}), 422
    except Exception as exc:
        logger.error(f"Error cancelling order {order_id}: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify(result), 200
