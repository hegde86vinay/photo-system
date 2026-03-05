"""
SYSTEM DESIGN CONCEPT: Cache-Aside Pattern (Lazy Loading)

CACHE-ASIDE VS OTHER PATTERNS:
  Cache-Aside (this file):
    Read:  Try cache → miss → read DB → populate cache → return
    Write: Write DB → invalidate cache (don't write to cache on write)
    Pro:   Cache only holds data actually read; resilient to cache failures
    Con:   First request always a cache miss (cold start)

  Write-Through:
    Write: Write DB AND write cache atomically
    Pro:   Cache always warm for written data
    Con:   Writes are slower; cache polluted with data never read again

  Write-Behind (Write-Back):
    Write: Write cache immediately, flush to DB asynchronously
    Pro:   Fastest writes
    Con:   Risk of data loss if cache crashes before DB flush

WHY CACHE-ASIDE HERE?
  Read:write ratio is ~100:1. We optimize for reads.
  Cache-aside is resilient: if Redis fails, app continues (slower, from DB).
  Write-through would waste cache space on 100x more writes.

CACHE HIT RATIO TARGET:
  At 1M DAU viewing ~5 photos each = 5M views/day = 58 views/sec avg.
  Cache TTL = 1 hour. Photo metadata = ~500 bytes.
  If 80% of views hit the same 20% of photos (Pareto): ~80% cache hit ratio.
  80% hit ratio at 58 req/sec means only 11.6 DB reads/sec → trivial DB load.

REDIS LRU EVICTION:
  maxmemory 256mb, policy allkeys-lru.
  When full, Redis evicts least-recently-used keys automatically.
  This matches exactly how photo popularity works (old unpopular photos evicted first).
"""
import json
import uuid
from typing import Any
import redis.asyncio as aioredis
from config import get_settings

settings = get_settings()

# Prometheus-style in-memory counters (exported via /metrics endpoint)
_cache_hits = 0
_cache_misses = 0


class CacheService:
    def __init__(self):
        self._client: aioredis.Redis | None = None

    async def get_client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(settings.redis_url, decode_responses=True)
        return self._client

    async def get_photo_metadata(self, photo_id: uuid.UUID) -> dict | None:
        """
        Cache-aside read: try Redis, return None on miss.
        Caller is responsible for DB fallback and cache population.
        """
        global _cache_hits, _cache_misses
        client = await self.get_client()
        key = f"photo:{photo_id}"
        raw = await client.get(key)
        if raw:
            _cache_hits += 1
            return json.loads(raw)
        _cache_misses += 1
        return None

    async def set_photo_metadata(self, photo_id: uuid.UUID, data: dict) -> None:
        """Populate cache after a DB read. TTL = 1 hour."""
        client = await self.get_client()
        key = f"photo:{photo_id}"
        await client.setex(key, settings.cache_ttl, json.dumps(data, default=str))

    async def invalidate(self, photo_id: uuid.UUID) -> None:
        """
        Invalidate on write/update. DEL is safer than writing new value:
        avoids race condition where stale value overwrites a fresher DB value.
        """
        client = await self.get_client()
        await client.delete(f"photo:{photo_id}")

    async def ping(self) -> bool:
        """Health check: verify Redis is reachable."""
        try:
            client = await self.get_client()
            return await client.ping()
        except Exception:
            return False

    @staticmethod
    def get_metrics() -> dict:
        total = _cache_hits + _cache_misses
        hit_ratio = round(_cache_hits / total, 4) if total > 0 else 0.0
        return {
            "cache_hits_total": _cache_hits,
            "cache_misses_total": _cache_misses,
            "cache_hit_ratio": hit_ratio,
        }
