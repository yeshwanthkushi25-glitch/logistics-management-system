"""
Logistics Information Management System (Logistics IMS) - Role-Based Access Control (RBAC).

Defines:
- 5 distinct logistics system roles: Admin, WarehouseManager, Dispatcher, Driver, Customer.
- Comprehensive role-permission matrix with domain wildcard matching.
- Decorators for Flask endpoints: @require_auth, @require_role, @require_permission.
- Bearer token extraction and JSON 401 / 403 response handling.
- Request context management via flask.g (g.current_user, g.current_role).
"""

import re
import logging
from functools import wraps
from typing import List, Set, Dict, Any, Optional, Callable, Union

from flask import request, jsonify, g, has_request_context

try:
    from app.auth.security import (
        verify_token,
        decode_token,
        AuthenticationError,
        InvalidTokenError,
        TokenExpiredError,
    )
except ImportError:
    from proposed_security import (
        verify_token,
        decode_token,
        AuthenticationError,
        InvalidTokenError,
        TokenExpiredError,
    )

logger = logging.getLogger(__name__)


# =============================================================================
# 1. Five Logistics System Roles
# =============================================================================

ROLE_ADMIN = "Admin"
ROLE_WAREHOUSE_MANAGER = "WarehouseManager"
ROLE_DISPATCHER = "Dispatcher"
ROLE_DRIVER = "Driver"
ROLE_CUSTOMER = "Customer"

ALL_ROLES = [
    ROLE_ADMIN,
    ROLE_WAREHOUSE_MANAGER,
    ROLE_DISPATCHER,
    ROLE_DRIVER,
    ROLE_CUSTOMER,
]


# =============================================================================
# 2. Fine-Grained Permissions & Role-Permission Matrix
# =============================================================================

ROLE_PERMISSIONS_MATRIX: Dict[str, Set[str]] = {
    # 1. Admin: Unrestricted access across all operational, financial, and auditing domains
    ROLE_ADMIN: {
        "*",
        "admin:access",
        "inventory:*",
        "warehouses:*",
        "orders:*",
        "shipments:*",
        "network:*",
        "telemetry:*",
        "pod:*",
        "users:*",
        "analytics:*",
        "audit:*",
        "cache:*",
    },

    # 2. WarehouseManager: Manage warehouse stock, allocate, restock, view inventory and orders
    ROLE_WAREHOUSE_MANAGER: {
        "warehousemanager:access",
        "inventory:*",
        "inventory:read",
        "inventory:allocate",
        "inventory:release",
        "inventory:restock",
        "inventory:adjust",
        "warehouses:*",
        "warehouses:read",
        "warehouses:manage",
        "shipments:read",
        "orders:read",
    },

    # 3. Dispatcher: Create and manage shipments, assign drivers, compute routes, transport corridors
    ROLE_DISPATCHER: {
        "dispatcher:access",
        "shipments:*",
        "shipments:create",
        "shipments:read",
        "shipments:update_status",
        "shipments:assign_driver",
        "shipments:track",
        "network:*",
        "network:read",
        "network:route",
        "network:bottlenecks",
        "network:reroute",
        "network:manage",
        "orders:read",
    },

    # 4. Driver: Update assigned shipment status, ingest telemetry, upload proof-of-delivery
    ROLE_DRIVER: {
        "driver:access",
        "shipments:read_assigned",
        "shipments:track",
        "shipments:update_status",
        "shipments:update_milestone",
        "pod:upload",
        "pod:read",
        "telemetry:write",
        "telemetry:read",
    },

    # 5. Customer: Place orders, track personal shipments, view order history
    ROLE_CUSTOMER: {
        "customer:access",
        "orders:create",
        "orders:read_own",
        "orders:read",
        "shipments:track",
    },
}


# =============================================================================
# 3. Wildcard Permission Matching Logic
# =============================================================================

def permission_matches(user_perm: str, required_perm: str) -> bool:
    """
    Evaluates whether a granted user permission satisfies a required permission.

    Supports:
    - Exact match: 'orders:create' == 'orders:create'
    - Global superuser wildcard: '*' matches any required permission
    - Namespace prefix wildcard: 'inventory:*' matches 'inventory:restock', 'inventory:adjust', etc.
    """
    if user_perm == "*" or required_perm == "*":
        return True
    if user_perm == required_perm:
        return True
    if user_perm.endswith(":*"):
        prefix = user_perm[:-2]  # strip ':*'
        if required_perm.startswith(prefix + ":") or required_perm == prefix:
            return True
    return False


def get_effective_permissions(role: str, user_permissions: Optional[List[str]] = None) -> Set[str]:
    """
    Computes the full set of effective permissions combining the system role matrix
    with any token-specific granted permissions.
    """
    role_perms = set(ROLE_PERMISSIONS_MATRIX.get(role, set()))
    if user_permissions:
        role_perms.update(user_permissions)
    return role_perms


def check_user_has_permission(
    effective_permissions: Set[str],
    required_perm: str
) -> bool:
    """Checks whether any effective permission matches the required permission."""
    for p in effective_permissions:
        if permission_matches(p, required_perm):
            return True
    return False


# =============================================================================
# 4. Bearer Token Extraction & Validation
# =============================================================================

def extract_bearer_token(auth_header: Optional[str]) -> str:
    """
    Extracts the JWT token from the HTTP Authorization header.
    Expects format: 'Bearer <token>' (case-insensitive prefix).

    Raises:
        InvalidTokenError: If missing, malformed, or blank token.
    """
    if not auth_header:
        raise InvalidTokenError("Missing Authorization header.")

    parts = auth_header.strip().split()
    if len(parts) == 0:
        raise InvalidTokenError("Missing Authorization header.")
    if parts[0].lower() != "bearer":
        raise InvalidTokenError("Invalid token header format. Expected 'Bearer <token>'.")
    if len(parts) < 2 or not parts[1].strip():
        raise InvalidTokenError("Bearer token is empty.")

    return parts[1].strip()


def authenticate_request() -> Dict[str, Any]:
    """
    Authenticates the current Flask request by inspecting headers, decoding the JWT,
    and binding identity claims to flask.g.

    Returns:
        Decoded payload dictionary.

    Raises:
        InvalidTokenError: If token header is missing, malformed, or invalid.
        TokenExpiredError: If token signature has expired.
    """
    if not has_request_context():
        raise AuthenticationError("No active Flask request context.")

    # Check if already authenticated in this request lifecycle
    if hasattr(g, "current_user") and g.current_user:
        return g.current_user

    auth_header = request.headers.get("Authorization")
    token = extract_bearer_token(auth_header)
    payload = verify_token(token)

    # Bind to request context
    g.current_user = payload
    g.current_role = payload.get("role")
    g.user_id = payload.get("sub") or payload.get("user_id")

    return payload


# =============================================================================
# 5. Route Decorators
# =============================================================================

def require_auth(fn: Callable) -> Callable:
    """
    Flask route decorator requiring valid authentication.
    Returns HTTP 401 Unauthorized JSON response on missing/invalid/expired token.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            authenticate_request()
        except TokenExpiredError as exc:
            logger.warning(f"Authentication failed: {exc}")
            return jsonify({
                "error": "Unauthorized",
                "message": "Token has expired.",
                "details": str(exc)
            }), 401
        except (InvalidTokenError, AuthenticationError) as exc:
            logger.warning(f"Authentication failed: {exc}")
            return jsonify({
                "error": "Unauthorized",
                "message": str(exc)
            }), 401
        except Exception as exc:
            logger.error(f"Unexpected authentication failure: {exc}")
            return jsonify({
                "error": "Unauthorized",
                "message": "Authentication failed."
            }), 401

        return fn(*args, **kwargs)

    return wrapper


def require_role(*allowed_roles: str) -> Callable:
    """
    Flask route decorator enforcing role-based access control.

    Args:
        *allowed_roles: One or more role names permitted to access the route.
                        (e.g. 'WarehouseManager', 'Admin').

    Rules:
    - Unauthenticated requests return HTTP 401 Unauthorized.
    - Admin role ALWAYS has unrestricted system access.
    - Other roles must be present in *allowed_roles, else returns HTTP 403 Forbidden.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                payload = authenticate_request()
            except TokenExpiredError as exc:
                return jsonify({
                    "error": "Unauthorized",
                    "message": "Token has expired.",
                    "details": str(exc)
                }), 401
            except (InvalidTokenError, AuthenticationError) as exc:
                return jsonify({
                    "error": "Unauthorized",
                    "message": str(exc)
                }), 401
            except Exception as exc:
                return jsonify({
                    "error": "Unauthorized",
                    "message": "Authentication failed."
                }), 401

            user_role = payload.get("role")

            # Admin has unrestricted system access
            if user_role == ROLE_ADMIN:
                return fn(*args, **kwargs)

            # Check if user role is in the allowed list
            if user_role in allowed_roles:
                return fn(*args, **kwargs)

            logger.warning(
                f"RBAC Access Denied: User '{payload.get('username')}' with role '{user_role}' "
                f"attempted to access endpoint requiring {allowed_roles}."
            )
            return jsonify({
                "error": "Forbidden",
                "message": f"Role '{user_role}' is not authorized to access this resource.",
                "required_roles": list(allowed_roles)
            }), 403

        return wrapper
    return decorator


def require_permission(*required_permissions: str, require_all: bool = False) -> Callable:
    """
    Flask route decorator enforcing fine-grained permission authorization.

    Args:
        *required_permissions: One or more permission strings required.
        require_all: If True, all specified permissions must be satisfied.
                     If False (default), at least one satisfied permission grants access.

    Rules:
    - Unauthenticated requests return HTTP 401 Unauthorized.
    - Admin role ALWAYS has unrestricted system access.
    - Token-level or role-level permissions are evaluated using wildcard semantics.
    - Denied requests return HTTP 403 Forbidden.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                payload = authenticate_request()
            except TokenExpiredError as exc:
                return jsonify({
                    "error": "Unauthorized",
                    "message": "Token has expired.",
                    "details": str(exc)
                }), 401
            except (InvalidTokenError, AuthenticationError) as exc:
                return jsonify({
                    "error": "Unauthorized",
                    "message": str(exc)
                }), 401
            except Exception as exc:
                return jsonify({
                    "error": "Unauthorized",
                    "message": "Authentication failed."
                }), 401

            user_role = payload.get("role")

            # Admin has unrestricted system access
            if user_role == ROLE_ADMIN:
                return fn(*args, **kwargs)

            effective_perms = get_effective_permissions(
                user_role,
                payload.get("permissions")
            )

            # Wildcard superuser check
            if "*" in effective_perms:
                return fn(*args, **kwargs)

            if require_all:
                satisfied = all(check_user_has_permission(effective_perms, rp) for rp in required_permissions)
            else:
                satisfied = any(check_user_has_permission(effective_perms, rp) for rp in required_permissions)

            if satisfied:
                return fn(*args, **kwargs)

            logger.warning(
                f"RBAC Permission Denied: User '{payload.get('username')}' (Role: '{user_role}') "
                f"lacks required permissions {required_permissions}."
            )
            return jsonify({
                "error": "Forbidden",
                "message": "Insufficient permissions to perform this operation.",
                "required_permissions": list(required_permissions)
            }), 403

        return wrapper
    return decorator


# =============================================================================
# 6. Request Context Helper Utilities
# =============================================================================

def get_current_user() -> Optional[Dict[str, Any]]:
    """Returns the authenticated user payload dict bound to flask.g, or None."""
    if has_request_context():
        return getattr(g, "current_user", None)
    return None


def get_current_role() -> Optional[str]:
    """Returns the authenticated role string bound to flask.g, or None."""
    if has_request_context():
        return getattr(g, "current_role", None)
    return None


def get_current_user_id() -> Optional[str]:
    """Returns the authenticated user ID string bound to flask.g, or None."""
    if has_request_context():
        return getattr(g, "user_id", None)
    return None


def has_role(role_name: str, user_payload: Optional[Dict[str, Any]] = None) -> bool:
    """Checks whether the current request user or supplied payload matches a role."""
    payload = user_payload or get_current_user()
    if not payload:
        return False
    user_role = payload.get("role")
    if user_role == ROLE_ADMIN:
        return True
    return user_role == role_name


def has_permission(permission_name: str, user_payload: Optional[Dict[str, Any]] = None) -> bool:
    """Checks whether the current request user or supplied payload possesses a permission."""
    payload = user_payload or get_current_user()
    if not payload:
        return False
    user_role = payload.get("role", "Customer")
    if user_role == ROLE_ADMIN:
        return True
    effective = get_effective_permissions(user_role, payload.get("permissions"))
    return check_user_has_permission(effective, permission_name)
