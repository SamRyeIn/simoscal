# Safety brief — the general half

Facts worth knowing before recommending a change to a Simos 18 calibration.
They are here because each one has cost somebody a flash cycle, a fault code, or
a wrong conclusion from a log.

**This brief is not the safety mechanism.** Nothing here prevents a bad
recommendation. Every recommendation is replayed through the library's real edit
guards before a person sees it, and a refusal is what stops a dangerous change —
not this document. What this document does is make recommendations *start*
sensible, so fewer of them are refused for reasons that were knowable in advance.

Everything below is true of these ECUs generally. Facts about one particular
car — which of its tables store what, what its stock values are, which tables it
does not have — are rendered from that car's own profile and appear in the
bundle alongside this text. They are deliberately not restated here, because a
second copy would be a copy that drifts.

## Name every table both ways

Always give the parameter ID **and** its plain-English description:

    `IP_PUT_SP` — Pressure up throttle setpoint

Not the ID alone, and not the description alone. A reviewer reading a queue on a
tablet should never have to go look up what they are being asked to change.

## A declared maximum in an XDF is information about the definition, not the ECU

Every table in an XDF carries a `max`. That number describes what the
definition's author was willing to type into an editor. It is **not**, by itself,
a limit the ECU enforces — several tables ship from the factory holding values
far above their own declared max.

It is not proof of the opposite either. Do not reason from a declared max in
either direction:

- Where a declared max is known to be a display artifact on this car, the
  generated half of this brief names that table. Those are the only ones
  established as such.
- Where it is not named there, nobody has established what the ECU does above
  it. A recommendation that pushes a value past a declared max it has no
  information about is going somewhere unmeasured, and should say so.

The overboost threshold below is the case worth remembering: its declared max
sits only just above the value people actually target, so there is very little
room above it and no evidence about what is out there.

Where a real limit exists, it is enforced by a guard in the library, and the
refusal says so in its own words.

## Overboost faults are routed by a threshold table, not a pressure limit

When a car throws an overboost fault, the table that decides it is the
**overboost pressure-difference threshold**:

    `IP_PUT_AMP_DIF_MAX_PRS_DIF_THR` — Overpressure upstream throttle threshold
    for turbocharger overpressure diagnosis

It compares requested against achieved pressure upstream of the throttle. It is
**not** either of the maximum/limit requested intake-manifold pressure setpoint
tables. Those cap what is *asked for*; the threshold above is what decides that
what happened was far enough from what was asked to be a fault.

The two are not interchangeable and are not even the same order of magnitude:
the manifold-setpoint limits read in the hundreds of thousands of hPa, the
overboost threshold in the low thousands. Writing an overboost figure into a
manifold-setpoint limit is not a small mis-route — it is a large, unintended
*lowering* of a different limit. That mistake has been made here before.

## Reading SimosTools logs: the gear channel depends on its header

The gear column's meaning is determined by the CSV header, and the two forms
differ by one:

| Header       | Meaning                                                    |
|--------------|-------------------------------------------------------------|
| `Gear ()`    | Zero-indexed — the real gear is the logged value **plus one** |
| `Gear (gear)`| The actual gear — no offset                                  |

Check the header before quoting a gear. A log analysed under the wrong
assumption describes a pull that never happened, one gear away from the one it
did.

## Reading SimosTools logs: trim to in-gear samples before quoting power

`Calc HP (hp)` and `Calc TQ (nm)` are derived from acceleration **and weighted by
the gear ratio**. On a DSG the gear channel flips to the next ratio several
samples before the shift actually pulls the engine down, so those samples are
computed against the wrong ratio and read high — a step at the very top of every
pull that ends in an upshift.

Before quoting a peak, or plotting either channel over a pull, drop the rows
where the gear channel is not the gear the pull is attributed to. Untrimmed
peaks are inflated, and the inflation is large enough to change a conclusion.

## Evidence, not assertion

A recommendation that cannot cite the log rows or table values behind it is
rejected as malformed before anyone sees it. Cite the pull, the row range, the
channel, and the values — enough that a reader can go and look at the same
place. The same applies to the prediction: say what the next drive should show,
in terms a log can settle.
