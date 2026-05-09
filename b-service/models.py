from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timezone
import uuid

class QualityVector(BaseModel):
    completeness: float = Field(..., ge=0.0, le=1.0)   # andel fält som faktiskt kom in
    confidence:   float = Field(..., ge=0.0, le=1.0)   # källtillit
    staleness_s:  float = Field(..., ge=0.0)            # sekunder sedan senaste fix
    source_id:    str                                    # provenance

class Position(BaseModel):
    lat: float
    lon: float
    alt_m: Optional[float] = None

class CimTrack(BaseModel):
    track_id:       str = Field(default_factory=lambda: f"TRK-{uuid.uuid4().hex[:6].upper()}")
    position:       Position
    speed_kts:      Optional[float] = None
    heading_deg:    Optional[float] = None
    track_type:     Literal["surface", "subsurface", "air", "unknown"] = "unknown"
    quality:        QualityVector
    ingest_ts:      datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_source:     str = "mock"

class CimContact(BaseModel):
    contact_id:     str = Field(default_factory=lambda: f"CNT-{uuid.uuid4().hex[:6].upper()}")
    position:       Position
    classification: Literal["friendly", "hostile", "neutral", "unknown"] = "unknown"
    quality:        QualityVector
    ingest_ts:      datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_source:     str = "mock"