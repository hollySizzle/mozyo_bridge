"""execution_platform Feature package `f_180_llm_mcp_operation_entry` (#15148).

The LLM-facing standard operation entry: a **local MCP server** exposing the
high-level mozyo-bridge operations as typed tools, so an LLM reaches workflow /
identity / authority / send-safety judgement through the same shared application
processing the CLI reaches — never by composing a shell command.

What lands here (#15151):

- ``domain.jsonrpc`` — the pure JSON-RPC 2.0 envelope + stdio framing rules.
- ``domain.tool_catalog`` — the **closed** tool vocabulary and its typed schemas,
  plus the pinned negative surface (no arbitrary command, shell argv, raw pane /
  tmux operation, or mutating handoff / sublane tool is representable).
- ``domain.unit_selector`` — the exact ``UnitRecord`` identity a read-only Unit
  query must supply, fail-closed on missing / ambiguous / foreign selectors.
- ``domain.unit_state`` — the Unit read model: **independent** workflow / runtime
  / delivery / health axes, every observed field carrying ``source`` /
  ``observed_at`` / ``freshness``, with ``unknown`` / ``unconfirmed`` preserved
  rather than collapsed.
- ``application.mcp_server`` / ``application.tool_dispatch`` — the non-interactive
  stdio server and the typed tool dispatch.
- ``application.read_plan_tools`` / ``application.unit_state_tool`` — the handlers,
  each calling shared application / core processing **in-process**.

What lands here (#15152):

- ``domain.tool_schema_subset`` — the closed JSON Schema subset validator
  (mechanical carve-out from ``tool_catalog``).
- ``application.mutation_tools`` — the three declared mutating tool handlers
  (``handoff_send`` / ``handoff_reply`` / ``sublane_start``), each calling the
  typed shared application service the CLI calls (#15149's ``run_handoff``,
  #15152's ``run_sublane_start``) so durable anchor / role / lane / authority
  gates run identically for both entries and refuse before any side effect.
  The catalog declares them in the closed ``MUTATING_TOOL_NAMES`` set; the
  surface guard still forbids any pane locator / tmux target / command-string
  input on every tool.

Design sources of truth: ``vibes/docs/logics/cli-mcp-shared-application-api.md``
(the shared boundary #15149 established), ``vibes/docs/logics/unit-target-model.md``
(Unit / Target identity), ``vibes/docs/logics/managed-state-model.md`` and
``vibes/docs/logics/ack-completion-receiver-state.md`` (why runtime observation is
never promoted to workflow truth or task completion),
``vibes/docs/logics/local-mcp-tool-surface.md`` (the read/plan surface contract),
and ``vibes/docs/logics/mcp-mutation-tool-authority.md`` (the #15152 mutating
surface contract).

Boundary: the managed LLM entry switch is #15150, ticketless / cross-workspace
handoff rails are not published as tools in this round, and no external plugin
API is exposed here.
"""
