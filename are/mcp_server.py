"""MCP (Model Context Protocol) server for app-reverse-engineer.

Exposes tools so an AI agent can ask questions like
'How does search work in this app?' and get a plain-English investigation
with evidence and a recreation guide.

Uses the official `mcp` package when available, otherwise falls back to a
minimal stdlib stdio JSON-RPC server so the tool works offline without extra
pip installs.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .config import load_config, ensure_dirs
from .discovery import discover
from .analyzers.native import NativeAnalyzer
from .analyzers.javascript import JavaScriptAnalyzer
from .analyzers.electron import ElectronAnalyzer
from .report import finalize
from .history import save_investigation, list_investigations, get_investigation

ANALYZERS = [NativeAnalyzer(), ElectronAnalyzer(), JavaScriptAnalyzer()]


def _run_investigation(path: str, question: str, cfg: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    info = discover(path)
    safety = cfg.get("safety", {})
    result = None
    for an in ANALYZERS:
        if an.supports(info.__dict__ if hasattr(info, "__dict__") else info):
            result = an.run(info.__dict__ if hasattr(info, "__dict__") else info, question, safety)
            break
    if result is None:
        from .analyzers.base import AnalysisResult
        result = AnalysisResult(kind="unknown", info=info.__dict__ if hasattr(info, "__dict__") else info)
        result.notes.append("No analyzer matched this target.")

    report_md = finalize(cfg, question, info.__dict__ if hasattr(info, "__dict__") else info, result)
    inv_dir = save_investigation(None, question, info.__dict__ if hasattr(info, "__dict__") else info, result, report_md)
    return {
        "investigation_dir": inv_dir,
        "report": report_md,
        "app_type": result.kind,
        "evidence_count": len(result.evidence),
    }


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_analyze_app(arguments: dict[str, Any], cfg: dict[str, Any]) -> str:
    path = arguments["path"]
    try:
        info = discover(path)
        return json.dumps(info.to_dict(), indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


def tool_investigate_feature(arguments: dict[str, Any], cfg: dict[str, Any]) -> str:
    path = arguments["path"]
    question = arguments.get("question", "How does this app work?")
    try:
        out = _run_investigation(path, question, cfg)
        return json.dumps(out, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


def tool_list_history(arguments: dict[str, Any], cfg: dict[str, Any]) -> str:
    try:
        items = list_investigations()
        return json.dumps(items, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


def tool_get_history(arguments: dict[str, Any], cfg: dict[str, Any]) -> str:
    inv_id = arguments["id"]
    try:
        data = get_investigation(inv_id)
        if not data:
            return json.dumps({"error": f"investigation not found: {inv_id}"})
        return json.dumps(data, indent=2)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": str(exc)})


def tool_doctor(arguments: dict[str, Any], cfg: dict[str, Any]) -> str:
    from .setup import doctor_report
    return json.dumps(doctor_report(cfg), indent=2)


TOOLS = {
    "analyze_app": tool_analyze_app,
    "investigate_feature": tool_investigate_feature,
    "list_history": tool_list_history,
    "get_history": tool_get_history,
    "doctor": tool_doctor,
}


def call_tool(name: str, arguments: dict[str, Any], cfg: dict[str, Any]) -> str:
    if name not in TOOLS:
        return json.dumps({"error": f"unknown tool: {name}"})
    return TOOLS[name](arguments or {}, cfg)


# ---------------------------------------------------------------------------
# FastMCP (preferred) or stdlib fallback
# ---------------------------------------------------------------------------

def _try_fastmcp(cfg: dict[str, Any]):
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except Exception:
        return None

    mcp = FastMCP("app-reverse-engineer")

    @mcp.tool()
    def analyze_app(path: str) -> str:
        """Discover what an app/binary is (type, kind, source files, package.json)."""
        return tool_analyze_app({"path": path}, cfg)

    @mcp.tool()
    def investigate_feature(path: str, question: str = "How does this app work?") -> str:
        """Reverse engineer a feature and return a plain-English report with evidence."""
        return tool_investigate_feature({"path": path, "question": question}, cfg)

    @mcp.tool()
    def list_history() -> str:
        """List past investigations."""
        return tool_list_history({}, cfg)

    @mcp.tool()
    def get_history(id: str) -> str:
        """Get a past investigation by id."""
        return tool_get_history({"id": id}, cfg)

    @mcp.tool()
    def doctor() -> str:
        """Check the local environment (tools, packages, safety settings)."""
        return tool_doctor({}, cfg)

    return mcp


# Minimal stdlib JSON-RPC 2.0 stdio server (MCP-lite) used when `mcp` is absent.
def _run_stdio_fallback(cfg: dict[str, Any]) -> None:
    def respond(result=None, error=None, id_=None):
        resp: dict[str, Any] = {"jsonrpc": "2.0"}
        if id_ is not None:
            resp["id"] = id_
        if error is not None:
            resp["error"] = error
        else:
            resp["result"] = result
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()

    def handle_notification(method, params):
        # MCP notifications (e.g., initialized) - just ack
        pass

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            respond(error={"code": -32700, "message": "parse error"})
            continue
        method = msg.get("method")
        params = msg.get("params") or {}
        id_ = msg.get("id")

        if method == "initialize":
            respond(result={
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "app-reverse-engineer", "version": "0.1.0"},
            }, id_=id_)
        elif method == "initialized":
            handle_notification(method, params)
        elif method == "tools/list":
            respond(result={
                "tools": [
                    {"name": "analyze_app", "description": "Discover what an app/binary is."},
                    {"name": "investigate_feature", "description": "Reverse engineer a feature into plain English."},
                    {"name": "list_history", "description": "List past investigations."},
                    {"name": "get_history", "description": "Get a past investigation by id."},
                    {"name": "doctor", "description": "Check local environment."},
                ]
            }, id_=id_)
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                text = call_tool(name, arguments, cfg)
                respond(result={"content": [{"type": "text", "text": text}]}, id_=id_)
            except Exception as exc:  # noqa: BLE001
                respond(error={"code": -32603, "message": str(exc)}, id_=id_)
        else:
            respond(error={"code": -32601, "message": f"method not found: {method}"}, id_=id_)


def run(cfg: dict[str, Any] | None = None) -> None:
    cfg = cfg or load_config()
    ensure_dirs()
    mcp = _try_fastmcp(cfg)
    if mcp is not None:
        mcp.run()
    else:
        _run_stdio_fallback(cfg)
