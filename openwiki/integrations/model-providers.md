---
type: integration
title: "Model provider integration and LangGraph persistence"
description: >
  How BoundedChat unifies four model backends into a single LangChain interface,
  the exact client construction per provider, the Deep Agents / LangGraph
  checkpointer wiring via PostgresSaver, and the intentional suppression of
  LangSmith tracing around model workflows.
tags: [llm, langchain, langgraph, providers, tracing, persistence, deepagents]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-30T00:13:47.181Z
sources:
  - id: openwiki-source-5f5b95b3d6a215fa02ceb945
    resource: repo://.env.example
  - id: openwiki-source-f70156010a8eb5325870bfc6
    resource: repo://render.yaml
  - id: openwiki-source-aac0765bbf153c93ec271d04
    resource: repo://scripts/setup_db.py
  - id: openwiki-source-1d39ca689a3e1562bb21214c
    resource: repo://src/habitquest_backend/config.py
  - id: openwiki-source-5891884c8188a825b0aaec3e
    resource: repo://src/habitquest_backend/planning.py
  - id: openwiki-source-6e803a3ec70ccc9d4eb9e11b
    resource: repo://src/habitquest_backend/worker.py
  - id: openwiki-source-6300e00bb954ddbbea3f1c0d
    resource: repo://tests/test_hosting.py
generated: { by: "openwiki/0.7.2", at: "2026-10-30T00:13:47.181Z" }
---

# Model provider integration and LangGraph persistence

HabitQuest does not commit to a single LLM vendor. A single [`BoundedChat`](repo://src/habitquest_backend/planning.py#L108) class wraps every supported backend in a uniform LangChain `BaseChatModel` contract, enforces a shared budget (time, token reserve, and model-call count), and lets the rest of the planning pipeline stay vendor-agnostic. This page explains the four model modes, how each client is built, how LangGraph checkpoints survive retries, and why LangSmith tracing is deliberately disabled.

For the surrounding planning pipeline, see [Agent roles, prompts and code-enforced pick validation](../concepts/planning-agents.md). For the worker loop that owns the model budget, see [Runtime topology: API process, worker loop and job leases](../architecture/runtime-and-processes.md).

## Model modes and configuration

The active provider is selected by a single setting, `model_mode`, whose valid values are enforced by a [`Literal`](repo://src/habitquest_backend/config.py#L11) on [`Settings`](repo://src/habitquest_backend/config.py#L7):

| Mode | Client class | Required settings | Typical use |
|---|---|---|---|
| `fake` | Internal `_fake()` | none | Local development, deterministic tests, CI |
| `ollama` | `ChatOllama` | `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | Local GPU inference |
| `google` | `ChatGoogleGenerativeAI` | `GOOGLE_MODEL`, `GOOGLE_API_KEY` | Gemini via Google GenAI |
| `openai` | `ChatOpenAI` | `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY` | Any OpenAI-compatible endpoint (e.g., Nebius) |

[`Settings`](repo://src/habitquest_backend/config.py#L7) rejects an incomplete configuration at import time: **google** requires both a model id and an API key; **openai** requires a base URL, a model id, and an API key ([`google_configured`](repo://src/habitquest_backend/config.py#L34-L40)). The default is `fake`, which means a fresh checkout runs without external credentials ([`.env.example`](repo://.env.example#L3), [`render.yaml`](repo://render.yaml#L45-L46)).

The full environment surface is documented in [Operations: Configuration](../operations/configuration.md).

### Provenance in proposals

Every planning run writes the model identity and a fallback flag into the proposal record:

```python
context["model"] = {
    "fake": "fake",
    "ollama": f"ollama:{settings.ollama_model}",
    "google": f"google:{settings.google_model}",
    "openai": f"openai:{settings.openai_model}",
}[settings.model_mode]
```

This string is stored in the `model` field of the emitted proposal, and the `fallback` boolean is set to `true` when the agent path fails validation and the deterministic template is used instead ([`build_proposal`](repo://src/habitquest_backend/planning.py#L410-L519)). Together, `<mode>:<model id>` and `fallback: true` are the **only per-run model provenance**.

## Provider construction details

[`BoundedChat._generate()`](repo://src/habitquest_backend/planning.py#L125) builds the concrete client on every call, which means the timeout value is always recomputed from the remaining run budget. No client is cached across invocations.

### Fake mode

In `fake` mode, calls never leave the process. The mapper uses a deterministic keyword check ([`fake_mapping`](repo://src/habitquest_backend/planning.py#L84)): it scans parent concerns with regexes for keywords like `bed`, `meals`, `tidy`, etc. The director, planner, and reviewer roles return hard-coded tool-call patterns or JSON strings from [`_fake()`](repo://src/habitquest_backend/planning.py#L186). Because the behaviour is keyword-driven and not language understanding, it is fast, offline, and perfectly reproducible.

### Ollama mode

When `model_mode` is `ollama`, `BoundedChat` constructs a [`ChatOllama`](repo://src/habitquest_backend/planning.py#L168) instance with:

- `model` = [`settings.ollama_model`](repo://src/habitquest_backend/config.py#L12) (default `qwen3:8b`)
- `base_url` = [`settings.ollama_base_url`](repo://src/habitquest_backend/config.py#L13) (default `http://127.0.0.1:11434`)
- `temperature=0`
- `num_predict=1024`
- `client_kwargs={"timeout": remaining}`

The timeout is injected into the HTTP client used by the Ollama SDK, not into LangChain's own timeout parameter.

### Google mode

When `model_mode` is `google`, `BoundedChat` constructs a [`ChatGoogleGenerativeAI`](repo://src/habitquest_backend/planning.py#L141) with:

| Argument | Value | Source |
|---|---|---|
| `model` | model id | `settings.google_model` |
| `google_api_key` | API key string | `settings.google_api_key.get_secret_value()` |
| `temperature` | `0` | hard-coded |
| `max_output_tokens` | `1024` | hard-coded |
| `timeout` | remaining seconds | computed from run budget |
| `max_retries` | `1` | hard-coded |

Tests capture these arguments without calling Google:

- [`test_google_adapter_contract_without_calling_google`](repo://tests/test_hosting.py#L15) asserts that `ChatGoogleGenerativeAI` receives the model name, key, zero temperature, positive timeout, and bound tools.
- [`test_google_mode_requires_model_and_key`](repo://tests/test_hosting.py#L42) asserts that [`Settings`](repo://src/habitquest_backend/config.py#L7) raises `ValidationError` when the key is missing.

### OpenAI-compatible mode

When `model_mode` is `openai`, `BoundedChat` constructs a [`ChatOpenAI`](repo://src/habitquest_backend/planning.py#L155) pointing at any OpenAI-compatible endpoint. The arguments are:

| Argument | Value | Source |
|---|---|---|
| `model` | model id | `settings.openai_model` |
| `base_url` | endpoint URL | `settings.openai_base_url` |
| `api_key` | API key | `settings.openai_api_key` |
| `temperature` | `0` | hard-coded |
| `max_completion_tokens` | `1024` | hard-coded |
| `timeout` | remaining seconds | computed from run budget |
| `max_retries` | `1` | hard-coded |

This mode is used for third-party providers that expose an OpenAI-compatible chat-completion endpoint. The model **must support tool calling**; the planning pipeline depends on it.

Tests verify the contract without network access:

- [`test_openai_compatible_adapter_contract_without_calling_the_provider`](repo://tests/test_hosting.py#L78) captures the constructor arguments and asserts they match the expected endpoint, model, key, temperature, timeout, and token cap.
- [`test_openai_mode_requires_endpoint_model_and_key`](repo://tests/test_hosting.py#L115) asserts that [`Settings`](repo://src/habitquest_backend/config.py#L7) rejects incomplete OpenAI configurations.

### Summary table: client construction

| Mode | Client class | Settings used | Output cap | Timeout | Retries |
|---|---|---|---|---|---|
| `fake` | n/a (internal) | none | n/a | n/a | n/a |
| `ollama` | `ChatOllama` | `OLLAMA_MODEL`, `OLLAMA_BASE_URL` | `num_predict=1024` | `client_kwargs["timeout"]` | default SDK |
| `google` | `ChatGoogleGenerativeAI` | `GOOGLE_MODEL`, `GOOGLE_API_KEY` | `max_output_tokens=1024` | `timeout=remaining` | `1` |
| `openai` | `ChatOpenAI` | `OPENAI_BASE_URL`, `OPENAI_MODEL`, `OPENAI_API_KEY` | `max_completion_tokens=1024` | `timeout=remaining` | `1` |

All real-provider modes set `temperature=0`. The `fake` mode is the **deterministic fake**.

## Budget enforcement

`BoundedChat` is not just an adapter; it is a budget guard. Every call is preceded and followed by checks that prevent a slow or runaway model from exhausting the worker's resources.

### Time budget

Before generating, `_generate` computes the remaining time from the run start (`self.started`) and from an optional wall-clock `deadline`. If either is exhausted, it raises [`BudgetExceeded`](repo://src/habitquest_backend/planning.py#L80). After the model returns, it checks again: a response that arrived after the deadline is rejected ([`planning.py:180-183`](repo://src/habitquest_backend/planning.py#L180-L183)).

### Model-call and token budgets

The `charge()` callback (injected by the worker) increments a counter and a token reserve before every external call. If the job has already reached `max_model_calls` or the new token reserve would exceed `max_run_tokens`, `charge` raises `BudgetExceeded` and the run fails before the network request is sent ([`worker.py:96-110`](repo://src/habitquest_backend/worker.py#L96-L110)).

The token reserve is conservative: it is sized as the JSON-serialised request payload plus `2048` bytes, not the actual prompt tokens. This is intentional; it is a pessimistic upper bound that cannot be gamed by prompt shaping.

### Tool binding

All modes support tools via LangChain's standard `bind_tools()`. `BoundedChat` stores the tool specs and, for real providers, forwards them before `invoke()` ([`bind_tools`](repo://src/habitquest_backend/planning.py#L122-L123)). The fake mode encodes tool calls directly into the [`AIMessage`](repo://src/habitquest_backend/planning.py#L193) response.

## Deep Agents wiring and LangGraph persistence

The planning pipeline uses a Deep Agents Director-Planner arrangement. The Director delegates to a single subagent called `habit-planner`, which is a LangChain agent with one tool: `search_catalog(goal_id)`. The Director itself runs through `create_deep_agent` with a [`StateBackend`](repo://src/habitquest_backend/planning.py#L16) and a [`PostgresSaver`](repo://src/habitquest_backend/worker.py#L12) checkpointer.

### Checkpointer setup

In [`worker.run_once`](repo://src/habitquest_backend/worker.py#L81), the worker opens a dedicated `psycopg` connection to the same database, sets the role to `hq_worker` and the search path to `hq_checkpoint`, then passes the connection to [`PostgresSaver`](repo://src/habitquest_backend/worker.py#L163). This is the same `PostgresSaver` that `scripts/setup_db.py` uses to create `hq_checkpoint` tables at install time.

### Thread identity and resume

Each job has a `thread_id` (derived from the job id and lease token) and an optional `resume_thread` (copied from the previous attempt). When a run is retried, the worker copies the checkpoint values from the old thread into the new one ([`plan_with_agents:590-594`](repo://src/habitquest_backend/planning.py#L590-L594)). This means a retry resumes from the last model turn, not from scratch.

```python
snapshot = director.get_state(config)
if resume_thread and not snapshot.values:
    previous = director.get_state({"configurable": {"thread_id": resume_thread}})
    if previous.values:
        node = "tools" if previous.next == ("model",) else "model"
        director.update_state(config, previous.values, as_node=node)
```

### Tool restriction

Before creating the Director, the code registers a harness profile that **disables** the standard Deep Agents tool set and subagent creation:

```python
register_harness_profile(
    "habitquest:bounded",
    HarnessProfile(
        excluded_tools=frozenset(
            {"ls", "read_file", "write_file", "edit_file", "glob", "grep", "execute"}
        ),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)
```

This is the only security boundary between the model and the host filesystem. The Planner cannot invoke shell commands, browse files, or spawn arbitrary subagents; its only external action is the `search_catalog` tool whose return value is built from reviewed catalog data in code.

## LangSmith tracing: an intentional boundary

The planning pipeline is wrapped in a `with tracing_context(enabled=False):` block in [`worker.py`](repo://src/habitquest_backend/worker.py#L155). Both `.env.example` and `render.yaml` also set:

- `LANGSMITH_TRACING=false`
- `LANGCHAIN_TRACING_V2=false`

This is **deliberate**, not an oversight. The reasons are:

1. **Data residency**: Parent intake text and child profile data are passed as generation inputs; sending them to a third-party tracing service would violate the system's data boundary.
2. **Determinism and cost**: `fake` and `ollama` runs are meant to be local-only. Tracing would introduce non-hermetic network dependencies.
3. **Operational noise**: The worker is a polling loop; high-volume trace submissions would produce disproportionate noise relative to signal.

Production deployments that need observability are expected to rely on the `hq.events` table (populated by [`record()`](repo://src/habitquest_backend/worker.py#L90-L94)) and structured worker logs, not on LangSmith traces.

## Testing the provider contracts

Because the real providers require credentials and network access, the test suite replaces them with transport mocks that capture constructor arguments:

| Test | What it verifies |
|---|---|
| [`test_google_adapter_contract_without_calling_google`](repo://tests/test_hosting.py#L15) | `ChatGoogleGenerativeAI` receives model, key, temperature, timeout, token cap, and tools |
| [`test_google_mode_requires_model_and_key`](repo://tests/test_hosting.py#L42) | `Settings` rejects `google` without both model and key |
| [`test_openai_compatible_adapter_contract_without_calling_the_provider`](repo://tests/test_hosting.py#L78) | `ChatOpenAI` receives base_url, model, key, temperature, timeout, and token cap |
| [`test_openai_mode_requires_endpoint_model_and_key`](repo://tests/test_hosting.py#L115) | `Settings` rejects `openai` without base URL, model, and key |
| [`test_slow_model_response_is_rejected_after_it_returns`](repo://tests/test_final_boundaries.py#L84) | A response that exceeds the run deadline is rejected with `BudgetExceeded` |

These tests act as compile-time contracts: if a future langchain-google-genai or langchain-openai release changes the constructor signature, the tests will fail before a deployment reaches a real provider.

## Related pages

- [Agent roles, prompts and code-enforced pick validation](../concepts/planning-agents.md) — the planning pipeline that consumes BoundedChat
- [Runtime topology: API process, worker loop and job leases](../architecture/runtime-and-processes.md) — the worker loop, leases, and heartbeat
- [Operations: Configuration](../operations/configuration.md) — the full environment and settings surface
- [Operations: Network calls and egress](../operations/network-calls-and-egress.md) — outbound traffic patterns
