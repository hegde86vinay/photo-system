"""
REST API endpoints for photo operations.

SYSTEM DESIGN CONCEPT: Thin Controllers
  API handlers validate input and delegate to the service layer.
  No business logic here — only HTTP concerns (status codes, headers, content types).
  This separation allows the service layer to be tested independently.

ENDPOINTS:
  POST /api/photos/upload          Upload a new photo
  GET  /api/photos/search          Search by title / product_id
  GET  /api/photos/{id}            Get photo metadata + presigned URL
  GET  /api/photos/{id}/download   Stream raw photo bytes

NOTE on route ordering:
  /search must be registered BEFORE /{id} to avoid FastAPI treating
  "search" as a UUID path parameter.
"""
import uuid
from fastapi import APIRouter, UploadFile, File, Form, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from db.connection import get_db
from models.schemas import (
    PhotoUploadResponse, PhotoMetadataResponse,
    PhotoSearchResponse,
)
from services.photo_service import PhotoService

router = APIRouter(prefix="/api/photos", tags=["photos"])
_service = PhotoService()


@router.post("/upload", response_model=PhotoUploadResponse, status_code=201)
async def upload_photo(
    file: UploadFile = File(..., description="Photo file (JPEG, PNG, WebP, GIF)"),
    title: str = Form(..., min_length=1, max_length=500),
    user_id: uuid.UUID = Form(...),
    product_id: str | None = Form(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a photo.

    Flow: validate → MinIO (HOT) → PostgreSQL → Elasticsearch → return presigned URL.

    CONCEPT: Multipart upload
      For large files, production systems use multipart/chunked uploads directly to S3.
      Client gets a presigned multipart upload URL, uploads chunks directly to S3,
      then sends a "complete" request. Backend never touches the bytes.
      This scales to GB-sized files without OOM on the backend.
      Here we accept bytes directly (suitable up to ~10MB with our size limit).
    """
    return await _service.upload_photo(file, title, user_id, product_id, db)


@router.get("/search", response_model=PhotoSearchResponse)
async def search_photos(
    q: str | None = Query(default=None, description="Full-text search query"),
    product_id: str | None = Query(default=None, description="Filter by exact product ID"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """
    Search photos by title and/or product_id using Elasticsearch.

    CONCEPT: Why this route is BEFORE /{id}
      FastAPI matches routes in registration order.
      If /{id} came first, "search" would be interpreted as a UUID → 422 error.
      Always register specific routes before parameterized ones.
    """
    return await _service.search_photos(q, product_id, page, size, db)


@router.get("/{photo_id}", response_model=PhotoMetadataResponse)
async def get_photo(
    photo_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Get photo metadata + 1-hour presigned URL.

    Cache-Control header tells browsers/CDN to cache for 1 hour.
    CONCEPT: CDN caching
      In production, place CloudFront/CDN in front of this endpoint.
      CDN caches the presigned URL response; subsequent requests are served
      from CDN edge node near the user → <50ms latency globally.
      This is how we achieve the 200ms NFR even for users far from origin.
    """
    response = await _service.get_photo(photo_id, db)
    return response


@router.get("/{photo_id}/download")
async def download_photo(
    photo_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """
    Stream raw photo bytes with correct Content-Type header.

    CONCEPT: Streaming vs. Redirect
      Option A (this): Backend fetches from MinIO and streams to client.
        Pro: backend controls access, can log download metrics.
        Con: backend uses bandwidth (460 MB/s peak = problem at scale).
      Option B: 302 Redirect to presigned URL.
        Pro: zero backend bandwidth.
        Con: client must follow redirect; presigned URL exposed in browser history.
      → Use presigned URL (GET /api/photos/{id}) for production.
        Keep this endpoint for server-side download use cases.
    """
    data, content_type = await _service.download_photo(photo_id, db)
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f"attachment; filename={photo_id}"},
    )
