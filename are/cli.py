"""Command-line interface for app-reverse-engineer."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from .config import load_config, save_config, ensure_dirs, CONFIG_FILE
from .setup import run_setup as setup_run, doctor_report
from .discovery import discover
from .analyzers.native import NativeAnalyzer
from .analyzers.electron import ElectronAnalyzer
from .analyzers.javascript import JavaScriptAnalyzer
from .report import finalize
from .history import save_investigation, list_investigations, get_investigation
from .mcp_server import run as mcp_run

app = typer.Typer(
    name="are",
    help="app-reverse-engineer: local offline-first reverse engineering for AI agents.",
)

ANALYZERS = [NativeAnalyzer(), ElectronAnalyzer(), JavaScriptAnalyzer()]


def _load_cfg() -> dict:
    return load_config()


def _investigate(path: str, question: str, cfg: dict) -> tuple[dict, object, str]:
    from .analyzers.base import AnalysisResult
    info = discover(path)
    safety = cfg.get("safety", {})
    result = None
    for an in ANALYZERS:
        if an.supports(info.__dict__):
            result = an.run(info.__dict__, question, safety)
            break
    if result is None:
        result = AnalysisResult(kind="unknown", info=info.__dict__)
        result.notes.append("No analyzer matched this target.")
    report_md = finalize(cfg, question, info.__dict__, result)
    return info.__dict__, result, report_md


@app.command()
def setup(non_interactive: bool = typer.Option(False, "--non-interactive", "-y", help="Do not prompt; skip installs.")):
    """Run the guided setup (rerunnable). Checks tools and asks before changing anything."""
    cfg = _load_cfg()
    setup_run(cfg, non_interactive=non_interactive)


@app.command()
def doctor():
    """Check the local environment and show what is missing."""
    cfg = _load_cfg()
    report = doctor_report(cfg)
    print("=== app-reverse-engineer doctor ===")
    print(f"Config file: {report['config_file']} (exists={report['config_exists']})")
    print()
    print("System tools:")
    for label, ok in report["system_tools"].items():
        print(f"  [{'OK ' if ok else 'MISS'}] {label}")
    if report["missing_system"]:
        print("  Install the missing system tools yourself (this tool does not run sudo/apt).")
    print()
    print("Python packages:")
    for label, ok in report["python_packages"].items():
        print(f"  [{'OK ' if ok else 'MISS'}] {label}")
    if report["missing_python"]:
        print("  Run: are setup   (it will ask before installing with pip)")
    print()
    print("LLM configured:", report["llm_configured"])
    print("Safety:", report["safety"])
    print()
    print("Ready:", report["ready"])


@app.command()
def analyze(path: str):
    """Quick discovery: what kind of app/binary is this?"""
    cfg = _load_cfg()
    try:
        info = discover(path)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)
    import json
    print(json.dumps(info.to_dict(), indent=2))


@app.command()
def investigate(
    path: str,
    question: str = typer.Option("How does this app work?", "--question", "-q", help="Feature to investigate."),
    save: bool = typer.Option(True, "--save/--no-save", help="Save to investigation history."),
):
    """Reverse engineer a feature and print a plain-English report with evidence."""
    cfg = _load_cfg()
    ensure_dirs()
    try:
        info, result, report_md = _investigate(path, question, cfg)
    except FileNotFoundError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(1)

    print(report_md)

    if save:
        inv_dir = save_investigation(None, question, info, result, report_md)
        typer.secho(f"\nSaved to history: {inv_dir}", fg=typer.colors.GREEN)


@app.command()
def history(
    inv_id: Optional[str] = typer.Argument(None, help="Investigation id to show (omit to list)."),
):
    """List past investigations, or show one by id."""
    ensure_dirs()
    if inv_id is None:
        items = list_investigations()
        if not items:
            print("No investigations yet.")
            return
        print("=== Investigation history ===")
        for it in items:
            print(f"{it['id']} | {it['created_at']} | {it['app_type']} | {it['question']}")
            print(f"    target: {it['target']}")
        return
    data = get_investigation(inv_id)
    if not data:
        typer.secho(f"Investigation not found: {inv_id}", fg=typer.colors.RED)
        raise typer.Exit(1)
    print(data.get("report", "(no report)"))


@app.command()
def mcp():
    """Start the MCP server (stdio) for AI agents."""
    cfg = _load_cfg()
    typer.echo("Starting app-reverse-engineer MCP server on stdio...")
    mcp_run(cfg)


if __name__ == "__main__":
    app()
