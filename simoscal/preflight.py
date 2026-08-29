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
  title match.** A bin is "SC8S50", say, only if the whole SC8S50 profile
  resolves against the XDF *by symbol and by exact table geometry* — a
  same-named table with a different shape is a different table and fails
  recognition. So a random Simos 18 XDF cannot masquerade as a mapped
  calibration. Which profiles are tried comes from the registry
  (:data:`~simoscal.tune.profiles.BASE_PROFILES`), so adding a car is adding a
  map file; the bar each one has to clear is unchanged.
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
from typing import TYPE_CHECKING, Optional, Union
from xml.etree.ElementTree import ParseError as XmlParseError

from . import checksum
from .calfile import CalFile, structure_of
from .model import RegionBoundsError, SimosCalError
from .xdf import XdfParseError, parse_xdf

if TYPE_CHECKING:  # pragma: no cover - import cycle: tune imports this module
    from .tune.profile import Profile

__all__ = [
    "AmbiguousProfileError",
    "ChecksumState",
    "Verdict",
    "preflight",
    "READY",
    "READY_STALE_CHECKSUM",
    "INSPECT_ONLY",
    "BLOCKED",
]


class AmbiguousProfileError(SimosCalError):
    """Raised when more than one registered profile fully resolves against a file.

    Not a verdict, because no choice of file can fix it: two shipped maps both
    claiming the same calibration is a defect in the registry, and the user
    picking a different bin would only move it. Taking the first match would mean
    editing under one car's safety rules while the file might be another's —
    exactly the substitution this module exists to prevent — so the failure is
    loud and names both profiles.
    """


# -- verdict statuses -------------------------------------------------------- #
#: Recognised full bin (some registered profile matched), checksums verify clean
#: — safe to open for editing.
READY = "READY"
#: As READY, but a checksum is stale and *correctable*; the build step will fix
#: it. Still safe to edit — this is the normal state of an already-edited bin.
READY_STALE_CHECKSUM = "READY_STALE_CHECKSUM"
#: The XDF + bin parse and load, but no registered profile recognises them, so
#: no car's safety knowledge applies. Readable, never writable.
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
    # Preflight is the one caller that does not know the car yet — that is what
    # it is for — so it discovers the layout from the bin rather than being told,
    # and degrades to cannot-verify when it cannot.
    reports = {r.name: r for r in checksum.verify_discovered(data)}
    states: list[ChecksumState] = []

    corrected_clean: set[str] = set()
    if any(r.can_verify and r.is_stale for r in reports.values()):
        try:
            spec = checksum.discover_structure(data)
            fixed, _pre = checksum.correct(data, spec)
            corrected_clean = {
                r.name for r in checksum.verify(bytes(fixed), spec)
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


def _identify(
    cal: CalFile, xdf_label: str
) -> tuple[Optional["Profile"], dict[str, list[str]]]:
    """Try every registered base profile; return the one that matched, and the misses.

    Every profile is attempted — the loop does not stop at the first success —
    because "exactly one profile matched" is the fact that makes the verdict
    mean anything, and a first-match return could not tell that apart from
    "two matched and I stopped early". Order is therefore irrelevant to the
    outcome by construction rather than by convention.

    Returns ``(profile_or_None, misses_by_profile_name)``. Raises
    :class:`AmbiguousProfileError` when more than one matched.
    """
    from .tune.profile import resolve, ProfileResolutionError
    from .tune.profiles import BASE_PROFILES

    matched: list["Profile"] = []
    misses: dict[str, list[str]] = {}
    for profile in BASE_PROFILES:
        try:
            resolve(profile, cal, xdf_label=xdf_label)
        except ProfileResolutionError as exc:
            misses[profile.name] = [m.format() for m in exc.misses]
        else:
            matched.append(profile)

    if len(matched) > 1:
        names = ", ".join(p.name for p in matched)
        raise AmbiguousProfileError(
            f"{len(matched)} registered profiles all fully resolve against "
            f"{xdf_label}: {names}. Preflight cannot say which car's safety rules "
            "apply, and will not guess — one of these maps is wrong, or two cars "
            "genuinely share a layout and need a discriminator beyond resolution."
        )
    return (matched[0] if matched else None), misses


def _detect_switch_patch(
    bin_path: Path, switch_patch_xdf: Path, profile: "Profile"
) -> tuple[Optional[bool], dict]:
    """Resolve ``profile``'s switch-patch map against ``switch_patch_xdf`` + the bin.

    Present means the five slot grids resolve *and* slot 1 decodes to real values —
    a patch declared but not applied would resolve yet decode to the as-patched
    default (or fail). Returns ``(present_or_None, advanced_detail)``; ``None``
    only on an internal error, never as a guess.

    Which map to use comes from ``profile`` — the car preflight has already
    identified — and never from the patch XDF itself. The patch tables are bound
    by uniqueid, so another car's map would resolve against this file rather than
    miss, and report a confident answer about 92 wrong addresses.
    """
    from .tune.profile import resolve, ProfileResolutionError
    from .tune.profiles import patch_profile_for
    from .tune.profiles.switchpatch_2933 import slot_names

    try:
        patch_profile = patch_profile_for(profile)
    except KeyError as exc:
        # "Could not look", not "not present" — the CR-20260815-02 distinction,
        # reached here by a different route.
        return None, {"switch_patch_error": str(exc.args[0])}

    try:
        patch_cal = CalFile.open(
            str(switch_patch_xdf), str(bin_path), structure=structure_of(bin_path)
        )
    except Exception as exc:  # noqa: BLE001
        return None, {"switch_patch_error": f"could not open patch XDF: {exc}"}

    try:
        resolved = resolve(
            patch_profile, patch_cal,
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
    """Decide whether ``bin_path`` + ``xdf_path`` is a safely-editable bin.

    "Safely editable" means exactly one profile in
    :data:`~simoscal.tune.profiles.BASE_PROFILES` fully resolves against the XDF;
    :attr:`Verdict.profile_name` says which, and :attr:`Verdict.writable` follows
    from that match rather than from any hardcoded car.

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
        discovered = structure_of(bin_path)
        cal = CalFile.open(str(xdf_path), str(bin_path), structure=discovered)
    except (RegionBoundsError, SimosCalError) as exc:
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary="The bin could not be loaded against this XDF.",
            reasons=("The XDF's addressable region does not fit the bin.", str(exc)),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
        )

    # -- profile identity (exact, by symbol AND shape) ---------------------- #
    from .tune.profiles import BASE_PROFILES

    profile, misses = _identify(cal, str(xdf_path))
    if profile is None:
        # Parsed and loaded, but no registered profile recognises it. Readable,
        # not writable. The deftitle is quoted so the refusal says what the file
        # claims to be — untrusted, and never the basis for recognition, but the
        # difference between "not ours" and "this is SCGA0531_C_OEM.a2l, which we
        # do not map" is the difference between a dead end and a next step.
        tried = ", ".join(p.name for p in BASE_PROFILES) or "none"
        # Report the near miss: whichever profile came closest is the one a
        # reader can act on, and with one registered profile it is simply it.
        # Ties break on name rather than on registry order, so the *evidence* is
        # as order-independent as the verdict — a reader comparing two runs
        # should never see the near miss move because a profile was added.
        closest = min(misses, key=lambda n: (len(misses[n]), n)) if misses else None
        advanced: dict = {
            "deftitle": model.deftitle,
            "profiles_tried": [p.name for p in BASE_PROFILES],
        }
        if closest is not None:
            advanced["closest_profile"] = closest
            advanced["profile_misses"] = misses[closest][:20]
        if len(misses) > 1:
            advanced["profile_misses_by_profile"] = {
                n: m[:20] for n, m in misses.items()
            }
        return Verdict(
            ok_to_edit=False,
            status=INSPECT_ONLY,
            summary=(
                "This is a valid calibration file — it identifies itself as "
                f"{model.deftitle} — but it is not a layout this tool maps, so "
                "it can be inspected, not edited."
                if model.deftitle else
                "This is a valid calibration file, but it declares no title and "
                "is not a layout this tool maps, so it can be inspected, not "
                "edited."
            ),
            reasons=(
                "No profile this tool ships resolves against this XDF (tried: "
                f"{tried}): one or more of the profile's tables is missing or has "
                "a different shape here.",
                "Editing is limited to bins whose layout exactly matches a mapped "
                "profile, so the car-specific safety rules always apply.",
            ),
            bin_path=str(bin_path), xdf_path=str(xdf_path),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
            profile_name=None, profile_matched=False, writable=False,
            checksums=(),
            switch_patch_present=None,
            advanced=advanced,
        )

    # -- the bin must be the calibration the profile describes -------------- #
    # Identification above resolved the *XDF*: it read symbols and shapes out of
    # a definition file and opened no calibration at all. This is where the two
    # are tied together — the CAL block this bin's own headers describe has to be
    # the one the profile declares. Without it, one car's bin handed another
    # car's definition file is recognised as the *second* car, and every read and
    # write afterwards lands at the second car's addresses inside the first car's
    # file, in bytes its checksums do not cover, so the result builds and
    # verifies clean and is wrong everywhere (CR-20260828-01).
    #
    # ``profile`` came from BASE_PROFILES, whose membership rule *is* "declares a
    # structure", so this is always present — but read it once rather than
    # reaching through the Optional below.
    structure = profile.structure
    assert structure is not None, "a base profile always declares a structure"
    mismatch = profile.structure_mismatch(discovered)
    if mismatch is not None:
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary=(
                f"This XDF's tables match the {profile.name} profile, but the bin "
                f"is not a {profile.name} calibration."
            ),
            reasons=(
                mismatch + ".",
                "The definition file and the bin are from different cars. Every "
                "table would be read and written at the other car's addresses — "
                "outside the region this bin's checksums protect, so the result "
                "would build and flash without complaint.",
                "Pair this bin with its own definition file, or this definition "
                "file with its own bin.",
            ),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
            profile_name=profile.name,
            advanced={
                "deftitle": model.deftitle,
                # As with the BASEOFFSET refusal below: the profile *did* resolve,
                # and ``profile_matched`` stays False because nothing here may be
                # edited as that car. Both facts matter, so both are stated.
                "profile_resolved": True,
                "discovered_cal_file_offset": f"{discovered.cal_file_offset:#x}",
                "discovered_cal_base_address": f"{discovered.cal_base_address:#x}",
                "profile_cal_file_offset": f"{structure.cal_file_offset:#x}",
                "profile_cal_base_address": f"{structure.cal_base_address:#x}",
            },
        )

    # -- and it must be the whole image, not a slice of it ------------------ #
    # The size check further up compares the file against the *XDF's* declared
    # region. For a definition file that numbers tables from the start of the
    # calibration block, that region ends where the calibration does and says
    # nothing about the rest of the flash. A bin truncated there keeps every CAL
    # byte and enough ASW data for both checksums to verify, so it passes that
    # check, this module's checksum checks, and every later build gate — while
    # not being a flashable image at all (CR-20260828-03). The profile's declared
    # image size is the only thing that can tell the difference, and it is known
    # by now.
    if bin_size != structure.full_bin_size:
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary=(
                f"This is a {profile.name} calibration, but the file is not a "
                "complete image."
            ),
            reasons=(
                f"A {profile.name} bin is {structure.full_bin_size:,} bytes "
                f"({structure.full_bin_size:#x}); this file is {bin_size:,}.",
                "A partial image can still verify its checksums — they only cover "
                "the calibration block — so passing them is not evidence the file "
                "is whole.",
                "Import a complete bin read from the ECU.",
            ),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
            profile_name=profile.name,
            advanced={
                "deftitle": model.deftitle,
                "profile_resolved": True,
                "expected_bin_size": f"{structure.full_bin_size:#x}",
            },
        )

    # -- the XDF must count from where the profile says it counts ---------- #
    # Profile resolution matches on symbol and shape, which says nothing about
    # *where* the XDF reads. A definition file can name every table correctly,
    # declare every shape correctly, and still point every address at the wrong
    # part of the bin.
    #
    # Two conventions are legitimate and both appear in files we hold:
    # `SC8S50.V1.0.xdf` numbers its tables from the start of the whole bin and
    # declares BASEOFFSET 0x200000; `SCGa05_cal.xdf` numbers them from the start
    # of the extracted CAL block and declares 0. Neither is faulty — but a file
    # of the second kind handed a full bin reads every table 0x220000 short, in
    # bytes no CAL checksum covers, so an edit would build clean and flash wrong.
    #
    # Which convention a car's XDF uses is a per-car fact, declared on the
    # profile beside that car's other per-car facts, and this gate holds the file
    # to it. That direction matters: the library is not deciding what the file
    # means, it is checking the file is the one the profile was authored
    # against. A file declaring anything else is refused rather than
    # accommodated, so a third convention has to be read and declared by a human
    # before anything is written through it.
    #
    expected_base = profile.expected_xdf_base_offset
    # Subtract mode would make the XDF's addresses ECU addresses rather than
    # offsets, so the comparison below would be against the wrong quantity. No
    # shipped XDF uses it; it is refused here rather than mis-compared.
    if model.base_subtract or model.base_offset != expected_base:
        if profile.xdf_addresses_cal_relative:
            expectation = (
                f"The {profile.name} profile was authored against a definition "
                f"file that numbers tables from the start of the calibration "
                f"block, which declares BASEOFFSET 0."
            )
        else:
            expectation = (
                f"The {profile.name} profile was authored against a definition "
                f"file that numbers tables from the start of the whole bin, "
                f"which declares BASEOFFSET {expected_base:#x}."
            )
        return _blocked(
            bin_path=bin_path, xdf_path=xdf_path,
            summary=(
                f"This XDF's tables match the {profile.name} profile, but it "
                "does not count addresses from where that profile expects."
            ),
            reasons=(
                expectation,
                (
                    "This file declares BASEOFFSET "
                    f"{model.base_offset:#x}"
                    + (" with subtract=1" if model.base_subtract else "")
                    + "."
                ),
                "Every value read through it would come from the wrong address, "
                "and every write would land there too — outside the region the "
                "checksums protect, so the result would build and flash without "
                "complaint.",
                "Use the definition file this profile names, or declare the new "
                "convention on the profile after confirming what its addresses "
                "are relative to.",
            ),
            bin_size=bin_size, bin_sha256=bin_hash, xdf_sha256=xdf_hash,
            region_start=region_start, region_size=region_size,
            profile_name=profile.name,
            advanced={
                "deftitle": model.deftitle,
                # The profile *did* resolve; ``profile_matched`` stays False
                # because nothing here may be edited as that car. Both facts
                # matter to a caller, so the second one is stated rather than
                # inferred from a name appearing beside a False flag.
                "profile_resolved": True,
                "xdf_base_offset": f"{model.base_offset:#x}",
                "xdf_base_subtract": model.base_subtract,
                "expected_xdf_base_offset": f"{expected_base:#x}",
                "profile_cal_file_offset": f"{structure.cal_file_offset:#x}",
                "xdf_addresses_cal_relative": profile.xdf_addresses_cal_relative,
            },
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
            profile_name=profile.name, checksums=checksums,
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
            profile_name=profile.name, checksums=checksums,
        )

    # -- optional switch-patch detection ------------------------------------ #
    switch_present: Optional[bool] = None
    switch_advanced: dict = {}
    if switch_patch_xdf is not None:
        switch_present, switch_advanced = _detect_switch_patch(
            bin_path, Path(switch_patch_xdf), profile
        )

    # -- verdict ------------------------------------------------------------ #
    if stale_states:
        names = ", ".join(c.name for c in stale_states)
        status = READY_STALE_CHECKSUM
        summary = (f"Ready to edit — recognised {profile.name} bin. Note: {names} is "
                   "stale and will be corrected automatically at build time.")
        reasons = (
            f"This is the {profile.name} calibration and every profile table "
            "resolved by symbol and shape.",
            f"{names} does not currently match — normal for an already-edited bin; "
            "the build step recomputes it before the bin is shareable.",
        )
    else:
        status = READY
        summary = (f"Ready to edit — recognised {profile.name} bin with valid "
                   "checksums.")
        reasons = (
            f"This is the {profile.name} calibration and every profile table "
            "resolved by symbol and shape.",
            "Both embedded checksums (CAL_CRC, ECM3) verify clean.",
        )

    advanced = {
        "profile": profile.name,
        "deftitle": model.deftitle,
        "region": f"[{region_start:#x}, {region_end:#x})",
        "full_bin": bin_size == structure.full_bin_size,
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
        profile_name=profile.name, profile_matched=True, writable=True,
        checksums=checksums,
        switch_patch_present=switch_present,
        advanced=advanced,
    )
