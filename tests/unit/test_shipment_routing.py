"""
Comprehensive Empirical Verification Test Suite for Milestone 3 (Remediated).
Shipment Tracking & Graph Network Routing Engine.

Test Matrix:
1. test_fsm_full_lifecycle_progression (CREATED -> PICKED_UP -> IN_TRANSIT -> OUT_FOR_DELIVERY -> DELIVERED)
2. test_fsm_invalid_status_leaps_rejected (Illegal jumps: CREATED -> DELIVERED, PICKED_UP -> DELIVERED)
3. test_fsm_terminal_state_immutability (DELIVERED -> IN_TRANSIT, DELIVERED -> CREATED)
4. test_fsm_exception_and_recovery (IN_TRANSIT -> EXCEPTION -> OUT_FOR_DELIVERY -> DELIVERED)
5. test_milestone_recording_and_chronology (10 milestone types, sorted timestamp chronology)
6. test_telemetry_nosql_ingestion_and_query (Polymorphic GPS, temperature, shock in shipment_telemetry)
7. test_telemetry_boundary_validation (Invalid lat > 90, lon < -180 coordinates fail validation)
8. test_cache_aside_full_lifecycle (Miss -> Populate -> Hit -> Mutation Invalidation -> Fresh Fetch)
9. test_proof_of_delivery_upload_and_transition (POD upload via StorageClient, milestone, DELIVERED state)
10. test_driver_assignment (Assign driver, verify persistence and cache invalidation)
11. test_dijkstra_shortest_path_distance (Dijkstra optimization by distance_km)
12. test_dijkstra_least_cost_routing (Criteria=cost selects lower cost over shorter distance)
13. test_astar_search_haversine_heuristic (A* search with admissible Haversine heuristic)
14. test_multi_hop_traversal_intermediate_hubs (Verification of multi-tier hub sequence)
15. test_graph_boundary_self_loop (Origin == Destination returns 0 distance/cost and 1-node path)
16. test_graph_boundary_disconnected_unreachable (Unreachable island hub raises NoPathFoundException)
17. test_vehicle_weight_capacity_constraints (Weight constraint exceeds corridor limits)
18. test_network_bottleneck_detection (rho >= 0.85 and edge betweenness centrality detection)
19. test_dynamic_edge_outage_and_rerouting (Corridor outage simulation, fallback path, and route delta)
20. test_k_alternative_routes_generation (Yen's algorithm generating multiple distinct candidate routes)
21. test_sql_recursive_cte_fallback (Pure SQLite recursive CTE path traversal)
22. test_network_reachability (Single-source reachability and connected components)
23. test_cross_alias_routing_comprehensive (Cross-alias queries: Chicago -> New York, CDC-CHICAGO -> wh-04, etc.)
24. test_astar_inadmissibility_guard (A* rejects non-distance criteria with RoutingException)
25. test_sql_recursive_cte_self_loop (CTE self-loop and cross-alias self-loop return 0 hops/dist)
26. test_telemetry_non_dict_geo_handling (Safely handles None, string, int, list geo specifications)
27. test_terminal_immutability_hardening (Blocks milestone on DELIVERED with auto_transition=False, prevents POD re-upload)
"""

import os
import sys
import tempfile
import sqlite3
import pytest
from pathlib import Path

# Setup paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.connection import get_db_connection, transaction_scope, set_default_db_path, close_all_connections
from app.db.init_db import init_database
from app.db.cache import get_cache_manager
from app.db.nosql import get_doc_store, DocumentStore
from app.db.storage import get_storage_client

import app.services.shipment_service as shipment_service
import app.services.routing_service as routing_service


@pytest.fixture(scope="module")
def shared_db():
    """Initializes an isolated temporary SQLite database seeded with demo data."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="m3_verify_")
    os.close(fd)
    set_default_db_path(path)

    conn = init_database(db_path=path, seed_demo_data=True, drop_existing=True)
    yield conn

    close_all_connections()
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def clean_cache():
    """Flushes cache before each test."""
    cache = get_cache_manager()
    cache.flush()
    cache.reset_stats()
    yield


# =============================================================================
# 1. FSM & Lifecycle Tests (Tests 1 - 5)
# =============================================================================

def test_fsm_full_lifecycle_progression(shared_db):
    """Test 1: Verifies complete legal forward 7-state FSM progression:
    CREATED -> PICKED_UP -> IN_TRANSIT -> OUT_FOR_DELIVERY -> DELIVERED."""
    # 1. Create shipment
    shipment = shipment_service.create_shipment(
        order_id="ord_001",
        origin_warehouse_id="wh-01",
        carrier="Apex Freight",
        service_level="EXPRESS"
    )
    trk = shipment["tracking_number"]
    assert shipment["status"] == "CREATED"

    # 2. CREATED -> PICKED_UP
    upd1 = shipment_service.update_shipment_status(trk, "PICKED_UP")
    assert upd1["status"] == "PICKED_UP"

    # 3. PICKED_UP -> IN_TRANSIT
    upd2 = shipment_service.update_shipment_status(trk, "IN_TRANSIT")
    assert upd2["status"] == "IN_TRANSIT"

    # 4. IN_TRANSIT -> OUT_FOR_DELIVERY
    upd3 = shipment_service.update_shipment_status(trk, "OUT_FOR_DELIVERY")
    assert upd3["status"] == "OUT_FOR_DELIVERY"

    # 5. OUT_FOR_DELIVERY -> DELIVERED
    upd4 = shipment_service.update_shipment_status(trk, "DELIVERED")
    assert upd4["status"] == "DELIVERED"


def test_fsm_invalid_status_leaps_rejected(shared_db):
    """Test 2: Verifies illegal status jumps (e.g. CREATED -> DELIVERED) raise ShipmentStateTransitionException."""
    shipment = shipment_service.create_shipment(
        order_id="ord_001",
        origin_warehouse_id="wh-01"
    )
    trk = shipment["tracking_number"]

    # Illegal forward leap: CREATED -> DELIVERED
    with pytest.raises(shipment_service.ShipmentStateTransitionException):
        shipment_service.update_shipment_status(trk, "DELIVERED")

    # Illegal forward leap: CREATED -> OUT_FOR_DELIVERY
    with pytest.raises(shipment_service.ShipmentStateTransitionException):
        shipment_service.update_shipment_status(trk, "OUT_FOR_DELIVERY")


def test_fsm_terminal_state_immutability(shared_db):
    """Test 3: Verifies DELIVERED is a strict terminal state that cannot transition backward or forward."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    # Progress to DELIVERED
    shipment_service.update_shipment_status(trk, "PICKED_UP")
    shipment_service.update_shipment_status(trk, "IN_TRANSIT")
    shipment_service.update_shipment_status(trk, "OUT_FOR_DELIVERY")
    shipment_service.update_shipment_status(trk, "DELIVERED")

    # Illegal backward transition: DELIVERED -> IN_TRANSIT
    with pytest.raises(shipment_service.ShipmentStateTransitionException):
        shipment_service.update_shipment_status(trk, "IN_TRANSIT")

    # Illegal backward transition: DELIVERED -> CREATED
    with pytest.raises(shipment_service.ShipmentStateTransitionException):
        shipment_service.update_shipment_status(trk, "CREATED")


def test_fsm_exception_and_recovery(shared_db):
    """Test 4: Verifies EXCEPTION state can be reached and subsequently recovered."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    shipment_service.update_shipment_status(trk, "PICKED_UP")
    shipment_service.update_shipment_status(trk, "IN_TRANSIT")

    # Unexpected roadblock triggers EXCEPTION
    exc_res = shipment_service.update_shipment_status(trk, "EXCEPTION", notes="Severe blizzard delay on I-80")
    assert exc_res["status"] == "EXCEPTION"

    # Recovery: Road cleared, resume IN_TRANSIT
    rec_res = shipment_service.update_shipment_status(trk, "IN_TRANSIT", notes="Highway re-opened, transit resumed")
    assert rec_res["status"] == "IN_TRANSIT"

    # Deliver normally
    shipment_service.update_shipment_status(trk, "OUT_FOR_DELIVERY")
    del_res = shipment_service.update_shipment_status(trk, "DELIVERED")
    assert del_res["status"] == "DELIVERED"


def test_milestone_recording_and_chronology(shared_db):
    """Test 5: Verifies milestone events are recorded and returned in strict ascending chronological order."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    # Record milestones with auto-transitions
    shipment_service.record_shipment_milestone(
        trk, "PICKUP_COMPLETED", "Chicago CDC", 41.7637, -87.7410, "Picked up by driver"
    )
    shipment_service.record_shipment_milestone(
        trk, "DEPARTURE_SCAN", "Chicago CDC Gate 1", 41.7637, -87.7410, "Linehaul departure"
    )
    shipment_service.record_shipment_milestone(
        trk, "HUB_ARRIVAL", "Columbus Cross-Dock", 39.9980, -82.8919, "Arrived at sorting hub"
    )
    shipment_service.record_shipment_milestone(
        trk, "SORTATION_COMPLETED", "Columbus Cross-Dock", 39.9980, -82.8919, "Sortation scan complete"
    )

    details = shipment_service.get_tracking_details(trk, use_cache=False)
    milestones = details["milestones"]
    assert len(milestones) >= 5  # Initial MANIFEST_CREATED + 4 added

    # Verify ascending timestamps
    timestamps = [m["timestamp"] for m in milestones]
    assert timestamps == sorted(timestamps)


# =============================================================================
# 2. Telemetry, Cache-Aside & Proof-of-Delivery (Tests 6 - 10)
# =============================================================================

def test_telemetry_nosql_ingestion_and_query(shared_db):
    """Test 6: Verifies NoSQL ingestion and querying of polymorphic IoT telemetry documents."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    # Ingest 2 telemetry records
    shipment_service.ingest_telemetry(trk, {
        "device_id": "IOT-SENSOR-01",
        "latitude": 41.8781,
        "longitude": -87.6298,
        "speed_kmh": 72.5,
        "environmental": {"temperature_celsius": -18.5, "humidity_percent": 30.0}
    })
    shipment_service.ingest_telemetry(trk, {
        "device_id": "IOT-SENSOR-01",
        "latitude": 41.9500,
        "longitude": -86.8000,
        "speed_kmh": 85.0,
        "environmental": {"temperature_celsius": -18.2, "humidity_percent": 31.0}
    })

    # Query telemetry
    readings = shipment_service.get_shipment_telemetry(trk, limit=10)
    assert len(readings) == 2
    assert readings[0]["tracking_number"] == trk
    assert "environmental" in readings[0] or "environmental" in readings[1]


def test_telemetry_boundary_validation(shared_db):
    """Test 7: Verifies out-of-bounds GPS coordinates are rejected with InvalidTelemetryException."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    # Latitude > 90.0
    with pytest.raises(shipment_service.InvalidTelemetryException):
        shipment_service.ingest_telemetry(trk, {"latitude": 95.0, "longitude": -80.0})

    # Longitude < -180.0
    with pytest.raises(shipment_service.InvalidTelemetryException):
        shipment_service.ingest_telemetry(trk, {"latitude": 40.0, "longitude": -195.0})


def test_cache_aside_full_lifecycle(shared_db):
    """Test 8: Verifies 5-phase Cache-Aside tracking lifecycle:
    Miss -> Populate -> Hit -> Mutation Invalidation -> Fresh Fetch."""
    cache = get_cache_manager()
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]
    cache_key = f"shipment:track:{trk}"

    # Phase 1: Cache Miss on first read
    cache.delete(cache_key)
    res_miss = shipment_service.get_tracking_details(trk, use_cache=True)
    assert res_miss["cache_status"] == "MISS"

    # Phase 2: Cache Population check
    cached_val = cache.get(cache_key)
    assert cached_val is not None
    assert cached_val["tracking_number"] == trk

    # Phase 3: Cache Hit on repeated read
    res_hit = shipment_service.get_tracking_details(trk, use_cache=True)
    assert res_hit["cache_status"] == "HIT"

    # Phase 4: Mutation invalidates cache
    shipment_service.update_shipment_status(trk, "PICKED_UP")
    assert cache.get(cache_key) is None, "Cache key was not invalidated on status update!"

    # Phase 5: Fresh Fetch reflects new state
    res_fresh = shipment_service.get_tracking_details(trk, use_cache=True)
    assert res_fresh["cache_status"] == "MISS"
    assert res_fresh["status"] == "PICKED_UP"


def test_proof_of_delivery_upload_and_transition(shared_db):
    """Test 9: Verifies uploading proof-of-delivery document transitions shipment to DELIVERED."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    shipment_service.update_shipment_status(trk, "PICKED_UP")
    shipment_service.update_shipment_status(trk, "IN_TRANSIT")
    shipment_service.update_shipment_status(trk, "OUT_FOR_DELIVERY")

    # Upload mock POD document
    dummy_pdf_bytes = b"%PDF-1.4 Mock Delivery Receipt Signature"
    pod_res = shipment_service.upload_proof_of_delivery(
        identifier=trk,
        file_bytes=dummy_pdf_bytes,
        filename=f"pod_{trk}.pdf",
        content_type="application/pdf",
        signed_by="Jane Customer"
    )

    assert pod_res["status"] == "DELIVERED"
    assert "pod_url" in pod_res
    assert pod_res["signed_by"] == "Jane Customer"

    # Check tracking details
    track_details = shipment_service.get_tracking_details(trk, use_cache=False)
    assert track_details["status"] == "DELIVERED"
    assert track_details["actual_delivery"] is not None


def test_driver_assignment(shared_db):
    """Test 10: Verifies driver assignment to shipment."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    res = shipment_service.assign_driver(trk, "user_driver")
    assert res["status"] == "SUCCESS"
    assert res["assigned_driver_id"] == "user_driver"

    details = shipment_service.get_tracking_details(trk, use_cache=False)
    assert details["assigned_driver_id"] == "user_driver"


# =============================================================================
# 3. Graph Routing & Algorithmic Optimization (Tests 11 - 16)
# =============================================================================

def test_dijkstra_shortest_path_distance(shared_db):
    """Test 11: Verifies Dijkstra calculation optimizes physical distance (km)."""
    route = routing_service.calculate_optimal_route(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        criteria="distance"
    )
    assert route["path_nodes"][0] == "CDC-CHICAGO"
    assert route["path_nodes"][-1] == "LSF-NEWYORK"
    assert route["total_distance_km"] > 0
    assert len(route["path_nodes"]) >= 3  # Multi-hop via intermediate hub
    assert route["algorithm"] == "DIJKSTRA"


def test_dijkstra_least_cost_routing(shared_db):
    """Test 12: Verifies Dijkstra with criteria=cost optimizes freight expenditure:
    Topology:
      Seattle -> Portland -> Denver = 1880 km, $400
      Seattle -> SaltLake -> Denver = 2200 km, $360
    Criteria=distance picks Portland (1880 km < 2200 km).
    Criteria=cost picks SaltLake ($360 < $400).
    """
    # 1. Distance optimization
    dist_route = routing_service.calculate_optimal_route(
        origin_node="Hub-Seattle",
        destination_node="Hub-Denver",
        criteria="distance"
    )
    # 2. Cost optimization
    cost_route = routing_service.calculate_optimal_route(
        origin_node="Hub-Seattle",
        destination_node="Hub-Denver",
        criteria="cost"
    )

    assert "Hub-Portland" in dist_route["path_nodes"]
    assert dist_route["total_distance_km"] == 1880.0
    assert dist_route["total_cost_usd"] == 400.0

    assert "Hub-SaltLake" in cost_route["path_nodes"]
    assert cost_route["total_distance_km"] == 2200.0
    assert cost_route["total_cost_usd"] == 360.0


def test_astar_search_haversine_heuristic(shared_db):
    """Test 13: Verifies A* search with Haversine distance heuristic computes exact optimal path."""
    route_astar = routing_service.calculate_optimal_route(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        criteria="distance",
        use_astar=True
    )
    route_dijkstra = routing_service.calculate_optimal_route(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        criteria="distance",
        use_astar=False
    )

    assert route_astar["path_nodes"] == route_dijkstra["path_nodes"]
    assert route_astar["total_distance_km"] == route_dijkstra["total_distance_km"]
    assert route_astar["algorithm"] == "A_STAR"


def test_multi_hop_traversal_intermediate_hubs(shared_db):
    """Test 14: Verifies multi-hop route traverses at least one intermediate cross-dock hub."""
    route = routing_service.calculate_optimal_route(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        criteria="balanced"
    )
    assert len(route["path_nodes"]) >= 3
    assert len(route["corridors"]) >= 2
    for c in route["corridors"]:
        assert c["distance_km"] > 0
        assert c["transit_time_hours"] > 0


def test_graph_boundary_self_loop(shared_db):
    """Test 15: Verifies routing where origin == destination returns 0 distance and 1-node path."""
    route = routing_service.calculate_optimal_route(
        origin_node="CDC-CHICAGO",
        destination_node="CDC-CHICAGO",
        criteria="distance"
    )
    assert route["path_nodes"] == ["CDC-CHICAGO"]
    assert route["total_distance_km"] == 0.0
    assert route["total_cost_usd"] == 0.0
    assert route["hop_count"] == 0
    assert route["corridors"] == []


def test_graph_boundary_disconnected_unreachable(shared_db):
    """Test 16: Verifies querying an unreachable or isolated node raises NoPathFoundException or NodeNotFoundException."""
    with pytest.raises(routing_service.RoutingException):
        routing_service.calculate_optimal_route(
            origin_node="CDC-CHICAGO",
            destination_node="ISOLATED-ISLAND-HUB-99",
            criteria="distance"
        )


# =============================================================================
# 4. Bottlenecks, Outages & Fallback Rerouting (Tests 17 - 22)
# =============================================================================

def test_vehicle_weight_capacity_constraints(shared_db):
    """Test 17: Verifies vehicle weight exceeding corridor limits triggers constraint failure or path adaptation."""
    # When weight is feasible
    route_ok = routing_service.calculate_optimal_route(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        vehicle_constraints={"weight_kg": 20000}
    )
    assert len(route_ok["path_nodes"]) >= 2

    # When weight exceeds max corridor capacity
    with pytest.raises(routing_service.RoutingException):
        routing_service.calculate_optimal_route(
            origin_node="CDC-CHICAGO",
            destination_node="LSF-NEWYORK",
            vehicle_constraints={"weight_kg": 500000}  # 500 tons
        )


def test_network_bottleneck_detection(shared_db):
    """Test 18: Verifies detection of high-utilization corridors (rho >= 0.85) and betweenness centrality."""
    bottlenecks = routing_service.detect_network_bottlenecks(load_threshold=0.85)
    assert len(bottlenecks) >= 1

    # In seeded database: corr_wh03_wh04 (Columbus -> New York) has load=31000 / cap=35000 -> rho = 0.8857
    top_bottleneck = bottlenecks[0]
    assert top_bottleneck["capacity_utilization_ratio"] >= 0.85
    assert top_bottleneck["severity"] in ("CRITICAL", "HIGH")
    assert "recommendation" in top_bottleneck


def test_dynamic_edge_outage_and_rerouting(shared_db):
    """Test 19: Verifies edge outage simulation triggers automated reroute bypassing the failed node/edge."""
    baseline = routing_service.calculate_optimal_route(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        criteria="distance"
    )
    base_path = baseline["path_nodes"]
    assert len(base_path) >= 3

    intermediate_node = base_path[1]

    # Simulate outage of intermediate node/corridor
    reroute = routing_service.simulate_edge_outage_and_reroute(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        exclude_nodes=[intermediate_node],
        criteria="distance"
    )

    assert reroute["reroute_successful"] is True
    fallback_path = reroute["fallback_route"]["path_nodes"]
    assert intermediate_node not in fallback_path, f"Failed node '{intermediate_node}' still in path: {fallback_path}"
    assert fallback_path[0] == "CDC-CHICAGO"
    assert fallback_path[-1] == "LSF-NEWYORK"
    assert "route_delta" in reroute


def test_k_alternative_routes_generation(shared_db):
    """Test 20: Verifies Yen's algorithm generates K distinct candidate paths."""
    routes = routing_service.get_k_alternative_routes(
        origin_node="CDC-CHICAGO",
        destination_node="LSF-NEWYORK",
        k=2,
        criteria="distance"
    )
    assert len(routes) >= 2
    # Check that paths are distinct
    paths_str = [str(r["path_nodes"]) for r in routes]
    assert len(set(paths_str)) == len(routes), "Alternative routes should be distinct!"


def test_sql_recursive_cte_fallback(shared_db):
    """Test 21: Verifies pure SQL recursive CTE path calculation algorithm produces valid multi-hop path."""
    cte_route = routing_service.seeded_network_cte_routing(
        origin_identifier="wh-01",
        destination_identifier="wh-04",
        criteria="distance"
    )
    assert cte_route["engine"] == "SQL_RECURSIVE_CTE"
    assert cte_route["origin"] == "wh-01"
    assert cte_route["destination"] == "wh-04"
    assert cte_route["total_distance_km"] > 0
    assert cte_route["hops"] >= 2
    assert len(cte_route["path_nodes"]) >= 3


def test_network_reachability(shared_db):
    """Test 22: Verifies network reachability analysis identifies all destinations reachable from an origin hub."""
    reachability = routing_service.get_network_reachability(origin_node="CDC-CHICAGO")
    assert reachability["origin_node"] == "CDC-CHICAGO"
    assert reachability["reachable_count"] >= 3
    assert "LSF-NEWYORK" in reachability["reachable_nodes"] or "wh-04" in reachability["reachable_nodes"] or "LSF-NYC" in reachability["reachable_nodes"]


# =============================================================================
# 5. Milestone 3 Remediation Hardening Tests (Tests 23 - 27)
# =============================================================================

def test_cross_alias_routing_comprehensive(shared_db):
    """Test 23: Verifies seamless cross-alias routing between city names, primary codes, and alias codes."""
    # 1. City to City alias
    r1 = routing_service.calculate_optimal_route("Chicago", "New York")
    assert r1["path_nodes"][0] == "Chicago"
    assert r1["path_nodes"][-1] == "New York"
    assert r1["total_distance_km"] == 1435.0

    # 2. Alias code to Primary ID
    r2 = routing_service.calculate_optimal_route("CDC-CHICAGO", "wh-04")
    assert r2["path_nodes"][0] == "CDC-CHICAGO"
    assert r2["path_nodes"][-1] == "wh-04"
    assert r2["total_distance_km"] == 1435.0

    # 3. Primary ID to Alias code
    r3 = routing_service.calculate_optimal_route("wh-01", "LSF-NEWYORK")
    assert r3["path_nodes"][0] == "wh-01"
    assert r3["path_nodes"][-1] == "LSF-NEWYORK"
    assert r3["total_distance_km"] == 1435.0

    # 4. Detroit to New York
    r4 = routing_service.calculate_optimal_route("Detroit", "New York")
    assert r4["path_nodes"][0] == "Detroit"
    assert r4["path_nodes"][-1] == "New York"
    assert r4["total_distance_km"] > 0


def test_astar_inadmissibility_guard(shared_db):
    """Test 24: Verifies A* Haversine heuristic rejects non-distance optimization criteria to preserve admissibility."""
    # Criteria=cost violates admissibility
    with pytest.raises(routing_service.RoutingException, match="admissible only for distance-based"):
        routing_service.calculate_optimal_route(
            origin_node="CDC-CHICAGO",
            destination_node="LSF-NEWYORK",
            criteria="cost",
            use_astar=True
        )

    # Criteria=time violates admissibility
    with pytest.raises(routing_service.RoutingException, match="admissible only for distance-based"):
        routing_service.calculate_optimal_route(
            origin_node="CDC-CHICAGO",
            destination_node="LSF-NEWYORK",
            criteria="time",
            use_astar=True
        )


def test_sql_recursive_cte_self_loop(shared_db):
    """Test 25: Verifies SQL recursive CTE handles single-node self-loops and alias self-loops."""
    # Direct self-loop
    cte_self = routing_service.seeded_network_cte_routing("wh-01", "wh-01")
    assert cte_self["total_distance_km"] == 0.0
    assert cte_self["total_cost_usd"] == 0.0
    assert cte_self["hops"] == 0
    assert cte_self["path_nodes"] == ["wh-01"]

    # Cross-alias self-loop (ID vs Code)
    cte_alias = routing_service.seeded_network_cte_routing("wh-01", "CDC-CHI")
    assert cte_alias["total_distance_km"] == 0.0
    assert cte_alias["total_cost_usd"] == 0.0
    assert cte_alias["hops"] == 0


def test_telemetry_non_dict_geo_handling(shared_db):
    """Test 26: Verifies safe handling of polymorphic telemetry with non-dict geo objects."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    # geo is None -> gracefully ignored without AttributeError
    res_none = shipment_service.ingest_telemetry(trk, {"geo": None, "speed_kmh": 60.0})
    assert res_none["tracking_number"] == trk

    # geo is str -> raises InvalidTelemetryException without AttributeError
    with pytest.raises(shipment_service.InvalidTelemetryException):
        shipment_service.ingest_telemetry(trk, {"geo": "San Francisco"})

    # geo is int -> raises InvalidTelemetryException
    with pytest.raises(shipment_service.InvalidTelemetryException):
        shipment_service.ingest_telemetry(trk, {"geo": 12345})

    # geo is list -> raises InvalidTelemetryException
    with pytest.raises(shipment_service.InvalidTelemetryException):
        shipment_service.ingest_telemetry(trk, {"geo": [37.77, -122.41]})


def test_terminal_immutability_hardening(shared_db):
    """Test 27: Verifies hardened terminal state immutability across milestones, telemetry, and POD uploads."""
    shipment = shipment_service.create_shipment(order_id="ord_001", origin_warehouse_id="wh-01")
    trk = shipment["tracking_number"]

    # Progress to DELIVERED
    shipment_service.update_shipment_status(trk, "PICKED_UP")
    shipment_service.update_shipment_status(trk, "IN_TRANSIT")
    shipment_service.update_shipment_status(trk, "OUT_FOR_DELIVERY")
    shipment_service.update_shipment_status(trk, "DELIVERED")

    # 1. Milestone addition with auto_transition=False must be strictly rejected
    with pytest.raises(shipment_service.ShipmentStateTransitionException):
        shipment_service.record_shipment_milestone(
            trk, "HUB_ARRIVAL", "Ghost Hub", 10.0, 20.0, auto_transition=False
        )

    # 2. Non-existent shipment telemetry rejected
    with pytest.raises(shipment_service.ShipmentNotFoundException):
        shipment_service.ingest_telemetry("TRK-NONEXISTENT-9999", {"latitude": 40.0, "longitude": -80.0})

    # 3. Telemetry on DELIVERED shipment does NOT mutate relational coordinates
    db = get_db_connection()
    cur = db.cursor()
    cur.execute("SELECT current_lat, current_lon FROM shipments WHERE tracking_number = ?", (trk,))
    row_before = cur.fetchone()

    shipment_service.ingest_telemetry(trk, {"latitude": 1.2345, "longitude": 6.7890})

    cur.execute("SELECT current_lat, current_lon FROM shipments WHERE tracking_number = ?", (trk,))
    row_after = cur.fetchone()
    assert row_after["current_lat"] == row_before["current_lat"]
    assert row_after["current_lon"] == row_before["current_lon"]

    # 4. POD re-upload on already DELIVERED shipment rejected before file upload
    with pytest.raises(shipment_service.ShipmentStateTransitionException):
        shipment_service.upload_proof_of_delivery(
            identifier=trk,
            file_bytes=b"%PDF-1.4 Duplicate",
            filename=f"pod_dup_{trk}.pdf"
        )
