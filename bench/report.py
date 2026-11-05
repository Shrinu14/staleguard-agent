"""
Static HTML report from a results directory (plotly charts + jinja2 template).
Written to docs/index.html so GitHub Pages can serve it. Every number comes
from the result files of real runs; nothing here is estimated.

Token numbers are the API's own `usage` counts (summed per run), so
"tokens saved vs baseline" compares real measured runs.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

from agent import lookups
from bench.freshness_bench import score
from bench.runner import load_records


def summarize_coding(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per mode, in BENCH_MODES order."""
    rows = []
    baseline_tokens = None
    for mode in lookups.BENCH_MODES:
        rs = [r for r in records if r["mode"] == mode]
        if not rs:
            continue
        total = mean(r["prompt_tokens"] + r["completion_tokens"] for r in rs)
        prompt = mean(r["prompt_tokens"] for r in rs)
        cached = mean(r["cached_tokens"] for r in rs)
        if mode == "baseline":
            baseline_tokens = total
        rows.append({
            "mode": mode, "runs": len(rs),
            "success_rate": mean(1.0 if r["success"] else 0.0 for r in rs),
            "tokens": total, "prompt_tokens": prompt,
            "completion_tokens": mean(r["completion_tokens"] for r in rs),
            "cached_share": cached / prompt if prompt else 0.0,
            "turns": mean(r["turns"] for r in rs),
            "cost_usd": mean(r["cost_usd"] for r in rs),
            "seconds": mean(r["seconds"] for r in rs),
            "saved_vs_baseline": (1 - total / baseline_tokens) if baseline_tokens else None,
        })
    return rows


def success_matrix(records: list[dict[str, Any]]) -> tuple[list[str], list[str], list[list]]:
    tasks = sorted({r["task"] for r in records})
    modes = [m for m in lookups.BENCH_MODES if any(r["mode"] == m for r in records)]
    z = []
    for t in tasks:
        row = []
        for m in modes:
            rs = [r for r in records if r["task"] == t and r["mode"] == m]
            row.append(mean(1.0 if r["success"] else 0.0 for r in rs) if rs else None)
        z.append(row)
    return tasks, modes, z


def _charts(rows: list[dict], records: list[dict], fresh: list[dict]) -> dict[str, str]:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    def html(fig: Any) -> str:
        fig.update_layout(template="plotly_white", margin={"l": 50, "r": 30, "t": 50, "b": 50},
                          height=420, font={"family": "Inter, system-ui, sans-serif"})
        return fig.to_html(full_html=False, include_plotlyjs=False)

    charts: dict[str, str] = {}
    if rows:
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        modes = [r["mode"] for r in rows]
        fig.add_bar(x=modes, y=[r["prompt_tokens"] for r in rows], name="prompt tokens",
                    marker_color="#3b6fd4")
        fig.add_bar(x=modes, y=[r["completion_tokens"] for r in rows],
                    name="completion tokens", marker_color="#9db8ec")
        fig.add_scatter(x=modes, y=[r["success_rate"] * 100 for r in rows],
                        name="success rate (%)", mode="lines+markers",
                        line={"color": "#d4543b", "width": 3}, secondary_y=True)
        fig.update_layout(barmode="stack", title="Tokens per task run (mean) and success rate")
        fig.update_yaxes(title_text="tokens", secondary_y=False)
        fig.update_yaxes(title_text="success %", range=[0, 105], secondary_y=True)
        charts["tokens"] = html(fig)

        tasks, mcols, z = success_matrix(records)
        fig = go.Figure(go.Heatmap(z=[[None if v is None else v * 100 for v in row] for row in z],
                                   x=mcols, y=tasks, colorscale="RdYlGn", zmin=0, zmax=100,
                                   colorbar={"title": "% pass"}))
        fig.update_layout(title="Success per task and mode (% of runs graded as passing)")
        charts["matrix"] = html(fig)
    if fresh:
        groups = {"stale (expect changed)": "changed", "current (expect supported)": "supported"}
        fig = go.Figure()
        for status, color in (("changed", "#d4543b"), ("supported", "#3ba55c"),
                              ("unverifiable", "#c9a227")):
            fig.add_bar(x=list(groups), name=status, marker_color=color,
                        y=[sum(1 for r in fresh if r["expected"] == exp and r["status"] == status)
                           for exp in groups.values()])
        fig.update_layout(barmode="stack", title="Freshness verdicts by ground truth")
        charts["freshness"] = html(fig)
    return charts


def build_report(results_dir: Path, out_file: Path, template: Path) -> Path:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    coding = load_records(results_dir / lookups.BENCH_CODING_RESULTS)
    fresh = load_records(results_dir / lookups.BENCH_FRESHNESS_RESULTS)
    meta_path = results_dir / lookups.BENCH_META_FILE
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    rows = summarize_coding(coding)
    env = Environment(loader=FileSystemLoader(template.parent),
                      autoescape=select_autoescape(["html", "j2"]))
    page = env.get_template(template.name).render(
        meta=meta, rows=rows, coding=coding, fresh=fresh,
        fresh_score=score(fresh) if fresh else None,
        charts=_charts(rows, coding, fresh), modes=lookups.BENCH_MODES,
        plotly_cdn=lookups.PLOTLY_CDN, results_dir=results_dir.as_posix(),
        generated=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"))
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(page, encoding="utf-8")
    return out_file
