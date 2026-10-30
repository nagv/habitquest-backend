Start with [CLAUDE.md](CLAUDE.md) (project map, conventions, checks, decisions) and
[docs/status.md](docs/status.md) (what is done and what is left).

<!-- OPENWIKI:START -->

## OpenWiki

This repository can have a generated `openwiki/` evidence index. It is optional just-in-time context, not required startup reading. **The generated pages were removed after the lean MVP reset because they described the earlier design; until they are regenerated from `openwiki/INSTRUCTIONS.md`, use `README.md`, `CLAUDE.md` and `docs/` instead.**

- Do not enumerate, preload, or search wikis at task start. Use retrieval when the user asks for it, when unfamiliar architecture or dependency behavior materially affects the task, or when source inspection leaves an important uncertainty. Stop once the question is grounded.
- When those conditions apply and OpenWiki retrieval tools are available, use `openwiki_search` for just-in-time context and `openwiki_read` for the relevant complete sections. If search returns `workspace_required`, ask which listed workspace to use and retry with its ID.
- Use `openwiki_list_workspaces` or `openwiki_list_wikis` when workspace membership itself needs to be discovered.
- If the retrieval tools are unavailable, read `openwiki/quickstart.md` if it exists; otherwise `README.md` and `docs/`.
- Treat source code and tests as authoritative. A brief's unknowns and review items are verification gaps, not automatic requirements.
- Prefer the narrowest quiet validation that proves the changed behavior. Preserve complete failure output.

The wiki is regenerated with the OpenWiki tool (there is no scheduled workflow in this repository). Do not hand-edit generated OpenWiki pages unless explicitly asked; prefer updating source code/docs and letting OpenWiki regenerate.

<!-- OPENWIKI:END -->
