-- Parents resolve plan flags after reviewing them; confirming a newer plan resolves the older plan's flags.
GRANT UPDATE(resolved_at) ON hq.plan_flags TO hq_api;
