#!/bin/bash
#
# manage-bot.sh - Manage Matrix bots using dynamically generated Ansible playbooks
#
# Usage: manage-bot.sh [-i INVENTORY] [-h HOST] [-y] <bot> <action>
#
# Example:
#   manage-bot.sh matrix-watcher prepare
#   manage-bot.sh -h monitor11 claude-code-bot deploy
#   manage-bot.sh -i inventory/remote.yml matrix-watcher deploy
#   manage-bot.sh matrix-watcher remove

# Exit on error
set -e

# Configuration
ANSIBLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INVENTORY="${SOLTI_INVENTORY:-${ANSIBLE_DIR}/inventory/localhost.yml}"
TEMP_DIR="${ANSIBLE_DIR}/tmp"
HOST=""
YES_FLAG=false
EXPLICIT_INVENTORY=false
EXTRA_VARS=()

# Ensure temp directory exists
mkdir -p "${TEMP_DIR}"

# Supported bots (map CLI names to role names)
declare -A BOT_MAP
BOT_MAP["matrix-watcher"]="matrix_watcher"
BOT_MAP["claude-code-bot"]="claude_code_bot"
BOT_MAP["brain2-bot"]="brain2_bot"
BOT_MAP["card-capture-bot"]="card_capture_bot"
BOT_MAP["salty-bot"]="salty_bot"

SUPPORTED_BOTS=("matrix-watcher" "claude-code-bot" "brain2-bot" "card-capture-bot" "salty-bot")

# Supported actions
SUPPORTED_ACTIONS=(
    "prepare"
    "deploy"
    "remove"
    "verify"
)

# Map actions to state values
declare -A STATE_MAP
STATE_MAP["prepare"]="prepare"
STATE_MAP["deploy"]="present"
STATE_MAP["remove"]="absent"
STATE_MAP["verify"]="verify"

# Display usage information
usage() {
    echo "Usage: $(basename "$0") [-i INVENTORY] [-h HOST] [-y] <bot> <action> [options]"
    echo ""
    echo "Options:"
    echo "  -i INVENTORY     - Path to inventory file (default: \$SOLTI_INVENTORY or inventory/localhost.yml)"
    echo "  -h HOST          - Target specific host (default: uses bot group from inventory)"
    echo "  -y, --yes        - Skip safety prompts (for automation)"
    echo "  -e VAR=VALUE     - Set extra variables (can be used multiple times)"
    echo ""
    echo "Bots:"
    for bot in "${SUPPORTED_BOTS[@]}"; do
        echo "  - $bot"
    done
    echo ""
    echo "Actions:"
    for action in "${SUPPORTED_ACTIONS[@]}"; do
        echo "  - $action"
    done
    echo ""
    echo "Examples:"
    echo "  $(basename "$0") matrix-watcher prepare"
    echo "  $(basename "$0") matrix-watcher deploy"
    echo "  $(basename "$0") claude-code-bot deploy"
    echo "  $(basename "$0") -h monitor11 claude-code-bot deploy"
    echo "  $(basename "$0") matrix-watcher verify"
    echo "  $(basename "$0") matrix-watcher remove"
    echo "  DELETE_DATA=true $(basename "$0") matrix-watcher remove  # Delete bot data"
    exit 1
}

# Check if a bot is supported
is_bot_supported() {
    local bot="$1"
    for b in "${SUPPORTED_BOTS[@]}"; do
        if [[ "$b" == "$bot" ]]; then
            return 0
        fi
    done
    return 1
}

# Check if an action is supported
is_action_supported() {
    local action="$1"
    for act in "${SUPPORTED_ACTIONS[@]}"; do
        if [[ "$act" == "$action" ]]; then
            return 0
        fi
    done
    return 1
}

# Get execution context (local or remote)
get_execution_context() {
    if [[ -n "$HOST" ]]; then
        echo "remote"
    else
        echo "local"
    fi
}

# Safety prompt for destructive actions
safety_prompt() {
    local bot="$1"
    local action="$2"
    local context="$3"

    # Skip if -y flag provided
    if [[ "$YES_FLAG" == true ]]; then
        return 0
    fi

    # Only prompt for deploy/remove in certain contexts
    if [[ "$action" == "remove" ]]; then
        echo "WARNING: About to REMOVE bot: $bot"
        if [[ -n "${DELETE_DATA}" ]]; then
            echo "         DELETE_DATA is set - bot data will be deleted!"
        fi
        echo "         Context: $context"
        if [[ -n "$HOST" ]]; then
            echo "         Target host: $HOST"
        fi
        echo ""
        read -p "Continue? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo "Aborted."
            exit 1
        fi
    fi
}

# Generate dynamic playbook
generate_playbook() {
    local bot="$1"
    local action="$2"
    local role_name="${BOT_MAP[$bot]}"
    local state="${STATE_MAP[$action]}"

    # Determine task file for verify action
    local task_file="main.yml"
    if [[ "$action" == "verify" ]]; then
        task_file="_bot_base/tasks/verify.yml"
    fi

    # Generate playbook content
    local playbook_content
    if [[ -n "$HOST" ]]; then
        # Target specific host
        playbook_content="---
- name: Manage ${bot} bot
  hosts: ${HOST}
  vars:
    ${role_name}_state: ${state}
  roles:"
        if [[ "$action" == "verify" ]]; then
            playbook_content+="
    - role: ${role_name}
      tasks_from: ../../../_bot_base/tasks/verify.yml"
        else
            playbook_content+="
    - role: ${role_name}"
        fi
    else
        # Use inventory group
        playbook_content="---
- name: Manage ${bot} bot
  hosts: ${role_name}_svc
  vars:
    ${role_name}_state: ${state}
  roles:"
        if [[ "$action" == "verify" ]]; then
            playbook_content+="
    - role: ${role_name}
      tasks_from: ../../../_bot_base/tasks/verify.yml"
        else
            playbook_content+="
    - role: ${role_name}"
        fi
    fi

    echo "$playbook_content"
}

# Parse command-line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -i|--inventory)
            INVENTORY="$2"
            EXPLICIT_INVENTORY=true
            shift 2
            ;;
        -h|--host)
            HOST="$2"
            shift 2
            ;;
        -y|--yes)
            YES_FLAG=true
            shift
            ;;
        -e|--extra-vars)
            EXTRA_VARS+=("-e" "$2")
            shift 2
            ;;
        -*)
            echo "Unknown option: $1"
            usage
            ;;
        *)
            break
            ;;
    esac
done

# Require at least 2 positional arguments (bot, action)
if [[ $# -lt 2 ]]; then
    echo "Error: Missing required arguments"
    usage
fi

BOT="$1"
ACTION="$2"
shift 2  # Remaining args can be extra ansible-playbook options

# Validate bot
if ! is_bot_supported "$BOT"; then
    echo "Error: Unsupported bot '$BOT'"
    echo "Supported bots: ${SUPPORTED_BOTS[*]}"
    exit 1
fi

# Validate action
if ! is_action_supported "$ACTION"; then
    echo "Error: Unsupported action '$ACTION'"
    echo "Supported actions: ${SUPPORTED_ACTIONS[*]}"
    exit 1
fi

# Check inventory exists
if [[ ! -f "$INVENTORY" ]]; then
    echo "Error: Inventory file not found: $INVENTORY"
    exit 1
fi

# Get execution context
CONTEXT=$(get_execution_context)

# Safety prompt
safety_prompt "$BOT" "$ACTION" "$CONTEXT"

# Generate playbook
TEMP_PLAYBOOK="${TEMP_DIR}/manage-${BOT}-${ACTION}-$$.yml"
generate_playbook "$BOT" "$ACTION" > "$TEMP_PLAYBOOK"

echo "=== Managing Bot: $BOT ==="
echo "Action: $ACTION"
echo "Context: $CONTEXT"
if [[ -n "$HOST" ]]; then
    echo "Target Host: $HOST"
fi
echo "Inventory: $INVENTORY"
echo "Playbook: $TEMP_PLAYBOOK"
echo ""

# Execute playbook
ansible-playbook \
    -i "$INVENTORY" \
    "${EXTRA_VARS[@]}" \
    "$TEMP_PLAYBOOK" \
    "$@"

RESULT=$?

# Cleanup temp playbook
rm -f "$TEMP_PLAYBOOK"

if [[ $RESULT -eq 0 ]]; then
    echo ""
    echo "=== Bot Management Complete: $BOT ($ACTION) ==="
else
    echo ""
    echo "=== Bot Management Failed: $BOT ($ACTION) ==="
    exit $RESULT
fi
