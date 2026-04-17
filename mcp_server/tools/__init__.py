"""MCP tool registration helpers.

Each submodule exposes ``register(server, ctx)`` which registers its
tools with the given ``mcp.Server`` instance. ``server.py`` calls
them in order, so adding a new tool category is just: create a
module, implement ``register``, add an import to ``server.py``.
"""
