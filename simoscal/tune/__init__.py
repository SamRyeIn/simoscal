"""simoscal.tune — the human-facing authoring layer for tune revisions.

Where the rest of ``simoscal`` is a substrate (parse an XDF, decode a table,
stage a minimal-diff byte write), this package is the layer a *person* writes a
revision in. A revision script declares its whole calibration in physical units
through domain-level calls, then hands the standard safety pipeline to one
:meth:`~simoscal.tune.project.Tune.build` call:

    tune = Tune.open(SC8S50, xdf=XDF_PATH, bin=BIN_PATH)
    tune.boost.put_ceiling_psi(30.0)
    result = tune.build("R13", out_root=OUT_ROOT, reference_bin=R12_BIN)

Two rules make that safe to hand to someone with no simoscal history:

1. **Every domain call is journaled.** A call does not just move bytes — it
   appends a typed :class:`~simoscal.tune.journal.EditEntry` recording the
   logical name, the resolved ``` `ID` — Description ```, before/after physical
   values, units, and any guard verdict. ``build()`` renders ``report.md`` from
   that journal and drives its raw-diff audit from the journaled tables' byte
   offsets, so an edit that is not journaled is an edit the audit will flag.
2. **Table references go through a profile.** Logical names resolve against an
   explicit per-XDF map (:mod:`simoscal.tune.profiles`), exactly — never
   fuzzily. A name that does not resolve fails loud, listing every miss with
   title suggestions, *before* any bin is opened for editing.

The safety model is inherited unchanged from the rest of the library: fail
loud, never silently clamp, never flash. See ``Code/README.md`` § Safety.
"""

from __future__ import annotations

from .audit import Allowance, RawDiffAudit, RawDiffError, raw_diff_audit
from .journal import EditEntry, Journal
from .pipeline import BuildFailed, BuildResult, GateOutcome, build, run_gates
from .build_service import (
    AuditModel,
    BuildReport,
    ChecksumModel,
    EditModel,
    GateResult,
    TableRef,
    build_report,
    build_revision,
)
from .profile import (
    Profile,
    ProfileResolutionError,
    ResolvedProfile,
    ResolvedTable,
    TableSpec,
    resolve,
)
from .profiles import PROFILES, SC8S50, SCGA05, SWITCH_PATCH_2933
from .project import BASE_SPACE, PatchSpec, TableSpace, Tune, TuneError
from .recovery import (
    RecoveryError,
    SessionHistory,
    load_session,
    restore_session,
    save_session,
    serialize_session,
)
from .catalog import AxisInfo, TableInfo, catalog, table_detail
from .editing import EditOp, EditRejected, EditResult, Selection, apply_op
from .boostcurve import (
    BoostCurveModel,
    SlotCurve,
    SlotCurveResult,
    boost_curve_model,
    slot_curve_result,
)

__all__ = [
    # authoring
    "Tune",
    "PatchSpec",
    "build",
    "BuildResult",
    "BuildFailed",
    # renderer-independent build service
    "run_gates",
    "GateOutcome",
    "build_revision",
    "build_report",
    "BuildReport",
    "GateResult",
    "ChecksumModel",
    "AuditModel",
    "EditModel",
    "TableRef",
    # profiles
    "PROFILES",
    "SC8S50",
    "SCGA05",
    "SWITCH_PATCH_2933",
    "Profile",
    "TableSpec",
    "resolve",
    "ResolvedProfile",
    "ResolvedTable",
    "ProfileResolutionError",
    # journal + audit
    "Journal",
    "EditEntry",
    "Allowance",
    "RawDiffAudit",
    "raw_diff_audit",
    # plumbing
    "BASE_SPACE",
    "TableSpace",
    "TuneError",
    "RawDiffError",
    # session recovery
    "serialize_session",
    "restore_session",
    "save_session",
    "load_session",
    "SessionHistory",
    "RecoveryError",
    # read-only catalog
    "catalog",
    "table_detail",
    "TableInfo",
    "AxisInfo",
    # generic edit ops
    "apply_op",
    "EditOp",
    "Selection",
    "EditResult",
    "EditRejected",
    # boost-curve model
    "boost_curve_model",
    "BoostCurveModel",
    "SlotCurve",
    "slot_curve_result",
    "SlotCurveResult",
]
