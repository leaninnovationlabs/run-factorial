"""Operations that the dashboard routes and the MCP tools share (tech-stack decision 60).

Transport-neutral: nothing under this package imports fastapi. A service raises
`api.services.errors.ServiceError`. `api/errors.py` turns that into an HTTP status, and the MCP
registry turns it into a tool error. A function moves here only when a tool calls it.
"""
