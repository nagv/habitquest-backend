-- A coach reply that looked for habits and found none suitable, so the app can say so beside the tips.
ALTER TABLE hq.coach_messages ADD COLUMN no_habit_match boolean NOT NULL DEFAULT false;
