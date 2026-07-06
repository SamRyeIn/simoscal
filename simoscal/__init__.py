"""simoscal — read, edit, and minimal-diff write Simos18 (SC8S50) XDF/BIN calibrations.

Phase 1 substrate: parse a TunerPro ``.xdf``, map its tables against a 4 MB
``.bin``, read/edit values in physical units, and write a minimal-diff,
flashable ``.bin``. Flashing and checksum *recomputation* are out of scope — the
library verifies-and-warns only.

Operating principle: *fail loud, change nothing silently, keep every modified
bin verifiable before it is flashed.*

This ``__init__`` re-exports the core data model (U1). Parser, codec, writer, and
checksum modules (U2–U5) attach in later units.
"""

from __future__ import annotations

from .model import (
    AmbiguousTableError,
    Axis,
    Category,
    EmbeddedData,
    FloatBugGuardError,
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
from .calfile import CalFile, TableView
from .checksum import (
    ChecksumReport,
    StaleChecksumWarning,
    correct as correct_checksums,
    correction_patches,
    crc32_simos,
    verify as verify_checksums,
    verify_cal_crc,
    verify_ecm3,
)
from .safety import (
    EditRangeWarning,
    FLOAT_BUG_SYMBOLS,
    RangeBreach,
    check_display_range,
    check_raw_fits,
    is_float_bug_table,
)
from . import writer

__version__ = "0.1.0"

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
    "FLOAT_BUG_SYMBOLS",
    "check_display_range",
    "check_raw_fits",
    "is_float_bug_table",
    "writer",
]
