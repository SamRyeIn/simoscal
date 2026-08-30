"""``render_report_html`` — the reviewer-facing flash-gate page.

``report.md`` is the archival record; this is the page a human actually reads at
the moment they decide whether to flash. It renders from the same
:class:`~simoscal.tune.journal.Journal` and :class:`BuildResult` as the Markdown
report, so the two cannot disagree — the layout differs, the facts do not.

Design decisions that matter for correctness rather than looks:

* The verdict banner is driven by :attr:`BuildResult.ok` and the checksum state,
  never hard-coded. A failed build renders a red DO-NOT-FLASH banner listing the
  problems; a passed build renders the "reviewable, not approved" caution banner.
* "Needs your eyes" surfaces every entry a guard blocked, skipped, or declined,
  plus any warning and any recipe-coherence finding — the things a reviewer must
  not miss are lifted out of the full journal, not left to be scrolled past.
* Comparison plots are referenced by **relative path** (``compare/<file>.png``),
  because the page is written into the run folder next to that ``compare/``
  directory and opened locally. Headline edits (anything the author wrote by
  hand — not the bulk basics-SOP build-out) get their plot inline; the SOP
  build-out plots are collapsed into a linked list so the page stays light.

Never flashes; renders text. Nothing in this package flashes.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Sequence, Union

import numpy as np

from .journal import (
    KIND_SOP,
    VERDICT_APPLIED,
    VERDICT_BLOCKED,
    VERDICT_GUARDED_SKIP,
    VERDICT_SKIPPED,
    VERDICT_SUPERSEDED,
    VERDICT_UNCHANGED,
    EditEntry,
    Journal,
)

if TYPE_CHECKING:  # avoid an import cycle: pipeline imports this module
    from .pipeline import BuildResult
    from .project import Tune

__all__ = ["render_report_html"]

_TableKey = tuple[str, Union[str, int]]

# Verdicts a reviewer must look at before flashing, richest-signal first.
_ATTENTION_VERDICTS = (VERDICT_BLOCKED, VERDICT_GUARDED_SKIP, VERDICT_SKIPPED)

# Which verdict maps to which status colour in the journal table.
_VERDICT_CLASS = {
    VERDICT_APPLIED: "applied",
    VERDICT_UNCHANGED: "unchanged",
    VERDICT_GUARDED_SKIP: "guarded",
    VERDICT_BLOCKED: "blocked",
    VERDICT_SKIPPED: "skipped",
    VERDICT_SUPERSEDED: "applied",
}


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def render_report_html(
    tune: "Tune", result: "BuildResult", *, title: str = "", summary: str = ""
) -> str:
    """Render the build's journal and gate verdicts as a standalone HTML page."""
    journal = tune.journal
    heading = title or result.revision
    ok = result.ok

    parts: list[str] = []
    parts.append(f"<title>{_esc(result.revision)} flash review — {_esc(heading)}</title>")
    parts.append(_STYLE)
    parts.append('<div class="wrap">')
    parts.append(_topbar(result))
    parts.append(_verdict(result, heading, summary))
    parts.append(_gate_chips(result, journal))
    parts.append(_attention_section(tune, journal))
    parts.append(_changed_tables_section(result, journal))
    parts.append(_vs_stock_section(result, journal))
    parts.append(_journal_section(journal))
    parts.append(_artifacts_footer(result))
    parts.append("</div>")
    parts.append(_SCRIPT)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# sections
# --------------------------------------------------------------------------- #
def _topbar(result: "BuildResult") -> str:
    return (
        '<div class="topbar">'
        f'<span class="rev">{_esc(result.revision)}</span>'
        f'<span class="run mono">{_esc(result.out_dir.name)}</span>'
        f'<span class="meta mono">{_esc(result.bin_path.name)}</span>'
        "</div>"
    )


def _verdict(result: "BuildResult", heading: str, summary: str) -> str:
    ok = result.ok
    kind = "pass" if ok else "critical"
    if ok:
        eyebrow = "Flash gate · awaiting your review"
        title = "Reviewable, not approved — every automated gate passed. You decide."
        body = (
            "This bin passed checksum, readback, and byte-audit verification. "
            "That makes it reviewable, not approved: read the changes below before "
            "flashing."
        )
    else:
        eyebrow = "Flash gate · verification failed"
        title = "DO NOT FLASH — one or more gates failed."
        body = (
            "This bin did not pass every verification gate. The failures are listed "
            "below; the bin is not flash-ready."
        )

    chk = result.checksum_state
    chk_clean = result.checksums_clean
    badges = [
        _badge("caution" if ok else "critical", "REVIEW REQUIRED" if ok else "DO NOT FLASH"),
        _badge("pass" if chk_clean else "critical", f"CHECKSUMS {chk}"),
    ]

    problems_html = ""
    if not ok:
        items = "".join(f"<li>{_esc(p)}</li>" for p in result.problems)
        problems_html = f'<ul class="problems">{items}</ul>'

    summary_html = f'<p class="summary">{_esc(summary).strip()}</p>' if summary else ""

    return (
        f'<div class="verdict {kind}">'
        f'<span class="eyebrow">{_esc(eyebrow)}</span>'
        f"<h1>{_esc(title)}</h1>"
        f"<p>{_esc(body)}</p>"
        f"{problems_html}"
        f"{summary_html}"
        '<div class="flashline">'
        f'{"".join(badges)}'
        '<span class="noflash">&#9888; This tool never flashes — you flash with '
        "SimosTools once you approve.</span>"
        "</div>"
        "</div>"
    )


def _badge(cls: str, text: str) -> str:
    return f'<span class="badge {cls}"><span class="dot"></span>{_esc(text)}</span>'


def _gate_chips(result: "BuildResult", journal: Journal) -> str:
    chips: list[str] = []

    # Checksums.
    names = ", ".join(r.name for r in result.checksums) or "none verifiable"
    chips.append(_gate(
        "Checksums", result.checksum_state, names,
        ok=result.checksums_clean,
    ))

    # Final-bin readback.
    if result.readback_failures:
        chips.append(_gate(
            "Final-bin readback", "FAILED",
            f"{len(result.readback_failures)} table(s) mismatched", ok=False,
        ))
    else:
        n = len(journal.tables_touched())
        chips.append(_gate(
            "Final-bin readback", "PASS",
            f"{n} table(s) re-read, matched", ok=True,
        ))

    # Raw-diff audit.
    if result.diff is None:
        chips.append(_gate(
            "Raw-diff audit", "NOT RUN",
            "no reference bin declared", ok=None,
        ))
    else:
        chips.append(_gate(
            "Raw-diff audit vs prev",
            "CLEAN" if result.diff.clean else "UNEXPLAINED",
            _esc(result.diff.summary()), ok=result.diff.clean,
        ))

    return f'<div class="gates">{"".join(chips)}</div>'


def _gate(name: str, value: str, sub: str, *, ok: Optional[bool]) -> str:
    if ok is True:
        mark, cls = "&#10003;", "pass"
    elif ok is False:
        mark, cls = "&#10007;", "fail"
    else:
        mark, cls = "&middot;", "info"
    return (
        f'<div class="gate {cls}">'
        f'<div class="g-top"><span class="g-name">{_esc(name)}</span>'
        f'<span class="g-mark">{mark}</span></div>'
        f'<span class="g-val">{_esc(value)}</span>'
        f'<span class="g-sub mono">{sub}</span>'
        "</div>"
    )


def _attention_section(tune: "Tune", journal: Journal) -> str:
    """Everything a reviewer must not miss, lifted out of the full journal."""
    callouts: list[str] = []
    superseded = journal.superseded()

    for i, entry in enumerate(journal):
        if i in superseded:
            # Not held back — a later write in this revision stands in its place.
            continue
        if entry.verdict in _ATTENTION_VERDICTS:
            cls = "caution" if entry.verdict == VERDICT_BLOCKED else "info"
            callouts.append(_callout(cls, entry.verdict.upper(), entry.label, _detail(entry)))
        elif entry.warning:
            callouts.append(_callout("caution", "WARNING", entry.label, _esc(entry.warning)))

    # Recipe-coherence findings (a boost change shipped without matching fuelling,
    # etc.) — these gate the whole build, not one table.
    recipe = getattr(tune, "recipe_report", None)
    if recipe is not None:
        for finding in recipe.coherence():
            sev = getattr(finding, "severity", "")
            cls = "critical" if sev == "DO NOT FLASH" else "caution"
            callouts.append(_callout(
                cls, sev or "COHERENCE", "Recipe coherence",
                _esc(getattr(finding, "message", "")),
            ))

    head = _sec_head("Needs your eyes", f"{len(callouts)} item(s) held back or flagged")
    if not callouts:
        body = (
            '<div class="callout pass"><span class="c-tag">Clear</span>'
            '<span class="c-title">Nothing held back</span>'
            '<span class="c-body">Every declared edit applied, and no guard, skip, '
            "or warning was recorded.</span></div>"
        )
    else:
        body = "".join(callouts)
    return f"<section>{head}{body}</section>"


def _callout(cls: str, tag: str, title: str, body: str) -> str:
    return (
        f'<div class="callout {cls}">'
        f'<span class="c-tag">{_esc(tag)}</span>'
        f'<span class="c-title mono">{_esc(title)}</span>'
        f'<span class="c-body">{body}</span>'
        "</div>"
    )


def _changed_tables_section(result: "BuildResult", journal: Journal) -> str:
    """What changed this flash gets inline cards; the rest is collapsed.

    A revision re-declares the whole calibration, so "an edit touched this table"
    is not "this table differs from the last flash". When a reference bin was
    audited, its changed-offset set is the truth of what moved *this* revision —
    those tables become the cards a reviewer studies, and the unchanged remainder
    (carried forward from the previous flash) collapses out of the way.

    With no reference (a first revision), there is no previous flash to diff
    against, so the split falls back to hand-authored edits (inline) versus the
    bulk basics-SOP build-out (linked plots).
    """
    entries_by_table = _entries_by_table(journal)
    delta = result.diff.changed_offsets if result.diff is not None else None

    if delta is None:
        return _changed_tables_no_reference(result, entries_by_table)

    ref_name = Path(result.diff.reference).name
    changed_cards: list[str] = []
    carried: list[EditEntry] = []
    for key, entries in entries_by_table.items():
        moved = frozenset().union(*(e.offsets | e.declared for e in entries))
        primary = _primary_entry(entries)
        if moved & delta:
            changed_cards.append(_table_card(result, primary, result.plots_by_table.get(key, ())))
        else:
            carried.append(primary)

    out = [_sec_head("Changed this flash", f"different from {_esc(ref_name)} — the delta to review")]
    if changed_cards:
        out.append(f'<div class="cards">{"".join(changed_cards)}</div>')
    else:
        out.append('<p class="empty">No table values changed versus the previous revision.</p>')

    if carried:
        rows = "".join(
            "<tr>"
            f'<td class="id">{_esc(e.label)}</td>'
            f'<td class="scope mono">{_esc(e.scope_text())}</td>'
            f'<td class="ba mono">{_esc(e.after_text())}</td>'
            "</tr>"
            for e in carried
        )
        out.append(
            '<details class="linkbox"><summary>'
            f'<span class="chev">&#9656;</span>Carried unchanged from {_esc(ref_name)}'
            f'<span class="count mono">{len(carried)} table(s) · identical to last flash</span>'
            "</summary>"
            '<div class="table-scroll"><table>'
            "<thead><tr><th>Table</th><th>Change</th><th>Current value</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></div></details>"
        )
    return f"<section>{''.join(out)}</section>"


def _vs_stock_section(result: "BuildResult", journal: Journal) -> str:
    """Full-history comparison: this build against the untouched stock bin.

    Sourced from :attr:`BuildResult.plots_by_table_vs_stock`, which spans every
    table the journal has ever touched — not just this revision's delta — so
    it is normally the larger of the two comparison sections. Skips entirely
    when the build carried no stock snapshot or drew no such plots.
    """
    plots_by_table = result.plots_by_table_vs_stock
    if not plots_by_table:
        return ""
    entries_by_table = _entries_by_table(journal)
    cards = [
        _table_card(result, _primary_entry(entries_by_table[key]), plots)
        for key, plots in plots_by_table.items()
        if plots and key in entries_by_table
    ]
    out = [_sec_head(
        "Changed vs stock",
        "every table that differs from the untouched recovery bin",
    )]
    if cards:
        out.append(f'<div class="cards">{"".join(cards)}</div>')
    else:
        out.append('<p class="empty">No comparison plots vs stock to review.</p>')
    return f"<section>{''.join(out)}</section>"


def _changed_tables_no_reference(
    result: "BuildResult", entries_by_table: dict[_TableKey, list[EditEntry]]
) -> str:
    """First-revision fallback: hand-authored edits inline, SOP build-out linked."""
    headline: list[str] = []
    sop_links: list[str] = []
    for key, entries in entries_by_table.items():
        plots = result.plots_by_table.get(key, ())
        if any(e.kind != KIND_SOP for e in entries):
            headline.append(_table_card(result, _primary_entry(entries), plots))
        else:
            for p in plots:
                rel = _rel(result, p)
                sop_links.append(f'<li><a href="{_esc(rel)}">{_esc(Path(rel).name)}</a></li>')

    out = [_sec_head("Changed tables", "no reference bin — showing every hand-authored edit")]
    if headline:
        out.append(f'<div class="cards">{"".join(headline)}</div>')
    else:
        out.append('<p class="empty">No hand-authored table edits in this revision.</p>')
    if sop_links:
        out.append(
            '<details class="linkbox"><summary>'
            '<span class="chev">&#9656;</span>Basics-SOP build-out plots'
            f'<span class="count mono">{len(sop_links)} plot(s) · click to open</span>'
            f'</summary><ul class="links">{"".join(sop_links)}</ul></details>'
        )
    return f"<section>{''.join(out)}</section>"


def _table_card(result: "BuildResult", entry: EditEntry, plots: tuple[Path, ...]) -> str:
    before, after = entry.before_text(), entry.after_text()
    ba = ""
    if before or after:
        ba = (
            '<div class="ba mono">'
            f'<span class="b">{_esc(before) or "—"}</span>'
            '<span class="arr">&rarr;</span>'
            f'<span class="a">{_esc(after) or "—"}</span>'
            "</div>"
        )
    imgs = ""
    if plots:
        imgs = "".join(
            f'<img src="{_esc(_rel(result, p))}" alt="{_esc(entry.label)} comparison" loading="lazy">'
            for p in plots
        )
        imgs = f'<div class="imgs">{imgs}</div>'
    else:
        imgs = (
            '<div class="noimg">No comparison plot — the table\'s own axis was '
            "re-breakpointed, so a before/after overlay would compare different "
            "grids. See the values above and the journal detail.</div>"
        )
    pill = _VERDICT_CLASS.get(entry.verdict, "info")
    return (
        '<div class="card">'
        '<div class="card-head">'
        f'<span class="card-id mono">{_esc(entry.label)}</span>'
        f'<span class="vpill {pill}">{_esc(entry.verdict)}</span>'
        "</div>"
        f'<div class="card-scope mono">{_esc(entry.scope_text())}</div>'
        f"{ba}"
        f'<p class="card-why">{_esc(entry.intent)}</p>'
        f"{_sparkline(entry)}"
        f"{imgs}"
        "</div>"
    )


def _journal_section(journal: Journal) -> str:
    counts = journal.summary_counts()
    counts_html = " · ".join(f"<b>{v}</b> {_esc(k)}" for k, v in counts.items())
    superseded = journal.superseded()
    rows = "".join(
        _journal_row(e, superseded.get(i)) for i, e in enumerate(journal)
    )
    if not rows:
        rows = '<tr><td colspan="6" class="empty">No edits were journaled.</td></tr>'
    head = _sec_head("Edit journal", "every recorded edit")
    return (
        f"<section>{head}"
        f'<details class="journal-wrap"><summary>'
        f'<span class="chev">&#9656;</span>Full journal'
        f'<span class="count mono">{counts_html}</span></summary>'
        '<div class="table-scroll"><table>'
        "<thead><tr><th>Table</th><th>Change</th><th>Verdict</th>"
        "<th>Before</th><th></th><th>After</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div></details></section>"
    )


def _journal_row(
    entry: EditEntry, superseded_by: Optional[Sequence[EditEntry]] = None
) -> str:
    verdict = VERDICT_SUPERSEDED if superseded_by else entry.verdict
    pill = _VERDICT_CLASS.get(verdict, "info")
    title = ""
    if superseded_by:
        names = ", ".join(dict.fromkeys(w.name for w in superseded_by))
        title = (
            f' title="The base recipe deferred this; this revision writes it '
            f'below ({_esc(names)})."'
        )
    return (
        "<tr>"
        f'<td class="id">{_esc(entry.label)}</td>'
        f'<td class="scope mono">{_esc(entry.scope_text())}</td>'
        f'<td><span class="vpill {pill}"{title}>{_esc(verdict)}</span></td>'
        f'<td class="ba mono">{_esc(entry.before_text())}</td>'
        '<td class="arrow">&rarr;</td>'
        f'<td class="ba mono">{_esc(entry.after_text())}</td>'
        "</tr>"
    )


def _artifacts_footer(result: "BuildResult") -> str:
    return (
        '<div class="footnote">'
        "Reviewer-facing flash gate, generated by "
        "<code>simoscal.tune.render_report_html</code> from the build journal — "
        "the same source as <code>report.md</code>. Artifacts in this run folder: "
        f"<code>{_esc(result.bin_path.name)}</code> (the bin), "
        f'<a href="{_esc(result.report_path.name)}">report.md</a>, the '
        "<code>compare/</code> plots (vs previous revision), and the "
        "<code>compare_vs_stock/</code> plots (vs the untouched recovery bin). "
        "Every revision is a starting point, not a "
        "finished calibration: only logs validate it. Flash (human step) → log → "
        "review → iterate."
        "</div>"
    )


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _sec_head(title: str, eyebrow: str) -> str:
    return (
        '<div class="sec-head">'
        f"<h2>{_esc(title)}</h2><span class=\"rule\"></span>"
        f'<span class="eyebrow">{_esc(eyebrow)}</span>'
        "</div>"
    )


def _detail(entry: EditEntry) -> str:
    text = entry.intent
    if entry.detail:
        text = f"{text} — {entry.detail}" if text else entry.detail
    if entry.warning:
        text = f"{text} ⚠ {entry.warning}".strip()
    return _esc(text)


def _entries_by_table(journal: Journal) -> dict[_TableKey, list[EditEntry]]:
    grouped: dict[_TableKey, list[EditEntry]] = {}
    for entry in journal.touching():
        grouped.setdefault((entry.space, entry.key), []).append(entry)
    return grouped


def _primary_entry(entries: list[EditEntry]) -> EditEntry:
    """The entry that best describes a table: the last hand-authored one, else last.

    One table can be journaled twice — the basics SOP by symbol and a domain call
    by logical name. The domain call is the author's intent, so it wins the card
    header; falling back to the last entry keeps a pure-SOP table describable.
    """
    non_sop = [e for e in entries if e.kind != KIND_SOP]
    return (non_sop or entries)[-1]


def _rel(result: "BuildResult", path: Path) -> str:
    """A path relative to the run folder, for an ``<img>``/``<a>`` in report.html."""
    try:
        return str(path.relative_to(result.out_dir))
    except ValueError:
        return path.name


def _curve(after: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """Reduce a table's ``after`` values to a single 1-D curve, or ``None``.

    A boost/lambda setpoint grid is tiled — the same curve repeated down every
    row (or across every column) of a 2-D grid — so it collapses to one line
    without loss. A genuinely 2-D table has no single curve to draw, so it
    returns ``None`` and the card falls back to its plot/text rather than
    inventing a representative row.
    """
    if after is None:
        return None
    arr = np.asarray(after, dtype=np.float64)
    if arr.ndim == 1:
        return arr if arr.size >= 2 else None
    if arr.ndim == 2 and arr.size >= 2:
        if np.allclose(arr, arr[0], rtol=0, atol=1e-9):          # tiled by row
            return arr[0]
        if np.allclose(arr, arr[:, :1], rtol=0, atol=1e-9):      # tiled by column
            return arr[:, 0]
    return None


def _sparkline(entry: EditEntry) -> str:
    """An inline SVG sparkline of the table's after-curve, with data markers.

    Drawn from the journal's recorded values — no matplotlib, no file — so it is
    available even for a re-breakpointed table that has no before/after PNG. The
    y-scale spans the curve's own min..max (a boost target never approaches
    zero, so a zero-baseline would flatten every meaningful wiggle).
    """
    series = _curve(entry.after)
    if series is None:
        return ""
    n = series.size
    vmin, vmax = float(series.min()), float(series.max())
    span = vmax - vmin

    W, H = 320.0, 84.0
    pl, pr, pt, pb = 8.0, 8.0, 12.0, 12.0
    unit = _esc(entry.units) if entry.units else ""

    def x(i: int) -> float:
        return pl + (i / (n - 1)) * (W - pl - pr)

    def y(v: float) -> float:
        if span == 0:
            return H / 2.0
        return pt + (1.0 - (v - vmin) / span) * (H - pt - pb)

    pts = [(x(i), y(float(v))) for i, v in enumerate(series)]
    poly = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    markers = "".join(
        f'<circle class="mk" cx="{px:.1f}" cy="{py:.1f}" r="2.6">'
        f"<title>{float(series[i]):.6g}{(' ' + unit) if unit else ''}</title></circle>"
        for i, (px, py) in enumerate(pts)
    )
    # min/max guide rails with their values, so the shape has a scale.
    guides = ""
    if span > 0:
        guides = (
            f'<line class="guide" x1="{pl:.1f}" y1="{y(vmax):.1f}" x2="{W - pr:.1f}" y2="{y(vmax):.1f}"></line>'
            f'<line class="guide" x1="{pl:.1f}" y1="{y(vmin):.1f}" x2="{W - pr:.1f}" y2="{y(vmin):.1f}"></line>'
            f'<text class="gtext" x="{pl:.1f}" y="{y(vmax) - 3:.1f}">{vmax:.6g}</text>'
            f'<text class="gtext" x="{pl:.1f}" y="{y(vmin) + 9:.1f}">{vmin:.6g}</text>'
        )
    unit_lbl = f" · {unit}" if unit else ""
    return (
        '<div class="spark">'
        f'<span class="spark-lbl">Curve shape — after · {n} pts{unit_lbl}</span>'
        f'<svg class="sparkline" viewBox="0 0 {W:.0f} {H:.0f}" '
        f'role="img" aria-label="After-curve sparkline, {vmin:.6g} to {vmax:.6g}">'
        f"{guides}"
        f'<polyline class="line" points="{poly}"></polyline>'
        f"{markers}"
        "</svg></div>"
    )


# --------------------------------------------------------------------------- #
# presentation (inlined; the page is opened from disk with no network)
# --------------------------------------------------------------------------- #
_STYLE = """<style>
  :root {
    --ground:#0d1216; --panel:#151d23; --panel-2:#1a242b; --border:#26333c;
    --border-strong:#34454f; --text-hi:#e8eef2; --text-mid:#a2b3bd; --text-dim:#6d7f8a;
    --accent:#4aa3ff; --pass:#43c06d; --pass-dim:#1c3527; --caution:#e6ac3f;
    --caution-dim:#392e14; --critical:#ef5f5f; --critical-dim:#3a1d1e;
    --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif;
    --shadow:0 1px 0 rgba(255,255,255,.02),0 8px 24px rgba(0,0,0,.35);
  }
  @media (prefers-color-scheme:light){:root{
    --ground:#eaeef0; --panel:#fff; --panel-2:#f3f6f7; --border:#d6dee2;
    --border-strong:#c0ccd2; --text-hi:#16222a; --text-mid:#46565f; --text-dim:#78888f;
    --accent:#1f74d6; --pass:#1f9d52; --pass-dim:#dcf0e3; --caution:#a9781a;
    --caution-dim:#f6ecd4; --critical:#cf3c3c; --critical-dim:#f6dede;
    --shadow:0 1px 2px rgba(16,32,40,.06),0 8px 20px rgba(16,32,40,.08);}}
  :root[data-theme="dark"]{
    --ground:#0d1216; --panel:#151d23; --panel-2:#1a242b; --border:#26333c;
    --border-strong:#34454f; --text-hi:#e8eef2; --text-mid:#a2b3bd; --text-dim:#6d7f8a;
    --accent:#4aa3ff; --pass:#43c06d; --pass-dim:#1c3527; --caution:#e6ac3f;
    --caution-dim:#392e14; --critical:#ef5f5f; --critical-dim:#3a1d1e;
    --shadow:0 1px 0 rgba(255,255,255,.02),0 8px 24px rgba(0,0,0,.35);}
  :root[data-theme="light"]{
    --ground:#eaeef0; --panel:#fff; --panel-2:#f3f6f7; --border:#d6dee2;
    --border-strong:#c0ccd2; --text-hi:#16222a; --text-mid:#46565f; --text-dim:#78888f;
    --accent:#1f74d6; --pass:#1f9d52; --pass-dim:#dcf0e3; --caution:#a9781a;
    --caution-dim:#f6ecd4; --critical:#cf3c3c; --critical-dim:#f6dede;
    --shadow:0 1px 2px rgba(16,32,40,.06),0 8px 20px rgba(16,32,40,.08);}
  *{box-sizing:border-box;}
  body{margin:0;background:var(--ground);color:var(--text-hi);font-family:var(--sans);
    line-height:1.5;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:1060px;margin:0 auto;padding:28px 22px 80px;}
  .mono{font-family:var(--mono);font-variant-numeric:tabular-nums;}
  .eyebrow{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--text-dim);font-weight:600;}
  .topbar{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 18px;padding-bottom:16px;
    margin-bottom:22px;border-bottom:1px solid var(--border);}
  .topbar .rev{font-family:var(--mono);font-weight:700;font-size:20px;letter-spacing:.02em;}
  .topbar .run{font-size:13px;color:var(--text-mid);}
  .topbar .meta{margin-left:auto;font-size:12px;color:var(--text-dim);}
  .verdict{background:var(--panel);border:1px solid var(--border);border-left:4px solid var(--caution);
    border-radius:12px;padding:20px 22px;box-shadow:var(--shadow);margin-bottom:14px;}
  .verdict.critical{border-left-color:var(--critical);}
  .verdict h1{margin:6px 0 8px;font-size:clamp(20px,3.4vw,27px);letter-spacing:-.01em;
    text-wrap:balance;line-height:1.18;}
  .verdict p{margin:0;color:var(--text-mid);max-width:68ch;font-size:14.5px;}
  .verdict .summary{margin-top:10px;color:var(--text-mid);}
  .verdict .problems{margin:12px 0 0;padding-left:20px;color:var(--critical);font-size:14px;}
  .flashline{display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-top:16px;
    padding-top:14px;border-top:1px dashed var(--border-strong);font-size:13px;color:var(--text-mid);}
  .badge{display:inline-flex;align-items:center;gap:7px;font-family:var(--mono);font-size:12px;
    font-weight:600;padding:4px 10px;border-radius:999px;border:1px solid transparent;
    letter-spacing:.02em;white-space:nowrap;}
  .badge .dot{width:7px;height:7px;border-radius:50%;}
  .badge.caution{background:var(--caution-dim);color:var(--caution);border-color:color-mix(in srgb,var(--caution) 40%,transparent);}
  .badge.caution .dot{background:var(--caution);}
  .badge.pass{background:var(--pass-dim);color:var(--pass);border-color:color-mix(in srgb,var(--pass) 40%,transparent);}
  .badge.pass .dot{background:var(--pass);}
  .badge.critical{background:var(--critical-dim);color:var(--critical);border-color:color-mix(in srgb,var(--critical) 40%,transparent);}
  .badge.critical .dot{background:var(--critical);}
  .noflash{display:inline-flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12px;color:var(--text-dim);}
  .gates{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:22px 0 30px;}
  .gate{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:13px 14px;
    display:flex;flex-direction:column;gap:5px;}
  .gate .g-top{display:flex;align-items:center;justify-content:space-between;}
  .gate .g-name{font-size:12px;color:var(--text-mid);}
  .gate .g-mark{font-weight:700;}
  .gate.pass .g-mark,.gate.pass .g-val{color:var(--pass);}
  .gate.fail .g-mark,.gate.fail .g-val{color:var(--critical);}
  .gate.info .g-mark{color:var(--accent);}
  .gate .g-val{font-family:var(--mono);font-weight:700;font-size:15px;}
  .gate .g-sub{font-size:11px;color:var(--text-dim);}
  section{margin:34px 0;}
  .sec-head{display:flex;align-items:baseline;gap:12px;margin-bottom:14px;}
  .sec-head h2{margin:0;font-size:15px;}
  .sec-head .rule{flex:1;height:1px;background:var(--border);}
  .callout{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;background:var(--panel);
    border:1px solid var(--border);border-radius:10px;padding:15px 18px;margin-bottom:10px;}
  .callout.caution{border-left:3px solid var(--caution);}
  .callout.info{border-left:3px solid var(--accent);}
  .callout.critical{border-left:3px solid var(--critical);}
  .callout.pass{border-left:3px solid var(--pass);}
  .callout .c-tag{grid-row:1/span 2;align-self:start;margin-top:2px;font-family:var(--mono);
    font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;padding:3px 8px;border-radius:5px;}
  .callout.caution .c-tag{background:var(--caution-dim);color:var(--caution);}
  .callout.info .c-tag{background:color-mix(in srgb,var(--accent) 16%,transparent);color:var(--accent);}
  .callout.critical .c-tag{background:var(--critical-dim);color:var(--critical);}
  .callout.pass .c-tag{background:var(--pass-dim);color:var(--pass);}
  .callout .c-title{font-size:13px;color:var(--text-hi);font-weight:600;}
  .callout .c-body{font-size:13px;color:var(--text-mid);}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:12px;}
  .card{background:var(--panel);border:1px solid var(--border);border-radius:11px;padding:15px 16px;
    box-shadow:var(--shadow);}
  .card-head{display:flex;align-items:center;justify-content:space-between;gap:10px;}
  .card-id{font-size:12.5px;color:var(--text-hi);word-break:break-all;}
  .card-scope{font-size:11px;color:var(--text-dim);margin-top:3px;}
  .ba{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:10px 0;font-size:13px;}
  .ba .b{color:var(--text-mid);}
  .ba .arr{color:var(--text-dim);}
  .ba .a{color:var(--accent);font-weight:600;}
  .card-why{margin:0 0 12px;font-size:12.5px;color:var(--text-mid);}
  .spark{margin:0 0 12px;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:9px 11px 6px;}
  .spark-lbl{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.04em;
    text-transform:uppercase;color:var(--text-dim);margin-bottom:4px;}
  svg.sparkline{width:100%;height:auto;display:block;overflow:visible;}
  .sparkline .guide{stroke:var(--border-strong);stroke-width:.6;stroke-dasharray:3 3;}
  .sparkline .gtext{fill:var(--text-dim);font-family:var(--mono);font-size:8px;}
  .sparkline .line{fill:none;stroke:var(--accent);stroke-width:1.8;stroke-linejoin:round;stroke-linecap:round;}
  .sparkline .mk{fill:var(--panel);stroke:var(--accent);stroke-width:1.4;}
  .imgs{display:grid;gap:8px;}
  .imgs img{width:100%;height:auto;display:block;border:1px solid var(--border);border-radius:7px;background:#fff;}
  .noimg{font-size:12px;color:var(--text-dim);background:var(--panel-2);border:1px dashed var(--border-strong);
    border-radius:7px;padding:10px 12px;}
  .empty{color:var(--text-dim);font-size:13px;}
  details{border:1px solid var(--border);border-radius:10px;background:var(--panel);overflow:hidden;margin-top:14px;}
  summary{cursor:pointer;list-style:none;padding:14px 18px;display:flex;align-items:center;gap:12px;
    font-size:14px;font-weight:600;}
  summary::-webkit-details-marker{display:none;}
  summary .chev{transition:transform .18s ease;color:var(--text-dim);font-family:var(--mono);}
  details[open] summary .chev{transform:rotate(90deg);}
  summary .count{margin-left:auto;font-size:12px;color:var(--text-dim);font-weight:400;}
  .links{margin:0;padding:6px 18px 16px 40px;columns:2;column-gap:26px;}
  .links li{font-family:var(--mono);font-size:11.5px;margin:2px 0;break-inside:avoid;}
  .links a,.footnote a{color:var(--accent);text-decoration:none;}
  .links a:hover,.footnote a:hover{text-decoration:underline;}
  .table-scroll{overflow-x:auto;border-top:1px solid var(--border);}
  table{border-collapse:collapse;width:100%;font-size:12.5px;min-width:720px;}
  th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--border);vertical-align:top;}
  th{position:sticky;top:0;background:var(--panel-2);color:var(--text-dim);font-size:10.5px;
    letter-spacing:.06em;text-transform:uppercase;font-weight:600;z-index:1;}
  td.id{font-family:var(--mono);color:var(--text-hi);}
  td.scope{color:var(--text-dim);white-space:nowrap;}
  td.ba{color:var(--text-mid);white-space:nowrap;}
  td.arrow{color:var(--text-dim);text-align:center;}
  .vpill{font-family:var(--mono);font-size:10.5px;padding:2px 7px;border-radius:5px;white-space:nowrap;}
  .vpill.applied{background:var(--pass-dim);color:var(--pass);}
  .vpill.unchanged{background:var(--panel-2);color:var(--text-dim);}
  .vpill.guarded{background:var(--caution-dim);color:var(--caution);}
  .vpill.blocked{background:var(--caution-dim);color:var(--caution);}
  .vpill.skipped{background:var(--panel-2);color:var(--text-mid);}
  .vpill.info{background:var(--panel-2);color:var(--accent);}
  tbody tr:hover td{background:color-mix(in srgb,var(--accent) 5%,transparent);}
  .footnote{margin-top:30px;padding-top:16px;border-top:1px solid var(--border);font-size:12px;color:var(--text-dim);}
  .footnote code{font-family:var(--mono);color:var(--text-mid);}
  a:focus-visible,summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px;}
  @media (max-width:720px){.gates{grid-template-columns:1fr;}.links{columns:1;}}
</style>"""

# A single behaviour: a passed build collapses the full journal by default (the
# cards above already carry the story); a failed build leaves it open so the
# problem rows are visible without a click. No animation, nothing decorative.
_SCRIPT = """<script>
  (function () {
    var failed = document.querySelector('.verdict.critical');
    if (failed) {
      document.querySelectorAll('details.journal-wrap').forEach(function (d) { d.open = true; });
    }
  })();
</script>"""
