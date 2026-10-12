"""One module per strategy. Modules here are mutually independent (import-linter).

Importing this package registers every strategy, so a config can name any of them.
"""

from __future__ import annotations

from qtros.alpha.strategies import audjpy_asian_sweep, vwap_pullback

__all__ = ["audjpy_asian_sweep", "vwap_pullback"]
