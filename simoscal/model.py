"""Core data model for the simoscal library.

These are the immutable metadata types the rest of the library is built on. They
carry *no* bin bytes and perform *no* I/O — a parser (U2) populates them from an
``.xdf``, and the codec/writer (U3/U4) use them to read and edit a ``.bin``.

Design principle (see the plan's Stakes & Safety section): *fail loud, change
nothing silently*. The scaling transform is the one place a silent numeric error
would corrupt a tune, so :class:`ScalingEquation` refuses to transform values it
cannot prove are linear, and detects linearity by safe numeric probing of an
AST-parsed expression — never by ``eval``-ing arbitrary strings.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Optional, Sequence, Union

import numpy as np

__all__ = [
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
]


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class SimosCalError(Exception):
    """Base class for all simoscal errors."""


class AmbiguousTableError(SimosCalError):
    """Raised when a symbol/title lookup matches more than one table.

    Carries the ambiguous key and the list of candidate uniqueids so the caller
    can disambiguate rather than silently editing the wrong table.
    """

    def __init__(self, key: str, candidates: Sequence[str]):
        self.key = key
        self.candidates = list(candidates)
        joined = ", ".join(self.candidates)
        super().__init__(
            f"{key!r} matches {len(self.candidates)} tables: [{joined}]. "
            "Disambiguate by uniqueid."
        )


class NonLinearEquationError(SimosCalError):
    """Raised when a linear-only transform is requested on a non-linear equation.

    Non-linear tables fall back to raw-only editing; asking a non-linear
    :class:`ScalingEquation` to convert to/from physical units is a hard error,
    never a silent approximation.
    """


class RegionBoundsError(SimosCalError):
    """Raised when a computed file offset + extent falls outside the bin region."""


class FloatBugGuardError(SimosCalError):
    """Raised when a write to a float-bug-prone table would exceed its safe range.

    This guard is a safety mechanism (see plan Decision 9): it hard-rejects the
    irreversible-corruption case even when an override flag is set.
    """


class RawRangeError(SimosCalError):
    """Raised when an inverted value does not fit the element's raw integer type.

    Writing a value outside ``[raw_min, raw_max]`` for the element width would
    wrap/truncate and silently corrupt the cell, so it is a hard error for every
    table — never warn-and-allow, never overridable. (Out-of-*display*-range
    writes, by contrast, are warn+allow per Decision 8.)
    """


# --------------------------------------------------------------------------- #
# Safe expression evaluation (for linearity detection)
# --------------------------------------------------------------------------- #
_ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
_ALLOWED_UNARYOPS = (ast.UAdd, ast.USub)


class _ExpressionUnsafe(ValueError):
    """Internal: expression contains a node we refuse to evaluate."""


def _eval_node(node: ast.AST, var_name: str, x: float) -> float:
    """Evaluate a restricted arithmetic AST at ``var_name == x``.

    Only numeric literals, the single variable, and the arithmetic operators
    ``+ - * / **`` (plus unary +/-) are permitted. Anything else raises
    :class:`_ExpressionUnsafe` so an untrusted MATH string can never execute
    arbitrary code.
    """
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, var_name, x)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _ExpressionUnsafe(f"non-numeric constant: {node.value!r}")
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id != var_name:
            raise _ExpressionUnsafe(f"unknown identifier: {node.id!r}")
        return float(x)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, _ALLOWED_UNARYOPS):
        operand = _eval_node(node.operand, var_name, x)
        return +operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp) and isinstance(node.op, _ALLOWED_BINOPS):
        left = _eval_node(node.left, var_name, x)
        right = _eval_node(node.right, var_name, x)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left ** right
    raise _ExpressionUnsafe(f"disallowed expression node: {type(node).__name__}")


def _make_evaluator(expression: str, var_name: str):
    """Compile ``expression`` into a safe callable f(x). None if it won't parse."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return None

    def f(x: float) -> float:
        return _eval_node(tree, var_name, x)

    return f


# --------------------------------------------------------------------------- #
# ScalingEquation
# --------------------------------------------------------------------------- #
# Probe points used to fit/verify a linear equation. Chosen to avoid a single
# accidental coincidence and to steer clear of X=0 division issues; X=0 stays in
# the set because the intercept is what we ultimately need.
_PROBE_POINTS = (0.0, 1.0, 2.0, 5.0, -3.0, 0.5)
# Relative tolerance for declaring the probed function linear.
_LINEARITY_RTOL = 1e-9
_LINEARITY_ATOL = 1e-9


@dataclass(frozen=True)
class ScalingEquation:
    """A calibration scaling ``phys = m * X + b`` (linear) or a non-linear flag.

    ``m`` (slope) and ``b`` (intercept) map a raw integer element ``X`` to a
    physical value. For non-linear equations ``is_linear`` is False and the
    transform methods refuse to run.
    """

    m: float
    b: float
    is_linear: bool = True
    expression: Optional[str] = None

    # -- construction helpers ------------------------------------------------ #
    @classmethod
    def identity(cls) -> "ScalingEquation":
        """The identity scaling: ``phys == X`` (``m=1, b=0``)."""
        return cls(m=1.0, b=0.0, is_linear=True, expression="X")

    @classmethod
    def from_expression(cls, expression: str, var_name: str = "X") -> "ScalingEquation":
        """Build a :class:`ScalingEquation` from a TunerPro MATH expression.

        Detects linearity by numerically probing the AST-parsed expression at a
        handful of points. If the function is linear (identity, an ``m*X+b``
        form, or the ``((a*X)-b)/(c-(d*X))`` form with ``d=0``), ``m`` and ``b``
        are extracted and ``is_linear`` is True. Otherwise a non-linear equation
        is returned with ``is_linear=False`` and ``m``/``b`` set to NaN — its
        transform methods will raise :class:`NonLinearEquationError`.
        """
        f = _make_evaluator(expression, var_name)
        if f is None:
            return cls(m=float("nan"), b=float("nan"), is_linear=False,
                       expression=expression)

        try:
            samples = [(x, f(x)) for x in _PROBE_POINTS]
        except (_ExpressionUnsafe, ZeroDivisionError, OverflowError, ValueError):
            return cls(m=float("nan"), b=float("nan"), is_linear=False,
                       expression=expression)

        if any(not np.isfinite(y) for _, y in samples):
            return cls(m=float("nan"), b=float("nan"), is_linear=False,
                       expression=expression)

        # Fit slope/intercept from the first two distinct probe points.
        (x0, y0), (x1, y1) = samples[0], samples[1]
        m = (y1 - y0) / (x1 - x0)
        b = y0 - m * x0

        # Verify every probe point lies on that line.
        for x, y in samples:
            if not np.isclose(y, m * x + b, rtol=_LINEARITY_RTOL, atol=_LINEARITY_ATOL):
                return cls(m=float("nan"), b=float("nan"), is_linear=False,
                           expression=expression)

        return cls(m=float(m), b=float(b), is_linear=True, expression=expression)

    # -- transforms ---------------------------------------------------------- #
    def _require_linear(self) -> None:
        if not self.is_linear:
            raise NonLinearEquationError(
                f"equation is non-linear (expression={self.expression!r}); "
                "use raw-only editing instead of physical-unit conversion."
            )

    def to_physical(self, raw: Union[int, float, np.ndarray]) -> np.ndarray:
        """Convert raw integer element value(s) to physical units: ``m*X + b``.

        Accepts a scalar or numpy array; always returns a float64 numpy array.
        """
        self._require_linear()
        raw_arr = np.asarray(raw, dtype=np.float64)
        return np.asarray(self.m * raw_arr + self.b, dtype=np.float64)

    def to_raw(self, phys: Union[int, float, np.ndarray]) -> np.ndarray:
        """Invert the scaling: ``X = round((phys - b) / m)`` as int64.

        Rounds to the nearest representable raw integer (round-half-to-even).
        The codec (U3/U4) narrows to the table's actual element width; here the
        result stays int64 so no width information is lost prematurely.
        """
        self._require_linear()
        if self.m == 0.0:
            raise NonLinearEquationError(
                "scaling slope m is zero; equation is not invertible."
            )
        phys_arr = np.asarray(phys, dtype=np.float64)
        raw = np.rint((phys_arr - self.b) / self.m)
        return np.asarray(raw, dtype=np.int64)


# --------------------------------------------------------------------------- #
# EmbeddedData
# --------------------------------------------------------------------------- #
_VALID_ELEM_BITS = (8, 16, 32)


@dataclass(frozen=True)
class EmbeddedData:
    """Where and how a table's (or axis's) raw values live in the bin.

    Mirrors the XDF ``EMBEDDEDDATA`` element. ``address`` is the raw
    ``mmedaddress`` (pre-BASEOFFSET); the codec adds the base offset. Strides are
    in bits, matching TunerPro. ``signed``/``little_endian``/``is_float``/
    ``column_major`` are the decoded ``mmedtypeflags`` bits (see plan Decision 6).
    ``column_major`` selects the on-bin element order for multi-row/col tables:
    when set, each column's ``rows`` elements are contiguous (Y fastest); when
    clear, each row's ``cols`` elements are contiguous (row-major, X fastest).
    """

    address: int
    rows: int
    cols: int
    elem_bits: int
    major_stride_bits: int = 0
    minor_stride_bits: int = 0
    signed: bool = False
    little_endian: bool = True
    is_float: bool = False
    column_major: bool = False

    def __post_init__(self) -> None:
        if self.elem_bits not in _VALID_ELEM_BITS:
            raise ValueError(
                f"elem_bits must be one of {_VALID_ELEM_BITS}, got {self.elem_bits}"
            )
        if self.is_float and self.elem_bits != 32:
            raise ValueError(
                f"float elements must be 32-bit, got {self.elem_bits}-bit float"
            )
        if self.rows < 1 or self.cols < 1:
            raise ValueError(
                f"rows and cols must be >= 1, got rows={self.rows}, cols={self.cols}"
            )
        if self.address < 0:
            raise ValueError(f"address must be non-negative, got {self.address}")

    @property
    def element_bytes(self) -> int:
        """Width of one element in bytes (1, 2, or 4)."""
        return self.elem_bits // 8

    @property
    def count(self) -> int:
        """Total number of elements (``rows * cols``)."""
        return self.rows * self.cols

    @property
    def shape(self) -> tuple[int, int]:
        """``(rows, cols)`` shape of the decoded array."""
        return (self.rows, self.cols)

    @property
    def raw_int_range(self) -> Optional[tuple[int, int]]:
        """Inclusive ``(min, max)`` an integer element can hold, or ``None``.

        Returns ``None`` for float elements (their range is the IEEE-754 float
        range, not a small integer interval). Used by the writer to reject an
        inverted value that would overflow the element width.
        """
        if self.is_float:
            return None
        if self.signed:
            return (-(2 ** (self.elem_bits - 1)), 2 ** (self.elem_bits - 1) - 1)
        return (0, 2 ** self.elem_bits - 1)


# --------------------------------------------------------------------------- #
# Axis
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Axis:
    """One axis (x, y, or z) of a table.

    The z-axis carries the table's data (its ``embedded``/``scaling`` describe
    the cell values); x/y axes describe the row/column breakpoints. ``labels``
    holds static/text labels when the axis uses them instead of embedded data.
    """

    axis_id: str  # "x", "y", or "z"
    units: Optional[str] = None
    min: Optional[float] = None
    max: Optional[float] = None
    embedded: Optional[EmbeddedData] = None
    scaling: Optional[ScalingEquation] = None
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.axis_id not in ("x", "y", "z"):
            raise ValueError(f"axis_id must be 'x', 'y', or 'z', got {self.axis_id!r}")


# --------------------------------------------------------------------------- #
# Category
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Category:
    """A table category (from the XDF ``CATEGORY`` header / ``CATEGORYMEM``)."""

    name: str
    index: Optional[int] = None


# --------------------------------------------------------------------------- #
# Table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Table:
    """A calibration table: metadata + its x/y/z axes.

    ``uniqueid`` is the canonical, guaranteed-unique handle (the XDF
    ``uniqueid``, stored as an int). ``symbol`` (the XDF ``<description>`` /
    A2L symbol) and ``title`` are *not* guaranteed unique and are indexed as
    multimaps by the parser. The ``z`` axis holds the table data.
    """

    uniqueid: int
    title: Optional[str] = None
    symbol: Optional[str] = None
    categories: tuple[Category, ...] = ()
    x: Optional[Axis] = None
    y: Optional[Axis] = None
    z: Optional[Axis] = None

    @property
    def uniqueid_hex(self) -> str:
        """The canonical uniqueid rendered as ``0x…`` (matches XDF/TunerPro)."""
        return f"0x{self.uniqueid:x}"

    @property
    def embedded(self) -> Optional[EmbeddedData]:
        """Convenience: the z-axis embedded data (where cell values live)."""
        return self.z.embedded if self.z is not None else None

    @property
    def scaling(self) -> Optional[ScalingEquation]:
        """Convenience: the z-axis scaling equation."""
        return self.z.scaling if self.z is not None else None

    @property
    def shape(self) -> Optional[tuple[int, int]]:
        """``(rows, cols)`` from the z-axis embedded data, if present."""
        emb = self.embedded
        return emb.shape if emb is not None else None
