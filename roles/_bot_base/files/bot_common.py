"""
bot_common — shared infrastructure glue for solti-matrix-bots.

Deployed by _bot_base into <data_dir>/common/, a fixed sibling of every bot's own
<data_dir>/<bot-name>/ directory. Bots import it with a relative sys.path bootstrap:

    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'common'))
    import bot_common

Extracted 2026-08-21 from byte-identical duplicated code found in brain2-bot, card-capture-bot,
and salty-bot (get_db/get_s3/ensure_bucket) — see projects/pinkee/CLAUDE.md for the audit.
Deploy-injected on purpose, not a pip package — see that doc for the staging rationale.

Only pymongo/boto3 are imported lazily, inside the functions that need them, so importing
bot_common itself never requires either package unless a bot actually calls into Mongo or S3.
"""

import logging
import os

logger = logging.getLogger(__name__)

# ── Allowed users ───────────────────────────────────────────────────────────────

def allowed_users() -> list[str]:
    """Parse MATRIX_ALLOWED_USERS into a list of Matrix user IDs. Empty list = no restriction."""
    return [
        u.strip()
        for u in os.getenv('MATRIX_ALLOWED_USERS', '').split(',')
        if u.strip()
    ]


# ── MongoDB ─────────────────────────────────────────────────────────────────────

_mongo_client = None
_db = None


def get_db():
    """
    Cached MongoDB handle. Reads SOLTI_MONGODB_URI/SOLTI_MONGODB_DB, falling back to the
    older BRAIN2_MONGODB_URI/BRAIN2_MONGODB_DB names still set by existing bot deployments.
    Returns None (logged) if the connection fails — callers must handle that, matching the
    existing behavior in every bot this was extracted from.
    """
    global _mongo_client, _db
    if _db is not None:
        return _db

    from pymongo import MongoClient
    from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

    uri = os.getenv('SOLTI_MONGODB_URI', os.getenv('BRAIN2_MONGODB_URI', 'mongodb://localhost:27017'))
    db_name = os.getenv('SOLTI_MONGODB_DB', os.getenv('BRAIN2_MONGODB_DB', 'second_brain'))
    try:
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        _mongo_client.admin.command('ping')
        _db = _mongo_client[db_name]
        logger.info(f"MongoDB connected: {db_name}")
        return _db
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        logger.error(f"MongoDB connection failed: {e}")
        return None


# ── S3 / MinIO ──────────────────────────────────────────────────────────────────

_s3_client = None


def get_s3():
    """Cached S3/MinIO client. Reads S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY."""
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    import boto3

    _s3_client = boto3.client(
        's3',
        endpoint_url=os.getenv('S3_ENDPOINT_URL', 'http://localhost:9000'),
        aws_access_key_id=os.getenv('S3_ACCESS_KEY'),
        aws_secret_access_key=os.getenv('S3_SECRET_KEY'),
    )
    return _s3_client


def ensure_bucket(bucket: str):
    """Create the bucket if it doesn't already exist."""
    from botocore.exceptions import ClientError as S3ClientError

    s3 = get_s3()
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info(f"S3 bucket exists: {bucket}")
    except S3ClientError:
        s3.create_bucket(Bucket=bucket)
        logger.info(f"S3 bucket created: {bucket}")


# ── Model pricing / cost estimation ─────────────────────────────────────────────
# ($ per million tokens). Values as already hardcoded across the bots this was
# extracted from — not independently verified against a pricing page.

MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6":            (3.00, 15.00),
    "claude-sonnet-4-5-20250929":   (3.00, 15.00),
    "claude-haiku-4-5-20251001":    (1.00, 5.00),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate $ cost for a model call. Unknown models log a warning and return 0.0."""
    pricing = MODEL_PRICING.get(model)
    if pricing is None:
        logger.warning(f"estimate_cost: no pricing for model '{model}', returning 0.0")
        return 0.0
    cost_in, cost_out = pricing
    return (input_tokens * cost_in + output_tokens * cost_out) / 1_000_000


# ── Usage stats ─────────────────────────────────────────────────────────────────
# The three counters that repeat verbatim across every bot's own stats dict.
# Bot-specific counters (sessions_started, cards, classified, ...) stay in the bot's
# own dict — this only covers the universal core.

def new_stats(**extra) -> dict:
    """A stats dict with the three universal API-usage counters, plus any bot-specific ones."""
    base = {"api_calls": 0, "input_tokens": 0, "output_tokens": 0}
    base.update(extra)
    return base


def record_usage(stats: dict, input_tokens: int, output_tokens: int):
    """Update the universal counters on a stats dict after an API call."""
    stats["api_calls"] += 1
    stats["input_tokens"] += input_tokens
    stats["output_tokens"] += output_tokens
