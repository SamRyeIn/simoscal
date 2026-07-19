"""Account for every changed byte between two bins, or fail.

A build's last and strictest gate. Comparing a new revision's bin against the
previous revision byte for byte answers a question no table-level readback can:
*did anything change that we did not ask to change?* Stray bytes are how a
misresolved symbol, an aliased shared axis, or a stale buffer reaches an ECU.

The allowance set is not hand-written per revision — it is derived from the
journal, so an edit that was not journaled is an edit the audit flags. That
inversion is the point: forgetting to declare a change makes the build fail
loudly rather than quietly shipping it.

Generalized from the ``_byte_offsets`` / ``_raw_diff_audit`` helpers proven in
the R11 and R12 revision scripts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from .. import checksum
from ..calfile import TableView
from ..codec import file_offset_for
from ..model import SimosCalError

__all__ = [
    "Allowance",
    "RawDiffAudit",
    "RawDiffError",
    "checksum_storage_allowance",
    "patch_allowance",
    "raw_diff_audit",
    "table_byte_offsets",
]


class RawDiffError(SimosCalError):
    """Bytes changed that no declared allowance explains."""


@dataclass(frozen=True)
class Allowance:
    """A named set of byte offsets a build is permitted to have changed."""

    label: str
    offsets: frozenset[int]

    def __len__(self) -> int:
        return len(self.offsets)


def table_byte_offsets(
    view: TableView, *, rows: Optional[Sequence[int]] = None
) -> frozenset[int]:
    """Every file byte occupied by ``view``'s z-data (optionally only some rows).

    ``rows`` narrows the allowance to the rows an edit actually touched — the
    R11 case, where only ``IP_PUT_SP``'s full-load row was written and a change
    in any part-load row must still count as unexplained.
    """
    emb = view.table.z.embedded if view.table.z is not None else None
    if emb is None:
        raise ValueError(
            f"table {view.uniqueid_hex} has no embedded z-data — no byte extent"
        )
    start = file_offset_for(
        emb.address,
        view._cal.model.base_offset,
        view._cal.model.base_subtract,
    )
    width = emb.elem_bits // 8
    wanted = range(emb.rows) if rows is None else rows
    offsets: set[int] = set()
    for row in wanted:
        if not 0 <= row < emb.rows:
            raise ValueError(
                f"row {row} out of range for {view.uniqueid_hex} "
                f"({emb.rows} rows)"
            )
        for col in range(emb.cols):
            index = col * emb.rows + row if emb.column_major else row * emb.cols + col
            offsets.update(range(start + index * width, start + (index + 1) * width))
    return frozenset(offsets)


def checksum_storage_allowance(candidate: Union[str, Path, bytes]) -> Allowance:
    """The stored-checksum bytes, which are derived and so always allowed.

    CAL_CRC and ECM3 store values computed *over* the calibration; any real edit
    moves them. Allowing them is not a loophole — their correctness is asserted
    separately by the checksum verify gate, which is what makes them
    uninteresting here.
    """
    data = candidate if isinstance(candidate, bytes) else Path(candidate).read_bytes()
    offsets: set[int] = set()
    for _name, offset, length in checksum.stored_checksum_ranges(data):
        offsets.update(range(offset, offset + length))
    return Allowance("stored checksums (CAL_CRC, ECM3)", frozenset(offsets))


def patch_allowance(label: str, blocks: Iterable) -> Allowance:
    """Every byte a ``.btp`` patch declares, for a build that applies patches.

    Needed only when the reference bin is in a *different* patch state than the
    candidate (e.g. comparing a freshly patched revision against an unpatched
    stock bin). When both sides carry the same patches — the normal
    revision-to-revision case — those bytes are identical and never show up as
    changed at all.
    """
    offsets: set[int] = set()
    for block in blocks:
        offsets.update(range(block.offset, block.offset + block.length))
    return Allowance(f"patch {label}", frozenset(offsets))


@dataclass(frozen=True)
class RawDiffAudit:
    """The verdict: what changed, what explains it, what nothing explains."""

    reference: str
    candidate: str
    changed: int
    unexplained: tuple[int, ...] = ()
    #: allowance label → how many of its bytes actually changed
    attributed: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.unexplained

    def summary(self) -> str:
        if self.clean:
            return (
                f"{self.changed} changed byte(s), all attributed; unexplained = 0"
            )
        sample = ", ".join(hex(o) for o in self.unexplained[:12])
        return (
            f"{self.changed} changed byte(s); "
            f"{len(self.unexplained)} UNEXPLAINED: {sample}"
        )


def raw_diff_audit(
    reference: Union[str, Path],
    candidate: Union[str, Path],
    allowances: Sequence[Allowance],
) -> RawDiffAudit:
    """Diff two bins and attribute every changed byte to an allowance.

    Returns the verdict rather than raising, so a caller can put it in the
    report before deciding — the build pipeline treats a non-clean audit as a
    failed gate. A file-size mismatch *does* raise: two differently-sized bins
    are not two revisions of the same calibration.
    """
    ref_path, cand_path = Path(reference), Path(candidate)
    before, after = ref_path.read_bytes(), cand_path.read_bytes()
    if len(before) != len(after):
        raise RawDiffError(
            f"file-size mismatch: {ref_path.name} is {len(before)} bytes, "
            f"{cand_path.name} is {len(after)} — not two revisions of one bin"
        )

    changed = {i for i, (a, b) in enumerate(zip(before, after)) if a != b}
    attributed: dict[str, int] = {}
    remaining = set(changed)
    for allowance in allowances:
        hits = remaining & allowance.offsets
        if hits:
            attributed[allowance.label] = attributed.get(allowance.label, 0) + len(hits)
            remaining -= hits

    return RawDiffAudit(
        reference=str(ref_path),
        candidate=str(cand_path),
        changed=len(changed),
        unexplained=tuple(sorted(remaining)),
        attributed=attributed,
    )
