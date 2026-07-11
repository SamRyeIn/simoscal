"""BinToolz ``.btp`` patch adapter — check / apply / remove (plan U2-U4).

A thin wrapper over BinToolz's Qt-free byte layer that adds ``simoscal``-grade
guarantees: fail-loud identity and file-size guards, a post-apply full-bin diff
confined to the patch's declared blocks, round-trip support, and an explicit
``CAL_CRC`` / ``ECM3`` checksum report (never assumed). Nothing here flashes and
nothing patches in place — the input bin is read-only and every apply/remove
writes a *new* output file.

**Wrap, don't port** (BinToolz license carries no derivation grant): the
authoritative ``.btp`` byte logic stays in ``BinToolz-main/source/library`` and is
imported at runtime through a guarded, narrowly-scoped ``sys.path`` shim. This
module re-implements only the thin orchestration it must own anyway — the check
state machine, the identity guards, and the post-verification — never BinToolz's
byte manipulation.

BinToolz's own ``Patch.py`` orchestration layer is **not** used: it imports PyQt6
at module top, references an undefined ``self``, and calls ``PatchFunctionCheck``
with a missing argument (GUI-coupled, not safely importable headless). Only the
lower layer is wrapped: ``library.BTP`` (``BTP.load`` / ``checkChecksum`` /
``checkBin`` / ``changeBin``), ``library.SimosBIN`` (``load`` / ``hardwareType`` /
``softwareCode``), and ``Return.ReturnType``.

The U1 investigation (see ``knowledge/bintoolz-btp-patching.md`` "U1 findings")
established the checksum contract this adapter reports: applying the switch patch
leaves ``CAL_CRC`` **stale** (the ``.btp`` carries no corrected CAL CRC — correct
it before flashing), leaves ``ECM3`` **clean**, and touches ASW/code blocks whose
own checksums are **outside** ``simoscal``'s scope (SimosTools/VW_Flash compute
those at full-flash time) — reported *not-verifiable*, never assumed clean.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence

from .model import SimosCalError
from . import checksum as _checksum
from .checksum import ChecksumReport

__all__ = [
    "BtpError",
    "BinToolzNotFound",
    "PatchIntegrityError",
    "PatchIdentityError",
    "PatchStateError",
    "PatchConfinementError",
    "READY_TO_ACCEPT",
    "PATCH_FOUND",
    "NOT_READY",
    "PatchBlock",
    "PatchInfo",
    "PatchCheckResult",
    "ChangeResult",
    "SanityResult",
    "check",
    "apply",
    "remove",
    "switch_patch_sanity",
    "format_change_report",
    "default_bintoolz_root",
    "default_switch_patch_xdf",
    "SWITCH_PATCH_CATEGORIES",
]

# --- readiness states (mirror BinToolz's check outcome, project string style) - #
READY_TO_ACCEPT = "READY_TO_ACCEPT"  # bin holds the patch's original bytes
PATCH_FOUND = "PATCH_FOUND"          # bin already holds the patch's modified bytes
NOT_READY = "NOT_READY"              # bin matches neither (drifted / partial)

# Categories that mark the on-the-fly map-switching tables (BinToolz S50 XDF).
SWITCH_PATCH_CATEGORIES = (
    "Map Slot 1",
    "Map Slot 2",
    "Map Slot 3",
    "Map Slot 4",
    "Map Slot 5",
    "Map Switching",
)


# --- exceptions --------------------------------------------------------------- #
class BtpError(SimosCalError):
    """Base for every BTP-adapter failure."""


class BinToolzNotFound(BtpError):
    """The BinToolz source tree or its expected API surface is missing (AE7)."""


class PatchIntegrityError(BtpError):
    """The ``.btp`` file failed BinToolz's version / CRC32 self-check."""


class PatchIdentityError(BtpError):
    """Patch and bin disagree on hardware type, software code, or file size (AE4)."""


class PatchStateError(BtpError):
    """The bin is not in the state the requested operation requires."""


class PatchConfinementError(BtpError):
    """Post-verify found a changed byte outside the patch's declared blocks (AE2)."""


# --- BinToolz loader (import shim + API-surface check) ------------------------ #
def default_bintoolz_root() -> Path:
    """``BinToolz-main`` beside the ``Code/`` repo root (``../BinToolz-main``)."""
    # __file__ = Code/simoscal/btp.py → parents[2] is the project root next to Code/.
    return Path(__file__).resolve().parents[2] / "BinToolz-main"


def default_switch_patch_xdf(bintoolz_root: Optional[Path] = None) -> Path:
    """BinToolz's authoritative switch-patch XDF for the SC8S50 / S50 bin (U1)."""
    root = Path(bintoolz_root) if bintoolz_root is not None else default_bintoolz_root()
    return root / "definitions" / "S50 Switch Patch.29.33.V2.xdf"


@dataclass(frozen=True)
class _BinToolz:
    """The wrapped BinToolz classes/constants, loaded through the shim."""

    BTP: type
    SimosBIN: type
    ReturnType: type
    PatchBlockType: type
    header_size: int
    block_size: int


# Required (module, attribute) pairs — a missing one is BinToolz API drift (AE7).
_REQUIRED_API = (
    ("library.BTP", ("BTP", "PatchBlockType", "BTP_HEADER_SIZE", "BTP_PATCH_BLOCK_SIZE")),
    ("library.SimosBIN", ("SimosBIN",)),
    ("Return", ("ReturnType",)),
)
_REQUIRED_METHODS = {
    "BTP": ("load", "checkChecksum", "checkBin", "changeBin"),
    "SimosBIN": ("load", "hardwareType", "softwareCode"),
}


@contextmanager
def _bintoolz_on_path(source: Path) -> Iterator[None]:
    """Temporarily prepend ``source`` to ``sys.path`` (BinToolz uses flat imports)."""
    added = str(source)
    sys.path.insert(0, added)
    try:
        yield
    finally:
        try:
            sys.path.remove(added)
        except ValueError:  # pragma: no cover - defensive
            pass


def _load_bintoolz(bintoolz_root: Optional[Path]) -> _BinToolz:
    """Locate + import the BinToolz byte layer, verifying its API surface (AE7).

    Raises :class:`BinToolzNotFound` with the expected path / missing symbol when
    the tree is absent or its API has drifted — fail loud, never guess.
    """
    root = Path(bintoolz_root) if bintoolz_root is not None else default_bintoolz_root()
    source = root / "source"
    if not source.is_dir():
        raise BinToolzNotFound(
            f"BinToolz source not found at {source} (set bintoolz_root or place the "
            f"'BinToolz-main/' tree beside Code/); the .btp adapter wraps it at runtime"
        )

    modules: dict[str, object] = {}
    with _bintoolz_on_path(source):
        for mod_name, attrs in _REQUIRED_API:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError as exc:
                raise BinToolzNotFound(
                    f"BinToolz module '{mod_name}' could not be imported from {source}: {exc}"
                ) from exc
            for attr in attrs:
                if not hasattr(mod, attr):
                    raise BinToolzNotFound(
                        f"BinToolz '{mod_name}' is missing expected symbol '{attr}' "
                        f"(API drift?) — refusing to guess"
                    )
            modules[mod_name] = mod

    btp_mod = modules["library.BTP"]
    simosbin_mod = modules["library.SimosBIN"]
    return_mod = modules["Return"]
    for cls_name, methods in _REQUIRED_METHODS.items():
        cls = getattr(btp_mod if cls_name == "BTP" else simosbin_mod, cls_name)
        for meth in methods:
            if not callable(getattr(cls, meth, None)):
                raise BinToolzNotFound(
                    f"BinToolz {cls_name}.{meth}() is missing or not callable (API drift?)"
                )

    return _BinToolz(
        BTP=btp_mod.BTP,
        SimosBIN=simosbin_mod.SimosBIN,
        ReturnType=return_mod.ReturnType,
        PatchBlockType=btp_mod.PatchBlockType,
        header_size=btp_mod.BTP_HEADER_SIZE,
        block_size=btp_mod.BTP_PATCH_BLOCK_SIZE,
    )


# --- models ------------------------------------------------------------------- #
@dataclass(frozen=True)
class PatchBlock:
    """One declared ``(offset, length)`` block of a ``.btp`` (bin positions)."""

    offset: int
    length: int

    @property
    def end(self) -> int:
        """Half-open end (``offset + length``)."""
        return self.offset + self.length


@dataclass(frozen=True)
class PatchInfo:
    """Parsed ``.btp`` header + declared blocks (identity of the patch)."""

    path: str
    version: str
    software_code: str
    block_count: int
    block_checksum: int
    file_size: int
    blocks: tuple[PatchBlock, ...]

    @property
    def declared_bytes(self) -> int:
        """Total bytes the patch declares (may exceed the count that actually change)."""
        return sum(b.length for b in self.blocks)


@dataclass(frozen=True)
class PatchCheckResult:
    """Read-only readiness verdict for a (bin, patch) pair (AE1)."""

    readiness: str
    patch: PatchInfo
    bin_path: str
    bin_hardware: str
    bin_software_code: str
    bin_size: int

    @property
    def ready_to_apply(self) -> bool:
        return self.readiness == READY_TO_ACCEPT

    @property
    def already_patched(self) -> bool:
        return self.readiness == PATCH_FOUND


@dataclass(frozen=True)
class ChangeResult:
    """Outcome of an ``apply`` / ``remove``, with post-verification evidence."""

    operation: str  # "apply" | "remove"
    patch: PatchInfo
    input_path: str
    out_path: str
    changed_bytes: int
    changed_in_cal: int
    confined: bool
    checksum_reports: tuple[ChecksumReport, ...]
    sanity: Optional["SanityResult"] = None

    @property
    def cal_crc(self) -> Optional[ChecksumReport]:
        return next((r for r in self.checksum_reports if r.name == "CAL_CRC"), None)

    @property
    def ecm3(self) -> Optional[ChecksumReport]:
        return next((r for r in self.checksum_reports if r.name == "ECM3"), None)


@dataclass(frozen=True)
class SanityResult:
    """Switch-patch XDF sanity load of a patched bin (AE6)."""

    xdf_path: str
    bin_path: str
    categories: tuple[str, ...]
    tables_resolved: int
    tables_decoded: int
    decode_errors: tuple[tuple[str, str], ...] = ()  # (uniqueid_hex, error)
    all_finite: bool = True
    differ_from_stock: Optional[int] = None

    @property
    def plausible(self) -> bool:
        """Every slot/switch table resolved, decoded finitely, and — when a stock
        reference was supplied — the patched bin actually differs from it."""
        if self.tables_resolved == 0:
            return False
        if self.tables_decoded != self.tables_resolved:
            return False
        if self.decode_errors or not self.all_finite:
            return False
        if self.differ_from_stock is not None and self.differ_from_stock == 0:
            return False
        return True


# --- internal helpers --------------------------------------------------------- #
def _ret_ok(bt: _BinToolz, ret) -> bool:
    return ret == bt.ReturnType.OK


def _parse_patch_info(bt: _BinToolz, patch, path: Path) -> PatchInfo:
    """Read the header + declared block table off a loaded BinToolz ``BTP``."""
    header = patch.header
    blocks: list[PatchBlock] = []
    cur = patch.data[bt.header_size :]
    for _ in range(header.blockCount):
        bh = bt.PatchBlockType.fromBytes(cur[0:8])
        blocks.append(PatchBlock(offset=bh.offset, length=bh.length))
        # header(8) + original(length) + modified(length)
        cur = cur[bt.block_size + bh.length + bh.length :]
    return PatchInfo(
        path=str(path),
        version=header.version.split("\x00", 1)[0],
        software_code=header.softCode.split("\x00", 1)[0],
        block_count=header.blockCount,
        block_checksum=header.blockChecksum,
        file_size=header.fileSize,
        blocks=tuple(blocks),
    )


def _load_patch(bt: _BinToolz, patch_path: Path):
    """BTP.load with version + CRC32 self-check, mapped to loud exceptions."""
    if not patch_path.is_file():
        raise BtpError(f"patch file not found: {patch_path}")
    patch = bt.BTP()
    ret = patch.load(str(patch_path))
    if not _ret_ok(bt, ret):
        RT = bt.ReturnType
        if ret == RT.INVALID_VERSION:
            got = patch.header.version if patch.header is not None else "?"
            raise PatchIntegrityError(
                f"{patch_path.name}: not a recognized BinToolz patch "
                f"(version {got!r}); {ret.string()}"
            )
        if ret == RT.INVALID_CHECKSUM:
            stored = patch.header.blockChecksum if patch.header is not None else 0
            raise PatchIntegrityError(
                f"{patch_path.name}: patch CRC32 self-check failed — file corrupt "
                f"(header {stored:#010x} != computed {patch.checksum:#010x})"
            )
        raise PatchIntegrityError(f"{patch_path.name}: cannot load patch — {ret.string()}")
    return patch


def _load_bin(bt: _BinToolz, bin_path: Path):
    """SimosBIN.load, mapped to a loud exception on failure."""
    if not bin_path.is_file():
        raise BtpError(f"bin file not found: {bin_path}")
    binf = bt.SimosBIN()
    ret = binf.load(str(bin_path))
    if not _ret_ok(bt, ret):
        raise BtpError(f"{bin_path.name}: cannot load bin — {ret.string()}")
    return binf


def _guard_identity(bt: _BinToolz, patch, binf, info: PatchInfo, bin_path: Path) -> tuple[str, str]:
    """Hard-fail on hardware / software-code / file-size mismatch (AE4).

    Returns ``(hardware_key, bin_software_code)`` when all guards pass.
    """
    hw_key, hw_value = binf.hardwareType()
    if hw_value is None:
        raise PatchIdentityError(
            f"{bin_path.name}: unrecognized Simos hardware (bad box code / size "
            f"{len(binf.data)}); refusing to touch it"
        )
    # File size — BinToolz only logs this; we reject before any write.
    if info.file_size != len(binf.data):
        raise PatchIdentityError(
            f"{bin_path.name}: file-size mismatch — bin is {len(binf.data)} bytes, "
            f"patch expects {info.file_size}"
        )
    bin_sw = binf.softwareCode()
    if bin_sw is None:
        raise PatchIdentityError(
            f"{bin_path.name}: software code unreadable for hardware {hw_key!r}"
        )
    # Match BinToolz's rule: the bin's software code must be a prefix of the
    # patch header's software-code field.
    if info.software_code.find(bin_sw) != 0:
        raise PatchIdentityError(
            f"{bin_path.name}: software-code mismatch — bin {bin_sw!r}, "
            f"patch {info.software_code!r}"
        )
    return hw_key, bin_sw


def _cal_bounds(bt: _BinToolz, binf) -> tuple[int, int]:
    """``(start, end)`` half-open bin offsets of the CAL block (block 4)."""
    _, hw = binf.hardwareType()
    cal = hw.blocks[4]
    return cal.binPosition, cal.binPosition + cal.length


def _readiness(bt: _BinToolz, patch, binf) -> str:
    """The check state machine (bypasses BinToolz's broken ``Patch.py``).

    ``checkBin(remove=True)`` compares the bin against the patch's *modified*
    bytes → OK means already patched (PATCH_FOUND). ``checkBin(remove=False)``
    compares against the *original* bytes → OK means ready to accept. Neither →
    NOT_READY (drifted or partially patched).
    """
    if _ret_ok(bt, patch.checkBin(binf, remove=True)):
        return PATCH_FOUND
    if _ret_ok(bt, patch.checkBin(binf, remove=False)):
        return READY_TO_ACCEPT
    return NOT_READY


# --- public API: check -------------------------------------------------------- #
def check(
    bin_path,
    patch_path,
    *,
    bintoolz_root=None,
) -> PatchCheckResult:
    """Read-only readiness check (AE1): the input bin is never written.

    Loads the patch (version + CRC32 self-check), enforces the identity guards
    (hardware / software code / file size — AE4), and returns the readiness state
    (PATCH_FOUND / READY_TO_ACCEPT / NOT_READY) with the patch and bin identity.
    """
    bin_path = Path(bin_path)
    patch_path = Path(patch_path)
    bt = _load_bintoolz(bintoolz_root)
    patch = _load_patch(bt, patch_path)
    info = _parse_patch_info(bt, patch, patch_path)
    binf = _load_bin(bt, bin_path)
    hw_key, bin_sw = _guard_identity(bt, patch, binf, info, bin_path)
    return PatchCheckResult(
        readiness=_readiness(bt, patch, binf),
        patch=info,
        bin_path=str(bin_path),
        bin_hardware=hw_key,
        bin_software_code=bin_sw,
        bin_size=len(binf.data),
    )


# --- public API: apply / remove ----------------------------------------------- #
def _change(
    operation: str,
    required_state: str,
    remove: bool,
    bin_path: Path,
    patch_path: Path,
    out_path: Path,
    bintoolz_root,
) -> ChangeResult:
    bt = _load_bintoolz(bintoolz_root)
    patch = _load_patch(bt, patch_path)
    info = _parse_patch_info(bt, patch, patch_path)
    binf = _load_bin(bt, bin_path)
    _guard_identity(bt, patch, binf, info, bin_path)

    state = _readiness(bt, patch, binf)
    if state != required_state:
        raise PatchStateError(
            f"cannot {operation} {patch_path.name}: bin state is {state}, "
            f"requires {required_state} — nothing written"
        )

    input_bytes = bytes(binf.data)  # read-only snapshot for the post-diff
    cal_start, cal_end = _cal_bounds(bt, binf)

    # BinToolz mutates bin.data in place (on a copy of the bytes); the input file
    # is never touched. doCalBlock=True: include the CAL block (no ignore-data mode).
    ret = patch.changeBin(binf, remove=remove, doCalBlock=True)
    if not _ret_ok(bt, ret):
        raise BtpError(f"{operation} failed inside BinToolz changeBin: {ret.string()}")
    out_bytes = bytes(binf.data)

    # Post-verify: every changed byte must lie inside a declared block (AE2/AE3).
    if len(out_bytes) != len(input_bytes):
        raise PatchConfinementError(
            f"{operation} changed the bin length "
            f"({len(input_bytes)} → {len(out_bytes)}) — refusing to write"
        )
    changed = [i for i in range(len(out_bytes)) if out_bytes[i] != input_bytes[i]]
    declared = info.blocks
    outside = [i for i in changed if not any(b.offset <= i < b.end for b in declared)]
    if outside:
        raise PatchConfinementError(
            f"{operation} changed {len(outside)} byte(s) outside the patch's "
            f"declared blocks (first at {outside[0]:#x}) — refusing to write"
        )
    changed_in_cal = sum(1 for i in changed if cal_start <= i < cal_end)

    # Checksum report over the RESULT (AE5) — verify + report, never assume.
    reports = tuple(_checksum.verify(out_bytes))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(out_bytes)

    return ChangeResult(
        operation=operation,
        patch=info,
        input_path=str(bin_path),
        out_path=str(out_path),
        changed_bytes=len(changed),
        changed_in_cal=changed_in_cal,
        confined=True,
        checksum_reports=reports,
    )


def apply(
    bin_path,
    patch_path,
    out_path,
    *,
    bintoolz_root=None,
) -> ChangeResult:
    """Apply a ``.btp`` to a copy of ``bin_path``, writing ``out_path``.

    Requires the bin to be READY_TO_ACCEPT (refuses an already-patched or drifted
    bin, loud, nothing written). Post-verifies the confined diff (AE2) and reports
    ``CAL_CRC`` / ``ECM3`` state (AE5). The input file is never modified.
    """
    return _change(
        "apply", READY_TO_ACCEPT, False,
        Path(bin_path), Path(patch_path), Path(out_path), bintoolz_root,
    )


def remove(
    bin_path,
    patch_path,
    out_path,
    *,
    bintoolz_root=None,
) -> ChangeResult:
    """Remove a ``.btp`` from a copy of ``bin_path``, writing ``out_path``.

    Requires the bin to be PATCH_FOUND (refuses an unpatched bin, loud, nothing
    written). Applying then removing yields a byte-identical bin (AE3).
    """
    return _change(
        "remove", PATCH_FOUND, True,
        Path(bin_path), Path(patch_path), Path(out_path), bintoolz_root,
    )


# --- public API: switch-patch XDF sanity (U4 / AE6) --------------------------- #
def switch_patch_sanity(
    bin_path,
    *,
    xdf_path=None,
    stock_bin_path=None,
    categories: Sequence[str] = SWITCH_PATCH_CATEGORIES,
    bintoolz_root=None,
) -> SanityResult:
    """Load a patched bin against the switch-patch XDF and check the slot tables.

    "Plausible" (AE6): every Map Slot 1-5 / Map Switching table resolves against
    the authoritative XDF, decodes without a codec error, and has finite values;
    and — when ``stock_bin_path`` is given — the patched bin actually *differs*
    from stock in those regions (so the check can't false-pass on an unpatched
    bin). Defaults to BinToolz's ``S50 Switch Patch.29.33.V2.xdf`` (U1: the
    curated v1.005/v1.006 XDFs reuse a uniqueid across slots and do not load).
    """
    import numpy as np

    from .calfile import CalFile

    bin_path = Path(bin_path)
    xdf = Path(xdf_path) if xdf_path is not None else default_switch_patch_xdf(bintoolz_root)
    if not xdf.is_file():
        raise BtpError(f"switch-patch XDF not found: {xdf}")

    cal = CalFile.open(str(xdf), str(bin_path))
    stock_cal = (
        CalFile.open(str(xdf), str(stock_bin_path)) if stock_bin_path is not None else None
    )
    want = set(categories)

    resolved = decoded = 0
    errors: list[tuple[str, str]] = []
    all_finite = True
    differ: Optional[int] = 0 if stock_cal is not None else None

    for view in cal.unique_tables():
        vcats = {c.name for c in view.table.categories}
        if not (vcats & want):
            continue
        resolved += 1
        try:
            values = view.values
        except Exception as exc:  # noqa: BLE001 - a codec failure is a real finding
            errors.append((view.uniqueid_hex, f"{type(exc).__name__}: {exc}"))
            continue
        decoded += 1
        if not np.all(np.isfinite(np.asarray(values, dtype=float))):
            all_finite = False
        if stock_cal is not None:
            try:
                if not np.array_equal(stock_cal.get(view.uniqueid).values, values):
                    differ += 1  # type: ignore[operator]
            except Exception:  # noqa: BLE001 - table absent in stock view; skip
                pass

    return SanityResult(
        xdf_path=str(xdf),
        bin_path=str(bin_path),
        categories=tuple(categories),
        tables_resolved=resolved,
        tables_decoded=decoded,
        decode_errors=tuple(errors),
        all_finite=all_finite,
        differ_from_stock=differ,
    )


# --- human-readable report (review gate) -------------------------------------- #
def _checksum_line(report: Optional[ChecksumReport]) -> str:
    if report is None:
        return "not reported"
    if not report.can_verify:
        return f"not-verifiable ({report.detail})"
    return "STALE — correct before flashing" if report.is_stale else "clean"


def format_change_report(result: ChangeResult) -> str:
    """Markdown apply/remove report for the human review gate (SOP-report style)."""
    p = result.patch
    lines: list[str] = []
    lines.append(f"# BTP {result.operation} — {Path(p.path).name}")
    lines.append("")
    lines.append("**⚠ This does not flash.** Review, then flash externally "
                 "(SimosTools / VW_Flash) — a switch-patched bin needs a FULL flash.")
    lines.append("")
    lines.append("## Patch")
    lines.append(f"- file: `{Path(p.path).name}`")
    lines.append(f"- version: `{p.version}`  software code: `{p.software_code}`")
    lines.append(f"- blocks: {p.block_count}  declared bytes: {p.declared_bytes}  "
                 f"CRC32: `{p.block_checksum:#010x}`")
    lines.append("")
    lines.append("## Result")
    lines.append(f"- input:  `{result.input_path}` (unmodified)")
    lines.append(f"- output: `{result.out_path}`")
    lines.append(f"- bytes changed: {result.changed_bytes} "
                 f"({result.changed_in_cal} in CAL, {result.changed_bytes - result.changed_in_cal} in ASW/code)")
    lines.append(f"- confined to declared blocks: {'YES' if result.confined else 'NO'}")
    lines.append("")
    lines.append("## Checksums")
    lines.append(f"- `CAL_CRC`: {_checksum_line(result.cal_crc)}")
    lines.append(f"- `ECM3`: {_checksum_line(result.ecm3)}")
    lines.append("- ASW/code block checksums: **not-verifiable here** — outside "
                 "`simoscal`'s scope; SimosTools/VW_Flash compute them at full-flash time.")
    if result.operation == "apply" and result.cal_crc is not None and result.cal_crc.is_stale:
        lines.append("")
        lines.append("> Applying the patch left `CAL_CRC` stale (expected — the `.btp` "
                     "carries no corrected CAL CRC). Correct it before flashing "
                     "(`save(..., correct_checksums=True)` on the patched base, or the flasher).")
    if result.sanity is not None:
        s = result.sanity
        lines.append("")
        lines.append("## Switch-patch XDF sanity")
        lines.append(f"- XDF: `{Path(s.xdf_path).name}`")
        lines.append(f"- slot/switch tables: resolved {s.tables_resolved}, "
                     f"decoded {s.tables_decoded}, errors {len(s.decode_errors)}")
        if s.differ_from_stock is not None:
            lines.append(f"- differ from stock: {s.differ_from_stock} "
                         "(distinguishes a patched bin from stock)")
        lines.append(f"- plausible: {'YES' if s.plausible else 'NO'}")
    return "\n".join(lines) + "\n"
