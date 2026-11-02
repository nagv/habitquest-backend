# HabitQuest database schema

The tables of the `hq` schema, how they relate, and what each one is for. The SQL that builds them is in
`db/migrations`. [localsetup.md](localsetup.md) explains how to build a database from it.

Columns marked *kid login* come from `202611030001_kid_login.sql`.

## The whole schema

Every table and how they connect. Below this diagram are the same tables in four smaller diagrams, with their key
columns.

```mermaid
erDiagram
  users ||--o{ caregivers : "is a"
  families ||--o{ caregivers : "has"
  families ||--o{ children : "has"
  families ||--|| family_settings : "has"
  families ||--o{ rewards : "offers"
  families ||--o{ surprises : "plans"
  families ||--o{ ai_usage : "uses"
  families ||--o{ ai_reports : "gets"
  goals ||--o{ habits : "groups"
  children ||--o{ pain_points : "has"
  children ||--o{ child_goals : "works on"
  goals ||--o{ child_goals : "chosen as"
  pain_points |o--o{ child_goals : "leads to"
  children ||--o{ child_habits : "plans"
  habits ||--o{ child_habits : "used in"
  child_habits ||--o{ completions : "done as"
  children ||--o{ points_ledger : "earns"
  completions |o--o| points_ledger : "pays"
  children ||--|| child_extras : "has"
  children ||--o{ reward_requests : "asks for"
  rewards |o--o{ reward_requests : "requested as"
  children ||--o{ checkins : "writes"
  children ||--o{ surprise_reveals : "opens"
  surprises ||--o{ surprise_reveals : "opened as"
  children ||--o{ coach_messages : "discussed in"
  children ||--o{ ai_suggestions : "gets"
  habits ||--o{ ai_suggestions : "suggested in"
```

## Families and people

```mermaid
erDiagram
  users {
    uuid id PK
    text google_sub UK
    text email
    text name
  }
  families {
    uuid id PK
    text name
    text timezone
    text pin_hash "grown-ups' PIN"
    text invite_code UK "grown-ups join"
    text login_code UK "kid login"
  }
  caregivers {
    uuid family_id PK, FK
    uuid user_id PK, FK
    text display_name
    text role
  }
  children {
    uuid id PK
    uuid family_id FK
    text name
    int age
    text buddy
    text world
    jsonb routine_times
    int tier_override
    text kid_pin_hash "kid login"
  }
  family_settings {
    uuid family_id PK, FK
    int app_limit
    boolean buddy_talk
  }
  child_extras {
    uuid child_id PK, FK
    text_array accessories
    text wearing
    uuid goal FK
    int level_seen
    int journey_bonus
  }
  users ||--o{ caregivers : "is a"
  families ||--o{ caregivers : "has"
  families ||--o{ children : "has"
  families ||--|| family_settings : "has"
  children ||--|| child_extras : "has"
```

## Catalog and each child's plan

```mermaid
erDiagram
  goals {
    text id PK
    text title
    text_array keywords
    boolean is_active
  }
  habits {
    text id PK
    text goal_id FK
    text kid_title
    text anchor
    int min_age
    int max_age
    int tier
    int points
    boolean is_active
  }
  children {
    uuid id PK
  }
  pain_points {
    uuid id PK
    uuid child_id FK
    text text
    text mapped_by
    uuid created_by FK
  }
  child_goals {
    uuid child_id PK, FK
    text goal_id PK, FK
    uuid pain_point_id FK
    text_array recommended_habit_ids
    text status
  }
  child_habits {
    uuid id PK
    uuid child_id FK
    text habit_id FK
    time cue_time
    text_array days
    text status
  }
  goals ||--o{ habits : "groups"
  children ||--o{ pain_points : "has"
  children ||--o{ child_goals : "works on"
  goals ||--o{ child_goals : "chosen as"
  pain_points |o--o{ child_goals : "leads to"
  children ||--o{ child_habits : "plans"
  habits ||--o{ child_habits : "used in"
```

## Daily quests, points and rewards

```mermaid
erDiagram
  child_habits {
    uuid id PK
  }
  completions {
    uuid id PK
    uuid child_habit_id FK
    date day
    text status
    uuid decided_by FK
  }
  points_ledger {
    bigint id PK
    uuid child_id FK
    int amount
    text reason
    uuid completion_id FK, UK
    text note
  }
  children {
    uuid id PK
  }
  families {
    uuid id PK
  }
  rewards {
    uuid id PK
    uuid family_id FK
    text title
    int cost
    text kind
    int minutes
  }
  reward_requests {
    uuid id PK
    uuid child_id FK
    uuid reward_id FK
    int cost
    text status
    date day
  }
  child_habits ||--o{ completions : "done as"
  completions |o--o| points_ledger : "pays"
  children ||--o{ points_ledger : "earns"
  families ||--o{ rewards : "offers"
  children ||--o{ reward_requests : "asks for"
  rewards |o--o{ reward_requests : "requested as"
```

## Habit coach, check-ins and surprises

```mermaid
erDiagram
  children {
    uuid id PK
  }
  families {
    uuid id PK
  }
  habits {
    text id PK
  }
  coach_messages {
    bigint id PK
    uuid child_id FK
    text role
    text content
    text_array shown_habits
    boolean no_habit_match
  }
  ai_suggestions {
    uuid id PK
    uuid child_id FK
    text habit_id FK
    text source
    text kind
    text status
  }
  ai_usage {
    uuid family_id PK, FK
    date day PK
    int calls
  }
  ai_reports {
    uuid id PK
    uuid family_id FK
    text kind
    jsonb result
  }
  checkins {
    uuid id PK
    uuid child_id FK
    date day
    text mood
    text hardest FK
    text favorite FK
  }
  surprises {
    uuid id PK
    uuid family_id FK
    uuid child_id FK "null = every child"
    text kind
    int points
    date day
  }
  surprise_reveals {
    uuid surprise_id PK, FK
    uuid child_id PK, FK
  }
  children ||--o{ coach_messages : "discussed in"
  children ||--o{ ai_suggestions : "gets"
  habits ||--o{ ai_suggestions : "suggested in"
  families ||--o{ ai_usage : "uses"
  families ||--o{ ai_reports : "gets"
  children ||--o{ checkins : "writes"
  families ||--o{ surprises : "plans"
  surprises ||--o{ surprise_reveals : "opened as"
  children ||--o{ surprise_reveals : "opens"
```

A few links are left out of the diagrams to keep them readable:
- The `created_by` and `decided_by` columns of several tables, which point at `users`.
- `checkins.hardest` and `checkins.favorite`, which point at `habits`.
- `child_extras.goal`, which points at `rewards`.
- `surprises.child_id`, which points at `children`.

## What each table is for

| Area | Table | Purpose |
|---|---|---|
| People | `users` | Everyone who signs in as a grown-up, linked to their Google account. Kids aren't users: they use a kid session for one child |
| People | `families` | A household: its name and time zone, the grown-ups' PIN, the invite code another grown-up joins with, and the family code kids sign in with |
| People | `caregivers` | Which grown-ups belong to which family, with their display name and role (mother, father, guardian, other). Every family permission comes from this table |
| People | `children` | Each child: name, age, buddy, current world, the start times of their routines, and their own kid PIN |
| Catalog | `goals` | The pre-approved goals, such as "Calmer bedtimes", with the keywords that match a parent's description of a problem |
| Catalog | `habits` | The pre-approved quests under each goal: the kid's wording, time of day, suitable ages, difficulty tier and points. Only these can go into a plan |
| Plan | `pain_points` | A problem a parent described, and whether the AI, keywords or the parent matched it to goals |
| Plan | `child_goals` | Goals suggested for or chosen for a child, with the reason and the recommended quests |
| Plan | `child_habits` | The child's quest plan: which quest, at what time and on which days. Paused and removed quests stay, as history |
| Daily loop | `completions` | One row per quest per day: the kid marks it done and a grown-up approves or rejects it. Sparks are counted from approved rows |
| Daily loop | `points_ledger` | Every change to a child's points: earned for an approved quest, bonuses, spending, refunds and adjustments. The balance is the sum, and a trigger stops it going below zero |
| Rewards | `rewards` | The family's reward shop: screen time, privileges, outings, toys and mystery boxes, each with a points cost. New families get a default set |
| Rewards | `reward_requests` | A kid's request to spend points on a reward, waiting for a grown-up to approve or decline it |
| Kid extras | `child_extras` | Small per-child state: discoveries, accessories and what the buddy is wearing, the saving goal, opened chests, the last level seen, the journey bonus, and screen-time counters |
| Kid extras | `checkins` | The kid's end-of-day check-in: mood, hardest and favourite quest, stickers and a note. Worrying notes are flagged for a grown-up |
| Kid extras | `surprises` | A note or double-points day a grown-up plans for one child or all of them |
| Kid extras | `surprise_reveals` | Which child has opened which surprise, so each opens only once |
| Family | `family_settings` | Family-wide settings: the daily app time limit and whether the buddy talks |
| AI | `coach_messages` | The habit coach conversation for each child: the parent's messages, the coach's replies with sources, and app notes |
| AI | `ai_suggestions` | Plan changes the AI proposed (add, retime, pause, resume or remove a quest). Nothing changes until a grown-up accepts one with the PIN |
| AI | `ai_usage` | AI calls per family per day, so one family can't run up the provider bill |
| AI | `ai_reports` | The latest AI quest plan and weekly review shown on the dashboard |
| System | `public.hq_migrations` | Which migration files have run, with a fingerprint of each, so `setup_db.py` runs each one exactly once |
