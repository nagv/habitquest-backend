ALTER TABLE hq.jobs ADD COLUMN kind text NOT NULL DEFAULT 'plan' CHECK(kind IN ('plan','analysis'));
ALTER TABLE hq.jobs ADD COLUMN analysis jsonb;
CREATE TABLE hq.policy(name text PRIMARY KEY,version int NOT NULL CHECK(version>0),max_active_habits int NOT NULL CHECK(max_active_habits BETWEEN 1 AND 3));
INSERT INTO hq.policy VALUES('onboarding',1,3);
GRANT SELECT ON hq.policy TO hq_api,hq_worker;
