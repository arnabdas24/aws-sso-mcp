# AGENTS.md

Guidance for AI coding agents (Copilot, Claude Code, Cursor, Codex, …).

Read, in order:
1. [.github/copilot-instructions.md](.github/copilot-instructions.md) — commands, architecture, invariants
2. [.github/instructions/python.instructions.md](.github/instructions/python.instructions.md) — Python style and design
3. [.github/instructions/security.instructions.md](.github/instructions/security.instructions.md) — security rules
3a. [.github/instructions/solution-design.instructions.md](.github/instructions/solution-design.instructions.md) — design process, MCP tool design, resilience, STRIDE
4. [.github/instructions/code-reviewer.instructions.md](.github/instructions/code-reviewer.instructions.md) — when reviewing
5. [docs/developer-guide.md](docs/developer-guide.md) — background
6. [docs/adr/README.md](docs/adr/README.md) — why the load-bearing decisions were made
7. [SECURITY.md](SECURITY.md) — threat model and reporting policy

Before finishing: `ruff check . && ruff format --check . && mypy --strict src && pytest && vulture`.
