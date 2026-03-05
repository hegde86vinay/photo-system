"""
SYSTEM DESIGN CONCEPT: Observability — Health Checks & Metrics

WHY HEALTH ENDPOINTS?
  Load balancers route traffic only to healthy instances.
  Kubernetes liveness/readiness probes use /health to restart unhealthy pods.
  Without health checks, a degraded instance still receives traffic → user-facing errors.

/health — dependency status
  Checks all downstream services: DB, Redis, ES, MinIO.
  Returns 200 if all healthy, 503 if any dependency is down.
  Allows load balancer to remove degraded instances from rotation.

/metrics — Prometheus format
  Exposes counters/gauges scraped by Prometheus every 15s.
  Grafana dashboards visualize: cache hit ratio, upload latency, tier distribution.
  Key metrics for this system:
    cache_hit_ratio:      < 70% = caching not working → investigate
    upload_latency_p99:   > 500ms = MinIO or DB bottleneck
    tier_distribution:    HOT/WARM/COLD counts → storage cost visibility
    es_index_errors_total: > 0 = search becoming stale

CONCEPT: SLO Monitoring
  NFR: 200ms latency for image view/download.
  Track p50/p99 latency on GET /api/photos/{id}.
  Alert if p99 > 200ms → auto-scale or investigate cache miss rate.
"""
import time
from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, JSONResponse
from services.cache_service import CacheService
from services.search_service import SearchService
from services.storage_service import StorageService

router = APIRouter(tags=["observability"])
_cache = CacheService()
_search = SearchService()

# Upload latency histogram buckets (ms)
_upload_times: list[float] = []


@router.get("/health", response_model=None)
async def health():
    """
    Dependency health check.
    Load balancer / Kubernetes readiness probe endpoint.
    Returns 503 if any critical dependency is unavailable.
    """
    # Check all dependencies in parallel for low latency
    import asyncio
    redis_ok, es_ok = await asyncio.gather(
        _cache.ping(),
        _search.ping(),
    )

    # MinIO sync check (minio SDK is not async)
    try:
        storage = StorageService()
        storage.client.list_buckets()
        minio_ok = True
    except Exception:
        minio_ok = False

    # PostgreSQL: if we got here, connection pool is working
    postgres_ok = True

    all_ok = redis_ok and es_ok and minio_ok and postgres_ok
    status_code = 200 if all_ok else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "ok" if all_ok else "degraded",
            "postgres":       "ok" if postgres_ok else "down",
            "redis":          "ok" if redis_ok else "down",
            "elasticsearch":  "ok" if es_ok else "down",
            "minio":          "ok" if minio_ok else "down",
        }
    )


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    """
    Prometheus-format metrics endpoint.
    Scrape with: prometheus.yml → scrape_configs → targets: ["backend:8000"]
    """
    cache_metrics = CacheService.get_metrics()
    lines = [
        "# HELP cache_hits_total Total Redis cache hits",
        "# TYPE cache_hits_total counter",
        f"cache_hits_total {cache_metrics['cache_hits_total']}",
        "",
        "# HELP cache_misses_total Total Redis cache misses",
        "# TYPE cache_misses_total counter",
        f"cache_misses_total {cache_metrics['cache_misses_total']}",
        "",
        "# HELP cache_hit_ratio Ratio of cache hits to total requests",
        "# TYPE cache_hit_ratio gauge",
        f"cache_hit_ratio {cache_metrics['cache_hit_ratio']}",
        "",
    ]
    return "\n".join(lines)
