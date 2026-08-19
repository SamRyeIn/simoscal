# Test fixtures

| File | Purpose | Source |
|------|---------|--------|
| `mini.xdf` | Hand-written 3-table XDF snippet for parser unit tests. | In-repo. |
| `tunerpro_oracle.json` | **Not committed by default.** The one-time TunerPro read-parity capture that AE1 checks against. | Recorded on Windows — see below. |

The large real files the acceptance suite runs against belong at the **repo
root** (`Code/`), not here, and are **not committed** — neither an OEM bin nor
the SC8S50 XDFs are ours to redistribute (see `LICENSE-THIRD-PARTY`), so `bin/`
and `xdf/` are gitignored. Supply your own:

- `../../xdf/SC8S50.V1.0.xdf`
- `../../bin/5G0906259L__0002.bin`

`conftest.py` resolves them and **skips** any test needing them when they are
absent, so a fresh clone still runs green.

---

## Why `tunerpro_oracle.json` exists

Every read-correctness oracle the library ships (type-envelope bounds, the
`struct` cross-decode, inverse round-trip, known-value pins) *consumes the
library's own interpretation of the `mmedtypeflags` bits* — endian (`0x02`),
signed (`0x04`), float (`0x10000`) — see plan Decision 6. They prove the
**implementation** is self-consistent, but they cannot independently confirm
the **bit semantics** are right, because a wrong-but-consistent interpretation
would pass them all.

TunerPro is the authoring tool for these XDFs and is the one **independent**
authority on what a cell should display. Capturing a handful of its displayed
values, once, settles Decision 6 outright. AE1
(`test_acceptance.py::test_ae1_values_match_tunerpro`) is that check. It is
gated behind the `tunerpro` pytest marker and skips until this file is present —
so the day-to-day Mac workflow never depends on Windows.

## What to capture

One Windows session, TunerPro loading `SC8S50.V1.0.xdf` over
`5G0906259L__0002.bin`. Record **~10 tables** deliberately spanning the decode
surface so the capture exercises every type path:

| Coverage target | Why |
|-----------------|-----|
| 8-bit signed | `0x04` sign bit at width 8 |
| 8-bit unsigned | baseline unsigned |
| 16-bit signed | multi-byte + endian (`0x02`) with sign |
| 16-bit unsigned | multi-byte endian, no sign |
| 32-bit signed *(if present)* | widest integer path |
| 32-bit float (e.g. `C_PRS_IM_SP_MAX`) | `0x10000` float path |
| ≥1 multi-row table (e.g. `ID_PORT_SP`, 10×10) | stride / cell-order (row-major) |
| ≥1 table with non-identity scaling (`m ≠ 1` or `b ≠ 0`) | the linear MATH is applied |

Record the values **exactly as TunerPro displays them** (physical units, after
scaling). For each table note its `uniqueid` (TunerPro shows it; also findable
via `CalFile.get(symbol).uniqueid_hex`).

## File schema

AE1 reads this shape (see `test_acceptance.py`):

```json
{
  "xdf": "SC8S50.V1.0.xdf",
  "bin": "5G0906259L__0002.bin",
  "captured": "2026-07-05 by <name>, TunerPro v5.00.xxxx",
  "tolerance": 0.01,
  "tables": [
    {
      "uniqueid": "0x11f9c",
      "symbol": "ID_PORT_SP",
      "note": "10x10 int8, identity scaling",
      "values": [
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
        ...
      ]
    },
    {
      "uniqueid": "0x...",
      "symbol": "C_PRS_IM_SP_MAX",
      "note": "1x1 float32, hPa",
      "tol": 0.5,
      "values": [[2400.0]]
    }
  ]
}
```

Field rules:

- **`tables`** (required) — list of captured tables; AE1 fails if empty.
- **`uniqueid`** (required) — hex string (`"0x11f9c"`) or integer; the primary
  handle passed to `CalFile.get`.
- **`values`** (required) — the full 2-D array of TunerPro-displayed physical
  values, **row-major**, matching the table's `(rows, cols)` shape. A scalar
  table is `[[value]]`.
- **`symbol`** / **`note`** (optional) — documentation only; `symbol` is used in
  failure messages.
- **`tol`** (optional, per-table) — absolute tolerance for that table; overrides
  the top-level `tolerance`. Use a looser value (e.g. `0.5`) for float tables
  where display rounding differs from the raw float.
- **`tolerance`** (optional, top-level) — default absolute tolerance
  (`0.01` if omitted).

## Running AE1 once the capture exists

Drop the file at `Code/tests/fixtures/tunerpro_oracle.json`, then:

```
cd Code && ./.venv/bin/python -m pytest tests/test_acceptance.py -m tunerpro -v
```

It runs (instead of skipping) and compares every captured table against the
library's decode within tolerance. Commit the JSON so AE1 stays enforced —
re-capture only when a new box code / XDF is introduced.
