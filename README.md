# app-reverse-engineer

A **local, offline-first** command-line toolkit that lets an AI agent reverse engineer apps and native binaries, explain how a feature works in plain English, and show the evidence it used — plus a way to recreate a similar feature in your own project.

Works from the terminal **and** as an MCP (Model Context Protocol) server so AI agents can call it.

```
are investigate /path/to/app --question "How does search work in this app?"
```

## What it does

1. **Discovers** what you're pointing at: native binary (ELF/PE/Mach-O), Electron app, Node/JS project, or plain folder.
2. **Statically inspects** it — strings, dynamic symbols, shared libraries, ELF sections, `package.json`, source code — looking for clues related to your question.
3. **Collects evidence** (strings, symbols, source snippets, config, imports) with file:line locations and relevance scores.
4. **Writes a plain-English report**: how the feature works, the evidence used, and a code skeleton to recreate a similar feature.
5. **Saves every investigation** to `~/.app-reverse-engineer/history/` so you can rerun setup, review history, and see what happened.

All analysis stays **on your machine** by default. No code is uploaded.

## Supported targets

| Target | What's inspected |
|--------|------------------|
| Native binary | `file`, `strings`, `nm -D` / `objdump -T`, `ldd`, `readelf -S` |
| Node / JS project | `package.json`, source files (`.js/.ts/.jsx/.tsx`), keyword + network-call (`fetch`/`axios`) scanning |
| Electron app | Everything above + main process (`ipcMain`, `BrowserWindow`, `webPreferences`) and preload script (`contextBridge`) |

## Install

```bash
git clone <your-repo-url>   # or copy this folder
cd app-reverse-engineer
pip3 install -e .
```

This creates the `are` command. Then run the guided setup:

```bash
are setup
```

Setup checks what's available (system tools + Python packages) and **asks before changing anything**. It never runs `sudo`/`apt` for you — it tells you what's missing and offers to install Python packages via `pip --user` only if you confirm. Rerun `are setup` anytime.

## Commands

```
are doctor                          # Check environment; shows what's missing
are setup [-y]                      # Guided setup (rerunnable; -y skips prompts)
are analyze <path>                  # Quick discovery: what kind of app/binary?
are investigate <path> -q "..."     # Full investigation; prints report + saves to history
are history [id]                    # List investigations, or show one by id
are mcp                             # Start the MCP server on stdio
```

Example:

```bash
are investigate ./my-electron-app -q "How does search work?"
# report printed + saved to ~/.app-reverse-engineer/history/<id>-<slug>/
are history <id>                    # reopen a saved report
```

## MCP (for AI agents)

`are mcp` starts a stdio MCP server exposing these tools:

- `analyze_app(path)` — discover target type
- `investigate_feature(path, question)` — full plain-English investigation
- `list_history()` — past investigations
- `get_history(id)` — a saved investigation
- `doctor()` — environment check

The official `mcp` Python package is used when installed; otherwise a built-in stdlib stdio JSON-RPC fallback server is used so it works without extra installs. Run `are setup` to install `mcp` (it will ask first).

## LLM polishing (optional)

Reports are plain-English by default using built-in templates. If you configure an LLM (Ollama local or OpenAI) via `are setup`, the report can be polished for readability. **Nothing is sent anywhere unless you explicitly configure a provider.**

## Safety & privacy

- Analysis is offline and local by default.
- `allow_network_analysis` and `allow_launching_apps` are off by default; dynamic analysis (running an app, taking screenshots) requires you to turn these on in setup.
- The tool does not modify your target app or binary.

## Project layout

```
app-reverse-engineer/
├── pyproject.toml
├── README.md
└── are/
    ├── __main__.py       # python -m are entry point
    ├── cli.py            # terminal commands (typer)
    ├── config.py         # ~/.app-reverse-engineer config + dirs
    ├── setup.py          # guided, ask-before-change installer
    ├── discovery.py      # detect app/binary type
    ├── report.py         # plain-English report + recreation guide
    ├── history.py        # save/list/get investigations
    ├── llm.py            # optional Ollama/OpenAI polishing
    ├── mcp_server.py     # MCP server (FastMCP or stdlib fallback)
    └── analyzers/
        ├── base.py
        ├── native.py     # ELF/PE/Mach-O static analysis
        ├── javascript.py # Node/JS project analysis
        └── electron.py   # Electron app analysis
```

## Next steps you may want

- Add real screenshot capture (Playwright) behind the `allow_launching_apps` consent flag.
- Add decompiler integration (Ghidra/radare2) for deeper native analysis.
- Add more language analyzers (Python, Java, Go, Rust).