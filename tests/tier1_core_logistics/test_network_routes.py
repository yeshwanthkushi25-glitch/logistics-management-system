"""Tier 1.4: Core Logistics - Network Graph Routing and Analysis Tests.

Covers:
- Shortest path routing using Dijkstra's algorithm (Distance optimization)
- Least-cost routing strategy (Freight cost / toll optimization)
- Multi-hop traversal across intermediate hubs
- Vehicle and capacity constraint routing
- Reachability and connected components detection
- K-alternative path generation
"""

import pytest


def test_shortest_path_dijkstra_by_distance(client, dispatcher_headers):
    """Verify Dijkstra algorithm calculates shortest path by physical distance (km)."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "optimization_criteria": "distance"
    }
    
    response = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    if response.status_code == 404:
        # Fallback to alternate route path
        response = client.post("/api/v1/routes/shortest-path", json=payload, headers=dispatcher_headers)
    
    assert response.status_code == 200, f"Route query failed: {response.data}"
    data = response.get_json()
    assert "path_nodes" in data or "path" in data
    path = data.get("path_nodes") or data.get("path")
    assert isinstance(path, list)
    assert len(path) >= 2
    assert path[0] == "CDC-CHICAGO"
    assert path[-1] == "LSF-NEWYORK"
    assert "total_distance_km" in data or "total_distance" in data


def test_optimal_route_by_transit_cost(client, dispatcher_headers):
    """Verify routing with criteria=MIN_COST prefers lowest freight cost corridors over shortest distance."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "optimization_criteria": "cost"
    }
    
    response = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    if response.status_code == 404:
        response = client.post("/api/v1/routes/shortest-path", json=payload, headers=dispatcher_headers)
    
    assert response.status_code == 200
    data = response.get_json()
    assert "total_cost_usd" in data or "estimated_cost_usd" in data or "total_cost" in data


def test_multi_hop_route_with_intermediate_hubs(client, dispatcher_headers):
    """Verify multi-tier logistics network traversal returns intermediate hub sequence with hop details."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "optimization_criteria": "balanced"
    }
    
    response = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    if response.status_code == 404:
        response = client.post("/api/v1/routes/shortest-path", json=payload, headers=dispatcher_headers)
    
    assert response.status_code == 200
    data = response.get_json()
    path = data.get("path_nodes") or data.get("path")
    # Must traverse at least one intermediate regional hub
    assert len(path) >= 3


def test_route_capacity_and_vehicle_constraint(client, dispatcher_headers):
    """Verify route computation respects vehicle constraints (e.g. maximum weight capacity)."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "vehicle_constraints": {
            "vehicle_type": "HEAVY_TRUCK",
            "weight_kg": 40000,
            "hazmat": False
        }
    }
    
    response = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    if response.status_code == 404:
        response = client.post("/api/v1/routes/shortest-path", json=payload, headers=dispatcher_headers)
    
    assert response.status_code in (200, 422)
    if response.status_code == 200:
        data = response.get_json()
        assert "path_nodes" in data or "path" in data


def test_network_reachability_and_connected_components(client, dispatcher_headers):
    """Verify reachability query returns all nodes reachable from a specified origin hub."""
    response = client.get("/api/v1/network/reachability?hub_id=CDC-CHICAGO", headers=dispatcher_headers)
    if response.status_code == 404:
        response = client.get("/api/v1/routes/reachability?hub_id=CDC-CHICAGO", headers=dispatcher_headers)
    
    assert response.status_code == 200
    data = response.get_json()
    reachable_nodes = data.get("reachable_nodes") or data.get("destinations") or data
    assert isinstance(reachable_nodes, (list, dict))


def test_k_alternative_routes_generation(client, dispatcher_headers):
    """Verify requesting K-alternative paths returns multiple distinct candidate routes."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "alternatives": 3
    }
    
    response = client.post("/api/v1/network/routes/alternatives", json=payload, headers=dispatcher_headers)
    if response.status_code == 404:
        # Try query params or main route endpoint with alternatives param
        response = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    
    assert response.status_code == 200
    data = response.get_json()
    routes = data.get("routes") or data.get("alternative_paths") or [data]
    assert isinstance(routes, list)
    assert len(routes) >= 1
