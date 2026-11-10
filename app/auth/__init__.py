"""
Logistics Information Management System (Logistics IMS) - Authentication & RBAC Module.

Exposes core security and authorization interfaces:
- Password hashing & verification
- Stateless JWT issuance & validation
- User registration, login authentication, and profile retrieval
- 5-role logistics RBAC matrix and wildcard matching
- Route decorators (@require_auth, @require_role, @require_permission)
- Request context utilities (get_current_user, get_current_role)
"""

try:
    from app.auth.security import (
        hash_password,
        verify_password,
        generate_token,
        decode_token,
        verify_token,
        authenticate_user,
        register_user,
        get_user_by_id,
        get_user_by_username,
        get_user_profile,
        AuthenticationError,
        InvalidTokenError,
        TokenExpiredError,
        UserNotFoundError,
        InvalidCredentialsError,
    )
except ImportError:
    from proposed_security import (
        hash_password,
        verify_password,
        generate_token,
        decode_token,
        verify_token,
        authenticate_user,
        register_user,
        get_user_by_id,
        get_user_by_username,
        get_user_profile,
        AuthenticationError,
        InvalidTokenError,
        TokenExpiredError,
        UserNotFoundError,
        InvalidCredentialsError,
    )

try:
    from app.auth.rbac import (
        ROLE_ADMIN,
        ROLE_WAREHOUSE_MANAGER,
        ROLE_DISPATCHER,
        ROLE_DRIVER,
        ROLE_CUSTOMER,
        ALL_ROLES,
        ROLE_PERMISSIONS_MATRIX,
        permission_matches,
        get_effective_permissions,
        check_user_has_permission,
        extract_bearer_token,
        authenticate_request,
        require_auth,
        require_role,
        require_permission,
        get_current_user,
        get_current_role,
        get_current_user_id,
        has_role,
        has_permission,
    )
except ImportError:
    from proposed_rbac import (
        ROLE_ADMIN,
        ROLE_WAREHOUSE_MANAGER,
        ROLE_DISPATCHER,
        ROLE_DRIVER,
        ROLE_CUSTOMER,
        ALL_ROLES,
        ROLE_PERMISSIONS_MATRIX,
        permission_matches,
        get_effective_permissions,
        check_user_has_permission,
        extract_bearer_token,
        authenticate_request,
        require_auth,
        require_role,
        require_permission,
        get_current_user,
        get_current_role,
        get_current_user_id,
        has_role,
        has_permission,
    )

__all__ = [
    # Security
    "hash_password",
    "verify_password",
    "generate_token",
    "decode_token",
    "verify_token",
    "authenticate_user",
    "register_user",
    "get_user_by_id",
    "get_user_by_username",
    "get_user_profile",
    "AuthenticationError",
    "InvalidTokenError",
    "TokenExpiredError",
    "UserNotFoundError",
    "InvalidCredentialsError",
    # RBAC
    "ROLE_ADMIN",
    "ROLE_WAREHOUSE_MANAGER",
    "ROLE_DISPATCHER",
    "ROLE_DRIVER",
    "ROLE_CUSTOMER",
    "ALL_ROLES",
    "ROLE_PERMISSIONS_MATRIX",
    "permission_matches",
    "get_effective_permissions",
    "check_user_has_permission",
    "extract_bearer_token",
    "authenticate_request",
    "require_auth",
    "require_role",
    "require_permission",
    "get_current_user",
    "get_current_role",
    "get_current_user_id",
    "has_role",
    "has_permission",
]
