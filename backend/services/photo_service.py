"""
SYSTEM DESIGN CONCEPT: Service Layer as Orchestrator

The photo_service coordinates all sub-systems for each operation.
It owns the business logic and the sequence of calls across:
  PostgreSQL → Redis → MinIO → Elasticsearch

UPLOAD FLOW:
  1. Validate file (type, size)
  2. Generate unique object key
  3. Upload bytes to MinIO HOT bucket  ← durability first
  4. Insert metadata row in PostgreSQL ← source of truth
  5. Index in Elasticsearch            ← search replica (eventual consistency OK)
  6. Return metadata + presigned URL

  ORDERING MATTERS:
  If MinIO upload fails → don't write to DB → no orphan metadata.
  If ES index fails → log + continue → NFR #3 allows eventual consistency.
  If DB insert fails after MinIO upload → orphan object in MinIO.
  Production mitigation: store "pending" status in DB before MinIO upload;
  update to "active" after. Background job cleans up stuck "pending" rows.

FETCH FLOW:
  1. Try Redis cache (cache-aside)
  2. On miss: query PostgreSQL, populate cache
  3. Log access (for tier migration decisions)
  4. Generate presigned URL for the correct tier bucket
  5. Return metadata + URL

DOWNLOAD FLOW:
  Direct stream from MinIO (supports Range requests for byte-range access).
"""
import uuid
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import UploadFile, HTTPException
from models.db_models import Photo, User
from models.schemas import PhotoUploadResponse, PhotoMetadataResponse, PhotoSearchResponse, PhotoSearchResult
from services.storage_service import StorageService
from services.cache_service import CacheService
from services.search_service import SearchService

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10MB hard limit per upload


class PhotoService:
    def __init__(self):
        self.storage = StorageService()
        self.cache   = CacheService()
        self.search  = SearchService()

    async def upload_photo(self, file: UploadFile, title: str, user_id: uuid.UUID,
                           product_id: str | None, db: AsyncSession) -> PhotoUploadResponse:
        # ── Validate ──────────────────────────────────────────────────────────
        if file.content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(400, f"Unsupported file type: {file.content_type}. Allowed: {ALLOWED_CONTENT_TYPES}")

        file_data = await file.read()
        if len(file_data) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(413, f"File too large: {len(file_data)} bytes. Max: {MAX_FILE_SIZE_BYTES}")

        # Verify user exists
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(404, f"User {user_id} not found")

        # ── Store binary in MinIO first (durability before metadata) ──────────
        object_key = self.storage.generate_object_key(file.filename or "photo.jpg")
        self.storage.upload(file_data, object_key, file.content_type or "image/jpeg")
        logger.info(f"Uploaded to MinIO: {object_key} ({len(file_data)} bytes)")

        # ── Insert metadata in PostgreSQL (source of truth) ───────────────────
        photo = Photo(
            user_id      = user_id,
            title        = title,
            product_id   = product_id,
            filename     = file.filename or "photo.jpg",
            file_path    = object_key,
            size_bytes   = len(file_data),
            content_type = file.content_type or "image/jpeg",
            storage_tier = "HOT",
        )
        db.add(photo)
        await db.commit()
        await db.refresh(photo)
        logger.info(f"Saved metadata: photo_id={photo.id}")

        # ── Index in Elasticsearch (eventual consistency, NFR #3) ─────────────
        try:
            await self.search.ensure_index()
            await self.search.index_photo(
                photo_id    = photo.id,
                title       = photo.title,
                product_id  = photo.product_id,
                user_id     = photo.user_id,
                size_bytes  = photo.size_bytes,
                storage_tier= photo.storage_tier,
                created_at  = photo.created_at.isoformat(),
            )
        except Exception as e:
            # Log but don't fail the upload — search is eventually consistent
            logger.warning(f"ES indexing failed for photo {photo.id}: {e}. Will retry on next search.")

        # ── Generate presigned URL for immediate viewing ───────────────────────
        url = self.storage.get_presigned_url(photo.file_path, photo.storage_tier)

        return PhotoUploadResponse(
            id           = photo.id,
            title        = photo.title,
            product_id   = photo.product_id,
            filename     = photo.filename,
            size_bytes   = photo.size_bytes,
            storage_tier = photo.storage_tier,
            created_at   = photo.created_at,
            url          = url,
        )

    async def get_photo(self, photo_id: uuid.UUID, db: AsyncSession) -> PhotoMetadataResponse:
        # ── Cache-aside: try Redis first ──────────────────────────────────────
        cached = await self.cache.get_photo_metadata(photo_id)
        if cached:
            logger.debug(f"Cache HIT: photo {photo_id}")
            url = self.storage.get_presigned_url(cached["file_path"], cached["storage_tier"])
            return PhotoMetadataResponse(**cached, url=url)

        # ── Cache miss: read from PostgreSQL ──────────────────────────────────
        logger.debug(f"Cache MISS: photo {photo_id} — querying DB")
        stmt = select(Photo).where(Photo.id == photo_id)
        result = await db.execute(stmt)
        photo = result.scalar_one_or_none()
        if not photo:
            raise HTTPException(404, f"Photo {photo_id} not found")

        # ── Populate cache for future reads ───────────────────────────────────
        meta_dict = {
            "id":               str(photo.id),
            "user_id":          str(photo.user_id),
            "title":            photo.title,
            "product_id":       photo.product_id,
            "filename":         photo.filename,
            "file_path":        photo.file_path,
            "size_bytes":       photo.size_bytes,
            "content_type":     photo.content_type,
            "storage_tier":     photo.storage_tier,
            "created_at":       photo.created_at.isoformat(),
            "last_accessed_at": photo.last_accessed_at.isoformat(),
        }
        await self.cache.set_photo_metadata(photo_id, meta_dict)

        url = self.storage.get_presigned_url(photo.file_path, photo.storage_tier)
        return PhotoMetadataResponse(**meta_dict, url=url)

    async def download_photo(self, photo_id: uuid.UUID, db: AsyncSession) -> tuple[bytes, str]:
        """Returns raw bytes + content_type for streaming download."""
        stmt = select(Photo).where(Photo.id == photo_id)
        result = await db.execute(stmt)
        photo = result.scalar_one_or_none()
        if not photo:
            raise HTTPException(404, f"Photo {photo_id} not found")
        data = self.storage.get_object(photo.file_path, photo.storage_tier)
        return data, photo.content_type

    async def search_photos(self, q: str | None, product_id: str | None,
                            page: int, size: int, db: AsyncSession) -> PhotoSearchResponse:
        """Search via Elasticsearch, enrich results with presigned URLs."""
        if not q and not product_id:
            raise HTTPException(400, "Provide at least one of: q, product_id")

        total, hits = await self.search.search(q, product_id, page, size)

        results = []
        for hit in hits:
            url = self.storage.get_presigned_url(
                # For search results we need the file_path. ES stores only summary fields.
                # Fetch file_path from DB for each hit (or store in ES — trade-off).
                # Here: quick DB lookup per hit (small result set, fast by indexed UUID).
                await self._get_file_path(uuid.UUID(hit["id"]), db),
                hit.get("storage_tier", "HOT"),
            )
            results.append(PhotoSearchResult(
                id           = uuid.UUID(hit["id"]),
                title        = hit["title"],
                product_id   = hit.get("product_id"),
                size_bytes   = hit.get("size_bytes", 0),
                storage_tier = hit.get("storage_tier", "HOT"),
                created_at   = hit["created_at"],
                url          = url,
                score        = hit.get("score", 1.0),
            ))

        return PhotoSearchResponse(total=total, page=page, size=size, results=results)

    async def _get_file_path(self, photo_id: uuid.UUID, db: AsyncSession) -> str:
        stmt = select(Photo.file_path).where(Photo.id == photo_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none() or ""
