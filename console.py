"""Stderr logging helper for MCP stdio transport safety."""
import sys

def log(*args, **kwargs):
    """Print to stderr with flush (MCP stdout is protocol channel)."""
    print(*args, **kwargs, file=sys.stderr, flush=True)