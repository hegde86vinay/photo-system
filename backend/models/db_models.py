"""
ORM models mirroring the partitioned PostgreSQL schema.

SYSTEM DESIGN CONCEPT: ORM vs Raw SQL
  ORM (SQLAlchemy): Pythonic, prevents SQL injection, easy refactoring.
  Raw SQL: Faster for complex queries, full control over query plan.
  → Use ORM for CRUD, raw SQL for analytics/reporting queries.
"""
import uuid
from datetime import datetime, timezone
from sqlalchemy import String, BigInteger, Text, DateTime, ForeignKey, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from db.connection import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    photos: Mapped[list["Photo"]] = relationship("Photo", back_populates="user")


class Photo(Base):
    """
    CONCEPT: Denormalized storage_tier field
    Avoids JOIN with a separate tiers table on every download request.
    Trade-off: small inconsistency window between MinIO move and DB update,
    acceptable per NFR #3 (eventual consistency).
    """
    __tablename__ = "photos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    product_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)       # MinIO object key
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False, default="image/jpeg")
    storage_tier: Mapped[str] = mapped_column(String(10), nullable=False, default="HOT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_accessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("storage_tier IN ('HOT','WARM','COLD')", name="ck_photos_tier"),
    )

    user: Mapped[User] = relationship("User", back_populates="photos")
