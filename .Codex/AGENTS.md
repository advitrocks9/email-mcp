# Project conventions

- Keep the Outlook REST backend independent from token acquisition.
- Preserve Safari token extraction on macOS unless a real mailbox check proves a replacement.
- Use a dedicated Edge profile and loopback-only DevTools endpoint on Windows.
- Do not add Graph OAuth. Imperial tenant policy blocks it.
- Never expose send, permanent-delete, or empty-trash MCP tools.
- Keep mailbox mutations reversible and covered by the local audit log.
- Require unit tests on macOS and Windows before merging token-provider changes.
