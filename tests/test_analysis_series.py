"""U8 — tests for the declarative plot inventory and the shared series extractor.

The inventory in ``simoscal.analysis.series`` is the single source of truth for
which channel belongs on which evidence panel. Two very different renderers read
it — matplotlib on the desktop, a Compose canvas on Android — so the properties
worth pinning are the ones that keep those two describing the same log the same
way: the ids, the sources being resolvable at all, and the payload's
``drawn`` flag meaning exactly what the PNG writer means by it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from simoscal.analysis import (
    PLOT_SPECS,
    CheckContext,
    Role,
    Tone,
    default_battery,
    detect_pulls,
    load_logset,
    plot_payload,
    run_battery,
    series_segments,
)
from simoscal.analysis.log import CHANNEL_SPECS
from simoscal.analysis.series import (
    DERIVED,
    SPEC_BY_ID,
    SeriesSpec,
    contiguous_runs,
    min_knock_arrays,
    panel_available,
    pull_ordinals,
)

from tests.faultinject import PullSpec, build_folder


def _context(folder) -> CheckContext:
    logset = load_logset(folder)
    return CheckContext(logset=logset, pulls=detect_pulls(logset), cal=None)


@pytest.fixture
def ctx(tmp_path):
    """A two-pull synthetic log set with the opt-in channels the plots want."""
    build_folder(
        tmp_path,
        [PullSpec(put_overshoot=25.0, knock={3: -3.0}), PullSpec()],
        wastegate=True,
        ign_table=True,
    )
    return _context(tmp_path)


@pytest.fixture
def bare_ctx(tmp_path):
    """The same log without the opt-in wastegate channels — so a plot has no data."""
    build_folder(tmp_path, [PullSpec()], wastegate=False, ign_table=False)
    return _context(tmp_path)


# --------------------------------------------------------------------------- #
# The inventory itself
# --------------------------------------------------------------------------- #
def test_plot_ids_are_unique_and_alphabetical():
    ids = [spec.id for spec in PLOT_SPECS]
    assert len(ids) == len(set(ids))
    # The app presents plots in this order and so does the inventory; one order
    # everywhere is one less thing for the two halves to disagree about.
    assert ids == sorted(ids)


def test_every_plot_id_is_a_check_id_or_the_known_standalone():
    check_ids = {check.id for check in default_battery()}
    for spec in PLOT_SPECS:
        # A plot sharing a check's id is how `_attach_plot_refs` wires the PNG
        # onto a fired finding; `ignition` deliberately has no check.
        assert spec.id in check_ids or spec.id == "ignition", spec.id


def test_every_series_source_resolves():
    """A source is a canonical channel id or a DERIVED key — never a typo."""
    known_channels = {spec.id for spec in CHANNEL_SPECS}
    for plot in PLOT_SPECS:
        for panel in plot.panels:
            for series in panel.series:
                assert series.source in known_channels or series.source in DERIVED, (
                    f"{plot.id}/{panel.title}: unknown source {series.source!r}"
                )


def test_every_role_and_tone_is_known():
    roles = {Role.PRIMARY, Role.REFERENCE, Role.SECONDARY, Role.TRANSIENT}
    tones = {Tone.ZERO, Tone.WATCH, Tone.HIGH}
    for plot in PLOT_SPECS:
        for panel in plot.panels:
            assert {s.role for s in panel.series} <= roles
            assert {s.mask for s in panel.series} <= {"loaded", "settled", "none"}
            assert {t.tone for t in panel.thresholds} <= tones


def test_every_plot_carries_a_description_and_a_tip():
    """Both are user-facing copy the app renders above the plot; neither may be blank."""
    for plot in PLOT_SPECS:
        assert plot.title.strip()
        assert len(plot.description.strip()) > 30, plot.id
        assert len(plot.tip.strip()) > 30, plot.id
        assert plot.panels


def test_every_panel_has_a_primary_series():
    """A panel of nothing but setpoints would draw what was asked for and never
    what happened — always a spec bug, so it is asserted rather than handled."""
    for plot in PLOT_SPECS:
        for panel in plot.panels:
            assert any(s.role == Role.PRIMARY for s in panel.series), (
                f"{plot.id}/{panel.title} has no measured series"
            )


# --------------------------------------------------------------------------- #
# The shared extractor
# --------------------------------------------------------------------------- #
def test_contiguous_runs_splits_mask_holes():
    mask = np.array([False, True, True, False, True, False, False, True])
    assert contiguous_runs(mask) == [(1, 2), (4, 4), (7, 7)]
    assert contiguous_runs(np.array([True])) == [(0, 0)]
    assert contiguous_runs(np.array([False, False])) == []


def test_min_knock_arrays_takes_the_most_retarded():
    a = np.array([0.0, -1.0, np.nan])
    b = np.array([-2.0, 0.0, -3.0])
    assert list(min_knock_arrays([a, b])) == [-2.0, -1.0, -3.0]
    assert min_knock_arrays([None, None]) is None


def test_series_segments_are_x_sorted_and_finite(ctx):
    data = series_segments(ctx, SeriesSpec("put", Role.PRIMARY))
    assert data, "the synthetic log should produce PUT samples"
    for entry in data:
        for segment in entry.segments:
            assert segment.x.size == segment.y.size
            assert np.all(np.isfinite(segment.x)) and np.all(np.isfinite(segment.y))
            assert np.all(np.diff(segment.x) >= 0), "a line must sweep one way in x"


def test_series_segments_absent_channel_yields_nothing(ctx):
    assert series_segments(ctx, SeriesSpec("no_such_channel", Role.PRIMARY)) == []


def test_transient_role_selects_only_unsettled_samples(ctx):
    """The scatter role and the settled line must not draw the same sample twice."""
    settled = series_segments(ctx, SeriesSpec("lambda_error", Role.PRIMARY, mask="settled"))
    transient = series_segments(ctx, SeriesSpec("lambda_error", Role.TRANSIENT))

    def points(entries):
        return {
            (round(float(x), 6), round(float(y), 6))
            for entry in entries
            for segment in entry.segments
            for x, y in zip(segment.x, segment.y)
        }

    assert not (points(settled) & points(transient))


def test_pull_ordinals_are_positional(ctx):
    ordinals = pull_ordinals(ctx)
    assert sorted(ordinals.values()) == list(range(len(ctx.pulls)))
    assert ordinals[ctx.pulls[0].index] == 0


def test_panel_available_gates_on_required_channels(ctx):
    from simoscal.analysis.series import PanelSpec

    gauge_panel = SPEC_BY_ID["boost"].panels[0]
    assert gauge_panel.requires == ("put", "ambient_press", "put_sp")
    # This log carries all three, so the gauge-boost panel is drawable.
    assert panel_available(ctx, gauge_panel)

    # Take one requirement away and it is not. Without ambient pressure there is
    # no baseline to zero gauge boost against, and this library does not guess one.
    impossible = PanelSpec(
        title="needs a channel nothing logs",
        y_label="",
        series=gauge_panel.series,
        requires=("put", "no_such_channel"),
    )
    assert not panel_available(ctx, impossible)


# --------------------------------------------------------------------------- #
# The JSON payload the bridge sends
# --------------------------------------------------------------------------- #
def test_plot_payload_lists_every_plot_even_when_undrawn(bare_ctx):
    payload = plot_payload(bare_ctx)
    assert [p["id"] for p in payload] == [s.id for s in PLOT_SPECS]
    # Listing an undrawn plot is deliberate: it is how the app tells "this was
    # fine" from "this was never logged", the same reason SKIPPED is explicit.
    by_id = {p["id"]: p for p in payload}
    assert not by_id["wastegate"]["drawn"], "no wastegate channels in this log"
    assert by_id["boost"]["drawn"]


def test_plot_payload_drawn_flag_follows_the_data(ctx):
    payload = {p["id"]: p for p in plot_payload(ctx)}
    assert payload["boost"]["drawn"]
    for plot in payload.values():
        for panel in plot["panels"]:
            assert panel["drawn"] == any(s["segments"] for s in panel["series"])


def test_plot_payload_thresholds_survive_an_undrawn_panel(bare_ctx):
    """A panel with no data still declares its thresholds, but is not 'drawn'."""
    wastegate = {p["id"]: p for p in plot_payload(bare_ctx)}["wastegate"]
    panel = wastegate["panels"][1]                 # the correction-terms panel
    assert panel["series"] == []
    assert panel["thresholds"], "threshold lines are part of the spec, not the data"
    assert not panel["drawn"], "threshold lines alone never make a panel drawable"


def test_plot_payload_is_json_serializable_and_carries_ordinals(ctx):
    payload = plot_payload(ctx)
    text = json.dumps(payload)          # raises on any numpy scalar that leaked
    assert len(text) > 0
    ordinals = pull_ordinals(ctx)
    for plot in payload:
        for panel in plot["panels"]:
            for series in panel["series"]:
                assert series["ordinal"] == ordinals[series["pull"]]
                assert set(series) == {
                    "source", "role", "label", "pull", "ordinal", "segments",
                }


def test_plot_payload_matches_the_png_inventory(ctx, tmp_path):
    """Whatever the payload calls drawn is what the desktop renderer writes a file for.

    This is the property the whole refactor exists for: if the two ever disagree,
    a person reading the app and a person reading the report are looking at
    different plot sets for the same log.
    """
    from simoscal.analysis.evidence import _PLOTTERS

    payload = {p["id"]: p["drawn"] for p in plot_payload(ctx)}
    for plot_id, plotter in _PLOTTERS.items():
        wrote = plotter(ctx, tmp_path / f"{plot_id}.png")
        assert wrote == payload[plot_id], plot_id


def test_battery_still_runs_alongside_the_payload(ctx):
    """The payload is additive — it must not perturb the findings it accompanies."""
    before = run_battery(default_battery(), ctx)
    plot_payload(ctx)
    after = run_battery(default_battery(), ctx)
    assert [f.message for f in before.findings] == [f.message for f in after.findings]
