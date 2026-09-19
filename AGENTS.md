# AGENTS.md

## Project
Repository: `zageabb/flask_chat`

This file is the persistent working agreement for ChatGPT, Codex, and other coding agents operating on this repository.

## Start here
Before changing code:
1. Read this file.
2. Read the repository README and relevant documentation.
3. Read `TODO.md`, `DESIGN.md`, roadmap, phase, audit, and development notes when present.
4. Inspect the existing implementation before proposing replacement architecture.
5. Continue the next incomplete task or phase unless the user explicitly asks for something else.

## Development rules
- Preserve the existing architecture, UI conventions, and working behaviour unless a change is required.
- Prefer extending existing modules over rewriting working code.
- Keep modules focused and independently testable; avoid unnecessary monolithic files.
- Maintain backwards compatibility where practical.
- Keep business logic separate from UI, storage, integration, and transport layers.
- Do not hard-code passwords, tokens, API keys, server addresses, ports, or environment-specific paths when configuration can be used.
- Put secrets in environment/configuration mechanisms and never commit production secrets.
- Keep configuration explicit and documented.
- Update README/docs/TODO when implementation changes make them inaccurate.
- Clearly mark scaffolds, placeholders, limitations, and unfinished features.

## Reuse before duplication
Before building a capability from scratch, inspect relevant existing repositories and reuse proven patterns or modules where appropriate, especially:
- `context-studio`
- `general-search`
- `tender_designer`
- `should-cost-intelligence`
- `should-cost-price-estimator`
- `system-knowledge-designer`
- `olladex`
- `AI_Spreadsheet`

Do not copy code blindly when a shared abstraction or adaptation is cleaner.

## Testing and quality
- Run the relevant automated tests before committing.
- Add or update tests for material behaviour changes.
- Run build, lint, type-check, migration, or validation commands used by this repository when available.
- Do not claim a feature is complete if tests fail or only a scaffold exists.
- Fix regressions introduced by the change before moving on.

## Git workflow
- Verify the default branch before acting.
- Do not force-push the default branch.
- Do not rewrite published history unless the user explicitly requests it.
- Keep commits focused and use clear commit messages.
- Do not push a change known to fail relevant tests/build unless explicitly requested as work in progress.

## Deployment
Where this project is server-hosted, GitHub may feed automatic deployment to an Ubuntu server, often through Docker or a service.
- Inspect the repository's real deployment configuration before changing it.
- Preserve existing ports, volumes, environment variables, health checks, and service names unless the requested change requires otherwise.
- Do not assume every repository is Dockerised.
- Keep local development possible where the existing project supports it.

## Agent behaviour
- Make the smallest coherent change that fully satisfies the task.
- Prefer implementation over speculative redesign.
- Use repository evidence as the source of truth.
- If documentation and code disagree, identify the mismatch and update the appropriate source.
- Do not invent completed work, test results, files, endpoints, or integrations.
- When work spans phases, complete and verify the current phase before starting the next.
