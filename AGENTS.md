# AGENTS.md instructions for trading-card-scanner

## Source of Truth

Repo-local agents live under `.codex/skills`. Keep `.agent/skills` and `.claude/agents` in sync with that inventory when updating repo-local agent prompts.

This repo intentionally excludes trading and investment-specific agents from the source inventory. Do not add domain agents for portfolio risk, execution quality, market regimes, trade thesis drift, trading surveillance, market-data corporate actions, catalyst calendars, or strategy/model-risk review unless the user explicitly asks for them.

## Service and Process Defaults

Do not add feature flags, enable/disable toggles, kill switches, dry-run gates, rollout flags, or env-controlled process gates unless the user explicitly requests that control.

Services, jobs, workers, routes, integrations, and shared contracts are enabled by default when added. Missing credentials, identity, endpoints, roles, or provider configuration should fail fast instead of silently disabling behavior.

If temporary rollout control is explicitly requested, include a named owner, expiry or removal condition, tests, and documentation in the same change.

## Shared Contract Routing

Before editing shared API, schema, serialization, or mirrored contract shapes, identify the owning source of truth. State whether the change is local-only or requires upstream contract/source changes first.

Default to local-only only when repository evidence shows the shape is internal to this project.

## How to Use Agents

- Discovery: repo-local agents live under `.codex/skills`.
- Trigger rules: if the user names an agent or the task clearly matches one, use the minimal set that covers the request.
- Coordination: use `delivery-orchestrator-agent` for planning/routing and `gateway-bookkeeper` only for auditable multi-repo, PR, CI/CD, deployment, Azure DevOps, or tracked-work status.
- Output discipline: visible agent output should stay terse. Default to one compact start note and one compact completion note; expand only when blocked, risky, explicitly requested, or mutating external tracking systems.
- Safety: apply `strict-branch-and-merge-discipline` before edits, branches, commits, pushes, or PRs.
