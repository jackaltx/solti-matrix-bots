#!/usr/bin/env python3
"""
Matrix SOLTI Event Validation Bot (matrix-nio version)

Monitors Matrix room for SOLTI events, validates against schema rubrics
using a layered protocol model, and posts human-readable summaries back
to the room.

Protocol Layers:
    Layer 1 — Transport:  msgtype == "com.solti.event" (machine-only)
    Layer 2 — Envelope:   solti.{schema, timestamp, source, data} present and schema known
    Layer 3 — Content:    data fields satisfy schema rubric (hierarchical)

Backward compatibility: also handles legacy m.text + solti events (Gen 2)
and dev.solti.log_data events (Gen 1) — silently ignores them (no reply).

Usage:
    ./bin/matrix-bot-nio.py

Environment Variables:
    MATRIX_ACCESS_TOKEN - Bot or user access token (or reads from data/matrix-logger-token.txt)
    MATRIX_HOMESERVER_URL - Homeserver URL (required)
    MATRIX_ROOM_ID - Room ID or alias (required)
    MATRIX_BOT_USER_ID - Bot's Matrix user ID (required)

Requirements:
    pip install matrix-nio
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from nio import AsyncClient, RoomMessageText, RoomMessage, MatrixRoom, LoginError, SyncError
except ImportError:
    print("Error: matrix-nio not installed", file=sys.stderr)
    print("Install with: pip install matrix-nio", file=sys.stderr)
    sys.exit(1)

# Custom msgtype for SOLTI machine events (Layer 1)
SOLTI_MSGTYPE = "com.solti.event"

# Schema rubrics — required fields for each schema (Layer 3)
SCHEMA_RUBRICS = {
    "test.v1": {
        "description": "Molecule test event",
        "required_fields": ["message", "timestamp"]
    },
    "verify.fail.v1": {
        "description": "Verification failure",
        "required_fields": [
            "distribution",
            "hostname",
            "summary",
            "summary.total_services",
            "summary.failed_services",
            "summary.passed_services",
            "services",
            "failed_service_names"
        ]
    },
    "verify.pass.v1": {
        "description": "Verification success",
        "required_fields": [
            "distribution",
            "hostname",
            "summary",
            "summary.total_services",
            "summary.failed_services",
            "summary.passed_services",
            "services",
            "failed_service_names"
        ]
    },
    "deploy.start.v1": {
        "description": "Deployment started",
        "required_fields": ["service", "host", "playbook", "operator"]
    },
    "deploy.complete.v1": {
        "description": "Deployment completed",
        "required_fields": [
            "service", "host", "playbook",
            "operator", "duration", "status"
        ]
    }
}

# Event statistics
event_stats = {
    "total": 0,
    "layer2_failures": 0,
    "layer3_valid": 0,
    "layer3_invalid": 0,
    "legacy": 0,
    "ignored": 0,
}


def load_token():
    """Get access token from env var or file."""
    token = os.getenv('MATRIX_WATCHER_TOKEN')
    if token:
        return token

    token_file = Path(__file__).parent.parent / 'data' / 'matrix-watcher-token.txt'
    if token_file.exists():
        return token_file.read_text().strip()

    print(f"Error: Token not found. Set MATRIX_WATCHER_TOKEN or create {token_file}", file=sys.stderr)
    sys.exit(1)


def format_timestamp():
    """Get current timestamp in readable format."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def has_nested_field(data, field_path):
    """
    Check if nested field exists in data dictionary.

    Args:
        data: Dictionary to check
        field_path: Dot-separated path like "summary.total_services"

    Returns:
        bool: True if field exists and is not None
    """
    if not isinstance(data, dict):
        return False

    parts = field_path.split('.', 1)
    key = parts[0]

    if key not in data:
        return False

    if len(parts) == 1:
        return data[key] is not None
    else:
        return has_nested_field(data[key], parts[1])


# ---------------------------------------------------------------------------
# Layered validation
# ---------------------------------------------------------------------------

def check_layer1(content):
    """
    Layer 1 — Transport check.

    Is this a SOLTI machine event (com.solti.event)?
    Also detects legacy formats for backward compatibility.

    Returns:
        "solti"   — new com.solti.event format
        "legacy"  — old m.text + solti envelope (Gen 2)
        "gen1"    — dev.solti.log_data (Gen 1)
        None      — not a SOLTI event, ignore silently
    """
    msgtype = content.get("msgtype", "")

    if msgtype == SOLTI_MSGTYPE:
        return "solti"

    if msgtype == "m.text":
        if "solti" in content:
            return "legacy"
        if "dev.solti.log_data" in content:
            return "gen1"

    return None


def check_layer2(content):
    """
    Layer 2 — Envelope check.

    Is the solti envelope well-formed and the schema known?

    Returns:
        dict with keys:
            valid: bool
            schema: str or None
            source: str or None
            reason: str (if invalid)
    """
    solti = content.get("solti", {})

    schema = solti.get("schema")
    if not schema:
        return {"valid": False, "schema": None, "source": solti.get("source"), "reason": "Missing solti.schema"}

    if schema not in SCHEMA_RUBRICS:
        return {"valid": False, "schema": schema, "source": solti.get("source"), "reason": f"Unknown schema: {schema}"}

    for field in ["timestamp", "source", "data"]:
        if field not in solti:
            return {"valid": False, "schema": schema, "source": solti.get("source"), "reason": f"Missing solti.{field}"}

    return {"valid": True, "schema": schema, "source": solti.get("source"), "data": solti["data"]}


def check_layer3(schema, data):
    """
    Layer 3 — Content check.

    Does the data payload satisfy the schema rubric?
    Hierarchical: checks nested fields via dot-notation.

    Returns:
        dict with keys:
            valid: bool
            missing_fields: list (if invalid)
    """
    rubric = SCHEMA_RUBRICS[schema]
    missing_fields = [
        field for field in rubric["required_fields"]
        if not has_nested_field(data, field)
    ]

    if missing_fields:
        return {"valid": False, "missing_fields": missing_fields}

    return {"valid": True}


# ---------------------------------------------------------------------------
# Human-readable summary builders
# ---------------------------------------------------------------------------

def build_layer2_alert(layer2_result):
    """Build alert message for Layer 2 failures (sender pipeline broken)."""
    schema = layer2_result.get("schema", "unknown")
    source = layer2_result.get("source", "unknown")
    reason = layer2_result.get("reason", "unknown")
    return f"⚠️ SOLTI envelope error from {source}\n  Schema: {schema}\n  Problem: {reason}\n  Action: fix the sender pipeline"


def build_pass_summary(layer2_result, layer3_result, data):
    """Build quiet acknowledgement for Layer 3 pass."""
    schema = layer2_result["schema"]
    source = layer2_result["source"]

    if schema in ("verify.pass.v1", "verify.fail.v1"):
        summary = data.get("summary", {})
        total = summary.get("total_services", "?")
        passed = summary.get("passed_services", "?")
        failed = summary.get("failed_services", "?")
        distribution = data.get("distribution", "unknown")
        return (
            f"✅ {schema} | {source} | {distribution}\n"
            f"  services: {passed}/{total} passed"
        )

    if schema == "deploy.complete.v1":
        service = data.get("service", "?")
        host = data.get("host", "?")
        duration = data.get("duration", "?")
        return f"✅ {schema} | {service} on {host} ({duration}s)"

    if schema == "deploy.start.v1":
        service = data.get("service", "?")
        host = data.get("host", "?")
        return f"🚀 {schema} | {service} on {host}"

    return f"✅ {schema} from {source}"


def build_fail_summary(layer2_result, layer3_result, data):
    """Build structured failure summary with reproduction context."""
    schema = layer2_result["schema"]
    source = layer2_result["source"]

    if schema in ("verify.fail.v1",):
        summary = data.get("summary", {})
        total = summary.get("total_services", "?")
        passed = summary.get("passed_services", "?")
        failed_names = data.get("failed_service_names", [])
        distribution = data.get("distribution", "unknown")
        hostname = data.get("hostname", "unknown")
        context = data.get("context", {})
        scenario = context.get("scenario", "?")
        report_path = data.get("report_path", "")
        failed_task = data.get("failed_task", "")
        error_msg = data.get("error_msg", "")

        lines = [
            f"❌ {schema} | {source} | {distribution}",
            f"  host: {hostname}  passed: {passed}/{total}",
            f"  failed: {', '.join(failed_names) if failed_names else 'none listed'}",
        ]
        if failed_task:
            lines.append(f"  failed_task: {failed_task}")
        if error_msg:
            lines.append(f"  error: {error_msg}")
        if report_path:
            lines.append(f"  report: {report_path}")
        if scenario and scenario != "?":
            lines.append(f"  reproduce: molecule test -s {scenario}")

        # Layer 3 missing fields
        missing = layer3_result.get("missing_fields", [])
        if missing:
            lines.append(f"  missing fields: {', '.join(missing)}")

        return "\n".join(lines)

    if schema == "deploy.complete.v1":
        service = data.get("service", "?")
        host = data.get("host", "?")
        status = data.get("status", "unknown")
        duration = data.get("duration", "?")
        return f"❌ {schema} | {service} on {host} — {status} ({duration}s)"

    # Generic fallback
    missing = layer3_result.get("missing_fields", [])
    msg = f"❌ {schema} from {source} — content invalid"
    if missing:
        msg += f"\n  missing: {', '.join(missing)}"
    return msg


# ---------------------------------------------------------------------------
# Message callback and agent workflow
# ---------------------------------------------------------------------------

async def handle_agent_workflow(layer1_type, layer2_result, layer3_result, data, room, client):
    """
    Construct and post human-readable summary back to the room.

    Layer 2 failure → alert (sender pipeline broken)
    Layer 3 pass    → quiet acknowledgement
    Layer 3 fail    → structured failure summary with reproduction context
    """
    if not layer2_result["valid"]:
        msg = build_layer2_alert(layer2_result)
    elif layer3_result["valid"]:
        msg = build_pass_summary(layer2_result, layer3_result, data)
    else:
        msg = build_fail_summary(layer2_result, layer3_result, data)

    try:
        print(f"{format_timestamp()} | Sending reply to room...")
        response = await client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": msg
            }
        )
        print(f"{format_timestamp()} | Reply sent: {response.event_id if hasattr(response, 'event_id') else response}")
    except Exception as e:
        print(f"{format_timestamp()} | Failed to send reply: {e}", file=sys.stderr)


async def message_callback(room: MatrixRoom, event: RoomMessage, client: AsyncClient):
    """
    Callback for room message events.

    Runs layered validation and posts human-readable summary.
    """
    # Skip bot's own messages
    if event.sender == client.user_id:
        return

    if not hasattr(event, 'source') or 'content' not in event.source:
        return

    content = event.source['content']
    timestamp = format_timestamp()

    # Layer 1 — what kind of message is this?
    layer1_type = check_layer1(content)

    if layer1_type is None:
        event_stats["ignored"] += 1
        return

    if layer1_type in ("legacy", "gen1"):
        # Backward compat — validate silently, no reply (old senders)
        event_stats["legacy"] += 1
        print(f"{timestamp} | [legacy {layer1_type}] ignored for reply")
        return

    # layer1_type == "solti" — new format, full processing
    event_stats["total"] += 1

    # Layer 2 — envelope check
    layer2_result = check_layer2(content)

    if not layer2_result["valid"]:
        event_stats["layer2_failures"] += 1
        print(f"{timestamp} | L2 FAIL: {layer2_result['reason']} (schema={layer2_result.get('schema')}, source={layer2_result.get('source')})")
        await handle_agent_workflow(layer1_type, layer2_result, {"valid": False, "missing_fields": []}, {}, room, client)
        return

    schema = layer2_result["schema"]
    source = layer2_result["source"]
    data = layer2_result["data"]

    # Layer 3 — content check
    layer3_result = check_layer3(schema, data)

    if layer3_result["valid"]:
        event_stats["layer3_valid"] += 1
        print(f"{timestamp} | L3 PASS: {schema} from {source}")
    else:
        event_stats["layer3_invalid"] += 1
        print(f"{timestamp} | L3 FAIL: {schema} from {source} — missing: {', '.join(layer3_result['missing_fields'])}")

    await handle_agent_workflow(layer1_type, layer2_result, layer3_result, data, room, client)


async def main():
    """Main async bot loop."""
    homeserver_url = os.getenv('MATRIX_HOMESERVER_URL', '')
    room_id = os.getenv('MATRIX_ROOM_ID', '')
    bot_user_id = os.getenv('MATRIX_BOT_USER_ID', '')
    if not homeserver_url or not room_id or not bot_user_id:
        print("Error: MATRIX_HOMESERVER_URL, MATRIX_ROOM_ID, and MATRIX_BOT_USER_ID are required", file=sys.stderr)
        sys.exit(1)
    token = load_token()

    print("=" * 80)
    print(f"{format_timestamp()} | Matrix Bot Starting (matrix-nio)")
    print(f"  Homeserver: {homeserver_url}")
    print(f"  Bot User:   {bot_user_id}")
    print(f"  Room:       {room_id}")
    print(f"  Protocol:   layered (L1={SOLTI_MSGTYPE})")
    print("=" * 80)

    client = AsyncClient(homeserver_url, bot_user_id)
    client.access_token = token

    # Wrap callback to pass client reference
    async def _callback(room, event):
        await message_callback(room, event, client)

    client.add_event_callback(_callback, RoomMessage)

    try:
        print(f"{format_timestamp()} | Performing initial sync...")
        sync_response = await client.sync(timeout=30000)

        if isinstance(sync_response, SyncError):
            print(f"Error: Sync failed: {sync_response.message}", file=sys.stderr)
            await client.close()
            return

        if sync_response.rooms:
            print(f"{format_timestamp()} | Initial sync complete")

        if room_id.startswith('#'):
            print(f"{format_timestamp()} | Resolving room alias...")
            resolve_response = await client.room_resolve_alias(room_id)
            if resolve_response.room_id:
                resolved_room_id = resolve_response.room_id
                print(f"  Resolved:   {resolved_room_id}")
            else:
                print(f"Error: Failed to resolve room alias {room_id}", file=sys.stderr)
                await client.close()
                return
        else:
            resolved_room_id = room_id

        join_response = await client.join(resolved_room_id)
        if hasattr(join_response, 'room_id'):
            print(f"{format_timestamp()} | Joined room: {join_response.room_id}")

        print(f"{format_timestamp()} | Listening for SOLTI events...")
        print(f"  Press Ctrl+C to stop")
        print("=" * 80)

        await client.sync_forever(timeout=30000, full_state=False)

    except KeyboardInterrupt:
        print("\n\nShutdown requested. Exiting gracefully...")
    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
    finally:
        await client.close()

        print("\n" + "=" * 80)
        print(f"{format_timestamp()} | Bot stopped")
        print(f"  Total com.solti.event:  {event_stats['total']}")
        print(f"    Layer 2 failures:     {event_stats['layer2_failures']}")
        print(f"    Layer 3 valid:        {event_stats['layer3_valid']}")
        print(f"    Layer 3 invalid:      {event_stats['layer3_invalid']}")
        print(f"  Legacy events:          {event_stats['legacy']}")
        print(f"  Ignored (non-SOLTI):    {event_stats['ignored']}")
        print("=" * 80)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
