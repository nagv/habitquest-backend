# Local onboarding implementation

Specification: HabitQuest AI Functional Workflow v6 sections 14-15 and the approved plan in this conversation.

## Scope

Authenticated parent concerns -> durable goal-analysis job -> explicit clarification and goal confirmation -> restricted Director/Planner -> complete-candidate Reviewer -> source/version validation -> parent-confirmed versioned plan and chapter preview.

## Delivered task sequence

1. Python 3.12 uv package, validated settings, liveness/readiness, pytest and coverage gate.
2. SQL migrations, reviewed development catalog/chapter templates, memberships and child RLS.
3. Signed JWT/JWKS authentication, child/session APIs, explicit goal confirmation and source eligibility.
4. Durable jobs, leases, fencing, persisted call/request-unit budget, persisted deadline and cancellation.
5. Deep Agents/LangGraph runtime, scoped retrieval, mandatory final review, bounded revision.
6. Lease-specific checkpoint branches and snapshot-based recovery into fresh branches.
7. Locked source validation and atomic idempotent plan confirmation; explicit preference memory operations.
8. Native Postgres and Supabase local startup paths, separate-process demo, CI and test runbook.

## Review focus and verification

- Parent and sibling isolation: direct database-role and HTTP tests.
- Source revocation, policy/plan/session changes and races: transaction locks plus stale-state tests.
- Cancelled/expired workers and old checkpoints: fenced publication, separate recovery branches and crash tests.
- Unsafe/invalid model output, omitted goals, review revision, unknown sources: fail-closed tests.
- Offline determinism versus real inference: fake model exercises actual tools/graph; Ollama is separately configured and requires a downloaded model.

## Explicitly later

Full family deletion/retention operations, live daily assignments, completion approval/Spark ledger, chapter unlock accounting, screen time, redemption, frontend/Phaser and formal model-quality evals. The small reviewed catalog, lexical safety check and fake-model behavior are development fixtures, not a production suitability claim.
