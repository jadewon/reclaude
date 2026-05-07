#!/usr/bin/env python3
"""reclaude - live browser for your Claude Code sessions.

Scans ~/.claude/projects/ and serves a real-time dashboard so you can find
the right session to `claude --resume` after an iTerm/terminal crash.
"""

import html
import json
import os
import pathlib
import re
import sqlite3
import threading
import time
from datetime import datetime
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


_SNIPPET_BEFORE = 30
_SNIPPET_AFTER = 110
_FULL_CAP = 2000
_MAX_SNIPPETS_PER_FILE = 3
_WS_RUN = re.compile(r"\s+")


def _extract_text(obj: dict) -> str:
    """Pull human-readable text out of a single jsonl line (one message)."""
    parts: list[str] = []
    msg = obj.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for block in c:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    parts.append(block.get("text") or "")
                elif t == "tool_use":
                    inp = block.get("input")
                    if inp is not None:
                        try:
                            parts.append(json.dumps(inp, ensure_ascii=False))
                        except (TypeError, ValueError):
                            pass
                elif t == "tool_result":
                    bc = block.get("content")
                    if isinstance(bc, str):
                        parts.append(bc)
                    elif isinstance(bc, list):
                        for sub in bc:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                parts.append(sub.get("text") or "")
    if obj.get("type") == "summary" and isinstance(obj.get("summary"), str):
        parts.append(obj["summary"])
    if obj.get("type") == "custom-title" and isinstance(obj.get("customTitle"), str):
        parts.append(obj["customTitle"])
    if obj.get("type") == "attachment":
        att = obj.get("attachment")
        if isinstance(att, dict):
            ac = att.get("content")
            if isinstance(ac, str):
                parts.append(ac)
            elif isinstance(ac, list):
                for sub in ac:
                    if isinstance(sub, dict):
                        for k in ("text", "content"):
                            if isinstance(sub.get(k), str):
                                parts.append(sub[k])
    return "\n".join(p for p in parts if p)


def _make_snippet(raw_line: bytes, fallback_word: str, regex_pat, phrase_pat) -> dict | None:
    """Parse one jsonl line and return a snippet dict around the best hit.

    Match-finding priority:
      1. phrase_pat (multi-word query joined with optional [-_\\s]* separators)
      2. regex_pat (when in regex mode)
      3. fallback_word (a single literal word from word-AND mode)
    """
    try:
        obj = json.loads(raw_line.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    text = _extract_text(obj)
    if not text:
        return None
    text = _WS_RUN.sub(" ", text).strip()
    if not text:
        return None

    start = end = -1
    if phrase_pat is not None:
        m = phrase_pat.search(text)
        if m:
            start, end = m.span()
    if start < 0 and regex_pat is not None:
        m = regex_pat.search(text)
        if m:
            start, end = m.span()
    if start < 0 and fallback_word:
        idx = text.lower().find(fallback_word.lower())
        if idx >= 0:
            start, end = idx, idx + len(fallback_word)
    if start < 0:
        return None

    pre = max(0, start - _SNIPPET_BEFORE)
    post = min(len(text), end + _SNIPPET_AFTER)
    role = obj.get("type") or "?"
    if role == "user" and obj.get("isMeta"):
        role = "meta"
    full = text if len(text) <= _FULL_CAP else text[: _FULL_CAP] + "…"
    ts_epoch: float | None = None
    raw_ts = obj.get("timestamp")
    if isinstance(raw_ts, str):
        try:
            ts_epoch = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts_epoch = None
    return {
        "role": role,
        "before": ("…" if pre > 0 else "") + text[pre:start],
        "match": text[start:end],
        "after": text[end:post] + ("…" if post < len(text) else ""),
        "full": full,
        "ts": ts_epoch,
    }


# ───── search index (SQLite, on-disk bigram inverted index) ───────────────────
# Idle background indexer + per-thread reader connections. See
# docs/plans/v1-bigram-index.md for the rationale (bigrams handle Korean as
# well as English; FTS5 trigram is rejected for CJK quality and build-flag
# fragility).

def _index_db_path() -> pathlib.Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(HOME / ".cache")
    return pathlib.Path(base) / "reclaude" / "index.db"


_index_local = threading.local()


def _index_conn() -> sqlite3.Connection:
    conn = getattr(_index_local, "conn", None)
    if conn is not None:
        return conn
    p = _index_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS files ("
        " path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS postings ("
        " gram TEXT NOT NULL, path TEXT NOT NULL,"
        " PRIMARY KEY (gram, path)) WITHOUT ROWID"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_postings_path ON postings(path)")
    _index_local.conn = conn
    return conn


def _bigrams_of(word: str) -> set[str]:
    if len(word) < 2:
        return set()
    return {word[i:i + 2] for i in range(len(word) - 1)}


def _extract_file_bigrams(text: str) -> set[str]:
    """Lowercase + bigrams from every non-whitespace run."""
    grams: set[str] = set()
    for run in text.lower().split():
        if len(run) < 2:
            continue
        for i in range(len(run) - 1):
            grams.add(run[i:i + 2])
    return grams


def _index_file(path: pathlib.Path, mtime: float, size: int) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    grams = _extract_file_bigrams(text)
    path_str = str(path)
    conn = _index_conn()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM postings WHERE path = ?", (path_str,))
        if grams:
            conn.executemany(
                "INSERT OR IGNORE INTO postings(gram, path) VALUES (?, ?)",
                [(g, path_str) for g in grams],
            )
        conn.execute(
            "INSERT OR REPLACE INTO files(path, mtime, size) VALUES (?, ?, ?)",
            (path_str, mtime, size),
        )
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return False
    return True


def _index_purge(path_str: str) -> None:
    conn = _index_conn()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM postings WHERE path = ?", (path_str,))
        conn.execute("DELETE FROM files WHERE path = ?", (path_str,))
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def _index_pick_pending() -> tuple[pathlib.Path, float, int] | None:
    """Find one jsonl file that is missing from the index or stale.

    Also opportunistically purges entries for files that no longer exist on
    disk; returns None when there's nothing left to do.
    """
    conn = _index_conn()
    indexed = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT path, mtime, size FROM files"
    )}
    seen: set[str] = set()
    pending: tuple[pathlib.Path, float, int] | None = None
    if PROJECTS.exists():
        for p in PROJECTS.glob("*/*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            ps = str(p)
            seen.add(ps)
            prev = indexed.get(ps)
            if prev is None or prev[0] != st.st_mtime or prev[1] != st.st_size:
                if pending is None:
                    pending = (p, st.st_mtime, st.st_size)
    if pending is not None:
        return pending
    for ps in indexed:
        if ps not in seen:
            _index_purge(ps)
            return None  # one unit of work per cycle
    return None


def _index_lookup(query: str) -> set[str] | None:
    """Return candidate jsonl paths for `query`, or None if index can't filter.

    For each whitespace-split word in the query, the file must contain *all*
    bigrams of that word (so each word has at least one possible occurrence).
    Words shorter than 2 codepoints contribute nothing to filtering. If no
    word contributes any bigram, returns None and callers should fall back
    to a full scan.
    """
    if not query:
        return None
    words = [w for w in query.lower().split() if w]
    if not words:
        return None
    conn = _index_conn()
    final: set[str] | None = None
    used_index = False
    for w in words:
        grams = _bigrams_of(w)
        if not grams:
            continue
        word_paths: set[str] | None = None
        for g in grams:
            cur = conn.execute("SELECT path FROM postings WHERE gram = ?", (g,))
            paths = {r[0] for r in cur}
            if word_paths is None:
                word_paths = paths
            else:
                word_paths &= paths
            if not word_paths:
                break
        if word_paths is None:
            continue
        used_index = True
        if final is None:
            final = word_paths
        else:
            final &= word_paths
        if not final:
            return final
    if not used_index:
        return None
    return final or set()


def _search_sessions(query: str, use_regex: bool = False) -> tuple[list[str], dict[str, list[dict]], dict[str, int]]:
    """Search every jsonl for the query.

    Word-AND mode (default): split query by whitespace; a session matches when
    every word appears somewhere in the file. Snippets are taken from any line
    where one of the words hit.

    Regex mode: query is compiled as a single case-insensitive regex; a session
    matches when at least one line matches.
    """
    if not query or not PROJECTS.exists():
        return [], {}, {}
    pat = None
    pat_bytes = None
    phrase_pat = None
    phrase_pat_bytes = None
    word_strs: list[str] = []
    if use_regex:
        try:
            pat = re.compile(query, re.IGNORECASE)
            pat_bytes = re.compile(query.encode("utf-8"), re.IGNORECASE)
        except re.error:
            return [], {}, {}
    else:
        word_strs = [w for w in query.lower().split() if w]
        if not word_strs:
            return [], {}, {}
        if len(word_strs) >= 2:
            joined = r"[\-_\s]*".join(re.escape(w) for w in word_strs)
            phrase_pat = re.compile(joined, re.IGNORECASE)
            phrase_pat_bytes = re.compile(joined.encode("utf-8"), re.IGNORECASE)

    words_bytes = [w.encode("utf-8") for w in word_strs]

    sids: list[str] = []
    snippets: dict[str, list[dict]] = {}
    scores: dict[str, int] = {}

    candidate_paths: list[pathlib.Path]
    if use_regex:
        candidate_paths = list(PROJECTS.glob("*/*.jsonl"))
    else:
        try:
            cand = _index_lookup(query)
        except sqlite3.Error:
            cand = None
        if cand is None:
            candidate_paths = list(PROJECTS.glob("*/*.jsonl"))
        else:
            candidate_paths = [pathlib.Path(p) for p in cand]

    for path in candidate_paths:
        try:
            with path.open("rb") as f:
                phrase_snips: list[dict] = []
                word_snips: list[dict] = []
                found: set[bytes] = set()
                phrase_hit = False
                for raw_line in f:
                    line_lower = raw_line.lower()
                    fallback: str | None = None
                    line_matches = False
                    is_phrase_line = False
                    if pat_bytes is not None:
                        if pat_bytes.search(raw_line):
                            line_matches = True
                            found.add(b"__regex__")
                    else:
                        for w_bytes, w_str in zip(words_bytes, word_strs):
                            if w_bytes in line_lower:
                                if w_bytes not in found:
                                    found.add(w_bytes)
                                if fallback is None:
                                    fallback = w_str
                                line_matches = True
                        if phrase_pat_bytes is not None and phrase_pat_bytes.search(line_lower):
                            is_phrase_line = True
                            phrase_hit = True
                    if is_phrase_line and len(phrase_snips) < _MAX_SNIPPETS_PER_FILE:
                        snip = _make_snippet(raw_line, fallback or "", pat, phrase_pat)
                        if snip is not None:
                            phrase_snips.append(snip)
                    elif line_matches and not is_phrase_line and len(word_snips) < _MAX_SNIPPETS_PER_FILE:
                        snip = _make_snippet(raw_line, fallback or "", pat, phrase_pat)
                        if snip is not None:
                            word_snips.append(snip)
        except OSError:
            continue
        if pat_bytes is not None:
            qualifies = b"__regex__" in found
        else:
            qualifies = len(found) == len(words_bytes)
        if qualifies:
            sids.append(path.stem)
            file_snips = (phrase_snips + word_snips)[:_MAX_SNIPPETS_PER_FILE]
            if file_snips:
                snippets[path.stem] = file_snips
            scores[path.stem] = 2 if phrase_hit else 1
    return sids, snippets, scores


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
  .search-spinner {{
    position: absolute;
    right: 12px;
    top: 50%;
    width: 12px;
    height: 12px;
    margin-top: -6px;
    border: 1.5px solid var(--muted-soft);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    display: none;
  }}
  body.searching .search-spinner {{ display: block; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
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

  .snippets {{ display: flex; flex-direction: column; gap: 4px; cursor: pointer; }}
  .snippets:hover {{ background: var(--row-hover); }}
  .snippet {{
    display: flex;
    align-items: baseline;
    gap: 8px;
    font-size: 12px;
    line-height: 1.55;
    color: var(--fg-dim);
  }}
  .snippet-role {{
    flex: 0 0 auto;
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--muted);
    border: 1px solid var(--border);
    padding: 1px 5px;
    border-radius: 2px;
  }}
  .snippet-text {{
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .snippet mark, .modal-body mark {{
    background: var(--accent-soft);
    color: var(--accent);
    padding: 0 2px;
    font-weight: 600;
    border-radius: 2px;
  }}

  .modal-backdrop {{
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
    padding: 40px;
  }}
  .modal {{
    background: var(--surface);
    border: 1px solid var(--border-strong);
    width: min(900px, 100%);
    max-height: 100%;
    display: flex;
    flex-direction: column;
    box-shadow: 0 20px 60px rgba(0,0,0,0.4);
  }}
  .modal-head {{
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 16px 22px;
    border-bottom: 1px solid var(--border);
  }}
  .modal-title {{
    flex: 1;
    font-family: "Fraunces", serif;
    font-style: italic;
    font-size: 22px;
    color: var(--fg);
  }}
  .modal-close {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-dim);
    font-size: 18px;
    width: 32px;
    height: 32px;
    cursor: pointer;
    line-height: 1;
  }}
  .modal-close:hover {{ color: var(--accent); border-color: var(--accent); }}
  .modal-meta {{
    padding: 10px 22px;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    color: var(--muted);
    font-family: "JetBrains Mono", monospace;
  }}
  .modal-meta .sep {{ margin: 0 10px; color: var(--muted-soft); }}
  .modal-body {{
    flex: 1;
    overflow-y: auto;
    padding: 16px 22px 22px;
    display: flex;
    flex-direction: column;
    gap: 18px;
  }}
  .modal-msg {{
    border-left: 2px solid var(--border-strong);
    padding: 6px 12px;
  }}
  .modal-msg.has-hit {{ border-left-color: var(--accent); }}
  .modal-msg-role {{
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--muted);
    margin-bottom: 6px;
  }}
  .modal-msg.has-hit .modal-msg-role {{ color: var(--accent); }}
  .modal-msg-text {{
    font-size: 12.5px;
    line-height: 1.65;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--fg-dim);
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
  .empty-search {{
    display: none;
    align-items: center;
    justify-content: center;
    gap: 10px;
  }}
  .empty-spinner {{
    width: 14px;
    height: 14px;
    border: 1.5px solid var(--muted-soft);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }}
  body.searching .empty .empty-text {{ display: none; }}
  body.searching .empty .empty-search {{ display: inline-flex; }}
  body.searching .empty::before {{ content: "" ; }}

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
        <span id="search-spinner" class="search-spinner" aria-hidden="true"></span>
      </div>
      <button id="regex-mode" class="btn" type="button" title="treat query as regex"><span class="check">□</span>regex</button>
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
            <th id="col-context">last prompt</th>
            <th>last</th>
            <th>resume</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
      <div class="empty" id="empty" style="display:none">
        <span class="empty-text">no sessions match your filters</span>
        <span class="empty-search"><span class="empty-spinner" aria-hidden="true"></span>searching all sessions…</span>
      </div>
    </main>
  </div>
</div>

<div id="modal-backdrop" class="modal-backdrop" style="display:none">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <div class="modal-title" id="modal-title"></div>
      <button class="modal-close" id="modal-close" type="button" aria-label="close">×</button>
    </div>
    <div class="modal-meta" id="modal-meta"></div>
    <div class="modal-body" id="modal-body"></div>
  </div>
</div>

<script>
const ROWS = new Map();  // session_id -> row
let DATA = [];
let GENERATED_AT = "";
let SEARCH_RESULT = null;  // null = no active search; Set<session_id> when active
let SEARCH_SNIPPETS = {{}};  // session_id -> snippet array (role, before, match, after)
let SEARCH_SCORES = {{}};    // session_id -> int (2 = phrase hit, 1 = word matches)
let SEARCH_REQ_ID = 0;
let SEARCH_REGEX_MODE = localStorage.getItem("sessions-regex") === "1";

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
  const contextCell = renderContextCell(r);
  const resumeArg = r.custom_title ? `"${{r.custom_title}}"` : r.session_id;
  const cmd = r.cwd
    ? `cd "${{r.cwd}}" && claude --resume ${{resumeArg}}`
    : `claude --resume ${{resumeArg}}`;
  return `
    <tr>
      <td><div class="cell-name"><span class="${{activeCls}}"></span>${{name}}</div></td>
      <td>${{cwdCell}}</td>
      <td>${{contextCell}}</td>
      <td>${{renderLastCell(r)}}</td>
      <td><button class="copy" data-cmd="${{escapeHtml(cmd)}}">copy</button></td>
    </tr>`;
}}

function renderLastCell(r) {{
  if (SEARCH_RESULT !== null) {{
    const ts = latestMatchTs(r.session_id);
    if (ts) {{
      return `<span class="when" title="match @ ${{new Date(ts*1000).toISOString()}}">${{fmtWhen(ts)}}</span>`;
    }}
  }}
  return `<span class="when" title="${{new Date(r.mtime*1000).toISOString()}}">${{fmtWhen(r.mtime)}}</span>`;
}}

function latestMatchTs(sid) {{
  const snips = SEARCH_SNIPPETS[sid];
  if (!snips || !snips.length) return null;
  let max = 0;
  for (const s of snips) {{
    if (typeof s.ts === "number" && s.ts > max) max = s.ts;
  }}
  return max || null;
}}

function renderContextCell(r) {{
  const snips = SEARCH_SNIPPETS[r.session_id];
  if (snips && snips.length) {{
    const items = snips.map(s => {{
      const role = escapeHtml(s.role || "?");
      const before = escapeHtml(s.before || "");
      const match = escapeHtml(s.match || "");
      const after = escapeHtml(s.after || "");
      return `<div class="snippet"><span class="snippet-role">${{role}}</span><span class="snippet-text">${{before}}<mark>${{match}}</mark>${{after}}</span></div>`;
    }}).join("");
    return `<div class="snippets" data-sid="${{escapeHtml(r.session_id)}}" title="click to view full context">${{items}}</div>`;
  }}
  const prompt = r.last_prompt ? escapeHtml(r.last_prompt) : "";
  return `<div class="prompt" title="${{escapeHtml(r.last_prompt || "")}}">${{prompt}}</div>`;
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
  if (SEARCH_RESULT !== null) {{
    matches.sort((a, b) => {{
      const sa = SEARCH_SCORES[a.session_id] ?? 1;
      const sb = SEARCH_SCORES[b.session_id] ?? 1;
      if (sa !== sb) return sb - sa;
      const ta = latestMatchTs(a.session_id) ?? a.mtime;
      const tb = latestMatchTs(b.session_id) ?? b.mtime;
      return tb - ta;
    }});
  }}
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
  tbody.querySelectorAll(".snippets").forEach(cell => {{
    cell.addEventListener("click", (e) => {{
      e.stopPropagation();
      openSnippetModal(cell.dataset.sid);
    }});
  }});
}}

function buildHighlightRegex() {{
  const q = document.getElementById("q").value.trim();
  if (!q) return null;
  if (SEARCH_REGEX_MODE) {{
    try {{ return new RegExp(q, "gi"); }} catch (e) {{ return null; }}
  }}
  const words = q.split(/\\s+/).filter(Boolean);
  if (!words.length) return null;
  const esc = s => s.replace(/[.*+?^${{}}()|[\\]\\\\]/g, "\\\\$&");
  // longest words first so phrase patterns win over individual words
  const sortedWords = [...words].sort((a, b) => b.length - a.length);
  const phrase = words.map(esc).join("[\\\\-_\\\\s]*");
  const parts = [phrase, ...sortedWords.map(esc)];
  return new RegExp("(" + parts.join("|") + ")", "gi");
}}

function highlightText(text, re) {{
  if (!re) return escapeHtml(text);
  let out = "";
  let last = 0;
  re.lastIndex = 0;
  let m;
  while ((m = re.exec(text)) !== null) {{
    if (m.index === re.lastIndex) {{ re.lastIndex++; continue; }}
    out += escapeHtml(text.slice(last, m.index));
    out += "<mark>" + escapeHtml(m[0]) + "</mark>";
    last = m.index + m[0].length;
  }}
  out += escapeHtml(text.slice(last));
  return out;
}}

function openSnippetModal(sid) {{
  const row = ROWS.get(sid);
  const snips = SEARCH_SNIPPETS[sid] || [];
  if (!row) return;
  const re = buildHighlightRegex();
  const title = row.custom_title || "(unnamed)";
  document.getElementById("modal-title").textContent = title;
  const cwd = row.cwd ? prettyPath(row.cwd) : "(unknown)";
  document.getElementById("modal-meta").innerHTML =
    `<span>${{escapeHtml(cwd)}}</span><span class="sep">·</span><span>${{escapeHtml(shortId(sid))}}</span><span class="sep">·</span><span>${{snips.length}} match block${{snips.length === 1 ? "" : "s"}}</span>`;
  const body = document.getElementById("modal-body");
  if (!snips.length) {{
    body.innerHTML = `<div class="modal-msg"><div class="modal-msg-text">${{escapeHtml(row.last_prompt || "(no preview available)")}}</div></div>`;
  }} else {{
    body.innerHTML = snips.map(s => {{
      const role = escapeHtml(s.role || "?");
      const text = s.full || ((s.before || "") + (s.match || "") + (s.after || ""));
      return `<div class="modal-msg has-hit"><div class="modal-msg-role">${{role}}</div><div class="modal-msg-text">${{highlightText(text, re)}}</div></div>`;
    }}).join("");
  }}
  document.getElementById("modal-backdrop").style.display = "flex";
}}

function closeSnippetModal() {{
  document.getElementById("modal-backdrop").style.display = "none";
}}

document.getElementById("modal-close").addEventListener("click", closeSnippetModal);
document.getElementById("modal-backdrop").addEventListener("click", (e) => {{
  if (e.target.id === "modal-backdrop") closeSnippetModal();
}});
document.addEventListener("keydown", (e) => {{
  if (e.key === "Escape" && document.getElementById("modal-backdrop").style.display !== "none") {{
    closeSnippetModal();
  }}
}});

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

function syncContextHeader() {{
  const el = document.getElementById("col-context");
  if (!el) return;
  el.textContent = SEARCH_RESULT === null ? "last prompt" : "matches";
}}

function localSearchIds(q) {{
  const out = new Set();
  if (SEARCH_REGEX_MODE) {{
    let re;
    try {{ re = new RegExp(q, "i"); }} catch (e) {{ return out; }}
    for (const r of DATA) {{
      const hay = [r.custom_title, r.cwd, r.session_id, r.last_prompt].filter(Boolean).join(" ");
      if (re.test(hay)) out.add(r.session_id);
    }}
    return out;
  }}
  const words = q.toLowerCase().split(/\\s+/).filter(Boolean);
  if (!words.length) return out;
  for (const r of DATA) {{
    const hay = [r.custom_title, r.cwd, r.session_id, r.last_prompt].filter(Boolean).join(" ").toLowerCase();
    if (words.every(w => hay.includes(w))) out.add(r.session_id);
  }}
  return out;
}}

async function updateSearch() {{
  const q = document.getElementById("q").value.trim();
  if (!q) {{
    SEARCH_RESULT = null;
    SEARCH_SNIPPETS = {{}};
    SEARCH_SCORES = {{}};
    document.body.classList.remove("searching");
    syncContextHeader();
    render();
    return;
  }}
  const reqId = ++SEARCH_REQ_ID;
  SEARCH_RESULT = localSearchIds(q);
  SEARCH_SNIPPETS = {{}};
  SEARCH_SCORES = {{}};
  syncContextHeader();
  render();
  document.body.classList.add("searching");
  try {{
    const url = "/search?q=" + encodeURIComponent(q) + (SEARCH_REGEX_MODE ? "&regex=1" : "");
    const res = await fetch(url);
    if (reqId !== SEARCH_REQ_ID) return;
    const d = await res.json();
    const merged = new Set(SEARCH_RESULT);
    for (const sid of (d.session_ids || [])) merged.add(sid);
    SEARCH_RESULT = merged;
    SEARCH_SNIPPETS = d.snippets || {{}};
    SEARCH_SCORES = d.scores || {{}};
    render();
  }} catch (e) {{
    // network error: keep local-only results, no snippets
  }} finally {{
    if (reqId === SEARCH_REQ_ID) document.body.classList.remove("searching");
  }}
}}

let _searchTimer;
document.getElementById("q").addEventListener("input", () => {{
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(updateSearch, 200);
}});

(function initRegexBtn() {{
  const btn = document.getElementById("regex-mode");
  const check = btn.querySelector(".check");
  function sync() {{
    btn.classList.toggle("active", SEARCH_REGEX_MODE);
    if (check) check.textContent = SEARCH_REGEX_MODE ? "■" : "□";
  }}
  sync();
  btn.addEventListener("click", () => {{
    SEARCH_REGEX_MODE = !SEARCH_REGEX_MODE;
    localStorage.setItem("sessions-regex", SEARCH_REGEX_MODE ? "1" : "0");
    sync();
    if (document.getElementById("q").value.trim()) updateSearch();
  }});
}})();

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
        if upsert or remove:
            msg = f"[{time.strftime('%H:%M:%S')}] delta: +{len(upsert)} -{len(remove)}"
            print(msg)
            _publish({
                "type": "delta",
                "upsert": upsert,
                "remove": remove,
                "generated": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
            })
        try:
            pending = _index_pick_pending()
        except sqlite3.Error as e:
            print(f"index pick error: {e}")
            pending = None
        if pending is not None:
            p, mt, sz = pending
            try:
                _index_file(p, mt, sz)
            except sqlite3.Error as e:
                print(f"index write error for {p}: {e}")


def serve(port: int) -> None:
    import socket
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class V6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

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
            use_regex = (qs.get("regex") or ["0"])[0] in ("1", "true", "yes")
            sids, snippets, scores = _search_sessions(q, use_regex=use_regex)
            body = json.dumps(
                {"q": q, "regex": use_regex, "session_ids": sids, "snippets": snippets, "scores": scores},
                ensure_ascii=False,
            ).encode("utf-8")
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

    # Two listeners: macOS ignores IPV6_V6ONLY=0, so ::1 alone won't accept IPv4 (browsers may pick ::1 via happy-eyeballs).
    v4 = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    v6 = V6Server(("::1", port), Handler)
    print(f"claude-sessions serving at http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    threading.Thread(target=v4.serve_forever, daemon=True).start()
    try:
        v6.serve_forever()
    except KeyboardInterrupt:
        print()
        v4.server_close()
        v6.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run the Claude Code session browser server")
    ap.add_argument("--port", type=int, default=9999, help="HTTP port (default: 9999)")
    args = ap.parse_args()
    serve(args.port)
