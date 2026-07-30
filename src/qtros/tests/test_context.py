"""T-1 availability boundary tests.

These verify the single most consequential structural property in the system: a strategy
deciding on bar T sees features computed as of bar T-1, and has no route to bar T's
feature values.

The test design relies on one trick worth stating plainly. Feature values are set equal to
their own bar index, so a context at bar 7 reporting ``6.0`` proves the shift is applied
and reporting ``7.0`` proves it is not. No arithmetic, no tolerance, and no reasoning about
rolling windows stands between the assertion and the property. A test that instead compared
computed indicator values would fail for a dozen reasons unrelated to the boundary.

The boundary is verified in three independent ways, because a single check on something
this load-bearing is a single point of failure:

1. Directly, by value, at every decidable index.
2. Structurally, by asserting the context exposes no path to the underlying frame.
3. Negatively, by asserting the factory refuses frames that would produce a double shift.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields
from decimal import Decimal

import polars as pl
import pytest

from qtros.domain.errors import ConfigurationError, SchemaContractError
from qtros.domain.feature_contracts import FeatureFrame, LagPolicy
from qtros.domain.instrument import InstrumentSpec
from qtros.domain.signals import BarContext, PortfolioView, UnknownFeatureError
from qtros.engine.context import AVAILABILITY_LAG_BARS, BarContextFactory, to_decimal

from tests.conftest import failing_audit


@pytest.fixture
def flat_view() -> PortfolioView:
    """Return a read-only portfolio view for a flat, unhalted account."""
    return PortfolioView(
        cash=Decimal("1000000"),
        closed_equity=Decimal("1000000"),
        mtm_equity=Decimal("1000000"),
        open_position=None,
        realized_pnl_to_date=Decimal("0"),
        trade_count_to_date=0,
    )


@pytest.fixture
def factory(
    build_feature_frame: Callable[..., FeatureFrame],
    instrument: InstrumentSpec,
) -> BarContextFactory:
    """Return a factory over ten bars whose feature value equals the bar index."""
    return BarContextFactory(build_feature_frame(rows=10), instrument)


class TestFeatureLagBoundary:
    """A decision at bar T may only see features from bar T-1."""

    def test_features_come_from_the_previous_bar(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """The headline assertion: at bar 7 the feature marker reads 6."""
        context: BarContext = factory.build(7, flat_view)
        assert context.features["bar_marker"] == 6.0

    def test_features_never_equal_the_current_bar_index(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """Holds at every decidable index, not just a sampled one.

        A shift applied by an off-by-one that happens to be correct in the middle of the
        frame would pass a single-index test and fail here.
        """
        for index in range(factory.first_decision_index, factory.last_decision_index + 1):
            context = factory.build(index, flat_view)
            assert context.features["bar_marker"] == float(index - AVAILABILITY_LAG_BARS)
            assert context.features["bar_marker"] != float(index)

    def test_the_feature_row_is_stamped_with_the_bar_it_came_from(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """The provenance stamp lets a log line state which bar was reasoned from."""
        context = factory.build(5, flat_view)
        expected_bar = factory.bar_at(4)
        assert context.features.as_of_ts_utc == expected_bar.ts_utc
        assert context.features.as_of_ts_utc < context.bar.ts_utc

    def test_reported_feature_lag_is_one_bar(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """Strategies can assert on the lag rather than assuming it."""
        assert factory.build(3, flat_view).feature_lag_bars == 1


class TestCurrentBarVisibility:
    """Bar T is fully visible: it has closed, so its close is a known fact.

    This is the deliberate asymmetry in the design. The availability lag guards against
    look-ahead in *feature computation*, which is the failure mode that survives code
    review. It is not an attempt to hide a completed bar — under next-bar-open execution
    the decision is taken at bar T's close and filled at T+1's open, so bar T's close was
    genuinely observable at decision time. Concealing it would model a decision nobody
    makes. DIR-001 covers the residual risk by prohibiting inline feature maths on bar data.
    """

    def test_the_context_carries_the_current_bar(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """The bar in the context is bar T, not bar T-1."""
        context = factory.build(6, flat_view)
        assert context.bar.ts_utc == factory.bar_at(6).ts_utc
        assert context.bar.open == factory.bar_at(6).open

    def test_the_full_ohlc_of_the_current_bar_is_available(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """Open, high, low, and close of the decided bar are all present."""
        context = factory.build(6, flat_view)
        assert context.bar.high >= context.bar.open >= context.bar.low
        assert context.bar.close is not None

    def test_prices_are_exact_decimals_not_binary_floats(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
        flat_view: PortfolioView,
    ) -> None:
        """Conversion goes through text, so 0.1 stays 0.1.

        ``Decimal(0.1)`` yields the binary float's true value and would poison every cash
        figure derived from it. This is the boundary where that must not happen.
        """
        frame = build_feature_frame(rows=5, prices=["95.1", "95.2", "95.3", "95.4", "95.5"])
        context = BarContextFactory(frame, instrument).build(2, flat_view)
        assert context.bar.open == Decimal("95.3")
        assert to_decimal(0.1) == Decimal("0.1")


class TestDecisionWindow:
    """Bars without a lagged feature row or a following execution bar are not decided on."""

    def test_first_decision_index_is_the_availability_lag(
        self, factory: BarContextFactory
    ) -> None:
        """Bar zero has no predecessor to draw features from."""
        assert factory.first_decision_index == AVAILABILITY_LAG_BARS == 1

    def test_building_at_index_zero_is_refused(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """Refused rather than served with improvised values."""
        with pytest.raises(IndexError, match="availability lag"):
            factory.build(0, flat_view)

    def test_last_decision_index_excludes_the_final_bar(
        self, factory: BarContextFactory
    ) -> None:
        """A decision on the final bar has no bar to fill against.

        Deciding there and dropping the unfillable order would understate trade count and
        overstate the strategy's selectivity.
        """
        assert factory.last_decision_index == factory.bar_count - 2

    def test_feature_row_lookup_below_the_lag_is_refused(
        self, factory: BarContextFactory
    ) -> None:
        """The lower-level accessor enforces the same rule as ``build``."""
        with pytest.raises(IndexError, match="no feature row"):
            factory.feature_row_for(0)

    def test_bar_lookup_outside_the_frame_is_refused(
        self, factory: BarContextFactory
    ) -> None:
        """Out-of-range access raises rather than wrapping around."""
        with pytest.raises(IndexError):
            factory.bar_at(factory.bar_count)


class TestFeatureAccess:
    """A missing or null feature must be loud, never silently falsy."""

    def test_unknown_feature_raises_rather_than_returning_none(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """A typo'd feature name that read as ``None`` would produce a strategy that
        never trades and looks like a strategy with no edge."""
        context = factory.build(4, flat_view)
        with pytest.raises(UnknownFeatureError, match="outside its declared feature set"):
            _ = context.features["atr_14"]

    def test_the_error_lists_the_available_features(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """The usual cause is a typo and the usual next question is 'what is valid'."""
        context = factory.build(4, flat_view)
        with pytest.raises(UnknownFeatureError) as raised:
            _ = context.features["bar_marke"]
        assert "bar_marker" in raised.value.context["available"]

    def test_require_rejects_a_null_feature(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
        flat_view: PortfolioView,
    ) -> None:
        """``require`` is for decisions a null would make meaningless."""
        frame = build_feature_frame(
            rows=5, feature_values={"sparse": [None, None, 1.0, 2.0, 3.0]}
        )
        context = BarContextFactory(frame, instrument).build(1, flat_view)
        assert context.features.has("sparse") is False
        with pytest.raises(UnknownFeatureError, match="null at this bar"):
            context.features.require("sparse")

    def test_has_reports_presence_and_nullity_together(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """Present-and-non-null is the only useful notion of availability."""
        context = factory.build(3, flat_view)
        assert context.features.has("bar_marker") is True
        assert context.features.has("nonexistent") is False


class TestStructuralIsolation:
    """The context must expose no route to the feature frame at all.

    Value-level tests confirm the shift is currently applied correctly. This confirms the
    stronger property: even a strategy author actively trying to reach a future value has
    no expression available to them.
    """

    def test_the_context_exposes_no_frame_or_index_into_one(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """The field set is closed and contains no frame reference."""
        context = factory.build(5, flat_view)
        assert {field.name for field in fields(context)} == {
            "bar",
            "features",
            "instrument",
            "portfolio",
            "session",
            "bar_index",
        }
        for field in fields(context):
            assert not isinstance(getattr(context, field.name), pl.DataFrame)

    def test_the_context_is_immutable(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """A strategy cannot rewrite its own inputs mid-decision."""
        context = factory.build(5, flat_view)
        with pytest.raises((AttributeError, TypeError)):
            context.bar_index = 99  # type: ignore[misc]

    def test_the_feature_row_holds_only_lagged_values(
        self, factory: BarContextFactory, flat_view: PortfolioView
    ) -> None:
        """The row is a snapshot of one bar, not a window over several."""
        context = factory.build(5, flat_view)
        assert set(context.features.values) == {"bar_marker"}
        assert context.features.values["bar_marker"] == 4.0


class TestFactoryGuards:
    """Frames that would corrupt the boundary are refused at construction."""

    def test_a_preshifted_frame_is_refused(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
    ) -> None:
        """The double-shift guard (ARCH-004 F-1).

        A frame shifted in the pipeline and shifted again here would serve T-2 features.
        Nothing downstream would fail: the backtest would run clean on stale data, which
        is the worst available outcome.
        """
        frame = build_feature_frame(
            rows=10, shift_applied=1, lag_policy=LagPolicy.SHIFT_IN_PIPELINE
        )
        with pytest.raises(ConfigurationError, match="already carries an availability shift"):
            BarContextFactory(frame, instrument)

    def test_an_unaudited_frame_is_refused(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
    ) -> None:
        """Directive 1 is not satisfied by a frame whose causality was never tested."""
        from dataclasses import replace

        frame = replace(build_feature_frame(rows=10), lag_audit=None)
        with pytest.raises(ConfigurationError, match="not audited"):
            BarContextFactory(frame, instrument)

    def test_a_frame_that_failed_its_audit_is_refused(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
    ) -> None:
        """A failing audit must block the run, not annotate it."""
        frame = build_feature_frame(
            rows=10, audit=failing_audit(10, ("bar_marker",))
        )
        with pytest.raises(ConfigurationError, match="failed its prefix-invariance audit"):
            BarContextFactory(frame, instrument)

    def test_audit_enforcement_can_be_waived_only_explicitly(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
    ) -> None:
        """The waiver exists for unit tests of unrelated behaviour, never for a run."""
        from dataclasses import replace

        frame = replace(build_feature_frame(rows=10), lag_audit=None)
        assert BarContextFactory(frame, instrument, require_audit=False).bar_count == 10

    def test_a_frame_missing_bar_columns_is_refused(
        self, instrument: InstrumentSpec, build_feature_frame: Callable[..., FeatureFrame]
    ) -> None:
        """A frame without prices cannot produce an execution bar."""
        from dataclasses import replace

        original = build_feature_frame(rows=10)
        stripped = replace(original, frame=original.frame.drop("high"))
        with pytest.raises(SchemaContractError, match="missing bar columns"):
            BarContextFactory(stripped, instrument)

    def test_a_null_price_at_the_boundary_is_refused(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
    ) -> None:
        """Warmup trimming should have removed these; if one survives, stop."""
        original = build_feature_frame(rows=5)
        holed = original.frame.with_columns(
            pl.when(pl.int_range(pl.len()) == 2)
            .then(None)
            .otherwise(pl.col("close"))
            .alias("close")
        )
        from dataclasses import replace

        factory = BarContextFactory(replace(original, frame=holed), instrument)
        with pytest.raises(SchemaContractError, match="null price"):
            factory.bar_at(2)


class TestSessionPositioning:
    """Session flags must follow the declared session column, not the frame boundaries."""

    def test_session_open_and_close_are_detected(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
    ) -> None:
        """Two sessions of three bars each yield two opens and two closes."""
        frame = build_feature_frame(rows=6, session_index=[0, 0, 0, 1, 1, 1])
        factory = BarContextFactory(frame, instrument, session_column="session_index")
        opens = [factory.session_at(index).is_session_open for index in range(6)]
        closes = [factory.session_at(index).is_session_close for index in range(6)]
        assert opens == [True, False, False, True, False, False]
        assert closes == [False, False, True, False, False, True]

    def test_bar_index_within_session_resets_at_each_boundary(
        self,
        build_feature_frame: Callable[..., FeatureFrame],
        instrument: InstrumentSpec,
    ) -> None:
        """Session-relative position is what session-aware strategies reason about."""
        frame = build_feature_frame(rows=6, session_index=[0, 0, 0, 1, 1, 1])
        factory = BarContextFactory(frame, instrument, session_column="session_index")
        assert [factory.session_at(i).bar_index_in_session for i in range(6)] == [
            0, 1, 2, 0, 1, 2,
        ]

    def test_absent_session_column_treats_the_run_as_one_session(
        self, factory: BarContextFactory
    ) -> None:
        """A single continuous session is the honest default, not a fabricated split."""
        assert factory.session_at(0).is_session_open is True
        assert factory.session_at(5).is_session_open is False
        assert factory.session_at(factory.bar_count - 1).is_session_close is True
