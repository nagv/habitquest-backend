---
type: workflow
title: "Workflow: parent onboarding, intake and a confirmed plan"
description: End-to-end parent journey from child onboarding through intake, planning, proposal review and confirmation, showing the API endpoints, worker lifecycle, version checks and terminal failure modes.
tags: [workflow, planning, onboarding, api, worker]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-30T00:13:47.181Z
sources:
  - id: openwiki-source-70663512997ed0573d9f7f78
    resource: repo://src/habitquest_backend/main.py
  - id: openwiki-source-5891884c8188a825b0aaec3e
    resource: repo://src/habitquest_backend/planning.py
  - id: openwiki-source-4a28e17e3778cdec4395ddbc
    resource: repo://src/habitquest_backend/service.py
  - id: openwiki-source-6e803a3ec70ccc9d4eb9e11b
    resource: repo://src/habitquest_backend/worker.py
  - id: openwiki-source-4c573540c2631e3dec22664a
    resource: repo://tests/test_recovery.py
generated: { by: "openwiki/0.7.2", at: "2026-10-30T00:13:47.181Z" }
---

# Parent onboarding, intake and a confirmed plan

A parent joins the service, describes a child's pain points, and receives a reviewed habit proposal they can edit and confirm. The journey spans the FastAPI handlers, a durable Postgres queue, a planning worker that orchestrates model agents, and a final confirmation step that writes an immutable plan version.

## End-to-end sequence

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
sequenceDiagram
    participant P as Parent client
    participant API as API handlers
    participant DB as Postgres
    participant W as Planning worker
    participant MA as Model adapter

    P->>API: POST /v1/children
    API->>DB: INSERT household, membership, child
    DB-->>API: child record
    API-->>P: 201 child id

    P->>API: POST /v1/children/{id}/intakes
    API->>DB: INSERT intake
    DB-->>API: intake record
    API-->>P: 201 intake id

    P->>API: POST /v1/intakes/{id}/plan-runs<br/>Idempotency-Key: {key}
    API->>DB: enqueue (capture intake_version,<br/>base_plan_version)
    DB-->>API: job row
    API-->>P: 202 Accepted<br/>run_id + status_url

    Note over DB: Job queued with lease<br/>and deadline

    W->>DB: claim (queued or expired lease)
    DB-->>W: job + lease_token
    Note over W,DB: Snapshot versions:<br/>intake_version, base_plan_version

    W->>DB: SELECT child, intake, policy,<br/>base plan, release
    DB-->>W: planning context

    W->>MA: map_concerns<br/>(BoundedChat mapper)
    MA-->>W: GoalMapping

    W->>MA: plan_with_agents<br/>(director delegates to planner)
    MA-->>W: PlannerSelection

    W->>MA: review proposal<br/>(BoundedChat reviewer)
    MA-->>W: Review result

    Note over W,DB: Worker revalidation gate
    W->>DB: Verify intake_version,<br/>base_plan_version, profile_version
    Note over W,DB: Fails run if any version<br/>changed since enqueue

    W->>DB: lock_release, INSERT proposal,<br/>UPDATE job status -> ready
    DB-->>W: committed

    P->>API: GET /v1/plan-runs/{run_id}
    API->>DB: SELECT job status
    DB-->>API: status: ready
    API-->>P: 200 OK

    P->>API: GET /v1/proposals/{run_id}
    API-->>P: 200 proposal data

    P->>API: PATCH /v1/proposals/{run_id}<br/>edit + expected_proposal_version
    API->>DB: SELECT proposal FOR UPDATE
    API->>DB: apply_patch (revalidate rules)
    Note over API,DB: 409 if profile_version<br/>or proposal_version changed
    Note over API,DB: 422 if rule violated<br/>(coverage, max_active, minutes)
    DB-->>API: updated proposal
    API-->>P: 200 OK

    P->>API: POST /v1/proposals/{run_id}/confirm<br/>expected_plan_version +<br/>expected_proposal_version
    API->>DB: validate_sources + INSERT plan
    Note over API,DB: 409 if plan_version,<br/>proposal_version, profile_version,<br/>or catalog release changed
    API->>DB: UPDATE proposal confirmed,<br/>resolve old flags,<br/>UPSERT child_goals
    DB-->>API: plan record
    API-->>P: 200 OK
```

*Figure: Parent journey from child creation to confirmed plan, showing API, Postgres, worker and model adapter interactions. Nodes marked "Version check" show where intake_version, base_plan_version or profile_version changes can abort or conflict the run.*

## Step-by-step

### 1. Onboard a child

`POST /v1/children` creates the household and membership on first use, then inserts the child profile with the routine schedule and other durable fields. The routine schedule is the source of truth for which anchors (morning, after_school, evening, bedtime) are available to schedule habits against.

### 2. Describe pain points

`POST /v1/children/{child_id}/intakes` stores one round of the parent's concerns and the daily `available_minutes` budget. A child can have many intakes over time; each later plan run starts from the most recent confirmed plan.

### 3. Start planning

`POST /v1/intakes/{intake_id}/plan-runs` requires an `Idempotency-Key` header. The `enqueue` helper captures the current `intake_version` and `base_plan_version` into the job row so that later changes to the intake or an intervening plan confirmation do not race with the worker. Retrying with the same key returns the same run; a different intake or intake version with the same key returns 409.

### 4. Worker claim and execution

The worker polls with `claim()`, which selects a queued job or a running job whose lease has expired. It sets a `lease_token` and `lease_until`, then spawns a heartbeat thread that renews the lease until the job finishes.

Inside `run_once()` the worker loads the child profile, the intake snapshot, the current catalog release, and the onboarding policy. It calls `generate()`, which runs three model-backed stages:

1. **Mapper** (`BoundedChat` role `mapper`): maps free-text concerns to catalog goal IDs.
2. **Director / Planner** (`BoundedChat` roles `director` and `planner`): the director delegates to a planner subagent that can call a `search_catalog` tool to see reviewed shortlists. The planner must return a `PlannerSelection` with picks and rationale.
3. **Reviewer** (`BoundedChat` role `reviewer`): a mandatory final check that accepts or rejects the planner's selection for safety and fidelity.

`BoundedChat` wraps the actual provider (Google, OpenAI or Ollama) and enforces time and token budgets. If the run exceeds the time budget or reserved tokens, it raises `BudgetExceeded`.

If the planner returns invalid picks or the reviewer rejects, `plan_with_agents` tries one revision. After two failed attempts it falls back to the deterministic reviewed template plan.

### 5. Revalidation before publication

After the model stages complete but before the proposal is inserted, the worker revalidates three version snapshots against the current database state:

- `intake_version` — if the intake was edited after enqueue, the run fails.
- `base_plan_version` — if a new plan was confirmed after enqueue, the run fails.
- `profile_version` — if the child profile was edited after enqueue, the run fails.

### 6. Poll status

`GET /v1/plan-runs/{run_id}` returns the job status (`queued`, `running`, `ready`, `failed`, `cancelled`) along with `attempts`, `error`, `model_calls` and `reserved_tokens`.

### 7. Review the proposal

`GET /v1/proposals/{proposal_id}` returns the full proposal: mapped goals with evidence spans, active habits with level/minutes/cue/days, future candidates, shortlists with scores, routine links, chapters, questions, rationale, and the review result.

### 8. Edit the proposal

`PATCH /v1/proposals/{proposal_id}` applies a single edit (`remove_goal`, `reorder_goals`, `swap_habit`, `change_level`, `change_schedule`, `demote_active`, `promote_future`). The handler checks `expected_proposal_version` and that the profile version has not changed (409 on mismatch), then calls `apply_patch` which rebuilds the proposal from the reviewed catalog and re-runs the publication rules:

- `max_active_habits` — the plan cannot hold more active habits than policy allows.
- `available_minutes` — the total daily minutes cannot exceed the intake budget.
- `coverage` — no previously covered goal can be left uncovered.
- `chapter` — every covered goal must have an approved chapter in the child's world.

A rule violation raises `ValueError`, which the handler turns into a 422 whose message starts with the rule name.

### 9. Confirm the plan

`POST /v1/proposals/{proposal_id}/confirm` requires both `expected_plan_version` and `expected_proposal_version`. The handler:

1. Locks the proposal and checks it is not expired, that the intake version matches, and that the proposal version matches the expected one.
2. Verifies the expected plan version equals the current latest plan version and the base plan version in the proposal.
3. Checks the profile version in the proposal data matches the current child profile.
4. Calls `validate_sources()`, which locks the onboarding policy, the current release, and all referenced habit versions, then checks that none were withdrawn or replaced.
5. Inserts the new plan version into `hq.plans`.
6. Marks the proposal as confirmed.
7. Resolves any open `plan_flags` on superseded plans.
8. Upserts `hq.child_goals` for every covered goal, setting status to `active`.

## Terminal outcomes and error codes

A planning run can end in one of three terminal states reported by `GET /v1/plan-runs/{run_id}`:

| Status | Meaning |
|--------|---------|
| `ready` | Proposal created successfully; parent can review and confirm. |
| `failed` | Run could not complete; `error` field names the cause. |
| `cancelled` | Parent cancelled before the worker finished. |

The `failed` state carries one of these error codes:

- **`deadline_exhausted`** — The worker exceeded `max_run_seconds` or the model response arrived after the deadline.
- **`retry_exhausted`** — The job exhausted `max_attempts` (for example after repeated lease timeouts).
- **`provider_or_infrastructure_failure`** — A transient exception (network, model provider) that is retried until attempts are exhausted.
- **`ValueError`** (or the exception classname) — Validation rejected the input or model output. Examples include unsafe concern text, a goal mapping outside the current release, invalid planner picks, or a state change detected during revalidation.

## Related pages

- [Data model and persistence](../architecture/data-model-and-persistence.md) — Tables, version fields and RLS policies.
- [Runtime and processes](../architecture/runtime-and-processes.md) — Worker polling, leases and hosting modes.
- [Verification strategy](../testing/verification-strategy.md) — How the test suite covers idempotency, recovery and budget failures.
- [Proposal edits and confirmation](proposal-edits-and-confirmation.md) — Detailed edit operations and confirmation rules.
