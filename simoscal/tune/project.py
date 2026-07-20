"""The :class:`Tune` object — an open calibration a revision script edits.

A ``Tune`` binds one bin to one or more *table spaces*. A space is a profile
plus the XDF it was authored against: a patched Simos 18 bin has two of them,
because its base calibration lives in the OEM XDF and the patch-added slot
tables live in the patch author's XDF, and neither XDF knows about the other's
tables.

All spaces share **one** byte buffer. That is what lets a revision edit base
tables and patch tables in any order and save once, instead of the
save-reopen-save-reopen relay the R07–R12 scripts each had to hand-roll. The
buffer is shared safely because the two XDFs declare the same region and base
offset (asserted at open) and describe disjoint tables.

Every write goes through :meth:`Tune.write`, which is the only place in the
package that stages bytes. It measures which bytes actually moved, captures any
guard verdict or range warning, and journals the result — so "edit" and "record
the edit" are one operation that cannot come apart.
"""

from __future__ import annotations

import shutil
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence, Union

import numpy as np

from .. import btp
from ..calfile import CalFile
from ..model import FloatBugGuardError, RawRangeError, SimosCalError
from ..safety import EditRangeWarning
from ..xdf import parse_xdf
from . import audit
from .journal import (
    KIND_CHECK,
    KIND_PATCH,
    KIND_RAW,
    KIND_TABLE,
    VERDICT_APPLIED,
    VERDICT_BLOCKED,
    VERDICT_SKIPPED,
    VERDICT_UNCHANGED,
    EditEntry,
    Journal,
)
from .profile import Profile, ResolvedProfile, ResolvedTable
from .profile import resolve as resolve_profile

__all__ = ["BASE_SPACE", "PatchSpec", "TableSpace", "Tune", "TuneError"]

#: Name of the space a tune's primary (OEM) XDF occupies.
BASE_SPACE = "base"


class TuneError(SimosCalError):
    """A tune could not be opened or edited as declared."""


@dataclass(frozen=True)
class PatchSpec:
    """One ``.btp`` patch to apply before any table is read or written."""

    label: str
    path: Path
    description: str = ""


@dataclass
class TableSpace:
    """A profile bound to one XDF, sharing the tune's byte buffer."""

    name: str
    profile: Profile
    xdf: Path
    cal: CalFile
    tables: ResolvedProfile


class Tune:
    """An open calibration: bin + XDF(s) + profile(s) + the edit journal."""

    def __init__(
        self,
        *,
        source_bin: Path,
        spaces: Mapping[str, TableSpace],
        patch_results: Sequence[btp.ChangeResult] = (),
        journal: Optional[Journal] = None,
    ) -> None:
        self.source_bin = source_bin
        self.spaces: dict[str, TableSpace] = dict(spaces)
        self.patch_results = tuple(patch_results)
        self.journal = journal if journal is not None else Journal()
        self._domains: dict[str, object] = {}

    # -- construction -------------------------------------------------------- #
    @classmethod
    def open(
        cls,
        profile: Profile,
        *,
        xdf: Union[str, Path],
        bin: Union[str, Path],
        patches: Iterable[PatchSpec] = (),
        extra_spaces: Mapping[str, tuple[Profile, Union[str, Path]]] = (),
    ) -> "Tune":
        """Open ``bin`` under ``profile``/``xdf``, applying ``patches`` first.

        Resolution happens here, before a single byte can be written, so an XDF
        that is missing a mapped table fails with the full list of gaps and an
        untouched bin (AE3).

        ``patches`` are applied in the order given, each checked
        READY_TO_ACCEPT and verified confined — never forced. They run first
        because patch-added tables do not exist in the bin until they do.
        ``extra_spaces`` maps a space name to ``(profile, xdf)`` for those
        patch-added tables.
        """
        source_bin = Path(bin)
        patch_specs = tuple(patches)
        working_bin, results = _apply_patches(source_bin, patch_specs)

        try:
            base_cal = CalFile.open(str(xdf), str(working_bin))
            base_tables = resolve_profile(
                profile, base_cal, xdf_label=str(xdf)
            )
            spaces = {
                BASE_SPACE: TableSpace(
                    name=BASE_SPACE, profile=profile, xdf=Path(xdf),
                    cal=base_cal, tables=base_tables,
                )
            }
            for name, (extra_profile, extra_xdf) in dict(extra_spaces).items():
                spaces[name] = _open_shared_space(
                    name, extra_profile, Path(extra_xdf), base_cal
                )
        finally:
            if working_bin != source_bin:
                shutil.rmtree(working_bin.parent, ignore_errors=True)

        tune = cls(source_bin=source_bin, spaces=spaces, patch_results=results)
        for spec, result in zip(patch_specs, results):
            tune.journal.record(EditEntry(
                space=BASE_SPACE, name=spec.label, label=f"`{spec.label}` — "
                f"{spec.description or 'BinToolz patch'}",
                key=spec.label, kind=KIND_PATCH, verdict=VERDICT_APPLIED,
                intent=spec.description,
                detail=(f"{result.changed_bytes} byte(s) changed "
                        f"({result.changed_in_cal} in CAL), confined="
                        f"{result.confined}"),
            ))
        return tune

    # -- reading -------------------------------------------------------------- #
    def space(self, name: str = BASE_SPACE) -> TableSpace:
        try:
            return self.spaces[name]
        except KeyError:
            raise TuneError(
                f"no table space {name!r}; this tune has: "
                f"{', '.join(sorted(self.spaces))}"
            ) from None

    def table(self, name: str, *, space: str = BASE_SPACE) -> ResolvedTable:
        """The resolved table behind a logical name."""
        return self.space(space).tables[name]

    def values(self, name: str, *, space: str = BASE_SPACE) -> np.ndarray:
        """A copy of the table's current values, in physical units."""
        return np.array(self.table(name, space=space).view.values, dtype=np.float64)

    def axis(
        self, name: str, which: str = "x", *, space: str = BASE_SPACE
    ) -> Optional[np.ndarray]:
        """The table's own ``x``/``y`` breakpoints, or ``None`` if label-only."""
        axis = self.table(name, space=space).view.axis_values(which)
        return None if axis is None else np.asarray(axis, dtype=np.float64).ravel()

    # -- writing (the one place bytes are staged) ----------------------------- #
    def write(
        self,
        name: str,
        values,
        *,
        intent: str,
        space: str = BASE_SPACE,
        kind: str = KIND_TABLE,
        raw: bool = False,
        detail: str = "",
    ) -> EditEntry:
        """Write a table in physical units (or raw elements) and journal it.

        Returns the :class:`EditEntry` rather than nothing, so a domain module
        can enrich its ``detail`` — but the entry is already in the journal
        either way. There is no path that stages bytes without recording them.

        Never silently clamps: a guard rejection is journaled as ``blocked``
        and leaves the table byte-identical, and ``build()`` refuses to finish
        while any blocked entry is present.
        """
        resolved = self.table(name, space=space)
        view = resolved.view
        extent = sorted(audit.table_byte_offsets(view))
        image = self.space(space).cal.binimage

        before_phys = np.array(view.values, dtype=np.float64)
        before_bytes = _read_extent(image, extent)
        target = np.asarray(values, dtype=np.float64)
        if target.shape != before_phys.shape:
            target = target.reshape(before_phys.shape)

        verdict, warning, blocked_detail = VERDICT_APPLIED, "", ""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            try:
                if raw:
                    view.set_raw(target)
                else:
                    view.set(target)
            except (FloatBugGuardError, RawRangeError) as exc:
                verdict, blocked_detail = VERDICT_BLOCKED, str(exc)
        if verdict != VERDICT_BLOCKED:
            warning = "; ".join(
                str(w.message) for w in caught
                if issubclass(w.category, EditRangeWarning)
            )

        after_phys = np.array(view.values, dtype=np.float64)
        after_bytes = _read_extent(image, extent)
        moved = frozenset(
            offset for offset, (a, b) in zip(extent, zip(before_bytes, after_bytes))
            if a != b
        )
        if verdict == VERDICT_APPLIED and not moved:
            verdict = VERDICT_UNCHANGED

        return self.journal.record(EditEntry(
            space=space,
            name=name,
            label=resolved.label,
            key=resolved.spec.key,
            kind=KIND_RAW if raw else kind,
            verdict=verdict,
            units=resolved.units,
            intent=intent,
            before=before_phys,
            after=after_phys,
            offsets=moved,
            rows_changed=_rows_changed(before_phys, after_phys),
            detail=blocked_detail or detail,
            warning=warning,
        ))

    def write_cells(
        self,
        name: str,
        cells: Mapping[tuple[int, int], float],
        *,
        intent: str,
        space: str = BASE_SPACE,
        detail: str = "",
    ) -> EditEntry:
        """Write selected cells of a grid, leaving every other cell as it was."""
        values = self.values(name, space=space)
        for (row, col), value in cells.items():
            values[row, col] = value
        return self.write(
            name, values, intent=intent, space=space, kind=KIND_TABLE, detail=detail
        )

    def note(
        self,
        name: str,
        detail: str,
        *,
        intent: str = "",
        verdict: str = VERDICT_SKIPPED,
        kind: str = KIND_CHECK,
        space: str = BASE_SPACE,
    ) -> EditEntry:
        """Journal something that moved no bytes — a skip, or a check verdict.

        A deliberate non-change is part of the calibration's story, and a
        reviewer who cannot see it has to infer it from silence.
        """
        try:
            label = self.table(name, space=space).label
            key: Union[str, int] = self.table(name, space=space).spec.key
        except (KeyError, TuneError):
            label, key = f"`{name}`", name
        return self.journal.record(EditEntry(
            space=space, name=name, label=label, key=key,
            kind=kind, verdict=verdict, intent=intent, detail=detail,
        ))

    # -- domain facades -------------------------------------------------------- #
    def _domain(self, name: str, factory):
        """Lazily build and cache a domain facade, so ``tune.boost`` is stable."""
        existing = self._domains.get(name)
        if existing is None:
            existing = self._domains[name] = factory(self)
        return existing

    @property
    def boost(self):
        """Boost setpoints and the caps that can defeat them."""
        from .domains.boost import Boost

        return self._domain("boost", Boost)

    @property
    def wastegate(self):
        """Wastegate position feedforward (both VVL maps, always together)."""
        from .domains.wastegate import Wastegate

        return self._domain("wastegate", Wastegate)

    @property
    def limits(self):
        """Limiter ceilings, including the kg/stk airmass cap."""
        from .domains.limits import Limits

        return self._domain("limits", Limits)

    # -- saving --------------------------------------------------------------- #
    def save(self, path: Union[str, Path], *, correct_checksums: bool = True) -> list:
        """Write the shared buffer out once, checksums corrected by default."""
        return self.space(BASE_SPACE).cal.save(
            path, correct_checksums=correct_checksums, warn_stale=not correct_checksums
        )

    def build(self, revision: str, **kwargs):
        """Run the standard verification pipeline. See :mod:`.pipeline`."""
        from .pipeline import build

        return build(self, revision, **kwargs)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<Tune bin={self.source_bin.name!r} "
            f"spaces={sorted(self.spaces)} edits={len(self.journal)}>"
        )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _read_extent(image, offsets: Sequence[int]) -> bytes:
    """The bytes at ``offsets`` (read as one span — table z-data is contiguous)."""
    if not offsets:
        return b""
    start, end = offsets[0], offsets[-1] + 1
    span = image.read(start, end - start)
    return bytes(span[o - start] for o in offsets)


def _rows_changed(before: np.ndarray, after: np.ndarray) -> tuple[int, ...]:
    if before.shape != after.shape or before.ndim != 2:
        return ()
    diff = ~np.isclose(before, after, rtol=0, atol=1e-12)
    return tuple(int(r) for r in np.flatnonzero(diff.any(axis=1)))


def _open_shared_space(
    name: str, profile: Profile, xdf: Path, base_cal: CalFile
) -> TableSpace:
    """Bind another XDF to the *same* byte buffer as ``base_cal``.

    Refuses if the two XDFs disagree about the addressable region or the base
    offset — sharing a buffer across differing address arithmetic would write
    the right value to the wrong place.
    """
    model = parse_xdf(str(xdf))
    base = base_cal.model
    mismatch = [
        f"{field}: {getattr(base, field)!r} vs {getattr(model, field)!r}"
        for field in ("region_start", "region_size", "base_offset", "base_subtract")
        if getattr(base, field) != getattr(model, field)
    ]
    if mismatch:
        raise TuneError(
            f"cannot share one bin between the base XDF and {xdf.name}: "
            f"they disagree on {'; '.join(mismatch)}"
        )
    cal = CalFile(model, base_cal.binimage)
    return TableSpace(
        name=name, profile=profile, xdf=xdf, cal=cal,
        tables=resolve_profile(profile, cal, xdf_label=str(xdf)),
    )


def _apply_patches(
    source: Path, patches: Sequence[PatchSpec]
) -> tuple[Path, tuple[btp.ChangeResult, ...]]:
    """Apply patches copy-on-write into a temp dir; the source bin is never touched.

    Each apply is gated on READY_TO_ACCEPT and on the result being confined to
    the patch's declared blocks. A patch that is already present, or a bin the
    patch does not recognise, stops the build — never a forced apply.
    """
    if not patches:
        return source, ()

    work_dir = Path(tempfile.mkdtemp(prefix="simoscal-patch-"))
    current, results = source, []
    try:
        for spec in patches:
            pre = btp.check(current, spec.path)
            if not pre.ready_to_apply:
                raise TuneError(
                    f"patch {spec.label!r}: bin state is {pre.readiness}, "
                    "which is not READY_TO_ACCEPT — refusing to apply"
                )
            out = work_dir / f"_staged_{len(results)}_{spec.path.stem}.bin"
            result = btp.apply(current, spec.path, out)
            if not result.confined:
                raise TuneError(
                    f"patch {spec.label!r}: changed bytes outside its declared "
                    "blocks — refusing to continue"
                )
            results.append(result)
            current = out
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise
    return current, tuple(results)
