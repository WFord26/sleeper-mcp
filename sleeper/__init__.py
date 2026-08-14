"""
Shared core for the Sleeper fantasy tooling.

Per ADR 001, this package is the single source of truth for API access,
caching, and scoring. Two adapters sit on top of it:

  * sleeper_fantasy_mcp.py  the MCP server (conversational surface)
  * web/app.py              the dashboard (browser surface)

Nothing in this package returns a string meant for human display. Modules here
return typed structures; rendering belongs to the adapters.
"""

__version__ = "2.0.0"
