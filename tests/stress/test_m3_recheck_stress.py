"""
Adversarial Empirical Stress & Verification Test Suite for Milestone 3 Recheck.
Focus areas:
1. Full combinatorial cross-alias routing matrix (IDs, codes, city names, airport codes)
2. A* non-distance admissibility guards (cost, time, balanced)
3. Self-loops (direct and cross-alias identity queries)
4. Heavy multi-threaded concurrency (concurrent routing + telemetry ingestion + FSM transitions + Cache-Aside)
"""

import os
import sys
import time
import random
import tempfile
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.connection import (
    get_db_connection,
    set_default_db_path,
    close_all_connections
)
from app.db.init_db import init_database
from app.db.cache import get_cache_manager
from app.db.nosql import get_doc_store
import app.services.shipment_service as shipment_service
import app.services.routing_service as routing_service
from app.services.routing_service import (
    calculate_optimal_route,
    simulate_edge_outage_and_reroute,
    get_k_alternative_routes,
    RoutingException,
    NoPathFoundException,
    NodeNotFoundException
)


def run_test_suite():
    print("=" * 80)
    print("Milestone 3 Recheck: Adversarial Stress & Concurrency Verification")
    print("=" * 80)

    # 1. Setup isolated database
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="m3_recheck_")
    os.close(fd)
    set_default_db_path(db_path)
    init_database(db_path=db_path, seed_demo_data=True, drop_existing=True)

    cache = get_cache_manager()
    cache.flush()
    cache.reset_stats()

    passed_checks = 0
    failed_checks = 0

    def assert_check(condition: bool, description: str):
        nonlocal passed_checks, failed_checks
        if condition:
            print(f"  [PASS] {description}")
            passed_checks += 1
        else:
            print(f"  [FAIL] {description}")
            failed_checks += 1

    # =========================================================================
    # Task 3.1: Cross-Alias Routing Combinations
    # =========================================================================
    print("\n--- Test Suite 3.1: Cross-Alias Routing Matrix ---")

    chicago_aliases = ["wh-01", "CDC-CHI", "CDC-CHICAGO", "wh_cdc_chi", "Chicago", "CHI"]
    nyc_aliases = ["wh-04", "LSF-NYC", "LSF-NEWYORK", "wh_lsf_nyc", "New York", "NYC", "Corona"]

    # Test all pairs of Chicago -> New York
    pair_count = 0
    all_chicago_nyc_passed = True
    for orig in chicago_aliases:
        for dest in nyc_aliases:
            pair_count += 1
            try:
                res = calculate_optimal_route(orig, dest, criteria="distance")
                # Expected: route via Columbus (HUB-CMH) or Detroit, total distance 1435.0 km
                if res["total_distance_km"] <= 0 or res["hop_count"] < 1:
                    all_chicago_nyc_passed = False
                    print(f"    Failed on {orig} -> {dest}: total_distance_km={res['total_distance_km']}")
                if res["path_nodes"][0] != orig or res["path_nodes"][-1] != dest:
                    all_chicago_nyc_passed = False
                    print(f"    Failed label match on {orig} -> {dest}: path={res['path_nodes']}")
            except Exception as e:
                all_chicago_nyc_passed = False
                print(f"    Exception on {orig} -> {dest}: {e}")

    assert_check(
        all_chicago_nyc_passed,
        f"All {pair_count} combinations of Chicago -> New York aliases resolved accurately with preserved caller labels"
    )

    # Test other regional pairs: Detroit -> Columbus
    detroit_aliases = ["wh-02", "RDC-DET", "RDC-DETROIT", "wh_rdc_det", "Detroit", "DET"]
    columbus_aliases = ["wh-03", "HUB-CMH", "HUB-COLUMBUS", "wh_hub_cmh", "Columbus", "CMH"]
    det_cmh_passed = True
    det_cmh_count = 0
    for orig in detroit_aliases:
        for dest in columbus_aliases:
            det_cmh_count += 1
            try:
                res = calculate_optimal_route(orig, dest, criteria="distance")
                if res["total_distance_km"] != 325.0:
                    det_cmh_passed = False
                    print(f"    Failed on {orig} -> {dest}: dist={res['total_distance_km']}")
            except Exception as e:
                det_cmh_passed = False
                print(f"    Exception on {orig} -> {dest}: {e}")

    assert_check(
        det_cmh_passed,
        f"All {det_cmh_count} combinations of Detroit -> Columbus aliases resolved correctly (325.0 km)"
    )

    # Test Reverse: New York -> Chicago
    rev_res = calculate_optimal_route("New York", "Chicago")
    assert_check(
        rev_res["total_distance_km"] == 1435.0 and rev_res["path_nodes"] == ["New York", "HUB-CMH", "Chicago"],
        "Reverse routing New York -> Chicago resolved correctly"
    )

    # =========================================================================
    # Task 3.2: A* Non-Distance Admissibility Guards
    # =========================================================================
    print("\n--- Test Suite 3.2: A* Admissibility Guards ---")

    inadmissible_criteria = ["cost", "min_cost", "freight_cost", "time", "fastest", "balanced"]
    all_inadmissible_rejected = True

    for crit in inadmissible_criteria:
        try:
            calculate_optimal_route("Chicago", "New York", criteria=crit, use_astar=True)
            all_inadmissible_rejected = False
            print(f"    A* unexpectedly succeeded for non-distance criteria: '{crit}'")
        except RoutingException as e:
            # Expected
            pass
        except Exception as e:
            all_inadmissible_rejected = False
            print(f"    Wrong exception type for criteria '{crit}': {type(e).__name__}: {e}")

    assert_check(
        all_inadmissible_rejected,
        f"A* rejected all {len(inadmissible_criteria)} non-distance criteria with RoutingException"
    )

    # Admissible criteria must succeed: distance
    astar_dist = calculate_optimal_route("Chicago", "New York", criteria="distance", use_astar=True)
    dijk_dist = calculate_optimal_route("Chicago", "New York", criteria="distance", use_astar=False)
    assert_check(
        astar_dist["total_distance_km"] == dijk_dist["total_distance_km"] == 1435.0,
        f"A* distance ({astar_dist['total_distance_km']} km) equals Dijkstra distance ({dijk_dist['total_distance_km']} km)"
    )

    # =========================================================================
    # Task 3.3: Self-Loops (Direct & Cross-Alias)
    # =========================================================================
    print("\n--- Test Suite 3.3: Direct & Cross-Alias Self-Loops ---")

    # Direct identical identity
    direct_self_loops = ["wh-01", "CDC-CHICAGO", "Chicago", "wh-02", "Detroit", "wh-04", "New York"]
    direct_passed = True
    for node in direct_self_loops:
        res = calculate_optimal_route(node, node)
        if res["total_distance_km"] != 0.0 or res["total_cost_usd"] != 0.0 or res["path_nodes"] != [node]:
            direct_passed = False
            print(f"    Direct self-loop failed for {node}: {res}")

    assert_check(direct_passed, f"All {len(direct_self_loops)} direct self-loops returned 0 distance and 1-node path")

    # Cross-alias self-loops (two different aliases resolving to same canonical hub)
    cross_alias_pairs = [
        ("Chicago", "CDC-CHI"),
        ("wh-01", "Chicago"),
        ("CDC-CHICAGO", "wh-01"),
        ("wh_cdc_chi", "CDC-CHICAGO"),
        ("Detroit", "wh-02"),
        ("RDC-DETROIT", "Detroit"),
        ("wh_rdc_det", "RDC-DET"),
        ("Columbus", "HUB-CMH"),
        ("HUB-COLUMBUS", "wh-03"),
        ("New York", "LSF-NYC"),
        ("NYC", "wh-04"),
        ("Corona", "New York"),
        ("LSF-NEWYORK", "Corona")
    ]
    cross_passed = True
    for orig, dest in cross_alias_pairs:
        res = calculate_optimal_route(orig, dest)
        if res["total_distance_km"] != 0.0 or res["hop_count"] != 0:
            cross_passed = False
            print(f"    Cross-alias self-loop failed for {orig} -> {dest}: {res}")

    assert_check(cross_passed, f"All {len(cross_alias_pairs)} cross-alias self-loops returned 0 distance and 0 hops")

    # =========================================================================
    # Task 4: Heavy Multi-Threaded Concurrency
    # =========================================================================
    print("\n--- Test Suite 4: Heavy Multi-Threaded Concurrency (Routing + Telemetry + FSM) ---")

    # Seed orders for concurrent test workers
    conn = get_db_connection()
    with conn:
        for i in range(100):
            conn.execute(
                """INSERT INTO orders (id, order_number, customer_id, status, total_amount, shipping_address, destination_city, destination_lat, destination_lon)
                   VALUES (?, ?, ?, 'CONFIRMED', 100.0, '123 Main St, Chicago, IL', 'Chicago', 41.8781, -87.6298)""",
                (f"ord_concurrent_{i}", f"ORD-CONC-{i}", "user_cust_alice")
            )

    # Create several test shipments
    shipments = []
    for i in range(5):
        s = shipment_service.create_shipment(
            order_id=f"ord_concurrent_{i}",
            origin_warehouse_id="wh-01",
            carrier="ConcurrentCarrier",
            service_level="EXPRESS"
        )
        shipments.append(s)

    NUM_ROUTING_THREADS = 12
    NUM_TELEMETRY_THREADS = 12
    NUM_FSM_THREADS = 6
    TOTAL_THREADS = NUM_ROUTING_THREADS + NUM_TELEMETRY_THREADS + NUM_FSM_THREADS

    ROUTING_OPS_PER_THREAD = 25
    TELEMETRY_OPS_PER_THREAD = 30
    FSM_OPS_PER_THREAD = 10

    concurrency_errors = []
    concurrency_lock = threading.Lock()

    def routing_worker(worker_id: int):
        midwest_origins = ["Chicago", "wh-01", "CDC-CHICAGO", "Detroit", "wh-02"]
        midwest_dests = ["New York", "wh-04", "LSF-NYC", "Columbus", "HUB-CMH"]
        pacific_pairs = [
            ("Hub-Seattle", "Hub-Denver"),
            ("Hub-Seattle", "Hub-Portland"),
            ("Hub-Portland", "Hub-Denver"),
            ("Hub-Seattle", "Hub-SaltLake"),
            ("Hub-SaltLake", "Hub-Denver")
        ]
        criteria_list = ["distance", "cost", "time", "balanced"]

        for op in range(ROUTING_OPS_PER_THREAD):
            try:
                if op % 3 == 0:
                    orig, dest = random.choice(pacific_pairs)
                    crit = "distance"
                else:
                    orig = random.choice(midwest_origins)
                    dest = random.choice(midwest_dests)
                    crit = random.choice(criteria_list)

                r = calculate_optimal_route(orig, dest, criteria=crit)
                assert r["total_distance_km"] >= 0.0

                # Also run occasional edge outage reroute
                if op % 5 == 0:
                    reroute = simulate_edge_outage_and_reroute("Chicago", "New York", "corr_wh03_wh04")
                    assert reroute["reroute_successful"] is True

            except Exception as e:
                with concurrency_lock:
                    concurrency_errors.append(f"RoutingWorker-{worker_id} op {op} failed: {type(e).__name__}: {e}")

    def telemetry_worker(worker_id: int):
        for op in range(TELEMETRY_OPS_PER_THREAD):
            try:
                sh = random.choice(shipments)
                trk = sh["tracking_number"]
                payload = {
                    "device_id": f"SENSOR-TH-{worker_id}",
                    "worker_id": worker_id,
                    "op": op,
                    "timestamp": time.time(),
                    "latitude": 41.8781 + random.uniform(-0.1, 0.1),
                    "longitude": -87.6298 + random.uniform(-0.1, 0.1),
                    "environmental": {
                        "temperature_celsius": round(random.uniform(-20.0, 5.0), 2),
                        "battery_pct": random.randint(10, 100)
                    }
                }
                # Ingest telemetry
                rec = shipment_service.ingest_telemetry(trk, payload)
                assert rec is not None
                # Query tracking details (exercises Cache-Aside under concurrent telemetry)
                details = shipment_service.get_tracking_details(trk)
                assert details is not None
                assert details["tracking_number"] == trk
            except Exception as e:
                with concurrency_lock:
                    concurrency_errors.append(f"TelemetryWorker-{worker_id} op {op} failed: {type(e).__name__}: {e}")

    def fsm_worker(worker_id: int):
        for op in range(FSM_OPS_PER_THREAD):
            try:
                # Create dedicated shipment for this worker transition cycle
                order_idx = (worker_id * FSM_OPS_PER_THREAD + op) % 100
                sh = shipment_service.create_shipment(
                    order_id=f"ord_concurrent_{order_idx}",
                    origin_warehouse_id="wh-01",
                    carrier="FSMCarrier",
                    service_level="STANDARD",
                    actor_id="user_driver"
                )
                sh_id = sh["id"]
                trk = sh["tracking_number"]

                # Step 1: PICKED_UP
                shipment_service.update_shipment_status(
                    sh_id,
                    "PICKED_UP",
                    actor_id="user_driver",
                    location={"city": "Chicago", "state": "IL", "latitude": 41.8781, "longitude": -87.6298},
                    notes="Picked up"
                )

                # Step 2: IN_TRANSIT
                shipment_service.update_shipment_status(
                    sh_id,
                    "IN_TRANSIT",
                    actor_id="user_driver",
                    location={"city": "Gary", "state": "IN", "latitude": 41.5934, "longitude": -87.3464},
                    notes="En route"
                )

                # Step 3: Read tracking details to test Cache-Aside
                details = shipment_service.get_tracking_details(trk)
                assert details["status"] == "IN_TRANSIT"

            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                with concurrency_lock:
                    concurrency_errors.append(f"FsmWorker-{worker_id} op {op} failed: {type(e).__name__}: {e}\n{tb}")

    print(f"  Launching {TOTAL_THREADS} concurrent threads ({NUM_ROUTING_THREADS} routing, {NUM_TELEMETRY_THREADS} telemetry, {NUM_FSM_THREADS} FSM)...")
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=TOTAL_THREADS) as executor:
        futures = []
        for i in range(NUM_ROUTING_THREADS):
            futures.append(executor.submit(routing_worker, i))
        for i in range(NUM_TELEMETRY_THREADS):
            futures.append(executor.submit(telemetry_worker, i))
        for i in range(NUM_FSM_THREADS):
            futures.append(executor.submit(fsm_worker, i))

        for f in as_completed(futures):
            f.result()

    elapsed = time.time() - t0
    total_ops = (
        NUM_ROUTING_THREADS * ROUTING_OPS_PER_THREAD +
        NUM_TELEMETRY_THREADS * TELEMETRY_OPS_PER_THREAD +
        NUM_FSM_THREADS * FSM_OPS_PER_THREAD
    )
    print(f"  Completed {total_ops} mixed operations across {TOTAL_THREADS} threads in {elapsed:.2f}s ({total_ops/elapsed:.1f} ops/sec)")

    if concurrency_errors:
        print(f"  Encountered {len(concurrency_errors)} concurrency errors:")
        for err in concurrency_errors[:10]:
            print(f"    - {err}")

    assert_check(
        len(concurrency_errors) == 0,
        f"Multi-threaded concurrency stress test completed with 0 errors across {total_ops} operations"
    )

    # Clean up
    close_all_connections()
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass

    # =========================================================================
    # Final Summary
    # =========================================================================
    print("\n" + "=" * 80)
    print(f"SUMMARY: {passed_checks} PASSED, {failed_checks} FAILED out of {passed_checks + failed_checks} checks.")
    print("=" * 80)

    if failed_checks > 0:
        sys.exit(1)


if __name__ == "__main__":
    run_test_suite()
