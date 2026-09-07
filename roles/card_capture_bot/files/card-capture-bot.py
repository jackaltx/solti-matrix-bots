#!/usr/bin/env python3
"""
Card Capture Bot — Business card OCR via Matrix, with S3 storage + inbox review

Images dropped in #CardCapture are stored to S3, extracted via Claude vision,
and held in an inbox collection for review before committing to people.

When VAULT_ROLE_ID_FILE is set (Vault AppRole mode), all secrets are fetched
from Vault at startup instead of being baked into the systemd environment:
    VAULT_ADDR           — Vault address
    VAULT_ROLE_ID_FILE   — path to role-id credential file
    VAULT_SECRET_ID_FILE — path to secret-id credential file

Vault paths (KV v2, all under kv/data/):
    hosts/<bot_host>/card-capture/mongodb       → uri (BRAIN2_MONGODB_URI)
    hosts/<bot_host>/card-capture/rustfs        → access_key, secret_key
    hosts/mylab/anthropic                       → api_key (shared key, not duplicated)
    hosts/<matrix_host>/synapse/bots/card-capture → password (used for Matrix login)

Legacy environment variables (used when VAULT_ROLE_ID_FILE is not set):
    MATRIX_CARD_CAPTURE_TOKEN  - Bot access token (static, baked at deploy time)
    ANTHROPIC_API_KEY          - Anthropic API key
    BRAIN2_MONGODB_URI         - MongoDB URI
    S3_ACCESS_KEY / S3_SECRET_KEY

Non-secret config (always from environment, never from Vault):
    MATRIX_HOMESERVER_URL, MATRIX_ROOM_ID, MATRIX_BOT_USER_ID,
    BRAIN2_MONGODB_DB, MATRIX_ALLOWED_USERS, S3_ENDPOINT_URL, S3_BUCKET
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from nio import AsyncClient, RoomMessage, RoomMessageImage, RoomMessageText, MatrixRoom, SyncError
except ImportError:
    print("Error: matrix-nio not installed", file=sys.stderr)
    sys.exit(1)

try:
    from anthropic import AsyncAnthropic
except ImportError:
    print("Error: anthropic not installed", file=sys.stderr)
    sys.exit(1)

try:
    from bson import ObjectId
except ImportError:
    print("Error: pymongo not installed", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
import bot_common

get_db = bot_common.get_db
get_s3 = bot_common.get_s3
ensure_bucket = bot_common.ensure_bucket

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    stream=sys.stderr,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ALLOWED_USERS = bot_common.allowed_users()

VISION_MODEL        = "claude-sonnet-4-6"
MIN_CONFIDENCE      = 0.4
DEFAULT_EVENT_HOURS = 4
BOT_START_MS        = int(time.time() * 1000)

S3_ENDPOINT_URL = os.getenv('S3_ENDPOINT_URL', 'http://localhost:9000')
S3_BUCKET       = os.getenv('S3_BUCKET', 'card-captures')

_processed_events: set[str] = set()

stats = {
    "cards":        0,
    "stored":       0,
    "committed":    0,
    "api_calls":    0,
    "input_tokens":  0,
    "output_tokens": 0,
}

CARD_EXTRACTION_PROMPT = """Extract all contact information from this business card image.

Return JSON only — no prose:
{
  "name": "",
  "title": "",
  "company": "",
  "email": "",
  "phone": "",
  "address": "",
  "website": "",
  "social": [],
  "confidence": 0.0,
  "missing_fields": [],
  "notes": ""
}

confidence:
  1.0  All key fields clearly readable
  0.8  Most fields readable, minor uncertainty
  0.6  Name/company readable, others uncertain
  0.4  Partial: significant portions unreadable
  0.2  Very little readable
  0.0  Cannot extract anything useful

missing_fields: field names visible on card but unreadable.
social: strings like "linkedin.com/in/handle" or "@twitter_handle".
notes: dual-language, handwritten additions, unusual layout, etc."""

# ── S3 ────────────────────────────────────────────────────────────────────────
# get_s3() / ensure_bucket() now come from bot_common (aliased above).


def upload_card_image(image_bytes: bytes, media_type: str, bucket: str) -> tuple[ObjectId, str]:
    """Upload image to S3. Returns (ObjectId, key). Raises on failure."""
    ext_map = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}
    ext = ext_map.get(media_type, "jpg")
    oid = ObjectId()
    now = datetime.now(timezone.utc)
    key = f"{now.year}/{now.month:02d}/{str(oid)}.{ext}"
    get_s3().put_object(Bucket=bucket, Key=key, Body=image_bytes, ContentType=media_type)
    logger.info(f"S3 upload: {bucket}/{key} ({len(image_bytes)} bytes)")
    return oid, key


def fetch_s3_image(bucket: str, key: str) -> tuple[bytes, str]:
    """Fetch image bytes from S3. Returns (bytes, media_type)."""
    ext = key.rsplit(".", 1)[-1] if "." in key else "jpg"
    mt_map = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}
    media_type = mt_map.get(ext, "image/jpeg")
    response = get_s3().get_object(Bucket=bucket, Key=key)
    return response["Body"].read(), media_type


# ── MongoDB ───────────────────────────────────────────────────────────────────
# get_db() now comes from bot_common (aliased above).


def _now():
    return datetime.now(timezone.utc).isoformat()


# ── Event context (persisted in bot_state collection) ─────────────────────────

def get_event_context() -> dict | None:
    db = get_db()
    if db is None:
        return None
    doc = db.bot_state.find_one({"key": "event_context"})
    if not doc:
        return None
    expires_at = doc.get("expires_at")
    if expires_at and datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
        db.bot_state.delete_one({"key": "event_context"})
        logger.info(f"Event context expired: {doc.get('event_name')}")
        return None
    return doc


def set_event_context(name: str, hours: float) -> dict:
    now        = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=hours)).isoformat()
    doc = {
        "key":        "event_context",
        "event_name": name,
        "set_at":     now.isoformat(),
        "expires_at": expires_at,
        "hours":      hours,
    }
    db = get_db()
    if db is not None:
        db.bot_state.replace_one({"key": "event_context"}, doc, upsert=True)
    logger.info(f"Event context set: {name} ({hours}h)")
    return doc


def clear_event_context():
    db = get_db()
    if db is not None:
        db.bot_state.delete_one({"key": "event_context"})
    logger.info("Event context cleared")


def format_time_remaining(expires_at_iso: str) -> str:
    delta = datetime.fromisoformat(expires_at_iso) - datetime.now(timezone.utc)
    total = int(delta.total_seconds())
    if total <= 0:
        return "expired"
    h, m = divmod(total // 60, 60)
    return f"{h}h {m}m remaining" if h else f"{m}m remaining"


# ── Inbox operations ──────────────────────────────────────────────────────────

def get_last_pending(db, sender: str) -> dict | None:
    return db.inbox.find_one(
        {"sender": sender, "status": "pending"},
        sort=[("captured_at", -1)]
    )


def write_inbox(db, oid: ObjectId, extracted: dict, mxc_url: str,
                bucket: str, key: str, sender: str, event_name: str = "") -> None:
    doc = {
        "_id":                 oid,
        "status":              "pending",
        "extracted":           extracted,
        "confidence":          extracted.get("confidence", 0.0),
        "card_image_mxc":      mxc_url,
        "card_image_bucket":   bucket,
        "card_image_key":      key,
        "captured_at":         _now(),
        "committed_at":        None,
        "meet_note":           "",
        "sender":              sender,
        "event_name":          event_name,
        "extraction_attempts": 1,
    }
    db.inbox.insert_one(doc)


def commit_inbox(db, inbox_doc: dict, meet_note: str = "") -> str:
    """Move inbox record to people. Returns inserted people doc ID."""
    extracted  = inbox_doc.get("extracted", {})
    event_name = inbox_doc.get("event_name", "")
    mxc_url    = inbox_doc.get("card_image_mxc", "")

    contacts = []
    if extracted.get("email"):
        contacts.append({"type": "email",   "value": extracted["email"]})
    if extracted.get("phone"):
        contacts.append({"type": "phone",   "value": extracted["phone"]})
    if extracted.get("website"):
        contacts.append({"type": "website", "value": extracted["website"]})
    for s in extracted.get("social", []):
        contacts.append({"type": "social",  "value": s})

    people_doc = {
        "name":              extracted.get("name", ""),
        "title":             extracted.get("title", ""),
        "company":           extracted.get("company", ""),
        "tags":              ["business-card"],
        "notes":             extracted.get("notes", ""),
        "last_contact":      "",
        "projects":          [],
        "contacts":          contacts,
        "address":           extracted.get("address", ""),
        "source":            "card-capture",
        "card_image_mxc":    mxc_url,
        "card_image_bucket": inbox_doc["card_image_bucket"],
        "card_image_key":    inbox_doc["card_image_key"],
        "created_at":        _now(),
        "updated_at":        _now(),
    }
    if event_name:
        people_doc["event_name"] = event_name
    if meet_note:
        people_doc["meet_note"] = meet_note

    result = db.people.insert_one(people_doc)

    db.inbox.update_one(
        {"_id": inbox_doc["_id"]},
        {"$set": {"status": "committed", "committed_at": _now(), "meet_note": meet_note}}
    )

    db.replay_log.insert_one({
        "replay_id":             str(uuid.uuid4()),
        "timestamp":             _now(),
        "source":                "card-capture",
        "raw_input":             f"[card] {mxc_url}",
        "actual_classification": "people",
        "stored_collection":     "people",
        "confidence":            inbox_doc.get("confidence", 0.0),
        "model":                 VISION_MODEL,
        "human_verified":        True,
        "passed":                True,
    })

    return str(result.inserted_id)


# ── Vision extraction ─────────────────────────────────────────────────────────

async def extract_card(image_bytes: bytes, media_type: str, api_key: str) -> dict:
    aclient = AsyncAnthropic(api_key=api_key)
    b64 = base64.standard_b64encode(image_bytes).decode()
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"
    try:
        response = await aclient.messages.create(
            model=VISION_MODEL,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type":       "base64",
                            "media_type": media_type,
                            "data":       b64,
                        }
                    },
                    {"type": "text", "text": CARD_EXTRACTION_PROMPT}
                ]
            }]
        )
        stats["api_calls"]    += 1
        stats["input_tokens"]  += response.usage.input_tokens
        stats["output_tokens"] += response.usage.output_tokens

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Vision returned invalid JSON: {e}")
        return {"confidence": 0.0, "missing_fields": [], "notes": f"Parse error: {e}"}
    except Exception as e:
        logger.error(f"Vision API error: {e}")
        raise


# ── Reply formatting ──────────────────────────────────────────────────────────

def _card_headline(extracted: dict) -> str:
    name    = extracted.get("name", "")
    title   = extracted.get("title", "")
    company = extracted.get("company", "")
    parts   = [p for p in [name, title, f"@ {company}" if company else ""] if p]
    return ", ".join(parts) or "Unknown"


def format_inbox_reply(extracted: dict, inbox_id: str, attempt: int = 1) -> str:
    headline = _card_headline(extracted)
    conf     = extracted.get("confidence", 0.0)

    lines = [f"**Inbox** — {headline}"]

    detail = []
    if extracted.get("email"):
        detail.append(f"email: {extracted['email']}")
    if extracted.get("phone"):
        detail.append(f"phone: {extracted['phone']}")
    if detail:
        lines.append("  " + "  |  ".join(detail))
    if extracted.get("website"):
        lines.append(f"  website: {extracted['website']}")
    if extracted.get("address"):
        lines.append(f"  address: {extracted['address']}")
    for s in extracted.get("social", []):
        lines.append(f"  social: {s}")

    attempt_str = f"  (attempt {attempt})" if attempt > 1 else ""
    lines.append(f"  confidence: {conf:.0%}{attempt_str}  |  image saved")

    for f in extracted.get("missing_fields", []):
        lines.append(f"  ⚠️ {f} not readable")
    if extracted.get("notes"):
        lines.append(f"  _{extracted['notes']}_")

    lines.append("")
    lines.append("/commit  to save  ·  /reextract  if fields are wrong")
    return "\n".join(lines)


def format_committed_reply(extracted: dict, people_id: str, meet_note: str = "") -> str:
    headline = _card_headline(extracted)
    lines = [f"**Committed** — {headline} → people"]
    if meet_note:
        lines.append(f"  note: {meet_note}")
    lines.append(f"  ID: `{people_id}`")
    return "\n".join(lines)


def format_discarded_reply(extracted: dict) -> str:
    headline = _card_headline(extracted)
    return f"**Discarded** — {headline}\n  Image retained in S3 for audit."


# ── Matrix helpers ────────────────────────────────────────────────────────────

async def send_reply(room: MatrixRoom, message: str, client: AsyncClient):
    try:
        await client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message}
        )
    except Exception as e:
        logger.error(f"Failed to send reply: {e}")


# ── Image callback ────────────────────────────────────────────────────────────

async def image_callback(room: MatrixRoom, event: RoomMessageImage,
                         client: AsyncClient, bot_user_id: str, api_key: str):
    if event.sender == bot_user_id:
        return
    if event.server_timestamp < BOT_START_MS:
        return
    if event.event_id in _processed_events:
        return
    _processed_events.add(event.event_id)

    if ALLOWED_USERS and event.sender not in ALLOWED_USERS:
        logger.warning(f"Unauthorized image from: {event.sender}")
        return

    mxc_url    = event.url
    event_ctx  = get_event_context()
    event_name = event_ctx["event_name"] if event_ctx else ""

    logger.info(f"Card image from {event.sender}: {mxc_url}" +
                (f" [event: {event_name}]" if event_name else ""))
    stats["cards"] += 1

    await send_reply(room, "Processing card...", client)

    # Download from Matrix homeserver
    try:
        resp = await client.download(mxc=mxc_url)
        if not hasattr(resp, 'body') or not resp.body:
            await send_reply(room, "⚠️ Could not download image from homeserver.", client)
            return
        image_bytes = resp.body
        media_type  = getattr(resp, 'content_type', 'image/jpeg') or 'image/jpeg'
    except Exception as e:
        logger.error(f"Image download failed: {e}")
        await send_reply(room, f"⚠️ Download failed: {e}", client)
        return

    # Upload to S3 — fail hard if S3 is unavailable
    try:
        oid, s3_key = upload_card_image(image_bytes, media_type, S3_BUCKET)
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        await send_reply(room, f"⚠️ S3 upload failed — card not saved: {e}", client)
        return

    if not api_key:
        await send_reply(room, "⚠️ ANTHROPIC_API_KEY not configured.", client)
        return

    # Vision extraction
    try:
        extracted = await extract_card(image_bytes, media_type, api_key)
    except Exception as e:
        await send_reply(room, f"⚠️ Vision API error: {e}", client)
        return

    confidence = extracted.get("confidence", 0.0)
    logger.info(f"Extraction confidence: {confidence:.2f}")

    # Write to inbox regardless of confidence
    db = get_db()
    if db is None:
        await send_reply(room, "⚠️ MongoDB unavailable — card not saved.", client)
        return

    write_inbox(db, oid, extracted, mxc_url, S3_BUCKET, s3_key, event.sender, event_name)
    stats["stored"] += 1

    inbox_id = str(oid)
    reply    = format_inbox_reply(extracted, inbox_id)
    logger.info(f"Inbox write: {inbox_id} (confidence={confidence:.2f}, sender={event.sender})")
    await send_reply(room, reply, client)


# ── Text callback ─────────────────────────────────────────────────────────────

async def text_callback(room: MatrixRoom, event: RoomMessage,
                        client: AsyncClient, bot_user_id: str, api_key: str):
    if event.sender == bot_user_id:
        return
    if event.server_timestamp < BOT_START_MS:
        return
    if not hasattr(event, 'body') or not event.body:
        return
    if not isinstance(event, RoomMessageText):
        return

    body = event.body.strip()
    if not body.startswith('/'):
        return

    if ALLOWED_USERS and event.sender not in ALLOWED_USERS:
        logger.warning(f"Unauthorized command from: {event.sender}")
        return

    parts = body.lstrip('/').split()
    cmd   = parts[0].lower() if parts else ""
    args  = parts[1:]

    if cmd == "help":
        await send_reply(room, """\
**Card Capture Bot**

Drop any business card photo — no command needed.
Image is saved to S3, extracted, and held for review.

  /commit [note]         — save last card to people (optional note)
  /reextract             — re-run vision on last card's S3 image
  /discard               — discard last card (image kept for audit)
  /pending               — show all cards waiting in inbox
  /event <name> [hours]  — tag scans with an event name (default 4h)
  /event                 — show current event and time remaining
  /event clear           — cancel current event
  /status                — MongoDB + S3 + session stats
  /help                  — this message""", client)

    elif cmd == "commit":
        db = get_db()
        if db is None:
            await send_reply(room, "⚠️ MongoDB unavailable.", client)
            return
        inbox_doc = get_last_pending(db, event.sender)
        if inbox_doc is None:
            await send_reply(room, "No pending card to commit. Drop a card image first.", client)
            return
        meet_note = " ".join(args)
        try:
            people_id = commit_inbox(db, inbox_doc, meet_note)
            stats["committed"] += 1
            reply = format_committed_reply(inbox_doc.get("extracted", {}), people_id, meet_note)
            logger.info(f"Committed inbox {inbox_doc['_id']} → people {people_id}")
        except Exception as e:
            logger.error(f"Commit failed: {e}")
            await send_reply(room, f"⚠️ Commit failed: {e}", client)
            return
        await send_reply(room, reply, client)

    elif cmd == "reextract":
        db = get_db()
        if db is None:
            await send_reply(room, "⚠️ MongoDB unavailable.", client)
            return
        inbox_doc = get_last_pending(db, event.sender)
        if inbox_doc is None:
            await send_reply(room, "No pending card to re-extract.", client)
            return
        if not api_key:
            await send_reply(room, "⚠️ ANTHROPIC_API_KEY not configured.", client)
            return

        await send_reply(room, "Re-extracting from S3 image...", client)

        try:
            image_bytes, media_type = fetch_s3_image(
                inbox_doc["card_image_bucket"], inbox_doc["card_image_key"]
            )
            extracted = await extract_card(image_bytes, media_type, api_key)
        except Exception as e:
            logger.error(f"Re-extraction failed: {e}")
            await send_reply(room, f"⚠️ Re-extraction failed: {e}", client)
            return

        attempt = inbox_doc.get("extraction_attempts", 1) + 1
        db.inbox.update_one(
            {"_id": inbox_doc["_id"]},
            {"$set": {
                "extracted":           extracted,
                "confidence":          extracted.get("confidence", 0.0),
                "extraction_attempts": attempt,
            }}
        )
        reply = format_inbox_reply(extracted, str(inbox_doc["_id"]), attempt)
        logger.info(f"Re-extracted inbox {inbox_doc['_id']} (attempt {attempt})")
        await send_reply(room, reply, client)

    elif cmd == "discard":
        db = get_db()
        if db is None:
            await send_reply(room, "⚠️ MongoDB unavailable.", client)
            return
        inbox_doc = get_last_pending(db, event.sender)
        if inbox_doc is None:
            await send_reply(room, "No pending card to discard.", client)
            return
        db.inbox.update_one(
            {"_id": inbox_doc["_id"]},
            {"$set": {"status": "discarded"}}
        )
        reply = format_discarded_reply(inbox_doc.get("extracted", {}))
        logger.info(f"Discarded inbox {inbox_doc['_id']}")
        await send_reply(room, reply, client)

    elif cmd == "pending":
        db = get_db()
        if db is None:
            await send_reply(room, "⚠️ MongoDB unavailable.", client)
            return
        pending = list(db.inbox.find(
            {"status": "pending"},
            sort=[("captured_at", -1)]
        ).limit(20))
        if not pending:
            await send_reply(room, "**Pending** — inbox is empty.", client)
            return
        lines = [f"**Pending** — {len(pending)} card(s) waiting"]
        for i, doc in enumerate(pending, 1):
            ext      = doc.get("extracted", {})
            headline = _card_headline(ext)
            sender   = doc.get("sender", "?")
            conf     = doc.get("confidence", 0.0)
            lines.append(f"  {i}. {headline}  ({sender}, {conf:.0%})")
        await send_reply(room, "\n".join(lines), client)

    elif cmd == "event":
        if not args:
            ctx = get_event_context()
            if ctx:
                remaining = format_time_remaining(ctx["expires_at"])
                await send_reply(room,
                    f"**Event:** {ctx['event_name']}  ({remaining})\n"
                    f"Set at: {ctx['set_at'][:16].replace('T', ' ')} UTC\n"
                    f"/event clear to cancel", client)
            else:
                await send_reply(room,
                    "No event set.\n/event <name> [hours] to tag scans.", client)
        elif args[0].lower() == "clear":
            clear_event_context()
            await send_reply(room, "Event cleared — scans will not be tagged.", client)
        else:
            hours      = DEFAULT_EVENT_HOURS
            name_parts = args
            last       = args[-1].rstrip('h')
            if last.replace('.', '', 1).isdigit():
                hours      = float(last)
                name_parts = args[:-1]
            name = " ".join(name_parts)
            if not name:
                await send_reply(room, "Usage: /event <name> [hours]", client)
                return
            set_event_context(name, hours)
            await send_reply(room,
                f"**Event set:** {name}\n"
                f"All card scans tagged for the next {hours:.0f}h.\n"
                f"/event clear to cancel early.", client)

    elif cmd == "status":
        db   = get_db()
        inp  = stats["input_tokens"]
        out  = stats["output_tokens"]
        cost = bot_common.estimate_cost(VISION_MODEL, inp, out)
        ctx  = get_event_context()

        mongo_ok = db is not None
        s3_ok    = False
        try:
            get_s3().head_bucket(Bucket=S3_BUCKET)
            s3_ok = True
        except Exception:
            pass

        pending_count = 0
        if mongo_ok:
            try:
                pending_count = db.inbox.count_documents({"status": "pending"})
            except Exception:
                pass

        event_line = (f"\n**Event:** {ctx['event_name']}  ({format_time_remaining(ctx['expires_at'])})"
                      if ctx else "")

        await send_reply(room, f"""\
**Card Capture Bot — Status**

**MongoDB:** {'connected' if mongo_ok else 'unavailable'}
**S3:** {'connected' if s3_ok else 'unavailable'}  ({S3_ENDPOINT_URL}/{S3_BUCKET})
**Inbox:** {pending_count} pending
**Session:** {stats['cards']} cards · {stats['stored']} in inbox · {stats['committed']} committed
**API:** {stats['api_calls']} calls · {inp:,} in / {out:,} out · ${cost:.4f}
**Model:** {VISION_MODEL}{event_line}""", client)

    else:
        await send_reply(room, f"Unknown command `/{cmd}` — try `/help`", client)


# ── Secrets ───────────────────────────────────────────────────────────────────

def load_vault_credentials(homeserver_url: str, bot_user_id: str) -> None:
    """
    Authenticate via Vault AppRole and inject all secrets into os.environ.
    Called only when VAULT_ROLE_ID_FILE is set. Exits on any failure.

    After this function returns, the following env vars are populated:
        MATRIX_CARD_CAPTURE_TOKEN  — freshly obtained from Matrix login
        ANTHROPIC_API_KEY
        BRAIN2_MONGODB_URI
        S3_ACCESS_KEY, S3_SECRET_KEY

    All other code (get_db, get_s3, load_token, load_api_key) reads from
    os.environ unchanged — no other callers need modification.
    """
    logger.info("Vault AppRole mode — fetching credentials from Vault")

    try:
        vault_token = bot_common.vault_approle_auth()
    except Exception as e:
        logger.error(f"Vault AppRole auth failed: {e}")
        sys.exit(1)

    vault_addr = os.getenv('VAULT_ADDR', '').rstrip('/')

    # Determine Vault path prefixes from bot user ID and homeserver
    # MATRIX_BOT_USER_ID = @card-capture:jackaltx.com → matrix_host = jackaltx.com
    # MATRIX_HOMESERVER_URL is separate from the Vault host key (bot_host is in systemd Env)
    # We use a convention: env var VAULT_BOT_HOST + VAULT_MATRIX_HOST set by systemd template,
    # OR fall back to parsing from the inventory-set env vars.
    bot_host    = os.getenv('VAULT_BOT_HOST', 'bot-test')
    matrix_host = os.getenv('VAULT_MATRIX_HOST', 'matrix-web')

    def _read(path: str) -> dict:
        try:
            return bot_common.vault_kv_read(vault_token, path, vault_addr)
        except Exception as e:
            logger.error(f"Vault read failed — {path}: {e}")
            sys.exit(1)

    # MongoDB
    mongo = _read(f"hosts/{bot_host}/card-capture/mongodb")
    os.environ['BRAIN2_MONGODB_URI'] = mongo['uri']
    logger.info("Vault: MongoDB URI loaded")

    # S3 / rustfs
    s3 = _read(f"hosts/{bot_host}/card-capture/rustfs")
    os.environ['S3_ACCESS_KEY'] = s3['access_key']
    os.environ['S3_SECRET_KEY'] = s3['secret_key']
    logger.info("Vault: S3 credentials loaded")

    # Anthropic — shared key at mylab level, not duplicated under the bot path
    anthropic_host = os.getenv('VAULT_ANTHROPIC_HOST', 'mylab')
    anthropic = _read(f"hosts/{anthropic_host}/anthropic")
    os.environ['ANTHROPIC_API_KEY'] = anthropic['api_key']
    logger.info("Vault: Anthropic API key loaded")

    # Matrix — login with password to get a fresh access token
    matrix = _read(f"hosts/{matrix_host}/synapse/bots/card-capture")
    matrix_password = matrix['password']
    try:
        access_token = bot_common.matrix_login(homeserver_url, bot_user_id, matrix_password)
    except Exception as e:
        logger.error(f"Matrix login failed: {e}")
        sys.exit(1)
    os.environ['MATRIX_CARD_CAPTURE_TOKEN'] = access_token
    logger.info("Vault: Matrix token obtained via login")


def load_token():
    token = os.getenv('MATRIX_CARD_CAPTURE_TOKEN')
    if token:
        return token
    secrets_file = Path.home() / '.secrets/LabMatrix'
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            if line.startswith('export MATRIX_CARD_CAPTURE_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"\'')
    logger.error("MATRIX_CARD_CAPTURE_TOKEN not found")
    return None


def load_api_key():
    key = os.getenv('ANTHROPIC_API_KEY')
    if key:
        return key
    secrets_file = Path.home() / '.secrets/LabMatrix'
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            if line.startswith('export ANTHROPIC_API_KEY='):
                return line.split('=', 1)[1].strip().strip('"\'')
    logger.error("ANTHROPIC_API_KEY not found")
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    homeserver_url = os.getenv('MATRIX_HOMESERVER_URL', '')
    room_id        = os.getenv('MATRIX_ROOM_ID', '')
    bot_user_id    = os.getenv('MATRIX_BOT_USER_ID', '')

    if not homeserver_url or not room_id or not bot_user_id:
        logger.error("MATRIX_HOMESERVER_URL, MATRIX_ROOM_ID, MATRIX_BOT_USER_ID are required")
        sys.exit(1)

    # Vault AppRole mode: fetch all secrets before any other initialization
    if os.getenv('VAULT_ROLE_ID_FILE'):
        load_vault_credentials(homeserver_url, bot_user_id)

    token   = load_token()
    api_key = load_api_key()

    if not token:
        logger.error("No Matrix token — cannot start")
        sys.exit(1)

    db  = get_db()
    ctx = get_event_context()

    # Ensure S3 bucket exists before accepting cards
    try:
        ensure_bucket(S3_BUCKET)
        s3_status = f"✓ ({S3_ENDPOINT_URL}/{S3_BUCKET})"
    except Exception as e:
        s3_status = f"✗ {e}"
        logger.error(f"S3 bucket check failed: {e}")

    print("=" * 72)
    print("Card Capture Bot")
    print(f"  Homeserver     : {homeserver_url}")
    print(f"  Bot user       : {bot_user_id}")
    print(f"  Room           : {room_id}")
    print(f"  MongoDB        : {'connected' if db is not None else 'UNAVAILABLE'}")
    print(f"  S3             : {s3_status}")
    print(f"  API key        : {'✓' if api_key else '✗ missing'}")
    print(f"  Min confidence : {MIN_CONFIDENCE:.0%}")
    if ctx:
        print(f"  Event          : {ctx['event_name']} ({format_time_remaining(ctx['expires_at'])})")
    print("=" * 72)

    client = AsyncClient(homeserver_url, bot_user_id)
    client.access_token = token

    async def _image_cb(room, event):
        await image_callback(room, event, client, bot_user_id, api_key)

    async def _text_cb(room, event):
        await text_callback(room, event, client, bot_user_id, api_key)

    client.add_event_callback(_image_cb, RoomMessageImage)
    client.add_event_callback(_text_cb, RoomMessage)

    try:
        sync = await client.sync(timeout=30000)
        if isinstance(sync, SyncError):
            logger.error(f"Initial sync failed: {sync.message}")
            return
        logger.info("Initial sync complete")

        room_resp = await client.room_resolve_alias(room_id)
        if hasattr(room_resp, 'room_id'):
            resolved = room_resp.room_id
            join = await client.join(resolved)
            if hasattr(join, 'room_id'):
                logger.info(f"Joined {join.room_id}")

        logger.info("Listening for card images…")
        await client.sync_forever(timeout=30000, full_state=False)

    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
