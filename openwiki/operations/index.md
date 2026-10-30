# Files

- [Settings and environment configuration](configuration.md) - Exhaustive reference for the Pydantic Settings class that drives HabitQuest runtime. Covers every field with its default, bounds, consumer, model-mode cross-field validators, .env file loading, and how render.yaml and scripts/bootstrap_local.py supply values.

- [Dependency inventory: runtime, dev and Node tools](dependencies.md)
- [Local bootstrap, migrations and Render deployment](deployment-and-migrations.md) - How to bootstrap HabitQuest locally with either Supabase or native PostgreSQL, how forward-only migrations are checksummed and applied with advisory locking, how the seed catalog and checkpoint schema are set up, and how the Render Blueprint builds, starts and health-checks the service — including the single-service worker option and its idle-sleep limitation.
- [Network Calls, Egress and Per-Deployment Allowlist](network-calls-and-egress.md) - Exhaustive inventory of every outbound connection the HabitQuest backend can make, including the process and configuration that controls it, plus the allowlist required for each deployment shape.
