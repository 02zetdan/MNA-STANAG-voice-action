from models import CimTrack, CimContact, QualityVector
from datetime import datetime, timezone
from typing import Literal

# ── Trösklar ──────────────────────────────────────────────────────────────────
CONFIDENCE_CONFIRMED  = 0.80   
CONFIDENCE_UNCERTAIN  = 0.50   
STALENESS_MAX_S       = 30.0   
COMPLETENESS_MIN      = 0.60   

def _effective_confidence(q: QualityVector) -> float:
    staleness_penalty = min(q.staleness_s / STALENESS_MAX_S, 1.0) * 0.3
    return max(0.0, q.confidence - staleness_penalty)

def _certainty_label(eff_conf: float) -> Literal["CONFIRMED", "UNCERTAIN", "REJECTED"]:
    if eff_conf >= CONFIDENCE_CONFIRMED:
        return "CONFIRMED"
    if eff_conf >= CONFIDENCE_UNCERTAIN:
        return "UNCERTAIN"
    return "REJECTED"

def map_track(track: CimTrack) -> dict | None:
    eff_conf = _effective_confidence(track.quality)
    label    = _certainty_label(eff_conf)
    if label == "REJECTED":
        return {
            "_rejected": True,
            "reason": "confidence_too_low",
            "track_id": track.track_id,
            "effective_confidence": round(eff_conf, 3),
            "quality": track.quality.model_dump(),
        }
    gaps = []
    speed   = track.speed_kts
    heading = track.heading_deg
    if track.quality.completeness < COMPLETENESS_MIN:
        gaps.append("low_completeness")
        if speed is None:
            speed   = 0.0
            gaps.append("speed_defaulted_0")
        if heading is None:
            heading = 0.0
            gaps.append("heading_defaulted_0")
    return {
        "_rejected": False,
        "type":         "CATL.TrackUpdate",
        "schema":       "openlink/v1/catl_track",
        "trackId":      track.track_id,
        "trackType":    track.track_type.upper(),
        "isControllable": track.is_controllable,
        "position": {
            "lat": track.position.lat,
            "lon": track.position.lon,
        },
        "speedKts":     speed,
        "headingDeg":   heading,
        "certainty":    label,
        "qualityMeta": {
            "effectiveConfidence": round(eff_conf, 3),
            "completeness":        round(track.quality.completeness, 3),
            "stalenessS":          round(track.quality.staleness_s, 2),
            "sourceId":            track.quality.source_id,
            "gaps":                gaps,
        },
        "ts": datetime.now(timezone.utc).isoformat() + "Z",
        "rawSource": track.raw_source,
    }

def map_contact(contact: CimContact) -> dict | None:
    eff_conf = _effective_confidence(contact.quality)
    label    = _certainty_label(eff_conf)
    if label == "REJECTED":
        return {
            "_rejected": True,
            "reason":    "confidence_too_low",
            "contact_id": contact.contact_id,
            "effective_confidence": round(eff_conf, 3),
            "quality": contact.quality.model_dump(),
        }
    gaps = []
    if contact.quality.completeness < COMPLETENESS_MIN:
        gaps.append("low_completeness")
    return {
        "_rejected":      False,
        "type":           "CATL.ContactReport",
        "schema":         "openlink/v1/catl_contact",
        "contactId":      contact.contact_id,
        "classification": contact.classification.upper(),
        "position": {
            "lat": contact.position.lat,
            "lon": contact.position.lon,
        },
        "certainty": label,
        "qualityMeta": {
            "effectiveConfidence": round(eff_conf, 3),
            "completeness":        round(contact.quality.completeness, 3),
            "stalenessS":          round(contact.quality.staleness_s, 2),
            "sourceId":            contact.quality.source_id,
            "gaps":                gaps,
        },
        "ts":        datetime.now(timezone.utc).isoformat() + "Z",
        "rawSource": contact.raw_source,
    }