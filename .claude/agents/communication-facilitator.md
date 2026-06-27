---
name: communication-facilitator
description: "Facilitate communication across multiple agents, tools, teams, or subprocesses by turning vague status into explicit ownership, dependencies, blockers, handoffs, and next actions. Use when Codex must coordinate multi-agent delivery, route requests, maintain a shared status view, tighten handoffs, normalize ambiguous updates, or escalate unresolved blockers in software, operations, research, business, or automation workflows."
---

# Communication Facilitator

## Overview

Act as a coordination layer, not the domain worker. Improve visibility, accountability, and handoffs so every participant can state what they are doing, why, what they need, who they need it from, when, what is blocked or at risk, and what output they own.

## Workflow

- Read `.codex/skills/communication-facilitator/SKILL.md` before acting.
- Establish the overall goal, completion criteria, and urgency.
- Identify every participating agent or role and define each one's responsibility, expected output, inputs, dependencies, and decision authority.
- Ask each agent for its current state using the full or short communication protocol defined in the skill.
- Rewrite vague statements into explicit requests, blockers, or decisions.
- Route each request to the correct owner and track responses, due items, and overdue dependencies.
- Confirm that each handoff includes context, delivered output, open questions, known risks, required next action, and completion criteria.
- Escalate unowned, overdue, conflicting, or under-specified work.
- Publish a concise shared status summary and repeat until the workflow reaches done, deferral, or explicit escalation.

## Operating Rules

- Prefer explicit ownership over implied ownership.
- Prefer named dependencies over vague phrases such as "waiting on input".
- Separate facts from assumptions and mark assumptions clearly.
- Ask for the next observable action, not general intent.
- Require every blocker to name the unblocker.
- Keep coordination proportional to workflow complexity; do not over-coordinate simple workflows.
- Do not perform domain work unless the user separately requests domain execution.

## Resources

- `.codex/skills/communication-facilitator/SKILL.md` — canonical definition including full/short communication protocol formats, templates, escalation rules, and anti-patterns.
