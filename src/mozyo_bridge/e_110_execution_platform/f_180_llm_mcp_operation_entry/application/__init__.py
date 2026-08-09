"""``f_180_llm_mcp_operation_entry`` application layer (Redmine #15151).

The stdio server, the tool dispatch, and the tool handlers that bind the closed
tool vocabulary to the **shared** application / core processing. No handler shells
out to the CLI: every one imports the same in-process entry point the CLI command
imports (``cli-mcp-shared-application-api.md`` Invariant 1 / 5).
"""
