"""V0 feasibility-gate parity payload — one module, run identically on host and Android.

The V0 go/no-go asks a single question: **does the already-trusted ``simoscal``
engine produce byte-for-byte and field-for-field identical results when embedded
in an Android app via Chaquopy?** This module is the answer's only source. Both
the host runner (``android/parity/run_host_parity.py``) and the Android
instrumentation harness import *this file* and call :func:`run_parity`, so the
two sides cannot drift by running different code.

The contract that makes the comparison meaningful:

* Every number that crosses into the report is formatted through :func:`_f`
  (``%.17g``) so a float round-trips exactly rather than through ``repr``
  platform variance.
* The report splits into a **compared** part and an **informational** part.
  ``environment`` and ``timings`` are informational by construction — the Python
  build, ABI, and wall-clock are *expected* to differ between a Mac and a phone,
  and comparing them would fail the gate for the wrong reason. Everything else
  is compared, and :func:`parity_digest` reduces it to one sha256.
* Nothing here writes to the source bin. Edits stage in ``CalFile``'s in-memory
  buffer and are saved into a caller-supplied work directory, so the parity run
  itself upholds the immutable-source rule the rest of the pipeline depends on.

Why these particular operations: they are the narrowest set that still crosses
every layer the phone has to get right — XML parsing at 5.8 MB, numpy decode
math, the checksum implementation (integer-exact), the minimal-diff writer, and
the psi→hPa floor that the boost ceiling depends on. If all five agree, the
engine is portable; if any disagrees, a piecemeal port would inherit the same
disagreement.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Optional

#: Bump when the operation set changes, so an old golden cannot silently pass.
PARITY_VERSION = 1

#: The table the single-cell edit leg writes to, and the cell + value it writes.
#:
#: ``IP_PUT_SP`` — Pressure up throttle setpoint is the right target for three
#: reasons: it is linear (so the physical-units path is exercised rather than the
#: raw fallback), its scaling is genuinely fractional (stock cells decode to
#: values like 591.0361 hPa, not round numbers), and it is only 4 × 6, so the
#: whole grid fits in the report for eyeballing. The fractional scaling is the
#: point — a requested 1500 hPa cannot be represented exactly, so the encoded
#: value differs from the request, and that quantization arithmetic is precisely
#: what a different runtime could get wrong.
#:
#: This edits an in-memory copy and saves to the caller's work directory; the
#: source bin is never written to.
EDIT_TABLE = "IP_PUT_SP"
EDIT_CELL = (0, 0)
EDIT_VALUE = 1500.0

#: The slot and cap the boost leg applies. 10 psi gauge is deliberately low: it
#: must sit below the base ``IP_PUT_SP`` — Pressure up throttle setpoint ceiling
#: so ``_check_below_base_ceiling`` passes, since this leg tests encoding parity,
#: not the guard.
BOOST_SLOT = 5
BOOST_PSI = 10.0

#: psi values whose floored hPa encoding is checked directly. A rounding
#: difference here is the one that would let a cap encode *above* what was asked.
PSI_FLOOR_PROBES = (0.1, 1.0, 7.3, 10.0, 14.7, 20.0, 25.5, 30.0)


def _f(value: Any) -> str:
    """Format a float losslessly and identically on every platform."""
    return f"{float(value):.17g}"


def _farray(values) -> list:
    """Recursively format a numpy array (or nested sequence) through :func:`_f`."""
    import numpy as np

    arr = np.asarray(values)
    if arr.ndim == 0:
        return _f(arr)
    return [_farray(sub) for sub in arr]


def _canonical(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _checksum_reports(reports) -> list:
    """A ChecksumReport list, reduced to comparable fields.

    ``stored``/``computed`` are integers by construction, so they are compared
    exactly rather than through :func:`_f` — a checksum that differs by one bit
    must fail, and float formatting would be the wrong lens for that.
    """
    return [
        {
            "name": r.name,
            "can_verify": bool(r.can_verify),
            "is_stale": bool(r.is_stale),
            "stored": None if r.stored is None else int(r.stored),
            "computed": None if r.computed is None else int(r.computed),
            "covered": [[int(a), int(b)] for a, b in r.covered],
            "detail": r.detail,
        }
        for r in reports
    ]


def environment() -> dict:
    """Informational only — never compared. Records *where* a run happened."""
    import numpy as np

    return {
        "python_version": sys.version,
        "python_version_info": list(sys.version_info[:3]),
        "numpy_version": np.__version__,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "byteorder": sys.byteorder,
        "maxsize": sys.maxsize,
        "float_repr_style": sys.float_repr_style,
    }


# -- individual legs --------------------------------------------------------- #
def _leg_parse(xdf_path: Path, bin_path: Path) -> tuple[Any, dict, float]:
    """Open the XDF + bin and describe the resulting model."""
    from simoscal import CalFile

    started = time.perf_counter()
    cal = CalFile.open(str(xdf_path), str(bin_path))
    elapsed = time.perf_counter() - started

    return cal, {
        "table_count": len(cal),
        "unique_table_count": len(cal.unique_tables()),
        "categories": cal.categories(),
        "region_start": int(cal.model.region_start),
        "region_size": int(cal.model.region_size),
        "bin_sha256": _sha256_file(bin_path),
        "bin_size": bin_path.stat().st_size,
    }, elapsed


def _leg_enumerate(cal, decode_all: bool) -> tuple[dict, float]:
    """Sweep every unique table's metadata, and optionally decode every value.

    The metadata sweep is the cheap, always-on check that the XML parsed to the
    same model. ``decode_all`` additionally forces every table through the numpy
    decode path — the strongest available evidence that the scaling math is
    identical — and is what the parse-time measurement reports on.

    A table that legitimately refuses to decode (non-linear MATH, unsupported
    width) is recorded *as its error type*, not skipped: an engine that raises a
    different error on the phone has still diverged.
    """
    started = time.perf_counter()
    meta_lines = []
    value_lines = []
    decode_errors: dict[str, int] = {}

    for view in sorted(cal.unique_tables(), key=lambda v: v.table.uniqueid):
        meta_lines.append(
            _canonical({
                "uniqueid": int(view.table.uniqueid),
                "symbol": view.symbol or "",
                "title": view.title or "",
                "shape": list(view.shape),
                "units": view.units or "",
            })
        )
        if not decode_all:
            continue
        try:
            value_lines.append(
                _canonical({
                    "uniqueid": int(view.table.uniqueid),
                    "values": _farray(view.values),
                })
            )
        except Exception as exc:  # noqa: BLE001 - the error type is the datum
            name = type(exc).__name__
            decode_errors[name] = decode_errors.get(name, 0) + 1
            value_lines.append(
                _canonical({
                    "uniqueid": int(view.table.uniqueid),
                    "error": name,
                })
            )

    elapsed = time.perf_counter() - started
    result = {
        "table_count": len(meta_lines),
        "metadata_digest": _sha256_text("\n".join(meta_lines)),
        "decoded_all": bool(decode_all),
    }
    if decode_all:
        result["values_digest"] = _sha256_text("\n".join(value_lines))
        result["decode_error_counts"] = decode_errors
    return result, elapsed


def _leg_edit_and_save(cal, work_dir: Path) -> dict:
    """Copy-on-write single-cell edit, then save with checksums corrected.

    This is the whole write path in miniature: stage one cell in physical units,
    confirm only that cell's bytes were staged, write, correct the checksums, and
    then **reopen the file that was actually written** to read the cell back.
    Reading back off the saved file rather than trusting the in-memory buffer is
    the same discipline ``build()`` applies, for the same reason.
    """
    view = cal.get(EDIT_TABLE)
    row, col = EDIT_CELL

    before = _farray(view.values)
    before_cell = _f(view.values[row][col])

    # Warnings are compared, not swallowed. The library's safety contract is
    # "warn loud, never clamp silently", so a runtime that stayed quiet where the
    # host warned has diverged on a safety behaviour, not just on a number.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        view.set_cell(row, col, EDIT_VALUE)
    edit_warnings = sorted(
        f"{type(w.message).__name__}: {w.message}" for w in caught
    )

    after = _farray(view.values)
    after_cell = _f(view.values[row][col])
    edited_ranges = [[int(off), int(length)] for off, length in cal.edited_ranges]

    out_path = work_dir / "v0_parity_edited.bin"
    reports = cal.save(out_path, correct_checksums=True)

    # The file that landed on disk is re-opened and re-read by ``_leg_readback``,
    # deliberately from a fresh ``CalFile`` rather than this in-memory buffer.
    return {
        "table": EDIT_TABLE,
        "cell": list(EDIT_CELL),
        "requested_value": _f(EDIT_VALUE),
        "before_cell": before_cell,
        "after_cell": after_cell,
        "edit_warnings": edit_warnings,
        "before_grid": before,
        "after_grid": after,
        "edited_ranges": edited_ranges,
        "saved_sha256": _sha256_file(out_path),
        "saved_size": out_path.stat().st_size,
        "checksums_after_save": _checksum_reports(reports),
    }


def _leg_readback(xdf_path: Path, saved_bin: Path) -> dict:
    """Reopen the saved bin from scratch and re-verify it end to end."""
    from simoscal import CalFile

    cal = CalFile.open(str(xdf_path), str(saved_bin))
    view = cal.get(EDIT_TABLE)
    row, col = EDIT_CELL
    return {
        "readback_cell": _f(view.values[row][col]),
        "readback_grid": _farray(view.values),
        "checksums": _checksum_reports(cal.verify_checksums()),
    }


def _leg_psi_floor() -> dict:
    """The psi→hPa floor, probed directly.

    ``slot_curve`` floors rather than rounds so a cap asked for as "20 psi"
    cannot encode above 20 psi. That is a safety property of the boost domain, so
    it is checked as arithmetic here — independent of whether a bin is present.
    """
    from simoscal.tune.units import AMBIENT_HPA, hpa_from_psi, psi_from_hpa

    probes = {}
    for psi in PSI_FLOOR_PROBES:
        floored = hpa_from_psi(psi, rounding="floor")
        probes[_f(psi)] = {
            "hpa_floor": _f(floored),
            "psi_roundtrip": _f(psi_from_hpa(floored)),
            "not_above_request": bool(psi_from_hpa(floored) <= psi),
        }
    return {"ambient_hpa": _f(AMBIENT_HPA), "probes": probes}


def _leg_boost_curve(
    xdf_path: Path,
    patched_bin: Path,
    patch_xdf: Path,
    work_dir: Path,
) -> dict:
    """Run one ``slot_curve()`` against a real patched bin, through ``Tune``.

    Opened with ``extra_spaces`` and **no** ``patches=``: the bin is already
    patched, which is exactly how Quick Edit v1 receives it, and it keeps the
    BinToolz tree out of the mobile dependency closure.
    """
    from simoscal.tune import SC8S50, Tune
    from simoscal.tune.domains.switchpatch import PATCH_SPACE
    from simoscal.tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933

    tune = Tune.open(
        SC8S50,
        xdf=str(xdf_path),
        bin=str(patched_bin),
        extra_spaces={PATCH_SPACE: (SWITCH_PATCH_2933, str(patch_xdf))},
    )

    slot_name = f"slot{BOOST_SLOT}_put_setpoint"
    before = _farray(tune.values(slot_name, space=PATCH_SPACE))
    base_ceiling = _f(
        max(float(v) for v in tune.values("put_setpoint")[-1])
    )

    entry = tune.switchpatch.slot_curve(
        BOOST_SLOT, psi=BOOST_PSI,
        intent="V0 parity: flat cap, must encode identically on both runtimes",
    )
    after = _farray(tune.values(slot_name, space=PATCH_SPACE))

    return {
        "slot": BOOST_SLOT,
        "requested_psi": _f(BOOST_PSI),
        "base_ceiling_hpa": base_ceiling,
        "before_grid": before,
        "after_grid": after,
        "journal_entry": {
            "name": entry.name,
            "label": entry.label,
            "key": str(entry.key),
            "kind": entry.kind,
            "verdict": entry.verdict,
            "intent": entry.intent,
            "detail": entry.detail,
        },
        "patched_bin_sha256": _sha256_file(patched_bin),
    }


# -- orchestration ----------------------------------------------------------- #
def run_parity(
    *,
    xdf_path: str,
    bin_path: str,
    work_dir: str,
    patch_xdf_path: Optional[str] = None,
    patched_bin_path: Optional[str] = None,
    decode_all: bool = True,
) -> dict:
    """Run every parity leg and return the full report.

    ``patch_xdf_path``/``patched_bin_path`` are optional: when either is absent
    the boost leg records ``skipped`` **in the compared part of the report**, so
    a host golden generated without them can never silently "match" an Android
    run that had them. Skipping is a result, not an absence.
    """
    xdf = Path(xdf_path)
    src_bin = Path(bin_path)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    timings: dict[str, str] = {}
    steps: dict[str, Any] = {}

    cal, parse_info, parse_secs = _leg_parse(xdf, src_bin)
    steps["parse"] = parse_info
    timings["parse_seconds"] = _f(parse_secs)

    enumerate_info, enumerate_secs = _leg_enumerate(cal, decode_all)
    steps["enumerate"] = enumerate_info
    timings["enumerate_seconds"] = _f(enumerate_secs)

    steps["checksums_before_edit"] = _checksum_reports(cal.verify_checksums())

    edit_started = time.perf_counter()
    steps["edit_and_save"] = _leg_edit_and_save(cal, work)
    timings["edit_and_save_seconds"] = _f(time.perf_counter() - edit_started)

    steps["readback"] = _leg_readback(xdf, work / "v0_parity_edited.bin")
    steps["psi_floor"] = _leg_psi_floor()

    if patch_xdf_path and patched_bin_path:
        boost_started = time.perf_counter()
        steps["boost_curve"] = _leg_boost_curve(
            xdf, Path(patched_bin_path), Path(patch_xdf_path), work
        )
        timings["boost_curve_seconds"] = _f(time.perf_counter() - boost_started)
    else:
        steps["boost_curve"] = {"skipped": "patched bin or switch-patch XDF not supplied"}

    compared = {"parity_version": PARITY_VERSION, "steps": steps}
    return {
        "compared": compared,
        "digest": parity_digest(compared),
        "environment": environment(),
        "timings": timings,
    }


def parity_digest(compared: dict) -> str:
    """The single sha256 the go/no-go turns on."""
    return _sha256_text(_canonical(compared))


def compare(host: dict, device: dict) -> dict:
    """Diff two reports, returning the first differing paths.

    Returns ``{"match": True}`` when the compared parts agree. Otherwise lists
    every differing leaf path with both values, so a failure names *what* diverged
    rather than only that something did.
    """
    diffs: list[dict] = []

    def walk(a: Any, b: Any, path: str) -> None:
        if type(a) is not type(b):
            diffs.append({"path": path, "host": repr(a), "device": repr(b)})
            return
        if isinstance(a, dict):
            for key in sorted(set(a) | set(b)):
                if key not in a:
                    diffs.append({"path": f"{path}.{key}", "host": "<missing>",
                                  "device": repr(b[key])})
                elif key not in b:
                    diffs.append({"path": f"{path}.{key}", "host": repr(a[key]),
                                  "device": "<missing>"})
                else:
                    walk(a[key], b[key], f"{path}.{key}")
        elif isinstance(a, list):
            if len(a) != len(b):
                diffs.append({"path": f"{path}.length", "host": len(a), "device": len(b)})
                return
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}[{i}]")
        elif a != b:
            diffs.append({"path": path, "host": repr(a), "device": repr(b)})

    walk(host.get("compared"), device.get("compared"), "")

    # Re-derive both digests from the payloads rather than trusting the numbers
    # each side reported. A report that was truncated, hand-edited, or written by
    # a mismatched parity version would otherwise diff clean on the fields that
    # survived and carry a stale digest nobody checked.
    host_recomputed = parity_digest(host.get("compared", {}))
    device_recomputed = parity_digest(device.get("compared", {}))
    digest_ok = (
        host.get("digest") == host_recomputed
        and device.get("digest") == device_recomputed
    )

    return {
        "match": bool(not diffs and digest_ok),
        "host_digest": host.get("digest"),
        "device_digest": device.get("digest"),
        "host_digest_recomputed": host_recomputed,
        "device_digest_recomputed": device_recomputed,
        "digests_self_consistent": digest_ok,
        "diff_count": len(diffs),
        "diffs": diffs[:100],
    }
