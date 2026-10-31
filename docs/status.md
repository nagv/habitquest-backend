# HabitQuest: status and what's left

As of 2026-10-30. Backend: `nagv/habitquest-backend`, live at https://habitquest-api-qmt1.onrender.com
(`/docs` for the interactive API). Frontend: `rahulwarekar20/habbit-quest`, being connected locally on a
`feat/backend-api` branch using `HANDOFF.md`.

## Backend: done

The backend was rebuilt as a lean MVP in five merged pull requests. CI passes on every one, with 100% test
coverage (146 tests).

| PR | Step | What it delivered |
|---|---|---|
| #9 | Reset | 11-table schema with row-level security (each family sees only its own data), no version columns. Seed catalog: 7 goals, 36 habits taken from the frontend's quests. Catalog endpoints. Old planning, worker and editorial code removed. |
| #10 | Accounts | Google sign-in (`POST /v1/auth/google`), families with one or more caregivers (father, mother, guardian, other), invite codes, 4-digit family PIN, kid tokens for kid mode, CORS for `localhost:5180`. |
| #11 | Onboarding | Children (age 3-13, buddy, world, routine times). Pain points: a safety screen, then AI maps the text to 1-3 goals with reasons and recommends up to 2 habits per goal, falling back to keyword matching. Plan: pick up to 6 habits by age and unlocked tier; edit, pause or remove later. |
| #12 | Daily loop | Today's habits in the family's time zone, kid "I did it!", caregiver approve or "not yet", 1 Spark plus the habit's points per approval, levels (Sprout to Legend) unlocking tiers. The database holds kid tokens to their own child, today, and "submitted". |
| #13 | Points | Caregiver records what points were spent on; the balance can never go below zero (checked by the API and the database). Points history for parents and kids. |

Also done:
- **Render:** auto-deploys from `main`; every deploy has gone live. `GOOGLE_CLIENT_ID` is set.
- **Google Cloud:** project "HabitQuest", consent screen in Testing, web client with the origins
  `http://localhost` and `http://localhost:5180`.
- **Dev login** from `/docs` (`parent`, `parent:<label>` and the shared `DEV_LOGIN_PASSWORD`) for testing.
- **Docs in the repo:** `README.md` (endpoints, rules, settings), `CLAUDE.md` (guide for coding sessions:
  conventions, checks, environment, decisions), `docs/mvp-plan.md` (scope and steps), `docs/frontend-integration.md`
  (API contract for the frontend) and this file. The old generated `openwiki/` pages were removed; regenerate
  them from `openwiki/INSTRUCTIONS.md`.

## Update 2026-10-31: AI helpers and browser features moved to the backend

On branches `feat/ai-coach` (backend) and `feat/ai-coach-ui` (frontend), not yet merged. Added: the habit coach
(LangChain agent), quest planner, weekly review, buddy lines, reward shop and requests, check-ins with screening,
surprises, kid extras, family settings, server-paid multipliers, rate limits, family export and deletion. This reverses
the earlier plan to replace the coach with the one-shot form, and resolves open decisions 1-3 and the Groq question.
Pain-point onboarding stays available but the frontend now uses the coach. Not yet run on real Nebius.
## Backend: settings to finish

| Item | Why | Where |
|---|---|---|
| Upgrade or replace the database **before 2026-11-28** | The free Render Postgres expires and its data is lost | Render → `habitquest-db` |
| `AI_MODE=openai` plus `OPENAI_BASE_URL`, `OPENAI_MODEL` (must support JSON output and tool calls), `OPENAI_API_KEY`, optional `OPENAI_SMALL_MODEL` | Every AI feature; until then they use keyword matching or built-in lines | Render → habitquest-api → Environment |
| `CORS_ORIGINS` | Needed once the frontend is hosted (comma-separated; keep `http://localhost:5180` for local work) | Render → Environment |
| Paid always-on instance | The free instance sleeps; the first request can take about a minute | Render → habitquest-api |
| Turn off `DEV_LOGIN` | Before real families use it: anyone with the password can act as a test parent | Render → Environment |
| Publish the Google consent screen | While it is in Testing, only listed test users can sign in | Google Cloud → Google Auth Platform → Audience |
| Remove leftover variables `MODEL_MODE`, `RUN_WORKER_IN_API` | Ignored by the new code; tidy-up only | Render → Environment |

## Backend: later features (not built)

- **Rewards:** a reward catalog and kid requests that a parent approves (today a parent records spending directly).
- **Point bonuses:** golden quest, streaks and double days (the backend pays base points only).
- **Check-ins:** kids' mood check-ins with safety screening, buddy encouragement lines, surprises.
- **Kid extras saved on the server:** worlds and discoveries, treasure chest, buddy accessories, kid-mode time limit.
- **Account management:** leaving a family or removing a caregiver, deleting a child, signing out everywhere
  (kid and caregiver tokens stay valid until they expire: 8 hours).
- **Privacy:** family deletion and data export, retention rules, and a privacy notice covering the AI provider.
- **Security:** rate limiting on sign-in and the AI endpoint.
- **AI quality:** a review of the goal mapping on real parent descriptions.

Known limits:
- Editing a child's age does not re-check habits already in their plan.
- A habit's points are fixed at the moment of approval.

## Frontend: done

- Analysis of the app and the integration plan.
- [frontend-integration.md](frontend-integration.md) (given to the local session as `HANDOFF.md`): the full API contract (every endpoint and response shape), the screen-by-screen changes, local
  settings and test steps. It has been given to a Claude session on your computer.

No frontend code has been pushed anywhere yet. Your account has no push access to
`rahulwarekar20/habbit-quest`, so changes stay local until Rahul adds you as a collaborator or you use a fork.

## Frontend: left to do (details in [frontend-integration.md](frontend-integration.md))

**Set-up**
- [ ] `.env.local` with `VITE_API_URL`, `VITE_GOOGLE_CLIENT_ID`, `VITE_DEV_LOGIN`; dev server on port 5180
- [ ] Typed API client `src/lib/api.ts`: caregiver token in `localStorage`, kid token in `sessionStorage`,
      `detail` error messages, a 401 sends the user back to sign-in, a "waking up the server" state

**Screens**
- [ ] Sign-in: Google button (plus dev login when enabled), then `GET /v1/me`
- [ ] Family setup: create (name, time zone, PIN, your name and role) or join with an invite code; replaces
      local email/password registration
- [ ] Children: add and edit (age, buddy, world, routine times); `#/setup` becomes per-child routine times
- [ ] "Describe a problem": pain points, suggested goals with reasons, habit options with recommended ones
      pre-ticked (max 6), save the plan; show safety guidance when returned. Replaces the Groq habit coach
- [ ] Quest library: edit the plan; pause, remove or retime one habit
- [ ] Parent dashboard: approvals (approve / not yet), each child's level, Sparks and points, invite code and
      caregivers
- [ ] Kid mode: start a kid session per child; today's quests and "I did it!" from `/v1/kid/*`; leaving kid
      mode checks the PIN with `pin/verify`; level-up celebration when `progress.level` goes up
- [ ] Rewards page: record a reward (points and note) and show the history
- [ ] Kid shop: show the balance and history; spending is recorded by a parent, so turn "buy" into "ask a
      grown-up" or hide it
- [ ] Remove the `localStorage` family model and the local PIN hashing where the API replaces them

**Stays in the browser for now:** explorable worlds, treasure chest, wardrobe, check-ins, surprises, mystery box,
point bonuses, kid-mode time limit, and buddy lines (only with a Groq key).

**Finish and ship**
- [ ] `npm test` and `npm run build` pass; walk through the test steps in `HANDOFF.md` section 5
- [ ] Get the code into a repository: collaborator access on `rahulwarekar20/habbit-quest`, or a fork
- [ ] Host the frontend (for example a Render static site), then add its address to `CORS_ORIGINS` on the API
      and to the Google client's Authorized JavaScript origins

## Integration needs: what each side must provide

**Backend provides (in place)**
- The API at `https://habitquest-api-qmt1.onrender.com`, with every endpoint in
  [frontend-integration.md](frontend-integration.md) live and documented at `/docs`.
- Browser access (CORS) from `http://localhost:5180`; Google sign-in for client
  `589389699921-ol224oo6m4svf9tts3enahorubm4lb1s.apps.googleusercontent.com`; the dev login for testing.
- Habit ids, buddies, ages and level thresholds that match the frontend's, so existing artwork and texts map
  directly (two new habits: `tidy-space`, `day-chat`).

**Frontend must provide**
- `VITE_API_URL`, `VITE_GOOGLE_CLIENT_ID` and (for testing) `VITE_DEV_LOGIN` settings, on port 5180 locally.
- Two token stores: the caregiver token (`localStorage`, 8 hours, 1 hour for dev login) and the kid token
  (`sessionStorage`, 8 hours, only for `/v1/kid/*`). A 401 means sign in again.
- The Google Identity Services button, posting its `credential` to `POST /v1/auth/google`.
- Patience with the free server: up to a minute for the first request after an idle period.

**Both sides, when the frontend is hosted**
- Add the hosted address to `CORS_ORIGINS` on Render and to the Google client's Authorized JavaScript origins.
- Turn off `DEV_LOGIN` and publish the Google consent screen before real families sign up.
- Any change to an endpoint's shape: update the backend tests, [frontend-integration.md](frontend-integration.md)
  and the frontend's `src/lib/api.ts` together. The API's machine-readable contract is `/openapi.json`; the
  frontend can generate its types from it with `openapi-typescript`.

## Decisions still open

1. **Reward requests:** should kids ask for rewards in the app (a backend feature) or keep "ask a grown-up"?
2. **Point bonuses:** golden quest, streaks and double days on the server, or drop them for the MVP?
3. **Worlds:** the backend's chosen world (space, forest, city) versus the frontend's unlockable worlds (camp,
   mosswood, sky): one system or both?
4. **Groq agent server:** keep it for buddy lines and check-in moderation, or move those to the backend later?
