# Changelog

All notable changes to this collection will be documented here.

## [0.1.0] - 2026-05-05

### Added
- Initial collection release
- `_bot_base` role: shared bot infrastructure (venv, systemd user services, directory setup)
- `matrix_watcher` role: event validation bot using matrix-nio
- `claude_code_bot` role: AI analysis assistant using matrix-nio and Anthropic SDK
- `brain2_bot` role: second brain classifier with MongoDB capture
- State-driven lifecycle: prepare → deploy → verify → remove
- `manage-bot.sh` dynamic playbook generator
- Support for local and remote host deployments
