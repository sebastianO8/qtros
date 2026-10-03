"""Strategy registry (ARCH-002 D-4).

Strategies self-register under a stable identifier and version. YAML references
``strategy_id`` and ``strategy_version`` only — never an import path — so a config file is
not arbitrary code execution and a refactor cannot silently break an archived manifest.
Deleting a registered version *does* break reproduction, and the registry says so
explicitly rather than raising ``ModuleNotFoundError`` from a future year.

The registry stores opaque factories. It knows nothing about what a strategy is; that is
``alpha/``'s business, and ``configuration/`` may not import ``alpha/``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from qtros.domain.errors import ConfigurationError

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class StrategyRegistryError(ConfigurationError):
    """A strategy reference could not be resolved, or a registration was invalid."""


@dataclass(frozen=True, slots=True)
class RegisteredStrategy:
    """One (id, version) registration.

    Attributes:
        strategy_id: Stable key.
        strategy_version: Semantic version of the registered logic.
        factory: Callable producing a strategy instance from its parameters.
        qualified_name: Import path of the registered object, for the manifest only.
    """

    strategy_id: str
    strategy_version: str
    factory: Callable[..., Any]
    qualified_name: str

    @property
    def key(self) -> tuple[str, str]:
        """Return the registry key."""
        return (self.strategy_id, self.strategy_version)


class StrategyRegistry:
    """Mapping from ``(strategy_id, strategy_version)`` to a factory."""

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], RegisteredStrategy] = {}

    def register(
        self,
        strategy_id: str,
        strategy_version: str,
        factory: Callable[..., Any],
        *,
        qualified_name: str | None = None,
    ) -> RegisteredStrategy:
        """Register a factory.

        Args:
            strategy_id: Stable key, ``snake_case``.
            strategy_version: ``MAJOR.MINOR.PATCH``.
            factory: Callable producing a strategy.
            qualified_name: Import path recorded for provenance.

        Returns:
            The registration record.

        Raises:
            StrategyRegistryError: On a malformed id or version, or a duplicate key
                bound to a different object. Re-registering the same object is a no-op
                so test re-imports do not fail.
        """
        if not _ID.match(strategy_id):
            raise StrategyRegistryError("strategy_id must be snake_case", strategy_id=strategy_id)
        if not _SEMVER.match(strategy_version):
            raise StrategyRegistryError(
                "strategy_version must be MAJOR.MINOR.PATCH",
                strategy_id=strategy_id,
                strategy_version=strategy_version,
            )
        key = (strategy_id, strategy_version)
        existing = self._entries.get(key)
        if existing is not None:
            if existing.factory is factory:
                return existing
            raise StrategyRegistryError(
                "strategy id/version already registered to a different object",
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                existing=existing.qualified_name,
            )
        record = RegisteredStrategy(
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            factory=factory,
            qualified_name=qualified_name
            or f"{getattr(factory, '__module__', '?')}.{getattr(factory, '__qualname__', '?')}",
        )
        self._entries[key] = record
        return record

    def resolve(self, strategy_id: str, strategy_version: str) -> RegisteredStrategy:
        """Look up a registration.

        Args:
            strategy_id: Stable key.
            strategy_version: Version requested.

        Returns:
            The registration record.

        Raises:
            StrategyRegistryError: If the id is unknown, or the id is known but the
                version is not — with the available versions listed, because "this version
                was deleted" is a different problem from "this strategy never existed".
        """
        key = (strategy_id, strategy_version)
        if key in self._entries:
            return self._entries[key]
        versions = sorted(v for (sid, v) in self._entries if sid == strategy_id)
        if versions:
            raise StrategyRegistryError(
                "strategy version is not registered; an archived config may reference a "
                "version that has since been removed",
                strategy_id=strategy_id,
                requested=strategy_version,
                available=versions,
            )
        raise StrategyRegistryError(
            "strategy id is not registered",
            strategy_id=strategy_id,
            known=sorted({sid for (sid, _) in self._entries}),
        )

    def __contains__(self, key: object) -> bool:
        """Return whether an ``(id, version)`` pair is registered."""
        return key in self._entries

    def __iter__(self) -> Iterator[RegisteredStrategy]:
        """Iterate registrations in key order."""
        return iter(self._entries[k] for k in sorted(self._entries))

    def __len__(self) -> int:
        """Return the registration count."""
        return len(self._entries)

    def as_mapping(self) -> Mapping[tuple[str, str], RegisteredStrategy]:
        """Return a read-only snapshot."""
        return dict(self._entries)

    def clear(self) -> None:
        """Remove every registration. Test use only."""
        self._entries.clear()


strategy_registry: StrategyRegistry = StrategyRegistry()
"""The process-wide registry ``alpha/`` registers into and the loader resolves against."""
