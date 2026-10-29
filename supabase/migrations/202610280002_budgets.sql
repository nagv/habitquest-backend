ALTER TABLE hq.jobs ADD COLUMN model_calls int NOT NULL DEFAULT 0;
ALTER TABLE hq.jobs ADD COLUMN reserved_tokens int NOT NULL DEFAULT 0;
