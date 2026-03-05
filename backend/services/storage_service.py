"""
SYSTEM DESIGN CONCEPT: Object Storage + Presigned URLs

WHY OBJECT STORAGE (MinIO/S3) INSTEAD OF DATABASE BLOBS?
  1. Cost: S3 = $0.023/GB vs RDS storage = $0.115/GB (5x cheaper)
  2. Throughput: S3 can serve millions of concurrent downloads; DB cannot.
  3. CDN integration: S3 has native CloudFront/CDN support; DB blobs don't.
  4. Streaming: S3 supports byte-range requests (video seek, resumable downloads).
  5. VACUUM: PostgreSQL VACUUM on a table with TOAST blobs is extremely slow.

PRESIGNED URL PATTERN:
  Client ──GET /api/photos/{id}──→ Backend ──→ DB (metadata)
  Backend generates a presigned URL (signed with MinIO secret, time-limited)
  Client ──GET presigned_url──→ MinIO directly (BYPASSES backend)

  This eliminates the backend as a bandwidth bottleneck.
  At 460 MB/s peak bandwidth, the backend would collapse without this pattern.
  MinIO/S3 can scale bandwidth horizontally.

STORAGE TIER MAPPING:
  HOT  (photos-hot)  → S3 Standard    (0-12 months, frequent access)
  WARM (photos-warm) → S3 Standard-IA (1-3 years, infrequent access, 40% cheaper)
  COLD (photos-cold) → S3 Glacier     (3-7 years, retrieval takes hours, 80% cheaper)
"""
import io
import uuid
from datetime import timedelta
from minio import Minio
from minio.error import S3Error
from config import get_settings

settings = get_settings()


class StorageService:
    def __init__(self):
        self.client = Minio(
            settings.minio_endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=settings.minio_secure,
        )

    def upload(self, file_data: bytes, object_key: str, content_type: str = "image/jpeg") -> str:
        """
        Upload photo bytes to the HOT bucket.
        Returns the object key (permanent identifier in our DB).

        CONCEPT: Object key design
          Key = "{year}/{month}/{uuid}.{ext}"
          Year/month prefix allows efficient listing by time period.
          UUID ensures no collisions across concurrent uploads.
        """
        self.client.put_object(
            bucket_name=settings.hot_bucket,
            object_name=object_key,
            data=io.BytesIO(file_data),
            length=len(file_data),
            content_type=content_type,
        )
        return object_key

    def get_presigned_url(self, object_key: str, tier: str = "HOT") -> str:
        """
        Generate a presigned URL valid for 1 hour.
        Client downloads directly from MinIO — backend not involved in data transfer.

        CONCEPT: Time-limited presigned URLs
          - URL is cryptographically signed with MinIO secret key
          - Expires after 1 hour (configurable)
          - No backend involvement during download → zero bandwidth cost on backend
          - In production: use CloudFront signed URLs for CDN caching layer
        """
        bucket = settings.bucket_for_tier(tier)
        return self.client.presigned_get_object(
            bucket_name=bucket,
            object_name=object_key,
            expires=timedelta(hours=1),
        )

    def get_object(self, object_key: str, tier: str = "HOT") -> bytes:
        """
        Download photo bytes (used for streaming download endpoint).
        Supports byte-range requests (important for large files / video).
        """
        bucket = settings.bucket_for_tier(tier)
        response = self.client.get_object(bucket_name=bucket, object_name=object_key)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def move_to_tier(self, object_key: str, from_tier: str, to_tier: str) -> None:
        """
        Move object between storage tiers (HOT→WARM→COLD).

        CONCEPT: Copy-then-delete (not rename)
          Object storage has no atomic rename/move operation.
          We must: 1) copy to destination bucket, 2) delete from source.
          Risk: crash between copy and delete leaves duplicate.
          Mitigation: check destination exists before deleting source.
        """
        from_bucket = settings.bucket_for_tier(from_tier)
        to_bucket = settings.bucket_for_tier(to_tier)

        # Copy to destination
        from minio.commonconfig import CopySource
        self.client.copy_object(
            bucket_name=to_bucket,
            object_name=object_key,
            source=CopySource(from_bucket, object_key),
        )

        # Verify copy succeeded before deleting source
        try:
            self.client.stat_object(to_bucket, object_key)
        except S3Error:
            raise RuntimeError(f"Copy verification failed: {object_key} not found in {to_bucket}")

        # Delete from source
        self.client.remove_object(from_bucket, object_key)

    def generate_object_key(self, original_filename: str) -> str:
        """
        Generate a unique object key with time-based prefix for efficient listing.
        Pattern: YYYY/MM/uuid4.ext
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else "jpg"
        return f"{now.year}/{now.month:02d}/{uuid.uuid4().hex}.{ext}"
