"""CalFile: the façade tying a parsed XDF to a bin, plus :class:`TableView`.

:class:`CalFile` binds an :class:`~simoscal.xdf.XdfModel` (metadata) to a
:class:`~simoscal.binimage.BinImage` (bytes) and exposes the query surface the
rest of the pipeline uses: :meth:`get`, :meth:`search`, :meth:`categories`,
:meth:`unique_tables`.

Lookups return a :class:`TableView` — a thin binding of a metadata
:class:`~simoscal.model.Table` to *this* CalFile. The view is where the bin
actually gets read: :attr:`TableView.values` triggers a lazy, cached decode.
Keeping the bind here (not on ``Table``) preserves the U1 rule that ``model.py``
carries no bin bytes and does no I/O. The U4 writer will add ``set``/``save`` to
this same view.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Mapping, Optional, Union

import numpy as np

from . import checksum, safety, writer
from .binimage import BinImage
from .checksum import ChecksumReport, StaleChecksumWarning, StructureSpec
from .codec import CodecError, decode_physical, decode_raw
from .model import Axis, FloatBugPolicyUnset, Table
from .xdf import XdfModel, parse_xdf

__all__ = ["CalFile", "TableView"]


class TableView:
    """A :class:`~simoscal.model.Table` bound to a :class:`CalFile`'s bin.

    Proxies the table's metadata (``uniqueid``, ``symbol``, ``title``,
    ``shape``, ``units``) and decodes its bytes on demand. ``values`` (physical)
    and ``raw`` are decoded lazily and cached; the underlying metadata is on
    :attr:`table`.
    """

    def __init__(self, table: Table, calfile: "CalFile") -> None:
        self.table = table
        self._cal = calfile
        self._values: Optional[np.ndarray] = None
        self._raw: Optional[np.ndarray] = None

    # -- metadata proxies ---------------------------------------------------- #
    @property
    def uniqueid(self) -> int:
        return self.table.uniqueid

    @property
    def uniqueid_hex(self) -> str:
        return self.table.uniqueid_hex

    @property
    def symbol(self) -> Optional[str]:
        return self.table.symbol

    @property
    def title(self) -> Optional[str]:
        return self.table.title

    @property
    def shape(self) -> Optional[tuple[int, int]]:
        return self.table.shape

    @property
    def units(self) -> Optional[str]:
        return self.table.z.units if self.table.z is not None else None

    # -- decoded values ------------------------------------------------------ #
    @property
    def values(self) -> np.ndarray:
        """The table's cell values in physical units, ``(rows, cols)``, cached.

        Linear scaling is applied (``phys = m·X + b``); a non-linear/absent
        scaling falls back to raw-as-float. Decoded once, then cached.
        """
        if self._values is None:
            self._values = self._cal._decode_physical(self.table.z)
        return self._values

    @property
    def raw(self) -> np.ndarray:
        """The table's raw (pre-scaling) integer/float cell values, cached."""
        if self._raw is None:
            self._raw = self._cal._decode_raw(self.table.z)
        return self._raw

    def axis_values(self, which: str) -> Optional[np.ndarray]:
        """Physical breakpoints for the ``'x'`` or ``'y'`` axis.

        Returns a decoded array when that axis has embedded data, or ``None``
        when it is label-only (many x/y axes carry static labels, not bytes).
        """
        if which not in ("x", "y"):
            raise ValueError(f"axis must be 'x' or 'y', got {which!r}")
        axis = getattr(self.table, which)
        if axis is None or axis.embedded is None:
            return None
        return self._cal._decode_physical(axis)

    def invalidate(self) -> None:
        """Drop cached decodes (used after an edit invalidates them)."""
        self._values = None
        self._raw = None

    # -- edits (physical units) ---------------------------------------------- #
    def _float_bug_symbols(self) -> frozenset[str]:
        """This car's float-bug flagged-list, or refuse the write without one.

        The guard's question — "is *this* table one whose declared maximum is an
        editor artifact?" — is per-car, and a CalFile opened with no profile has
        no answer. Refusing here rather than defaulting to "none flagged" is the
        point: a silently unguarded write to a limiter ceiling is the failure
        mode the guard exists for.
        """
        symbols = self._cal.float_bug_symbols
        if symbols is None:
            raise FloatBugPolicyUnset(
                f"table {self.uniqueid_hex} ({self.symbol or 'no symbol'}): this "
                "CalFile was opened without a float-bug policy, so the guard "
                "cannot be evaluated and the write is refused. Pass "
                "float_bug_symbols= when opening — e.g. SC8S50.float_bug_symbols "
                "from simoscal.tune.profiles.sc8s50, or frozenset() for a "
                "calibration that flags nothing."
            )
        return symbols

    def _z_writable(self) -> Axis:
        z = self.table.z
        if z is None or z.embedded is None:
            raise CodecError(
                f"table {self.uniqueid_hex} has no z-axis embedded data to write"
            )
        return z

    def set(self, values, *, override: bool = False) -> None:
        """Set the whole table from physical-unit values, minimal-diff.

        Inverts the linear scaling, range-checks, and stages only the table's
        bytes. Out-of-display-range values warn+allow (:class:`EditRangeWarning`);
        a flagged float-bug table over its upper limit raises
        :class:`FloatBugGuardError` even with ``override=True``. A non-linear
        table raises :class:`NonLinearEquationError` — use :meth:`set_raw`.
        """
        z = self._z_writable()
        emb = self.table.embedded
        arr = np.asarray(values, dtype=np.float64)
        if arr.shape != emb.shape:
            arr = arr.reshape(emb.shape)
        safety.check_display_range(
            self.table, arr,
            float_bug_symbols=self._float_bug_symbols(), override=override,
        )
        raw = writer.physical_to_raw(z, arr)
        off, length = writer.stage_full(
            z, self._cal.binimage,
            base_offset=self._cal.model.base_offset,
            base_subtract=self._cal.model.base_subtract,
            raw_values=raw,
        )
        self._cal._record_edit(off, length)
        self.invalidate()

    def set_cell(self, row: int, col: int, value, *, override: bool = False) -> None:
        """Set one cell from a physical-unit value, minimal-diff (one element)."""
        z = self._z_writable()
        safety.check_display_range(
            self.table, np.array([[value]], dtype=np.float64),
            float_bug_symbols=self._float_bug_symbols(),
            override=override, origin=(row, col),
        )
        raw = writer.physical_to_raw(z, np.array([[value]], dtype=np.float64))
        off, length = writer.stage_cell(
            z, self._cal.binimage,
            base_offset=self._cal.model.base_offset,
            base_subtract=self._cal.model.base_subtract,
            row=row, col=col, raw_value=np.asarray(raw).ravel()[0],
        )
        self._cal._record_edit(off, length)
        self.invalidate()

    # -- edits (raw units, for non-linear / low-level) ----------------------- #
    def set_raw(self, raw_values) -> None:
        """Set the whole table from raw element values (no scaling applied).

        The escape hatch for non-linear tables (and deliberate low-level edits).
        Range-checked against the element width; no display-range warning, since
        raw editing is an explicit choice.
        """
        z = self._z_writable()
        off, length = writer.stage_full(
            z, self._cal.binimage,
            base_offset=self._cal.model.base_offset,
            base_subtract=self._cal.model.base_subtract,
            raw_values=raw_values,
        )
        self._cal._record_edit(off, length)
        self.invalidate()

    def set_raw_cell(self, row: int, col: int, raw_value) -> None:
        """Set one cell from a raw element value (no scaling applied)."""
        z = self._z_writable()
        off, length = writer.stage_cell(
            z, self._cal.binimage,
            base_offset=self._cal.model.base_offset,
            base_subtract=self._cal.model.base_subtract,
            row=row, col=col, raw_value=raw_value,
        )
        self._cal._record_edit(off, length)
        self.invalidate()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<TableView {self.uniqueid_hex} {self.symbol!r} "
            f"shape={self.shape} units={self.units!r}>"
        )


def structure_of(bin_path: Union[str, Path]) -> StructureSpec:
    """The CAL structure of the bin at ``bin_path``, discovered from its bytes.

    A convenience for callers that hold a path and no profile. It is discovery,
    not assumption: :func:`~simoscal.checksum.discover_structure` accepts a
    layout only when that bin's stored ECM3 value recomputes exactly, and raises
    :class:`~simoscal.checksum.StructureNotFound` otherwise.
    """
    return checksum.discover_structure(Path(bin_path).read_bytes())


class CalFile:
    """A parsed XDF bound to a bin: query tables, read them in physical units."""

    def __init__(
        self,
        model: XdfModel,
        binimage: BinImage,
        *,
        structure: StructureSpec,
        float_bug_symbols: Optional[frozenset[str]] = None,
        stock_references: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.model = model
        self.binimage = binimage
        #: Where this car's CAL block sits and how it is addressed. Every
        #: checksum call this object makes passes it explicitly.
        self.structure = structure
        #: Symbols whose declared display maximum is an editor artifact rather
        #: than an ECU limit, from the active profile. ``None`` means no profile
        #: was supplied: reads work, physical-unit writes refuse
        #: (:class:`~simoscal.model.FloatBugPolicyUnset`).
        self.float_bug_symbols = float_bug_symbols
        #: What stock reads on this car, keyed by the short id a guidance string
        #: names. Empty means guidance renders without any stock comparison.
        self.stock_references: Mapping[str, str] = dict(stock_references or {})
        # One cached view per uniqueid so repeated get() calls share the decode.
        self._views: dict[int, TableView] = {}
        # (offset, length) byte ranges staged by edits this session — consumed by
        # U5 to warn when an edit touched a checksummed range.
        self._edited_ranges: list[tuple[int, int]] = []

    @classmethod
    def open(
        cls,
        xdf_path: Union[str, Path],
        bin_path: Union[str, Path],
        *,
        structure: StructureSpec,
        float_bug_symbols: Optional[frozenset[str]] = None,
        stock_references: Optional[Mapping[str, str]] = None,
    ) -> "CalFile":
        """Parse ``xdf_path`` and load ``bin_path``, wiring the region from the XDF.

        The bin's addressable region (start + size) is taken from the XDF
        ``REGION`` header so out-of-region reads fail loud.

        ``structure`` says where this car's CAL block sits and how it is
        addressed; it carries no default because "the SC8S50 one" silently
        standing in for another car is exactly the failure this argument exists
        to prevent. Obtain one from a profile, or from
        :func:`~simoscal.checksum.discover_structure` on the bin's bytes.

        ``float_bug_symbols`` and ``stock_references`` are the other two per-car
        facts a profile supplies. Both may be omitted for a read-only open; a
        physical-unit write through a CalFile with no ``float_bug_symbols``
        raises :class:`~simoscal.model.FloatBugPolicyUnset` rather than skipping
        the guard.
        """
        model = parse_xdf(str(xdf_path))
        binimage = BinImage.from_path(
            bin_path,
            region_start=model.region_start,
            region_size=model.region_size,
        )
        return cls(
            model, binimage, structure=structure,
            float_bug_symbols=float_bug_symbols,
            stock_references=stock_references,
        )

    # -- internal decode helpers (used by TableView) ------------------------- #
    def _decode_physical(self, axis: Axis) -> np.ndarray:
        return decode_physical(
            axis,
            self.binimage,
            base_offset=self.model.base_offset,
            base_subtract=self.model.base_subtract,
        )

    def _decode_raw(self, axis: Axis) -> np.ndarray:
        return decode_raw(
            axis,
            self.binimage,
            base_offset=self.model.base_offset,
            base_subtract=self.model.base_subtract,
        )

    def _view_for(self, table: Table) -> TableView:
        view = self._views.get(table.uniqueid)
        if view is None:
            view = TableView(table, self)
            self._views[table.uniqueid] = view
        return view

    def _record_edit(self, offset: int, length: int) -> None:
        self._edited_ranges.append((offset, length))

    @property
    def edited(self) -> bool:
        """Whether any edit has been staged into the bin this session."""
        return bool(self._edited_ranges)

    @property
    def edited_ranges(self) -> list[tuple[int, int]]:
        """The ``(offset, length)`` byte ranges staged by edits, in order."""
        return list(self._edited_ranges)

    def verify_checksums(self) -> list[ChecksumReport]:
        """Verify the CAL block's embedded checksums against the current buffer.

        Returns a :class:`~simoscal.checksum.ChecksumReport` per checksum (CAL CRC
        + ECM3). No bytes are written — this is the verify-and-report path. An
        image that cannot be checked (e.g. CAL-only, no ASW1) yields reports with
        ``can_verify=False`` rather than raising.
        """
        return checksum.verify(self.binimage.to_bytes(), self.structure)

    def save(
        self,
        path: Union[str, Path],
        *,
        correct_checksums: bool = False,
        warn_stale: bool = True,
    ) -> list[ChecksumReport]:
        """Write the current bin buffer (original bytes + staged edits) to ``path``.

        Minimal-diff by construction: only edited byte ranges differ from the
        original. Returns the checksum reports for the saved buffer.

        Checksums follow the plan's *verify + report, never silently rewrite*
        rule:

        * Default (``correct_checksums=False``): the bin is written **as-is**. If
          an edit this session touched a checksummed range and left it stale, a
          :class:`~simoscal.checksum.StaleChecksumWarning` is emitted (unless
          ``warn_stale=False``) — the bin must be corrected before flashing.
        * ``correct_checksums=True``: the CAL CRC and ECM3 checksums are corrected
          in place (ECM3 first, since its stored value sits inside the CRC's
          coverage) before writing, so the saved bin verifies clean.
        """
        if correct_checksums:
            # Apply only the stored-checksum bytes so the buffer stays minimal-diff.
            for off, patch in checksum.correction_patches(
                self.binimage.to_bytes(), self.structure
            ):
                self.binimage.write(off, patch)

        self.binimage.save(path)
        reports = checksum.verify(self.binimage.to_bytes(), self.structure)

        if warn_stale and not correct_checksums:
            for report in reports:
                if report.can_verify and report.is_stale:
                    touched = checksum.ranges_overlap(
                        self._edited_ranges, report.covered
                    )
                    scope = (
                        "an edit this session touched its range"
                        if touched
                        else "checksum does not match the calibration"
                    )
                    warnings.warn(
                        StaleChecksumWarning(
                            f"{report.name} is stale ({scope}); saved bin at "
                            f"{path} is NOT flash-ready — re-save with "
                            f"correct_checksums=True or fix via the flasher."
                        ),
                        stacklevel=2,
                    )
        return reports

    # -- queries ------------------------------------------------------------- #
    def get(self, key: Union[str, int]) -> TableView:
        """Return the single :class:`TableView` matching ``key``.

        Delegates to :meth:`XdfModel.get` — same semantics: a symbol/title
        matching more than one distinct table raises ``AmbiguousTableError``;
        a missing key raises ``KeyError``.
        """
        return self._view_for(self.model.get(key))

    def search(self, substring: str, *, case_sensitive: bool = False) -> list[TableView]:
        """Every table whose symbol or title contains ``substring``, as views."""
        return [
            self._view_for(t)
            for t in self.model.search(substring, case_sensitive=case_sensitive)
        ]

    def unique_tables(self) -> list[TableView]:
        """One :class:`TableView` per distinct uniqueid (the canonical sweep set).

        Wraps :meth:`XdfModel.unique_tables` so oracle sweeps and consumers that
        want every table exactly once don't double-count the 98 cross-listed
        calibrations.
        """
        return [self._view_for(t) for t in self.model.unique_tables()]

    def categories(self) -> list[str]:
        """Category names that contain at least one table, sorted."""
        return self.model.categories()

    def __len__(self) -> int:
        return len(self.model.by_id)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<CalFile tables={len(self.model.by_id)} "
            f"bin={self.binimage.size:#x}>"
        )
