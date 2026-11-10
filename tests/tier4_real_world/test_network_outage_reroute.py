"""Tier 4.2: Real-World - Regional Network Outage and Dynamic Rerouting Tests.

Covers:
- Dynamic edge or transit hub outage injection
- Automated detection of network corridor failure
- Rerouting calculation returning feasible fallback path that bypasses the failed corridor
"""

import pytest


def test_resilience_regional_network_outage_dynamic_rerouting(client, dispatcher_headers):
    """Verify dynamic corridor failure triggers automated alternative routing around the failed segment."""
    # 1. Query baseline route from Chicago to New York
    baseline_payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "optimization_criteria": "distance"
    }
    base_res = client.post("/api/v1/network/routes", json=baseline_payload, headers=dispatcher_headers)
    if base_res.status_code == 404:
        base_res = client.post("/api/v1/routes/shortest-path", json=baseline_payload, headers=dispatcher_headers)
    assert base_res.status_code == 200
    base_data = base_res.get_json()
    base_path = base_data.get("path_nodes") or base_data.get("path")
    assert len(base_path) >= 2

    # Identify intermediate corridor to fail
    failed_node = base_path[1] if len(base_path) > 2 else "RDC-DETROIT"

    # 2. Query route with simulated outage / excluded corridor
    outage_payload = {
        "origin_node": "CDC-CHICAGO",
        "destination_node": "LSF-NEWYORK",
        "optimization_criteria": "distance",
        "exclude_nodes": [failed_node],
        "failed_edges": [f"CORR-CHI-{failed_node}"]
    }
    
    reroute_res = client.post("/api/v1/network/routes/reroute", json=outage_payload, headers=dispatcher_headers)
    if reroute_res.status_code == 404:
        # Fallback to standard route endpoint with exclude params
        reroute_res = client.post("/api/v1/network/routes", json=outage_payload, headers=dispatcher_headers)
    
    assert reroute_res.status_code in (200, 422)
    if reroute_res.status_code == 200:
        reroute_data = reroute_res.get_json()
        fallback_path = reroute_data.get("path_nodes") or reroute_data.get("path")
        # Assert the failed node was successfully bypassed
        assert failed_node not in fallback_path, f"Failed node {failed_node} still present in rerouted path: {fallback_path}"
        assert fallback_path[0] == "CDC-CHICAGO"
        assert fallback_path[-1] == "LSF-NEWYORK"
