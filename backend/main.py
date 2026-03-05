"""
FastAPI application entry point.

SYSTEM DESIGN CONCEPT: Lifespan Events (Startup / Shutdown)
  On startup:  warm up connection pools, ensure ES index exists.
  On shutdown: close DB connections gracefully — prevent connection leaks.

  In production: startup also runs DB migrations (Alembic), warms Redis cache
  with frequently accessed photo IDs, and validates external service connectivity.

CONCEPT: CORS Policy
  Frontend (http://localhost:3000) is a different origin than backend (http://localhost:8000).
  Without CORS headers, browsers block cross-origin requests (Same-Origin Policy).
  In production: restrict allow_origins to your actual domain, not "*".
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from db.connection import engine
from services.search_service import SearchService
from api.photos import router as photos_router
from api.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    print("Starting up Photo System...")

    # Ensure Elasticsearch index exists (idempotent)
    search = SearchService()
    try:
        await search.ensure_index()
        print("✓ Elasticsearch index ready")
    except Exception as e:
        print(f"⚠ Elasticsearch init failed: {e} (search will degrade gracefully)")

    print("✓ All services connected — ready to serve requests")

    yield   # ← Application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────────
    print("Shutting down — closing connection pools...")
    await engine.dispose()
    print("✓ DB connections closed")


app = FastAPI(
    title="Photo Sharing System",
    description="""
## System Design Learning App

This API demonstrates production-grade distributed system patterns:

- **Object Storage** (MinIO/S3): Upload/download via presigned URLs
- **Cache-Aside** (Redis): Metadata caching with LRU eviction
- **Full-Text Search** (Elasticsearch): Inverted index on photo titles
- **Table Partitioning** (PostgreSQL): Time-based sharding for 5B+ rows
- **Storage Tiering**: HOT→WARM→COLD lifecycle for 7-year retention
- **Observability**: /health + /metrics endpoints

Scale: 500M users · 2M photos/day · ~1 PB over 7 years
    """,
    version="1.0.0",
    lifespan=lifespan,
)

# CORS: allow frontend (localhost:3000) to call backend (localhost:8000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(photos_router)
app.include_router(health_router)


@app.get("/", tags=["root"])
async def root():
    return {
        "service":    "Photo Sharing System",
        "version":    "1.0.0",
        "docs":       "/docs",
        "health":     "/health",
        "metrics":    "/metrics",
        "endpoints": {
            "upload":   "POST /api/photos/upload",
            "view":     "GET  /api/photos/{id}",
            "download": "GET  /api/photos/{id}/download",
            "search":   "GET  /api/photos/search?q=keyword&product_id=P001",
        }
    }
