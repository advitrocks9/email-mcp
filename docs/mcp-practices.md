# MCP Practices Applied Here

This server is a local stdio MCP server for personal email. The design follows the
current MCP guidance that matters for this threat model.

## Local Server Execution

- Use `stdio`, not a long-running unauthenticated HTTP listener.
- Document the exact startup command in the README and Codex/Claude config.
- Keep the command deterministic with `uv run --frozen --directory ...`.
- Keep secrets in `.env` or the path selected by `EMAIL_MCP_CONFIG`, which is never committed.

Source: <https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices>

## Scope and Capability Boundaries

- Gmail uses `gmail.modify`, not a broader mail scope.
- Send, permanent delete, and empty trash are not registered as MCP tools.
- Mutations are reversible where the provider API permits it.
- Each mutation writes an audit entry to `~/.email-mcp/actions.log`.

Source: <https://modelcontextprotocol.io/specification/2025-06-18/basic/security_best_practices>

## Tool Design for Agents

- Tool names are specific verbs: `list_messages`, `search_messages`,
  `get_message`, `batch_move_messages`, `bulk_apply_to_search`,
  `list_recent_actions`, `create_draft`, `undo_last`.
- Tool descriptions explain when to call the tool, not just what it does.
- Parameters use literals and bounded values where useful:
  - `provider`: `gmail`, `outlook`, or `both`
  - mutation provider: `gmail` or `outlook`
  - `limit`: 1 to 50
  - batch id lists: 1 to 200
  - bulk query limit: 1 to 200
  - `undo_last.n`: 1 to 10
- Parameter descriptions explain provider-scoped ids, pagination cursors, dry-run
  behavior, and untrusted email bodies.
- MCP `ToolAnnotations` mark read-only, idempotent, destructive, and open-world
  behavior for clients that use those hints.
- Query-scoped mutation defaults to `dry_run=true`. Agents should show the user the
  sample and count fields before re-running with `dry_run=false`.
- Batch tools return per-message success/error rows so the agent can report partial
  progress rather than treating a cleanup as all-or-nothing.

Sources:

- <https://modelcontextprotocol.io/specification/draft/server/tools>
- <https://modelcontextprotocol.io/docs/develop/clients/client-best-practices>

## Email-Specific Prompt Injection Boundary

Email bodies come from outside the trusted conversation. Agents should summarize
and extract tasks from message bodies, but they should not execute instructions
contained in those bodies.

The `get_message` tool description and `include_body` parameter description both
state that boundary because message bodies are the highest-risk output.
