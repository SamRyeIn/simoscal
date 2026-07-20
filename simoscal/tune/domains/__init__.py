"""Domain modules — the named vocabulary a revision is written in.

Each module is a thin facade over :meth:`Tune.write`, grouping the tables a
tuner thinks about together and encoding, once, the rules that go with them:
which cells a change belongs in, which units it is stated in, which guard
applies, and which sibling tables must move with it.

They add no new way to reach a bin — every method routes through the journal —
so what they really add is *the correct default*: the wastegate's two VVL
tables move together, the boost ceiling touches only the full-load row, and the
airmass cap takes mg/stk because taking raw kg/stk is how a limiter gets
removed by accident.

Reached as attributes of a tune: ``tune.boost``, ``tune.wastegate``,
``tune.limits``, ``tune.fueling``, ``tune.ignition``, ``tune.switchpatch``.
"""

from __future__ import annotations

from .boost import Boost
from .limits import Limits
from .wastegate import Wastegate

__all__ = ["Boost", "Limits", "Wastegate"]
