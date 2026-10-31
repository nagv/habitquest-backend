# Frontend integration guide

How to connect the HabitQuest frontend (`rahulwarekar20/habbit-quest`) to this backend: settings, the API
contract (every endpoint and response shape the frontend needs), the screen-by-screen changes and local test
steps. It doubles as the brief for a coding session in the frontend repository. Progress is tracked in
[status.md](status.md); the backend's own docs are in the [README](../README.md).

The `habbit-quest` frontend (React 18 + Vite + TypeScript, hash router, port 5180). Today it
keeps every family in `localStorage` (`src/lib/store.tsx`, `src/lib/storage.ts`) and has its own Groq agent
server (`server/`). Your job: make accounts, children, onboarding, the daily quest loop and points run against
the HabitQuest backend API described below, so the app can be tested locally against the hosted API.

Work on a new git branch (`feat/backend-api`). Keep the existing design system (`src/styles.css`) and
components. Run `npm test` and `npm run build` before you finish, and try the flows in the browser.

## 1. Configuration

Create `.env.local` (git-ignored) and an `.env.example` entry:

```
VITE_API_URL=https://habitquest-api-qmt1.onrender.com
VITE_GOOGLE_CLIENT_ID=589389699921-ol224oo6m4svf9tts3enahorubm4lb1s.apps.googleusercontent.com
VITE_DEV_LOGIN=true
```

- The API is on Render's free plan: the first request after it has been idle can take up to a minute. Show a
  "waking up the server…" state rather than failing fast (use a 70-second timeout on the first call).
- The backend allows browser calls from `http://localhost:5180` (CORS), and the Google client allows the
  origins `http://localhost` and `http://localhost:5180`. Keep the dev server on port 5180.
- `VITE_DEV_LOGIN=true` shows a development login (username + shared password) next to Google sign-in, for
  testing without a Google account. Never put the password in an env file; the tester types it.

## 2. API basics

- Base URL: `VITE_API_URL`. All bodies are JSON (`Content-Type: application/json`) except the dev login.
- Auth: `Authorization: Bearer <token>`. There are two kinds of token:
  - **Caregiver token** (from Google or dev login): for every endpoint except `/v1/kid/*`. Lasts 8 hours (dev
    login: 1 hour). Store it in `localStorage` with its expiry.
  - **Kid token** (from `POST /v1/children/{id}/kid-session`): only for `/v1/kid/*`. Lasts 8 hours. Store it in
    `sessionStorage` (per tab, like today's kid mode).
- Errors: `{"detail": "message"}`, or for invalid input (422 from validation) `{"detail": [{"msg": ..., "loc": [...]}]}`.
  401 means the token is missing or expired: send the user to sign in again (or, in kid mode, to the parent PIN).
  404 also means "not yours". Show `detail` strings to the user; they are written for people.
- Write one typed client, `src/lib/api.ts`, with a `request<T>(method, path, body?, {kid?: boolean})` helper and
  one function per endpoint below. Put the response types next to it.

## 3. Endpoints and shapes

### Sign-in
- **Google**: load `https://accounts.google.com/gsi/client`, call
  `google.accounts.id.initialize({client_id: VITE_GOOGLE_CLIENT_ID, callback})` and `renderButton`. In the
  callback, `POST /v1/auth/google {credential: response.credential}` →
  `{access_token, token_type: "bearer", expires_in: 28800}`.
- **Dev login** (only when `VITE_DEV_LOGIN=true`): `POST /v1/auth/token` as
  `application/x-www-form-urlencoded` with `username` (`parent`, or `parent:<any label>` for a separate test
  parent) and `password` → same shape, `expires_in: 3600`.
- `GET /v1/me` → `{id, name, email, families: [{id, name, display_name, role}]}`. Empty `families` → show
  family setup.

### Family
- `POST /v1/families {name, timezone, pin, display_name, role}` → 201 Family. `timezone` is IANA (use
  `Intl.DateTimeFormat().resolvedOptions().timeZone`); `pin` is exactly 4 digits; `role` is
  `father | mother | guardian | other`.
- Family = `{id, name, timezone, invite_code, has_pin, created_at, caregivers: [{user_id, display_name, role}]}`.
- `POST /v1/families/join {invite_code, display_name, role}` → Family. 404 unknown code, 409 already a member.
- `GET /v1/families/{id}`, `PATCH /v1/families/{id} {name?, timezone?}`,
  `PATCH /v1/families/{id}/caregivers/me {display_name?, role?}`,
  `POST /v1/families/{id}/invite-code` (new code) → Family.
- `PUT /v1/families/{id}/pin {pin}` → 204. `POST /v1/families/{id}/pin/verify {pin}` → `{valid: boolean}`.
  Use verify wherever the app asks for the parent PIN today (leaving kid mode, grown-up pages). The PIN is no
  longer hashed in the browser.

### Children
- `POST /v1/families/{familyId}/children {name, age, buddy, world, routine_times}` → 201 Child.
  - `age` 3-13; `buddy` `fox | owl | bear | bunny`; `world` `space | forest | city` (a theme the parent picks;
    keep the frontend's explorable worlds camp/mosswood/sky as they are);
    `routine_times`: `{morning?, midday?, after_school?, evening?, bedtime?}` as 24-hour `"HH:MM"`.
- Child = `{id, family_id, name, age, buddy, world, routine_times, created_at}`.
- `GET /v1/families/{familyId}/children` → Child[]; `GET /v1/children/{id}`; `PATCH /v1/children/{id}` (any
  field; `routine_times` replaces the whole object).

### Onboarding: pain points → goals → habits
- `POST /v1/children/{id}/pain-points {text}` (5-2000 chars, the parent's own words). AI maps it to 1-3
  pre-approved goals with a reason and recommends up to 2 habits per goal.
  - 201 → `{pain_point: {id, text, mapped_by, created_at}, safety: null, plan: Plan}`. `mapped_by` is `ai`,
    `keywords` (AI unavailable; no recommendations) or `manual` (nothing matched: let the parent pick goals
    from `GET /v1/goals`).
  - 200 → `{pain_point: null, safety: {guidance}, plan: null}` when the text suggests a child may be at risk.
    Show `guidance` prominently and nothing else.
- Plan = `{child_id, progress, goals: PlanGoal[], habits: PlanHabit[]}`
  - progress = `{sparks, level, level_name, unlocked_tier, next_level_at}` (levels: Sprout 0, Explorer 5,
    Adventurer 15, Hero 30, Legend 50; tier 2 at Explorer, tier 3 at Adventurer; `next_level_at` null at the top).
  - PlanGoal = `{goal_id, title, status: "suggested" | "active", reason, recommended_habit_ids, options: Option[]}`,
    Option = `{id, goal_id, title, description, kid_title, kid_hint, icon, anchor, tier, points, recommended}`.
    `options` are already filtered to the child's age and unlocked tier.
  - PlanHabit = `{id, habit_id, goal_id, title, description, kid_title, kid_hint, icon, anchor, tier, points,
    cue_time: "HH:MM", days: ["mon",...], status: "active" | "paused", started_on}`. `id` is the child-habit id.
- `GET /v1/children/{id}/plan` → Plan.
- `PUT /v1/children/{id}/plan {goals: string[], habits: [{habit_id, cue_time?, days?}]}` → Plan. At most 6
  habits; each must belong to a listed goal. Times default to the child's routine time, days to every day.
  Unlisted goals are dropped and unlisted habits removed. 422 `detail` explains a refusal. Use it for the first
  plan and for later changes.
- `PATCH /v1/child-habits/{childHabitId} {cue_time?, days?, status?: "active" | "paused" | "removed"}` → Plan.
- Catalog: `GET /v1/goals` → `[{id, title, description}]`; `GET /v1/goals/{id}/habits?age=&max_tier=`.

### Kid mode and the daily loop
- `POST /v1/children/{id}/kid-session` (caregiver token) → `{access_token, token_type, expires_in, child: {id, name}}`.
- `GET /v1/kid/me` (kid token) → `{id, name, age, buddy, world}`.
- `GET /v1/kid/today` (kid token) or `GET /v1/children/{id}/today` (caregiver) → Today =
  `{child: {id, name, buddy, world}, day: "YYYY-MM-DD", progress, points, habits: TodayHabit[]}`,
  TodayHabit = `{child_habit_id, habit_id, kid_title, kid_hint, icon, anchor, tier, points, cue_time,
  status: "todo" | "submitted" | "approved" | "rejected", completion_id}`. "Today" is in the family's time zone.
- `POST /v1/kid/completions {child_habit_id}` (kid token) → Today. "I did it!". A repeat tap is harmless; a
  `rejected` habit can be submitted again; 409 if already approved.
- `GET /v1/families/{familyId}/approvals` → `[{completion_id, child_id, child_name, habit_id, title, kid_title,
  icon, points, day, submitted_at}]`, oldest first.
- `POST /v1/completions/{id}/approve` / `.../reject` → `{completion: {id, status, day, submitted_at, decided_at},
  child_id, progress, points}`. Approve gives 1 Spark and the habit's points; compare `progress.level` with the
  previous value to trigger the level-up celebration. 409 if already decided.

### Points
- `POST /v1/children/{id}/redemptions {points, note}` (caregiver) → 201 `{entry, points}`; 422
  `"Not enough points: N available"`. Sparks are never spent.
- `GET /v1/children/{id}/points?limit=50` (caregiver) and `GET /v1/kid/points` (kid) →
  `{points, earned, spent, entries: [{id, amount, reason: "earned" | "redeemed" | "adjusted", note, created_at,
  habit_id, kid_title, icon, completion_day, by?}]}` (`by` = caregiver display name, caregiver view only).

### AI helpers, rewards, check-ins and extras

`/docs` and `/openapi.json` have every shape. In short:

- Coach: `GET`, `POST`, `DELETE /v1/children/{id}/coach/messages`. Suggestions: `GET /v1/families/{id}/suggestions`,
  `POST /v1/suggestions/{id}/accept` with `{pin}`, `POST /v1/suggestions/{id}/dismiss`.
- `POST /v1/families/{id}/plan-suggestions`, `POST /v1/families/{id}/reviews`, `GET /v1/families/{id}/reports`,
  `POST /v1/kid/buddy-line`.
- Rewards: `/v1/families/{id}/rewards`, `/v1/rewards/{id}`, `/v1/kid/rewards`, `/v1/kid/reward-requests`,
  `/v1/families/{id}/reward-requests`, `/v1/reward-requests/{id}/approve` (`{pin}`) and `/decline`.
- Check-ins and surprises: `/v1/kid/checkins`, `/v1/kid/checkins/today`, `/v1/families/{id}/checkins` and `/read`,
  `/v1/families/{id}/surprises`, `/v1/kid/surprises`, `/v1/kid/surprises/{id}/open`.
- Extras: `/v1/kid/extras`, `/v1/kid/chest`, `/v1/kid/app-time`, `/v1/children/{id}/extras`,
  `/v1/children/{id}/app-time/extend`, `/v1/families/{id}/settings`.
- Data: `GET /v1/families/{id}/export`, `POST /v1/families/{id}/delete` and `POST /v1/children/{id}/delete` (`{pin}`),
  `DELETE /v1/families/{id}/caregivers/me`.
- `today` also returns `streak`, `week`, and each habit's `boosts` and `multiplier`. Worlds are `camp`, `mosswood`, `sky`.
## 4. How the screens map

| Screen | Change |
|---|---|
| Login / Register | Google button (plus dev login when enabled). After sign-in: `GET /v1/me`; no family → family setup (create with name, time zone, PIN, your name and role; or join with invite code). Remove local email/password registration. |
| Add child (part of register today) | `POST /v1/families/{id}/children` with name, age, buddy, world and routine times. |
| Habit setup (`#/setup`) | Becomes per child: routine times per part of the day (`PATCH /v1/children/{id}`). |
| Habit coach (`AiHelpers`) | Replace with "Describe a problem" → pain points → show suggested goals with reasons and habit options (recommended pre-ticked), max 6 → `PUT plan`. Show `safety.guidance` when returned. |
| Quest library (`#/quests/:child`) | Edit the plan: goals and their options from `GET plan`, saving with `PUT plan`; pause/remove/retime with `PATCH child-habits`. |
| Parent dashboard | Approvals from `GET /approvals` with approve / not yet; each child's level, Sparks and points from `GET /children/{id}/today`; invite code and caregivers from the family. |
| Rewards page (`#/rewards`) | "Record a reward": `POST redemptions` with points and note; history from `GET /points`. |
| Kid picker / kid quests (`#/kid`, `#/kid/:child`) | Choosing a child calls `kid-session` (caregiver token), then the kid screens use the kid token: `GET /v1/kid/today`, `POST /v1/kid/completions`. Leaving kid mode: PIN via `pin/verify`. |
| Kid shop (`#/shop/:child`) | Balance and history from `GET /v1/kid/points`. Spending is recorded by a parent, so turn "buy" into "ask a grown-up" (local only) or hide it. |

Keep in the browser for now (no backend yet): explorable worlds and discoveries, the treasure chest, buddy
accessories and wardrobe, check-ins, surprises, mystery box, point multipliers (golden quest, streaks, double
days: the backend pays base points only), and the kid-mode time limit. The Groq agent server is no longer
needed for the coach or planner; buddy lines and check-in moderation may keep using it if `GROQ_API_KEY` is set,
otherwise their built-in fallbacks apply.

Habit ids from the frontend's library are the same in the backend catalog (`wake`, `bedtime`, `brush-teeth`,
`story-time`, `make-bed`, …), with two additions: `tidy-space` and `day-chat`. Ages, buddies and the level
thresholds match the frontend.

## 5. Test it locally

1. `npm install`, then `npm run dev` and open http://localhost:5180.
2. Sign in (Google, or dev login as `parent` with the shared password the backend owner gives you).
3. Create a family, add a child, describe a problem ("bedtime is a battle and he wants more tablet"), pick
   habits, save the plan.
4. Start kid mode for the child, tap "I did it!", leave kid mode with the PIN, approve on the dashboard, and see
   Sparks and points go up.
5. Sign in as `parent:test2` in another browser, join with the invite code, and check that both caregivers see
   the same children.

Finish with `npm test` and `npm run build` passing, and summarise what changed and anything left in the browser.
