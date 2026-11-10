"""
Logistics Information Management System (Logistics IMS) - Authentication & User API Blueprint.

Defines REST routes:
- POST /api/v1/auth/register: User account creation and role assignment
- POST /api/v1/auth/login: Credential verification and JWT access token issuance
- GET /api/v1/auth/me: Current authenticated identity introspection
- GET /api/v1/admin/users: Administrative user directory list (enforces Admin role)
"""

import uuid
import json
import logging
from typing import Dict, Any, Optional

from flask import Blueprint, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

from app.db.connection import get_db_connection, transaction_scope
from app.db.nosql import get_doc_store

from app.auth.security import generate_token, verify_password, hash_password
from app.auth.rbac import require_auth, require_role, ROLE_ADMIN

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth_routes", __name__)


# =============================================================================
# Helper: Resolve Role ID
# =============================================================================

def _resolve_role(conn, role_name_or_id: str) -> Optional[Dict[str, Any]]:
    """Resolves role record from database by name or id."""
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, permissions FROM roles WHERE id = ? OR LOWER(name) = LOWER(?) LIMIT 1",
        (role_name_or_id, role_name_or_id)
    )
    row = cur.fetchone()
    if row:
        perms = row["permissions"]
        if isinstance(perms, str):
            try:
                perms = json.loads(perms)
            except Exception:
                perms = [perms]
        return {"id": row["id"], "name": row["name"], "permissions": perms}
    return None


# =============================================================================
# POST /api/v1/auth/register
# =============================================================================

@auth_bp.route("/register", methods=["POST"])
def register():
    """
    Registers a new user in the system with an assigned role.

    Payload:
        username (str): Unique username (3-50 chars)
        email (str): Unique email address
        password (str): Plaintext password (min 6 chars)
        role (str, optional): Role name (defaults to 'Customer')
        full_name (str, optional): User's legal name
    """
    data = request.get_json(silent=True) or {}

    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""
    role_name = (data.get("role") or "Customer").strip()
    full_name = (data.get("full_name") or username).strip()

    if not username or not email or not password:
        return jsonify({
            "error": "Bad Request",
            "message": "Missing required fields: username, email, and password are required."
        }), 400

    if len(password) < 6:
        return jsonify({
            "error": "Bad Request",
            "message": "Password must be at least 6 characters in length."
        }), 400

    conn = get_db_connection()
    cur = conn.cursor()

    # Check for existing username or email
    cur.execute(
        "SELECT id, username, email FROM users WHERE username = ? OR email = ? LIMIT 1",
        (username, email)
    )
    existing = cur.fetchone()
    if existing:
        return jsonify({
            "error": "Conflict",
            "message": "A user with the specified username or email already exists."
        }), 409

    # Resolve role
    role_info = _resolve_role(conn, role_name)
    if not role_info:
        # Fallback to customer role
        role_info = _resolve_role(conn, "Customer")
        if not role_info:
            return jsonify({
                "error": "Bad Request",
                "message": f"Specified role '{role_name}' is not recognized."
            }), 400

    user_id = f"usr-{uuid.uuid4().hex[:12]}"
    pwd_hash = hash_password(password)

    with transaction_scope(isolation="IMMEDIATE") as tx_conn:
        tx_conn.execute("""
            INSERT INTO users (id, username, email, password_hash, role_id, full_name, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (user_id, username, email, pwd_hash, role_info["id"], full_name))

        # Log registration audit event in NoSQL document store
        doc_store = get_doc_store()
        doc_store.insert("audit_event_logs", {
            "event_id": f"EVT-REG-{uuid.uuid4().hex[:8].upper()}",
            "action": "USER_REGISTERED",
            "user_id": user_id,
            "username": username,
            "role": role_info["name"],
            "timestamp": tx_conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        })

    return jsonify({
        "status": "SUCCESS",
        "message": "User registered successfully.",
        "user_id": user_id,
        "username": username,
        "email": email,
        "role": role_info["name"],
        "full_name": full_name
    }), 201


# =============================================================================
# POST /api/v1/auth/login
# =============================================================================

@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Authenticates user credentials and returns a signed JWT access token.

    Payload:
        username (str): Username or email address
        password (str): Plaintext password
    """
    data = request.get_json(silent=True) or {}
    identifier = (data.get("username") or data.get("email") or "").strip()
    password = data.get("password") or ""

    if not identifier or not password:
        return jsonify({
            "error": "Bad Request",
            "message": "Missing username/email or password."
        }), 400

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT u.id, u.username, u.email, u.password_hash, u.is_active, u.full_name,
               r.name AS role_name, r.permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.username = ? OR u.email = ?
        LIMIT 1
    """, (identifier, identifier))
    user_row = cur.fetchone()

    if not user_row or not verify_password(password, user_row["password_hash"]):
        return jsonify({
            "error": "Unauthorized",
            "message": "Invalid username or password."
        }), 401

    if not user_row["is_active"]:
        return jsonify({
            "error": "Forbidden",
            "message": "Account has been deactivated. Please contact an administrator."
        }), 403

    raw_perms = user_row["permissions"]
    if isinstance(raw_perms, str):
        try:
            permissions = json.loads(raw_perms)
        except Exception:
            permissions = [raw_perms]
    else:
        permissions = raw_perms or []

    # Construct JWT token payload
    token_claims = {
        "sub": user_row["id"],
        "username": user_row["username"],
        "email": user_row["email"],
        "role": user_row["role_name"],
        "permissions": permissions,
        "full_name": user_row["full_name"]
    }
    access_token = generate_token(token_claims)

    return jsonify({
        "status": "SUCCESS",
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 86400,
        "user": {
            "id": user_row["id"],
            "username": user_row["username"],
            "email": user_row["email"],
            "role": user_row["role_name"],
            "full_name": user_row["full_name"],
            "permissions": permissions
        }
    }), 200


# =============================================================================
# GET /api/v1/auth/me
# =============================================================================

@auth_bp.route("/me", methods=["GET"])
@require_auth
def get_current_user_profile():
    """Returns the profile and effective permissions for the currently authenticated identity."""
    current_user = getattr(g, "current_user", None)
    if not current_user:
        return jsonify({"error": "Unauthorized", "message": "Not authenticated."}), 401

    user_id = current_user.get("sub") or current_user.get("id") or current_user.get("user_id")

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.username, u.email, u.full_name, u.is_active, u.created_at,
               r.name AS role_name, r.permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = ?
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()

    if row:
        raw_perms = row["permissions"]
        if isinstance(raw_perms, str):
            try:
                perms = json.loads(raw_perms)
            except Exception:
                perms = [raw_perms]
        else:
            perms = raw_perms or []

        return jsonify({
            "id": row["id"],
            "username": row["username"],
            "email": row["email"],
            "full_name": row["full_name"],
            "role": row["role_name"],
            "permissions": perms,
            "created_at": row["created_at"]
        }), 200

    # Fallback to token claims if DB record not directly fetched
    return jsonify({
        "id": user_id,
        "username": current_user.get("username"),
        "role": current_user.get("role"),
        "permissions": current_user.get("permissions", [])
    }), 200


# =============================================================================
# GET /api/v1/admin/users
# =============================================================================

@auth_bp.route("/admin/users", methods=["GET"])
@require_role(ROLE_ADMIN)
def list_admin_users():
    """
    Administrative directory listing of all system user accounts.
    Strictly restricted to Admin role (returns 403 Forbidden for others).
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT u.id, u.username, u.email, u.full_name, u.is_active, u.created_at,
               r.name AS role_name
        FROM users u
        JOIN roles r ON u.role_id = r.id
        ORDER BY u.created_at ASC
    """)
    rows = cur.fetchall()

    users_list = [
        {
            "id": r["id"],
            "username": r["username"],
            "email": r["email"],
            "full_name": r["full_name"],
            "role": r["role_name"],
            "is_active": bool(r["is_active"]),
            "created_at": r["created_at"]
        }
        for r in rows
    ]

    return jsonify({
        "users": users_list,
        "total": len(users_list)
    }), 200


admin_bp = Blueprint("admin_routes", __name__)

@admin_bp.route("/users", methods=["GET"])
@require_role(ROLE_ADMIN)
def admin_users_route():
    return list_admin_users()
