"""
High-Throughput In-Memory Caching Layer for Logistics IMS.

Provides a thread-safe, Cache-Aside pattern manager supporting:
- Configurable TTL expiration with lazy purge on read
- Capacity-bounded LRU (Least Recently Used) eviction via collections.OrderedDict
- Expired key pruning on capacity boundary before evicting valid data
- Guarding against max_entries <= 0 and proper TTL <= 0 immediate expiration semantics
- Glob pattern invalidation ('shipment:track:*', 'inventory:wh-01:*') via fnmatch
- Operational telemetry: hits, misses, evictions, invalidations, and hit_ratio
"""

import collections
import fnmatch
import threading
import time
from typing import Any, Dict, NamedTuple, Optional


class CacheEntry(NamedTuple):
    """Immutable cache value wrapper with expiration timestamp."""
    value: Any
    expires_at: Optional[float]


class CacheManager:
    """
    Thread-safe in-memory CacheManager implementing LRU eviction,
    TTL expiration, pattern invalidation, and metrics tracking.
    """

    def __init__(self, max_entries: int = 1000, default_ttl: int = 300):
        """
        Initializes CacheManager.

        Args:
            max_entries: Maximum keys allowed before triggering LRU eviction (must be >= 1).
            default_ttl: Default time-to-live in seconds (300 = 5 minutes). Must be > 0 or None.
        """
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if default_ttl is not None and default_ttl <= 0:
            raise ValueError("default_ttl must be positive or None")

        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._cache: collections.OrderedDict[str, CacheEntry] = collections.OrderedDict()
        self._lock = threading.RLock()

        # Telemetry metrics
        self._hits: int = 0
        self._misses: int = 0
        self._evictions: int = 0
        self._invalidations: int = 0

    def get(self, key: str) -> Optional[Any]:
        """
        Retrieves a cached value by key.

        - If key does not exist: records miss, returns None.
        - If key exists but has expired: purges key, records miss and invalidation, returns None.
        - If key exists and is valid: updates LRU position, records hit, returns value.
        """
        with self._lock:
            if key not in self._cache:
                self._misses += 1
                return None

            entry = self._cache[key]
            now = time.monotonic()

            # Automatic expiration evaluation
            if entry.expires_at is not None and now >= entry.expires_at:
                del self._cache[key]
                self._misses += 1
                self._invalidations += 1
                return None

            # Cache hit: promote key to most recently used
            self._cache.move_to_end(key)
            self._hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl_seconds: Optional[int] = 300) -> None:
        """
        Stores an item in the cache with the given TTL.

        - If ttl_seconds is None, inherits default_ttl.
        - If ttl is None, the entry has no expiration (immortal).
        - If ttl <= 0, the entry expires immediately (expires_at is in the past / current time).
        - When at maximum capacity:
          1. First prunes any expired keys.
          2. If still at capacity, evicts the least recently used entry (popitem(last=False)).
        """
        ttl = ttl_seconds if ttl_seconds is not None else self.default_ttl
        expires_at = (time.monotonic() + ttl) if ttl is not None else None
        entry = CacheEntry(value=value, expires_at=expires_at)

        with self._lock:
            if key in self._cache:
                self._cache[key] = entry
                self._cache.move_to_end(key)
                return

            # Capacity management
            if len(self._cache) >= self.max_entries:
                # Step 1: Prune any expired keys first
                now = time.monotonic()
                expired_keys = [
                    k for k, e in self._cache.items()
                    if e.expires_at is not None and now >= e.expires_at
                ]
                for k in expired_keys:
                    del self._cache[k]
                    self._invalidations += 1

                # Step 2: If still at or over capacity, perform LRU eviction
                while len(self._cache) >= self.max_entries:
                    evicted_key, _ = self._cache.popitem(last=False)
                    self._evictions += 1

            self._cache[key] = entry
            self._cache.move_to_end(key)

    def delete(self, key: str) -> bool:
        """
        Explicitly removes a specific key from the cache.

        Returns:
            True if key existed and was deleted, False otherwise.
        """
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._invalidations += 1
                return True
            return False

    def invalidate_pattern(self, pattern: str) -> int:
        """
        Evicts all keys matching the glob pattern (e.g. 'inventory:wh-01:*').

        Returns:
            Count of keys successfully invalidated.
        """
        with self._lock:
            matching_keys = [
                k for k in self._cache.keys()
                if fnmatch.fnmatchcase(k, pattern)
            ]
            for k in matching_keys:
                del self._cache[k]
            count = len(matching_keys)
            self._invalidations += count
            return count

    def flush(self) -> None:
        """Clears all entries from the cache."""
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> Dict[str, Any]:
        """
        Returns telemetry metrics dictionary.

        Keys:
        - hits / cache_hits: Total successful cache reads.
        - misses / cache_misses: Total cache read misses (including expired items).
        - evictions: Total items evicted due to LRU capacity limits.
        - invalidations: Total keys removed via delete, pattern, or TTL expiry.
        - hit_ratio: hits / (hits + misses) rounded to 4 decimals (0.0 to 1.0).
        - size: Current active entries count.
        - max_entries: Capacity limit.
        """
        with self._lock:
            total_requests = self._hits + self._misses
            hit_ratio = round(self._hits / total_requests, 4) if total_requests > 0 else 0.0
            return {
                "hits": self._hits,
                "cache_hits": self._hits,
                "misses": self._misses,
                "cache_misses": self._misses,
                "evictions": self._evictions,
                "invalidations": self._invalidations,
                "hit_ratio": hit_ratio,
                "size": len(self._cache),
                "max_entries": self.max_entries
            }

    def reset_stats(self) -> None:
        """Resets telemetry metric counters without clearing cache entries."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._evictions = 0
            self._invalidations = 0


# Global Singleton Instance
_GLOBAL_CACHE_MANAGER: Optional[CacheManager] = None
_CACHE_LOCK = threading.Lock()


def get_cache_manager(max_entries: int = 1000, default_ttl: int = 300) -> CacheManager:
    """Singleton provider for CacheManager instance."""
    global _GLOBAL_CACHE_MANAGER
    with _CACHE_LOCK:
        if _GLOBAL_CACHE_MANAGER is None:
            _GLOBAL_CACHE_MANAGER = CacheManager(max_entries=max_entries, default_ttl=default_ttl)
        return _GLOBAL_CACHE_MANAGER
