---
name: git-hygiene-orchestrator
description: "Audit and clean Git branches, remote-tracking refs, and worktrees safely. Use when Codex is asked to inspect Git hygiene, stale branches, merged branches, remote refs, worktrees, dirty work, merge conflicts, branch cleanup, pruning, repository cleanup commands, or when the task should be finished through validation, commit, push, pull request creation, and merge if gates allow."
---

# Git Hygiene Orchestrator

## Overview

Audit Git branches, remote-tracking refs, and worktrees before recommending or executing cleanup. Protect uncommitted work, detect conflicts, classify stale or merged branches using evidence, and separate low-risk metadata pruning from higher-risk branch, worktree, and remote deletion. Default to audit-first and conservative.

## Workflow

- Read `.codex/skills/git-hygiene-orchestrator/SKILL.md` before acting.
- Start by reading repository instructions such as `AGENTS.md`, `CONTRIBUTING.md`, `.codex/`, and `.github/` when present.
- Run read-only inventory commands to gather branch, worktree, and remote state before proposing any action.
- Classify branches as Keep, Delete local, Review, or High risk based on merge status, worktree usage, and unique commits.
- Group cleanup into risk tiers: safe metadata pruning, local branch deletion, worktree removal, remote branch deletion.
- Report the proposed cleanup grouped by risk before executing any destructive step.
- Execute safe metadata pruning first (`git worktree prune`, `git remote prune`), then proceed to higher-risk actions only with explicit user approval.
- Re-run verification commands after cleanup to confirm the result.

## Finish Workflow

When given a blanket finish approval or "finish it", run: validate/test, stage task-owned files, commit, fetch+rebase, push branch, open pull request, merge only when branch protection and required checks allow.

## Hard Safety Rules

- Do not run `git reset --hard`, `git clean`, `git branch -D`, or `git push --force` without explicit user approval of that exact action.
- Do not delete branches checked out by any active worktree.
- Do not remove worktrees with dirty, staged, untracked, or conflicted files without explicit approval.
- Prefer `git branch -d` over `git branch -D`.
- Treat remote branch deletion as higher risk than local deletion.

## Resources

- `.codex/skills/git-hygiene-orchestrator/SKILL.md` — canonical definition including inventory commands, worktree audit steps, conflict resolution workflow, branch analysis criteria, cleanup rules, and pull request defaults.
