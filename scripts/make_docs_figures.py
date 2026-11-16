"""Draw the explanatory figures used on the documentation site.

Figures 1 and 2 use the zircon test fixture (real Qtegra Rich data from the
Zircon_04Nov26 session); figure 3 uses simulated Poisson counts. Output goes to
``docs/images/`` as SVG. Run from the repository root:

    .venv/bin/python scripts/make_docs_figures.py
"""

from pathlib import Path
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from UPbRPy.qtegra import counting_dwell, read_rich_csv  # noqa: E402

RICH = ROOT / "tests/fixtures/qtegra/Zircon_demo/Zircon_demo_Rich.csv"
OUT = ROOT / "docs/images"

COLORS = {"29Si": "#2e7d32", "206Pb": "#8d6e00", "207Pb": "#c77700", "238U": "#1e4fd8"}
plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": "#e6e6e6",
    "grid.linewidth": 0.6,
    "svg.fonttype": "none",
    "svg.hashsalt": "upbrpy-docs",
})


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, bbox_inches="tight", metadata={"Date": None})
    plt.close(fig)
    print("wrote", OUT / name)


def signal_start(acq, names):
    """First sweep where 238U rises above 50x its first-ten-sweep median."""
    u = acq.y[:, names.index("238U")]
    blank = max(np.nanmedian(u[:10]), 1.0)
    return int(np.argmax(u > 50 * blank))


def figure_acquisition(session):
    acq = next(a for a in session.acquisitions if a.label == "S513_628")
    names = [c.name for c in acq.channels]
    start = signal_start(acq, names)
    t = acq.time[:, 0]
    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    for ch in ("29Si", "238U", "206Pb", "207Pb"):
        y = acq.y[:, names.index(ch)].astype(float)
        ax.plot(t, np.where(y > 0, y, np.nan), "o-", ms=2.5, lw=1, color=COLORS[ch], label=ch)
    edge = 0.5 * (t[start - 1] + t[start])
    ax.axvspan(t[0], edge, color="#9e9e9e", alpha=0.12, lw=0)
    ax.axvspan(edge, t[-1], color="#ffb74d", alpha=0.10, lw=0)
    ax.text(0.5 * (t[0] + edge), 3e7, "gas blank\n(laser off)", ha="center", va="top", fontsize=9)
    ax.text(0.5 * (edge + t[-1]), 3e7, "sample signal (laser on)", ha="center", va="top", fontsize=9)
    k = start + 12
    ax.axvline(t[k], color="#555", lw=0.8, ls=":")
    ax.annotate("one sweep: every isotope\nmeasured once, ≈0.4 s",
                xy=(t[k], 3e3), xytext=(t[k] - 4.9, 1.2e2),
                fontsize=8.5, arrowprops=dict(arrowstyle="->", color="#555"))
    ax.set_yscale("log")
    ax.set_ylim(5, 5e7)
    ax.set_xlim(t[0], t[-1])
    ax.set_xlabel("Time since the start of the acquisition (s)")
    ax.set_ylabel("Count rate (cps, log scale)")
    ax.set_title("One acquisition: reference zircon S513, spot 628", loc="left", fontsize=10.5)
    ax.legend(ncol=4, fontsize=8.5, loc="upper center", bbox_to_anchor=(0.5, -0.2), frameon=False)
    save(fig, "acquisition-anatomy.svg")


def figure_count_steps(session):
    dwell = counting_dwell(session)["206Pb"][0]
    step = 1.0 / dwell
    values = []
    for acq in session.acquisitions:
        names = [c.name for c in acq.channels]
        start = signal_start(acq, names)
        values.append(acq.y[:max(start - 2, 0), names.index("206Pb")])
    values = np.concatenate(values).astype(float)
    values = values[np.isfinite(values) & (values < 9.5 * step)]
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    bins = np.arange(-0.5, 9.5 * step, 1.0)
    ax.hist(values, bins=bins, color="#8d6e00", lw=0)
    for n in range(0, 10):
        ax.axvline(n * step, color="#8d6e00", alpha=0.15, lw=6, zorder=0)
    ax.set_xticks([n * step for n in range(10)])
    ax.set_xticklabels([f"{n * step:.0f}\n{n} ion{'s' if n != 1 else ''}" for n in range(10)], fontsize=8)
    ax.set_xlabel("206Pb count rate in the gas blank (cps), and the number of ions it means")
    ax.set_ylabel("Number of sweeps")
    ax.set_title(f"Readings only fall on steps of {step:.2f} cps, so the dwell time is "
                 f"1 / {step:.2f} = {dwell:.3f} s", loc="left", fontsize=10)
    save(fig, "count-steps.svg")


def figure_error_bars():
    rng = np.random.default_rng(3)
    dwell, rate206, true_ratio, n = 0.075, 10_000.0, 0.06, 30
    n206 = rng.poisson(rate206 * dwell, n)
    n207 = rng.poisson(rate206 * dwell * true_ratio, n)
    ratio = n207 / n206
    rel_new = np.sqrt(1 / n207 + 1 / n206)
    rel_old = np.sqrt(1 / (n207 / dwell) + 1 / (n206 / dwell))
    x = np.arange(1, n + 1)
    fig, axes = plt.subplots(1, 2, figsize=(7.6, 3.2), sharey=True)
    panels = (
        (axes[0], rel_old, "Old: count rate used as number of ions", "#c62828"),
        (axes[1], rel_new, "Now: count rate × dwell time", "#1565c0"),
    )
    for ax, rel, title, color in panels:
        err = 2 * ratio * rel
        hit = np.abs(ratio - true_ratio) <= err
        ax.axhline(true_ratio, color="#333", lw=1, ls="--")
        ax.errorbar(x, ratio, yerr=err, fmt="o", ms=3.5, color=color, ecolor=color, elinewidth=1, capsize=2)
        ax.set_title(title, fontsize=9.5, loc="left")
        ax.text(0.02, 0.97, f"{hit.mean():.0%} of error bars (2σ) reach the true value",
                transform=ax.transAxes, fontsize=8.5, va="top")
        ax.set_ylim(0.02, 0.115)
        ax.set_xlabel("Sweep")
    axes[0].set_ylabel("207Pb/206Pb")
    axes[1].text(n + 0.6, true_ratio, "true\nvalue", fontsize=8, va="center")
    fig.suptitle("Simulated sweeps: 10,000 cps of 206Pb, dwell 0.075 s (750 ions per sweep)",
                 x=0.02, ha="left", fontsize=10)
    fig.tight_layout()
    save(fig, "error-bars-before-after.svg")


def main():
    session = read_rich_csv(RICH)
    figure_acquisition(session)
    figure_count_steps(session)
    figure_error_bars()


if __name__ == "__main__":
    main()
