"""MCP tool handlers, grouped by domain.

Every tool delegates to server.dispatch.dispatch_{read,write} so caller_id
stamping, WAL logging, and the write lock are applied consistently.
"""
