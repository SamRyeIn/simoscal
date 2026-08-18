"""Compatibility preflight — decide, before any edit session exists, whether a
bin + XDF pair is a calibration this tool can safely edit.

This is the gate a caller runs the instant a user picks files — the front door of
the library — and its whole job is to say *no* early and legibly. A wrong byte flashed to the ECU can
brick it or damage the engine, so the safe default is to refuse anything not
positively recognised — never to edit hopefully and hope the guards downstream
catch it.

Three things make this trustworthy:

* **It never modifies the source.** Every check reads; nothing is written, and
  the file the user picked is untouched no matter the verdict.
* **Recognition is by explicit profile identity, never by filename or a fuzzy
  title match.** A bin is "SC8S50" only if the whole SC8S50 profile resolves
  against the XDF *by symbol and by exact table geometry* — a same-named table
  with a different shape is a different table and fails recognition. So a random
  Simos 18 XDF cannot masquerade as this car's calibration.
* **There is no "continue anyway".** The verdict's :attr:`Verdict.ok_to_edit`
  is the whole decision; a blocked input offers *choose another file*, not a
  bypass. And the function keeps no state between calls, so re-running on a
  replacement file cannot inherit anything from a rejected one.

The verdict separates a **stale-but-correctable checksum** (reportable — the
build step recomputes it) from an **unrecognised layout** (blocking). Those are
very different: the first is a normal consequence of an edit and is fixed
automatically before flashing; the second means we do not understand the file
and must not touch it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union
from xml.etree.ElementTree import ParseError as XmlParseError

from . import checksum
from .calfile import CalFile
from .model import RegionBoundsError, SimosCalError
from .xdf import XdfParseError, parse_xdf

__all__ = [
    "ChecksumState",
    "Verdict",
    "preflight",
    "READY",
    "READY_STALE_CHECKSUM",
    "INSPECT_ONLY",
    "BLOCKED",
]

#: A full Simos18 SC8S50 image is 4 MiB. Anything smaller is a CAL-only slice or
#: a truncated file — neither is a flashable image this tool will edit.
FULL_BIN_SIZE = 0x400000

# -- verdict statuses -------------------------------------------------------- #
#: Recognised SC8S50 full bin, checksums verify clean — safe to open for editing.
READY = "READY"
#: As READY, but a checksum is stale and *correctable*; the build step will fix
#: it. Still safe to edit — this is the normal state of an already-edited bin.
READY_STALE_CHECKSUM = "READY_STALE_CHECKSUM"
#: The XDF + bin parse and load, but this is not the SC8S50 calibration, so the
#: SC8S50 safety knowledge does not apply. Readable, never writable.
INSPECT_ONLY = "INSPECT_ONLY"
#: Unusable: truncated, unparseable, out-of-region, CAL-only, or unrecognised.
BLOCKED = "BLOCKED"

_EDITABLE = frozenset({READY, READY_STALE_CHECKSUM})


@dataclass(frozen=True)
class ChecksumState:
    """One embedded checksum's preflight state, plain-language + raw."""

    name: str
    can_verify: bool
    is_stale: bool
    correctable: bool
    stored: Optional[int]
    computed: Optional[int]
    detail: str

    def message(self) -> str:
        if not self.can_verify:
            return f"{self.name}: cannot verify ({self.detail})"
        if self.is_stale:
            fix = "correctable at build time" if self.correctable else "NOT correctable"
            return f"{self.name}: stale ({fix})"
        return f"{self.name}: valid"


@dataclass(frozen=True)
class Verdict:
    """The whole compatibility decision for one bin + XDF pair.

    :attr:`ok_to_edit` is the single fact the caller acts on. Everything else is
    evidence for the human: :attr:`summary` is the one-line plain-language verdict,
    :attr:`reasons` the plain-language details, and :attr:`advanced` the raw
    specifics for a "show details" pane. Nothing here references the app or any
    UI — it is a data object a Compose screen (or a test) reads.
    """

    ok_to_edit: bool
    status: str
    summary: str
    reasons: tuple[str, ...]

    # provenance
    bin_path: str
    xdf_path: str
    bin_size: Optional[int]
    bin_sha256: Optional[str]
    xdf_sha256: Optional[str]

    # layout / identity
    region_start: Optional[int]
    region_size: Optional[int]
    profile_name: Optional[str]
    profile_matched: bool
    writable: bool

    # checksums
    checksums: tuple[ChecksumState, ...]

    # optional switch-patch finding (None = not checked)
    switch_patch_present: Optional[bool]

    advanced: dict = field(default_factory=dict)

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.ok_to_edit


# --------------------------------------------------------------------------- #
# internal helpers
# --------------------------------------------------------------------------- #
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _blocked(
    *,
    bin_path: Path,
    xdf_path: Path,
    summary: str,
    reasons: tuple[str, ...],
    bin_size: Optional[int] = None,
    bin_sha256: Optional[str] = None,
    xdf_sha256: Optional[str] = None,
    region_start: Optional[int] = None,
    region_size: Optional[int] = None,
    profile_name: Optional[str] = None,
    checksums: tuple[ChecksumState, ...] = (),
    advanced: Optional[dict] = None,
) -> Verdict:
    return Verdict(
        ok_to_edit=False,
        status=BLOCKED,
        summary=summary,
        reasons=reasons,
        bin_path=str(bin_path),
        xdf_path=str(xdf_path),
        bin_size=bin_size,
        bin_sha256=bin_sha256,
        xdf_sha256=xdf_sha256,
        region_start=region_start,
        region_size=region_size,
        profile_name=profile_name,
        profile_matched=False,
        writable=False,
        checksums=checksums,
        switch_patch_present=None,
        advanced=advanced or {},
    )


def _checksum_states(data: bytes) -> tuple[ChecksumState, ...]:
    """Verify both CAL checksums and describe them, without correcting anything.

    Correctability is derived by asking the checksum module for a corrected copy
    and re-verifying it — so "correctable" is a proven fact, not an assumption.
    """
    reports = {r.name: r for r in checksum.verify(data)}
    states: list[ChecksumState] = []

    corrected_clean: set[str] = set()
    if any(r.can_verify and r.is_stale for r in reports.values()):
        try:
            fixed, _pre = checksum.correct(data)
            corrected_clean = {
                r.name for r in checksum.verify(bytes(fixed))
                if r.can_verify and not r.is_stale
            }
        except Exception:  # noqa: BLE001 - correction is best-effort evidence only
            corrected_clean = set()

    for name in ("CAL_CRC", "ECM3"):
        r = reports.get(name)
        if r is None:
            continue
        correctable = r.can_verify and r.is_stale and name in corrected_clean
        states.append(ChecksumState(
            name=r.name,
            can_verify=r.can_verify,
            is_stale=r.is_stale,
            correctable=correctable,
            stored=r.stored,
            computed=r.computed,
            detail=r.detail,
        ))
    return tuple(states)


def _detect_switch_patch(
    bin_path: Path, switch_patch_xdf: Path
) -> tuple[Optional[bool], dict]:
    """Resolve the switch-patch profile against ``switch_patch_xdf`` + the bin.

    Present means the five slot grids resolve *and* slot 1 decodes to real values —
    a patch declared but not applied would resolve yet decode to the as-patched
    default (or fail). Returns ``(present_or_None, advanced_detail)``; ``None``
    only on an internal error, never as a guess.
    """
    from .tune.profile import resolve, ProfileResolutionError
    from .tune.profiles.switchpatch_2933 import SWITCH_PATCH_2933, slot_names

    try:
        patch_cal = CalFile.open(str(switch_patch_xdf), str(bin_path))
    except Exception as exc:  # noqa: BLE001
        return None, {"switch_patch_error": f"could not open patch XDF: {exc}"}

    try:
        resolved = resolve(
            SWITCH_PATCH_2933, patch_cal,
            names=list(slot_names("put_setpoint")),
            xdf_label=str(switch_patch_xdf),
        )
    except ProfileResolutionError as exc:
        return False, {
            "switch_patch": "slot tables did not resolve against the patch XDF "
                            "(patch not present, or a different patch)",
            "misses": [m.format() for m in exc.misses],
        }
    except Exception as exc:  # noqa: BLE001
        return None, {"switch_patch_error": str(exc)}

    try:
        import numpy as np

        grid = resolved.view("slot1_put_setpoint").values
        present = bool(np.isfinite(grid).all() and np.any(grid != 0.0))
        return present, {
            "switch_patch_slot1_range":
                f"{float(np.min(grid)):.0f}..{float(np.max(grid)):.0f}",
        }
    except Exception as exc:  # noqa: BLE001
        return None, {"switch_patch_error": f"slot decode failed: {exc}"}


# --------------------------------------------------------------------------- #
# the preflight
# --------------------------------------------------------------------------- #
def preflight(
    bin_path: Union[str, Path],
    xdf_path: Union[str, Path],
    *,
    switch_patch_xdf: Optional[Union[str, Path]] = None,
) -> Verdict:
    """Decide whether ``bin_path`` + ``xdf_path`` is a safely-editable SC8S50 bin.

    Reads only; the source files are never modified. Returns a :class:`Verdict`
    whose :attr:`Verdict.ok_to_edit` is the decision. The function holds no state
    between calls, so re-running on a replacement file carries nothing over from a
    file it just rejected.

    ``switch_patch_xdf`` is optional: when given, the verdict additionally reports
    whether the BinToolz 5-slot switch patch is present (which gates the
    boost-curve editor). When omitted, ``switch_patch_present`` is ``None`` —
    "not checked", never guessed.
    """
    bin_path = Path(bin_path)
    xdf_path = Path(xdf_path)

    # -- existence ---------------------------------------------------------- #
    if not bin_path.is_file():
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary="The bin file was not found.",
            reasons=(f"No file at {bin_path}.",),
        )
    if not xdf_path.is_file():
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary="The XDF definition file was not found.",
            reasons=(f"No file at {xdf_path}.",),
            bin_size=bin_path.stat().st_size,
        )

    bin_size = bin_path.stat().st_size
    bin_hash = _sha256(bin_path)
    xdf_hash = _sha256(xdf_path)

    # -- XDF parse ---------------------------------------------------------- #
    try:
        model = parse_xdf(str(xdf_path))
    except (XdfParseError, XmlParseError) as exc:
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary="The XDF could not be parsed.",
            reasons=("This does not look like a valid TunerPro XDF definition.",
                     str(exc)),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
        )
    region_start = model.region_start
    region_size = model.region_size
    region_end = region_start + region_size

    # -- bin size vs the XDF's declared region ------------------------------ #
    if bin_size < region_end:
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary="The bin is too small for this XDF — it looks truncated or CAL-only.",
            reasons=(
                f"The XDF addresses bytes up to {region_end:#x} "
                f"({region_end:,}), but the file is only {bin_size:,} bytes.",
                "Import a complete bin read from the ECU.",
            ),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
        )

    # -- load (region-checked) ---------------------------------------------- #
    try:
        cal = CalFile.open(str(xdf_path), str(bin_path))
    except (RegionBoundsError, SimosCalError) as exc:
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary="The bin could not be loaded against this XDF.",
            reasons=("The XDF's addressable region does not fit the bin.", str(exc)),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
        )

    # -- profile identity (exact, by symbol AND shape) ---------------------- #
    from .tune.profile import resolve, ProfileResolutionError
    from .tune.profiles import SC8S50

    try:
        resolve(SC8S50, cal, xdf_label=str(xdf_path))
        profile_matched = True
    except ProfileResolutionError as exc:
        # Parsed and loaded, but not our calibration. Readable, not writable.
        return Verdict(
            ok_to_edit=False,
            status=INSPECT_ONLY,
            summary="This is a valid calibration file, but not the SC8S50 (GTI) "
                    "layout this tool edits — it can be inspected, not edited.",
            reasons=(
                "The SC8S50 profile did not resolve against this XDF: one or more "
                "of its tables is missing or has a different shape here.",
                "Editing is limited to bins whose layout exactly matches the "
                "SC8S50 profile, so the car-specific safety rules always apply.",
            ),
            bin_path=str(bin_path), xdf_path=str(xdf_path),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
            profile_name=None, profile_matched=False, writable=False,
            checksums=(),
            switch_patch_present=None,
            advanced={"profile_misses": [m.format() for m in exc.misses][:20]},
        )

    # -- checksums ---------------------------------------------------------- #
    data = cal.binimage.to_bytes()
    checksums = _checksum_states(data)
    ecm3 = next((c for c in checksums if c.name == "ECM3"), None)

    # ECM3 needs the full bin (its area addresses live in ASW1). A CAL-only image
    # cannot verify ECM3 and is therefore not a flashable image we will edit.
    if ecm3 is not None and not ecm3.can_verify:
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary="This looks like a CAL-only image — the full-bin checksum "
                    "(ECM3) cannot be verified.",
            reasons=(
                "ECM3 protects an area whose addresses live outside the CAL block, "
                "so a CAL-only slice cannot be checksum-verified before flashing.",
                "Import a complete 4 MB bin read from the ECU.",
            ),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
            profile_name=SC8S50.name, checksums=checksums,
        )

    stale_states = [c for c in checksums if c.can_verify and c.is_stale]
    uncorrectable = [c for c in stale_states if not c.correctable]
    if uncorrectable:
        names = ", ".join(c.name for c in uncorrectable)
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary=f"A checksum ({names}) is wrong and cannot be corrected — the "
                    "bin may be damaged.",
            reasons=(
                f"{names} does not match the calibration and re-computing it did "
                "not produce a clean value.",
                "This usually means the file is corrupt; import a known-good bin.",
            ),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
            profile_name=SC8S50.name, checksums=checksums,
        )

    # -- optional switch-patch detection ------------------------------------ #
    switch_present: Optional[bool] = None
    switch_advanced: dict = {}
    if switch_patch_xdf is not None:
        switch_present, switch_advanced = _detect_switch_patch(
            bin_path, Path(switch_patch_xdf)
        )

    # -- verdict ------------------------------------------------------------ #
    if stale_states:
        names = ", ".join(c.name for c in stale_states)
        status = READY_STALE_CHECKSUM
        summary = (f"Ready to edit — recognised SC8S50 bin. Note: {names} is stale "
                   "and will be corrected automatically at build time.")
        reasons = (
            "This is the SC8S50 (GTI) calibration and every profile table resolved "
            "by symbol and shape.",
            f"{names} does not currently match — normal for an already-edited bin; "
            "the build step recomputes it before the bin is shareable.",
        )
    else:
        status = READY
        summary = "Ready to edit — recognised SC8S50 bin with valid checksums."
        reasons = (
            "This is the SC8S50 (GTI) calibration and every profile table resolved "
            "by symbol and shape.",
            "Both embedded checksums (CAL_CRC, ECM3) verify clean.",
        )

    advanced = {
        "profile": SC8S50.name,
        "region": f"[{region_start:#x}, {region_end:#x})",
        "full_bin": bin_size == FULL_BIN_SIZE,
        **switch_advanced,
    }

    return Verdict(
        ok_to_edit=status in _EDITABLE,
        status=status,
        summary=summary,
        reasons=reasons,
        bin_path=str(bin_path), xdf_path=str(xdf_path),
        bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
        region_start=region_start, region_size=region_size,
        profile_name=SC8S50.name, profile_matched=True, writable=True,
        checksums=checksums,
        switch_patch_present=switch_present,
        advanced=advanced,
    )
