"""
Logistics IMS - Database Core & Storage Infrastructure Package.

Exposes primary database connection, transaction manager, NoSQL document store,
high-throughput cache manager, cloud storage client, and initialization utilities.
"""

from app.db.connection import (
    get_db_connection,
    create_connection,
    close_db_connection,
    close_all_connections,
    transaction_scope,
    set_default_db_path,
    get_default_db_path,
)
from app.db.nosql import DocumentStore, get_doc_store
from app.db.cache import CacheManager, get_cache_manager
from app.db.storage import (
    BaseStorageClient,
    LocalStorageClient,
    S3StorageClient,
    DualModeStorageClient,
    get_storage_client,
    set_storage_client,
)
from app.db.init_db import init_database, seed_demo_dataset

__all__ = [
    "get_db_connection",
    "create_connection",
    "close_db_connection",
    "close_all_connections",
    "transaction_scope",
    "set_default_db_path",
    "get_default_db_path",
    "DocumentStore",
    "get_doc_store",
    "CacheManager",
    "get_cache_manager",
    "BaseStorageClient",
    "LocalStorageClient",
    "S3StorageClient",
    "DualModeStorageClient",
    "get_storage_client",
    "set_storage_client",
    "init_database",
    "seed_demo_dataset",
]
