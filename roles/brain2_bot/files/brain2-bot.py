#!/usr/bin/env python3
"""
Brain2 Bot — Second Brain capture via Matrix

Classifies @mentions into MongoDB collections using Claude API.
Confidence >= 0.6 → store in collection + replay_log + reply summary.
Confidence  < 0.6 → store in unclassified + replay_log + ask follow-up.

Environment Variables:
    MATRIX_SOLTI_BRAIN2_TOKEN  - Bot access token
    MATRIX_HOMESERVER_URL      - Homeserver URL
    MATRIX_ROOM_ID             - Room ID or alias
    MATRIX_BOT_USER_ID         - Bot Matrix user ID
    ANTHROPIC_API_KEY          - Anthropic API key
    BRAIN2_MONGODB_URI         - MongoDB URI (with credentials)
    BRAIN2_MONGODB_DB          - Database name (default: second_brain)
"""

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    from nio import AsyncClient, RoomMessage, MatrixRoom, SyncError
except ImportError:
    print("Error: matrix-nio not installed", file=sys.stderr)
    sys.exit(1)

try:
    from anthropic import Anthropic
except ImportError:
    print("Error: anthropic not installed", file=sys.stderr)
    sys.exit(1)

try:
    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
except ImportError:
    print("Error: pymongo not installed", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    stream=sys.stderr,
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ALLOWED_USERS = [
    u.strip()
    for u in os.getenv('MATRIX_ALLOWED_USERS', '').split(',')
    if u.strip()
]

CONFIDENCE_THRESHOLD = 0.6
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
PROMPT_VERSION = "classifier_v1"
MAX_OUTPUT_LENGTH = 60000

# Haiku 4.5 pricing ($/M tokens)
_COST_INPUT  = 1.00
_COST_OUTPUT = 5.00

stats = {
    "messages":    0,
    "classified":  0,
    "stored":      {},   # collection → count
    "api_calls":   0,
    "input_tokens":  0,
    "output_tokens": 0,
}

CLASSIFIER_SYSTEM = """You are a second brain classifier. Classify the input into exactly one of:
  people | project | idea | admin | unclassified

Rules:
  people:       mentions a specific person, relationship, or contact
  project:      has an outcome, deadline, or describes active work
  idea:         speculative, future, or exploratory thought
  admin:        recurring, housekeeping, or reference material
  unclassified: ambiguous or insufficient information

Confidence scale (0.0 – 1.0):
  1.0  Single unambiguous category, all key fields present
  0.8  Clear category, one minor field uncertain
  0.6  Probable category, meaningful ambiguity remains
  0.4  Two categories plausible, insufficient signal to decide
  0.2  Input too vague or fragmentary to classify reliably
  0.0  No usable signal

Also extract whatever fields are present for the classified type:
  people:  name, tags, notes, contacts (list of {type, value})
  project: title, status, collection, notes, due_date
  idea:    body, tags, status (raw/refined/promoted), projects
  admin:   type (recurring/reference/template), title, content, tags

Return JSON only — no prose:
{
  "classification": "",
  "confidence": 0.0,
  "reasoning": "",
  "missing_fields": [],
  "follow_up_question": "",
  "extracted_fields": {}
}"""

# ── MongoDB ───────────────────────────────────────────────────────────────────

_mongo_client = None
_db = None


def get_db():
    global _mongo_client, _db
    if _db is not None:
        return _db
    uri = os.getenv('BRAIN2_MONGODB_URI', 'mongodb://localhost:27017')
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


# ── Secrets ───────────────────────────────────────────────────────────────────

def load_token():
    token = os.getenv('MATRIX_SOLTI_BRAIN2_TOKEN')
    if token:
        return token
    secrets_file = Path.home() / '.secrets/LabMatrix'
    if secrets_file.exists():
        for line in secrets_file.read_text().splitlines():
            if line.startswith('export MATRIX_SOLTI_BRAIN2_TOKEN='):
                return line.split('=', 1)[1].strip().strip('"\'')
    logger.error("MATRIX_SOLTI_BRAIN2_TOKEN not found")
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


# ── Classifier ────────────────────────────────────────────────────────────────

def classify(text: str, api_key: str) -> dict:
    """Call Claude to classify and extract fields. Returns parsed dict."""
    client = Anthropic(api_key=api_key)
    try:
        response = client.messages.create(
            model=CLASSIFIER_MODEL,
            max_tokens=1024,
            system=CLASSIFIER_SYSTEM,
            messages=[{"role": "user", "content": text}]
        )
        stats["api_calls"]    += 1
        stats["input_tokens"]  += response.usage.input_tokens
        stats["output_tokens"] += response.usage.output_tokens

        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```[a-z]*\n?', '', raw).rstrip('`').strip()
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Classifier returned invalid JSON: {e}")
        return {
            "classification": "unclassified",
            "confidence": 0.0,
            "reasoning": "Classifier parse error",
            "missing_fields": [],
            "follow_up_question": "Could you rephrase that?",
            "extracted_fields": {}
        }
    except Exception as e:
        logger.error(f"Classifier API error: {e}")
        raise


# ── Document builders ─────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).isoformat()


def build_people_doc(raw_input: str, result: dict) -> dict:
    f = result.get("extracted_fields", {})
    return {
        "name":         f.get("name", ""),
        "tags":         f.get("tags", []),
        "notes":        f.get("notes", raw_input),
        "last_contact": "",
        "projects":     [],
        "contacts":     f.get("contacts", []),
        "source":       "matrix-room",
        "created_at":   _now(),
        "updated_at":   _now(),
    }


def build_project_doc(raw_input: str, result: dict) -> dict:
    f = result.get("extracted_fields", {})
    return {
        "title":      f.get("title", raw_input[:80]),
        "status":     f.get("status", "active"),
        "collection": f.get("collection", ""),
        "people":     [],
        "ideas":      [],
        "due_date":   f.get("due_date", None),
        "notes":      f.get("notes", raw_input),
        "source":     "matrix-room",
        "created_at": _now(),
        "updated_at": _now(),
    }


def build_idea_doc(raw_input: str, result: dict) -> dict:
    f = result.get("extracted_fields", {})
    return {
        "body":       f.get("body", raw_input),
        "tags":       f.get("tags", []),
        "status":     f.get("status", "raw"),
        "source":     "matrix-room",
        "projects":   f.get("projects", []),
        "created_at": _now(),
        "updated_at": _now(),
    }


def build_admin_doc(raw_input: str, result: dict) -> dict:
    f = result.get("extracted_fields", {})
    return {
        "type":       f.get("type", "reference"),
        "title":      f.get("title", raw_input[:80]),
        "content":    f.get("content", raw_input),
        "tags":       f.get("tags", []),
        "recurrence": f.get("recurrence", None),
        "source":     "matrix-room",
        "created_at": _now(),
        "updated_at": _now(),
    }


def build_unclassified_doc(raw_input: str, result: dict) -> dict:
    return {
        "raw_input":               raw_input,
        "source":                  "matrix-room",
        "timestamp":               _now(),
        "attempted_classification": result.get("classification", "unclassified"),
        "confidence":              result.get("confidence", 0.0),
        "missing_fields":          result.get("missing_fields", []),
        "follow_up_question":      result.get("follow_up_question", ""),
        "resolution":              "pending",
        "resolved_to":             None,
        "created_at":              _now(),
        "updated_at":              _now(),
    }


_BUILDERS = {
    "people":       build_people_doc,
    "project":      build_project_doc,
    "idea":         build_idea_doc,
    "admin":        build_admin_doc,
    "unclassified": build_unclassified_doc,
}

# Map classifier output names to MongoDB collection names
_COLLECTION_MAP = {
    "people":       "people",
    "project":      "projects",
    "idea":         "ideas",
    "admin":        "admin",
    "unclassified": "unclassified",
}


def write_replay_log(db, raw_input: str, result: dict, stored_collection: str):
    entry = {
        "replay_id":              str(uuid.uuid4()),
        "timestamp":              _now(),
        "source":                 "matrix-room",
        "raw_input":              raw_input,
        "actual_classification":  result.get("classification", "unclassified"),
        "stored_collection":      stored_collection,
        "confidence":             result.get("confidence", 0.0),
        "missing_fields":         result.get("missing_fields", []),
        "prompt_version":         PROMPT_VERSION,
        "model":                  CLASSIFIER_MODEL,
        "human_verified":         False,
        "passed":                 None,
    }
    db.replay_log.insert_one(entry)


def store_entry(raw_input: str, result: dict) -> tuple[str, str, str]:
    """
    Store classified entry in MongoDB.
    Returns (collection_name, inserted_id, error_message).
    """
    db = get_db()
    if db is None:
        return "", "", "MongoDB unavailable"

    classification = result.get("classification", "unclassified")
    confidence = result.get("confidence", 0.0)

    # Route low-confidence to unclassified regardless of classification
    if confidence < CONFIDENCE_THRESHOLD:
        classification = "unclassified"

    builder = _BUILDERS.get(classification, build_unclassified_doc)
    doc = builder(raw_input, result)

    collection_name = _COLLECTION_MAP.get(classification, "unclassified")
    try:
        inserted = db[collection_name].insert_one(doc)
        write_replay_log(db, raw_input, result, collection_name)
        return collection_name, str(inserted.inserted_id), ""
    except Exception as e:
        logger.error(f"MongoDB write failed: {e}")
        return collection_name, "", str(e)


# ── Reply formatting ──────────────────────────────────────────────────────────

def format_stored_reply(result: dict, collection: str, doc_id: str) -> str:
    confidence = result.get("confidence", 0.0)
    reasoning = result.get("reasoning", "")
    fields = result.get("extracted_fields", {})

    lines = [
        f"**Stored** → `{collection}` (confidence: {confidence:.0%})",
        f"_{reasoning}_",
    ]
    if fields:
        summary = ", ".join(f"{k}: {v}" for k, v in fields.items() if v and k not in ("contacts",))
        if summary:
            lines.append(f"Fields: {summary}")
    lines.append(f"ID: `{doc_id}`")
    return "\n".join(lines)


def format_unclassified_reply(result: dict, doc_id: str) -> str:
    confidence = result.get("confidence", 0.0)
    follow_up = result.get("follow_up_question", "Can you provide more detail?")
    reasoning = result.get("reasoning", "")
    lines = [
        f"**Unclassified** (confidence: {confidence:.0%}) — saved for review. ID: `{doc_id}`",
        f"_{reasoning}_",
        f"\n{follow_up}",
    ]
    return "\n".join(lines)


# ── Matrix ────────────────────────────────────────────────────────────────────

async def send_reply(room: MatrixRoom, message: str, client: AsyncClient):
    if len(message) > MAX_OUTPUT_LENGTH:
        message = message[:MAX_OUTPUT_LENGTH] + "\n\n⚠️ Response truncated."
    try:
        await client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message}
        )
    except Exception as e:
        logger.error(f"Failed to send reply: {e}")


def extract_text(body: str, bot_mention: str) -> str:
    localpart = bot_mention.lstrip('@')
    text = re.sub(rf'@{re.escape(localpart)}(?::[^\s]+)?[:\s]*', '', body, flags=re.IGNORECASE)
    text = re.sub(r'^\*\s+', '', text)                                          # Matrix edit prefix
    text = re.sub(r'^(hey|hi|hello|please)[,:]?\s*', '', text, flags=re.IGNORECASE)
    return text.strip().rstrip('?!.')


async def message_callback(room: MatrixRoom, event: RoomMessage,
                           client: AsyncClient, bot_user_id: str, api_key: str):
    if event.sender == bot_user_id:
        return
    if not hasattr(event, 'body') or not event.body:
        return

    localpart = bot_user_id.split(':')[0][1:]
    if f"@{localpart}" not in event.body.lower():
        return

    if event.sender not in ALLOWED_USERS:
        logger.warning(f"Unauthorized: {event.sender}")
        await send_reply(room, f"⚠️ Unauthorized user: {event.sender}", client)
        return

    text = extract_text(event.body, f"@{localpart}")
    logger.info(f"Message from {event.sender}: {text[:80]}")

    # Slash commands — anything starting with / or bare mention
    if not text or text.startswith('/'):
        cmd = text.lstrip('/').split()[0].lower() if text else ""

        if cmd in ("", "help"):
            await send_reply(room, """\
**Brain2 Bot — Commands**

  /help    — this message
  /status  — MongoDB connection + collection counts
  /cost    — session API usage and estimated cost

Anything else is classified and stored:
  `@solti-brain2 idea: build a Grafana dashboard for MongoDB`
  `@solti-brain2 Bob Chen, email bob@example.com`
  `@solti-brain2 project: add WireGuard to solti-ensemble`""", client)
            return

        if cmd == "status":
            db = get_db()
            mongo_status = "connected" if db is not None else "unavailable"
            counts = {}
            if db is not None:
                for col in ("people", "projects", "ideas", "admin", "unclassified", "replay_log"):
                    try:
                        counts[col] = db[col].count_documents({})
                    except Exception:
                        counts[col] = "?"
            count_lines = "\n".join(f"  {k}: {v}" for k, v in counts.items())
            await send_reply(room, f"""\
**Brain2 Bot — Status**

**MongoDB:** {mongo_status}
**Collections:**
{count_lines}

**Session:** {stats['messages']} messages · {stats['classified']} classified
**Threshold:** {CONFIDENCE_THRESHOLD:.0%} · **Model:** {CLASSIFIER_MODEL}""", client)
            return

        if cmd == "cost":
            inp  = stats["input_tokens"]
            out  = stats["output_tokens"]
            cost = (inp * _COST_INPUT + out * _COST_OUTPUT) / 1_000_000
            stored_lines = "\n".join(
                f"  {k}: {v}" for k, v in stats["stored"].items()
            ) or "  (none yet)"
            await send_reply(room, f"""\
**Brain2 Bot — Session Cost**

**API calls:** {stats['api_calls']}
**Tokens:** {inp:,} in / {out:,} out
**Estimated cost:** ${cost:.4f}
**Model:** {CLASSIFIER_MODEL} (${_COST_INPUT:.2f}/M in · ${_COST_OUTPUT:.2f}/M out)

**Stored this session:**
{stored_lines}""", client)
            return

        await send_reply(room, f"Unknown command `/{cmd}` — try `/help`", client)
        return

    # Classify and store
    stats["messages"] += 1

    if not api_key:
        await send_reply(room, "⚠️ ANTHROPIC_API_KEY not configured.", client)
        return

    try:
        result = classify(text, api_key)
    except Exception as e:
        await send_reply(room, f"⚠️ Classifier error: {e}", client)
        return

    stats["classified"] += 1
    confidence = result.get("confidence", 0.0)
    collection, doc_id, err = store_entry(text, result)

    if err:
        await send_reply(room, f"⚠️ Storage error: {err}", client)
        return

    stats["stored"][collection] = stats["stored"].get(collection, 0) + 1

    if confidence >= CONFIDENCE_THRESHOLD:
        reply = format_stored_reply(result, collection, doc_id)
    else:
        reply = format_unclassified_reply(result, doc_id)

    logger.info(f"Stored → {collection} (confidence={confidence:.2f}, id={doc_id})")
    await send_reply(room, reply, client)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    homeserver_url = os.getenv('MATRIX_HOMESERVER_URL', '')
    room_id        = os.getenv('MATRIX_ROOM_ID', '')
    bot_user_id    = os.getenv('MATRIX_BOT_USER_ID', '')
    if not homeserver_url or not room_id or not bot_user_id:
        logger.error("MATRIX_HOMESERVER_URL, MATRIX_ROOM_ID, and MATRIX_BOT_USER_ID are required")
        sys.exit(1)

    token   = load_token()
    api_key = load_api_key()

    if not token:
        logger.error("No Matrix token — cannot start")
        sys.exit(1)

    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY — classification disabled")

    # Warm up MongoDB connection
    db = get_db()

    print("=" * 72)
    print(f"Brain2 Bot")
    print(f"  Homeserver : {homeserver_url}")
    print(f"  Bot user   : {bot_user_id}")
    print(f"  Room       : {room_id}")
    print(f"  MongoDB    : {'connected' if db is not None else 'UNAVAILABLE'}")
    print(f"  API key    : {'✓' if api_key else '✗ missing'}")
    print(f"  Threshold  : {CONFIDENCE_THRESHOLD:.0%}")
    print("=" * 72)

    client = AsyncClient(homeserver_url, bot_user_id)
    client.access_token = token

    async def _callback(room, event):
        await message_callback(room, event, client, bot_user_id, api_key)

    client.add_event_callback(_callback, RoomMessage)

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

        logger.info("Listening for @mentions…")
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
