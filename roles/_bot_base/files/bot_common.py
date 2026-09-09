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

Vault AppRole auth (stdlib only — no requests/httpx dependency):
    vault_token = vault_approle_auth()   # reads VAULT_ADDR/VAULT_ROLE_ID_FILE/VAULT_SECRET_ID_FILE
    data = vault_kv_read(vault_token, "hosts/bot-test/card-capture/mongodb")
    matrix_token = matrix_login(homeserver_url, user_id, password)
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


# ── Vault AppRole auth ───────────────────────────────────────────────────────────

def vault_approle_auth(
    vault_addr: str | None = None,
    role_id_file: str | None = None,
    secret_id_file: str | None = None,
) -> str:
    """
    Exchange AppRole credentials for a short-lived Vault token.

    Reads from env vars when args are omitted:
        VAULT_ADDR           — Vault HTTP/HTTPS address
        VAULT_ROLE_ID_FILE   — path to file containing the role-id
        VAULT_SECRET_ID_FILE — path to file containing the secret-id

    Returns the client_token string. Raises RuntimeError on failure.
    """
    addr = vault_addr or os.getenv('VAULT_ADDR', '')
    if not addr:
        raise RuntimeError("VAULT_ADDR not set and vault_addr not provided")

    rid_file = role_id_file or os.getenv('VAULT_ROLE_ID_FILE', '')
    sid_file = secret_id_file or os.getenv('VAULT_SECRET_ID_FILE', '')
    if not rid_file or not sid_file:
        raise RuntimeError("VAULT_ROLE_ID_FILE and VAULT_SECRET_ID_FILE must be set")

    with open(rid_file) as f:
        role_id = f.read().strip()
    with open(sid_file) as f:
        secret_id = f.read().strip()

    payload = json.dumps({"role_id": role_id, "secret_id": secret_id}).encode()
    req = urllib.request.Request(
        f"{addr.rstrip('/')}/v1/auth/approle/login",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        token = body["auth"]["client_token"]
        logger.info("Vault AppRole auth successful")
        return token
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Vault AppRole login failed ({e.code}): {e.read().decode()}") from e


def vault_kv_read(vault_token: str, kv_path: str, vault_addr: str | None = None) -> dict:
    """
    Read a KV v2 secret. kv_path is relative to the mount, e.g. 'hosts/bot-test/card-capture/mongodb'.
    Returns the data dict (the inner `data.data` layer). Raises RuntimeError if not found.
    """
    addr = vault_addr or os.getenv('VAULT_ADDR', '')
    url = f"{addr.rstrip('/')}/v1/kv/data/{kv_path}"
    req = urllib.request.Request(url, headers={"X-Vault-Token": vault_token})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
        return body["data"]["data"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Vault read failed for {kv_path} ({e.code}): {e.read().decode()}") from e


def matrix_login(homeserver_url: str, user_id: str, password: str) -> str:
    """
    Log in to a Matrix homeserver with a password and return the access token.
    user_id may be a full MXID (@bot:domain) or a localpart (bot).
    Uses stdlib urllib only.
    """
    payload = json.dumps({
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": user_id},
        "password": password,
    }).encode()
    url = f"{homeserver_url.rstrip('/')}/_matrix/client/v3/login"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        token = body["access_token"]
        logger.info(f"Matrix login successful for {user_id}")
        return token
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Matrix login failed ({e.code}): {e.read().decode()}") from e


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
