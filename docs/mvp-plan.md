# MVP plan

The backend is the engine for the HabitQuest frontend (`rahulwarekar20/habbit-quest`). The MVP keeps the
schema lean: no version columns, no background jobs, no proposal or plan snapshots, and a flat catalog.

## What the MVP covers

1. **Register a family** with one or more caregivers, signing in with Google. Each caregiver has a role:
   father, mother, guardian or other. Further caregivers join with the family's invite code.
2. **Onboard children** with minimal details (name, age 3-13, buddy, world, routine times). Kids use the
   family device in kid mode: a caregiver opens it, and the backend issues a short-lived kid token that can only
   read that child's quests and submit completions. Leaving kid mode needs the family PIN.
3. **Pain points to goals to habits.** The parent describes a problem in their own words. AI maps it to one to
   three pre-approved goals with a short reason, and recommends up to two habits per goal. The parent picks from
   each goal's pre-approved habits (recommended ones pre-ticked), adjusts times and days, and activates the plan.
4. **Completions.** The kid submits "I did it!" for today's habit.
5. **Approval.** A caregiver approves (1 Spark plus the habit's points) or rejects (the kid can try again).
6. **Redemption.** A caregiver records what points were used for; the amount is deducted from the balance.

## AI in habit selection

One structured model call takes the child's age and the pain-point text (no names) plus the active goals and,
per goal, the habits suited to the child's age and unlocked tier. It returns goals with reasons and recommended
habits. Ids outside the given lists are dropped, so the model cannot invent a goal or habit. A safety screen
runs first: text suggesting a child may be at risk returns guidance instead of goals. If the model fails or
takes longer than about 15 seconds, keyword matching on `goals.keywords` maps the goals instead (without
recommendations); if nothing matches, the parent picks goals from the list. Nothing changes until the parent
activates the plan.

## Sparks, levels and points

- Sparks: the number of approved completions. Never spent.
- Levels from Sparks: Sprout (0), Explorer (5, unlocks tier 2 habits), Adventurer (15, unlocks tier 3),
  Hero (30), Legend (50).
- Points: the sum of `points_ledger`. Approval adds the habit's points (tier 1: 10, tier 2: 15, tier 3: 20);
  a redemption subtracts them. The frontend's multipliers (golden quest, streak, double days) come later.

## Steps

Each step is one pull request with tests at 100% coverage.

1. **Reset (done):** the 11-table schema with row-level security, the seeded catalog, catalog
   endpoints, removal of the versioned planning and editorial code.
2. **Accounts (done):** Google sign-in (`POST /v1/auth/google` checks Google's ID token and returns our token), family
   creation and joining by invite code, caregiver roles, family PIN, kid tokens, CORS for the frontend.
3. **Children and AI onboarding:** child profiles; pain points mapped to goals with recommendations; habit
   picking by age and tier; plan activation; editing the plan.
4. **Daily loop:** today's habits in the family's time zone, submit, approve or reject, Sparks, level, points.
5. **Redemption:** record a redemption against the balance; points history.
6. **Frontend wiring:** a typed API client in the frontend; Google sign-in, onboarding, kid mode and approvals
   on the API. Worlds, the treasure chest and check-ins stay in the browser for now.

## Later

Rewards catalog and kid requests, point multipliers, check-ins and their safety screening, buddy lines,
family deletion and data export, retention rules, and a paid Render plan (the free database expires).
