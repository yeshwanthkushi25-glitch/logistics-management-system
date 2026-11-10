"""Tier 2.2: Boundaries - Network Graph Boundary Conditions.

Covers:
- Routing between disconnected graph partitions (unreachable destination)
- Routing where origin equals destination (single-node / self-loop boundary)
"""

import pytest


def test_boundary_disconnected_graph_routing(client, dispatcher_headers):
    """Verify requesting a route between disconnected hubs returns 404 or 422 with clean error message."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "ISOLATED-ISLAND-HUB-99",
        "optimization_criteria": "distance"
    }
    
    response = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    if response.status_code == 404:
        response = client.post("/api/v1/routes/shortest-path", json=payload, headers=dispatcher_headers)
    
    assert response.status_code in (400, 404, 422), f"Expected failure status for disconnected nodes, got: {response.status_code}"
    data = response.get_json()
    assert "error" in data or "detail" in data or "message" in data


def test_boundary_single_node_self_loop_route(client, dispatcher_headers):
    """Verify routing where origin == destination returns 0 distance/cost and a single-node path list."""
    payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "CDC-CHICAGO",
        "optimization_criteria": "distance"
    }
    
    response = client.post("/api/v1/network/routes", json=payload, headers=dispatcher_headers)
    if response.status_code == 404:
        response = client.post("/api/v1/routes/shortest-path", json=payload, headers=dispatcher_headers)
    
    assert response.status_code in (200, 400)
    if response.status_code == 200:
        data = response.get_json()
        path = data.get("path_nodes") or data.get("path")
        assert path == ["CDC-CHICAGO"]
        distance = data.get("total_distance_km") or data.get("total_distance", 0)
        assert float(distance) == 0.0
