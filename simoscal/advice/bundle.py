"""The context bundle — one deterministic file describing a whole session.

Whoever answers a bundle is somewhere else entirely: a different machine, no
session in front of them, no bin, no XDF. Everything they need to write a
*replayable* recommendation has to be in this one file, and everything they must
not have — the calibration's bytes — has to be out of it. Those two sentences are
the whole design.

**What travels.** Every table the profile resolves, with its current physical
values and its decoded axes; the edit journal as it stands; whatever datalogs
were picked, as the analysis battery's own findings document; the safety brief
:mod:`simoscal.advice.brief` renders for this car; and the provenance that says
which calibration all of it is about.

**What does not.** The bin and the XDF themselves. The bundle carries their
SHA-256 and nothing else of them — a test asserts it, because "we would never"
is not a mechanism. Filesystem paths stay out too: they are a property of the
device that exported, not of the calibration, and two people exporting the same
session state should get the same file.

**Provenance carries structure identity, not just hashes.** Since the profile
became a registry lookup, "which car" is a runtime answer, and an address means
different bytes depending on it: an `SC8S50.V1.0.xdf` address counts from the
start of the whole 4 MB bin, an `SCGa05_cal.xdf` address from the start of the
extracted CAL block. A bundle that did not say which convention it was written
in would invite a reply that is confidently wrong about where it writes.

**Deterministic by contract** (D7). Sorted keys, no timestamp anywhere in the
payload, no dependence on dict iteration order: the same session state exported
twice is byte-identical. That is what makes the back-test reproducible and lets
a person diff two revisions' bundles to see what actually changed.

Nothing here decides anything. The bundle is *context*; the dry-run replay in
:mod:`simoscal.advice.review` is the gate, and the brief says so in its own
first paragraph.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..tune.catalog import catalog
from ..tune.journal import journal_summary
from .brief import safety_brief
from .schema import SCHEMA_VERSION

__all__ = [
    "BUNDLE_VERSION",
    "BundleFile",
    "bundle",
    "logs_section",
    "render",
    "summary_of",
    "write_bundle",
]

#: The bundle's own format version, independent of ``BRIDGE_VERSION`` and of the
#: reply schema's version for the same reason those two are independent of each
#: other: this file is read off the device by something that was not shipped
#: with either.
BUNDLE_VERSION = 1


@dataclass(frozen=True)
class BundleFile:
    """A written bundle: where it landed, what it hashes to, and how big it is."""

    path: Path
    sha256: str
    bytes_written: int
    summary: dict


# --------------------------------------------------------------------------- #
# JSON safety and determinism
# --------------------------------------------------------------------------- #
def _clean(obj: Any) -> Any:
    """Recursively convert to JSON-safe primitives, failing loud on the unknown.

    Deliberately not a best-effort coercion: a value this does not recognise is
    a contract bug in whatever built the payload, and silently stringifying it
    would put a repr in a file another program parses. The catalog and the
    journal already hand over plain floats and strings, so the numpy branches
    here are a backstop, not the normal path.
    """
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        # JSON has no NaN or infinity. A table that decoded to one is a real
        # fact about the calibration, and ``null`` is how the analysis JSON
        # already spells it.
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, Mapping):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = sorted(obj, key=repr) if isinstance(obj, (set, frozenset)) else obj
        return [_clean(v) for v in items]
    # numpy scalars, without importing numpy at module scope
    item = getattr(obj, "item", None)
    if item is not None and getattr(obj, "shape", None) == ():
        return _clean(item())
    tolist = getattr(obj, "tolist", None)
    if tolist is not None:
        return _clean(tolist())
    raise TypeError(f"a bundle cannot carry {type(obj).__name__}")


def render(payload: Mapping[str, Any]) -> str:
    """The payload as the exact text a bundle file holds.

    ``sort_keys`` is the determinism guarantee (D7) rather than a style choice:
    it removes every dependence on the order a dict happened to be built in,
    including the profile registry's and the journal's. List order is meaning —
    the tables stay in the profile's declared order, the journal in the order
    the calls were made — and sorting keys does not touch it.
    """
    return json.dumps(
        _clean(payload), sort_keys=True, ensure_ascii=True, indent=2
    ) + "\n"


# --------------------------------------------------------------------------- #
# the sections
# --------------------------------------------------------------------------- #
def _provenance(tune, session: Optional[Mapping[str, Any]]) -> dict:
    """Which calibration this is — enough for a reply to be matched back to it.

    The first three fields are the ones :func:`simoscal.advice.review.review`
    compares against the open session, so a reply copies them verbatim; the rest
    is structure identity, which no comparison uses but every *address* depends
    on.

    Paths are deliberately absent. Where a bin sat on one device says nothing
    about the calibration and would make two exports of the same session state
    differ, which is exactly what D7 forbids.
    """
    session = dict(session or {})
    base = tune.space("base").profile
    out: dict[str, Any] = {
        "profile": session.get("profile", base.name),
        "bin_sha256": session.get("bin_sha256", ""),
        "xdf_sha256": session.get("xdf_sha256", ""),
        "spaces": sorted(tune.spaces),
        "has_switch_patch": bool(session.get("has_switch_patch", len(tune.spaces) > 1)),
        "profiles": {
            name: space.profile.name for name, space in sorted(tune.spaces.items())
        },
    }
    if session.get("recovered"):
        # A recovered session replayed its journal onto the source bin rather
        # than being opened fresh. Worth stating: it is the one case where the
        # stock ghost behind a table is not available anywhere.
        out["recovered"] = True

    structure = base.structure
    if structure is not None:
        out["structure"] = {
            "name": structure.name,
            "cal_file_offset": structure.cal_file_offset,
            "cal_base_address": structure.cal_base_address,
            "cal_block_length": structure.cal_block_length,
            "full_bin_size": structure.full_bin_size,
        }
    out["xdf_addresses_from_cal"] = bool(base.xdf_addresses_cal_relative)
    out["address_note"] = (
        "Addresses in this calibration's definition are counted from the start "
        "of the CAL block, not the start of the whole bin."
        if base.xdf_addresses_cal_relative else
        "Addresses in this calibration's definition are counted from the start "
        "of the whole bin."
    )
    return out


def _table_section(tune) -> list[dict]:
    """Every resolved table, domain-owned ones included.

    ``include_domain_owned=True`` where the generic editor's catalog says False,
    and the difference is the point: the editor omits an owner-locked table
    because it may not *write* it, while an answering side that could not see
    the boost maps could not recommend anything about boost at all. Each table
    carries its ``owner``, so which call would write it is still stated.

    The stock ghost (:attr:`TableInfo.source_values`) is left out on purpose.
    The journal already reports before-and-after for everything this session
    changed, so the ghost would be a second copy of the same fact — bought by
    decoding a second full buffer for every table on a tablet.
    """
    out = []
    for info in catalog(tune, include_domain_owned=True):
        table = {
            "space": info.space,
            "name": info.name,
            "id": str(info.key),
            "symbol": info.symbol,
            "title": info.title,
            "description": info.description,
            "label": info.id_and_description,
            "units": info.units,
            "units_description": info.units_description,
            "signature": info.signature,
            "shape": list(info.shape),
            "ndim": info.ndim,
            "reversible": info.reversible,
            "is_axis": info.is_axis,
            "owner": info.owner,
            "group": info.group,
            "categories": list(info.categories),
            "values": info.values,
        }
        for which, axis in (("x_axis", info.x_axis), ("y_axis", info.y_axis)):
            table[which] = None if axis is None else {
                "units": axis.units,
                "symbol": axis.symbol,
                "label": axis.label,
                "values": list(axis.values),
            }
        out.append(table)
    return out


def logs_section(paths: Sequence, *, names: Optional[Mapping[str, str]] = None) -> dict:
    """The picked datalogs as the analysis battery's own findings document.

    The battery is the library's one description of what a log says — the same
    checks, thresholds, pull detection and explicit SKIPPED list that
    ``python -m simoscal.analysis`` writes — so a bundle that re-summarised logs
    its own way would be a second opinion nobody maintains.

    Two deliberate omissions. **No plot series**: the app's ``log_overlay`` and
    ``analyze_logs`` ops carry those because something is going to draw them,
    and here nothing is — it would be megabytes of samples for a reader who
    cannot plot. **No calibration**: the cal-aware checks want the bin the logs
    were *recorded on*, and this session's working buffer is a bin that has not
    been flashed. They land in SKIPPED with their reason, which is the honest
    answer; the table section carries the current calibration in full for anyone
    wanting to reason about it directly.

    Imported lazily because the analysis package pulls numpy and the CSV reader,
    and a bundle exported with no logs should not pay for either.
    """
    from ..analysis import (
        CheckContext,
        default_battery,
        detect_pulls,
        findings_to_dict,
        load_logset_files,
        run_battery,
    )

    logset = load_logset_files(list(paths), names=dict(names or {}))
    pulls = detect_pulls(logset)
    document = findings_to_dict(run_battery(default_battery(), CheckContext(
        logset=logset, pulls=pulls, cal=None,
    )))
    # The folder is a directory name on the exporting device — on Android a
    # content-addressed staging directory. It is not a fact about the logs and
    # would make one session's bundle device-dependent (D7).
    document.pop("folder", None)
    return document


def _how_to_reply() -> dict:
    """The contract a reply must meet, stated in the bundle that prompts it.

    Restated here rather than left to the answering guide because the guide is a
    file in a repository and this is the file in front of the reader. It says
    what makes a reply *usable*; what makes it *safe* is the replay, and the
    brief's first paragraph says so.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "reference": "docs/advice-schema.md in the simoscal repository",
        "provenance_to_echo": ["profile", "bin_sha256", "xdf_sha256"],
        "rules": [
            "Name every table by its logical `name` plus the `id` and "
            "`description` in this bundle's table section — a recommendation "
            "naming one without the other is refused before replay.",
            "Address a change the way this bundle's tables are addressed: "
            "space, operation, selection kind and args, and a value or array in "
            "physical units.",
            "Evidence is mandatory and must point at something in this bundle: "
            "a pull, a finding, a journal entry, or a table's current values.",
            "Every recommendation carries a prediction that the next log can "
            "grade as held or did-not-hold.",
            "A recommendation this file's guards refuse is dropped, not "
            "queued — say in `summary` what you would have changed instead.",
        ],
    }


# --------------------------------------------------------------------------- #
# assembling and writing
# --------------------------------------------------------------------------- #
def bundle(
    tune,
    *,
    provenance: Optional[Mapping[str, Any]] = None,
    logs: Optional[Mapping[str, Any]] = None,
    log_names: Iterable[str] = (),
    notes: str = "",
) -> dict:
    """The whole session as one JSON-safe payload.

    ``logs`` is a findings document as :func:`logs_section` produces it, passed
    in rather than loaded here so the caller owns file verification — on the app
    a log is opened only after its hash has been checked, and that check must
    happen before anything is written, not inside the writer.
    """
    payload: dict[str, Any] = {
        "bundle_version": BUNDLE_VERSION,
        "reply": _how_to_reply(),
        "provenance": _provenance(tune, provenance),
        "safety_brief": safety_brief(tune),
        "tables": _table_section(tune),
        "journal": journal_summary(tune.journal),
        "journal_counts": dict(tune.journal.summary_counts()),
        "logs": logs,
        "log_names": list(log_names),
    }
    if notes:
        payload["notes"] = notes
    return payload


def summary_of(payload: Mapping[str, Any]) -> dict:
    """What a person is shown *before* they share the file.

    Counts rather than content: a bundle is about to leave the device, and "how
    much of what" is the question worth answering at that moment.
    """
    logs = payload.get("logs") or {}
    return {
        "bundle_version": payload.get("bundle_version"),
        "profile": (payload.get("provenance") or {}).get("profile", ""),
        "tables": len(payload.get("tables") or []),
        "journal_entries": len(payload.get("journal") or []),
        "logs": list(payload.get("log_names") or []),
        "pulls": len(logs.get("pulls") or []),
        "findings": len(logs.get("findings") or []),
    }


def write_bundle(payload: Mapping[str, Any], dest) -> BundleFile:
    """Render and write the bundle, returning where it went and what it hashes to.

    The text is built in full before the file is opened, so a payload that
    cannot be rendered leaves no partial file behind — the same reason the op
    verifies every log's hash before it gets this far.
    """
    text = render(payload)
    data = text.encode("utf-8")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return BundleFile(
        path=dest,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes_written=len(data),
        summary=summary_of(payload),
    )
