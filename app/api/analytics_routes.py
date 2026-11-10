"""
Logistics Information Management System (Logistics IMS) - Analytics & Caching API Blueprint.

Defines REST routes:
- GET /api/v1/analytics/kpis: Operational performance indicators (turnover, fulfillment rate, active shipments)
- GET /api/v1/analytics/audit/logs: Immutable audit trail records from NoSQL document store
- GET /api/v1/analytics/cache/stats (and /api/v1/cache/stats, /api/v1/cache/metrics): Cache-Aside telemetry & hit ratio
"""

import logging
from typing import Dict, Any, Optional

from flask import Blueprint, request, jsonify, g

from app.db.connection import get_db_connection
from app.db.cache import get_cache_manager
from app.db.nosql import get_doc_store

from app.auth.rbac import (
    require_auth,
    require_role,
    ROLE_ADMIN,
    ROLE_WAREHOUSE_MANAGER,
    ROLE_DISPATCHER
)

logger = logging.getLogger(__name__)

analytics_bp = Blueprint("analytics_routes", __name__)


# =============================================================================
# GET /api/v1/analytics/kpis
# =============================================================================

@analytics_bp.route("/kpis", methods=["GET"])
@require_auth
def get_operational_kpis():
    """
    Computes key performance metrics across inventory, orders, and shipments:
    - Total inventory on hand, reserved, and available
    - Total inventory valuation in USD
    - Order counts by status & overall fulfillment completion rate (%)
    - Active shipments distribution by 7-state FSM status
    """
    conn = get_db_connection()
    cur = conn.cursor()

    # 1. Inventory Aggregations
    cur.execute("""
        SELECT 
            COALESCE(SUM(i.quantity_on_hand), 0) AS total_on_hand,
            COALESCE(SUM(i.quantity_reserved), 0) AS total_reserved,
            COALESCE(SUM(i.quantity_available), 0) AS total_available,
            COALESCE(SUM(i.quantity_on_hand * p.unit_price), 0.0) AS total_valuation_usd
        FROM inventory i
        JOIN products p ON i.product_id = p.id
    """)
    inv_row = cur.fetchone()

    # 2. Orders Aggregations & Fulfillment Rate
    cur.execute("""
        SELECT 
            status,
            COUNT(*) AS count,
            COALESCE(SUM(total_amount), 0.0) AS aggregate_amount
        FROM orders
        GROUP BY status
    """)
    order_rows = cur.fetchall()
    orders_by_status = {r["status"]: r["count"] for r in order_rows}
    total_orders = sum(orders_by_status.values())
    delivered_orders = orders_by_status.get("DELIVERED", 0) + orders_by_status.get("CONFIRMED", 0)
    fulfillment_rate = round((delivered_orders / total_orders * 100.0), 2) if total_orders > 0 else 100.0

    # 3. Shipments Aggregations across 7-state FSM
    cur.execute("""
        SELECT status, COUNT(*) AS count
        FROM shipments
        GROUP BY status
    """)
    shipment_rows = cur.fetchall()
    shipments_by_status = {r["status"]: r["count"] for r in shipment_rows}
    active_in_transit = (
        shipments_by_status.get("PICKED_UP", 0) +
        shipments_by_status.get("IN_TRANSIT", 0) +
        shipments_by_status.get("OUT_FOR_DELIVERY", 0)
    )

    # 4. Cache Statistics
    cache = get_cache_manager()
    cache_stats = cache.get_stats()

    return jsonify({
        "status": "SUCCESS",
        "inventory": {
            "total_on_hand": int(inv_row["total_on_hand"]),
            "total_reserved": int(inv_row["total_reserved"]),
            "total_available": int(inv_row["total_available"]),
            "total_valuation_usd": round(float(inv_row["total_valuation_usd"]), 2)
        },
        "orders": {
            "total_orders": total_orders,
            "orders_by_status": orders_by_status,
            "fulfillment_rate_percent": fulfillment_rate
        },
        "shipments": {
            "total_shipments": sum(shipments_by_status.values()),
            "active_in_transit": active_in_transit,
            "shipments_by_status": shipments_by_status
        },
        "cache_performance": cache_stats
    }), 200


# =============================================================================
# GET /api/v1/analytics/audit/logs
# =============================================================================

@analytics_bp.route("/audit/logs", methods=["GET"])
@require_role(ROLE_ADMIN)
def get_audit_event_logs():
    """
    Queries immutable audit event logs from the NoSQL document store.
    Strictly restricted to Admin role.
    """
    action_filter = request.args.get("action")
    try:
        limit = min(500, max(1, int(request.args.get("limit", 50))))
    except ValueError:
        limit = 50

    doc_store = get_doc_store()
    query = {}
    if action_filter:
        query["action"] = action_filter

    try:
        logs = doc_store.find("audit_event_logs", query)
    except Exception as exc:
        logger.error(f"Error querying audit logs: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    # Sort descending by timestamp or insertion order
    sorted_logs = sorted(
        logs,
        key=lambda x: x.get("timestamp") or x.get("_created_at") or "",
        reverse=True
    )[:limit]

    return jsonify({
        "status": "SUCCESS",
        "count": len(sorted_logs),
        "logs": sorted_logs
    }), 200


# =============================================================================
# GET /api/v1/analytics/cache/stats & /api/v1/cache/stats & /api/v1/cache/metrics
# =============================================================================

@analytics_bp.route("/cache/stats", methods=["GET"])
@analytics_bp.route("/cache/metrics", methods=["GET"])
@require_auth
def get_cache_telemetry():
    """
    Returns real-time telemetry from the in-memory CacheManager:
    - hits, cache_hits
    - misses, cache_misses
    - invalidations
    - hit_ratio
    - current entry count & capacity
    """
    cache = get_cache_manager()
    stats = cache.get_stats()

    # Provide both standard and test-suite expected keys
    hits = stats.get("hits", 0)
    misses = stats.get("misses", 0)
    invalidations = stats.get("invalidations", 0)
    hit_ratio = stats.get("hit_ratio", 0.0)

    return jsonify({
        "status": "SUCCESS",
        "hits": hits,
        "cache_hits": hits,
        "misses": misses,
        "cache_misses": misses,
        "invalidations": invalidations,
        "hit_ratio": hit_ratio,
        "cache_hit_ratio": hit_ratio,
        "max_entries": stats.get("max_entries", 1000),
        "current_entries": stats.get("current_entries", 0)
    }), 200


cache_bp = Blueprint("cache_routes", __name__)

@cache_bp.route("/stats", methods=["GET"])
@cache_bp.route("/metrics", methods=["GET"])
@require_auth
def cache_root_stats():
    return get_cache_telemetry()
