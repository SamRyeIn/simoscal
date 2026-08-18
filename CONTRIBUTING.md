# Contributing to simoscal

Thanks for wanting to help. This project edits calibrations that get flashed to
real engines, so the bar here is a little different from a normal library: a
contribution that is merely *probably* correct is not good enough.

Please read [`docs/BETA_GUIDE.md`](docs/BETA_GUIDE.md) first if you haven't.

---

## The contribution licence grant

**Please include this line in your pull request description:**

> I have read CONTRIBUTING.md and I grant the contribution licence described
> there.

By opening a pull request you confirm that:

1. You wrote the contribution yourself, or otherwise have the right to submit
   it, and it does not include code you are not permitted to relicense.
2. You grant Sam Ryan a perpetual, worldwide, non-exclusive, royalty-free,
   irrevocable licence to use, reproduce, modify, distribute, and sublicense
   your contribution — under this project's licence or under other terms,
   including as part of a differently-licensed work.
3. You retain copyright in your contribution. This grant is a licence, not an
   assignment; you keep the right to use your own work however you like.

**Why this is here, plainly:** simoscal is GPL-3.0. Without this grant, every
merged contribution would permanently fix the licensing of the combined work,
because relicensing would need every past contributor's agreement. Sam may want
to ship something built on this library under different terms one day, and this
keeps that an open question rather than one that gets decided by accident. If
that trade isn't
one you want to make, file an issue instead of a PR — a good bug report is worth
more than a patch anyway.

This is a plain-language summary of intent, not legal advice.

---

## What's most useful

**Bug reports.** Especially against bins and XDFs that aren't the maintainer's.
Include the preflight verdict, your revision script or a minimal snippet, the
full traceback, and your Python version and OS.

Confusing error messages count as bugs. In a tool whose job is to fail loudly, an
error that fails loudly *in an unclear way* has only done half the job.

**Datalogs from other cars.** The analysis battery has only ever been exercised
against one vehicle. Send raw CSVs plus the PID list you logged with — gear
indexing depends on the PID list, so logs without it are ambiguous.

**Profile modules for other box codes.** See below.

---

## Never do these

- **Never commit a `.bin`.** `.gitignore` excludes them; don't force-add. This
  applies to tuned bins and stock reads alike.
- **Never attach a bin to an issue or PR.**
- **Never weaken a safety gate to make something pass.** If the checksum
  verification, the raw-range guard, the float-bug guard, the shape-checked
  profile resolution, or the byte-level build audit is in your way, that is a
  conversation to have in an issue first. A PR that loosens one of these without
  that discussion will be declined regardless of how good the rest of it is.
- **Never make a table writable that the library currently refuses to write.**
  Some refusals encode a unit trap or an unresolved question, not an oversight.

---

## Contributing a profile module

This is the contribution most likely to come out of the beta, and the one with
the sharpest edges.

A profile maps logical names to the symbols in one XDF, declaring each table's
shape and units. Everything else in the library speaks logical names, so adding a
box code is writing one module and nothing else. Start from
`simoscal/tune/profiles/sc8s50.py`.

What a profile PR needs:

1. **Declared shapes for every table.** Resolution fails loud when a same-named
   symbol has different geometry in your XDF — that check is the entire safety
   argument for porting, so don't work around it.
2. **A round-trip demonstration.** Your stock bin opens, saves with no edits, and
   the output is byte-identical to the input with checksums verifying clean.
   Show the command and the result.
3. **Provenance for the XDF.** Where it came from, who authored it, which
   version. A profile is only as trustworthy as the definition under it.
4. **Explicit notes on any unit traps you found.** If a table's XDF label
   disagrees with what the ECU stores — the way `C_M_AIR_CYL_SP_MAX` — Maximum
   allowed airmass setpoint is labelled mg/stroke but stores kg/stroke — say so
   in the spec, tag it, and give it an owner. Do not leave it generically
   writable.

A merged profile is **not writable on arrival.** It resolves and it reads; a
maintainer marks it validated after review, and only then does it accept writes.
That isn't a comment on your work — a profile that has misidentified one table
looks exactly like a correct one until it writes to someone's engine, and the
author is the person least able to see it.

---

## Code conventions

- **Name every ECU table by ID *and* plain-English description**, everywhere —
  code comments, commit messages, PR text, issues. Write
  `` `C_PRS_IM_SP_MAX` — Maximum requested intake-manifold pressure setpoint ``,
  not one or the other. IDs alone are unreadable; descriptions alone are
  ambiguous. If you genuinely don't know what an ID means, say so rather than
  guessing.
- **Match the surrounding code.** Comment density, naming, and idiom vary by
  module for reasons; follow the file you're in.
- **Comments explain why, not what.** The existing modules are unusually heavy on
  rationale, especially where a decision encodes a safety property. Keep that up
   — those comments are the reason the traps stay caught.
- **Tests are not optional** for behaviour changes. Run the suite before opening
  a PR:

  ```bash
  ./.venv/bin/python -m pytest tests -q
  ```

- **No new runtime dependencies** without discussing it in an issue first.

`code_review.md` is the living review log. Check its findings index before
extending something that's already been reviewed — it will often tell you why the
code is shaped the way it is.

---

## Licence

simoscal is GPL-3.0 (see [`LICENSE`](LICENSE)). Third-party notices, and a list
of files in this repository that the GPL grant does **not** cover, are in
[`LICENSE-THIRD-PARTY`](LICENSE-THIRD-PARTY).
