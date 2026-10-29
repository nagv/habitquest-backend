# Execution ledger - local onboarding backend

Plan: HabitQuest AI Functional Workflow v6, section 15, and the implementation steps in this chat.

Scope: authenticated concerns -> confirmed goals -> reviewed catalog-backed habits and chapter outline -> versioned parent-confirmed plan. Rewards and live world execution remain later integrations.

Pre-flight: API consumes typed scope/contracts; worker consumes durable job and checkpoint IDs; confirmation consumes reviewed proposal plus current source/plan versions. These interfaces must agree throughout.

Ruling: Work in the user-requested Backend folder, preserving the existing empty HabitQuestAgent folder. No existing Git repository or application files were present. No additional worktree is necessary for this new repository.

Ruling: Docker and Ollama are unavailable on this host. Verify mechanics with an isolated native PostgreSQL 18 cluster and deterministic model; also provide local Supabase configuration, real JWT/JWKS validation, and Ollama integration. Real Supabase Auth and real model smoke tests cannot be claimed without those runtimes.

Ruling: Stage files in the chat's writable working directory, then copy only project-owned files to the authorized destination using elevated filesystem access. Do not copy the staging virtual environment.

## Evidence

- Initialized Python 3.12 uv package and installed locked agent/API/database/test dependencies.
- First contract tests written before application implementation.

## Final verification

- Independent read-only review identified checkpoint fencing, source-lock races, full source-field validation, session/plan locking, final-candidate review, durable deadlines/budgets and membership insertion scope. Fixed with regression tests; no critical/important finding was left open.
- Target-folder run: 79 tests passed; 512 owned application statements and 116 branch opportunities, all covered (100%). Worker subprocess measurement included.
- Ruff check/format and `python -m mypy src --follow-imports=silent` passed. Windows blocked the mypy executable launcher; its standard Python module entrypoint passed.
- Native PostgreSQL cluster bootstrapped under .local with SCRAM password authentication and loopback binding; startup/restart and API readiness verified.
- Actual API plus separate worker-process synthetic demo produced confirmed bedtime, morning and responsibility habits and three Space chapters.
- npm audit: zero findings after updating the development Supabase CLI to 2.122.0.
- Supabase CLI launch returned spawn UNKNOWN; Docker and Ollama are absent. Supabase runtime/Auth, pgTAP and downloaded-model smoke checks remain unverified. Native tests do not establish model recommendation quality.
- Ruling: reserved_tokens records conservative request reservations, not measured tokenizer usage/cost; this distinction is documented. Persisted call/deadline limits remain enforced.
- Ruling: explicit failure is the first-slice fallback. No unchecked generated plan is published when review, suitability or budgets fail. A richer approved-template fallback can follow.
- Ruling: no hosted release, reward ledger or real family pilot is included. Family deletion/retention, evals and production credentials remain prerequisites for a family pilot.
- Ruling: one coherent initial feature commit is used for this new repository; no pre-existing application history was available. No remote push or deployment.
- Deferred minor items: none from the independent review. Broader product capabilities remain intentionally out of scope as specified in implementation-plan.md.
