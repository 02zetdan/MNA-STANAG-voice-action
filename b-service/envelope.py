import uuid
from datetime import datetime, timezone

def wrap(payload: dict, message_type: str = "MESSAGE_TYPE_OBSERVATION_REPORT") -> dict:
    """
    Lindar in ett mappat payload i en OPENLINK-kompatibel envelope.
    Ed25519-signatur är mockad — ersätt med riktig nyckel vid behov.
    """
    return {
        "version":          1,
        "message_id":       str(uuid.uuid4()),
        "sent_at":          datetime.now(timezone.utc).isoformat() + "Z",
        "sender_node_id":   "b-service-node-01",
        "sender_public_key": "MOCK_ED25519_PUBKEY",
        "signature":         "MOCK_SIGNATURE",
        "type":              message_type,
        "idempotency_key":   str(uuid.uuid4()),
        "hop_count":         0,
        "payload":           payload,
    }