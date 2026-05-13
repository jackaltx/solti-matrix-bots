# solti-matrix-bots: Project Assessment (2026-05-12)

## Executive Summary

The `solti-matrix-bots` project is a mature, self-contained Ansible collection designed for the deployment and lifecycle management of Matrix-based AI and utility bots. It follows the "App, not Library" philosophy, providing its own inventory, configuration, and a wrapper script (`manage-bot.sh`) that abstracts away Ansible complexity for the user.

The project currently supports five bots ranging from simple loggers to sophisticated "Second Brain" capture tools using Claude AI, MongoDB, and S3-compatible storage.

## Architectural Assessment

### 1. Deployment Model ("App, not Library")
- **Strengths**: High portability, minimal external dependencies (only requires Ansible/Python/Systemd), and a consistent user experience via `manage-bot.sh`.
- **Implementation**: Uses `ansible.cfg` and a named-host inventory (`inventory/localhost.yml`) to make "firefly" (localhost) the default target.

### 2. Lifecycle Management (`_bot_base`)
- **Pattern**: A standardized lifecycle (`prepare` -> `deploy` -> `verify` -> `remove`) ensures consistency across all bots.
- **Efficiency**: All bots share a single Python virtual environment (`~/matrix-bots/venv`) on the host, reducing disk usage and setup time.
- **Process Management**: Bots run as systemd user services, providing process isolation and automatic restarts without requiring root privileges.

### 3. Configuration & Security
- **Externalization**: Domain settings reside in `all.yml` (gitignored), while secrets are sourced from a shell environment file (`~/.secrets/LabMatrix`).
- **Authorization**: All AI-enabled bots implement a `matrix_allowed_users` whitelist, preventing unauthorized interaction.
- **Secret Handling**: Secrets are resolved at deploy time and baked into systemd units via `EnvironmentFile` or `Environment` directives, keeping them out of source control and Ansible logs.

## Bot Portfolio

| Bot | Purpose | Tech Stack |
| :--- | :--- | :--- |
| **`matrix-watcher`** | Infrastructure validation | `matrix-nio` |
| **`claude-code-bot`** | AI Assistant (Read-only) | `matrix-nio`, Anthropic SDK |
| **`brain2-bot`** | Classifier / Capture | `matrix-nio`, Claude, MongoDB |
| **`card-capture-bot`**| Business Card Scanner | `matrix-nio`, Claude Vision, S3, MongoDB |
| **`salty-bot`** | Voice-First Capture | `matrix-nio`, Claude, S3, MongoDB |

## Documentation Review

The documentation is high-quality and distributed across several layers:
- **`CLAUDE.md`**: Technical "source of truth" for developers and AI assistants.
- **`README.md`**: User-facing entry point with quick-start and architecture overview.
- **`docs/`**: Deep-dive design documents (notably for `card-capture` and `salty-bot`) that explain the "why" behind complex UX decisions.
- **Role READMEs**: Targeted instructions for individual bot deployment.

## Observed Gaps & Recommendations

1. **Bot Registration Consistency**: `CLAUDE.md` currently only lists three bots (`matrix-watcher`, `claude-code-bot`, `brain2-bot`), missing the newer `card_capture_bot` and `salty_bot`. *Recommendation: Sync CLAUDE.md with the full role list.*
2. **Persistence Gaps**: As noted in `salty-multi-media-bot.md`, `salty-bot` sessions are currently in-memory. A bot restart during an open session leads to data loss. *Recommendation: Implement MongoDB-backed session persistence.*
3. **Integration Hub Potential**: The project has successfully moved toward a "MongoDB as hub" model. Further integration between `people` (from card-capture) and `ideas` (from salty) would create a powerful knowledge graph.
4. **Testing**: While the lifecycle includes a `verify` state, there is no mention of automated integration tests (e.g., Molecule) for the roles themselves in the main documentation.

## Conclusion

The project is well-architected, following modern infrastructure-as-code patterns while maintaining extreme ease of use. The transition from simple bots to a multi-bot "Second Brain" ecosystem is well-supported by the `_bot_base` abstraction and the shared MongoDB/S3 storage backend.
