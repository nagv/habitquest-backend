---
type: "Reference"
title: "HabitQuest backend wiki: start here"
openwiki_generated: true
verified:
  - by: openwiki/0.7.2
    at: 2026-10-30T00:13:47.181Z
sources:
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-f70156010a8eb5325870bfc6
    resource: repo://render.yaml
  - id: openwiki-source-1d39ca689a3e1562bb21214c
    resource: repo://src/habitquest_backend/config.py
  - id: openwiki-source-70663512997ed0573d9f7f78
    resource: repo://src/habitquest_backend/main.py
  - id: openwiki-source-6e803a3ec70ccc9d4eb9e11b
    resource: repo://src/habitquest_backend/worker.py
generated: { by: "openwiki/0.7.2", at: "2026-10-30T00:13:47.181Z" }
---


# HabitQuest backend wiki: start here

HabitQuest onboarding is a Python/FastAPI backend that turns a parent's description of a child's pain points into a ranked, agent-picked, parent-confirmed habit plan. Every suggested habit comes from a versioned, two-person-reviewed catalog that editors maintain through a separate workflow.

**`MODEL_MODE=fake` is the default.** It is a deterministic, rule-based test double that exercises the real LangGraph pipeline and catalog tools without making any outbound model calls. Most development and testing needs no provider key.

## What you are working with

Three systems share one PostgreSQL database. There is no message broker and no in-process call from the API to the planner.

| System | Runs as | Owns |
|---|---|---|
| FastAPI request process | `uvicorn habitquest_backend.main:app` | Authentication, HTTP contracts, household writes, queue writes, parent edits and confirmation |
| Durable planning worker | `python -m habitquest_backend.worker` (or a thread inside the API) | Claiming jobs, running the model pipeline, publishing proposals |
| Postgres/Supabase schema | One database, three service roles (`hq_api`, `hq_worker`, `hq_editor`) | All durable state: household rows, queue rows, plan documents, catalog drafts, published releases, LangGraph checkpoints |

<!-- openwiki: broken internal link [../architecture/system-overview.md] file "../architecture/system-overview.md" does not exist. Fix the href or restore the target, then delete this comment. -->
For the full component map and module boundaries, see [System overview and component map](../architecture/system-overview.md).

## Route by intent

- **I want to understand the architecture and components**
<!-- openwiki: broken internal link [../architecture/system-overview.md] file "../architecture/system-overview.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [System overview and component map](../architecture/system-overview.md)

- **I want to add or change an API endpoint**
<!-- openwiki: broken internal link [../architecture/system-overview.md] file "../architecture/system-overview.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../architecture/authentication-and-authorization.md] file "../architecture/authentication-and-authorization.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [System overview and component map](../architecture/system-overview.md) for module boundaries; [Authentication, database roles and RLS boundaries](../architecture/authentication-and-authorization.md) for route-level authorization

- **I want to change planning rules, scoring, or agent validation**
<!-- openwiki: broken internal link [../concepts/catalog-and-eligibility.md] file "../concepts/catalog-and-eligibility.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../concepts/planning-agents.md] file "../concepts/planning-agents.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Catalog model, eligibility, scoring and template coverage](../concepts/catalog-and-eligibility.md) for deterministic narrowing and scoring; [Agent roles, prompts and code-enforced pick validation](../concepts/planning-agents.md) for the Deep Agents graph and fallback logic

- **I want to understand the catalog editorial workflow**
<!-- openwiki: broken internal link [../workflows/editorial-catalog-release.md] file "../workflows/editorial-catalog-release.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Workflow: two-person catalog review, release and withdrawal](../workflows/editorial-catalog-release.md)

- **I want to understand the parent journey from onboarding to confirmed plan**
<!-- openwiki: broken internal link [../workflows/parent-planning-run.md] file "../workflows/parent-planning-run.md" does not exist. Fix the href or restore the target, then delete this comment. -->
<!-- openwiki: broken internal link [../workflows/proposal-edits-and-confirmation.md] file "../workflows/proposal-edits-and-confirmation.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Workflow: parent onboarding, intake and a confirmed plan](../workflows/parent-planning-run.md) for the end-to-end flow; [Workflow: proposal edits, publication rules and confirmation](../workflows/proposal-edits-and-confirmation.md) for the seven edit operations and revalidation rules

- **I want to deploy or bootstrap locally**
<!-- openwiki: broken internal link [../operations/deployment-and-migrations.md] file "../operations/deployment-and-migrations.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Local bootstrap, migrations and Render deployment](../operations/deployment-and-migrations.md)

- **I want to debug a stuck planning run**
<!-- openwiki: broken internal link [../architecture/runtime-and-processes.md] file "../architecture/runtime-and-processes.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Runtime topology: API process, worker loop and job leases](../architecture/runtime-and-processes.md) for claim, lease, heartbeat, deadline and retry

- **I want to run the test suite**
<!-- openwiki: broken internal link [../testing/verification-strategy.md] file "../testing/verification-strategy.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Tests, coverage gate, pgTAP and the demo harness](../testing/verification-strategy.md)

- **I want to understand or change settings**
<!-- openwiki: broken internal link [../operations/configuration.md] file "../operations/configuration.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Settings and environment configuration](../operations/configuration.md)

- **I want to understand the data model and migrations**
<!-- openwiki: broken internal link [../architecture/data-model-and-persistence.md] file "../architecture/data-model-and-persistence.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Data model, migrations and persistence](../architecture/data-model-and-persistence.md)

- **I want to add a model provider or understand LangGraph persistence**
<!-- openwiki: broken internal link [../integrations/model-providers.md] file "../integrations/model-providers.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Model provider integration and LangGraph persistence](../integrations/model-providers.md)

- **I want to check what network calls are made**
<!-- openwiki: broken internal link [../operations/network-calls-and-egress.md] file "../operations/network-calls-and-egress.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Network calls, egress and per-deployment allowlist](../operations/network-calls-and-egress.md)

- **I want to understand dependencies**
<!-- openwiki: broken internal link [../operations/dependencies.md] file "../operations/dependencies.md" does not exist. Fix the href or restore the target, then delete this comment. -->
  → [Dependency inventory: runtime, dev and Node tools](../operations/dependencies.md)
