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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Callable, Iterable, Iterator, Mapping, Optional, Sequence, Union,
)

import numpy as np

from .. import btp
from ..binimage import BinImage
from ..calfile import CalFile, structure_of
from ..checksum import StructureSpec
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

__all__ = [
    "BASE_SPACE", "PatchSpec", "PostCheck", "TableSpace", "Tune", "TuneError",
]

#: Name of the space a tune's primary (OEM) XDF occupies.
BASE_SPACE = "base"


class TuneError(SimosCalError):
    """A tune could not be opened or edited as declared."""


@dataclass(frozen=True)
class PostCheck:
    """A gate that can only be answered by the finished file.

    ``run`` takes the saved bin's path and returns ``(passed, detail)``. Some
    verifications — does the switch patch still load and decode? — are about
    the artifact rather than any single table, and staging cannot answer them.
    """

    name: str
    run: Callable[[Path], tuple[bool, str]]
    description: str = ""
    recovery_key: str = ""
    recovery_params: Mapping[str, object] = field(default_factory=dict)


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
        #: Set by :meth:`apply_basics_sop`; ``build()`` runs its coherence rules.
        self.recipe_report = None
        #: Gates that can only be evaluated on the finished file — patch
        #: sanity, for one. Domains register them; ``build()`` runs them after
        #: saving and treats a failure as a failed build.
        self.post_checks: list[PostCheck] = []
        self._domains: dict[str, object] = {}
        # Lazily-built read-only decoders over ``_source_snapshot`` — see
        # :meth:`source_space`. Built at most once per space, because the
        # snapshot is captured here and never reassigned.
        self._source_image: Optional[BinImage] = None
        self._source_spaces: dict[str, Optional[CalFile]] = {}
        # The shared buffer before any write — the patched stock the build
        # starts from. Captured here (construction precedes every write) so the
        # audit can tell a legitimate restore-to-stock (candidate byte equals
        # source) from an undeclared change (candidate byte differs). See
        # CR-20260720-02 and ``audit.restore_to_source_allowance``.
        try:
            self._source_snapshot: bytes = (
                self.space(BASE_SPACE).cal.binimage.to_bytes()
            )
        except (TuneError, KeyError, AttributeError):  # pragma: no cover
            self._source_snapshot = b""
        #: The snapshot the cached decoders were built over, by identity. The
        #: snapshot is write-once in normal use; keying on it means a caller
        #: that replaces it (a test blanking the ghost, say) gets decoders that
        #: agree with the new value rather than stale ones that outlived it.
        self._source_key: object = self._source_snapshot

    @property
    def source_snapshot(self) -> bytes:
        """The patched stock buffer the build started from, before any write."""
        return self._source_snapshot

    def source_space(self, name: str = BASE_SPACE) -> Optional["CalFile"]:
        """``name``'s tables as the *source* buffer held them — the stock ghost.

        A read-only :class:`~simoscal.CalFile` over :attr:`source_snapshot`,
        decoded through the same XDF model the live space uses, so a ghost value
        and a working value are the same quantity decoded the same way. Returns
        ``None`` when there is no snapshot — a recovered session, which replayed
        its journal onto the source bin rather than opening it fresh.

        **Built at most once per space, and that is the point.** The snapshot is
        the whole bin; wrapping it costs a full copy of it, because
        :class:`~simoscal.binimage.BinImage` owns its bytes. Building one per
        *table*, as reading a ghost used to, made listing a 70-table catalog
        cost 70 copies of a 4 MB buffer — and they were not even transient:
        ``CalFile._views`` and ``TableView._cal`` form a reference cycle, so
        refcounting never freed them and they piled up until a cyclic-GC pass
        happened to run. One decoder per space, held for the tune's life, is
        4 MB once and no cycle churn at all.

        Every space shares the live buffer (:func:`_open_shared_space` binds
        each extra XDF to the base space's image), so they share one ghost image
        too — each space differs only in the model reading it.
        """
        if self._source_key is not self._source_snapshot:
            self._source_key = self._source_snapshot
            self._source_image = None
            self._source_spaces.clear()
        if name in self._source_spaces:
            return self._source_spaces[name]

        result: Optional[CalFile] = None
        try:
            table_space = self.space(name)
            if self._source_snapshot:
                if self._source_image is None:
                    model = self.space(BASE_SPACE).cal.model
                    self._source_image = BinImage(
                        self._source_snapshot,
                        region_start=model.region_start,
                        region_size=model.region_size,
                    )
                result = CalFile(
                    table_space.cal.model,
                    self._source_image,
                    structure=table_space.cal.structure,
                )
        except Exception:  # noqa: BLE001 - a missing ghost must never break a read
            # Same policy as the caller in ``catalog``: a ghost is a nicety, and
            # losing an editing surface because its optional reference copy
            # would not open would be the worse failure.
            result = None
        self._source_spaces[name] = result
        return result

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
            discovered = structure_of(working_bin)
            _require_profile_calibration(profile, working_bin, discovered)
            base_cal = CalFile.open(
                str(xdf), str(working_bin), structure=discovered,
                base_offset=profile.xdf_base_offset,
                float_bug_symbols=profile.float_bug_symbols,
                stock_references=profile.stock_references,
            )
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

    def invalidate_views(self) -> None:
        """Drop every cached decode, so the next read comes off the buffer.

        A :class:`~simoscal.calfile.TableView` caches what it decoded, and two
        objects hold views of the same table — the ``CalFile``'s own cache and
        the :class:`~simoscal.tune.profile.ResolvedProfile`'s. Anything that
        moves bytes underneath them (an undo, a session recovery, a dry run
        rolling back) must clear **both**, or a stale decode outlives the bytes
        it came from and the caller is told a value the bin no longer holds.
        """
        for space in self.spaces.values():
            for name in space.tables.names():
                space.tables[name].view.invalidate()
            space.cal._views.clear()

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
            declared=frozenset(extent),
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

    # -- speculative edits ----------------------------------------------------- #
    @contextmanager
    def dry_run(self) -> Iterator["Tune"]:
        """Run edits for real inside the block, then undo every trace of them.

        The one mechanism behind every ``dry_run=`` keyword in the tune API.
        Edits inside the block take the ordinary path — the same guards, the
        same encode, the same journal entry, the same exception on a refusal —
        and on the way out the buffer, the edit ledger, the journal, the
        registered post-checks and the decode caches are all put back where
        they were. What the caller keeps is the *result object* the edit
        returned; what the session keeps is nothing.

        Running the real path and rewinding, rather than simulating it, is the
        point: a second validation implementation would be a second thing to
        keep in step with the guards, and the day it drifted the preview would
        say "this is safe" about an edit the real path refuses. There is
        nothing to drift here — it is the same code.

        Only the declared region is restored, because
        :meth:`~simoscal.BinImage.write` refuses to stage a byte outside it, so
        no edit can have moved one.

        Nesting is safe: an inner block rewinds to where it started, which is
        wherever the outer block had got to.
        """
        images: list[tuple] = []
        seen: set[int] = set()
        marks: list[tuple] = []
        for space in self.spaces.values():
            cal = space.cal
            marks.append((cal, cal.edit_mark()))
            image = cal.binimage
            if id(image) in seen:
                continue
            seen.add(id(image))
            images.append(
                (image, image.read(image.region_start, image.region_size))
            )

        journal_mark = self.journal.mark()
        checks_mark = len(self.post_checks)
        recipe_report = self.recipe_report
        try:
            yield self
        finally:
            for image, region in images:
                image.write(image.region_start, region)
            for cal, mark in marks:
                cal.rollback_edits(mark)
            self.journal.rollback_to(journal_mark)
            del self.post_checks[checks_mark:]
            self.recipe_report = recipe_report
            self.invalidate_views()

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

    @property
    def fueling(self):
        """Lambda setpoint grids, their shared axes, and the enrichment floors."""
        from .domains.fueling import Fueling

        return self._domain("fueling", Fueling)

    @property
    def switchpatch(self):
        """BinToolz switch-patch slots: per-slot boost caps and TC flags."""
        from .domains.switchpatch import SwitchPatch

        return self._domain("switchpatch", SwitchPatch)

    @property
    def ignition(self):
        """Base timing, addressed by ``(rpm, load)`` the way logs report knock."""
        from .domains.ignition import Ignition

        return self._domain("ignition", Ignition)

    def apply_basics_sop(self, *, space: str = BASE_SPACE, **kwargs):
        """Apply the whole ``ecu-tuning-basics`` SOP, journaled per table.

        The bulk pass every revision since R00 starts from. Its outcomes are
        folded into the journal with each changed byte attributed to the table
        that owns it, so the recipe's writes are audited like any other.
        """
        from .sop_bridge import apply_basics_sop as _apply

        return _apply(self, space=space, **kwargs)

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


def _require_profile_calibration(
    profile: Profile, bin_path: Path, discovered: StructureSpec
) -> None:
    """Refuse a bin that is not the calibration ``profile`` describes.

    :func:`~simoscal.preflight.preflight` is the front door and asks this first,
    but it is not the only door: :meth:`Tune.open` is a public entry point that a
    revision script, a demo, or a test calls directly, and a guard only one of
    two paths honours is not a guard (CR-20260828-01, CR-20260828-03). The
    reasoning behind each half is in
    :meth:`~simoscal.tune.profile.Profile.structure_mismatch` and in preflight's
    own gates; this is the same rule stated where the other entry point can
    trip over it.

    A profile with no structure only adds tables to another profile's space and
    could never identify a calibration on its own, so it declines to judge one
    rather than passing everything.
    """
    if profile.structure is None:
        return
    mismatch = profile.structure_mismatch(discovered)
    if mismatch is not None:
        raise TuneError(
            f"{bin_path.name} is not a {profile.name} calibration: {mismatch}. "
            "The bin and the profile (with its XDF) are from different cars, and "
            "every table would be written at the other car's addresses — outside "
            "the region this bin's checksums cover, so the result would build and "
            "verify clean and be wrong everywhere."
        )
    size = bin_path.stat().st_size
    if size != profile.structure.full_bin_size:
        raise TuneError(
            f"{bin_path.name} is {size:,} bytes, but a {profile.name} image "
            f"is {profile.structure.full_bin_size:,} "
            f"({profile.structure.full_bin_size:#x}). "
            "A partial image can still verify both checksums — they only cover the "
            "calibration block — so this refusal is the only thing between a "
            "truncated file and a build that calls it flash-ready."
        )


def _open_shared_space(
    name: str, profile: Profile, xdf: Path, base_cal: CalFile
) -> TableSpace:
    """Bind another XDF to the *same* byte buffer as ``base_cal``.

    Refuses if the two XDFs would not put the same address in the same place —
    sharing a buffer across differing address arithmetic writes the right value
    to the wrong place.

    What must agree is where each file's addresses *land*, not what each file's
    header says. The two are the same thing until a base profile declares a
    rebase, and then they are not: A05's base XDF is written against the
    extracted CAL block and declares ``BASEOFFSET 0``, while its switch-patch XDF
    is written against the whole bin and declares ``0x220000``. Both resolve to
    ``0x220000``, so they may share a buffer — and comparing the declared values
    would have refused the only pairing that car has.
    """
    model = parse_xdf(str(xdf))
    mismatch: list[str] = []
    if base_cal.model.base_subtract != model.base_subtract:
        mismatch.append(
            f"base_subtract: {base_cal.model.base_subtract!r} vs "
            f"{model.base_subtract!r}"
        )
    # This space gets no declaration of its own, so its declared offset *is* its
    # effective one; it has to match where the base space actually reads.
    if model.base_offset != base_cal.base_offset:
        mismatch.append(
            f"effective base offset: {base_cal.base_offset:#x} vs "
            f"{model.base_offset:#x}"
        )
    # Region, likewise on effective terms. The bytes this space may touch are the
    # base space's — it shares that buffer — so the requirement is that this XDF
    # agrees they are addressable at all, not that it declares the same window.
    # A05's base XDF declares [0x0, 0x7d000), CAL-scoped like its addresses and
    # too small to hold its own highest table, so equality here would compare two
    # statements in different coordinate systems.
    binimage = base_cal.binimage
    if (
        model.region_start > binimage.region_start
        or model.region_start + model.region_size < binimage.region_end
    ):
        mismatch.append(
            f"addressable region: the base space reads "
            f"[{binimage.region_start:#x}, {binimage.region_end:#x}) but this XDF "
            f"declares only [{model.region_start:#x}, "
            f"{model.region_start + model.region_size:#x})"
        )
    if mismatch:
        raise TuneError(
            f"cannot share one bin between the base XDF and {xdf.name}: "
            f"they disagree on {'; '.join(mismatch)}"
        )
    # Union rather than replace: this space is a second XDF over the *same* bin,
    # so a symbol the base profile flags is still flagged wherever it is reached
    # from. Narrowing the set here would quietly unguard a base table that the
    # patch XDF also defines.
    cal = CalFile(
        model, base_cal.binimage, structure=base_cal.structure,
        # The check above proves the two files *declare* the same base offset;
        # this makes them *use* the same one. They address one shared buffer, so
        # an override applied to the base and not to this space would put the
        # two XDFs' identical addresses in different places in the same bin.
        base_offset=base_cal.base_offset,
        float_bug_symbols=(base_cal.float_bug_symbols or frozenset())
        | profile.float_bug_symbols,
        stock_references={**base_cal.stock_references, **profile.stock_references},
    )
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
