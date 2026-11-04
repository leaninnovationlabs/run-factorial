"""FastAPI application: read models, admin, and the wake-up signals (webhook, refresh).

No re-exports: the uvicorn target is `orchestrator.api.serve:app`, and importing the route tree as
a side effect of `import orchestrator.api` would drag FastAPI into the worker and Alembic.
"""

from __future__ import annotations
