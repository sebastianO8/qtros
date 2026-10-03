"""Results writer (ARCH-002 §6, Rule 2).

The only package other than ``data/`` permitted to touch the filesystem, and only under
``results/``. Run directories are written once and never modified.
"""

from __future__ import annotations

from qtros.persistence.registry import export_registry, scan_results
from qtros.persistence.serialize import to_jsonable
from qtros.persistence.writer import RunWriter, WrittenRun, git_state, library_versions, run_id_for

__all__ = [
    "RunWriter",
    "WrittenRun",
    "export_registry",
    "git_state",
    "library_versions",
    "run_id_for",
    "scan_results",
    "to_jsonable",
]
