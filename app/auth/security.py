"""
Logistics Information Management System (Logistics IMS) - Security & Authentication Layer.

Provides:
- PBKDF2 HMAC-SHA256 salted password hashing (100,000+ iterations) with timing-attack resistant verification (hmac.compare_digest).
- Stateless HMAC-SHA256 JWT generation, decoding, and validation with configurable TTL (default 24h).
- User management and authentication database helpers integrated with the SQLite schema.
"""

import os
import json
import time
import uuid
import hmac
import hashlib
import sqlite3
import logging
from typing import Optional, Dict, Any, List, Union
from werkzeug.security import generate_password_hash, check_password_hash
import jwt

logger = logging.getLogger(__name__)

# Default PBKDF2 iterations for password hashing
DEFAULT_PBKDF2_ITERATIONS = 100000

# Default JWT configuration fallback
DEFAULT_JWT_SECRET = "logistics-ims-jwt-secret-key-change-in-prod-32b"
DEFAULT_JWT_ALGORITHM = "HS256"
DEFAULT_JWT_EXPIRATION_SECONDS = 86400  # 24 hours


# =============================================================================
# Custom Exception Hierarchy
# =============================================================================

class AuthenticationError(Exception):
    """Base exception for all authentication-related errors."""
    pass


class InvalidTokenError(AuthenticationError):
    """Raised when a JWT token is malformed, has an invalid signature, or is invalid."""
    pass


class TokenExpiredError(AuthenticationError):
    """Raised when a JWT token has expired (exp claim in the past)."""
    pass


class UserNotFoundError(AuthenticationError):
    """Raised when a user cannot be found in the database."""
    pass


class InvalidCredentialsError(AuthenticationError):
    """Raised when provided login credentials do not match."""
    pass


# =============================================================================
# Configuration Access Helpers
# =============================================================================

def get_jwt_secret() -> str:
    """
    Retrieves the JWT signing secret key from Flask current_app context,
    environment variables, or defaults.
    """
    try:
        from flask import current_app, has_app_context
        if has_app_context() and current_app and "JWT_SECRET" in current_app.config:
            return current_app.config["JWT_SECRET"]
    except Exception:
        pass
    return os.environ.get("JWT_SECRET", DEFAULT_JWT_SECRET)


def get_jwt_algorithm() -> str:
    """Retrieves the JWT signing algorithm (default HS256)."""
    try:
        from flask import current_app, has_app_context
        if has_app_context() and current_app and "JWT_ALGORITHM" in current_app.config:
            return current_app.config["JWT_ALGORITHM"]
    except Exception:
        pass
    return os.environ.get("JWT_ALGORITHM", DEFAULT_JWT_ALGORITHM)


def get_jwt_expiration() -> int:
    """Retrieves token expiration duration in seconds (default 86400 / 24 hours)."""
    try:
        from flask import current_app, has_app_context
        if has_app_context() and current_app and "JWT_EXPIRATION_SECONDS" in current_app.config:
            return int(current_app.config["JWT_EXPIRATION_SECONDS"])
    except Exception:
        pass
    return int(os.environ.get("JWT_EXPIRATION_SECONDS", DEFAULT_JWT_EXPIRATION_SECONDS))


# =============================================================================
# 1. Password Hashing & Verification
# =============================================================================

def hash_password(password: str, iterations: int = DEFAULT_PBKDF2_ITERATIONS) -> str:
    """
    Hashes a plaintext password using secure PBKDF2 HMAC-SHA256 with 100,000+ iterations
    and a cryptographically random salt.

    Returns:
        Formatted string: 'pbkdf2:sha256:<iterations>$<salt>$<hash>'
    """
    if not password:
        raise ValueError("Password cannot be empty.")
    if len(password) < 1:
        raise ValueError("Password cannot be empty.")
    
    # Use Werkzeug's secure implementation which generates standard PBKDF2 HMAC-SHA256
    return generate_password_hash(password, method=f"pbkdf2:sha256:{iterations}")


def verify_password(password: str, hashed: str) -> bool:
    """
    Verifies a plaintext password against a stored hash using timing-attack resistant
    constant-time comparison (hmac.compare_digest).

    Supports:
    - pbkdf2:sha256:<iterations>$<salt>$<hash>
    - scrypt:<params>$<salt>$<hash>
    - Werkzeug standard hash formats
    """
    if not password or not hashed:
        return False
    try:
        # check_password_hash internally executes hmac.compare_digest on the resulting hash
        return check_password_hash(hashed, password)
    except Exception as exc:
        logger.warning(f"Error during password hash verification: {exc}")
        return False


# =============================================================================
# 2. Stateless JWT Token Generation & Validation
# =============================================================================

def generate_token(
    user_payload: Dict[str, Any],
    secret: Optional[str] = None,
    expires_in: Optional[int] = None
) -> str:
    """
    Generates a stateless HMAC-SHA256 signed JWT token containing:
    - sub (and user_id): Unique user identifier
    - username: User login name
    - role: User system role (e.g. Admin, WarehouseManager, Dispatcher, Driver, Customer)
    - permissions: List of authorized permission strings
    - iat: Issued-at Unix timestamp
    - exp: Expiration Unix timestamp

    Args:
        user_payload: Dictionary containing user identity and attributes.
        secret: Optional secret key override (falls back to app config or env).
        expires_in: Optional lifetime in seconds (falls back to 24h default).

    Returns:
        Encoded JWT token string.
    """
    if not isinstance(user_payload, dict):
        raise ValueError("user_payload must be a dictionary.")

    now = int(time.time())
    ttl = expires_in if expires_in is not None else get_jwt_expiration()
    signing_secret = secret or get_jwt_secret()
    algorithm = get_jwt_algorithm()

    # Extract or normalize user identifier
    sub = user_payload.get("sub") or user_payload.get("user_id") or user_payload.get("id")
    if not sub:
        raise ValueError("user_payload must contain 'sub', 'user_id', or 'id'.")

    username = user_payload.get("username", str(sub))
    role = user_payload.get("role", "Customer")
    
    # Extract permissions list
    permissions = user_payload.get("permissions")
    if permissions is None:
        # Fallback to role permissions from RBAC matrix
        try:
            from app.auth.rbac import ROLE_PERMISSIONS_MATRIX
        except ImportError:
            try:
                from proposed_rbac import ROLE_PERMISSIONS_MATRIX
            except ImportError:
                ROLE_PERMISSIONS_MATRIX = {}
        perms_set = ROLE_PERMISSIONS_MATRIX.get(role, set())
        permissions = sorted(list(perms_set))
    elif not isinstance(permissions, list):
        permissions = list(permissions)

    payload = {
        "sub": str(sub),
        "user_id": str(sub),
        "username": str(username),
        "role": str(role),
        "permissions": permissions,
        "iat": user_payload.get("iat", now),
        "exp": user_payload.get("exp", now + ttl),
        "iss": "logistics-ims"
    }

    # Copy any additional custom claims from user_payload
    for key, val in user_payload.items():
        if key not in payload:
            payload[key] = val

    token = jwt.encode(payload, signing_secret, algorithm=algorithm)
    return token if isinstance(token, str) else token.decode("utf-8")


def decode_token(token: str, secret: Optional[str] = None) -> Dict[str, Any]:
    """
    Decodes and validates an HMAC-SHA256 JWT token.

    Args:
        token: Raw JWT string.
        secret: Optional secret key override.

    Returns:
        Decoded claims dictionary.

    Raises:
        TokenExpiredError: If the token has expired.
        InvalidTokenError: If the token signature is invalid or malformed.
    """
    if not token or not isinstance(token, str):
        raise InvalidTokenError("JWT token string is required.")

    signing_secret = secret or get_jwt_secret()
    algorithm = get_jwt_algorithm()

    try:
        decoded = jwt.decode(
            token,
            signing_secret,
            algorithms=[algorithm],
            options={"require": ["exp", "iat", "sub"]}
        )
        return decoded
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired.") from exc
    except jwt.InvalidSignatureError as exc:
        raise InvalidTokenError("Invalid token signature.") from exc
    except jwt.DecodeError as exc:
        raise InvalidTokenError("Malformed or undecodable token.") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError(f"Invalid token: {exc}") from exc
    except Exception as exc:
        raise InvalidTokenError(f"Token validation failed: {exc}") from exc


def verify_token(token: str, secret: Optional[str] = None) -> Dict[str, Any]:
    """
    Validates token integrity and schema conformance.

    Returns:
        Decoded payload dictionary with verified sub, username, and role.
    """
    payload = decode_token(token, secret=secret)
    if not payload.get("sub") and not payload.get("user_id"):
        raise InvalidTokenError("Token payload missing required 'sub' or 'user_id' claim.")
    if "role" not in payload:
        raise InvalidTokenError("Token payload missing required 'role' claim.")
    return payload


# =============================================================================
# 3. User Management & Database Integration Helpers
# =============================================================================

def get_user_by_id(user_id: str, db_conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """
    Fetches user profile and assigned role permissions from SQLite database.
    """
    from app.db.connection import get_db_connection
    conn = db_conn or get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT u.id, u.username, u.email, u.password_hash, u.role_id,
               u.full_name, u.is_active, u.created_at, u.updated_at,
               r.name AS role_name, r.permissions AS role_permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.id = ?
    """, (user_id,))
    row = cur.fetchone()
    if not row:
        return None

    try:
        permissions = json.loads(row["role_permissions"]) if row["role_permissions"] else []
    except (ValueError, TypeError):
        permissions = []

    return {
        "id": row["id"],
        "user_id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "role_id": row["role_id"],
        "role": row["role_name"],
        "full_name": row["full_name"],
        "is_active": bool(row["is_active"]),
        "permissions": permissions,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"]
    }


def get_user_by_username(username: str, db_conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """
    Fetches user record by username from SQLite database.
    """
    from app.db.connection import get_db_connection
    conn = db_conn or get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT u.id, u.username, u.email, u.password_hash, u.role_id,
               u.full_name, u.is_active, u.created_at, u.updated_at,
               r.name AS role_name, r.permissions AS role_permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.username = ?
    """, (username,))
    row = cur.fetchone()
    if not row:
        return None

    try:
        permissions = json.loads(row["role_permissions"]) if row["role_permissions"] else []
    except (ValueError, TypeError):
        permissions = []

    return {
        "id": row["id"],
        "user_id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "password_hash": row["password_hash"],
        "role_id": row["role_id"],
        "role": row["role_name"],
        "full_name": row["full_name"],
        "is_active": bool(row["is_active"]),
        "permissions": permissions,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"]
    }


def authenticate_user(
    username_or_email: str,
    password: str,
    db_conn: Optional[sqlite3.Connection] = None
) -> Optional[Dict[str, Any]]:
    """
    Validates user credentials against the SQLite database.

    Args:
        username_or_email: Username or email string.
        password: Plaintext password candidate.
        db_conn: Optional SQLite connection.

    Returns:
        User profile dictionary (with password_hash removed) on success, or None on failure.
    """
    from app.db.connection import get_db_connection
    conn = db_conn or get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT u.id, u.username, u.email, u.password_hash, u.role_id,
               u.full_name, u.is_active, u.created_at, u.updated_at,
               r.name AS role_name, r.permissions AS role_permissions
        FROM users u
        JOIN roles r ON u.role_id = r.id
        WHERE u.username = ? OR u.email = ?
    """, (username_or_email, username_or_email))
    row = cur.fetchone()
    if not row:
        return None

    # Check active status
    if row["is_active"] != 1:
        logger.warning(f"Authentication failed: User '{username_or_email}' is deactivated.")
        return None

    # Timing-attack resistant password verification
    if not verify_password(password, row["password_hash"]):
        return None

    try:
        permissions = json.loads(row["role_permissions"]) if row["role_permissions"] else []
    except (ValueError, TypeError):
        permissions = []

    return {
        "id": row["id"],
        "user_id": row["id"],
        "username": row["username"],
        "email": row["email"],
        "role_id": row["role_id"],
        "role": row["role_name"],
        "full_name": row["full_name"],
        "is_active": True,
        "permissions": permissions,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"]
    }


def register_user(
    username: str,
    email: str,
    password: str,
    role: str = "Customer",
    full_name: str = "",
    db_conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Registers a new user record in the SQLite database with securely hashed credentials.

    Args:
        username: Unique username.
        email: Unique email address.
        password: Plaintext password (min 6 characters).
        role: Role name (e.g. 'Customer', 'Driver', 'Dispatcher', 'WarehouseManager', 'Admin')
              or role ID ('role_customer', etc.).
        full_name: Full display name.
        db_conn: Optional SQLite connection.

    Returns:
        Newly created user profile dictionary (excluding password_hash).

    Raises:
        ValueError: On validation failure or duplicate record.
    """
    from app.db.connection import get_db_connection, transaction_scope
    conn = db_conn or get_db_connection()
    cur = conn.cursor()

    # Input validation
    if not username or len(username.strip()) < 3:
        raise ValueError("Username must be at least 3 characters long.")
    if not email or "@" not in email:
        raise ValueError("A valid email address is required.")
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters long.")

    username = username.strip()
    email = email.strip().lower()

    # Resolve role_id and permissions from roles table
    cur.execute("SELECT id, name, permissions FROM roles WHERE name = ? OR id = ?", (role, role))
    role_row = cur.fetchone()
    if not role_row:
        raise ValueError(f"Role '{role}' is not recognized in system roles.")

    role_id = role_row["id"]
    role_name = role_row["name"]
    try:
        permissions = json.loads(role_row["permissions"]) if role_row["permissions"] else []
    except (ValueError, TypeError):
        permissions = []

    # Check for existing username or email
    cur.execute("SELECT id FROM users WHERE username = ? OR email = ?", (username, email))
    if cur.fetchone():
        raise ValueError("A user with this username or email already exists.")

    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    pwd_hash = hash_password(password)
    display_name = full_name.strip() if full_name else username

    with transaction_scope(conn):
        conn.execute("""
            INSERT INTO users (id, username, email, password_hash, role_id, full_name, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
        """, (user_id, username, email, pwd_hash, role_id, display_name))

    logger.info(f"Successfully registered user '{username}' (ID: {user_id}, Role: {role_name}).")
    return {
        "id": user_id,
        "user_id": user_id,
        "username": username,
        "email": email,
        "role_id": role_id,
        "role": role_name,
        "full_name": display_name,
        "is_active": True,
        "permissions": permissions
    }


def get_user_profile(user_id: str, db_conn: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """
    Public profile lookup excluding password_hash.
    """
    user = get_user_by_id(user_id, db_conn=db_conn)
    if not user:
        return None
    user.pop("password_hash", None)
    return user
