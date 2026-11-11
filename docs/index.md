---
hide:
  - navigation
  - toc
---

# UPbRPy documentation

UPbRPy turns laser-ablation ICP-MS measurements of U, Th and Pb isotopes into
isotope ratios, dates and trace-element concentrations, and records every step so
a reduction can be repeated. This site explains what UPbRPy reads, what it keeps
from each file, and what it writes.

<div class="grid cards" markdown>

-   :material-lightbulb-on-outline: **Start here: key ideas**

    ---

    What an acquisition, a sweep, a channel and a count rate are, in plain
    words, with one picture of how a measurement is laid out.

    [:octicons-arrow-right-24: Key ideas](guide/key-ideas.md)

-   :material-counter: **Counts, count rates and dwell time**

    ---

    Why the instrument's "counts per second" cannot be used directly as a
    number of ions, and what that means for uncertainties.

    [:octicons-arrow-right-24: Counting and dwell time](guide/counting-and-dwell.md)

-   :material-file-export-outline: **Which Qtegra export should I use?**

    ---

    A short decision guide: what each export contains and what you lose
    without it.

    [:octicons-arrow-right-24: Choosing an export](guide/choosing-an-export.md)

-   :material-book-open-variant: **Reference**

    ---

    The exact file layouts, data structures, reference-material files,
    project files and export tables, field by field.

    [:octicons-arrow-right-24: Data model reference](data-model/index.md)

</div>

## How data moves through UPbRPy

```mermaid
flowchart TD
    A["Qtegra export<br/>Rich CSV and/or per-spot files"] --> B["Read<br/>one session of acquisitions"]
    B --> C["Experiment tables<br/>signal per sweep, sample names, timing"]
    C --> D["Review<br/>choose gas blank and signal windows"]
    G["Reference materials<br/>TOML library"] --> E
    D --> E["Reduce<br/>ratios, corrections, uncertainties"]
    E --> F["Results<br/>spot means, depth profiles, export tables"]
    D -. saved as .-> H[".upbr project"]
    F -. saved as .-> I["Reduction bundle"]
```

!!! info "Recent change that affects uncertainties (2026-11-11)"
    Before this date, UPbRPy calculated the counting uncertainty of each sweep
    as if the instrument's count rate were the number of ions detected. That
    made sweep-level uncertainties several times too small. Spot averages were
    not affected. [Read what changed and why](guide/counting-and-dwell.md).

The source code is on [GitHub](https://github.com/duserzym/UPbRPy).
