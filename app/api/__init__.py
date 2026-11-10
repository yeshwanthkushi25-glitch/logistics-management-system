"""
Logistics Information Management System (Logistics IMS) - REST API Package.

Exports all REST blueprints and the centralized blueprint registration helper.
"""

from typing import List, Tuple, Any
from flask import Flask

from app.api.auth_routes import auth_bp, admin_bp
from app.api.inventory_routes import inventory_bp, warehouses_bp
from app.api.order_routes import order_bp
from app.api.shipment_routes import shipment_bp, telemetry_bp
from app.api.routing_routes import routing_bp, routes_alias_bp
from app.api.analytics_routes import analytics_bp, cache_bp


ALL_BLUEPRINTS: List[Tuple[Any, str]] = [
    # 1. Authentication & Users
    (auth_bp, "/api/v1/auth"),
    (admin_bp, "/api/v1/admin"),

    # 2. Inventory & Warehouses
    (inventory_bp, "/api/v1/inventory"),
    (warehouses_bp, "/api/v1/warehouses"),

    # 3. Orders & Fulfillment
    (order_bp, "/api/v1/orders"),

    # 4. Shipments & Telemetry
    (shipment_bp, "/api/v1/shipments"),
    (telemetry_bp, "/api/v1/telemetry"),

    # 5. Graph Routing & Network Topology
    (routing_bp, "/api/v1/network"),
    (routes_alias_bp, "/api/v1/routes"),

    # 6. Analytics, KPIs & Caching Metrics
    (analytics_bp, "/api/v1/analytics"),
    (cache_bp, "/api/v1/cache"),
]


def register_api_blueprints(app: Flask) -> None:
    """Central registration function for all REST blueprints on the application instance."""
    for bp, prefix in ALL_BLUEPRINTS:
        app.register_blueprint(bp, url_prefix=prefix)
