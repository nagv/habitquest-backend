# Files

- [Catalog model, eligibility, scoring and template coverage](catalog-and-eligibility.md) - The versioned catalog contains reviewed goals, concerns, habits, chapters, and releases. A deterministic pipeline filters eligible habits by child profile constraints, scores the survivors, builds a reviewed template plan that the LLM planner must respect, and enforces strict coverage semantics.

- [Agent roles, prompts and code-enforced pick validation](planning-agents.md) - The restricted Deep Agents runtime for parent intake planning: a mapper, a Director delegating to a habit-planner subagent with only a server-scoped search_catalog tool, a mandatory Reviewer, and the BoundedChat budget/bit-cost guard that keep the model from publishing anything unchecked.

