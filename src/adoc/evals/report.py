"""Render `evals.runner.SuiteResult`(s) to markdown + JSON (PLAN.md
"Self-evaluation" / "Model rotation": "`adoc eval --candidate <provider:model>`
runs the full suite against the incumbent binding and emits a comparison
report").

`write_report` writes both a `.md` and a `.json` file per invocation
(`<out_dir>/<suite>-<label>.md`/`.json`, where `<label>` is `report` for a
single run or `comparison` when an incumbent+candidate pair is given) and
returns the markdown text.
"""

from __future__ import annotations

import json
from pathlib import Path

from adoc.evals.runner import SuiteResult


def render_markdown(result: SuiteResult) -> str:
    lines = [
        f"# Eval suite: {result.suite}",
        "",
        f"- Binding: {result.binding_label}",
        f"- Cases: {sum(1 for c in result.cases if c.passed)}/{len(result.cases)} passed"
        f" ({result.pass_rate:.0%})",
        "",
        "## Metrics",
        "",
    ]
    if result.metrics:
        lines.append("| Metric | Value |")
        lines.append("|---|---|")
        for metric in result.metrics:
            lines.append(f"| {metric.name} | {metric.value:.4g} |")
    else:
        lines.append("_No metrics reported._")
    lines.append("")

    failures = [c for c in result.cases if not c.passed]
    if failures:
        lines.append("## Failing cases")
        lines.append("")
        for case in failures:
            lines.append(f"- `{case.case_id}`: {case.detail}")
        lines.append("")

    return "\n".join(lines) + "\n"


def render_comparison_markdown(incumbent: SuiteResult, candidate: SuiteResult) -> str:
    """Render an incumbent-vs-candidate comparison table for one suite.
    Both `SuiteResult`s must be from the same suite (`incumbent.suite ==
    candidate.suite`), typically produced by two `run_suite` calls — one
    without `--candidate`, one with.
    """
    if incumbent.suite != candidate.suite:
        raise ValueError(
            f"cannot compare results from different suites: "
            f"{incumbent.suite!r} vs {candidate.suite!r}"
        )

    lines = [
        f"# Eval suite comparison: {incumbent.suite}",
        "",
        f"- Incumbent binding: {incumbent.binding_label}",
        f"- Candidate binding: {candidate.binding_label}",
        "",
        "| Metric | Incumbent | Candidate | Delta |",
        "|---|---|---|---|",
    ]
    names = sorted({m.name for m in incumbent.metrics} | {m.name for m in candidate.metrics})
    for name in names:
        inc_value = incumbent.metric(name)
        cand_value = candidate.metric(name)
        inc_str = f"{inc_value:.4g}" if inc_value is not None else "n/a"
        cand_str = f"{cand_value:.4g}" if cand_value is not None else "n/a"
        if inc_value is not None and cand_value is not None:
            delta = f"{cand_value - inc_value:+.4g}"
        else:
            delta = "n/a"
        lines.append(f"| {name} | {inc_str} | {cand_str} | {delta} |")
    lines.append("")
    lines.append(
        f"Pass rate: incumbent {incumbent.pass_rate:.0%} -> candidate {candidate.pass_rate:.0%}"
    )
    lines.append("")
    return "\n".join(lines) + "\n"


def write_report(result: SuiteResult, out_dir: Path) -> Path:
    """Write `result` as `<out_dir>/<suite>-report.{md,json}` and return
    the markdown file's path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{result.suite}-report.md"
    json_path = out_dir / f"{result.suite}-report.json"
    md_path.write_text(render_markdown(result), encoding="utf-8")
    json_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return md_path


def write_comparison_report(incumbent: SuiteResult, candidate: SuiteResult, out_dir: Path) -> Path:
    """Write an incumbent-vs-candidate comparison as
    `<out_dir>/<suite>-comparison.{md,json}` and return the markdown
    file's path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{incumbent.suite}-comparison.md"
    json_path = out_dir / f"{incumbent.suite}-comparison.json"
    md_path.write_text(render_comparison_markdown(incumbent, candidate), encoding="utf-8")
    payload = {
        "incumbent": incumbent.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return md_path


__all__ = [
    "render_comparison_markdown",
    "render_markdown",
    "write_comparison_report",
    "write_report",
]
