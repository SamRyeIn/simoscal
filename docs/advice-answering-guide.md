# Answering a context bundle

You have been handed a **context bundle**: one JSON file describing somebody's
open tuning session — every table their calibration resolves, what those tables
currently hold, what they have changed so far, what their last drive logged, and
the safety brief for that car. Your job is to read it and write back one
**recommendations file**.

This guide is the method. [`advice-schema.md`](advice-schema.md) is the format;
read it too, and treat it as the authority on every field. What follows is the
part a schema cannot state: how to get from a bundle to advice worth acting on.

Three things to hold onto before anything else:

1. **You cannot see the bin.** Neither its bytes nor the XDF's travel in a
   bundle — only their hashes. Everything you know about the calibration is the
   decoded physical values in the `tables` section. If a fact you want is not in
   the bundle, you do not have it, and you must not assume it.
2. **Nothing you write is trusted.** Every recommendation is replayed against
   the live session through the library's real edit guards before a person sees
   it. A refusal there is silent to the reviewer: the item is dropped and
   counted, never rendered as a suggestion. Being well-formed gets you replayed;
   being right is a separate job.
3. **A person decides, one item at a time.** There is no accept-all. Write for a
   reviewer holding a tablet who will read your `intent`, `evidence` and
   `prediction` and then say yes or no to that item alone.

---

## 1. What is in a bundle

| Key                | What it is                                                                 |
|--------------------|----------------------------------------------------------------------------|
| `bundle_version`   | The bundle format's own version.                                           |
| `reply`            | The contract your answer must meet, restated in the file itself.           |
| `provenance`       | Which calibration this is — and which address convention it is written in. |
| `safety_brief`     | Two halves: facts about these ECUs generally, and facts about *this* car.  |
| `tables`           | Every table the profile resolved, with current physical values and axes.   |
| `journal`          | What this session has already changed, in order, with each stated intent.  |
| `journal_counts`   | The same, counted by verdict.                                              |
| `logs`             | The analysis battery's findings document for whatever datalogs were picked.|
| `log_names`        | Which logs those were.                                                     |
| `notes`            | Free text the person added, when they added any. Often the actual question.|

Read them in this order: **`notes` → `safety_brief` → `logs` → `journal` →
`tables`**. The notes say what was asked. The brief says what will get you
refused. The logs say what the car actually did. The journal says what has
already been tried and why. The tables are the reference you consult once you
know what you are looking for — not a thing to read front to back.

### `provenance` — copy three fields, read the rest

`profile`, `bin_sha256` and `xdf_sha256` are copied **verbatim** into your
reply's `provenance`. They are how your answer is matched to the session that
asked; a mismatch is refused wholesale, before any replay, because advice aimed
at a different bin is not weak advice — it is advice about cells that are not
the cells it thinks they are.

The rest is structure identity, and you read it rather than echo it:
`structure`, `spaces`, `profiles`, `xdf_addresses_from_cal` and `address_note`.
The address note matters if you ever quote an address: the same number means a
different byte depending on whether the definition counts from the start of the
whole bin or the start of the extracted CAL block. Prefer not to quote addresses
at all — name tables, not offsets.

`has_switch_patch` tells you whether there is a second table space. If there is,
tables in it are addressed with that space's name in `change.space`; `base` and
the patch space can hold same-named tables, so the space is never optional.

### `tables` — the calibration, decoded

Each entry carries the logical `name` (what a recommendation resolves through),
the `id` and `description` (the two halves of `` `ID` — Description ``), `units`,
`shape`, `values` in physical units, and the decoded `x_axis` / `y_axis` where
the table has them. Read the axes: a cell index means nothing until you know the
rpm and load it sits at.

Two fields decide whether you may recommend a change at all:

- **`owner`** — empty means the generic editor writes it and any operation is
  available. Non-empty means a domain call owns the write because it enforces an
  invariant a grid write cannot see. For an owner-locked table you may only
  state the values it should **end at**: `set`, `fill` or `paste`. `add`, `mul`,
  `interpolate` and friends are dropped, because the owning call is handed a
  result, not an expression.
- **`is_axis`** — an axis table must stay strictly increasing. Writing one is a
  re-breakpoint, and it changes the meaning of every cell in every table that
  shares it. Do not propose one casually, and never propose one without saying
  in `intent` what else moves.

**`source_values`** appears only on tables this session has already edited, and
it is what the *imported* bin held. That is the grid the bundled datalogs were
actually recorded on. When you reason from a log, reason against `source_values`
where it exists and `values` everywhere else — otherwise you are explaining a
log with a calibration that was never driven.

### `logs` — the analysis battery's own document

The `logs` section is exactly what `python -m simoscal.analysis` writes: the same
checks, thresholds and pull detection, so you are reading the library's one
description of what a log says rather than a summary written for you.

| Key                | What it holds                                                                  |
|--------------------|---------------------------------------------------------------------------------|
| `pulls`            | Each detected pull: gear, rpm range, row range, peak boost, PUT error, knock, HPFP, lambda error, ambient conditions. |
| `findings`         | Each check's verdict: `severity`, `title`, `message`, structured `evidence`, `pull_refs`. |
| `skipped`          | Checks that did **not** run, each with its reason. Read this list.               |
| `coverage`         | Which table cells the logged operating points actually visited.                  |
| `battery` / `ran`  | Which checks exist and which of them ran.                                        |
| `cal_resolved`     | Whether the cal-aware checks had a calibration.                                  |

`cal_resolved` is normally **false** here, and that is deliberate: the cal-aware
checks want the bin the logs were *recorded on*, and the session's working buffer
has not been flashed. Those checks land in `skipped` with their reason. A finding
that did not run is not a finding that passed — if the question you were asked
depends on a skipped check, say so in `summary` instead of guessing.

There are no plot series in a bundle. You have numbers, not pictures.

### `safety_brief` — read it before you write anything

Its authored half carries the facts that have each already cost somebody a flash
cycle: how overboost faults are actually routed, why an XDF's declared maximum is
information about the definition rather than a limit the ECU enforces, and the
two rules for reading these logs — the gear channel's meaning depends on its
header, and the acceleration-derived power channels must be trimmed to in-gear
samples before any peak is quoted.

Its generated half is about *this* car: which of its tables store a unit their
label does not admit to, which declare a maximum that is not a limit, what stock
reads, and which tables this car does not have. It is rendered from the car's own
profile, so it is current by construction. Where the brief names a table as a
trap, believe it over your own reading of the units.

The brief is **not** the safety mechanism, and says so in its own first
paragraph. It exists so your recommendations *start* sensible, and so fewer of
them are refused for reasons that were knowable before you wrote them.

---

## 2. The method

### Establish what actually happened

Start from `logs.findings`, in severity order, and pull each one back to its
pulls. For every claim you intend to make, find the number in the bundle that
carries it: a pull index, a row range, an rpm band, a channel, a value. If you
cannot find one, you do not have a finding — you have an impression, and an
impression cannot be written down here, because evidence is a schema requirement
and an unevidenced record is rejected before anyone reads it.

Check the conditions before comparing anything. `pulls[].environment` carries
ambient temperature and pressure, intake temperature and coolant temperature. Two
pulls at different IATs are not the same experiment, and a change sized against
the difference between them is sized against the weather.

### Find the table that owns the behaviour

Now go to `tables` and find what actually decides the thing the log shows. Two
mistakes are common enough to name:

- **Recommending against the symptom's table rather than the cause's.** A boost
  shortfall shows up as a pressure error; it is not necessarily fixed in a
  pressure table. Read the journal — the change that caused it may already be
  in there.
- **Recommending against a limit when the setpoint is what binds.** A limit that
  never binds does nothing when you move it. If you cannot show from the logs
  that a limit is being reached, moving it changes nothing and spends a flash.

Use `coverage` to check the cells you want to move were actually visited. A cell
the car never operated in has no log evidence behind it, whatever you believe
about its shape.

### Size the change, do not guess it

Say where the number came from. A ratio from the logs, a rule of thumb from the
brief, a walk-back to a value this lineage has already run — any of those is a
defensible origin. "Try 5 % more" is not. Prefer, in order:

1. **Walking back a prior edit.** If the journal shows a change whose premise the
   new logs have inverted, undoing it is the best-bounded move available: the
   destination is a value the car has already run and logged.
2. **A bounded step toward a target you can compute** from logged values.
3. **A conservative fraction of the gap**, stated as such, when the transfer
   function is uncertain. Recovering half a shortfall and re-logging beats
   recovering all of it and hoping.

Every calibration is a starting point, not a finished tune. Leaving headroom for
the next iteration is correct, not timid.

### Write one recommendation per coherent change

Records are replayed **independently against the session's current state**, not
cumulatively. Two that each pass alone can still conflict when both are accepted;
overlapping cells are flagged to the reviewer but not resolved. So:

- Split unrelated changes into separate records — a reviewer can then take one
  and leave the other.
- Keep one coherent change in one record. A curve is one record, not twelve
  cells.
- Never write two records that touch the same cells with different intent.

Where a table is written as one curve tiled across meaningless rows, state the
one curve — the adapter will refuse a record that asks for different values in
different rows, and it is right to.

### Predict something the next drive can settle

Every record carries a `prediction`, and it exists so the next log review can say
plainly whether it held. Name a channel, a condition and a number:

> "Peak PUT tracks setpoint within 5 kPa from 4000 to 5500 rpm in 3rd gear at
> IAT below 35 °C."

Not "boost should be better". The prediction is what makes a recommendation
gradeable, and a whole back-test rests on grading them.

---

## 3. Rules that get you dropped if you break them

These are enforced, not advisory. Each costs the whole record.

| Rule | What happens if you break it |
|------|------------------------------|
| Echo `provenance` verbatim | The **entire file** is refused before any replay. |
| Name the table by logical `name` **and** `id` **and** `description` | Malformed; never rendered. |
| Pair the right `id` with the right `name` | Dropped at replay — the queue must never show one table's name over another's change. |
| Cite evidence | Malformed. An empty string is not evidence. |
| Carry a prediction | Malformed. |
| Use `set`/`fill`/`paste` on an owner-locked table | Arithmetic on one is dropped. |
| Stay inside the closed sets for `risk` and `confidence` | Malformed. |
| State values in **physical units** | Never raw bytes, never scaled counts. |
| Give an axis strictly increasing values | Refused by the guard, with its own words. |

A few tables have a write path and still take **no** recommendation, because the
owning call takes a different quantity than the grid holds. The canonical one is
the maximum-allowed-airmass-setpoint table, which is labelled mg/stk and stores
kg/stk: a stated grid value is ambiguous between the two by the exact factor that
removes the limiter. Do not recommend a change to a table the brief names this
way. Say in `summary` what you would have changed and why, and leave it to the
screen that knows the units.

---

## 4. Answering well when there is nothing to say

An empty `recommendations` list is a **valid and useful answer**: "I looked, and
nothing in these logs justifies a change." It is distinguishable from a file that
failed to parse, and it is a much better answer than a low-confidence change
invented to fill the file.

Use `summary` for everything that is true but not a recommendation:

- What you would have changed if a guard did not forbid it.
- Which check you needed was in `skipped`, and what log would un-skip it.
- What the next drive should capture to make a real recommendation possible.
- A risk you can see in the calibration that no single edit addresses.

`summary` is free text and always reaches the reviewer, whether or not any record
survives replay.

---

## 5. A worked shape

The pattern, with the reasoning each field carries:

```json
{
  "id": "rec-1",
  "table": {
    "name": "<logical name from the bundle's tables section>",
    "id": "<the same entry's id>",
    "description": "<the same entry's description>"
  },
  "change": {
    "space": "base",
    "operation": "set",
    "selection": { "kind": "cells", "args": [[7, 14], [7, 15]] },
    "array": [0.600, 0.535]
  },
  "intent": "walk back the two cells R08 opened, which the new logs show under-delivering",
  "evidence": "pulls 1 and 3, 4000-4500 rpm: PUT runs 10.4 kPa under setpoint while the wastegate integral carries +17.8 %; both cells still hold their R08 values per journal entry 12",
  "risk": "safety-relevant",
  "confidence": "medium",
  "prediction": "the 4000-4500 rpm shortfall halves to about -5 kPa with the integral below +10 % in 3rd gear"
}
```

Read what each field is doing. `intent` is the one line a reviewer sees first, in
the shape an `intent=` carries in a revision script. `evidence` points at
specific rows in specific pulls *and* at the journal entry that explains why
those cells hold what they hold. `risk` is `safety-relevant` because the change
adds boost. `confidence` is `medium` because the transfer function was estimated
rather than measured. `prediction` names a channel, a band and a number, so the
next log review can grade it.

---

## 6. Before you send

- [ ] `schema_version` is the version the bundle's `reply.schema_version` names.
- [ ] `provenance` is the bundle's three fields, character for character.
- [ ] Every `table.name` exists in the bundle's `tables`, with the `id` and
      `description` that entry carries.
- [ ] Every `change.space` is a space the bundle lists.
- [ ] Every index is inside the table's `shape`.
- [ ] Every value is in the units the table reports.
- [ ] Every record cites something in **this** bundle.
- [ ] Every prediction names a channel, a condition and a number.
- [ ] No record touches a table the brief names as not-recommendable.
- [ ] No two records touch the same cells.
- [ ] `summary` says what you did not recommend, and why.

The reviewer's time is the scarce resource. Three recommendations you can defend
line by line are worth more than ten that each need checking.
