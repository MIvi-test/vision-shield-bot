# Repository Guidelines

## Project Structure & Module Organization

This MAX photo moderation service contains documentation, scaffolding, Python development tooling, CI, and the MAX emulator v0 with tests. Backend, workers, frontend manifests, and Compose are not implemented yet.

- `docs/ARCHITECTURE.md`: architecture entry point; `docs/sprints.md`: task IDs and acceptance criteria.
- `apps/`: planned `backend/`, `max-emulator/`, `media-worker/`, `cv-worker/`, and `moderator-ui/` applications.
- `packages/contracts/`: planned event schemas, panel OpenAPI, moderation policies, and anonymized MAX fixtures.
- `infra/`: infrastructure configuration; the planned `compose.yaml` belongs at repository root.
- `tests/`: planned integration and contract scenarios. Keep test images synthetic or openly licensed and record their sources.

Create directories as implementation begins.

## Build, Test, and Development Commands

- `cp .env.example .env`: prepare local configuration; fill credentials locally.
- `git diff --check`: check whitespace before submitting changes.
- `git status --short`: inspect changed and untracked files.
- `gitleaks git . --log-opts="--all" --redact --no-banner --exit-code=1`: scan committed history with Gitleaks 8.30.1.

Launch the standalone emulator with `uv run uvicorn max_emulator:create_app --factory --app-dir apps/max-emulator --host 127.0.0.1 --port 8081`; see `apps/max-emulator/README.md`. Compose arrives in S1-03. Install Python dependencies with `uv sync --locked`, then use `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, and `uv run pytest`. SDK integration tests bind only to a random localhost port. Frontend CI will run `npm ci` and `npm run build` in `apps/moderator-ui` once its package manifest exists; the lockfile and build script must arrive with it.

## Coding Style & Naming Conventions

Use four-space indentation and `snake_case` for Python modules and functions; use `PascalCase` for classes. Follow Ruff formatting once configured. The planned frontend uses React and TypeScript; keep component names in `PascalCase` and follow its formatter when introduced. Keep Markdown concise and update affected architecture documents alongside implementation.

## Testing Guidelines

Use pytest for Python and contract tests; Vitest is planned for the frontend. Name Python tests `test_*.py` and test functions `test_*`. No numerical coverage threshold is defined. Prioritize duplicate-event handling, contract validation, authorization, file limits, and failure recovery per `docs/testing.md`. Contract changes require checks on both interacting components.

## Commit & Pull Request Guidelines

History contains Russian and English summaries, including `docs: add architecture, MVP and sprint plan`; no strict commit convention is established. Use concise, descriptive messages and task branches such as `s1-05-max-emulator`.

Use the PR template: reference the task, explain behavior, report checks, and identify risks. Select a reviewer manually and obtain one teammate approval; administrators can bypass protection. CODEOWNERS is deferred. Gitleaks, Python checks, and Frontend build run on push and PR; making them required checks needs separate GitHub configuration. S1-02 remains open until merge blocking is verified and real tests and frontend build exist.

## Security & Architecture Boundaries

Never commit `.env`, credentials, personal data, or signed URLs. Keep `.env.example` limited to placeholders. The emulator imitates MAX APIs; Backend owns bot replies and submission state. Media and CV must not receive database credentials or MAX tokens; their target deployment has no internet access. Follow `docs/security.md`.
