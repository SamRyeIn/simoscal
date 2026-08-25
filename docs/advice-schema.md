# The recommendations file — schema reference

This is the file format for answering a **context bundle**. A person exports a
bundle from the app, asks Claude anywhere, and brings the answer back as one
JSON file. This document is written for whoever writes that answer.

Read one thing first: **passing this schema means the file is readable, not that
its advice is safe.** Every recommendation that survives validation is then
replayed against the live session through the library's real edit guards with
`dry_run=True`. A refusal there is silent to the reviewer — the recommendation
is dropped and counted, never rendered. Writing a well-formed file is table
stakes; writing a *correct* one is the job.

Implemented by `simoscal/advice/schema.py`. Current version: **1**.

## Versioning

`schema_version` is versioned **independently of `BRIDGE_VERSION`**: this file is
authored outside the app and will change shape faster than the app's protocol
does. A version this library does not read is one clean rejection saying so —
the fields underneath are not examined, because they were written to a schema
nobody here knows.

## Shape

```json
{
  "schema_version": 1,
  "provenance": {
    "profile": "SC8S50",
    "bin_sha256": "7d7aa2f878fcbdd024dc318d988befc96084e83daf67ed3b66bace53d900ca0e",
    "xdf_sha256": "f063505f65353825bb36148e283392e6b5ffa15bd9ee8cfee0e2ec754fca59b1"
  },
  "summary": "one knock-driven change; boost tracking looked clean",
  "recommendations": [
    {
      "id": "rec-1",
      "table": {
        "name": "slot_put_max_1",
        "id": "IP_FAC_BPA_SP[1]",
        "description": "Map for boost pressure actuator setpoint"
      },
      "change": {
        "space": "patch",
        "operation": "set",
        "selection": { "kind": "cells", "args": [[0, 7]] },
        "value": 57.5
      },
      "intent": "pull wastegate duty where the pull knocked",
      "evidence": "pull #3, rows 188-204, knock count 3, IAT 48 °C",
      "risk": "safety-relevant",
      "confidence": "medium",
      "prediction": "knock count returns to 0 across 5000-6000 rpm at similar IAT"
    }
  ]
}
```

## Envelope

| Field             | Type   | Required | Meaning                                                                 |
|-------------------|--------|----------|--------------------------------------------------------------------------|
| `schema_version`  | int    | yes      | The schema this file is written to. Currently only `1` is read.          |
| `provenance`      | object | yes      | Which calibration this answers — copy it from the bundle, unchanged.     |
| `summary`         | string | no       | A free-text line for the reviewer. Defaults to `""`.                     |
| `recommendations` | list   | yes      | Zero or more records. **Empty is a valid answer**: "I found nothing."    |

No other top-level fields are accepted. An unrecognized one is reported by name
rather than ignored, because the usual cause is a typo in a field that *was*
meant to be read.

### `provenance`

| Field        | Type   | Meaning                                                              |
|--------------|--------|----------------------------------------------------------------------|
| `profile`    | string | The resolved profile the bundle named.                               |
| `bin_sha256` | string | 64 hex chars. The bin the bundle was exported from.                  |
| `xdf_sha256` | string | 64 hex chars. The XDF that bundle resolved against.                  |

Copy all three verbatim from the bundle. They exist so a reply can be matched to
the session that prompted it: a reply aimed at a different bin is not a set of
weak recommendations, it is a set of recommendations about cells that are not
the cells it thinks they are. The schema checks these are present and shaped
like hashes; the *match* is checked at replay, where the session is.

## A recommendation

Every field below is **required**. There is no partial record: an item that
cannot supply one of these is malformed and is never rendered.

| Field        | Type   | Meaning                                                                          |
|--------------|--------|-----------------------------------------------------------------------------------|
| `id`         | string | Unique within the file. How a drop, a queue item, or a rejection refers to it.    |
| `table`      | object | Which table — see below. Both the ID and the description, always.                 |
| `change`     | object | The proposed write, addressed the way an op addresses one — see below.            |
| `intent`     | string | One line, the shape an `intent=` carries in a revision script.                    |
| `evidence`   | string | The log rows or table values that justify it. **Mandatory** — see below.          |
| `risk`       | string | `cosmetic` · `performance` · `safety-relevant`. Closed set.                       |
| `confidence` | string | `low` · `medium` · `high`. Closed set.                                            |
| `prediction` | string | What the next drive should show if this change is right.                          |

### `table`

| Field         | Meaning                                                                        |
|---------------|---------------------------------------------------------------------------------|
| `name`        | The **logical (profile) name** — the `name` field in the bundle's catalog. This is what the replay resolves and edits through. |
| `id`          | The parameter ID, e.g. `C_M_AIR_CYL_SP_MAX`.                                   |
| `description` | Its plain-English description.                                                  |

`id` and `description` are the two halves of the project's naming rule —
`` `ID` — Description `` — and **both are required**. One half without the other
is rejected, so a reviewer is never asked to approve a change to something they
have to go look up.

Which write path replays a record is **not** a field in this file: it follows
from which table the record names. A recommendation is not a new invariant, so
it gets no new write path — it gets the ones that already exist.

### `change`

| Field       | Type            | Meaning                                                        |
|-------------|-----------------|-----------------------------------------------------------------|
| `space`     | string          | The table space, e.g. `base` or `patch`. Required — the two can hold same-named tables. |
| `operation` | string          | One of the closed set below.                                    |
| `selection` | object          | Which cells — `{"kind": ..., "args": [...]}`.                   |
| `value`     | number          | A scalar operand. Finite; a boolean is not a number here.       |
| `array`     | list            | A 1-D list or a rectangular 2-D list of finite numbers.         |

**Values are physical units** — psi, °C, lambda, rpm — the same units the
bundle's catalog reports. Never raw bytes, never scaled counts.

`operation` is one of: `set`, `add`, `sub`, `mul`, `div`, `fill`, `interpolate`,
`paste`, `restore`.

Operand rules follow from the operation:

| Operation                                | Operand                             |
|------------------------------------------|--------------------------------------|
| `set` `add` `sub` `mul` `div` `fill`     | `value` **or** `array`, not both     |
| `paste`                                  | `array` (a scalar is not a paste)    |
| `interpolate` `restore`                  | neither — the result comes from the table itself |

`selection` uses exactly the encoding the `edit` op already takes:

| Selection                                    | Cells                                  |
|----------------------------------------------|-----------------------------------------|
| `{"kind": "all"}`                            | the whole table                         |
| `{"kind": "row", "args": [3]}`               | row 3                                   |
| `{"kind": "col", "args": [7]}`               | column 7                                |
| `{"kind": "region", "args": [0, 2, 3, 5]}`   | rows 0–2 × cols 3–5, inclusive          |
| `{"kind": "cells", "args": [[3, 7], [3, 8]]}`| the listed `[row, col]` pairs           |

Indices are zero-based, non-negative integers. Whether they fit the table is
checked at replay against the table's real shape, not here.

### Why evidence is mandatory

A recommendation citing nothing is **malformed, not weak**. It is rejected by
this schema and never reaches the review queue. This is deliberate: it makes
"no evidence, no flag" a property of the format rather than something a reviewer
has to remember to enforce at the end of a long day. Cite the pull, the row
range, the channel, the values — enough that a reader can go look.

### Why the sets are closed

`risk` gates how an item is presented: a safety-relevant item is styled so it is
hard to thumb past. A tier that arrived unannounced would render as though it
were ordinary, which is the exact failure the tier exists to prevent. Same
reasoning for `confidence`, which is read comparatively across a queue — free
text cannot be compared, sorted, or back-tested against what actually happened.

### Why every recommendation carries a prediction

An accepted recommendation has to be **gradeable**. The next log review, after
the change is flashed and driven, should be able to say plainly whether the
prediction held. "Peak boost tracks setpoint within 10 kPa through 5500 rpm" can
be graded; "should feel better" cannot.

## Rejection

Validation returns **every** problem in the file at once, each naming the record
and the field:

```
3 problems in the recommendations file:
  - recommendations[0].evidence: 'evidence' must not be empty
  - recommendations[1].risk: unknown risk 'mild'; expected one of cosmetic, performance, safety-relevant
  - recommendations[2].change.value: operation 'set' requires 'value' or 'array'
```

That is so a whole file can be fixed in one pass. One problem per round trip
turns fixing a file into a conversation.

Malformed JSON is reported as one failure with the decoder's line and column —
never a traceback.

## What happens to a well-formed file

Each record is replayed against the live session through the library's **real**
edit path, with the write rewound afterwards. Three outcomes, counted
separately:

| Outcome       | Meaning                                                                    |
|---------------|-----------------------------------------------------------------------------|
| **queued**    | The guards accepted it. Shown to a person with a preview of the *real* effect — what the bin would actually hold, re-decoded, not what the record claimed. |
| **dropped**   | The guards refused it, or the table has no write path. Never shown as a suggestion. The refusal reason comes back so the file can be improved. |
| **malformed** | It failed this schema. Counted apart from *dropped*, because a bad file and bad advice are different problems. |

Recommendations are replayed **independently against the session's current
state**, not cumulatively. Two that each pass alone can still conflict when both
are accepted; the review flags queued items whose cells overlap, but it will not
resolve the conflict for you. Prefer one recommendation per coherent change.

### Domain-owned tables take a value, not arithmetic

Some tables are written by a call that knows an invariant no grid write can see
— the per-slot boost grids (one curve tiled across every row), the road-speed
limiter (four scalars written as one coherent set), the cylinder-cut trio
(soft ≤ medium ≤ hard), the full-load enrichment map (a lean bound). A
recommendation naming one of those is routed to that call automatically; you do
not name the call, and there is no field for it.

For those tables, use `set`, `fill`, or `paste` — an operation that states the
values the table should end at. `add`, `mul`, `interpolate` and friends are
dropped, because the owning call is given a result, not an expression.

A few tables are refused outright even though a write path exists. The important
one: `C_M_AIR_CYL_SP_MAX` — Maximum allowed airmass setpoint is labelled mg/stk
and *stores kg/stk*, so a stated grid value is ambiguous between the two by a
factor of a million — the factor that removes the limiter entirely. The courier
will not guess. Do not recommend a change to it; say in `summary` what you would
have changed and why, and leave the edit to the screen that knows the units.

### `table.id` must match the calibration

The `id` you give must be the same identifier the bundle's catalog reported for
that logical `name`. A record pairing one table's name with another table's ID is
dropped — otherwise the queue would show a person one table's name over another
table's change.
