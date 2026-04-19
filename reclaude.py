#!/usr/bin/env python3
"""reclaude - live browser for your Claude Code sessions.

Scans ~/.claude/projects/ and serves a real-time dashboard so you can find
the right session to `claude --resume` after an iTerm/terminal crash.
"""

import html
import json
import os
import pathlib
import subprocess
import time
from urllib.parse import parse_qs, urlparse

HOME = pathlib.Path.home()
PROJECTS = HOME / ".claude" / "projects"
SESSIONS_DIR = HOME / ".claude" / "sessions"


def _active_sessions() -> dict[str, dict]:
    """pid-keyed files in ~/.claude/sessions/ list currently-running sessions."""
    out: dict[str, dict] = {}
    if not SESSIONS_DIR.exists():
        return out
    for p in SESSIONS_DIR.iterdir():
        if p.suffix != ".json":
            continue
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        sid = data.get("sessionId")
        if sid:
            out[sid] = data
    return out


def _scan_file(path: pathlib.Path) -> dict | None:
    session_id = path.stem
    cwd = None
    custom_title = None
    last_prompt = None
    line_count = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_count += 1
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            if cwd is None and isinstance(obj.get("cwd"), str):
                cwd = obj["cwd"]
            t = obj.get("type")
            if t == "custom-title" and obj.get("customTitle"):
                custom_title = obj["customTitle"]
            elif t == "agent-name" and obj.get("agentName") and not custom_title:
                custom_title = obj["agentName"]
            if t == "user" and not obj.get("isSidechain"):
                msg = obj.get("message") or {}
                content = msg.get("content")
                if isinstance(content, str) and content.strip() and not content.startswith("<"):
                    last_prompt = content.strip()
                elif isinstance(content, list):
                    for piece in content:
                        if isinstance(piece, dict) and piece.get("type") == "text":
                            txt = (piece.get("text") or "").strip()
                            if txt and not txt.startswith("<"):
                                last_prompt = txt
                                break
    try:
        mtime = path.stat().st_mtime
        size = path.stat().st_size
    except OSError:
        return None
    return {
        "session_id": session_id,
        "cwd": cwd,
        "custom_title": custom_title,
        "last_prompt": last_prompt,
        "mtime": mtime,
        "size": size,
        "lines": line_count,
    }


def gather_rows() -> list[dict]:
    active = _active_sessions()
    rows: list[dict] = []
    for jsonl in PROJECTS.glob("*/*.jsonl"):
        row = _scan_file(jsonl)
        if row is None:
            continue
        row["active"] = row["session_id"] in active
        if not row["cwd"] and row["session_id"] in active:
            row["cwd"] = active[row["session_id"]].get("cwd")
        if not row["custom_title"] and row["session_id"] in active:
            row["custom_title"] = active[row["session_id"]].get("name")
        rows.append(row)
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return rows


def _search_sessions(query: str) -> list[str]:
    """Find session_ids whose jsonl content contains the query (case-insensitive substring).

    Tries ripgrep first; falls back to a pure-python scan if rg is missing.
    """
    if not query or not PROJECTS.exists():
        return []
    try:
        result = subprocess.run(
            ["rg", "-l", "-F", "-i", "-g", "*.jsonl", "--", query, str(PROJECTS)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except FileNotFoundError:
        return _search_sessions_python(query)
    except subprocess.TimeoutExpired:
        return []
    if result.returncode not in (0, 1):
        return _search_sessions_python(query)
    return [pathlib.Path(line).stem for line in result.stdout.splitlines() if line.endswith(".jsonl")]


def _search_sessions_python(query: str) -> list[str]:
    needle = query.lower().encode("utf-8")
    out: list[str] = []
    for p in PROJECTS.glob("*/*.jsonl"):
        try:
            with p.open("rb") as f:
                for line in f:
                    if needle in line.lower():
                        out.append(p.stem)
                        break
        except OSError:
            continue
    return out


def build_html() -> str:
    home_str = str(HOME)
    html_out = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>claude · sessions</title>
<link rel="icon" type="image/png" href="https://www.google.com/s2/favicons?domain=claude.com&sz=64">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..700;1,9..144,300..700&family=JetBrains+Mono:ital,wght@0,400;0,500;0,600;1,400&family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0b0e14;
    --surface: #0f1319;
    --surface-hi: #141923;
    --border: #1b212c;
    --border-strong: #293140;
    --fg: #ebe7dc;
    --fg-dim: #a0a5b0;
    --muted: #626a78;
    --muted-soft: #3a4150;
    --accent: #f2b138;
    --accent-dim: #3a2a0c;
    --accent-soft: rgba(242, 177, 56, 0.08);
    --pill-bg: rgba(242, 177, 56, 0.12);
    --pill-fg: #f2b138;
    --link: #79b8ff;
    --green: #9cde5c;
    --row-hover: rgba(242, 177, 56, 0.04);
  }}
  :root[data-theme="light"] {{
    --bg: #fbf8f0;
    --surface: #f3eee0;
    --surface-hi: #ebe5d2;
    --border: #dfd6bd;
    --border-strong: #c7bc9c;
    --fg: #1c1a15;
    --fg-dim: #4d4a42;
    --muted: #7d7a71;
    --muted-soft: #bdb49a;
    --accent: #a15c08;
    --accent-dim: #f1e3c3;
    --accent-soft: rgba(161, 92, 8, 0.08);
    --pill-bg: rgba(161, 92, 8, 0.1);
    --pill-fg: #a15c08;
    --link: #1e4a8a;
    --green: #55802b;
    --row-hover: rgba(161, 92, 8, 0.05);
  }}
  @media (prefers-color-scheme: light) {{
    :root:not([data-theme]) {{
      --bg: #fbf8f0;
      --surface: #f3eee0;
      --surface-hi: #ebe5d2;
      --border: #dfd6bd;
      --border-strong: #c7bc9c;
      --fg: #1c1a15;
      --fg-dim: #4d4a42;
      --muted: #7d7a71;
      --muted-soft: #bdb49a;
      --accent: #a15c08;
      --accent-dim: #f1e3c3;
      --accent-soft: rgba(161, 92, 8, 0.08);
      --pill-bg: rgba(161, 92, 8, 0.1);
      --pill-fg: #a15c08;
      --link: #1e4a8a;
      --green: #55802b;
      --row-hover: rgba(161, 92, 8, 0.05);
    }}
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; height: 100%; }}
  body {{
    font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    color: var(--fg);
    background: var(--bg);
    font-size: 13px;
    line-height: 1.55;
    font-feature-settings: "ss01", "ss02", "cv02", "cv11", "zero";
    -webkit-font-smoothing: antialiased;
    display: flex;
    flex-direction: column;
  }}
  ::selection {{ background: var(--accent); color: var(--bg); }}

  header {{
    flex: 0 0 auto;
    padding: 22px 32px 18px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: baseline;
    gap: 14px;
  }}
  .brand {{
    font-family: "JetBrains Mono", monospace;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--muted);
  }}
  .brand .arrow {{ color: var(--accent); margin: 0 4px; }}
  .title {{
    font-family: "Fraunces", serif;
    font-optical-sizing: auto;
    font-style: italic;
    font-weight: 400;
    font-size: 36px;
    letter-spacing: -0.01em;
    line-height: 1;
    color: var(--fg);
  }}
  .meta {{
    margin-left: auto;
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.02em;
  }}
  .meta .tick {{ color: var(--accent); margin: 0 6px; }}
  .conn-status {{ color: var(--green); font-size: 8px; vertical-align: middle; margin-right: 3px; }}
  .conn-status.down {{ color: var(--muted-soft); animation: pulse 1.2s ease-in-out infinite; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 0.3 }} 50% {{ opacity: 1 }} }}

  .layout {{
    flex: 1;
    min-height: 0;
    display: flex;
  }}

  /* ───── sidebar ───── */
  aside {{
    width: 320px;
    flex: 0 0 320px;
    overflow-y: auto;
    border-right: 1px solid var(--border);
    background: var(--surface);
    padding: 22px 0 60px;
  }}
  aside h2 {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.2em;
    font-weight: 600;
    color: var(--muted);
    margin: 0 0 10px;
    padding: 0 24px;
    display: flex;
    align-items: center;
    gap: 8px;
  }}
  aside h2::before {{ content: "»"; color: var(--accent); font-weight: 700; }}
  .all-btn {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    width: calc(100% - 16px);
    margin: 0 8px 14px;
    text-align: left;
    padding: 9px 14px;
    background: transparent;
    border: 1px solid transparent;
    color: var(--fg);
    cursor: pointer;
    font-family: inherit;
    font-size: 12px;
    font-weight: 500;
    letter-spacing: 0.02em;
  }}
  .all-btn:hover {{ background: var(--row-hover); }}
  .all-btn.selected {{
    background: var(--accent-soft);
    border-color: var(--accent);
    color: var(--accent);
  }}
  .all-btn .star {{ color: var(--accent); margin-right: 8px; }}
  .all-btn .c {{ color: var(--muted); font-variant-numeric: tabular-nums; }}
  .all-btn.selected .c {{ color: var(--accent); }}

  #tree {{ padding: 0 8px; }}
  .tree, .tree ul {{ list-style: none; padding: 0; margin: 0; }}
  .node-row {{
    display: flex;
    align-items: center;
    padding: 4px 10px;
    cursor: pointer;
    font-size: 12px;
    line-height: 1.5;
    border-left: 2px solid transparent;
    margin-left: 4px;
  }}
  .node-row:hover {{ background: var(--row-hover); }}
  .node-row.selected {{
    background: var(--accent-soft);
    border-left-color: var(--accent);
    color: var(--accent);
  }}
  .node-caret {{
    width: 12px;
    text-align: center;
    color: var(--muted);
    margin-right: 6px;
    user-select: none;
    font-size: 9px;
    flex: 0 0 12px;
  }}
  .node-row.selected .node-caret {{ color: var(--accent); }}
  .node-caret.leaf {{ color: var(--muted-soft); }}
  .node-caret.leaf::before {{ content: "·"; font-size: 14px; }}
  .node-caret.leaf {{ font-size: 0; }}
  .node-label {{ flex: 1; word-break: break-all; }}
  .node-count {{
    color: var(--muted);
    font-size: 10px;
    margin-left: 10px;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.04em;
  }}
  .node-row.selected .node-count {{ color: var(--accent); }}

  /* ───── main ───── */
  .content {{
    flex: 1;
    min-width: 0;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
  }}
  .toolbar {{
    padding: 14px 32px;
    display: flex;
    gap: 10px;
    align-items: center;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  .search-wrap {{
    flex: 1;
    position: relative;
  }}
  .search-wrap::before {{
    content: "/";
    position: absolute;
    left: 14px;
    top: 50%;
    transform: translateY(-50%);
    color: var(--accent);
    font-weight: 600;
    pointer-events: none;
  }}
  .toolbar input[type=text] {{
    width: 100%;
    padding: 9px 14px 9px 28px;
    border: 1px solid var(--border);
    font-size: 13px;
    font-family: inherit;
    background: var(--surface);
    color: var(--fg);
    letter-spacing: 0.01em;
  }}
  .toolbar input[type=text]::placeholder {{ color: var(--muted); }}
  .toolbar input[type=text]:focus {{
    outline: none;
    border-color: var(--accent);
    background: var(--surface-hi);
  }}
  .btn {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-dim);
    padding: 8px 12px;
    cursor: pointer;
    font-family: inherit;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: lowercase;
    white-space: nowrap;
  }}
  .btn:hover {{ border-color: var(--border-strong); color: var(--fg); }}
  .btn.active {{
    border-color: var(--accent);
    background: var(--accent-soft);
    color: var(--accent);
  }}
  .btn .check {{ margin-right: 6px; color: var(--muted-soft); }}
  .btn.active .check {{ color: var(--accent); }}
  .count {{
    color: var(--muted);
    font-size: 11px;
    letter-spacing: 0.04em;
    font-variant-numeric: tabular-nums;
    padding: 0 4px;
  }}
  .count .sep {{ color: var(--muted-soft); margin: 0 4px; }}
  .count .total {{ color: var(--fg-dim); }}

  main {{ padding: 0; flex: 1; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;
    table-layout: fixed;
  }}
  thead th {{
    text-align: left;
    font-weight: 600;
    color: var(--muted);
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: 61px;
    background: var(--bg);
    font-size: 10px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
  }}
  thead th:first-child {{ padding-left: 32px; }}
  thead th:last-child {{ padding-right: 32px; text-align: right; }}
  tbody td {{
    padding: 14px 16px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  tbody td:first-child {{ padding-left: 32px; }}
  tbody td:last-child {{ padding-right: 32px; text-align: right; }}
  tbody tr {{ transition: background 0.08s ease; }}
  tbody tr:hover {{ background: var(--row-hover); }}

  /* ───── cells ───── */
  .cell-name {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
  .dot {{
    display: inline-block;
    width: 6px;
    height: 6px;
    background: var(--muted-soft);
    flex: 0 0 6px;
    border-radius: 50%;
  }}
  .dot.active {{
    background: var(--green);
    box-shadow: 0 0 0 3px rgba(156, 222, 92, 0.18);
  }}
  .pill {{
    display: inline-block;
    background: var(--pill-bg);
    color: var(--pill-fg);
    padding: 3px 10px;
    font-size: 11px;
    font-weight: 500;
    letter-spacing: 0.01em;
    border: 1px solid transparent;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .dash {{
    color: var(--muted-soft);
    font-family: "Fraunces", serif;
    font-style: italic;
    font-size: 15px;
    line-height: 1;
    letter-spacing: 0;
  }}

  .cwd-path {{
    font-size: 12px;
    color: var(--fg-dim);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    letter-spacing: 0.01em;
  }}
  .cwd-path .root {{ color: var(--accent); }}
  .cwd-path .segs {{ color: var(--fg-dim); }}
  .cwd-path .leaf {{ color: var(--fg); font-weight: 500; }}
  .sid {{
    font-size: 10.5px;
    color: var(--muted);
    margin-top: 2px;
    letter-spacing: 0.08em;
  }}
  .sid::before {{ content: "#"; color: var(--muted-soft); margin-right: 1px; }}

  .prompt {{
    color: var(--fg-dim);
    font-size: 12px;
    line-height: 1.6;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}

  .when {{
    color: var(--muted);
    font-size: 11px;
    letter-spacing: 0.04em;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }}

  .copy {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-dim);
    padding: 6px 11px;
    cursor: pointer;
    font-family: inherit;
    font-size: 10.5px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }}
  .copy:hover {{ border-color: var(--accent); color: var(--accent); }}
  .copy.copied {{ color: var(--green); border-color: var(--green); }}
  .copy::before {{ content: "↗"; margin-right: 6px; color: var(--muted); }}
  .copy:hover::before {{ color: var(--accent); }}
  .copy.copied::before {{ content: "✓"; color: var(--green); }}

  /* ───── group headers ───── */
  .group-head td {{
    background: var(--surface);
    padding: 12px 16px 12px 32px;
    font-size: 11px;
    font-weight: 500;
    color: var(--fg-dim);
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    user-select: none;
    letter-spacing: 0.02em;
  }}
  .group-head td:hover {{ background: var(--surface-hi); color: var(--fg); }}
  .group-head .caret {{
    display: inline-block;
    width: 10px;
    color: var(--accent);
    margin-right: 10px;
    transition: transform 0.15s;
  }}
  .group-head.collapsed .caret {{ transform: rotate(-90deg); }}
  .group-head .group-count {{
    color: var(--muted);
    font-weight: 400;
    margin-left: 12px;
    font-size: 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }}
  .group-head .group-count::before {{ content: "[ "; color: var(--muted-soft); }}
  .group-head .group-count::after {{ content: " ]"; color: var(--muted-soft); }}

  .empty {{
    text-align: center;
    padding: 80px 40px;
    color: var(--muted);
  }}
  .empty::before {{
    content: "∅";
    display: block;
    font-family: "Fraunces", serif;
    font-size: 48px;
    color: var(--muted-soft);
    margin-bottom: 12px;
    font-style: italic;
  }}

  /* scrollbar */
  ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
  ::-webkit-scrollbar-track {{ background: transparent; }}
  ::-webkit-scrollbar-thumb {{
    background: var(--border-strong);
    border: 2px solid var(--bg);
  }}
  ::-webkit-scrollbar-thumb:hover {{ background: var(--muted); }}
</style>
</head>
<body>
<header>
  <span class="brand">claude <span class="arrow">»</span> code</span>
  <span class="title">sessions</span>
  <span class="meta">updated <span class="tick">·</span> <span id="gen-time">…</span> <span class="tick">·</span> <span id="conn-status" class="conn-status" title="live">●</span> live</span>
</header>
<div class="layout">
  <aside>
    <h2>directories</h2>
    <button id="all-btn" class="all-btn selected">
      <span><span class="star">★</span>all sessions</span>
      <span class="c" id="all-count"></span>
    </button>
    <div id="tree"></div>
  </aside>
  <div class="content">
    <div class="toolbar">
      <div class="search-wrap">
        <input id="q" type="text" placeholder="search names, paths, ids, full content…" autofocus>
      </div>
      <button id="named-only" class="btn" type="button"><span class="check">□</span>named</button>
      <button id="group-cwd" class="btn" type="button"><span class="check">□</span>by dir</button>
      <button id="group-name" class="btn" type="button"><span class="check">□</span>by name</button>
      <button id="toggle-all" class="btn" style="display:none">collapse all</button>
      <span class="count"><span id="count-shown">0</span><span class="sep">/</span><span class="total" id="count-total">0</span></span>
      <button id="theme-toggle" class="btn" title="theme">◐</button>
    </div>
    <main>
      <table>
        <colgroup>
          <col style="width: 180px">
          <col style="width: 320px">
          <col>
          <col style="width: 110px">
          <col style="width: 130px">
        </colgroup>
        <thead>
          <tr>
            <th>name</th>
            <th>directory</th>
            <th>last prompt</th>
            <th>last</th>
            <th>resume</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="empty" id="empty" style="display:none">no sessions match your filters</div>
    </main>
  </div>
</div>
<script>
const ROWS = new Map();  // session_id -> row
let DATA = [];
let GENERATED_AT = "";
let SEARCH_RESULT = null;  // null = no active search; Set<session_id> when active
let SEARCH_REQ_ID = 0;

function rebuildData() {{
  DATA = [...ROWS.values()].sort((a, b) => b.mtime - a.mtime);
}}

function setGenerated(g) {{
  GENERATED_AT = g || "";
  const el = document.getElementById("gen-time");
  if (el) el.textContent = GENERATED_AT;
}}

function applySnapshot(rows, generated) {{
  ROWS.clear();
  for (const r of rows) ROWS.set(r.session_id, r);
  rebuildData();
  setGenerated(generated);
  renderTree();
  render();
}}

function applyDelta(upsert, remove, generated) {{
  if (Array.isArray(remove)) for (const sid of remove) ROWS.delete(sid);
  if (Array.isArray(upsert)) for (const r of upsert) ROWS.set(r.session_id, r);
  rebuildData();
  setGenerated(generated);
  renderTree();
  render();
}}

function setConnStatus(ok) {{
  const el = document.getElementById("conn-status");
  if (!el) return;
  el.classList.toggle("down", !ok);
  el.title = ok ? "live" : "disconnected";
}}

function subscribeEvents() {{
  if (!window.EventSource) {{
    document.getElementById("gen-time").textContent = "unsupported";
    return;
  }}
  const es = new EventSource("/events");
  es.addEventListener("open", () => setConnStatus(true));
  es.addEventListener("error", () => setConnStatus(false));
  es.addEventListener("snapshot", (ev) => {{
    const d = JSON.parse(ev.data);
    applySnapshot(d.rows || [], d.generated);
  }});
  es.addEventListener("delta", (ev) => {{
    const d = JSON.parse(ev.data);
    applyDelta(d.upsert, d.remove, d.generated);
  }});
}}

function fmtWhen(ts) {{
  const d = new Date(ts * 1000);
  const now = new Date();
  const diffMs = now - d;
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return "now";
  if (diffMin < 60) return diffMin + "m";
  const diffH = Math.floor(diffMin / 60);
  if (diffH < 24) return diffH + "h";
  const diffD = Math.floor(diffH / 24);
  if (diffD < 30) return diffD + "d";
  return d.toISOString().slice(0, 10);
}}

function escapeHtml(s) {{
  return (s || "").replace(/[&<>"']/g, c => ({{
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }}[c]));
}}

function shortId(id) {{ return id ? id.slice(0, 8) : ""; }}

const HOME = "{home_str}";
function prettyPath(p) {{
  if (!p) return p;
  if (p === HOME) return "~";
  if (p.startsWith(HOME + "/")) return "~" + p.slice(HOME.length);
  if (p === "/private/tmp") return "/tmp";
  if (p.startsWith("/private/tmp/")) return "/tmp" + p.slice("/private/tmp".length);
  return p;
}}

function prettySegs(pretty) {{
  if (pretty === "(unknown)") return ["(unknown)"];
  if (pretty.startsWith("/")) {{
    const parts = pretty.split("/").filter(Boolean);
    return parts.map((p, i) => i === 0 ? "/" + p : p);
  }}
  return pretty.split("/");
}}

function renderCwdPath(cwd) {{
  if (!cwd) return `<span class="dash">—</span>`;
  const pretty = prettyPath(cwd);
  const segs = prettySegs(pretty);
  if (segs.length === 1) return `<span class="root">${{escapeHtml(segs[0])}}</span>`;
  const last = segs[segs.length - 1];
  const head = segs.slice(0, -1);
  const headHtml = head.map((s, i) => {{
    const cls = i === 0 ? "root" : "segs";
    return `<span class="${{cls}}">${{escapeHtml(s)}}</span>`;
  }}).join(`<span class="segs">/</span>`);
  return `${{headHtml}}<span class="segs">/</span><span class="leaf">${{escapeHtml(last)}}</span>`;
}}

function rowHtml(r, showCwd) {{
  const activeCls = r.active ? "dot active" : "dot";
  const name = r.custom_title
    ? `<span class="pill" title="${{escapeHtml(r.custom_title)}}">${{escapeHtml(r.custom_title)}}</span>`
    : `<span class="dash">—</span>`;
  const cwdPretty = r.cwd ? prettyPath(r.cwd) : "";
  const cwdCell = showCwd
    ? `<div class="cwd-path" title="${{escapeHtml(cwdPretty)}}">${{renderCwdPath(r.cwd)}}</div><div class="sid">${{shortId(r.session_id)}}</div>`
    : `<div class="sid">${{shortId(r.session_id)}}</div>`;
  const prompt = r.last_prompt ? escapeHtml(r.last_prompt) : "";
  const resumeArg = r.custom_title ? `"${{r.custom_title}}"` : r.session_id;
  const cmd = r.cwd
    ? `cd "${{r.cwd}}" && claude --resume ${{resumeArg}}`
    : `claude --resume ${{resumeArg}}`;
  return `
    <tr>
      <td><div class="cell-name"><span class="${{activeCls}}"></span>${{name}}</div></td>
      <td>${{cwdCell}}</td>
      <td><div class="prompt" title="${{escapeHtml(r.last_prompt || "")}}">${{prompt}}</div></td>
      <td><span class="when" title="${{new Date(r.mtime*1000).toISOString()}}">${{fmtWhen(r.mtime)}}</span></td>
      <td><button class="copy" data-cmd="${{escapeHtml(cmd)}}">copy</button></td>
    </tr>`;
}}

const COLLAPSED = new Set();
let CURRENT_GROUPS = [];
const TREE_EXPANDED = new Set(JSON.parse(localStorage.getItem("sessions-tree-expanded") || "[]"));
let SELECTED_PATH = localStorage.getItem("sessions-selected-path") || "";

function prettyForRow(r) {{
  return r.cwd ? prettyPath(r.cwd) : "(unknown)";
}}

function matchesSelected(r) {{
  if (!SELECTED_PATH) return true;
  const p = prettyForRow(r);
  return p === SELECTED_PATH || p.startsWith(SELECTED_PATH + "/");
}}

function buildTree() {{
  const root = {{ label: "", path: "", count: 0, children: new Map() }};
  for (const r of DATA) {{
    const pretty = prettyForRow(r);
    const segs = prettySegs(pretty);
    let cur = root;
    let curPath = "";
    for (let i = 0; i < segs.length; i++) {{
      const seg = segs[i];
      curPath = i === 0 ? seg : curPath + "/" + seg;
      let next = cur.children.get(seg);
      if (!next) {{
        next = {{ label: seg, path: curPath, count: 0, children: new Map() }};
        cur.children.set(seg, next);
      }}
      next.count++;
      cur = next;
    }}
  }}
  return root;
}}

function renderTree() {{
  const root = buildTree();
  const tree = document.getElementById("tree");
  const allBtn = document.getElementById("all-btn");
  document.getElementById("all-count").textContent = DATA.length;
  allBtn.classList.toggle("selected", SELECTED_PATH === "");

  function renderNode(node, depth) {{
    const hasChildren = node.children.size > 0;
    const expanded = TREE_EXPANDED.has(node.path);
    const selected = SELECTED_PATH === node.path;
    const caret = hasChildren
      ? `<span class="node-caret" data-caret="1">${{expanded ? "▾" : "▸"}}</span>`
      : `<span class="node-caret leaf"></span>`;
    let html = `<li>
      <div class="node-row ${{selected ? "selected" : ""}}" data-path="${{escapeHtml(node.path)}}" style="padding-left: ${{depth * 14 + 10}}px">
        ${{caret}}<span class="node-label">${{escapeHtml(node.label)}}</span><span class="node-count">${{node.count}}</span>
      </div>`;
    if (hasChildren && expanded) {{
      const kids = [...node.children.values()].sort((a, b) => b.count - a.count);
      html += `<ul>${{kids.map(k => renderNode(k, depth + 1)).join("")}}</ul>`;
    }}
    html += `</li>`;
    return html;
  }}

  const roots = [...root.children.values()].sort((a, b) => b.count - a.count);
  tree.innerHTML = `<ul class="tree">${{roots.map(r => renderNode(r, 0)).join("")}}</ul>`;

  tree.querySelectorAll(".node-row").forEach(row => {{
    row.addEventListener("click", (e) => {{
      const path = row.dataset.path;
      if (e.target.dataset.caret) {{
        if (TREE_EXPANDED.has(path)) TREE_EXPANDED.delete(path);
        else TREE_EXPANDED.add(path);
        localStorage.setItem("sessions-tree-expanded", JSON.stringify([...TREE_EXPANDED]));
        renderTree();
        return;
      }}
      SELECTED_PATH = path;
      const caretEl = row.querySelector(".node-caret");
      if (caretEl && !caretEl.classList.contains("leaf")) TREE_EXPANDED.add(path);
      localStorage.setItem("sessions-selected-path", SELECTED_PATH);
      localStorage.setItem("sessions-tree-expanded", JSON.stringify([...TREE_EXPANDED]));
      renderTree();
      render();
    }});
  }});
}}

function render() {{
  const namedOnly = document.getElementById("named-only").classList.contains("active");
  const groupByCwd = document.getElementById("group-cwd").classList.contains("active");
  const groupByName = document.getElementById("group-name").classList.contains("active");
  const grouped = groupByCwd || groupByName;
  const tbody = document.getElementById("rows");
  const empty = document.getElementById("empty");
  const toggleAll = document.getElementById("toggle-all");
  toggleAll.style.display = grouped ? "inline-block" : "none";
  const matches = DATA.filter(r => {{
    if (!matchesSelected(r)) return false;
    if (namedOnly && !r.custom_title) return false;
    if (SEARCH_RESULT === null) return true;
    return SEARCH_RESULT.has(r.session_id);
  }});
  document.getElementById("count-shown").textContent = matches.length;
  document.getElementById("count-total").textContent = DATA.length;
  if (matches.length === 0) {{ tbody.innerHTML = ""; empty.style.display = "block"; return; }}
  empty.style.display = "none";

  let html = "";
  if (grouped) {{
    const groups = new Map();
    for (const r of matches) {{
      const key = groupByName
        ? (r.custom_title || "(unnamed)")
        : (r.cwd || "(unknown)");
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(r);
    }}
    const sorted = [...groups.entries()].sort((a, b) => {{
      const am = Math.max(...a[1].map(r => r.mtime));
      const bm = Math.max(...b[1].map(r => r.mtime));
      return bm - am;
    }});
    CURRENT_GROUPS = sorted.map(([k]) => k);
    const allCollapsed = CURRENT_GROUPS.length > 0 && CURRENT_GROUPS.every(k => COLLAPSED.has(k));
    toggleAll.textContent = allCollapsed ? "expand all" : "collapse all";
    for (const [key, items] of sorted) {{
      const collapsed = COLLAPSED.has(key);
      let label;
      if (groupByName) {{
        label = key;
      }} else {{
        label = key === "(unknown)" ? key : prettyPath(key);
      }}
      html += `<tr class="group-head ${{collapsed ? "collapsed" : ""}}" data-key="${{escapeHtml(key)}}">
        <td colspan="5"><span class="caret">▾</span>${{escapeHtml(label)}}<span class="group-count">${{items.length}} sessions</span></td>
      </tr>`;
      if (!collapsed) {{
        html += items.map(r => rowHtml(r, groupByName)).join("");
      }}
    }}
  }} else {{
    html = matches.map(r => rowHtml(r, true)).join("");
  }}
  tbody.innerHTML = html;

  tbody.querySelectorAll(".copy").forEach(btn => {{
    btn.addEventListener("click", (e) => {{
      e.stopPropagation();
      navigator.clipboard.writeText(btn.dataset.cmd);
      btn.textContent = "copied";
      btn.classList.add("copied");
      setTimeout(() => {{ btn.textContent = "copy"; btn.classList.remove("copied"); }}, 1400);
    }});
  }});
  tbody.querySelectorAll(".group-head").forEach(row => {{
    row.addEventListener("click", () => {{
      const key = row.dataset.key;
      if (COLLAPSED.has(key)) COLLAPSED.delete(key); else COLLAPSED.add(key);
      render();
    }});
  }});
}}

function setToggleBtn(id, storageKey) {{
  const btn = document.getElementById(id);
  const check = btn.querySelector(".check");
  if (localStorage.getItem(storageKey) === "1") {{
    btn.classList.add("active");
    if (check) check.textContent = "■";
  }}
  btn.addEventListener("click", () => {{
    const on = !btn.classList.contains("active");
    btn.classList.toggle("active", on);
    if (check) check.textContent = on ? "■" : "□";
    localStorage.setItem(storageKey, on ? "1" : "0");
    render();
  }});
}}
setToggleBtn("named-only", "sessions-named-only");

(function initGroupBtns() {{
  const mode = localStorage.getItem("sessions-group-mode") || "none";
  function setMode(m) {{
    const cwd = document.getElementById("group-cwd");
    const name = document.getElementById("group-name");
    cwd.classList.toggle("active", m === "cwd");
    name.classList.toggle("active", m === "name");
    cwd.querySelector(".check").textContent = m === "cwd" ? "■" : "□";
    name.querySelector(".check").textContent = m === "name" ? "■" : "□";
    localStorage.setItem("sessions-group-mode", m);
  }}
  setMode(mode);
  document.getElementById("group-cwd").addEventListener("click", () => {{
    const cur = document.getElementById("group-cwd").classList.contains("active");
    COLLAPSED.clear();
    setMode(cur ? "none" : "cwd");
    render();
  }});
  document.getElementById("group-name").addEventListener("click", () => {{
    const cur = document.getElementById("group-name").classList.contains("active");
    COLLAPSED.clear();
    setMode(cur ? "none" : "name");
    render();
  }});
}})();

function localSearchIds(q) {{
  const lo = q.toLowerCase();
  const out = new Set();
  for (const r of DATA) {{
    const hay = [r.custom_title, r.cwd, r.session_id, r.last_prompt].filter(Boolean).join(" ").toLowerCase();
    if (hay.includes(lo)) out.add(r.session_id);
  }}
  return out;
}}

async function updateSearch() {{
  const q = document.getElementById("q").value.trim();
  if (!q) {{
    SEARCH_RESULT = null;
    render();
    return;
  }}
  const reqId = ++SEARCH_REQ_ID;
  // immediate local matches across visible metadata
  SEARCH_RESULT = localSearchIds(q);
  render();
  // augment with full-text matches from server (rg over jsonl bodies)
  try {{
    const res = await fetch("/search?q=" + encodeURIComponent(q));
    if (reqId !== SEARCH_REQ_ID) return;  // a newer query has started; drop stale result
    const d = await res.json();
    const merged = new Set(SEARCH_RESULT);
    for (const sid of (d.session_ids || [])) merged.add(sid);
    SEARCH_RESULT = merged;
    render();
  }} catch (e) {{
    // network error: keep local-only results
  }}
}}

let _searchTimer;
document.getElementById("q").addEventListener("input", () => {{
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(updateSearch, 200);
}});

document.getElementById("all-btn").addEventListener("click", () => {{
  SELECTED_PATH = "";
  localStorage.setItem("sessions-selected-path", "");
  renderTree();
  render();
}});

(function initTheme() {{
  const btn = document.getElementById("theme-toggle");
  const stored = localStorage.getItem("sessions-theme");
  if (stored === "dark" || stored === "light") {{
    document.documentElement.setAttribute("data-theme", stored);
  }}
  function effective() {{
    const set = document.documentElement.getAttribute("data-theme");
    if (set) return set;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }}
  function sync() {{ btn.textContent = effective() === "dark" ? "◑" : "◐"; }}
  sync();
  btn.addEventListener("click", () => {{
    const next = effective() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("sessions-theme", next);
    sync();
  }});
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", sync);
}})();

document.getElementById("toggle-all").addEventListener("click", () => {{
  const allCollapsed = CURRENT_GROUPS.length > 0 && CURRENT_GROUPS.every(k => COLLAPSED.has(k));
  if (allCollapsed) {{
    COLLAPSED.clear();
  }} else {{
    CURRENT_GROUPS.forEach(k => COLLAPSED.add(k));
  }}
  render();
}});

// keyboard: "/" focuses search
document.addEventListener("keydown", (e) => {{
  if (e.key === "/" && document.activeElement.tagName !== "INPUT") {{
    e.preventDefault();
    document.getElementById("q").focus();
  }}
}});

subscribeEvents();
</script>
</body>
</html>
"""
    return html_out


# ───── server state ───────────────────────────────────────────────────────────
import queue
import threading

_state_lock = threading.RLock()
_rows_by_id: dict[str, dict] = {}           # session_id -> row
_file_mtime: dict[str, float] = {}          # jsonl path -> mtime (last scanned)
_file_to_session: dict[str, str] = {}       # jsonl path -> session_id
_subscribers: list["queue.Queue[dict]"] = []


def _active_session_patch(row: dict, active: dict[str, dict]) -> None:
    row["active"] = row["session_id"] in active
    if not row["cwd"] and row["session_id"] in active:
        row["cwd"] = active[row["session_id"]].get("cwd")
    if not row["custom_title"] and row["session_id"] in active:
        row["custom_title"] = active[row["session_id"]].get("name")


def _scan_path(path: pathlib.Path, active: dict[str, dict]) -> dict | None:
    row = _scan_file(path)
    if row is None:
        return None
    _active_session_patch(row, active)
    return row


def _full_scan() -> None:
    """Populate all caches from scratch."""
    active = _active_sessions()
    with _state_lock:
        _rows_by_id.clear()
        _file_mtime.clear()
        _file_to_session.clear()
        for jsonl in PROJECTS.glob("*/*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            row = _scan_path(jsonl, active)
            if row is None:
                continue
            _rows_by_id[row["session_id"]] = row
            _file_mtime[str(jsonl)] = mtime
            _file_to_session[str(jsonl)] = row["session_id"]


def _compute_diff() -> tuple[list[dict], list[str]]:
    """Find changed files, rescan them, and return (upsert, remove) by session_id."""
    active = _active_sessions()
    upsert: list[dict] = []
    remove: list[str] = []

    current: dict[str, float] = {}
    try:
        for p in PROJECTS.glob("*/*.jsonl"):
            try:
                current[str(p)] = p.stat().st_mtime
            except OSError:
                pass
    except OSError:
        pass

    with _state_lock:
        prev_mtime = dict(_file_mtime)
        prev_file_to_sid = dict(_file_to_session)

    # added or modified
    for path_str, mt in current.items():
        if prev_mtime.get(path_str) == mt:
            continue
        row = _scan_path(pathlib.Path(path_str), active)
        if row is None:
            continue
        upsert.append(row)
        with _state_lock:
            _rows_by_id[row["session_id"]] = row
            _file_mtime[path_str] = mt
            _file_to_session[path_str] = row["session_id"]

    # removed files
    for path_str in prev_mtime:
        if path_str in current:
            continue
        sid = prev_file_to_sid.get(path_str)
        with _state_lock:
            _file_mtime.pop(path_str, None)
            _file_to_session.pop(path_str, None)
            if sid and sid in _rows_by_id:
                del _rows_by_id[sid]
        if sid:
            remove.append(sid)

    return upsert, remove


def _snapshot_payload() -> dict:
    with _state_lock:
        rows = sorted(_rows_by_id.values(), key=lambda r: r["mtime"], reverse=True)
    return {"type": "snapshot", "rows": rows, "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z")}


def _publish(event: dict) -> None:
    with _state_lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except Exception:
            pass


def _watcher_loop(interval: float = 2.0) -> None:
    while True:
        time.sleep(interval)
        upsert, remove = _compute_diff()
        if not upsert and not remove:
            continue
        msg = f"[{time.strftime('%H:%M:%S')}] delta: +{len(upsert)} -{len(remove)}"
        print(msg)
        _publish({
            "type": "delta",
            "upsert": upsert,
            "remove": remove,
            "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        })


def serve(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    _full_scan()
    with _state_lock:
        n = len(_rows_by_id)
    print(f"initial scan: {n} sessions")

    template_bytes = build_html().encode("utf-8")

    threading.Thread(target=_watcher_loop, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send_bytes(template_bytes, "text/html; charset=utf-8")
            elif self.path == "/events":
                self._stream_events()
            elif self.path.startswith("/search"):
                self._handle_search()
            else:
                self.send_error(404)

        def _handle_search(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            sids = _search_sessions(q)
            body = json.dumps({"q": q, "session_ids": sids}, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")

        def _send_bytes(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _write_event(self, event: dict) -> None:
            payload = json.dumps(event, ensure_ascii=False)
            self.wfile.write(f"event: {event['type']}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

        def _stream_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q: "queue.Queue[dict]" = queue.Queue()
            with _state_lock:
                _subscribers.append(q)
            try:
                self._write_event(_snapshot_payload())
                while True:
                    try:
                        ev = q.get(timeout=25)
                        self._write_event(ev)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _state_lock:
                    if q in _subscribers:
                        _subscribers.remove(q)

        def log_message(self, fmt, *args):
            line = fmt % args
            if "GET /events" in line:
                return
            print(f"[{time.strftime('%H:%M:%S')}] " + line)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"claude-sessions serving at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run the Claude Code session browser server")
    ap.add_argument("--port", type=int, default=9999, help="HTTP port (default: 9999)")
    args = ap.parse_args()
    serve(args.port)
