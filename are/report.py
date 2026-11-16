"""Build a plain-English investigation report from collected evidence."""

from __future__ import annotations

from typing import Any

from .llm import is_enabled, polish


def build_report(question: str, info: dict[str, Any], result) -> str:
    lines: list[str] = []
    kind = result.kind

    lines.append(f"# Investigation: {info.get('name', 'unknown')}")
    lines.append("")
    lines.append(f"**Question:** {question}")
    lines.append(f"**Target:** {info.get('path')}")
    lines.append(f"**App type:** {kind}")
    lines.append(f"**File type:** {info.get('file_type') or 'directory'}")
    lines.append("")

    # ---- Plain English "how it works" summary ----
    lines.append("## How the feature works (plain English)")
    lines.append("")
    summary = _plain_english_summary(question, info, result)
    lines.append(summary)
    lines.append("")

    # ---- Evidence ----
    lines.append("## Evidence used")
    lines.append("")
    if result.evidence:
        for i, ev in enumerate(result.evidence, 1):
            lines.append(f"### {i}. {ev.title}")
            lines.append("")
            lines.append(f"- **Source:** {ev.source or 'analysis'}")
            if ev.file:
                lines.append(f"- **File:** {ev.file}" + (f":{ev.line}" if ev.line is not None else ""))
            lines.append(f"- **Relevance:** {int(ev.score * 100)}%")
            lines.append("")
            lines.append("```")
            lines.append(ev.detail)
            lines.append("```")
            lines.append("")
    else:
        lines.append("No specific evidence was found. Try a more specific question or point to a different target.")
        lines.append("")

    # ---- Analyzer notes ----
    if result.notes:
        lines.append("## Analyst notes")
        lines.append("")
        for n in result.notes:
            lines.append(f"- {n}")
        lines.append("")

    # ---- Recreation guide ----
    lines.append("## Recreate a similar feature in your project")
    lines.append("")
    lines.append(_recreation_guide(kind, info, result))
    lines.append("")

    # ---- Optional LLM polish ----
    return "\n".join(lines)


def _plain_english_summary(question: str, info: dict[str, Any], result) -> str:
    kind = result.kind
    pj = info.get("package_json") or {}
    deps = {**pj.get("dependencies", {}), **pj.get("devDependencies", {})}

    if kind == "electron":
        has_ipc = any(e.kind == "source-snippet" and "ipc" in (e.title or "").lower() for e in result.evidence)
        has_bridge = any("contextbridge" in (e.detail or "").lower() for e in result.evidence)
        txt = (
            "This looks like an Electron app. Its search (or feature) logic is split between a main process and a "
            "renderer process. The main process creates windows and can handle system-level tasks; the renderer shows "
            "the UI. If you saw `ipcMain` / `ipcRenderer` or `contextBridge`, that means the two sides talk to each "
            "through messages. A search feature typically works like this: the renderer sends a query over IPC, the "
            "main process runs the search (or calls a native module), and sends results back to the renderer to display."
        )
        if has_bridge:
            txt += " The preload script exposes some of those functions directly to the renderer (contextBridge), which is where the UI likely calls into search."
        return txt

    if kind == "javascript":
        net = any("network call" in (e.title or "") for e in result.evidence)
        base = (
            "This is a JavaScript/Node project. Based on the source, the feature likely reads input, runs some "
            "matching or filtering logic, and returns results. "
        )
        if net:
            base += "It also appears to make network requests (fetch/axios), so search may hit a remote API rather than running locally."
        if deps:
            base += f" Notable dependencies: {', '.join(list(deps.keys())[:8])}."
        return base

    if kind == "native":
        syms = [e for e in result.evidence if e.kind == "symbol"]
        strs = [e for e in result.evidence if e.kind == "string"]
        txt = (
            "This is a native compiled binary. Reverse evidence comes from readable strings, imported/shared-library "
            "symbols, and ELF sections. Strings that mention your keyword suggest the binary handles that feature "
            "directly (for example, building a query or calling an API). Imported symbols show which libraries it "
            "relies on (networking, database, etc.). Without symbols or a debugger, this is a best-guess interpretation."
        )
        if syms:
            txt += f" Found {len(syms)} relevant symbol(s)."
        if strs:
            txt += f" Found {len(strs)} string hint(s)."
        return txt

    return "I could not determine the app type with confidence. Point the tool at a folder with package.json or at a compiled binary and try again."


def _recreation_guide(kind: str, info: dict[str, Any], result) -> str:
    if kind == "electron":
        return (
            "1. Add Electron (`npm install --save-dev electron`).\n"
            "2. In `main.js`, create a `BrowserWindow` and set up an `ipcMain.handle('search', ...)` handler that runs your search.\n"
            "3. In `preload.js`, use `contextBridge.exposeInMainWorld` to expose `window.searchApi.search(q)` to the renderer.\n"
            "4. In the renderer (React/Vue/plain JS), call `window.searchApi.search('...')` and render the results.\n"
            "Example handler skeleton:\n"
            "```js\n"
            "ipcMain.handle('search', async (event, query) => {\n"
            "  const results = await runSearch(query);\n"
            "  return results;\n"
            "});\n"
            "```"
        )
    if kind == "javascript":
        return (
            "1. Create a search function that takes a query and a list of items.\n"
            "2. Filter items where a field includes the query (case-insensitive).\n"
            "3. If you saw network calls, replace the local filter with a `fetch('/api/search?q=' + query)` call.\n"
            "4. Return and render the matches.\n"
            "```js\n"
            "function search(items, query) {\n"
            "  const q = query.toLowerCase();\n"
            "  return items.filter(i => String(i.title || i.name || '').toLowerCase().includes(q));\n"
            "}\n"
            "```"
        )
    if kind == "native":
        return (
            "1. Reproduce the feature in a small C/Rust/Go program.\n"
            "2. Use the same libraries found in `ldd` output (e.g., libcurl for HTTP, sqlite3 for search).\n"
            "3. Parse input, run the search/matching logic, and print or return results.\n"
            "4. Compare your program's strings/symbols with the original to confirm behavior."
        )
    return "Inspect the evidence above, then write a small prototype that mirrors the observed behavior."


def finalize(cfg: dict[str, Any], question: str, info: dict[str, Any], result) -> str:
    md = build_report(question, info, result)
    if is_enabled(cfg):
        try:
            polished = polish(cfg, question, md)
            if polished:
                md = polished
        except Exception:
            pass
    return md
