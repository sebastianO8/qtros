"""Read → resolve → flatten → validate → freeze → hash (ARCH-002 §4.8, §4.9, D-5).

One pass, one artifact. ``BacktestConfig`` names its parts by identifier; the loader
resolves each from ``config/{family}/`` exactly once and returns a fully flattened
:class:`ResolvedBacktestConfig`. That object — not the composed originals — is what gets
hashed and archived as ``results/{run_id}/config_resolved.yaml``.

YAML anchors, aliases, and merge keys are refused outright. They make "what values actually
ran?" hard to answer, and that is the only question ``results/`` exists to answer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final, TypeVar

import yaml
from pydantic import Field, ValidationError

from qtros.configuration.hashing import ConfigHashes, compute_hashes
from qtros.configuration.models.backtest import BacktestConfig
from qtros.configuration.models.base import QtrosConfig
from qtros.configuration.models.calendar import CalendarConfig
from qtros.configuration.models.features import FeatureSetConfig
from qtros.configuration.models.friction import FrictionConfig
from qtros.configuration.models.instrument import InstrumentConfig
from qtros.configuration.models.reporting import ReportingConfig
from qtros.configuration.models.risk import RiskLimitsConfig
from qtros.configuration.models.source import DataSourceConfig
from qtros.configuration.models.strategy import StrategyConfig
from qtros.domain.errors import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

CURRENT_SCHEMA_VERSION: Final[int] = 1

CONFIG_FAMILIES: Final[Mapping[str, str]] = {
    "backtest": "backtest",
    "instrument": "instruments",
    "source": "sources",
    "strategy": "strategies",
    "risk": "risk",
    "friction": "friction",
    "reporting": "reporting",
    "calendar": "calendars",
    "features": "features",
}
"""Family name to directory under the config root."""

_ANCHOR = re.compile(r"(^|[\s\[{,])&[A-Za-z0-9_-]+")
_ALIAS = re.compile(r"(^|[\s\[{,:])\*[A-Za-z0-9_-]+")
_MERGE = re.compile(r"(^|\s)<<\s*:")

Migration = Callable[[dict[str, Any]], dict[str, Any]]

T = TypeVar("T", bound=QtrosConfig)

MIGRATIONS: Final[dict[tuple[str, int], Migration]] = {}
"""``(family, from_version)`` → function producing the next version's document.

Empty in Phase 1. The table exists now so that a 2026 config still parses in year three;
adding the first migration is a one-line change rather than a redesign.
"""


class ResolvedBacktestConfig(QtrosConfig):
    """Every configuration a run needs, flattened, frozen, and ready to hash.

    Attributes:
        backtest: The composition record.
        instrument: Resolved instrument.
        source: Resolved data source.
        strategy: Resolved strategy declaration.
        risk: Resolved risk limits.
        friction: Resolved friction model parameters.
        reporting: Resolved scoring parameters.
        calendar: Resolved session calendar, from the instrument's ``calendar_id``.
        features: Resolved feature set, from the backtest's ``feature_set_id``.
    """

    backtest: BacktestConfig
    instrument: InstrumentConfig
    source: DataSourceConfig
    strategy: StrategyConfig
    risk: RiskLimitsConfig
    friction: FrictionConfig
    reporting: ReportingConfig = Field()
    calendar: CalendarConfig
    features: FeatureSetConfig

    def hashes(self) -> ConfigHashes:
        """Return the three Directive 10 hashes for this resolved configuration."""
        return compute_hashes(self)


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def _refuse_composition(text: str, path: Path) -> None:
    """Reject anchors, aliases, and merge keys (D-5).

    Args:
        text: Raw YAML.
        path: File, for the error.

    Raises:
        ConfigurationError: If any composition construct is present.
    """
    stripped = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
    for pattern, what in ((_MERGE, "merge key"), (_ANCHOR, "anchor"), (_ALIAS, "alias")):
        if pattern.search(stripped):
            raise ConfigurationError(
                f"YAML {what} is not permitted; configuration composes by reference only "
                "(ARCH-002 D-5)",
                path=str(path),
            )


def load_yaml_document(path: Path) -> dict[str, Any]:
    """Read one YAML file into a plain mapping.

    Args:
        path: File to read.

    Returns:
        The top-level mapping. Floats are left as floats so that the schema's
        ``StrictDecimal`` can refuse them with a clear message.

    Raises:
        ConfigurationError: If the file is missing, is not a mapping, or uses anchors.
    """
    if not path.is_file():
        raise ConfigurationError("configuration file not found", path=str(path))
    text = path.read_text(encoding="utf-8")
    _refuse_composition(text, path)
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError("configuration file is not valid YAML", path=str(path)) from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigurationError("configuration file must be a mapping", path=str(path))
    return loaded


def _migrate(family: str, document: dict[str, Any], path: Path) -> dict[str, Any]:
    """Bring a document up to the current schema version.

    Args:
        family: Config family.
        document: Raw document.
        path: File, for errors.

    Returns:
        A document at :data:`CURRENT_SCHEMA_VERSION`.

    Raises:
        ConfigurationError: If the version is newer than this code, or no migration path
            exists from an older one.
    """
    version = document.get("schema_version", CURRENT_SCHEMA_VERSION)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ConfigurationError("schema_version must be a positive integer", path=str(path))
    if version > CURRENT_SCHEMA_VERSION:
        raise ConfigurationError(
            "configuration was written by a newer schema than this code understands",
            path=str(path),
            file_version=version,
            supported=CURRENT_SCHEMA_VERSION,
        )
    current = dict(document)
    while version < CURRENT_SCHEMA_VERSION:
        step = MIGRATIONS.get((family, version))
        if step is None:
            raise ConfigurationError(
                "no migration path for this schema version",
                path=str(path),
                family=family,
                from_version=version,
            )
        current = step(current)
        version += 1
        current["schema_version"] = version
    return current


def _validate(model: type[T], document: Mapping[str, Any], path: Path) -> T:
    """Validate a document against a model, re-raising as a ConfigurationError."""
    try:
        return model.model_validate(document)
    except ValidationError as exc:
        raise ConfigurationError(
            f"{model.__name__} failed validation",
            path=str(path),
            errors=[
                {"loc": ".".join(str(part) for part in err["loc"]), "msg": err["msg"]}
                for err in exc.errors()
            ],
        ) from exc


# --------------------------------------------------------------------------------------
# Loader
# --------------------------------------------------------------------------------------


class ConfigLoader:
    """Resolves a backtest composition against a config root directory.

    Args:
        root: Directory containing the family subdirectories (``instruments/``, …).

    Raises:
        ConfigurationError: If the root does not exist.
    """

    __slots__ = ("_root",)

    def __init__(self, root: Path) -> None:
        if not root.is_dir():
            raise ConfigurationError("config root is not a directory", root=str(root))
        self._root: Path = root

    @property
    def root(self) -> Path:
        """Return the config root."""
        return self._root

    def _family_path(self, family: str, identifier: str) -> Path:
        """Return the file for one identifier within a family."""
        if "/" in identifier or "\\" in identifier or identifier.startswith("."):
            raise ConfigurationError(
                "configuration identifiers may not contain path separators",
                family=family,
                identifier=identifier,
            )
        return self._root / CONFIG_FAMILIES[family] / f"{identifier}.yaml"

    def load_one(self, family: str, identifier: str, model: type[T]) -> T:
        """Load and validate a single family member.

        Args:
            family: Family name from :data:`CONFIG_FAMILIES`.
            identifier: File stem.
            model: Model to validate against.

        Returns:
            The validated, frozen model.
        """
        path = self._family_path(family, identifier)
        document = _migrate(family, load_yaml_document(path), path)
        return _validate(model, document, path)

    def _load_strategy(self, strategy_id: str, strategy_version: str) -> StrategyConfig:
        """Find the strategy file whose id and version match the reference.

        Strategy files are matched by content rather than by file name, so a file may be
        named for readability (``audjpy_sweep_v1_0.yaml``) without the loader guessing a
        naming convention.

        Args:
            strategy_id: Registry key.
            strategy_version: Version requested.

        Returns:
            The matching strategy config.

        Raises:
            ConfigurationError: If no file or more than one file matches.
        """
        directory = self._root / CONFIG_FAMILIES["strategy"]
        matches: list[StrategyConfig] = []
        for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
            document = _migrate("strategy", load_yaml_document(path), path)
            if (
                document.get("strategy_id") == strategy_id
                and document.get("strategy_version") == strategy_version
            ):
                matches.append(_validate(StrategyConfig, document, path))
        if not matches:
            raise ConfigurationError(
                "no strategy config matches the reference",
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                searched=str(directory),
            )
        if len(matches) > 1:
            raise ConfigurationError(
                "more than one strategy config matches the reference",
                strategy_id=strategy_id,
                strategy_version=strategy_version,
            )
        return matches[0]

    def resolve(self, backtest: BacktestConfig) -> ResolvedBacktestConfig:
        """Resolve every reference in a composition.

        Args:
            backtest: The composition record.

        Returns:
            The flattened configuration.

        Raises:
            ConfigurationError: If a reference cannot be resolved or the parts disagree
                (for example, the source describes a different instrument).
        """
        instrument = self.load_one("instrument", backtest.instrument_id, InstrumentConfig)
        source = self.load_one("source", backtest.source_id, DataSourceConfig)
        strategy = self._load_strategy(
            backtest.strategy_ref.strategy_id, backtest.strategy_ref.strategy_version
        )
        risk = self.load_one("risk", backtest.risk_ref, RiskLimitsConfig)
        friction = self.load_one("friction", backtest.friction_ref, FrictionConfig)
        reporting = self.load_one("reporting", backtest.reporting_ref, ReportingConfig)
        calendar = self.load_one("calendar", instrument.calendar_id, CalendarConfig)
        features = self.load_one("features", backtest.feature_set_id, FeatureSetConfig)

        if source.instrument_id != instrument.instrument_id:
            raise ConfigurationError(
                "data source describes a different instrument than the backtest names",
                source_instrument=source.instrument_id,
                backtest_instrument=instrument.instrument_id,
            )
        if source.timeframe not in strategy.supported_timeframes:
            raise ConfigurationError(
                "strategy does not support the source timeframe",
                timeframe=str(source.timeframe),
                supported=sorted(str(t) for t in strategy.supported_timeframes),
            )
        return ResolvedBacktestConfig(
            backtest=backtest,
            instrument=instrument,
            source=source,
            strategy=strategy,
            risk=risk,
            friction=friction,
            reporting=reporting,
            calendar=calendar,
            features=features,
        )

    def load_backtest(self, identifier: str) -> ResolvedBacktestConfig:
        """Load a composition by identifier and resolve it.

        Args:
            identifier: File stem under ``config/backtest/``.

        Returns:
            The flattened configuration.
        """
        return self.resolve(self.load_one("backtest", identifier, BacktestConfig))
