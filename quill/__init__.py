"""quill — code-driven LLM software-dev pipeline.

The driver package. Both the bare CLI (`quill 42`) and the FastAPI service
(`quill_api`) drive the same `run_pipeline` entry point.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("quill")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0.0.0"
