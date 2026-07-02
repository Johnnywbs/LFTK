"""Two-way ANOVA over the CSV produced by factorial_experiment.py.

factor_a = docker vs k3s (orchestration abstraction)
factor_b = light vs heavy (load)

Run as:

    python3 k8s_lft/experiment/anova.py --csv results/factorial/results.csv \\
        --out results/factorial/anova
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from statsmodels.formula.api import ols
from statsmodels.stats.anova import anova_lm

# Measured under both factor_b levels (dash always runs) -> full 2-way ANOVA.
TWO_WAY_METRICS = [
    "latency_ms_mean",
    "dash_bitrate_kbps_mean",
    "dash_stall_rate",
    "cpu_pct_server",
    "cpu_pct_client",
]

# iperf only runs when factor_b == "heavy" (see factorial_experiment.py),
# so these two columns are NaN for every "light" row by construction --
# factor_b has a single level within the data that actually has these
# metrics, so `metric ~ C(factor_a) * C(factor_b)` has no valid interaction
# term to fit (this crashes statsmodels with "must have at least one row in
# constraint matrix" if attempted -- confirmed while testing this script end
# to end). These are analyzed as a one-way ANOVA on factor_a, restricted to
# the heavy-load subset.
HEAVY_ONLY_METRICS = ["throughput_mbps", "iperf_retransmits"]

METRIC_COLUMNS = TWO_WAY_METRICS + HEAVY_ONLY_METRICS


def run_two_way_anova(df: pd.DataFrame, metric_col: str) -> pd.DataFrame:
    clean = df.dropna(subset=[metric_col, "factor_a", "factor_b"])
    model = ols(f"{metric_col} ~ C(factor_a) * C(factor_b)", data=clean).fit()
    return anova_lm(model, typ=2)


def run_one_way_anova(df: pd.DataFrame, metric_col: str, group_col: str = "factor_a") -> pd.DataFrame:
    clean = df.dropna(subset=[metric_col, group_col])
    model = ols(f"{metric_col} ~ C({group_col})", data=clean).fit()
    return anova_lm(model, typ=2)


def run_all_anovas(csv_path: Path, out_dir: Path) -> dict[str, pd.DataFrame]:
    df = pd.read_csv(csv_path)
    df = df[df["status"] == "ok"]
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tables = {}
    for metric in TWO_WAY_METRICS:
        if metric not in df.columns or df[metric].dropna().empty:
            print(f"skip {metric}: no usable data")
            continue
        table = run_two_way_anova(df, metric)
        table.to_csv(out_dir / f"anova_{metric}.csv")
        tables[metric] = table
        print(f"\n=== {metric} (two-way: factor_a x factor_b) ===")
        print(table)

    heavy_df = df[df["factor_b"] == "heavy"]
    for metric in HEAVY_ONLY_METRICS:
        if metric not in heavy_df.columns or heavy_df[metric].dropna().empty:
            print(f"skip {metric}: no usable data")
            continue
        table = run_one_way_anova(heavy_df, metric)
        table.to_csv(out_dir / f"anova_{metric}_factor_a_only.csv")
        tables[metric] = table
        print(f"\n=== {metric} (one-way: factor_a only, heavy-load subset -- "
              f"iperf only runs under heavy load) ===")
        print(table)

    return tables


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Two-way ANOVA over factorial_experiment.py results")
    parser.add_argument("--csv", dest="csv_path", required=True)
    parser.add_argument("--out", default="results/factorial/anova")
    args = parser.parse_args(argv)
    run_all_anovas(Path(args.csv_path), Path(args.out))


if __name__ == "__main__":
    main()
