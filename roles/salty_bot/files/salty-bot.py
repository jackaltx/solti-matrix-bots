#!/usr/bin/env python3
"""
Salty Bot — Voice-friendly Matrix capture bot for Second Brain

Trigger word is "salty" (case-insensitive) as the first word of any message.
Designed for voice-to-text: no slash commands, natural language triggers.

  salty <anything>   — open a capture session (or start fresh if one is active)
  salty done         — save session to second_brain.ideas
  salty cancel       — discard session without saving
  salty status       — show current mode and session contents

While a session is open, any message (text, image, video) is accumulated.
Sessions autosave after SALTY_AUTOSAVE_MINUTES of inactivity (default: 10).

Environment variables:
    MATRIX_SALTY_TOKEN      — Bot access token
    MATRIX_HOMESERVER_URL   — Homeserver URL
    MATRIX_ROOM_ID          — Room ID or alias
    MATRIX_BOT_USER_ID      — Bot Matrix user ID (@salty:domain)
    ANTHROPIC_API_KEY       — Anthropic API key
    BRAIN2_MONGODB_URI      — MongoDB URI
    BRAIN2_MONGODB_DB       — Database name (default: second_brain)
    MATRIX_ALLOWED_USERS    — Comma-separated allowed sender IDs
    S3_ENDPOINT_URL         — MinIO/S3 endpoint
    S3_ACCESS_KEY           — S3 access key ID
    S3_SECRET_KEY           — S3 secret access key
    S3_BUCKET               — Bucket name (default: salty-captures)
    SALTY_AUTOSAVE_MINUTES  — Inactivity timeout (default: 10)
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from nio import (
        AsyncClient, MatrixRoom,
        RoomMessageAudio, RoomMessageFile, RoomMessageImage,
        RoomMessageText, RoomMessageVideo,
        SyncError,
    )
except ImportError:
    print("Error: matrix-nio not installed", file=sys.stderr)
    sys.exit(1)

try:
    from anthropic import AsyncAnthropic
except ImportError:
    print("Error: anthropic not installed", file=sys.stderr)
    sys.exit(1)

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
    from bson import ObjectId
except ImportError:
    print("Error: pymongo not installed", file=sys.stderr)
    sys.exit(1)

try:
    import boto3
    from botocore.exceptions import ClientError as S3ClientError
except ImportError:
    print("Error: boto3 not installed", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    stream=sys.stderr,
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ALLOWED_USERS = [
    u.strip()
    for u in os.getenv('MATRIX_ALLOWED_USERS', '').split(',')
    if u.strip()
]

AUTOSAVE_MINUTES = int(os.getenv('SALTY_AUTOSAVE_MINUTES', '10'))
VISION_MODEL     = "claude-sonnet-4-6"
TEXT_MODEL       = "claude-haiku-4-5-20251001"
BOT_START_MS     = int(time.time() * 1000)

S3_ENDPOINT_URL = os.getenv('S3_ENDPOINT_URL', 'http://localhost:9000')
S3_BUCKET       = os.getenv('S3_BUCKET', 'salty-captures')

_processed_events: set[str] = set()

stats = {
    "sessions_started":   0,
    "sessions_saved":     0,
    "sessions_autosaved": 0,
    "sessions_cancelled": 0,
    "images_captured":    0,
    "videos_captured":    0,
    "api_calls":          0,
    "input_tokens":       0,
    "output_tokens":      0,
}

# ── Session state ─────────────────────────────────────────────────────────────

@dataclass
class CaptureSession:
    sender: str
    intent: str                    # text after "salty" that opened the session
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    text_parts: list[str] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    # each attachment: {type, mxc_url, s3_bucket, s3_key, description}

    def age_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.started_at).total_seconds() / 60

    def idle_minutes(self) -> float:
        return (datetime.now(timezone.utc) - self.last_activity).total_seconds() / 60

    def autosave_in_minutes(self) -> float:
        return max(0.0, AUTOSAVE_MINUTES - self.idle_minutes())

    def is_empty(self) -> bool:
        return not self.text_parts and not self.attachments

    def content_summary(self) -> str:
        parts = []
        if self.text_parts:
            n = len(self.text_parts)
            parts.append(f"{n} text message{'s' if n != 1 else ''}")
        imgs  = sum(1 for a in self.attachments if a["type"] == "image")
        vids  = sum(1 for a in self.attachments if a["type"] == "video")
        auds  = sum(1 for a in self.attachments if a["type"] == "audio")
        files = sum(1 for a in self.attachments if a["type"] == "file")
        if imgs:
            parts.append(f"{imgs} image{'s' if imgs != 1 else ''}")
        if vids:
            parts.append(f"{vids} video{'s' if vids != 1 else ''}")
        if auds:
            parts.append(f"{auds} voice note{'s' if auds != 1 else ''}")
        if files:
            parts.append(f"{files} file{'s' if files != 1 else ''}")
        return " · ".join(parts) if parts else "nothing yet"


# Active sessions keyed by sender Matrix ID
sessions: dict[str, CaptureSession] = {}

# ── S3 ────────────────────────────────────────────────────────────────────────

_s3_client = None


def get_s3():
    global _s3_client
    if _s3_client is not None:
        return _s3_client
    _s3_client = boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=os.getenv('S3_ACCESS_KEY'),
        aws_secret_access_key=os.getenv('S3_SECRET_KEY'),
    )
    return _s3_client


def ensure_bucket(bucket: str):
    s3 = get_s3()
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info(f"S3 bucket exists: {bucket}")
    except S3ClientError:
        s3.create_bucket(Bucket=bucket)
        logger.info(f"S3 bucket created: {bucket}")


def upload_to_s3(data: bytes, media_type: str, bucket: str, subdir: str) -> tuple[ObjectId, str]:
    ext_map = {
        "image/jpeg": "jpg", "image/png": "png",
        "image/webp": "webp", "image/gif": "gif",
        "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    }
    ext = ext_map.get(media_type, "bin")
    oid = ObjectId()
    now = datetime.now(timezone.utc)
    key = f"{subdir}/{now.year}/{now.month:02d}/{oid}.{ext}"
    get_s3().put_object(Bucket=bucket, Key=key, Body=data, ContentType=media_type)
    logger.info(f"S3 upload: {bucket}/{key} ({len(data):,} bytes)")
    return oid, key


# ── MongoDB ───────────────────────────────────────────────────────────────────

_mongo_client = None
_db           = None


def get_db():
    global _mongo_client, _db
    if _db is not None:
        return _db
    uri     = os.getenv('BRAIN2_MONGODB_URI', 'mongodb://localhost:27017')
    db_name = os.getenv('BRAIN2_MONGODB_DB', 'second_brain')
    try:
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command('ping')
        _db = _mongo_client[db_name]
        logger.info(f"MongoDB connected: {db_name}")
        return _db
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logger.error(f"MongoDB connection failed: {e}")
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_idea(session: CaptureSession, title: str, cleaned_text: str,
               tags: list[str], autosaved: bool) -> str | None:
    db = get_db()
    if db is None:
        return None
    doc = {
        "title":        title,
        "intent":       session.intent,
        "text_parts":   session.text_parts,
        "cleaned_text": cleaned_text,
        "attachments":  session.attachments,
        "tags":         tags,
        "sender":       session.sender,
        "captured_at":  session.started_at.isoformat(),
        "saved_at":     _now_iso(),
        "autosaved":    autosaved,
        "source":       "salty-bot",
    }
    result = db.ideas.insert_one(doc)
    logger.info(f"Idea saved: {result.inserted_id} title={title!r}")
    return str(result.inserted_id)


# ── Claude ────────────────────────────────────────────────────────────────────

async def describe_image(image_bytes: bytes, media_type: str, api_key: str) -> str:
    if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        media_type = "image/jpeg"
    b64 = base64.standard_b64encode(image_bytes).decode()
    aclient = AsyncAnthropic(api_key=api_key)
    try:
        resp = await aclient.messages.create(
            model=VISION_MODEL,
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64},
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe what you see in 2-3 sentences for a personal note. "
                            "Focus on content — text, diagrams, objects, people — not aesthetics."
                        ),
                    },
                ],
            }],
        )
        stats["api_calls"]    += 1
        stats["input_tokens"]  += resp.usage.input_tokens
        stats["output_tokens"] += resp.usage.output_tokens
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Image description failed: {e}")
        return "[image — description unavailable]"


async def process_session(session: CaptureSession, api_key: str) -> tuple[str, str, list[str]]:
    """Clean text, generate title, extract tags. Returns (title, cleaned_text, tags)."""
    if session.is_empty():
        return "Empty note", "", []

    if not api_key:
        title = (session.intent[:50] + "…") if len(session.intent) > 50 else session.intent or "Note"
        return title, "\n".join(session.text_parts), []

    image_lines = [
        f"  - {a['description']}"
        for a in session.attachments
        if a["type"] == "image" and a.get("description")
    ]

    prompt_lines = []
    if session.intent:
        prompt_lines.append(f'Trigger: "{session.intent}"')
    if session.text_parts:
        prompt_lines.append("Voice messages (in order):")
        for i, t in enumerate(session.text_parts, 1):
            prompt_lines.append(f'  {i}. "{t}"')
    if image_lines:
        prompt_lines.append("Images:")
        prompt_lines.extend(image_lines)
    if sum(1 for a in session.attachments if a["type"] == "video") > 0:
        n = sum(1 for a in session.attachments if a["type"] == "video")
        prompt_lines.append(f"Videos: {n} attached (no transcription)")

    prompt = "\n".join(prompt_lines) + """

Fix voice-to-text errors and filler words, generate a short title, extract tags.

Return JSON only — no prose:
{
  "title": "4-8 word title",
  "cleaned_text": "cleaned and readable combined text",
  "tags": ["tag1", "tag2"]
}"""

    aclient = AsyncAnthropic(api_key=api_key)
    try:
        resp = await aclient.messages.create(
            model=TEXT_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        stats["api_calls"]    += 1
        stats["input_tokens"]  += resp.usage.input_tokens
        stats["output_tokens"] += resp.usage.output_tokens

        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
        data = json.loads(raw)
        return data.get("title", "Untitled"), data.get("cleaned_text", ""), data.get("tags", [])
    except Exception as e:
        logger.error(f"Session processing failed: {e}")
        title = (session.intent[:50] + "…") if len(session.intent) > 50 else session.intent or "Note"
        return title, "\n".join(session.text_parts), []


# ── Session save / cancel ─────────────────────────────────────────────────────

async def save_session(sender: str, client: AsyncClient, room_id: str,
                       api_key: str, autosaved: bool = False):
    session = sessions.pop(sender, None)
    if session is None:
        return

    if session.is_empty():
        await _send(client, room_id, "Nothing to save — session was empty.")
        return

    await _send(client, room_id, "Saving…")

    title, cleaned_text, tags = await process_session(session, api_key)
    idea_id = write_idea(session, title, cleaned_text, tags, autosaved)

    if idea_id is None:
        await _send(client, room_id, "⚠️ MongoDB unavailable — note not saved.")
        return

    tag_str  = "  " + "  ".join(f"#{t}" for t in tags) if tags else ""
    verb     = "Autosaved" if autosaved else "Saved"
    reply    = f"**{verb}** — *{title}*\n  {session.content_summary()}{tag_str}"
    await _send(client, room_id, reply)

    if autosaved:
        stats["sessions_autosaved"] += 1
    else:
        stats["sessions_saved"] += 1
    logger.info(f"Session {'autosaved' if autosaved else 'saved'} for {sender}: {idea_id}")


# ── Matrix helpers ────────────────────────────────────────────────────────────

async def _send(client: AsyncClient, room_id: str, body: str):
    try:
        await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": body},
        )
    except Exception as e:
        logger.error(f"Send failed: {e}")


def _is_allowed(sender: str) -> bool:
    return not ALLOWED_USERS or sender in ALLOWED_USERS


def _is_stale(event) -> bool:
    return event.server_timestamp < BOT_START_MS or event.event_id in _processed_events


def _mark(event):
    _processed_events.add(event.event_id)


# ── Text callback ─────────────────────────────────────────────────────────────

async def text_callback(room: MatrixRoom, event: RoomMessageText,
                        client: AsyncClient, bot_user_id: str, api_key: str):
    if event.sender == bot_user_id or _is_stale(event):
        return
    _mark(event)
    if not _is_allowed(event.sender):
        return

    body  = (event.body or "").strip()
    words = body.split()
    if not words:
        return

    # Strip trailing punctuation that voice-to-text might attach to the trigger word
    first = words[0].lower().rstrip('.,!?;:')

    if first != "salty":
        # Not a trigger — accumulate if session is active
        session = sessions.get(event.sender)
        if session is not None:
            session.text_parts.append(body)
            session.last_activity = datetime.now(timezone.utc)
            logger.info(f"Text accumulated for {event.sender}: {body[:60]!r}")
        return

    # Everything after "salty "
    rest = body[body.lower().find('salty') + 5:].strip()
    cmd  = rest.split()[0].lower().rstrip('.,!?;:') if rest else ""

    if cmd == "done":
        if sessions.get(event.sender) is None:
            await _send(client, room.room_id, "No active capture.")
        else:
            await save_session(event.sender, client, room.room_id, api_key)

    elif cmd == "cancel":
        session = sessions.pop(event.sender, None)
        if session is None:
            await _send(client, room.room_id, "Nothing to cancel.")
        else:
            stats["sessions_cancelled"] += 1
            await _send(client, room.room_id,
                        f"Cancelled — {session.content_summary()} discarded.")

    elif cmd == "status":
        session = sessions.get(event.sender)
        if session is None:
            await _send(client, room.room_id, "No active capture.")
        else:
            mins = session.autosave_in_minutes()
            mins_str = f"{mins:.0f} min" if mins >= 1 else "< 1 min"
            await _send(client, room.room_id,
                f"**Active capture** · {session.age_minutes():.0f} min old\n"
                f"  {session.content_summary()}\n"
                f"  Autosaves in ~{mins_str}")

    else:
        # New session trigger — autosave any existing open session first
        existing = sessions.get(event.sender)
        if existing and not existing.is_empty():
            logger.info(f"New trigger over active session for {event.sender} — autosaving first")
            await save_session(event.sender, client, room.room_id, api_key, autosaved=True)

        sessions[event.sender] = CaptureSession(sender=event.sender, intent=rest)
        stats["sessions_started"] += 1
        logger.info(f"Session opened for {event.sender}: {rest!r}")
        await _send(client, room.room_id,
                    "Ready — send text, photos, video, voice notes, or files. Say **salty done** to save.")


# ── Image callback ────────────────────────────────────────────────────────────

async def image_callback(room: MatrixRoom, event: RoomMessageImage,
                         client: AsyncClient, bot_user_id: str, api_key: str):
    if event.sender == bot_user_id or _is_stale(event):
        return
    _mark(event)
    if not _is_allowed(event.sender):
        return

    session = sessions.get(event.sender)
    if session is None:
        return  # no active session — ignore

    await _send(client, room.room_id, "Image received, processing…")

    try:
        resp = await client.download(mxc=event.url)
        if not hasattr(resp, 'body') or not resp.body:
            await _send(client, room.room_id, "⚠️ Could not download image.")
            return
        image_bytes = resp.body
        media_type  = getattr(resp, 'content_type', 'image/jpeg') or 'image/jpeg'
    except Exception as e:
        logger.error(f"Image download failed: {e}")
        await _send(client, room.room_id, f"⚠️ Download failed: {e}")
        return

    try:
        _, s3_key = upload_to_s3(image_bytes, media_type, S3_BUCKET, subdir="images")
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        await _send(client, room.room_id, f"⚠️ S3 upload failed: {e}")
        return

    description = await describe_image(image_bytes, media_type, api_key) if api_key else ""

    session.attachments.append({
        "type":        "image",
        "mxc_url":     event.url,
        "s3_bucket":   S3_BUCKET,
        "s3_key":      s3_key,
        "description": description,
    })
    session.last_activity = datetime.now(timezone.utc)
    stats["images_captured"] += 1

    preview = f"\n  _{description[:80]}{'…' if len(description) > 80 else ''}_" if description else ""
    await _send(client, room.room_id, f"Image saved.{preview}")
    logger.info(f"Image captured for {event.sender}: {s3_key}")


# ── Video callback ────────────────────────────────────────────────────────────

async def video_callback(room: MatrixRoom, event: RoomMessageVideo,
                         client: AsyncClient, bot_user_id: str):
    if event.sender == bot_user_id or _is_stale(event):
        return
    _mark(event)
    if not _is_allowed(event.sender):
        return

    session = sessions.get(event.sender)
    if session is None:
        return

    await _send(client, room.room_id, "Video received, uploading to storage…")

    try:
        resp = await client.download(mxc=event.url)
        if not hasattr(resp, 'body') or not resp.body:
            await _send(client, room.room_id, "⚠️ Could not download video.")
            return
        video_bytes = resp.body
        media_type  = getattr(resp, 'content_type', 'video/mp4') or 'video/mp4'
    except Exception as e:
        logger.error(f"Video download failed: {e}")
        await _send(client, room.room_id, f"⚠️ Download failed: {e}")
        return

    try:
        _, s3_key = upload_to_s3(video_bytes, media_type, S3_BUCKET, subdir="videos")
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        await _send(client, room.room_id, f"⚠️ S3 upload failed: {e}")
        return

    session.attachments.append({
        "type":        "video",
        "mxc_url":     event.url,
        "s3_bucket":   S3_BUCKET,
        "s3_key":      s3_key,
        "description": "",
    })
    session.last_activity = datetime.now(timezone.utc)
    stats["videos_captured"] += 1

    await _send(client, room.room_id, "Video saved.")
    logger.info(f"Video captured for {event.sender}: {s3_key}")


# ── File callback (PDFs, docs, etc.) ─────────────────────────────────────────

async def file_callback(room: MatrixRoom, event: RoomMessageFile,
                        client: AsyncClient, bot_user_id: str):
    if event.sender == bot_user_id or _is_stale(event):
        return
    _mark(event)
    if not _is_allowed(event.sender):
        return

    session = sessions.get(event.sender)
    if session is None:
        return

    filename = getattr(event, 'body', 'file') or 'file'
    await _send(client, room.room_id, f"File received ({filename}), uploading…")

    try:
        resp = await client.download(mxc=event.url)
        if not hasattr(resp, 'body') or not resp.body:
            await _send(client, room.room_id, "⚠️ Could not download file.")
            return
        file_bytes = resp.body
        media_type = getattr(resp, 'content_type', 'application/octet-stream') or 'application/octet-stream'
    except Exception as e:
        logger.error(f"File download failed: {e}")
        await _send(client, room.room_id, f"⚠️ Download failed: {e}")
        return

    try:
        _, s3_key = upload_to_s3(file_bytes, media_type, S3_BUCKET, subdir="files")
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        await _send(client, room.room_id, f"⚠️ S3 upload failed: {e}")
        return

    session.attachments.append({
        "type":        "file",
        "filename":    filename,
        "mxc_url":     event.url,
        "s3_bucket":   S3_BUCKET,
        "s3_key":      s3_key,
        "description": "",
    })
    session.last_activity = datetime.now(timezone.utc)

    await _send(client, room.room_id, f"File saved ({filename}).")
    logger.info(f"File captured for {event.sender}: {s3_key} ({filename})")


# ── Audio callback (voice memos) ─────────────────────────────────────────────

async def audio_callback(room: MatrixRoom, event: RoomMessageAudio,
                         client: AsyncClient, bot_user_id: str):
    if event.sender == bot_user_id or _is_stale(event):
        return
    _mark(event)
    if not _is_allowed(event.sender):
        return

    session = sessions.get(event.sender)
    if session is None:
        return

    filename = getattr(event, 'body', 'audio') or 'audio'
    await _send(client, room.room_id, f"Voice note received ({filename}), saving…")

    try:
        resp = await client.download(mxc=event.url)
        if not hasattr(resp, 'body') or not resp.body:
            await _send(client, room.room_id, "⚠️ Could not download audio.")
            return
        audio_bytes = resp.body
        media_type  = getattr(resp, 'content_type', 'audio/ogg') or 'audio/ogg'
    except Exception as e:
        logger.error(f"Audio download failed: {e}")
        await _send(client, room.room_id, f"⚠️ Download failed: {e}")
        return

    ext_map = {
        "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a",
        "audio/wav": "wav", "audio/webm": "webm", "audio/aac": "aac",
    }
    ext = ext_map.get(media_type, "ogg")
    oid = ObjectId()
    now = datetime.now(timezone.utc)
    s3_key = f"audio/{now.year}/{now.month:02d}/{oid}.{ext}"

    try:
        get_s3().put_object(Bucket=S3_BUCKET, Key=s3_key, Body=audio_bytes, ContentType=media_type)
        logger.info(f"S3 upload: {S3_BUCKET}/{s3_key} ({len(audio_bytes):,} bytes)")
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        await _send(client, room.room_id, f"⚠️ S3 upload failed: {e}")
        return

    session.attachments.append({
        "type":       "audio",
        "filename":   filename,
        "mxc_url":    event.url,
        "s3_bucket":  S3_BUCKET,
        "s3_key":     s3_key,
        "transcript": "",    # populated later if/when transcription is added
        "description": "",
    })
    session.last_activity = datetime.now(timezone.utc)

    await _send(client, room.room_id, "Voice note saved. Transcription can be added later.")
    logger.info(f"Audio captured for {event.sender}: {s3_key} ({filename})")


# ── Autosave background loop ──────────────────────────────────────────────────

async def autosave_loop(client: AsyncClient, room_id: str, api_key: str):
    while True:
        await asyncio.sleep(60)
        expired = [
            sender for sender, s in list(sessions.items())
            if s.idle_minutes() >= AUTOSAVE_MINUTES
        ]
        for sender in expired:
            logger.info(f"Autosave triggered for {sender}")
            await save_session(sender, client, room_id, api_key, autosaved=True)


# ── Secrets ───────────────────────────────────────────────────────────────────

def _load_secret(env_var: str) -> str | None:
    val = os.getenv(env_var)
    if val:
        return val
    secrets_file = Path.home() / '.secrets/LabMatrix'
    if secrets_file.exists():
        prefix = f'export {env_var}='
        for line in secrets_file.read_text().splitlines():
            if line.startswith(prefix):
                return line.split('=', 1)[1].strip().strip('"\'')
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    homeserver_url = os.getenv('MATRIX_HOMESERVER_URL', '')
    room_id        = os.getenv('MATRIX_ROOM_ID', '')
    bot_user_id    = os.getenv('MATRIX_BOT_USER_ID', '')

    if not homeserver_url or not room_id or not bot_user_id:
        logger.error("MATRIX_HOMESERVER_URL, MATRIX_ROOM_ID, MATRIX_BOT_USER_ID are required")
        sys.exit(1)

    token   = _load_secret('MATRIX_SALTY_TOKEN')
    api_key = _load_secret('ANTHROPIC_API_KEY')

    if not token:
        logger.error("MATRIX_SALTY_TOKEN not found — cannot start")
        sys.exit(1)

    db = get_db()

    try:
        ensure_bucket(S3_BUCKET)
        s3_status = f"✓ ({S3_ENDPOINT_URL}/{S3_BUCKET})"
    except Exception as e:
        s3_status = f"✗ {e}"
        logger.error(f"S3 bucket check failed: {e}")

    print("=" * 72)
    print("Salty Bot")
    print(f"  Homeserver   : {homeserver_url}")
    print(f"  Bot user     : {bot_user_id}")
    print(f"  Room         : {room_id}")
    print(f"  MongoDB      : {'connected' if db is not None else 'UNAVAILABLE'}")
    print(f"  S3           : {s3_status}")
    print(f"  API key      : {'✓' if api_key else '✗ missing (text cleanup disabled)'}")
    print(f"  Autosave     : {AUTOSAVE_MINUTES} min")
    print(f"  Allowed users: {', '.join(ALLOWED_USERS) if ALLOWED_USERS else 'all'}")
    print("=" * 72)

    client = AsyncClient(homeserver_url, bot_user_id)
    client.access_token = token

    async def _text_cb(room, event):
        await text_callback(room, event, client, bot_user_id, api_key)

    async def _image_cb(room, event):
        await image_callback(room, event, client, bot_user_id, api_key)

    async def _video_cb(room, event):
        await video_callback(room, event, client, bot_user_id)

    async def _file_cb(room, event):
        await file_callback(room, event, client, bot_user_id)

    async def _audio_cb(room, event):
        await audio_callback(room, event, client, bot_user_id)

    client.add_event_callback(_text_cb,  RoomMessageText)
    client.add_event_callback(_image_cb, RoomMessageImage)
    client.add_event_callback(_video_cb, RoomMessageVideo)
    client.add_event_callback(_file_cb,  RoomMessageFile)
    client.add_event_callback(_audio_cb, RoomMessageAudio)

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
                room_id = join.room_id
                logger.info(f"Joined {room_id}")

        asyncio.create_task(autosave_loop(client, room_id, api_key))
        logger.info(f"Listening — autosave after {AUTOSAVE_MINUTES} min idle…")
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
