"""
Logistics Information Management System (Logistics IMS) - Application Factory.

Production-grade Flask Application Factory for Milestone 4:
1. create_app(config=None, test_config=None) -> Flask:
   - Configures JSON encoding (ISO-8601 datetimes, UUIDs, Decimals, Sets).
   - Configures permissive CORS headers for local/cross-origin frontend evaluation.
   - Registers all REST API Blueprints under /api/v1:
     * auth_bp: /api/v1/auth (and /api/v1/admin)
     * inventory_bp: /api/v1/inventory (and /api/v1/warehouses)
     * order_bp: /api/v1/orders
     * shipment_bp: /api/v1/shipments (and /api/v1/telemetry)
     * routing_bp: /api/v1/network (and /api/v1/routes)
     * analytics_bp: /api/v1/analytics (and /api/v1/cache)
   - Registers Web Blueprint:
     * web_bp: /, /tracking, /inventory, /network
   - Binds global JSON error handlers for HTTP 400, 401, 403, 404, 405, 422, 500
     and maps domain exceptions cleanly to semantic HTTP status codes.
   - Registers teardown hooks for database connection management.
   - Exposes /health health-check endpoint.
"""

import os
import json
import uuid
import logging
from datetime import datetime, date, timezone
from decimal import Decimal
from typing import Optional, Dict, Any, Union

from flask import Flask, jsonify, request, Response
from werkzeug.exceptions import HTTPException

from app.config import config_by_name, Config

logger = logging.getLogger(__name__)


# =============================================================================
# Custom JSON Provider / Serializer
# =============================================================================

class LogisticsJSONProvider:
    """JSON Serialization Provider supporting domain objects."""
    @staticmethod
    def default(obj: Any) -> Any:
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, set):
            return list(obj)
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# =============================================================================
# Application Factory
# =============================================================================

def create_app(
    config: Optional[Union[str, Config, Dict[str, Any]]] = None,
    test_config: Optional[Dict[str, Any]] = None,
    **kwargs: Any
) -> Flask:
    """
    Constructs, configures, and returns a fully initialized Flask application instance.

    Args:
        config: Environment profile name (e.g. 'development', 'testing', 'production'),
                a Config class instance, or a dictionary of configuration overrides.
        test_config: Optional dictionary containing configuration overrides for testing.

    Returns:
        Configured Flask application instance with registered blueprints and error handlers.
    """
    # Support calling create_app(test_config_dict) directly as first argument
    if isinstance(config, dict) and test_config is None:
        test_config = config
        config = None

    base_dir = os.path.dirname(os.path.abspath(__file__))
    app = Flask(
        "logistics_ims",
        static_folder=os.path.join(base_dir, "static"),
        template_folder=os.path.join(base_dir, "templates")
    )

    # -------------------------------------------------------------------------
    # 1. Configuration Loading
    # -------------------------------------------------------------------------
    if isinstance(config, str):
        config_obj = config_by_name.get(config, Config)
        app.config.from_object(config_obj)
    elif config is not None:
        app.config.from_object(config)
    else:
        env_name = os.environ.get("FLASK_ENV", "default")
        app.config.from_object(config_by_name.get(env_name, Config))

    # Apply test config overrides if provided
    if test_config:
        app.config.update(test_config)

    # Configure custom JSON default encoder
    app.json.default = LogisticsJSONProvider.default

    # -------------------------------------------------------------------------
    # 2. Database Connection Synchronization & Teardown
    # -------------------------------------------------------------------------
    db_path = app.config.get("DATABASE_PATH")
    if db_path:
        from app.db.connection import set_default_db_path
        set_default_db_path(db_path)

    from app.db.connection import close_db_connection

    @app.teardown_appcontext
    def teardown_db(exception=None):
        close_db_connection()

    # -------------------------------------------------------------------------
    # 3. Request Context Authentication Reset & CORS Interceptor
    # -------------------------------------------------------------------------
    @app.before_request
    def handle_request_setup():
        """Resets per-request identity state on g and handles preflight OPTIONS."""
        from flask import g
        g.current_user = None
        g.current_role = None
        g.user_id = None

        if request.method == "OPTIONS":
            resp = Response("", status=204)
            return apply_cors_headers(resp)

    @app.after_request
    def apply_cors_headers(response: Response) -> Response:
        """Applies CORS and security headers to all outgoing responses."""
        response.headers.setdefault("Access-Control-Allow-Origin", "*")
        response.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, Idempotency-Key, X-Requested-With, Cache-Control"
        )
        response.headers.setdefault(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, DELETE, PATCH, OPTIONS"
        )
        response.headers.setdefault("Access-Control-Max-Age", "86400")
        return response

    # -------------------------------------------------------------------------
    # 4. Blueprint Registration
    # -------------------------------------------------------------------------
    register_blueprints(app)

    # -------------------------------------------------------------------------
    # 5. Global JSON Error Handlers
    # -------------------------------------------------------------------------
    register_error_handlers(app)

    # -------------------------------------------------------------------------
    # 6. Core Diagnostic Endpoints
    # -------------------------------------------------------------------------
    @app.route("/health")
    def health():
        """Basic service liveness probe."""
        return jsonify({
            "status": "healthy",
            "service": "Logistics IMS",
            "version": "1.0.0",
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    @app.route("/api/v1/ping")
    def ping():
        """API latency ping."""
        return jsonify({"ping": "pong"})

    return app


# =============================================================================
# Blueprint Registration Helper
# =============================================================================

def register_blueprints(app: Flask) -> None:
    """Imports and registers all modular API and Web Blueprints onto the Flask app."""
    # REST API Blueprints
    from app.api import register_api_blueprints
    register_api_blueprints(app)

    # Web Dashboard Blueprint
    try:
        from app.web_routes import web_bp
        app.register_blueprint(web_bp)
    except Exception as exc:
        logger.warning(f"Could not register web_bp: {exc}")


# =============================================================================
# Global Error Handling
# =============================================================================

def register_error_handlers(app: Flask) -> None:
    """Registers standard HTTP and domain-specific JSON error handlers."""

    @app.errorhandler(400)
    def handle_bad_request(err):
        msg = err.description if isinstance(err, HTTPException) else str(err)
        return jsonify({"error": "Bad Request", "message": msg, "status_code": 400}), 400

    @app.errorhandler(401)
    def handle_unauthorized(err):
        msg = err.description if isinstance(err, HTTPException) else str(err)
        return jsonify({"error": "Unauthorized", "message": msg, "status_code": 401}), 401

    @app.errorhandler(403)
    def handle_forbidden(err):
        msg = err.description if isinstance(err, HTTPException) else str(err)
        return jsonify({"error": "Forbidden", "message": msg, "status_code": 403}), 403

    @app.errorhandler(404)
    def handle_not_found(err):
        msg = err.description if isinstance(err, HTTPException) else str(err)
        return jsonify({"error": "Not Found", "message": msg, "status_code": 404}), 404

    @app.errorhandler(405)
    def handle_method_not_allowed(err):
        msg = err.description if isinstance(err, HTTPException) else str(err)
        return jsonify({"error": "Method Not Allowed", "message": msg, "status_code": 405}), 405

    @app.errorhandler(422)
    def handle_unprocessable_entity(err):
        msg = err.description if isinstance(err, HTTPException) else str(err)
        return jsonify({"error": "Unprocessable Entity", "message": msg, "status_code": 422}), 422

    @app.errorhandler(500)
    def handle_internal_server_error(err):
        logger.exception("Internal Server Error caught in global handler:")
        return jsonify({
            "error": "Internal Server Error",
            "message": "An unexpected error occurred while processing the request.",
            "status_code": 500
        }), 500

    # -------------------------------------------------------------------------
    # Domain Exceptions Mapping
    # -------------------------------------------------------------------------
    try:
        from app.services.inventory_service import (
            InsufficientStockException,
            ProductNotFoundException,
            WarehouseNotFoundException,
            InvalidInventoryOperationException
        )
        from app.services.fulfillment_service import (
            PaymentFailedException,
            OrderNotFoundException,
            OrderStateTransitionException
        )
        from app.services.shipment_service import (
            ShipmentNotFoundException,
            ShipmentStateTransitionException,
            InvalidShipmentStatusException,
            InvalidMilestoneException,
            InvalidTelemetryException,
            ProofOfDeliveryException
        )
        from app.services.routing_service import (
            NoPathFoundException,
            RoutingException,
            NodeNotFoundException,
            VehicleConstraintViolationException
        )

        @app.errorhandler(InsufficientStockException)
        def handle_insufficient_stock(err):
            return jsonify({
                "error": "Insufficient Stock",
                "message": str(err),
                "status_code": 409
            }), 409

        @app.errorhandler(PaymentFailedException)
        def handle_payment_failed(err):
            return jsonify({
                "error": "Payment Failed",
                "message": str(err),
                "status_code": 422
            }), 422

        @app.errorhandler(OrderNotFoundException)
        def handle_order_not_found(err):
            return jsonify({
                "error": "Order Not Found",
                "message": str(err),
                "status_code": 404
            }), 404

        @app.errorhandler(OrderStateTransitionException)
        def handle_order_state_transition(err):
            return jsonify({
                "error": "Invalid Order Transition",
                "message": str(err),
                "status_code": 422
            }), 422

        @app.errorhandler(ShipmentNotFoundException)
        def handle_shipment_not_found(err):
            return jsonify({
                "error": "Shipment Not Found",
                "message": str(err),
                "status_code": 404
            }), 404

        @app.errorhandler(ShipmentStateTransitionException)
        @app.errorhandler(InvalidShipmentStatusException)
        def handle_shipment_transition(err):
            return jsonify({
                "error": "Invalid Shipment Status Transition",
                "message": str(err),
                "status_code": 422
            }), 422

        @app.errorhandler(InvalidMilestoneException)
        def handle_invalid_milestone(err):
            return jsonify({
                "error": "Invalid Milestone",
                "message": str(err),
                "status_code": 422
            }), 422

        @app.errorhandler(InvalidTelemetryException)
        def handle_invalid_telemetry(err):
            return jsonify({
                "error": "Invalid Telemetry Data",
                "message": str(err),
                "status_code": 422
            }), 422

        @app.errorhandler(ProofOfDeliveryException)
        def handle_pod_exception(err):
            return jsonify({
                "error": "Proof of Delivery Failure",
                "message": str(err),
                "status_code": 422
            }), 422

        @app.errorhandler(ProductNotFoundException)
        @app.errorhandler(WarehouseNotFoundException)
        @app.errorhandler(NodeNotFoundException)
        def handle_entity_not_found(err):
            return jsonify({
                "error": "Entity Not Found",
                "message": str(err),
                "status_code": 404
            }), 404

        @app.errorhandler(InvalidInventoryOperationException)
        @app.errorhandler(RoutingException)
        @app.errorhandler(VehicleConstraintViolationException)
        def handle_invalid_operation(err):
            return jsonify({
                "error": "Invalid Operation",
                "message": str(err),
                "status_code": 400
            }), 400

        @app.errorhandler(NoPathFoundException)
        def handle_no_path(err):
            return jsonify({
                "error": "No Path Found",
                "message": str(err),
                "status_code": 422
            }), 422

    except ImportError as e:
        logger.warning(f"Could not bind all domain error handlers: {e}")
