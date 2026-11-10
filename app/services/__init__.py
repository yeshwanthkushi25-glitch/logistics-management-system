"""
Logistics IMS - Services Layer Package.

Provides high-level business logic and transactional workflows:
- InventoryService: Multi-warehouse stock tracking, reservations, restock, adjustments, and ROP.
- FulfillmentService: ACID transactional order placement, multi-warehouse splitting, rollbacks, and cancellations.
- ShipmentService: 7-state shipment FSM, milestone event logging, NoSQL IoT telemetry, Cache-Aside tracking, and proof-of-delivery.
- RoutingService: Network graph routing (Dijkstra, A* Haversine), K-alternatives, capacity bottleneck detection, dynamic outage rerouting, and SQL recursive CTE.
"""

from app.services.inventory_service import (
    InventoryException,
    InsufficientStockException,
    ProductNotFoundException,
    WarehouseNotFoundException,
    InvalidInventoryOperationException,
    calculate_reorder_point,
    get_inventory_levels,
    allocate_inventory,
    release_inventory,
    restock_inventory,
    adjust_inventory,
)

from app.services.fulfillment_service import (
    FulfillmentException,
    PaymentFailedException,
    OrderNotFoundException,
    OrderStateTransitionException,
    create_order_atomic,
    cancel_order,
    get_order,
    list_orders,
)

from app.services.shipment_service import (
    ShipmentException,
    ShipmentNotFoundException,
    InvalidShipmentStatusException,
    ShipmentStateTransitionException,
    InvalidMilestoneException,
    InvalidTelemetryException,
    ProofOfDeliveryException,
    SHIPMENT_STATES,
    VALID_TRANSITIONS,
    VALID_MILESTONES,
    MILESTONE_STATUS_MAP,
    create_shipment,
    update_shipment_status,
    record_shipment_milestone,
    ingest_telemetry,
    get_shipment_telemetry,
    get_tracking_details,
    upload_proof_of_delivery,
    assign_driver,
)

from app.services.routing_service import (
    RoutingException,
    NodeNotFoundException,
    NoPathFoundException,
    VehicleConstraintViolationException,
    NetworkBottleneckException,
    NetworkGraphManager,
    build_network_graph,
    haversine_distance,
    calculate_optimal_route,
    get_k_alternative_routes,
    detect_network_bottlenecks,
    simulate_edge_outage_and_reroute,
    get_network_reachability,
    seeded_network_cte_routing,
)

__all__ = [
    # Inventory Exceptions
    "InventoryException",
    "InsufficientStockException",
    "ProductNotFoundException",
    "WarehouseNotFoundException",
    "InvalidInventoryOperationException",
    # Inventory Functions
    "calculate_reorder_point",
    "get_inventory_levels",
    "allocate_inventory",
    "release_inventory",
    "restock_inventory",
    "adjust_inventory",
    # Fulfillment Exceptions
    "FulfillmentException",
    "PaymentFailedException",
    "OrderNotFoundException",
    "OrderStateTransitionException",
    # Fulfillment Functions
    "create_order_atomic",
    "cancel_order",
    "get_order",
    "list_orders",
    # Shipment Exceptions & Enums
    "ShipmentException",
    "ShipmentNotFoundException",
    "InvalidShipmentStatusException",
    "ShipmentStateTransitionException",
    "InvalidMilestoneException",
    "InvalidTelemetryException",
    "ProofOfDeliveryException",
    "SHIPMENT_STATES",
    "VALID_TRANSITIONS",
    "VALID_MILESTONES",
    "MILESTONE_STATUS_MAP",
    # Shipment Functions
    "create_shipment",
    "update_shipment_status",
    "record_shipment_milestone",
    "ingest_telemetry",
    "get_shipment_telemetry",
    "get_tracking_details",
    "upload_proof_of_delivery",
    "assign_driver",
    # Routing Exceptions
    "RoutingException",
    "NodeNotFoundException",
    "NoPathFoundException",
    "VehicleConstraintViolationException",
    "NetworkBottleneckException",
    # Routing Functions & Classes
    "NetworkGraphManager",
    "build_network_graph",
    "haversine_distance",
    "calculate_optimal_route",
    "get_k_alternative_routes",
    "detect_network_bottlenecks",
    "simulate_edge_outage_and_reroute",
    "get_network_reachability",
    "seeded_network_cte_routing",
]
