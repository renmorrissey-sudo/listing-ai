"""TopAI AI Harness — Phase 1 foundation package.

The harness is the single policy / tool-registry / orchestration layer on top
of the existing Ask TopAI Claude loop. It never sends anything itself: the
model proposes, :mod:`harness.policy` disposes, and a human (or an approved
job) executes.

Submodules are imported lazily so ``import harness`` stays cheap and never
requires a live Anthropic key (the Anthropic client is built only inside the
functions that actually call the model).

No blueprint registration lives here — app.py wiring is owned elsewhere.
"""

from __future__ import annotations

__all__ = ["orchestrator", "policy", "tools", "memory", "inbox"]

_SUBMODULES = frozenset(__all__)


def __getattr__(name):
    if name in _SUBMODULES:
        import importlib

        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | set(__all__))
