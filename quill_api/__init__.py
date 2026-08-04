"""quill_api — FastAPI service that owns the runs (WI-10).

Hosts the orchestrator, launches each pipeline run as a background task, holds live
in-process RunState, and exposes it over REST + SSE.
"""
