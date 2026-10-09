# ADR-0009: A self-contained HTML research report

**Status:** Accepted · Milestone 4

## Context

The research questions all have answers in a recording:
- How often do structural violations appear?
- How long do they last, and how much do they hold?
- How much of that survives fees?
- How much would a paper trader have kept?

Terminal tables answer them one command at a time. A research result also needs a shareable
artefact: something that can be attached to a write-up, opened years later, and read by someone
without Python installed.

tengine has a React dashboard backed by a live API. That suits a trading engine people interact
with. It is too heavy for a report on a finished recording.

## Decision

`karb report` writes **one HTML file** with:

- **No network access.** CSS, SVG and a small tooltip script are all inline. It opens offline in
  any browser and can be attached to an email or committed alongside a write-up.
- **A fixed set of sections:**
  - a row of headline figures;
  - a funnel from order-book snapshots to paper trades;
  - verified snapshots under each fee scenario;
  - a histogram of opportunity lifetimes;
  - a waterfall from planned to realized paper P&L;
  - a table of paper trades;
  - the screen hits;
  - method and caveats.
- **Colours chosen by job and validated, not picked by eye.**
  - The funnel stages use an ordinal blue ramp, re-stepped for dark mode rather than inverted.
  - Gains and losses use blue and red, with neutral grey totals.
  - Every ramp and pair passes the dataviz validator's colourblind-separation and contrast
    checks in both themes. The neutral grey intentionally fails the chroma floor: it is meant
    to read as grey.
- **Every number reachable without a mouse.**
  - Every bar is directly labelled.
  - Every chart has a table view underneath.
  - Tooltips enhance and never gate, and they work on keyboard focus too.
- **Exchange labels are escaped.** Tickers and titles come from the exchange and are
  HTML-escaped before rendering. Tooltips insert text with `textContent`, never `innerHTML`.
- **Empty states explain themselves.** For example: "No verified opportunity in this run…" is
  the expected result on an efficient book, not a broken chart.

## Consequences

- **A report is a pure function of the recording** (plus the fee-sensitivity replays it runs),
  so regenerating it after a code change shows the effect of the change on the same data.
- **No interactive filtering.** A different slice is a different report, which keeps the file
  static and the numbers reproducible.
- **Layout is checked, not assumed.** During development the report was rendered at desktop and
  mobile widths and checked programmatically. That check found and fixed two bugs: a clipped
  value label, and a falling waterfall label colliding with its category label.
