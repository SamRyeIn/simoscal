"""BinImage: the raw ``.bin`` byte buffer, region-aware.

Loads the 4 MB Simos18 ``.bin`` into a mutable ``bytearray`` and exposes
**region-checked** slice reads. The XDF's ``REGION`` (start + size) is the
authority on what byte range is addressable; any read whose extent falls outside
it — or outside the physical file — raises :class:`RegionBoundsError` rather than
returning garbage or silently truncating.

The buffer is mutable because the U4 writer stages edits into it in place; U3
only reads. Unedited bytes are never touched, which is what makes the later
minimal-diff save (Decision 10) possible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from .model import RegionBoundsError

__all__ = ["BinImage"]


class BinImage:
    """A loaded ``.bin`` as a mutable byte buffer with region-bounds checking.

    ``region_start``/``region_size`` come from the XDF ``REGION`` header (via
    :meth:`CalFile.open`). When omitted they default to the whole physical file,
    so a ``BinImage`` is usable stand-alone in tests.
    """

    def __init__(
        self,
        data: Union[bytes, bytearray, memoryview],
        *,
        region_start: int = 0,
        region_size: Optional[int] = None,
    ) -> None:
        self._data = bytearray(data)
        self.region_start = region_start
        self.region_size = region_size if region_size is not None else len(self._data)

    @classmethod
    def from_path(
        cls,
        path: Union[str, Path],
        *,
        region_start: int = 0,
        region_size: Optional[int] = None,
    ) -> "BinImage":
        """Load a ``.bin`` file into a :class:`BinImage`."""
        raw = Path(path).read_bytes()
        return cls(raw, region_start=region_start, region_size=region_size)

    @property
    def size(self) -> int:
        """Physical length of the loaded buffer in bytes."""
        return len(self._data)

    @property
    def region_end(self) -> int:
        """One past the last addressable byte of the declared region."""
        return self.region_start + self.region_size

    # -- reads --------------------------------------------------------------- #
    def _check_bounds(self, offset: int, length: int) -> None:
        """Raise :class:`RegionBoundsError` if ``[offset, offset+length)`` is out."""
        if length < 0:
            raise RegionBoundsError(f"negative read length {length}")
        end = offset + length
        if offset < self.region_start or end > self.region_end:
            raise RegionBoundsError(
                f"read [{offset:#x}, {end:#x}) falls outside declared region "
                f"[{self.region_start:#x}, {self.region_end:#x})"
            )
        if end > len(self._data):
            raise RegionBoundsError(
                f"read [{offset:#x}, {end:#x}) exceeds file size {len(self._data):#x}"
            )

    def read(self, offset: int, length: int) -> bytes:
        """Return ``length`` bytes at ``offset``, region-checked.

        ``offset`` is an absolute file offset (the codec has already added the
        BASEOFFSET). Out-of-region or past-end reads raise
        :class:`RegionBoundsError` — the decode never reads bytes it shouldn't.
        """
        self._check_bounds(offset, length)
        return bytes(self._data[offset : offset + length])

    # -- writes -------------------------------------------------------------- #
    def write(self, offset: int, data: bytes) -> None:
        """Stage ``data`` at ``offset`` in place, region-checked.

        Only the ``len(data)`` bytes at ``offset`` are touched — this is the
        primitive the writer uses for minimal-diff edits (Decision 10). Out-of-region
        writes raise :class:`RegionBoundsError` rather than growing/corrupting the
        buffer.
        """
        self._check_bounds(offset, len(data))
        self._data[offset : offset + len(data)] = data

    def to_bytes(self) -> bytes:
        """A copy of the current buffer (original bytes plus any staged edits)."""
        return bytes(self._data)

    def save(self, path: Union[str, Path]) -> None:
        """Write the current buffer to ``path``."""
        Path(path).write_bytes(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<BinImage size={self.size:#x} "
            f"region=[{self.region_start:#x}, {self.region_end:#x})>"
        )
