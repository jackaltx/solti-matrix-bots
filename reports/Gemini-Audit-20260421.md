# Project Audit: solti-matrix-bots
**Date:** 2026-04-21
**Auditor:** Gemini CLI
**Status:** Initial Review & Private Repo Initialization

---

## 1. Executive Summary

The `solti-matrix-bots` collection provides a robust, standardized framework for deploying and managing Matrix bots using Ansible. It successfully implements the `solti-containers` pattern of state-driven lifecycles (prepare, present, verify, absent) applied to systemd user services.

The architecture is modular, with a shared `_bot_base` role handling common infrastructure (virtual environments, systemd templates, directory structures) and bot-specific roles defining unique properties and scripts.

---

## 2. Key Findings

### 2.1 Architecture & Lifecycle
- **Unified Wrapper**: `manage-bot.sh` effectively abstracts complex Ansible commands into a simple CLI, providing a consistent UX for both local and remote deployments.
- **Shared Infrastructure**: The use of a shared Python virtual environment (`~/matrix-bots/venv`) per host is an excellent design choice for resource efficiency and simplified dependency management.
- **Idempotency**: The `present` state in `_bot_base` correctly uses systemd's `daemon_reload` and conditional restarts to ensure idempotent deployments.

### 2.2 Security Model
- **Secret Management**: Correctly utilizes `EnvironmentFile` in systemd services to load secrets from `~/.secrets/LabMatrix`, keeping sensitive tokens out of the repository.
- **Domain Abstraction**: The use of gitignored `group_vars/all.yml` for domain-specific settings ensures that the collection remains generic and safe for public (or private) distribution.
- **User Isolation**: Running bots as systemd user services (`--user`) minimizes the need for elevated privileges and provides good isolation.

### 2.3 Bot Implementations
- **matrix-watcher**: Well-implemented layered protocol (Transport → Envelope → Content) for validating SOLTI events.
- **claude-code-bot**: Sophisticated AI assistant using the Anthropic Claude SDK with tool integration. However, it currently contains more hardcoded configuration than the watcher bot.

---

## 3. Recommendations

### 3.1 High Priority: Configuration Externalization
The `claude-code-bot.py` script currently hardcodes `WORKING_DIR` and `ALLOWED_USERS`.
- **Recommendation**: Update the script to use `os.getenv('MATRIX_WORKING_DIR')` and `os.getenv('MATRIX_ALLOWED_USERS')`.
- **Action**: Pass these variables through the `environment` dictionary in `roles/claude_code_bot/defaults/main.yml`.

### 3.2 Medium Priority: Documentation Fulfillment
The `docs/` directory is currently empty, though referenced in the README.
- **Recommendation**: Create `docs/Bot-Management.md` to detail:
    - Procedures for adding new bots.
    - Troubleshooting common systemd user service issues.
    - Security guidelines for the `ALLOWED_USERS` whitelist.

### 3.3 Medium Priority: Variable Harmonization
There is slight inconsistency in environment variable naming (e.g., `MATRIX_WATCHER_TOKEN` vs `MATRIX_SOLTI_CLAUDE_CODE_TOKEN`).
- **Recommendation**: Standardize a naming convention for tokens and configuration variables to make the `LabMatrix` secrets file easier to manage.

### 3.4 Low Priority: Validation Logic
While the roles validate required configuration (domain, homeserver), the validation for `bot_properties` could be more exhaustive in `_bot_base`.
- **Recommendation**: Add assertions in `_bot_base/tasks/prepare.yml` to verify all required keys in the `bot_properties` dictionary are present before execution.

---

## 4. Completed Actions
- [x] Comprehensive review of collection structure and roles.
- [x] Initialized local git repository.
- [x] Created private GitHub repository: `jackaltx/solti-matrix-bots`.
- [x] Pushed initial codebase to `main` branch.

---
**End of Report**
