"""XDF parser: TunerPro ``.xdf`` XML → simoscal model, streaming and indexed.

Turns an ``.xdf`` into a populated :class:`XdfModel` — a set of :class:`Table`
objects plus lookup indexes — **without touching any bin**. Parsing is streamed
with :func:`xml.etree.ElementTree.iterparse`, materializing one ``XDFTABLE`` at a
time and clearing it, so the parser scales from the 5.8 MB ``V1.0`` file up to the
59 MB ``.ALL`` without loading the whole tree.

Key decode facts (see plan Decisions 5–7), grounded in ``SC8S50.V1.0.xdf``:

* ``BASEOFFSET offset="0x200000" subtract="0"`` — file offset = address + base.
* ``mmedtypeflags`` bits: ``0x01`` = signed, ``0x02`` = little-endian,
  ``0x04`` = column-major element order, ``0x10000`` = IEEE float. Decoded
  per-table, not from ``DEFAULTS``. (An earlier revision mis-assigned ``0x04``
  to *signed*, which decoded every ``0x6`` table as its transpose and with the
  wrong signedness — see code_review CR-20260706-21 / -22. Both were the same
  mismapping: ``0x04`` is column-major, and ``0x01`` — never set in ``V1.0`` —
  is the real sign bit, so every table here is unsigned.)
* ``CATEGORYMEM category="N"`` is **1-based**: it references header ``CATEGORY
  index = N-1`` (verified empirically against the Checksum/DTC/MIL tables).
* Every ``MATH`` equation in ``V1.0`` is linear; non-linear equations are flagged
  (``is_linear=False``) and continue, never crash the parse.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional, Union

from .model import (
    AmbiguousTableError,
    Axis,
    Category,
    EmbeddedData,
    ScalingEquation,
    SimosCalError,
    Table,
)

__all__ = [
    "XdfParseError",
    "Defaults",
    "XdfModel",
    "parse_xdf",
]

# mmedtypeflags bit meanings (TunerPro layout, plan Decision 6). Verified against
# a live TunerPro capture over SC8S50.V1.0.xdf / 5G0906259L__0002.bin: bit 0x04 is
# the column-major order flag, NOT signed; the sign bit is 0x01 (unset on every
# table in V1.0, so all are unsigned). See code_review CR-20260706-21 / -22.
_FLAG_SIGNED = 0x01
_FLAG_LITTLE_ENDIAN = 0x02
_FLAG_COLUMN_MAJOR = 0x04
_FLAG_FLOAT = 0x10000


class XdfParseError(SimosCalError):
    """Raised when an XDF element cannot be parsed into the model.

    The message names the offending table's ``uniqueid`` where known, so a
    malformed table is identifiable rather than an anonymous stack trace.
    """


def _axis_data_fingerprint(axis: Optional["Axis"]) -> tuple:
    """A hashable summary of an axis's *data-bearing* fields (not its labels).

    Used to prove that two ``XDFTABLE`` entries sharing a ``uniqueid`` really
    describe the same bytes + decode. Metadata (title, categories) is excluded.
    """
    if axis is None:
        return ()
    emb = axis.embedded
    emb_fp = (
        None
        if emb is None
        else (
            emb.address,
            emb.rows,
            emb.cols,
            emb.elem_bits,
            emb.major_stride_bits,
            emb.minor_stride_bits,
            emb.signed,
            emb.little_endian,
            emb.is_float,
            emb.column_major,
        )
    )
    sc = axis.scaling
    # Compare on the source expression (+ linear flag) to sidestep NaN != NaN
    # for any non-linear equation while still detecting a real decode change.
    sc_fp = None if sc is None else (sc.expression, sc.is_linear)
    return (emb_fp, sc_fp)


def _table_data_fingerprint(table: "Table") -> tuple:
    """Data-bearing fingerprint across a table's x/y/z axes."""
    return (
        _axis_data_fingerprint(table.x),
        _axis_data_fingerprint(table.y),
        _axis_data_fingerprint(table.z),
    )


def _parse_int(text: Optional[str], default: Optional[int] = None) -> Optional[int]:
    """Parse an XDF integer that may be hex (``0x…``) or decimal."""
    if text is None:
        return default
    text = text.strip()
    if text == "":
        return default
    return int(text, 16) if text.lower().startswith("0x") else int(text, 10)


def _parse_float(text: Optional[str]) -> Optional[float]:
    if text is None:
        return None
    text = text.strip()
    if text == "":
        return None
    return float(text)


@dataclass(frozen=True)
class Defaults:
    """The XDF ``<DEFAULTS>`` element — fallbacks when a table omits attributes."""

    datasizeinbits: int = 8
    sigdigits: int = 4
    outputtype: int = 1
    signed: bool = False
    lsbfirst: bool = True
    is_float: bool = False


class XdfModel:
    """Parsed XDF: tables + lookup indexes + header metadata. Holds no bin bytes.

    This is what a parser run produces and what :class:`CalFile` (U3) wraps to
    tie the tables to a bin. Query it by :meth:`get` (single match or
    :class:`AmbiguousTableError`), :meth:`search`, or the raw multimaps.
    """

    def __init__(
        self,
        *,
        base_offset: int,
        base_subtract: bool,
        region_start: int,
        region_size: int,
        defaults: Defaults,
        categories: dict[int, Category],
        tables: list[Table],
        deftitle: str = "",
    ) -> None:
        #: The XDF header's ``<deftitle>`` — what the file says it *is*, verbatim
        #: and untrusted. Never used to recognise a calibration (that is
        #: resolution's job, by symbol and shape); used only so a refusal can name
        #: the software in front of the user instead of only what it is not.
        self.deftitle = deftitle
        self.base_offset = base_offset
        self.base_subtract = base_subtract
        self.region_start = region_start
        self.region_size = region_size
        self.defaults = defaults
        # header index (0-based) -> Category
        self.category_by_index = categories
        # Every parsed XDFTABLE, faithful to the file (including the metadata-only
        # duplicates a2l2xdf emits — see duplicate_ids below).
        self.tables = tables

        # Indexes. Plan Decision 4 assumed uniqueid is globally unique; the real
        # V1.0 file breaks that (98 uniqueids appear twice, as the same
        # calibration cross-listed under different DTC/MIL titles + categories).
        # We treat a repeated uniqueid as the SAME logical table iff its
        # data-bearing fingerprint (address/shape/strides/typeflags/scaling) is
        # identical, and hard-fail otherwise. by_id keeps the first occurrence;
        # the symbol/title/category multimaps are deduped by uniqueid so a
        # cross-listed table is not falsely "ambiguous".
        self.by_id: dict[int, Table] = {}
        self.by_symbol: dict[str, list[Table]] = {}
        self.by_title: dict[str, list[Table]] = {}
        self.by_category: dict[str, list[Table]] = {}
        # uniqueid -> number of extra (duplicate) XDFTABLE entries seen.
        self.duplicate_ids: dict[int, int] = {}

        fingerprints: dict[int, tuple] = {}

        def _add_unique(index: dict[str, list[Table]], key: str, table: Table) -> None:
            bucket = index.setdefault(key, [])
            if any(t.uniqueid == table.uniqueid for t in bucket):
                return
            bucket.append(table)

        for t in tables:
            fp = _table_data_fingerprint(t)
            if t.uniqueid in self.by_id:
                if fingerprints[t.uniqueid] != fp:
                    raise XdfParseError(
                        f"uniqueid {t.uniqueid_hex} reused with DIFFERENT data "
                        "(address/shape/strides/typeflags/scaling conflict) — "
                        "cannot map this uniqueid to a single location safely."
                    )
                self.duplicate_ids[t.uniqueid] = self.duplicate_ids.get(t.uniqueid, 0) + 1
            else:
                self.by_id[t.uniqueid] = t
                fingerprints[t.uniqueid] = fp
            if t.symbol:
                _add_unique(self.by_symbol, t.symbol, t)
            if t.title:
                _add_unique(self.by_title, t.title, t)
            for cat in t.categories:
                _add_unique(self.by_category, cat.name, t)

    # -- queries ------------------------------------------------------------- #
    def get(self, key: Union[str, int]) -> Table:
        """Return the single table matching ``key`` (symbol, title, or uniqueid).

        ``key`` may be an int uniqueid, a ``0x…`` hex uniqueid string, a symbol,
        or a title. Raises :class:`AmbiguousTableError` if a symbol/title matches
        more than one table, or :class:`KeyError` if nothing matches. Correctness
        over convenience — it never silently returns an arbitrary match.
        """
        if isinstance(key, int):
            try:
                return self.by_id[key]
            except KeyError:
                raise KeyError(f"no table with uniqueid {key:#x}") from None

        if key in self.by_symbol:
            matches = self.by_symbol[key]
        elif key in self.by_title:
            matches = self.by_title[key]
        else:
            uid = None
            try:
                uid = _parse_int(key)
            except ValueError:
                uid = None
            if uid is not None and uid in self.by_id:
                return self.by_id[uid]
            raise KeyError(f"no table matching {key!r}")

        if len(matches) == 1:
            return matches[0]
        raise AmbiguousTableError(key, [t.uniqueid_hex for t in matches])

    def search(self, substring: str, *, case_sensitive: bool = False) -> list[Table]:
        """Return every table whose symbol or title contains ``substring``."""
        needle = substring if case_sensitive else substring.lower()

        def hit(text: Optional[str]) -> bool:
            if not text:
                return False
            hay = text if case_sensitive else text.lower()
            return needle in hay

        return [t for t in self.tables if hit(t.symbol) or hit(t.title)]

    def unique_tables(self) -> list[Table]:
        """One :class:`Table` per distinct ``uniqueid`` (the canonical view).

        ``tables`` is faithful to the file and includes the 98 cross-listed
        calibrations a2l2xdf emits twice (same uniqueid, different DTC/MIL
        title/category). Iterating ``tables`` therefore double-counts those; this
        view returns each uniqueid exactly once (first occurrence, file order),
        so oracle sweeps and consumers that want "every table once" don't
        double-count. See plan Decision 4.
        """
        return list(self.by_id.values())

    def categories(self) -> list[str]:
        """Category names that actually contain at least one table, sorted."""
        return sorted(self.by_category)

    def __len__(self) -> int:
        return len(self.tables)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"<XdfModel tables={len(self.tables)} "
            f"categories={len(self.category_by_index)} "
            f"base_offset={self.base_offset:#x}>"
        )


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def _decode_typeflags(
    flags: Optional[int], defaults: Defaults
) -> tuple[bool, bool, bool, bool]:
    """Return (little_endian, signed, is_float, column_major) from typeflags.

    When the attribute is absent, fall back to the header ``<DEFAULTS>``; a
    missing flag has no column-major bit, so it defaults to row-major.
    """
    if flags is None:
        return (defaults.lsbfirst, defaults.signed, defaults.is_float, False)
    return (
        bool(flags & _FLAG_LITTLE_ENDIAN),
        bool(flags & _FLAG_SIGNED),
        bool(flags & _FLAG_FLOAT),
        bool(flags & _FLAG_COLUMN_MAJOR),
    )


def _parse_embedded(
    ed: ET.Element, defaults: Defaults, uniqueid: int
) -> EmbeddedData:
    """Build an :class:`EmbeddedData` from an ``<EMBEDDEDDATA>`` element."""
    address = _parse_int(ed.get("mmedaddress"))
    if address is None:
        raise XdfParseError(
            f"table {uniqueid:#x}: EMBEDDEDDATA missing mmedaddress"
        )
    elem_bits = _parse_int(ed.get("mmedelementsizebits"), defaults.datasizeinbits)
    cols = _parse_int(ed.get("mmedcolcount"), 1)
    rows = _parse_int(ed.get("mmedrowcount"), 1)
    major = _parse_int(ed.get("mmedmajorstridebits"), 0)
    minor = _parse_int(ed.get("mmedminorstridebits"), 0)
    flags = _parse_int(ed.get("mmedtypeflags"))
    little_endian, signed, is_float, column_major = _decode_typeflags(flags, defaults)
    try:
        return EmbeddedData(
            address=address,
            rows=rows,
            cols=cols,
            elem_bits=elem_bits,
            major_stride_bits=major,
            minor_stride_bits=minor,
            signed=signed,
            little_endian=little_endian,
            is_float=is_float,
            column_major=column_major,
        )
    except ValueError as exc:
        raise XdfParseError(f"table {uniqueid:#x}: bad EMBEDDEDDATA — {exc}") from exc


def _parse_axis(ax: ET.Element, defaults: Defaults, uniqueid: int) -> Axis:
    """Build an :class:`Axis` from an ``<XDFAXIS>`` element."""
    axis_id = ax.get("id")
    if axis_id not in ("x", "y", "z"):
        raise XdfParseError(
            f"table {uniqueid:#x}: XDFAXIS has unexpected id {axis_id!r}"
        )

    # A non-z axis whose EMBEDDEDDATA carries no ``mmedaddress`` is a TunerPro
    # *label/static* axis: its breakpoints come from the ``<LABEL>`` elements, not
    # the bin (commonly flagged with ``mmedmajorstridebits="-32"``). Treat it as a
    # label axis (``embedded=None``) rather than an error — the switch-patch XDFs
    # use these heavily. The z-axis must always have real data, so it is left to
    # ``_parse_embedded`` to reject (and to the caller's z-embedded check).
    ed = ax.find("EMBEDDEDDATA")
    if ed is not None and axis_id != "z" and ed.get("mmedaddress") is None:
        ed = None
    embedded = _parse_embedded(ed, defaults, uniqueid) if ed is not None else None

    math = ax.find("MATH")
    if math is not None and math.get("equation"):
        scaling = ScalingEquation.from_expression(math.get("equation"))
    else:
        scaling = None

    labels = tuple(
        lbl.get("value", "")
        for lbl in ax.findall("LABEL")
    )

    # The standalone XDFTABLE this axis is embedded from, when the file says so.
    # Malformed or absent -> None: a link that cannot be parsed costs a label,
    # never a decode, so it is not worth failing a whole XDF over.
    embedinfo = ax.find("embedinfo")
    link_uniqueid = None
    if embedinfo is not None and embedinfo.get("linkobjid"):
        try:
            link_uniqueid = _parse_int(embedinfo.get("linkobjid"))
        except ValueError:
            link_uniqueid = None

    return Axis(
        axis_id=axis_id,
        units=ax.findtext("units"),
        min=_parse_float(ax.findtext("min")),
        max=_parse_float(ax.findtext("max")),
        embedded=embedded,
        scaling=scaling,
        labels=labels,
        link_uniqueid=link_uniqueid,
    )


def _symbol_from_description(description: Optional[str]) -> Optional[str]:
    """The A2L symbol is the first non-empty line of ``<description>``."""
    if not description:
        return None
    for line in description.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _parse_table(
    elem: ET.Element, defaults: Defaults, category_by_index: dict[int, Category]
) -> Table:
    """Build a :class:`Table` from an ``<XDFTABLE>`` element."""
    uid_text = elem.get("uniqueid")
    try:
        uniqueid = _parse_int(uid_text)
    except (ValueError, TypeError):
        uniqueid = None
    if uniqueid is None:
        raise XdfParseError(f"XDFTABLE missing/invalid uniqueid: {uid_text!r}")

    title = elem.findtext("title")
    symbol = _symbol_from_description(elem.findtext("description"))

    # Category membership. CATEGORYMEM category="N" is 1-based -> index N-1.
    cats: list[Category] = []
    for cm in elem.findall("CATEGORYMEM"):
        n = _parse_int(cm.get("category"))
        if n is None:
            continue
        cat = category_by_index.get(n - 1)
        if cat is not None:
            cats.append(cat)

    axes: dict[str, Axis] = {}
    for ax in elem.findall("XDFAXIS"):
        axis = _parse_axis(ax, defaults, uniqueid)
        axes[axis.axis_id] = axis

    if "z" not in axes or axes["z"].embedded is None:
        raise XdfParseError(
            f"table {uniqueid:#x}: z-axis has no EMBEDDEDDATA (nothing to read)"
        )

    return Table(
        uniqueid=uniqueid,
        title=title,
        symbol=symbol,
        categories=tuple(cats),
        x=axes.get("x"),
        y=axes.get("y"),
        z=axes.get("z"),
    )


def _parse_header(
    elem: ET.Element,
) -> tuple[str, int, bool, int, int, Defaults, dict[int, Category]]:
    """Extract deftitle, BASEOFFSET, REGION, DEFAULTS, and CATEGORYs from XDFHEADER."""
    deftitle = (elem.findtext("deftitle") or "").strip()
    base_offset = 0
    base_subtract = False
    region_start = 0
    region_size = 0
    defaults = Defaults()
    categories: dict[int, Category] = {}

    bo = elem.find("BASEOFFSET")
    if bo is not None:
        base_offset = _parse_int(bo.get("offset"), 0)
        base_subtract = _parse_int(bo.get("subtract"), 0) != 0

    reg = elem.find("REGION")
    if reg is not None:
        region_start = _parse_int(reg.get("startaddress"), 0)
        region_size = _parse_int(reg.get("size"), 0)

    df = elem.find("DEFAULTS")
    if df is not None:
        defaults = Defaults(
            datasizeinbits=_parse_int(df.get("datasizeinbits"), 8),
            sigdigits=_parse_int(df.get("sigdigits"), 4),
            outputtype=_parse_int(df.get("outputtype"), 1),
            signed=_parse_int(df.get("signed"), 0) != 0,
            lsbfirst=_parse_int(df.get("lsbfirst"), 1) != 0,
            is_float=_parse_int(df.get("float"), 0) != 0,
        )

    for cat in elem.findall("CATEGORY"):
        idx = _parse_int(cat.get("index"))
        name = cat.get("name")
        if idx is not None and name is not None:
            categories[idx] = Category(name=name, index=idx)

    return (
        deftitle, base_offset, base_subtract, region_start, region_size,
        defaults, categories,
    )


def parse_xdf(source) -> XdfModel:
    """Parse an XDF from a path or file-like object into an :class:`XdfModel`.

    Streams with ``iterparse``: the header is captured first, then each
    ``XDFTABLE`` is materialized, converted to a :class:`Table`, and cleared to
    keep memory flat across thousands of tables.
    """
    header_parsed = False
    deftitle = ""
    base_offset = 0
    base_subtract = False
    region_start = 0
    region_size = 0
    defaults = Defaults()
    categories: dict[int, Category] = {}
    tables: list[Table] = []

    for _, elem in ET.iterparse(source, events=("end",)):
        tag = elem.tag
        if tag == "XDFHEADER":
            (
                deftitle,
                base_offset,
                base_subtract,
                region_start,
                region_size,
                defaults,
                categories,
            ) = _parse_header(elem)
            header_parsed = True
            elem.clear()
        elif tag == "XDFTABLE":
            if not header_parsed:
                # Header should precede tables; parse with defaults if not.
                header_parsed = True
            tables.append(_parse_table(elem, defaults, categories))
            elem.clear()

    return XdfModel(
        deftitle=deftitle,
        base_offset=base_offset,
        base_subtract=base_subtract,
        region_start=region_start,
        region_size=region_size,
        defaults=defaults,
        categories=categories,
        tables=tables,
    )
