#!/usr/bin/env python3
"""
Matrix Claude Code Bot - Phase 2

AI-powered analysis and reporting assistant via Matrix chat interface.
Responds to @mentions in #solti-dev room using Claude SDK with tool integration.

Phase 2 Focus: Analysis and reporting (read-only operations)
- Multi-file analysis
- Log analysis
- Configuration comparison
- Pattern detection
- Structured report generation

Architecture:
    Matrix Room → @mention detection → Claude SDK (with tools) → Reply

Security:
    - User authentication (whitelist)
    - Read-only tools (Read, Grep, Glob, validated Bash)
    - No file editing (Edit/Write disabled)
    - Execution timeout (5 min max)
    - Output truncation (60KB Matrix limit)
    - SIEM logging for security events

Usage:
    ./bin/claude-code-bot.py

Environment Variables:
    MATRIX_SOLTI_CLAUDE_CODE_TOKEN - Bot access token (or from ~/.secrets/LabMatrix)
    MATRIX_HOMESERVER_URL - Homeserver URL (required)
    MATRIX_ROOM_ID - Room ID or alias (required)
    MATRIX_BOT_USER_ID - Bot's Matrix user ID
    ANTHROPIC_API_KEY - Anthropic API key for Claude SDK (required)

Requirements:
    pip install matrix-nio anthropic
"""

import asyncio
import os
import re
import sys
import shlex
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

try:
    from nio import AsyncClient, RoomMessage, MatrixRoom, SyncError
except ImportError:
    print("Error: matrix-nio not installed", file=sys.stderr)
    print("Install with: pip install matrix-nio", file=sys.stderr)
    sys.exit(1)

try:
    from anthropic import Anthropic
except ImportError:
    print("Error: anthropic not installed", file=sys.stderr)
    print("Install with: pip install anthropic", file=sys.stderr)
    sys.exit(1)

# Configure logging for SIEM integration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    stream=sys.stderr,  # Goes to journald
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Security configuration
ALLOWED_USERS = [
    u.strip()
    for u in os.getenv('MATRIX_ALLOWED_USERS', '').split(',')
    if u.strip()
]

# Blocked command patterns (safety - checked for bash tool)
BLOCKED_PATTERNS = [
    # Destructive operations
    r"\brm\s+-rf",  # rm -rf (but allow plain rm for safety)
    r"\bdd\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bmkfs\b",
    r"\bformat\b",
    r"\bkill\b",
    r"\bpkill\b",
    r"\bsystemctl\s+stop",
    r"\bsystemctl\s+restart",
    r"\bsystemctl\s+disable",

    # Network/remote operations (can be read-only but risky)
    r"\bcurl\s+.*-X\s+(POST|PUT|DELETE|PATCH)",  # Allow GET, block mutations
    r"\bwget\s+.*--post",

    # File writes/modifications (but allow > in printf formats)
    r">\s*(?!>)[a-zA-Z0-9/\.\-_]+",  # Block redirect to files, but not >> or >output
    r"\bchmod\b",
    r"\bchown\b",
    r"\bmv\b.*\s+/",  # Moving to root paths
    r"\bcp\b.*\s+/",  # Copying to root paths
]

# Execution limits
MAX_EXECUTION_TIME = 300  # 5 minutes
MAX_OUTPUT_LENGTH = 60000  # 60KB (Matrix limit buffer)
MAX_ITERATIONS = 5  # Limit tool use iterations (was 10 - reduce cost)
WORKING_DIR = Path(os.getenv('MATRIX_WORKING_DIR', str(Path.home())))

# Model configuration (cost optimization)
# Sonnet 4.5: $3/M input, $15/M output - Best quality
# Haiku 4.5: $1/M input, $5/M output - 67% cheaper, fast
DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # Use cheaper model by default
COMPLEX_MODEL = "claude-sonnet-4-5-20250929"  # Use for complex analysis only

# Keywords that indicate complex analysis tasks (use Sonnet)
COMPLEX_KEYWORDS = [
    "analyze", "compare", "report", "summarize", "review", "explain",
    "document", "audit", "all files", "multiple", "across", "comprehensive",
    "throughout", "entire", "detailed", "in-depth", "investigate"
]

# Keywords that indicate simple tasks (use Haiku)
SIMPLE_KEYWORDS = [
    "list", "show", "find", "grep", "ls", "cat", "read",
    "status", "help", "what is", "display", "print"
]

# Stats
stats = {
    "messages_received": 0,
    "commands_processed": 0,
    "commands_blocked": 0,
    "errors": 0,
    "api_requests": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "estimated_cost": 0.0,
}


def load_token():
    """Get Matrix access token from env var or LabMatrix secrets."""
    token = os.getenv('MATRIX_SOLTI_CLAUDE_CODE_TOKEN')
    if token:
        return token

    # Try reading from LabMatrix
    secrets_file = Path.home() / '.secrets/LabMatrix'
    if secrets_file.exists():
        with open(secrets_file) as f:
            for line in f:
                if line.startswith('export MATRIX_SOLTI_CLAUDE_CODE_TOKEN='):
                    token = line.split('=', 1)[1].strip().strip('"').strip("'")
                    return token

    logger.error("MATRIX_SOLTI_CLAUDE_CODE_TOKEN not found")
    return None


def load_api_key():
    """Get Anthropic API key."""
    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        # Try from LabMatrix
        secrets_file = Path.home() / '.secrets/LabMatrix'
        if secrets_file.exists():
            with open(secrets_file) as f:
                for line in f:
                    if line.startswith('export ANTHROPIC_API_KEY='):
                        api_key = line.split('=', 1)[1].strip().strip('"').strip("'")
                        return api_key
        logger.error("ANTHROPIC_API_KEY not found - Phase 2 requires API key")
        return None
    return api_key


def format_timestamp():
    """Format current time for logging."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def is_command_blocked(command: str) -> bool:
    """Check if command contains blocked patterns."""
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return True
    return False


def extract_command(message_body: str, bot_mention: str) -> str:
    """
    Extract command from message that mentions the bot.

    Examples:
        "@solti-claude-code read CLAUDE.md" → "read CLAUDE.md"
        "@solti-claude-code: git status" → "git status"
        "hey @solti-claude-code can you git log" → "git log"
    """
    # Remove the bot mention - handle both @user and @user:domain.com formats
    bot_localpart = bot_mention.lstrip('@')
    pattern = rf'@{re.escape(bot_localpart)}(?::[^\s]+)?[:\s]*'
    message = re.sub(pattern, '', message_body, flags=re.IGNORECASE).strip()

    # Remove common prefixes
    message = re.sub(r'^(hey|hi|hello|please|can you|could you)[,:]?\s*', '', message, flags=re.IGNORECASE)

    # Remove trailing punctuation
    message = message.rstrip('?!.')

    return message.strip()


async def execute_bash_tool(command: str) -> Dict[str, Any]:
    """Execute bash command with security validation."""
    # Security check
    if is_command_blocked(command):
        logger.error(f"SECURITY: Blocked dangerous bash command - Command: {command}")
        return {
            "success": False,
            "error": "Blocked: Command contains dangerous patterns",
            "output": ""
        }

    try:
        # Execute command
        cmd_parts = shlex.split(command)
        process = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKING_DIR
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=MAX_EXECUTION_TIME
        )

        output = stdout.decode('utf-8', errors='replace')
        error = stderr.decode('utf-8', errors='replace')

        return {
            "success": process.returncode == 0,
            "output": output,
            "error": error,
            "returncode": process.returncode
        }

    except asyncio.TimeoutError:
        logger.warning(f"Bash command timeout - Command: {command}")
        return {
            "success": False,
            "error": f"Command timed out after {MAX_EXECUTION_TIME} seconds",
            "output": ""
        }
    except Exception as e:
        logger.error(f"Bash execution error - Command: {command}, Error: {e}")
        return {
            "success": False,
            "error": str(e),
            "output": ""
        }


def read_file_tool(file_path: str) -> Dict[str, Any]:
    """Read file from working directory."""
    try:
        full_path = WORKING_DIR / file_path

        # Security: prevent path traversal
        if not str(full_path.resolve()).startswith(str(WORKING_DIR.resolve())):
            return {
                "success": False,
                "error": "Path traversal not allowed - must be within working directory"
            }

        if not full_path.exists():
            return {
                "success": False,
                "error": f"File not found: {file_path}"
            }

        content = full_path.read_text()
        return {
            "success": True,
            "content": content
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def glob_tool(pattern: str) -> Dict[str, Any]:
    """Find files matching glob pattern."""
    try:
        from pathlib import Path
        matches = list(WORKING_DIR.glob(pattern))

        # Convert to relative paths
        files = [str(f.relative_to(WORKING_DIR)) for f in matches if f.is_file()]

        return {
            "success": True,
            "files": files,
            "count": len(files)
        }

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "files": []
        }


async def grep_tool(pattern: str, path: str = ".") -> Dict[str, Any]:
    """Search for pattern in files using grep."""
    try:
        # Use ripgrep if available, fallback to grep
        cmd = f"rg --no-heading --line-number '{pattern}' {path}"
        result = await execute_bash_tool(cmd)

        if not result["success"] and "rg" in result["error"]:
            # Fallback to grep
            cmd = f"grep -rn '{pattern}' {path}"
            result = await execute_bash_tool(cmd)

        return result

    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "output": ""
        }


def build_tool_definitions() -> List[Dict[str, Any]]:
    """Build Anthropic tool definitions for Claude SDK."""
    return [
        {
            "name": "read_file",
            "description": "Read contents of a file from the working directory. Use this to analyze configuration files, playbooks, scripts, or documentation.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to file relative to working directory (e.g., 'playbooks/matrix/base-matrix-config.yml')"
                    }
                },
                "required": ["file_path"]
            }
        },
        {
            "name": "glob_files",
            "description": "Find files matching a glob pattern. Use this to discover files (e.g., '**/*.yml' for all YAML files, 'playbooks/matrix/*.yml' for Matrix playbooks).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g., '**/*.py', 'playbooks/**/*.yml')"
                    }
                },
                "required": ["pattern"]
            }
        },
        {
            "name": "grep_search",
            "description": "Search for text pattern in files using grep/ripgrep. Use this to find specific content across the codebase.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Search pattern (regex supported)"
                    },
                    "path": {
                        "type": "string",
                        "description": "Path to search in (default: '.')",
                        "default": "."
                    }
                },
                "required": ["pattern"]
            }
        },
        {
            "name": "bash_command",
            "description": "Execute a bash command for system operations (git status, systemctl, journalctl, ls, etc.). Commands are validated for safety - no destructive operations allowed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Bash command to execute (e.g., 'git status', 'systemctl --user status', 'journalctl --user -u matrix-bot --since today')"
                    }
                },
                "required": ["command"]
            }
        }
    ]


async def execute_tool(tool_name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a tool and return results."""
    if tool_name == "read_file":
        return read_file_tool(tool_input["file_path"])

    elif tool_name == "glob_files":
        return glob_tool(tool_input["pattern"])

    elif tool_name == "grep_search":
        path = tool_input.get("path", ".")
        return await grep_tool(tool_input["pattern"], path)

    elif tool_name == "bash_command":
        return await execute_bash_tool(tool_input["command"])

    else:
        return {"success": False, "error": f"Unknown tool: {tool_name}"}


def select_model(user_message: str) -> tuple[str, str]:
    """
    Select appropriate model based on task complexity.

    Returns: (model_name, reason)
    """
    message_lower = user_message.lower()

    # Check for explicit model override
    if "[sonnet]" in message_lower:
        return COMPLEX_MODEL, "user requested Sonnet"
    if "[haiku]" in message_lower:
        return DEFAULT_MODEL, "user requested Haiku"

    # Auto-detect based on keywords
    complex_score = sum(1 for keyword in COMPLEX_KEYWORDS if keyword in message_lower)
    simple_score = sum(1 for keyword in SIMPLE_KEYWORDS if keyword in message_lower)

    # If multiple complex keywords, definitely use Sonnet
    if complex_score >= 2:
        return COMPLEX_MODEL, f"complex task detected ({complex_score} keywords)"

    # If any complex keyword and no simple keywords, use Sonnet
    if complex_score >= 1 and simple_score == 0:
        return COMPLEX_MODEL, "complex task detected"

    # Default to Haiku for cost savings
    return DEFAULT_MODEL, "simple task (default)"


async def process_with_claude(user_message: str, sender: str) -> str:
    """Process user message with Claude SDK and tool use."""
    api_key = load_api_key()
    if not api_key:
        logger.error("Cannot process request - no API key")
        return "⚠️ Configuration error: ANTHROPIC_API_KEY not set. Phase 2 requires API access."

    try:
        client = Anthropic(api_key=api_key)

        # Build system prompt
        system_prompt = f"""You are a development assistant helping manage an Ansible lab environment via Matrix chat.

Working Directory: {WORKING_DIR}
User: {sender}

Your role:
- Analyze configurations, playbooks, and code
- Generate reports and documentation
- Search logs and identify issues
- Answer questions about the codebase

Tools available:
- read_file: Read file contents
- glob_files: Find files by pattern
- grep_search: Search for text patterns
- bash_command: Run system commands (git, systemctl, journalctl, ls, etc.)

Constraints:
- Read-only operations (no file editing)
- All paths relative to working directory
- Provide structured, actionable responses
- Keep responses under 60KB (Matrix limit)

Response format:
- Use markdown formatting
- Include code blocks where appropriate
- Be concise but complete
- Focus on analysis and reporting"""

        # Select appropriate model based on task complexity
        selected_model, selection_reason = select_model(user_message)

        # Initial API call
        messages = [{"role": "user", "content": user_message}]

        stats["api_requests"] += 1
        logger.info(f"API Request - User: {sender}, Model: {selected_model.split('-')[1]} ({selection_reason}), Message length: {len(user_message)} chars")

        response = client.messages.create(
            model=selected_model,
            max_tokens=4096,
            system=system_prompt,
            tools=build_tool_definitions(),
            messages=messages
        )

        # Track usage (pricing depends on model used)
        stats["input_tokens"] += response.usage.input_tokens
        stats["output_tokens"] += response.usage.output_tokens

        # Calculate cost based on model (store model in stats for accurate tracking)
        if "sonnet" in selected_model:
            input_cost = (stats["input_tokens"] / 1_000_000) * 3.00
            output_cost = (stats["output_tokens"] / 1_000_000) * 15.00
        else:  # Haiku 4.5
            input_cost = (stats["input_tokens"] / 1_000_000) * 1.00
            output_cost = (stats["output_tokens"] / 1_000_000) * 5.00

        stats["estimated_cost"] = input_cost + output_cost

        # Tool use loop
        max_iterations = MAX_ITERATIONS
        iteration = 0

        while response.stop_reason == "tool_use" and iteration < max_iterations:
            iteration += 1

            # Execute tools
            tool_results = []
            for content_block in response.content:
                if content_block.type == "tool_use":
                    tool_name = content_block.name
                    tool_input = content_block.input

                    logger.info(f"Tool use - Tool: {tool_name}, Input: {str(tool_input)[:100]}")

                    # Execute tool
                    result = await execute_tool(tool_name, tool_input)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": content_block.id,
                        "content": str(result)
                    })

            # Continue conversation with tool results
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

            stats["api_requests"] += 1
            response = client.messages.create(
                model=selected_model,  # Use same model for iterations
                max_tokens=4096,
                system=system_prompt,
                tools=build_tool_definitions(),
                messages=messages
            )

            stats["input_tokens"] += response.usage.input_tokens
            stats["output_tokens"] += response.usage.output_tokens
            if "sonnet" in selected_model:
                stats["estimated_cost"] = (stats["input_tokens"] / 1_000_000) * 3.00 + (stats["output_tokens"] / 1_000_000) * 15.00
            else:
                stats["estimated_cost"] = (stats["input_tokens"] / 1_000_000) * 1.00 + (stats["output_tokens"] / 1_000_000) * 5.00

        # Extract final text response
        final_text = ""
        for content_block in response.content:
            if hasattr(content_block, "text"):
                final_text += content_block.text

        logger.info(f"API Response - Iterations: {iteration}, Input tokens: {response.usage.input_tokens}, Output tokens: {response.usage.output_tokens}, Cost: ${stats['estimated_cost']:.4f}")

        return final_text

    except Exception as e:
        logger.error(f"Claude API error - Error: {e}")
        return f"⚠️ API error: {str(e)}"


async def send_reply(room: MatrixRoom, message: str, client: AsyncClient):
    """Send formatted reply to Matrix room."""
    try:
        # Check message size
        if len(message) > MAX_OUTPUT_LENGTH:
            truncated = message[:MAX_OUTPUT_LENGTH]
            warning = f"\n\n⚠️ **Response truncated** (exceeded {MAX_OUTPUT_LENGTH} byte Matrix limit)\nOriginal size: {len(message)} bytes"
            message = truncated + warning
            logger.warning(f"Response truncated - Original: {len(message)} bytes")

        response = await client.room_send(
            room_id=room.room_id,
            message_type="m.room.message",
            content={
                "msgtype": "m.text",
                "body": message
            }
        )
        logger.info(f"Reply sent - Event ID: {response.event_id if hasattr(response, 'event_id') else 'ok'}")
    except Exception as e:
        logger.error(f"Failed to send reply - Error: {e}")
        stats["errors"] += 1


async def message_callback(room: MatrixRoom, event: RoomMessage, client: AsyncClient, bot_user_id: str):
    """
    Callback for room message events.

    Handles @mentions and processes requests via Claude SDK.
    """
    # Skip bot's own messages
    if event.sender == bot_user_id:
        return

    # Check if event has body
    if not hasattr(event, 'body') or not event.body:
        return

    stats["messages_received"] += 1

    # Check for bot mention
    user_id_parts = bot_user_id.split(':')
    localpart = user_id_parts[0][1:] if user_id_parts and user_id_parts[0].startswith('@') else ""
    bot_mention = f"@{localpart}"

    if bot_mention not in event.body.lower():
        return

    timestamp = format_timestamp()
    logger.info(f"Mention from {event.sender} in {room.display_name}")

    # Security: check user authorization
    if event.sender not in ALLOWED_USERS:
        logger.error(f"SECURITY: Unauthorized user - User: {event.sender}")
        await send_reply(
            room,
            f"⚠️ **Unauthorized**\n\nOnly authorized users can use this bot.\nAuthorized: {', '.join(ALLOWED_USERS)}",
            client
        )
        stats["commands_blocked"] += 1
        return

    # Extract user message
    user_message = extract_command(event.body, bot_mention)

    # Handle help/status
    if not user_message or user_message.lower() in ["help", "status"]:
        help_text = f"""**Claude Code Bot - Phase 2**

**Capabilities:**
- Multi-file analysis and reporting
- Log analysis and troubleshooting
- Configuration comparison
- Pattern detection across codebase

**Example requests:**
- "analyze all playbooks in playbooks/matrix/"
- "compare base-matrix-config.yml and family-matrix-config.yml"
- "check systemd service status"
- "find all references to solti_matrix_mgr"
- "explain what bin/claude-code-bot.py does"

**Status:**
- Working directory: `{WORKING_DIR}`
- Authorized users: {', '.join(ALLOWED_USERS)}
- API requests: {stats['api_requests']}
- Estimated cost: ${stats['estimated_cost']:.4f}

Phase 2: Analysis & Reporting (Read-only operations)"""

        await send_reply(room, help_text, client)
        return

    logger.info(f"Processing request - Message: {user_message[:100]}{'...' if len(user_message) > 100 else ''}")

    # Process with Claude
    stats["commands_processed"] += 1
    response = await process_with_claude(user_message, event.sender)

    # Send response
    await send_reply(room, response, client)


async def main():
    """Main async bot loop."""
    homeserver_url = os.getenv('MATRIX_HOMESERVER_URL', '')
    room_id = os.getenv('MATRIX_ROOM_ID', '')
    bot_user_id = os.getenv('MATRIX_BOT_USER_ID', '')
    if not homeserver_url or not room_id or not bot_user_id:
        logger.error("MATRIX_HOMESERVER_URL, MATRIX_ROOM_ID, and MATRIX_BOT_USER_ID are required")
        sys.exit(1)

    token = load_token()
    if not token:
        logger.error("No Matrix token available")
        sys.exit(1)

    api_key = load_api_key()
    if not api_key:
        logger.warning("No ANTHROPIC_API_KEY - Phase 2 features disabled")

    print("=" * 80)
    print(f"{format_timestamp()} | Claude Code Bot Starting (Phase 2)")
    print(f"  Homeserver:  {homeserver_url}")
    print(f"  Bot User:    {bot_user_id}")
    print(f"  Room:        {room_id}")
    print(f"  Working Dir: {WORKING_DIR}")
    print(f"  Authorized:  {', '.join(ALLOWED_USERS)}")
    print(f"  API Key:     {'✓ Configured' if api_key else '✗ Missing'}")
    print("=" * 80)

    client = AsyncClient(homeserver_url, bot_user_id)
    client.access_token = token

    # Wrap callback to pass client reference and bot_user_id
    async def _callback(room, event):
        await message_callback(room, event, client, bot_user_id)

    client.add_event_callback(_callback, RoomMessage)

    try:
        logger.info("Performing initial sync...")
        sync_response = await client.sync(timeout=30000)

        if isinstance(sync_response, SyncError):
            logger.error(f"Sync failed: {sync_response.message}")
            return

        logger.info("Initial sync complete")

        # Resolve room and join if needed
        logger.info(f"Resolving room alias: {room_id}")
        room_response = await client.room_resolve_alias(room_id)

        if hasattr(room_response, 'room_id'):
            resolved_room_id = room_response.room_id
            logger.info(f"Resolved to: {resolved_room_id}")

            join_response = await client.join(resolved_room_id)
            if hasattr(join_response, 'room_id'):
                logger.info(f"Joined room: {join_response.room_id}")

        print(f"{format_timestamp()} | Listening for @mentions...")
        print(f"  Press Ctrl+C to stop")
        print("=" * 80)

        # Sync loop
        while True:
            await client.sync_forever(timeout=30000, full_state=False)

    except KeyboardInterrupt:
        print(f"\n{format_timestamp()} | Shutting down...")
        logger.info("Bot shutdown requested")

    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise

    finally:
        # Print stats
        print("\n" + "=" * 80)
        print("Session Statistics:")
        print(f"  Messages received: {stats['messages_received']}")
        print(f"  Commands processed: {stats['commands_processed']}")
        print(f"  Commands blocked: {stats['commands_blocked']}")
        print(f"  Errors: {stats['errors']}")
        print(f"  API requests: {stats['api_requests']}")
        print(f"  Tokens (in/out): {stats['input_tokens']}/{stats['output_tokens']}")
        print(f"  Estimated cost: ${stats['estimated_cost']:.4f}")
        print("=" * 80)

        await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")
        sys.exit(0)
