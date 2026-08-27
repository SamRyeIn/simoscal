"""U3 — deterministic JSON + Markdown emitters for a :class:`BatteryResult`.

The JSON (``analysis_findings.json``) is the machine-readable artifact for
tooling and regression tests: sorted keys, fixed float formatting, so identical
inputs produce a byte-identical file (R6/AE5). The Markdown
(``analysis_findings.md``) is the human summary Claude reads to write
``log_review.md`` — it carries the full battery list, thresholds, the pull
table (with environment context), findings grouped by severity, and the
explicit SKIPPED list.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from .pulls import Pull
from .registry import BatteryResult, Check, Finding, Severity, Skipped

__all__ = [
    "FLOAT_DECIMALS",
    "findings_to_dict",
    "render_markdown",
    "write_findings",
    "md_table",
]

# Every float in the JSON is rounded to this many decimals so serialization is
# stable and diffs stay readable.
FLOAT_DECIMALS = 4

SCHEMA = "simoscal.analysis/1"


def _clean(obj: Any) -> Any:
    """Recursively convert to JSON-safe types with fixed float rounding."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        obj = float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if obj != obj:          # NaN -> null (JSON has no NaN)
            return None
        if obj in (float("inf"), float("-inf")):
            return None
        return round(obj, FLOAT_DECIMALS)
    return obj


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def _check_meta(check: Check) -> dict[str, Any]:
    return {
        "id": check.id,
        "title": check.title,
        "needs_cal": check.needs_cal,
        "required_channels": list(check.required_channels),
        "optional_channels": list(check.optional_channels),
        "thresholds": check.thresholds,
        "description": check.description,
    }


def _pull_dict(p: Pull) -> dict[str, Any]:
    d = asdict(p)          # includes the nested environment dataclass
    return d


def _finding_dict(f: Finding) -> dict[str, Any]:
    return {
        "check_id": f.check_id,
        "severity": f.severity,
        "title": f.title,
        "message": f.message,
        "evidence": dict(f.evidence),
        "pull_refs": list(f.pull_refs),
        "plot_refs": list(f.plot_refs),
    }


def _log_meta(result: BatteryResult) -> list[dict[str, Any]]:
    out = []
    for lf in result.logset.files:
        q = lf.quality
        out.append({
            "name": lf.name,
            "n_rows": q.n_rows,
            "gear_resolution": lf.gear_resolution,
            "interval_median_s": q.interval_median_s,
            "n_short_rows": q.n_short_rows,
            "n_gaps": len(q.gaps),
            "stuck_channels": list(q.stuck_channels),
            "unmapped_count": len(lf.unmapped_headers),
            # The canonical channel ids this file actually carries. A reader with
            # only the findings cannot otherwise tell an absent channel from an
            # uninteresting one, and the back-test caught an answering session
            # declining a sized edit because it could not confirm two channels
            # were logged — they were (Docs/backtest/README.md).
            "channels": sorted(lf.data.keys()),
        })
    return out


def findings_to_dict(result: BatteryResult, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the full findings document as a JSON-ready dict.

    ``extra`` merges additional top-level sections (e.g. U7 ``coverage``).
    """
    doc: dict[str, Any] = {
        "schema": SCHEMA,
        "folder": result.logset.folder.name,
        "cal_resolved": result.cal_resolved,
        "logs": _log_meta(result),
        "battery": [_check_meta(c) for c in result.checks],
        "ran": list(result.ran),
        "pulls": [_pull_dict(p) for p in result.pulls],
        "findings": [_finding_dict(f) for f in result.findings],
        "skipped": [
            {
                "check_id": s.check_id,
                "title": s.title,
                "reason": s.reason,
                "missing_channels": list(s.missing_channels),
            }
            for s in result.skipped
        ],
    }
    if extra:
        doc.update(extra)
    return _clean(doc)


def _to_json(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, ensure_ascii=True, indent=2) + "\n"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Render an aligned GitHub-flavored Markdown table (padded columns)."""
    cols = len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(row[i]) if i < len(row) else 0)

    def fmt(cells: list[str]) -> str:
        padded = [(cells[i] if i < len(cells) else "").ljust(widths[i]) for i in range(cols)]
        return "| " + " | ".join(padded) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    out = [fmt(headers), sep]
    out.extend(fmt(r) for r in rows)
    return "\n".join(out)


def _num(v: Any, fmt: str = "{:.1f}", none: str = "n/a") -> str:
    if v is None:
        return none
    if isinstance(v, float) and v != v:
        return none
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


def _pull_rows(pulls) -> list[list[str]]:
    rows = []
    for p in pulls:
        gear = "unresolved" if not p.gear_resolved else (str(p.gear) if p.gear is not None else "?")
        rows.append([
            str(p.index),
            p.file,
            gear,
            f"{p.start_row}-{p.end_row}",
            f"{_num(p.rpm_min, '{:.0f}')}-{_num(p.rpm_max, '{:.0f}')}",
            f"{_num(p.airmass_min, '{:.0f}')}-{_num(p.airmass_max, '{:.0f}')}",
            _num(p.min_knock, "{:+.1f}"),
            _num(p.max_put, "{:.1f}"),
            _num(p.max_put_error, "{:+.1f}"),
            _num(p.max_boost, "{:.1f}"),
            f"{_num(p.lambda_error_min, '{:+.3f}')}..{_num(p.lambda_error_max, '{:+.3f}')}",
            _num(p.lpfp_max, "{:.1f}"),
            _num(p.hpfp_eff_max, "{:.1f}"),
            _num(p.turbo_speed_max, "{:.0f}"),
        ])
    return rows


def _env_rows(pulls) -> list[list[str]]:
    rows = []
    for p in pulls:
        e = p.environment
        rows.append([
            str(p.index),
            _num(e.ambient_temp_c, "{:.1f}"),
            _num(e.ambient_press_kpa, "{:.1f}"),
            _num(e.iat_start_c, "{:.1f}"),
            _num(e.coolant_temp_c, "{:.1f}"),
            _num(e.eth_content_pct, "{:.1f}"),
        ])
    return rows


def render_markdown(result: BatteryResult, *, extra_sections: list[str] | None = None) -> str:
    """Render the human-readable findings summary."""
    L: list[str] = []
    L.append(f"# Analysis Findings — {result.logset.folder.name}")
    L.append("")
    L.append(
        "Generated by `simoscal.analysis`. This is tool output, not a review — "
        "Claude reads it to write `log_review.md`. Nothing here proposes "
        "calibration changes."
    )
    L.append("")

    # Findings by severity.
    L.append("## Findings")
    L.append("")
    any_finding = False
    for sev in (Severity.HIGH, Severity.MEDIUM, Severity.LOW):
        group = result.findings_by_severity(sev)
        if not group:
            continue
        any_finding = True
        L.append(f"### {sev}")
        L.append("")
        for f in group:
            refs = f" (pulls {', '.join(map(str, f.pull_refs))})" if f.pull_refs else ""
            L.append(f"- **{f.title}** — {f.message}{refs}")
            if f.plot_refs:
                L.append(f"  - evidence: {', '.join(f.plot_refs)}")
        L.append("")
    if not any_finding:
        L.append("_No findings above the reporting threshold._")
        L.append("")

    # SKIPPED.
    L.append("## Skipped checks")
    L.append("")
    if result.skipped:
        L.append(md_table(
            ["Check", "Title", "Reason"],
            [[s.check_id, s.title, s.reason] for s in result.skipped],
        ))
    else:
        L.append("_None — every check in the battery ran._")
    L.append("")

    # Pull summary.
    L.append("## Pull summary")
    L.append("")
    if result.pulls:
        L.append(md_table(
            ["Pull", "File", "Gear", "Rows", "RPM", "Airmass (mg/stk)", "Min Knock",
             "Max PUT", "Max PUT Err", "Max Boost", "Lambda Err", "LPFP", "HPFP", "Turbo"],
            _pull_rows(result.pulls),
        ))
        L.append("")
        L.append("### Per-pull environment context")
        L.append("")
        L.append(md_table(
            ["Pull", "Ambient °C", "Ambient kPa", "IAT start °C", "Coolant °C", "Ethanol %"],
            _env_rows(result.pulls),
        ))
    else:
        L.append("_No WOT pulls detected._")
    L.append("")

    if extra_sections:
        L.extend(extra_sections)

    # Battery enumeration (printable, auditable).
    L.append("## Check battery")
    L.append("")
    battery_rows = []
    for c in result.checks:
        status = "ran" if c.id in result.ran else "skipped"
        thr = "; ".join(f"{k}={v}" for k, v in c.thresholds.items()) or "-"
        battery_rows.append([
            c.id, c.title, "yes" if c.needs_cal else "no", status,
            ", ".join(c.required_channels) or "-", thr,
        ])
    L.append(md_table(
        ["Check", "Title", "Needs cal", "Status", "Required channels", "Thresholds"],
        battery_rows,
    ))
    L.append("")

    # Logs parsed.
    L.append("## Logs parsed")
    L.append("")
    log_rows = []
    for lf in result.logset.files:
        q = lf.quality
        log_rows.append([
            lf.name, str(q.n_rows), lf.gear_resolution,
            _num(q.interval_median_s, "{:.3f}"), str(len(q.gaps)),
            ", ".join(q.stuck_channels) or "-",
        ])
    L.append(md_table(
        ["File", "Rows", "Gear mode", "Interval s", "Gaps", "Stuck channels"],
        log_rows,
    ))
    L.append("")

    if result.logset.notes:
        L.append("### Load notes")
        L.append("")
        for note in result.logset.notes:
            L.append(f"- {note}")
        L.append("")

    return "\n".join(L)


def write_findings(
    result: BatteryResult,
    folder: str | Path,
    *,
    extra_json: dict[str, Any] | None = None,
    extra_sections: list[str] | None = None,
) -> tuple[Path, Path]:
    """Write ``analysis_findings.json`` and ``.md`` into ``folder``.

    Returns the two written paths. The JSON is byte-stable across identical
    reruns.
    """
    folder = Path(folder)
    doc = findings_to_dict(result, extra=extra_json)
    json_path = folder / "analysis_findings.json"
    md_path = folder / "analysis_findings.md"
    json_path.write_text(_to_json(doc), encoding="utf-8")
    md_path.write_text(render_markdown(result, extra_sections=extra_sections), encoding="utf-8")
    return json_path, md_path
