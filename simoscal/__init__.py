"""simoscal — read, edit, and minimal-diff write Simos18 (SC8S50) XDF/BIN calibrations.

Phase 1 substrate: parse a TunerPro ``.xdf``, map its tables against a 4 MB
``.bin``, read/edit values in physical units, and write a minimal-diff,
flashable ``.bin``. Flashing and checksum *recomputation* are out of scope — the
library verifies-and-warns only. Phase 2 adds read-only CSV/xlsx export
(``simoscal.export``) and Phase 3 adds read-only PNG visualization
(``simoscal.plot``) on top of this substrate.

Operating principle: *fail loud, change nothing silently, keep every modified
bin verifiable before it is flashed.*
"""

from __future__ import annotations

from .model import (
    AmbiguousTableError,
    Axis,
    Category,
    EmbeddedData,
    FloatBugGuardError,
    FloatBugPolicyUnset,
    NonLinearEquationError,
    RawRangeError,
    RegionBoundsError,
    ScalingEquation,
    Table,
)
from .xdf import Defaults, XdfModel, XdfParseError, parse_xdf
from .binimage import BinImage
from .codec import (
    CodecError,
    decode_physical,
    decode_raw,
    decode_table,
    file_offset_for,
    numpy_dtype_for,
)
from .calfile import CalFile, TableView, structure_of
from .render import RenderedTable, render_table
from .preflight import ChecksumState, Verdict, preflight
from .sop_recipe import (
    RecipeReport,
    SYMBOL_MAP,
    TableOutcome,
    apply_basics_sop,
    format_report,
    resolve_symbol_map,
)
from .checksum import (
    ChecksumNotLocatable,
    ChecksumReport,
    SC8S50_STRUCTURE,
    StaleChecksumWarning,
    StructureNotFound,
    StructureSpec,
    correct as correct_checksums,
    correction_patches,
    crc32_simos,
    discover_structure,
    verify_discovered,
    verify as verify_checksums,
    verify_cal_crc,
    verify_ecm3,
)
from .safety import (
    EditRangeWarning,
    RangeBreach,
    check_display_range,
    check_raw_fits,
    is_float_bug_table,
)
from . import writer
from . import btp
from .btp import (
    BtpError,
    BinToolzNotFound,
    PatchIdentityError,
    PatchIntegrityError,
    PatchStateError,
    PatchConfinementError,
    PatchCheckResult,
    ChangeResult,
    SanityResult,
    format_change_report,
)

__version__ = "0.1.0"

# -- lazy heavy-dependency symbols (PEP 562) --------------------------------- #
# The core library depends only on numpy so it stays importable on-device
# (Chaquopy/Android) with no matplotlib/openpyxl. CSV/xlsx export and PNG
# visualization live behind the ``export`` and ``plot`` extras and are resolved
# lazily on first attribute access. Touching one without its extra installed
# raises an actionable ImportError naming the extra, rather than failing eagerly
# at ``import simoscal``.
_LAZY_EXPORT = {"export_tables", "select_tables", "write_csv", "write_xlsx"}
_LAZY_PLOT = {
    "TableMismatchError",
    "compare_bins",
    "compare_tables",
    "plot_table",
    "plot_tables",
}


def __getattr__(name: str):  # noqa: N807 — module-level PEP 562 hook
    if name in _LAZY_EXPORT:
        try:
            from . import export as _mod
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                f"simoscal.{name} needs the optional 'export' dependencies "
                f"(openpyxl). Install them with: pip install 'simoscal[export]'"
            ) from exc
        return getattr(_mod, name)
    if name in _LAZY_PLOT:
        try:
            from . import plot as _mod
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise ImportError(
                f"simoscal.{name} needs the optional 'plot' dependencies "
                f"(matplotlib). Install them with: pip install 'simoscal[plot]'"
            ) from exc
        return getattr(_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "__version__",
    "ScalingEquation",
    "EmbeddedData",
    "Axis",
    "Category",
    "Table",
    "AmbiguousTableError",
    "NonLinearEquationError",
    "RegionBoundsError",
    "FloatBugGuardError",
    "FloatBugPolicyUnset",
    "RawRangeError",
    "Defaults",
    "XdfModel",
    "XdfParseError",
    "parse_xdf",
    "BinImage",
    "CodecError",
    "decode_physical",
    "decode_raw",
    "decode_table",
    "file_offset_for",
    "numpy_dtype_for",
    "CalFile",
    "TableView",
    "structure_of",
    "StructureSpec",
    "SC8S50_STRUCTURE",
    "discover_structure",
    "verify_discovered",
    "StructureNotFound",
    "ChecksumNotLocatable",
    "RenderedTable",
    "render_table",
    "ChecksumState",
    "Verdict",
    "preflight",
    "select_tables",
    "write_csv",
    "write_xlsx",
    "export_tables",
    "plot_table",
    "compare_tables",
    "plot_tables",
    "compare_bins",
    "TableMismatchError",
    "apply_basics_sop",
    "resolve_symbol_map",
    "RecipeReport",
    "TableOutcome",
    "format_report",
    "SYMBOL_MAP",
    "ChecksumReport",
    "StaleChecksumWarning",
    "verify_checksums",
    "verify_cal_crc",
    "verify_ecm3",
    "correct_checksums",
    "correction_patches",
    "crc32_simos",
    "EditRangeWarning",
    "RangeBreach",
    "check_display_range",
    "check_raw_fits",
    "is_float_bug_table",
    "writer",
    "btp",
    "BtpError",
    "BinToolzNotFound",
    "PatchIdentityError",
    "PatchIntegrityError",
    "PatchStateError",
    "PatchConfinementError",
    "PatchCheckResult",
    "ChangeResult",
    "SanityResult",
    "format_change_report",
]
