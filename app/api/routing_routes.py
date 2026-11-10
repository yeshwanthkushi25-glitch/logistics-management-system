"""
Logistics Information Management System (Logistics IMS) - Network Graph Routing API Blueprint.

Defines REST routes:
- POST /api/v1/network/routes (and /api/v1/routes/shortest-path): Dijkstra / A* optimal path computation
- GET /api/v1/network/bottlenecks: Betweenness centrality & capacity utilization ratio (rho >= 0.85) analysis
- POST /api/v1/network/routes/reroute (and /api/v1/network/reroute): Simulated edge failure & dynamic rerouting
- GET /api/v1/network/alternatives (and POST /api/v1/network/routes/alternatives): Yen's K-shortest alternative paths
- GET /api/v1/network/reachability (and /api/v1/routes/reachability): Connected hubs reachability analysis
- GET /api/v1/network/graph: Graph topology summary (nodes, corridors, edge capacities)
"""

import logging
from typing import Dict, Any, Optional, List

from flask import Blueprint, request, jsonify, g

from app.db.connection import get_db_connection
from app.services.routing_service import (
    calculate_optimal_route,
    get_k_alternative_routes,
    detect_network_bottlenecks,
    simulate_edge_outage_and_reroute,
    get_network_reachability,
    build_network_graph,
    NoPathFoundException,
    RoutingException,
    NodeNotFoundException,
    VehicleConstraintViolationException
)

from app.auth.rbac import (
    require_auth,
    require_role,
    ROLE_ADMIN,
    ROLE_DISPATCHER,
    ROLE_WAREHOUSE_MANAGER
)

logger = logging.getLogger(__name__)

routing_bp = Blueprint("routing_routes", __name__)


# =============================================================================
# POST /api/v1/network/routes & POST /api/v1/routes/shortest-path
# =============================================================================

@routing_bp.route("/routes", methods=["POST"])
@routing_bp.route("/shortest-path", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_DISPATCHER, ROLE_WAREHOUSE_MANAGER)
def compute_route():
    """
    Computes mathematically optimal routing between origin and destination hubs.
    Supports distance, freight cost, transit time, and balanced multi-criteria optimization.
    """
    data = request.get_json(silent=True) or {}
    origin = data.get("origin_node") or data.get("origin")
    dest = data.get("destination_node") or data.get("destination")
    criteria = data.get("optimization_criteria") or data.get("criteria") or "distance"
    vehicle_constraints = data.get("vehicle_constraints")
    use_astar = bool(data.get("use_astar", False))
    exclude_nodes = data.get("exclude_nodes")
    exclude_edges = data.get("failed_edges") or data.get("exclude_edges")

    # Check if K-alternatives were requested in body
    if data.get("alternatives") and int(data.get("alternatives")) > 1:
        return get_alternatives()

    if not origin or not dest:
        return jsonify({
            "error": "Bad Request",
            "message": "Fields 'origin_node' and 'destination_node' are required."
        }), 400

    try:
        route_result = calculate_optimal_route(
            origin_node=origin,
            destination_node=dest,
            criteria=criteria,
            vehicle_constraints=vehicle_constraints,
            use_astar=use_astar,
            exclude_nodes=exclude_nodes,
            exclude_edges=exclude_edges
        )
    except NodeNotFoundException as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except (NoPathFoundException, VehicleConstraintViolationException) as exc:
        return jsonify({"error": "Unprocessable Entity", "message": str(exc)}), 422
    except (RoutingException, ValueError) as exc:
        return jsonify({"error": "Bad Request", "message": str(exc)}), 400
    except Exception as exc:
        logger.error(f"Routing calculation error: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify(route_result), 200


# =============================================================================
# GET /api/v1/network/bottlenecks
# =============================================================================

@routing_bp.route("/bottlenecks", methods=["GET"])
@require_role(ROLE_ADMIN, ROLE_DISPATCHER, ROLE_WAREHOUSE_MANAGER)
def get_bottlenecks():
    """
    Identifies high-congestion corridors where capacity utilization ratio rho >= 0.85
    and calculates edge betweenness centrality.
    """
    try:
        bottlenecks = detect_network_bottlenecks()
    except Exception as exc:
        logger.error(f"Bottleneck detection error: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify({
        "status": "SUCCESS",
        "threshold_rho": 0.85,
        "bottlenecks": bottlenecks,
        "count": len(bottlenecks)
    }), 200


# =============================================================================
# POST /api/v1/network/routes/reroute & POST /api/v1/network/reroute
# =============================================================================

@routing_bp.route("/routes/reroute", methods=["POST"])
@routing_bp.route("/reroute", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_DISPATCHER)
def reroute_outage():
    """
    Simulates acute network corridor or hub outage and calculates an automated resilient fallback.
    """
    data = request.get_json(silent=True) or {}
    origin = data.get("origin_node") or data.get("origin")
    dest = data.get("destination_node") or data.get("destination")
    criteria = data.get("optimization_criteria") or data.get("criteria") or "distance"
    failed_edge_id = data.get("failed_edge_id")
    failed_edges = data.get("failed_edges")
    exclude_nodes = data.get("exclude_nodes")

    if not origin or not dest:
        return jsonify({
            "error": "Bad Request",
            "message": "Fields 'origin_node' and 'destination_node' are required."
        }), 400

    try:
        reroute_result = simulate_edge_outage_and_reroute(
            origin_node=origin,
            destination_node=dest,
            failed_edge_id=failed_edge_id,
            failed_edges=failed_edges,
            exclude_nodes=exclude_nodes,
            criteria=criteria
        )
    except NodeNotFoundException as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except NoPathFoundException as exc:
        return jsonify({"error": "Unprocessable Entity", "message": str(exc)}), 422
    except Exception as exc:
        logger.error(f"Reroute simulation error: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    # Ensure path and path_nodes aliases are present
    fallback_route = reroute_result.get("fallback_route", {})
    reroute_result["path_nodes"] = fallback_route.get("path_nodes", [])
    reroute_result["path"] = fallback_route.get("path", [])

    return jsonify(reroute_result), 200


# =============================================================================
# GET /api/v1/network/alternatives & POST /api/v1/network/routes/alternatives
# =============================================================================

@routing_bp.route("/alternatives", methods=["GET"])
@routing_bp.route("/routes/alternatives", methods=["POST", "GET"])
@require_role(ROLE_ADMIN, ROLE_DISPATCHER, ROLE_WAREHOUSE_MANAGER)
def get_alternatives():
    """
    Generates Yen's K-shortest alternative paths between origin and destination hubs.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        origin = data.get("origin_node") or data.get("origin")
        dest = data.get("destination_node") or data.get("destination")
        k_val = data.get("alternatives") or data.get("k") or 3
        criteria = data.get("optimization_criteria") or data.get("criteria") or "distance"
    else:
        origin = request.args.get("origin_node") or request.args.get("origin")
        dest = request.args.get("destination_node") or request.args.get("destination")
        k_val = request.args.get("alternatives", request.args.get("k", 3))
        criteria = request.args.get("criteria", "distance")

    if not origin or not dest:
        return jsonify({
            "error": "Bad Request",
            "message": "Origin and destination parameters are required."
        }), 400

    try:
        k = max(1, min(10, int(k_val)))
        routes = get_k_alternative_routes(
            origin_node=origin,
            destination_node=dest,
            k=k,
            criteria=criteria
        )
    except NodeNotFoundException as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except NoPathFoundException as exc:
        return jsonify({"error": "Unprocessable Entity", "message": str(exc)}), 422
    except Exception as exc:
        logger.error(f"Alternative route computation error: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify({
        "status": "SUCCESS",
        "origin": origin,
        "destination": dest,
        "k_requested": k,
        "count": len(routes),
        "routes": routes,
        "alternative_paths": routes
    }), 200


# =============================================================================
# GET /api/v1/network/reachability & GET /api/v1/routes/reachability
# =============================================================================

@routing_bp.route("/reachability", methods=["GET"])
def check_reachability():
    """
    Returns all nodes reachable from a given origin hub across the multi-tier corridor network.
    """
    hub_id = request.args.get("hub_id") or request.args.get("origin_node")
    if not hub_id:
        return jsonify({
            "error": "Bad Request",
            "message": "Query parameter 'hub_id' is required."
        }), 400

    try:
        result = get_network_reachability(origin_node=hub_id)
    except NodeNotFoundException as exc:
        return jsonify({"error": "Not Found", "message": str(exc)}), 404
    except Exception as exc:
        logger.error(f"Reachability query error: {exc}")
        return jsonify({"error": "Internal Server Error", "message": str(exc)}), 500

    return jsonify({
        "status": "SUCCESS",
        "origin_hub_id": hub_id,
        "reachable_nodes": result.get("reachable_nodes", []),
        "destinations": result.get("destinations", []),
        "total_reachable": result.get("total_reachable", 0)
    }), 200


# =============================================================================
# GET /api/v1/network/graph
# =============================================================================

@routing_bp.route("/graph", methods=["GET"])
def get_graph_topology():
    """
    Returns the network topology graph: hubs (nodes) and transport corridors (edges).
    """
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT id, code, name, type, city, latitude, longitude, capacity_sqm FROM warehouses WHERE is_active = 1")
    nodes = [dict(r) for r in cur.fetchall()]

    cur.execute("""
        SELECT id, source_warehouse_id, target_warehouse_id, distance_km,
               transit_time_hours, base_cost, transport_mode, capacity_max_kg,
               current_load_kg, status
        FROM network_corridors
        WHERE status = 'ACTIVE'
    """)
    edges = [dict(r) for r in cur.fetchall()]

    return jsonify({
        "status": "SUCCESS",
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges
    }), 200


routes_alias_bp = Blueprint("routes_alias_routes", __name__)

@routes_alias_bp.route("/shortest-path", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_DISPATCHER, ROLE_WAREHOUSE_MANAGER)
def shortest_path_alias():
    return compute_route()

@routes_alias_bp.route("/reachability", methods=["GET"])
def reachability_alias():
    return check_reachability()

@routes_alias_bp.route("/alternatives", methods=["POST", "GET"])
@require_role(ROLE_ADMIN, ROLE_DISPATCHER, ROLE_WAREHOUSE_MANAGER)
def alternatives_alias():
    return get_alternatives()

@routes_alias_bp.route("/reroute", methods=["POST"])
@require_role(ROLE_ADMIN, ROLE_DISPATCHER)
def reroute_alias():
    return reroute_outage()
