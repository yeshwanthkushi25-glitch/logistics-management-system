"""
Logistics IMS - Shipment Tracking & 7-State FSM Lifecycle Service.

Production Architecture Blueprint for Milestone 3 (Remediated).

Features:
1. Strict 7-state Finite State Machine (FSM):
   CREATED -> PICKED_UP -> IN_TRANSIT -> OUT_FOR_DELIVERY -> DELIVERED / EXCEPTION / RETURNED.
   Rejects invalid/illegal status leaps with ShipmentStateTransitionException.
2. 10 Milestone Event Types persisted to 'shipment_milestones' relational table.
   Unconditional terminal state guard blocks milestone insertion on DELIVERED/RETURNED shipments.
3. Real-time IoT GPS and environmental telemetry ingestion & querying via SQLite JSON1 NoSQL DocumentStore.
   Safely guards non-dict 'geo' objects, enforces shipment existence, and protects terminal coordinates.
4. Cache-Aside pattern query acceleration with 'shipment:track:{tracking_number}' (TTL 60s),
   cache status tagging (HIT / MISS), and pattern-based mutation invalidation ('shipment:*').
5. Proof-of-Delivery (POD) document/signature upload via Cloud S3 / LocalStorageClient with pre-validation.
"""

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from app.db.connection import get_db_connection, transaction_scope
from app.db.cache import get_cache_manager
from app.db.nosql import DocumentStore, get_doc_store
from app.db.storage import get_storage_client

logger = logging.getLogger(__name__)


# =============================================================================
# Domain Exceptions
# =============================================================================

class ShipmentException(Exception):
    """Base domain exception for shipment operations."""
    pass


class ShipmentNotFoundException(ShipmentException):
    """Raised when a shipment cannot be found by ID or tracking number."""
    pass


class InvalidShipmentStatusException(ShipmentException):
    """Raised when an invalid status string or status transition is attempted."""
    pass


class ShipmentStateTransitionException(InvalidShipmentStatusException):
    """Raised specifically when an illegal state transition in the 7-state FSM is attempted."""
    pass


class InvalidMilestoneException(ShipmentException):
    """Raised when an invalid milestone type or payload is submitted."""
    pass


class InvalidTelemetryException(ShipmentException, ValueError):
    """Raised when telemetry coordinates or readings fail boundary validation."""
    pass


class ProofOfDeliveryException(ShipmentException):
    """Raised when proof-of-delivery upload or verification fails."""
    pass


# =============================================================================
# 7-State FSM Specification & Transition Matrix
# =============================================================================

SHIPMENT_STATES: Set[str] = {
    "CREATED",
    "PICKED_UP",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "DELIVERED",
    "EXCEPTION",
    "RETURNED",
}

# Strict valid forward/recovery transitions for the 7-state FSM
VALID_TRANSITIONS: Dict[str, Set[str]] = {
    "CREATED": {"PICKED_UP", "IN_TRANSIT", "EXCEPTION"},
    "PICKED_UP": {"IN_TRANSIT", "EXCEPTION"},
    "IN_TRANSIT": {"OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION", "IN_TRANSIT"},  # Intermediate hub scans keep IN_TRANSIT, or direct delivery
    "OUT_FOR_DELIVERY": {"DELIVERED", "EXCEPTION", "RETURNED", "OUT_FOR_DELIVERY"},
    "EXCEPTION": {"IN_TRANSIT", "OUT_FOR_DELIVERY", "RETURNED", "DELIVERED", "EXCEPTION"},
    "RETURNED": set(),   # Terminal state: cannot transition further
    "DELIVERED": set(),  # Terminal state: immutable delivery proof
}

VALID_MILESTONES: Set[str] = {
    "MANIFEST_CREATED",
    "PICKUP_COMPLETED",
    "DEPARTURE_SCAN",
    "HUB_ARRIVAL",
    "CUSTOMS_CLEARED",
    "SORTATION_COMPLETED",
    "OUT_FOR_DELIVERY_SCAN",
    "DELIVERY_ATTEMPTED",
    "DELIVERY_COMPLETED",
    "EXCEPTION",
    "CREATED",
    "PICKED_UP",
    "IN_TRANSIT",
    "OUT_FOR_DELIVERY",
    "DELIVERED",
    "RETURNED",
}

# Automatic status progression triggered by standard milestone scans
MILESTONE_STATUS_MAP: Dict[str, str] = {
    "MANIFEST_CREATED": "CREATED",
    "PICKUP_COMPLETED": "PICKED_UP",
    "DEPARTURE_SCAN": "IN_TRANSIT",
    "HUB_ARRIVAL": "IN_TRANSIT",
    "CUSTOMS_CLEARED": "IN_TRANSIT",
    "SORTATION_COMPLETED": "IN_TRANSIT",
    "OUT_FOR_DELIVERY_SCAN": "OUT_FOR_DELIVERY",
    "DELIVERY_ATTEMPTED": "EXCEPTION",
    "DELIVERY_COMPLETED": "DELIVERED",
    "EXCEPTION": "EXCEPTION",
    "CREATED": "CREATED",
    "PICKED_UP": "PICKED_UP",
    "IN_TRANSIT": "IN_TRANSIT",
    "OUT_FOR_DELIVERY": "OUT_FOR_DELIVERY",
    "DELIVERED": "DELIVERED",
    "RETURNED": "RETURNED",
}


# =============================================================================
# Helper Utilities
# =============================================================================

def _utc_now_iso() -> str:
    """Returns current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _generate_tracking_number() -> str:
    """Generates unique tracking number adhering to TRK-YYYYMMDD-XXXXXX format."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:6].upper()
    return f"TRK-{date_str}-{suffix}"


def _invalidate_shipment_caches(tracking_number: str) -> None:
    """Purges all cache keys associated with a shipment tracking number."""
    cache = get_cache_manager()
    cache.delete(f"shipment:track:{tracking_number}")
    cache.delete(f"shipment:tracking:{tracking_number}")
    cache.invalidate_pattern(f"shipment:*{tracking_number}*")
    cache.invalidate_pattern("shipment:*")


def _find_shipment_row(
    identifier: str,
    conn: sqlite3.Connection
) -> sqlite3.Row:
    """
    Finds a shipment row by tracking_number or internal id.
    Raises ShipmentNotFoundException if not found.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT s.*, 
               w.name as origin_warehouse_name, w.code as origin_warehouse_code,
               o.order_number, o.destination_city, o.destination_lat, o.destination_lon, o.shipping_address
        FROM shipments s
        JOIN warehouses w ON s.origin_warehouse_id = w.id
        JOIN orders o ON s.order_id = o.id
        WHERE s.tracking_number = ? OR s.id = ?
        LIMIT 1;
    """, (identifier, identifier))
    row = cur.fetchone()
    if not row:
        raise ShipmentNotFoundException(f"Shipment with identifier '{identifier}' not found.")
    return row


# =============================================================================
# Core Shipment Operations
# =============================================================================

def create_shipment(
    order_id: str,
    origin_warehouse_id: str,
    carrier: str = "Apex Freight Express",
    service_level: str = "STANDARD",
    tracking_number: Optional[str] = None,
    assigned_driver_id: Optional[str] = None,
    estimated_delivery: Optional[str] = None,
    initial_notes: Optional[str] = "Shipment manifest generated",
    actor_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Creates a new shipment record in 'CREATED' status and logs initial MANIFEST_CREATED milestone.
    """
    trk_num = tracking_number or _generate_tracking_number()
    shipment_id = f"shp_{uuid.uuid4().hex[:12]}"

    if service_level not in ("STANDARD", "EXPRESS", "PRIORITY", "SAME_DAY"):
        raise ValueError(f"Invalid service level: {service_level}")

    with transaction_scope(conn) as active_conn:
        cur = active_conn.cursor()

        # Validate order and warehouse exist
        cur.execute("SELECT id FROM orders WHERE id = ?", (order_id,))
        if not cur.fetchone():
            raise ShipmentException(f"Order '{order_id}' does not exist.")

        cur.execute("SELECT id, latitude, longitude, name FROM warehouses WHERE id = ?", (origin_warehouse_id,))
        wh_row = cur.fetchone()
        if not wh_row:
            raise ShipmentException(f"Warehouse '{origin_warehouse_id}' does not exist.")

        cur.execute("""
            INSERT INTO shipments (
                id, tracking_number, order_id, origin_warehouse_id, carrier,
                service_level, status, estimated_delivery, assigned_driver_id,
                current_lat, current_lon, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'CREATED', ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (
            shipment_id, trk_num, order_id, origin_warehouse_id, carrier,
            service_level, estimated_delivery, assigned_driver_id,
            wh_row["latitude"], wh_row["longitude"]
        ))

        # Record initial MANIFEST_CREATED milestone
        milestone_id = f"ms_{uuid.uuid4().hex[:12]}"
        cur.execute("""
            INSERT INTO shipment_milestones (
                id, shipment_id, milestone_type, location_name, latitude, longitude, notes, recorded_by, timestamp
            ) VALUES (?, ?, 'MANIFEST_CREATED', ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            milestone_id, shipment_id, wh_row["name"],
            wh_row["latitude"], wh_row["longitude"],
            initial_notes, actor_id
        ))

    _invalidate_shipment_caches(trk_num)

    return {
        "id": shipment_id,
        "shipment_id": shipment_id,
        "tracking_number": trk_num,
        "tracking_num": trk_num,
        "order_id": order_id,
        "origin_warehouse_id": origin_warehouse_id,
        "carrier": carrier,
        "service_level": service_level,
        "status": "CREATED",
        "assigned_driver_id": assigned_driver_id,
        "created_at": _utc_now_iso(),
        "milestone_id": milestone_id
    }


def update_shipment_status(
    identifier: str,
    new_status: str,
    actor_id: Optional[str] = None,
    location: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Transitions shipment status adhering strictly to the 7-state FSM.

    Validates:
    - Current status != new_status (or idempotent no-op)
    - Transition is legally permitted in VALID_TRANSITIONS
    - DELIVERED and RETURNED are terminal (no subsequent transitions allowed)

    Raises:
        ShipmentNotFoundException: If shipment not found.
        ShipmentStateTransitionException: If transition violates FSM.
    """
    new_status = new_status.upper().strip()
    if new_status not in SHIPMENT_STATES:
        raise InvalidShipmentStatusException(f"Invalid shipment status: '{new_status}'. Allowed: {sorted(SHIPMENT_STATES)}")

    with transaction_scope(conn) as active_conn:
        row = _find_shipment_row(identifier, active_conn)
        current_status = row["status"]
        shipment_id = row["id"]
        tracking_number = row["tracking_number"]

        # Terminal state check
        if current_status == "DELIVERED":
            raise ShipmentStateTransitionException(
                f"Cannot transition delivered shipment '{tracking_number}' from terminal state 'DELIVERED' to '{new_status}'."
            )
        if current_status == "RETURNED" and new_status != "RETURNED":
            raise ShipmentStateTransitionException(
                f"Cannot transition returned shipment '{tracking_number}' from terminal state 'RETURNED' to '{new_status}'."
            )

        # Idempotent re-affirmation
        if current_status == new_status:
            return {
                "id": shipment_id,
                "tracking_number": tracking_number,
                "status": current_status,
                "previous_status": current_status,
                "transition_status": "IDEMPOTENT_NOOP"
            }

        # Validate against FSM transition rules
        allowed_targets = VALID_TRANSITIONS.get(current_status, set())
        if new_status not in allowed_targets:
            raise ShipmentStateTransitionException(
                f"Illegal state transition: Cannot move shipment from '{current_status}' to '{new_status}'. "
                f"Allowed transitions from '{current_status}': {sorted(allowed_targets)}"
            )

        cur = active_conn.cursor()

        # Update timestamps
        now_ts = _utc_now_iso()
        actual_delivery_clause = ", actual_delivery = CURRENT_TIMESTAMP" if new_status == "DELIVERED" else ""
        dispatched_clause = ", dispatched_at = CURRENT_TIMESTAMP" if new_status == "IN_TRANSIT" and not row["dispatched_at"] else ""

        lat_val = location.get("latitude") if location else None
        lon_val = location.get("longitude") if location else None
        loc_name = location.get("location_name") if location else None

        if lat_val is not None and lon_val is not None:
            cur.execute(f"""
                UPDATE shipments
                SET status = ?, current_lat = ?, current_lon = ?, updated_at = CURRENT_TIMESTAMP {actual_delivery_clause} {dispatched_clause}
                WHERE id = ?;
            """, (new_status, lat_val, lon_val, shipment_id))
        else:
            cur.execute(f"""
                UPDATE shipments
                SET status = ?, updated_at = CURRENT_TIMESTAMP {actual_delivery_clause} {dispatched_clause}
                WHERE id = ?;
            """, (new_status, shipment_id))

        # Record corresponding milestone if applicable
        milestone_type = "EXCEPTION" if new_status == "EXCEPTION" else (
            "DELIVERY_COMPLETED" if new_status == "DELIVERED" else (
                "PICKUP_COMPLETED" if new_status == "PICKED_UP" else (
                    "OUT_FOR_DELIVERY_SCAN" if new_status == "OUT_FOR_DELIVERY" else (
                        "DEPARTURE_SCAN" if new_status == "IN_TRANSIT" else "EXCEPTION"
                    )
                )
            )
        )

        milestone_id = f"ms_{uuid.uuid4().hex[:12]}"
        cur.execute("""
            INSERT INTO shipment_milestones (
                id, shipment_id, milestone_type, location_name, latitude, longitude, notes, recorded_by, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            milestone_id, shipment_id, milestone_type,
            loc_name or f"Status changed to {new_status}",
            lat_val or row["current_lat"],
            lon_val or row["current_lon"],
            notes or f"FSM transition from {current_status} to {new_status}",
            actor_id
        ))

    _invalidate_shipment_caches(tracking_number)

    return {
        "id": shipment_id,
        "shipment_id": shipment_id,
        "tracking_number": tracking_number,
        "previous_status": current_status,
        "status": new_status,
        "shipment_status": new_status,
        "updated_at": now_ts,
        "milestone_id": milestone_id
    }


def record_shipment_milestone(
    identifier: str,
    milestone_type: str,
    location_name: str,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    notes: Optional[str] = None,
    recorded_by: Optional[str] = None,
    auto_transition: bool = True,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Appends a verified milestone event to 'shipment_milestones'.
    If auto_transition=True, drives the shipment's status to match the milestone type.
    Unconditionally rejects adding milestones to shipments in terminal states (DELIVERED, RETURNED).
    """
    milestone_type = milestone_type.upper().strip()
    status_aliases = {
        "IN_TRANSIT": "DEPARTURE_SCAN",
        "PICKED_UP": "PICKUP_COMPLETED",
        "OUT_FOR_DELIVERY": "OUT_FOR_DELIVERY_SCAN",
        "DELIVERED": "DELIVERY_COMPLETED",
        "CREATED": "MANIFEST_CREATED",
    }
    if milestone_type in status_aliases:
        milestone_type = status_aliases[milestone_type]

    if milestone_type not in VALID_MILESTONES:
        raise InvalidMilestoneException(
            f"Invalid milestone type: '{milestone_type}'. Allowed: {sorted(VALID_MILESTONES)}"
        )

    if latitude is not None and not (-90.0 <= float(latitude) <= 90.0):
        raise InvalidTelemetryException(f"Latitude out of bounds: {latitude}. Must be between -90.0 and 90.0.")
    if longitude is not None and not (-180.0 <= float(longitude) <= 180.0):
        raise InvalidTelemetryException(f"Longitude out of bounds: {longitude}. Must be between -180.0 and 180.0.")

    with transaction_scope(conn) as active_conn:
        row = _find_shipment_row(identifier, active_conn)
        shipment_id = row["id"]
        tracking_number = row["tracking_number"]
        current_status = row["status"]

        # Hardened terminal state check at function entry
        if current_status in ("DELIVERED", "RETURNED"):
            raise ShipmentStateTransitionException(
                f"Cannot record milestone '{milestone_type}' on shipment '{tracking_number}' in terminal state '{current_status}'."
            )

        # Check if milestone triggers a valid status change
        target_status = MILESTONE_STATUS_MAP.get(milestone_type)
        if auto_transition and target_status and target_status != current_status:
            # Check valid transition
            allowed = VALID_TRANSITIONS.get(current_status, set())
            if target_status not in allowed:
                raise ShipmentStateTransitionException(
                    f"Milestone '{milestone_type}' requires transition to '{target_status}', "
                    f"which is invalid from current status '{current_status}'."
                )

            # Update status
            cur = active_conn.cursor()
            actual_deliv = ", actual_delivery = CURRENT_TIMESTAMP" if target_status == "DELIVERED" else ""
            cur.execute(f"""
                UPDATE shipments
                SET status = ?, current_lat = COALESCE(?, current_lat), current_lon = COALESCE(?, current_lon),
                    updated_at = CURRENT_TIMESTAMP {actual_deliv}
                WHERE id = ?;
            """, (target_status, latitude, longitude, shipment_id))
            current_status = target_status
        elif latitude is not None and longitude is not None:
            cur = active_conn.cursor()
            cur.execute("""
                UPDATE shipments
                SET current_lat = ?, current_lon = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (latitude, longitude, shipment_id))

        milestone_id = f"ms_{uuid.uuid4().hex[:12]}"
        now_ts = _utc_now_iso()
        cur = active_conn.cursor()
        cur.execute("""
            INSERT INTO shipment_milestones (
                id, shipment_id, milestone_type, location_name, latitude, longitude, notes, recorded_by, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            milestone_id, shipment_id, milestone_type, location_name,
            latitude, longitude, notes, recorded_by
        ))

    _invalidate_shipment_caches(tracking_number)

    return {
        "id": milestone_id,
        "milestone_id": milestone_id,
        "shipment_id": shipment_id,
        "tracking_number": tracking_number,
        "milestone_type": milestone_type,
        "location_name": location_name,
        "latitude": latitude,
        "longitude": longitude,
        "notes": notes,
        "recorded_by": recorded_by,
        "timestamp": now_ts,
        "shipment_status": current_status,
        "status": "SUCCESS"
    }


# =============================================================================
# Real-Time Telemetry Ingestion & Querying (NoSQL)
# =============================================================================

def ingest_telemetry(
    tracking_number: str,
    telemetry_data: Dict[str, Any],
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Ingests polymorphic IoT telemetry (GPS, speed, cold-chain temperature, shock)
    into the 'shipment_telemetry' NoSQL DocumentStore collection.

    Validates coordinates, safely handles non-dict 'geo' objects, enforces shipment existence,
    updates relational current position (only if shipment is not in a terminal state),
    and invalidates the tracking cache.
    """
    if not isinstance(telemetry_data, dict):
        raise InvalidTelemetryException("Telemetry payload must be a JSON object (dict).")

    # Safely guard non-dict geo object
    geo_val = telemetry_data.get("geo")
    if "geo" in telemetry_data and geo_val is not None and not isinstance(geo_val, dict):
        raise InvalidTelemetryException(
            f"Invalid geo specification: expected object, got {type(geo_val).__name__}"
        )

    # Extract coordinates
    lat = telemetry_data.get("latitude")
    if lat is None and isinstance(geo_val, dict):
        lat = geo_val.get("latitude") or geo_val.get("lat")

    lon = telemetry_data.get("longitude")
    if lon is None and isinstance(geo_val, dict):
        lon = geo_val.get("longitude") or geo_val.get("lon")

    # Boundary verification
    if lat is not None:
        try:
            lat = float(lat)
            if not (-90.0 <= lat <= 90.0):
                raise InvalidTelemetryException(f"Latitude out of bounds: {lat}. Must be between -90.0 and 90.0.")
        except (TypeError, ValueError) as e:
            raise InvalidTelemetryException(f"Invalid latitude value: {lat}") from e

    if lon is not None:
        try:
            lon = float(lon)
            if not (-180.0 <= lon <= 180.0):
                raise InvalidTelemetryException(f"Longitude out of bounds: {lon}. Must be between -180.0 and 180.0.")
        except (TypeError, ValueError) as e:
            raise InvalidTelemetryException(f"Invalid longitude value: {lon}") from e

    # Verify shipment existence in relational database
    with transaction_scope(conn) as active_conn:
        row = _find_shipment_row(tracking_number, active_conn)
        shipment_id = row["id"]
        canonical_trk = row["tracking_number"]
        current_status = row["status"]

        # Update current position in relational shipments table only if not in terminal state
        if lat is not None and lon is not None and current_status not in ("DELIVERED", "RETURNED"):
            cur = active_conn.cursor()
            cur.execute("""
                UPDATE shipments
                SET current_lat = ?, current_lon = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (lat, lon, shipment_id))

    # Persist in NoSQL DocumentStore
    doc_copy = dict(telemetry_data)
    doc_copy["tracking_number"] = canonical_trk
    if "timestamp" not in doc_copy:
        doc_copy["timestamp"] = datetime.now(timezone.utc).isoformat()

    doc_store = get_doc_store()
    doc_id = doc_store.insert("shipment_telemetry", doc_copy)
    doc_copy["_id"] = doc_id

    _invalidate_shipment_caches(canonical_trk)

    return doc_copy


def get_shipment_telemetry(
    tracking_number: str,
    limit: int = 50,
    sort_by: str = "-created_at"
) -> List[Dict[str, Any]]:
    """
    Retrieves chronological telemetry readings for a shipment from NoSQL DocumentStore.
    """
    doc_store = get_doc_store()
    return doc_store.find(
        "shipment_telemetry",
        query={"tracking_number": tracking_number},
        limit=limit,
        sort_by=sort_by
    )


# =============================================================================
# Cache-Aside Shipment Tracking Details
# =============================================================================

def get_tracking_details(
    tracking_number: str,
    use_cache: bool = True,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Cache-Aside Tracking Query:
    1. Checks cache keys 'shipment:track:{tracking_number}' and 'shipment:tracking:{tracking_number}'.
       If hit, returns cached payload tagged with cache_status='HIT'.
    2. On miss, queries relational record + all shipment_milestones in ascending chronological order.
    3. Fetches latest NoSQL telemetry ping.
    4. Caches payload with TTL=60s.
    5. Returns payload tagged with cache_status='MISS'.
    """
    cache = get_cache_manager()
    cache_key = f"shipment:track:{tracking_number}"
    alias_key = f"shipment:tracking:{tracking_number}"

    if use_cache:
        cached_data = cache.get(cache_key) or cache.get(alias_key)
        if cached_data is not None:
            # Shallow copy to safely attach cache_status HIT
            result = dict(cached_data)
            result["cache_status"] = "HIT"
            return result

    # Cache MISS: Query relational database
    db = conn or get_db_connection()
    row = _find_shipment_row(tracking_number, db)

    shipment_id = row["id"]
    cur = db.cursor()

    # Milestones in strictly ascending chronological order
    cur.execute("""
        SELECT id, milestone_type, location_name, latitude, longitude, notes, recorded_by, timestamp
        FROM shipment_milestones
        WHERE shipment_id = ?
        ORDER BY timestamp ASC, id ASC;
    """, (shipment_id,))
    milestone_rows = cur.fetchall()

    milestones = [
        {
            "id": m["id"],
            "milestone_type": m["milestone_type"],
            "location_name": m["location_name"],
            "latitude": m["latitude"],
            "longitude": m["longitude"],
            "notes": m["notes"],
            "recorded_by": m["recorded_by"],
            "timestamp": str(m["timestamp"])
        }
        for m in milestone_rows
    ]

    # Latest NoSQL telemetry document
    latest_telemetry = None
    try:
        doc_store = get_doc_store()
        telem_list = doc_store.find(
            "shipment_telemetry",
            query={"tracking_number": tracking_number},
            limit=1,
            sort_by="-created_at"
        )
        if telem_list:
            latest_telemetry = telem_list[0]
    except Exception as e:
        logger.warning(f"Could not fetch telemetry for {tracking_number}: {e}")

    payload = {
        "id": shipment_id,
        "shipment_id": shipment_id,
        "tracking_number": row["tracking_number"],
        "tracking_num": row["tracking_number"],
        "order_id": row["order_id"],
        "order_number": row["order_number"],
        "origin_warehouse_id": row["origin_warehouse_id"],
        "origin_warehouse_name": row["origin_warehouse_name"],
        "carrier": row["carrier"],
        "service_level": row["service_level"],
        "status": row["status"],
        "shipment_status": row["status"],
        "estimated_delivery": str(row["estimated_delivery"]) if row["estimated_delivery"] else None,
        "actual_delivery": str(row["actual_delivery"]) if row["actual_delivery"] else None,
        "dispatched_at": str(row["dispatched_at"]) if row["dispatched_at"] else None,
        "current_lat": row["current_lat"],
        "current_lon": row["current_lon"],
        "assigned_driver_id": row["assigned_driver_id"],
        "destination_city": row["destination_city"],
        "destination_lat": row["destination_lat"],
        "destination_lon": row["destination_lon"],
        "shipping_address": row["shipping_address"],
        "milestones": milestones,
        "latest_telemetry": latest_telemetry,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"])
    }

    # Populate cache (TTL 60 seconds)
    if use_cache:
        cache.set(cache_key, payload, ttl_seconds=60)
        cache.set(alias_key, payload, ttl_seconds=60)

    # Return tagged with MISS for first fetch
    result = dict(payload)
    result["cache_status"] = "MISS"
    return result


# =============================================================================
# Proof of Delivery (POD) & Driver Assignment
# =============================================================================

def upload_proof_of_delivery(
    identifier: str,
    file_bytes: bytes,
    filename: str,
    content_type: str = "image/jpeg",
    signed_by: Optional[str] = None,
    notes: Optional[str] = None,
    actor_id: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Uploads proof-of-delivery document via StorageClient, logs DELIVERY_COMPLETED milestone,
    and transitions shipment to DELIVERED.
    Pre-validates that the shipment exists and is eligible for DELIVERED status before uploading file bytes.
    """
    if not file_bytes:
        raise ProofOfDeliveryException("Proof of delivery file bytes cannot be empty.")

    # Pre-validate shipment existence and status before initiating storage upload
    with transaction_scope(conn) as active_conn:
        row = _find_shipment_row(identifier, active_conn)
        current_status = row["status"]
        tracking_number = row["tracking_number"]

        if current_status in ("DELIVERED", "RETURNED"):
            raise ShipmentStateTransitionException(
                f"Cannot upload proof-of-delivery: shipment '{tracking_number}' is already in terminal state '{current_status}'."
            )

        allowed = VALID_TRANSITIONS.get(current_status, set())
        if "DELIVERED" not in allowed:
            raise ShipmentStateTransitionException(
                f"Cannot upload proof-of-delivery: transition from '{current_status}' to 'DELIVERED' is not allowed."
            )

    storage = get_storage_client()
    pod_url = storage.upload_file(file_bytes, filename, content_type=content_type)

    delivery_notes = notes or ""
    if signed_by:
        delivery_notes = f"Signed by: {signed_by}. {delivery_notes}".strip()
    delivery_notes = f"{delivery_notes} POD: {pod_url}".strip()

    # Transition to DELIVERED
    update_res = update_shipment_status(
        identifier=identifier,
        new_status="DELIVERED",
        actor_id=actor_id,
        notes=delivery_notes,
        conn=conn
    )

    return {
        "status": "DELIVERED",
        "tracking_number": update_res["tracking_number"],
        "pod_url": pod_url,
        "signed_by": signed_by,
        "notes": delivery_notes,
        "updated_at": update_res["updated_at"]
    }


def assign_driver(
    identifier: str,
    driver_id: str,
    conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Any]:
    """
    Assigns a registered transport driver to a shipment.
    """
    with transaction_scope(conn) as active_conn:
        row = _find_shipment_row(identifier, active_conn)
        shipment_id = row["id"]
        tracking_number = row["tracking_number"]

        cur = active_conn.cursor()
        cur.execute("SELECT id FROM users WHERE id = ?", (driver_id,))
        if not cur.fetchone():
            raise ShipmentException(f"Driver user '{driver_id}' does not exist.")

        cur.execute("""
            UPDATE shipments
            SET assigned_driver_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
        """, (driver_id, shipment_id))

    _invalidate_shipment_caches(tracking_number)

    return {
        "id": shipment_id,
        "tracking_number": tracking_number,
        "assigned_driver_id": driver_id,
        "status": "SUCCESS"
    }
