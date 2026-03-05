"""
Pydantic schemas for API request validation and response serialization.

SYSTEM DESIGN CONCEPT: Schema validation at the API boundary
  Validate at system boundaries (user input, external APIs) — not deep inside services.
  Pydantic catches malformed requests before they hit the DB.
"""
import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class PhotoUploadResponse(BaseModel):
    id: uuid.UUID
    title: str
    product_id: str | None
    filename: str
    size_bytes: int
    storage_tier: str
    created_at: datetime
    url: str                  # 1-hour presigned MinIO URL for immediate viewing


class PhotoMetadataResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    product_id: str | None
    filename: str
    size_bytes: int
    content_type: str
    storage_tier: str         # HOT / WARM / COLD — tells client which tier
    created_at: datetime
    last_accessed_at: datetime
    url: str                  # presigned URL valid for 1 hour


class PhotoSearchResult(BaseModel):
    id: uuid.UUID
    title: str
    product_id: str | None
    size_bytes: int
    storage_tier: str
    created_at: datetime
    url: str
    score: float = Field(default=1.0, description="Elasticsearch relevance score")


class PhotoSearchResponse(BaseModel):
    total: int
    page: int
    size: int
    results: list[PhotoSearchResult]


class HealthStatus(BaseModel):
    status: str               # "ok" or "degraded"
    postgres: str
    redis: str
    elasticsearch: str
    minio: str
