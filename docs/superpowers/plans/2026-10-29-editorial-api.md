# Editorial API (plan 3)

**Goal:** Let catalog staff maintain the habit catalog through a two-person review and release process (spec section 5): an `Editor` identity, draft → review → approval lifecycle with audit, release build/diff/validate/publish with rollback, emergency withdrawal that flags confirmed plans, and the anonymised catalog-requests listing.

**Architecture:** One forward-only migration adds the `hq_editor` role, `catalog_audit`, editor-only views and four `SECURITY DEFINER` functions that own every status change. Editors write draft content directly through column-level grants, so they can never set status, reviewer, withdrawal or author columns. The API is a FastAPI router under `/v1/editor`; it runs each request as `hq_editor` with `app.editor_id` set from the verified token and maps function refusals (SQLSTATE) to HTTP codes. Submit checks and release validation are pure Python over stored rows and loaded releases.

## Roadmap

| Plan | Scope |
|---|---|
| 1 (done) | Catalog schema, release 1, `catalog` package |
| 2a (done) | Profile, intakes, single planning job, template fallback, confirmation revalidation |
| 2b (done) | Proposal `PATCH` edits, change sets, `plan_flags`, `catalog_requests`, concern dataset |
| **3 (this)** | Editorial API: editor role, lifecycle functions, release build/diff/validate/publish, withdrawal endpoint |

## Design refinements recorded by this plan

1. Rejection returns a version to `draft`; the audit row keeps the reason. The `rejected` status stays in the schema but is unused.
2. One open (`draft` or `in_review`) version per entity; editing an approved entity means creating the next version.
3. Versions and their detail rows are frozen while `in_review` (`55000`), as well as after approval (`42501`).
4. Submit requires linked goals to exist in any status; references outside a release are release validation errors.
5. `build_release` takes the latest approved, non-withdrawn version of each entity. With `source_release_id` it copies a published release (rollback) without withdrawn habit versions.
6. Validation errors: missing world × goal chapter, chapter/habit/concern goal outside the release, prerequisite outside the release, concern without goals, withdrawn habit, habit without levels. Warning: a goal with no usable habit covering a whole age band.
7. `withdraw_habit` withdraws every approved version, audits each, and flags each child's current plan that uses the habit with a fixed parent-facing reason. It replaces the plan 2b Python helper.
8. Concern labels describe parents' problems, so they are not linted.

9. Follow-up: `GET /v1/editor/{kind}?status=` lists versions for the review queue; parents resolve plan flags with `POST /v1/plan-flags/{id}/resolve` (migration `202610290007_resolve_plan_flags.sql`), and confirming a newer plan resolves the flags on the plans it supersedes. An API test covers a release published while a proposal is open (`409 replan_required`).

## File structure

| File | Change |
|---|---|
| `supabase/migrations/202610290006_editorial.sql` | Create: role, audit, guards, grants, views, lifecycle/release/withdrawal functions |
| `src/habitquest_backend/auth.py` | Modify: `verify_token(..., hq_role=)` returns 403 without the claim |
| `src/habitquest_backend/db.py` | Modify: `transaction(..., editor=)` switches to `hq_editor` and sets `app.editor_id` |
| `src/habitquest_backend/catalog/repository.py` | Modify: `load_release(..., candidate=True)` reads unpublished releases |
| `src/habitquest_backend/catalog/releases.py` | Create: `validate_release`, `diff_items` |
| `src/habitquest_backend/editorial/{schemas,checks,api}.py` | Create: draft bodies, submit checks and lint, router |
| `src/habitquest_backend/main.py`, `service.py` | Modify: include the router; drop the Python `withdraw_habit` |
| `scripts/setup_db.py` | Modify: grant `hq_editor` to the setup user |
| `supabase/tests/rls.sql` | Modify: draft isolation and two-person pgTAP checks |
| `tests/test_editorial.py`, `tests/test_releases_and_checks.py` | Create |

## Verification

- `uv run python -m pytest --cov=habitquest_backend`: 100% owned statement and branch coverage.
- `uv run ruff check src tests scripts`, `uv run ruff format --check src tests scripts`, `uv run python -m mypy src`.

## Not in this plan

Optional LLM lint, parent notification of plan flags, database-side release validation (publication relies on the API running `validate` first), and an editor UI.
