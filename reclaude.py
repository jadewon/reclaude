#!/usr/bin/env python3
"""reclaude - live browser for your Claude Code sessions.

Scans ~/.claude/projects/ and serves a real-time dashboard so you can find
the right session to `claude --resume` after an iTerm/terminal crash.
"""

__version__ = "0.1.5"

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
    ai_title = None
    last_prompt = None
    entrypoint = None
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
            if entrypoint is None and isinstance(obj.get("entrypoint"), str):
                entrypoint = obj["entrypoint"]
            t = obj.get("type")
            if t == "custom-title" and obj.get("customTitle"):
                custom_title = obj["customTitle"]
            elif t == "agent-name" and obj.get("agentName") and not custom_title:
                custom_title = obj["agentName"]
            elif t == "ai-title" and obj.get("aiTitle"):
                # Auto-generated summary of the session, rewritten as the
                # conversation goes on — keep the last one. Kept separate from
                # custom_title because it is NOT a resume key: `claude --resume`
                # only accepts an explicitly-set name.
                ai_title = obj["aiTitle"]
            # The last human prompt is derived by scanning, not read off the
            # `last-prompt` line that also lives in the jsonl: despite the
            # name, its `leafUuid` points at whatever line is currently the
            # conversation leaf (usually a tool_result or attachment), not at
            # the last thing the human typed.
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
        "ai_title": ai_title,
        "last_prompt": last_prompt,
        "entrypoint": entrypoint,
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
_MESSAGE_TEXT_CAP = 8000  # per-message text limit in conversation view
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
    tur = obj.get("toolUseResult")
    if isinstance(tur, dict):
        for k in ("stdout", "stderr", "content", "output"):
            v = tur.get(k)
            if isinstance(v, str):
                parts.append(v)
    elif isinstance(tur, str):
        parts.append(tur)
    return "\n".join(p for p in parts if p)


def _classify_role(obj: dict) -> str:
    """Map jsonl type + content shape to a user-meaningful role label.

    The wire type is just `user`/`assistant`, but tool_result is delivered
    as type=user (it's Claude reading tool output) and tool_use is delivered
    as type=assistant (Claude calling a tool). Both look wrong to a human
    reading the transcript, so split them out.

    Local `!cmd` / slash-command output is also recorded as a type=user string
    message wrapped in <bash-stdout>/<local-command-stdout> etc. — it's machine
    output, not something the human typed, so classify it as "tool" too.
    """
    t = obj.get("type")
    if t == "user":
        if obj.get("isMeta"):
            return "meta"
        msg = obj.get("message") or {}
        c = msg.get("content")
        head = None
        if isinstance(c, str):
            head = c.lstrip()
        elif isinstance(c, list):
            for blk in c:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "tool_result":
                    return "tool"
                if blk.get("type") == "text" and head is None:
                    head = (blk.get("text") or "").lstrip()
        if head and head.startswith((
            "<bash-stdout>", "<bash-stderr>", "<bash-input>",
            "<local-command-stdout>", "<local-command-stderr>",
        )):
            return "tool"
        return "user"
    if t == "assistant":
        msg = obj.get("message") or {}
        c = msg.get("content")
        if isinstance(c, list):
            has_text = any(
                isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
                for b in c
            )
            has_tool = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in c)
            if has_tool and not has_text:
                return "tool"
        return "assistant"
    # everything else (system, summary, attachment, file-history-snapshot, …)
    # collapses to meta so the four user/assistant/tool/meta filters cover all.
    return "meta"


def _make_snippet(raw_line: bytes, fallback_word: str, regex_pat, phrase_pat, obj: dict | None = None) -> dict | None:
    """Parse one jsonl line and return a snippet dict around the best hit.

    Match-finding priority:
      1. phrase_pat (multi-word query joined with optional [-_\\s]* separators)
      2. regex_pat (when in regex mode)
      3. fallback_word (a single literal word from word-AND mode)

    Pass `obj` to reuse a line already parsed by the caller (role filtering)
    and avoid a second json.loads of the same line.
    """
    if obj is None:
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
    role = _classify_role(obj)
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

    The candidate set is:

        (files whose postings match every word's bigrams)
        ∪ (files on disk that are missing from the index or stale)

    The second term is what makes search correct *while* the index is still
    being built or while a recently-modified file hasn't been re-indexed yet:
    those files are always handed to the verification scan rather than being
    silently excluded. As more files become fresh in the index, the "unknown"
    term shrinks and the speedup grows.

    Returns None when no word in the query produced any usable bigram (e.g.
    every word is a single codepoint), so callers fall back to a full scan.
    """
    if not query:
        return None
    words = [w for w in query.lower().split() if w]
    if not words:
        return None
    conn = _index_conn()
    matched: set[str] | None = None
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
        if matched is None:
            matched = word_paths
        else:
            matched &= word_paths
    if not used_index:
        return None
    if matched is None:
        matched = set()

    # Files whose freshness the index can't yet vouch for must be verified
    # by the line scanner — silently skipping them would produce false
    # negatives during initial build and during the small window after a
    # file changes but before the watcher re-indexes it.
    indexed = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT path, mtime, size FROM files"
    )}
    unknown: set[str] = set()
    if PROJECTS.exists():
        for p in PROJECTS.glob("*/*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            ps = str(p)
            prev = indexed.get(ps)
            if prev is None or prev[0] != st.st_mtime or prev[1] != st.st_size:
                unknown.add(ps)
    return matched | unknown


_SEARCH_TIME_BUDGET = 2.0  # seconds of line-scan before truncating (newest-first)


def _search_sessions(
    query: str, use_regex: bool = False, roles: set[str] | None = None
) -> tuple[list[str], dict[str, list[dict]], dict[str, int], bool]:
    """Search every jsonl for the query.

    Word-AND mode (default): split query by whitespace; a session matches when
    every word appears somewhere in the file. Snippets are taken from any line
    where one of the words hit.

    Regex mode: query is compiled as a single case-insensitive regex; a session
    matches when at least one line matches.

    `roles` restricts which message kinds (per `_classify_role`: user /
    assistant / tool / meta) a hit may come from. None means no restriction;
    an empty set matches nothing. Lines are only parsed (to classify) when they
    already contain a raw byte hit, so the common non-matching line stays cheap.
    """
    if not query or not PROJECTS.exists():
        return [], {}, {}, False
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
            return [], {}, {}, False
    else:
        word_strs = [w for w in query.lower().split() if w]
        if not word_strs:
            return [], {}, {}, False
        if len(word_strs) >= 2:
            joined = r"[\-_\s]*".join(re.escape(w) for w in word_strs)
            phrase_pat = re.compile(joined, re.IGNORECASE)
            phrase_pat_bytes = re.compile(joined.encode("utf-8"), re.IGNORECASE)

    words_bytes = [w.encode("utf-8") for w in word_strs]
    # Per-word word-boundary regex (ASCII boundary only — non-ASCII words like
    # Korean are passed through as None, treating any occurrence as bounded so
    # we don't penalise compound words.
    bound_pats: list[re.Pattern[bytes] | None] = []
    for wb in words_bytes:
        if wb.isascii():
            bound_pats.append(
                re.compile(rb"(?<![A-Za-z0-9_])" + re.escape(wb) + rb"(?![A-Za-z0-9_])", re.IGNORECASE)
            )
        else:
            bound_pats.append(None)

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

    # The bigram index barely narrows common words (every session shares most
    # bigrams), so the verify+snippet pass would otherwise read every matched
    # file. Scan newest-first and stop after a wall-clock budget; the UI ranks
    # by recency anyway, and narrowing the query restores the missing tail.
    def _mtime(p: pathlib.Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    candidate_paths.sort(key=_mtime, reverse=True)
    deadline = time.monotonic() + _SEARCH_TIME_BUDGET
    truncated = False

    for i, path in enumerate(candidate_paths):
        if i and i % 32 == 0 and time.monotonic() > deadline:
            truncated = True
            break
        try:
            with path.open("rb") as f:
                phrase_cands: list[tuple] = []
                word_cands: list[tuple] = []
                found: set[bytes] = set()
                bounded_found: set[bytes] = set()
                phrase_hit = False
                for raw_line in f:
                    line_lower = raw_line.lower()
                    # Fast byte prefilter: skip lines with no raw hit at all so
                    # we never parse JSON for the overwhelming majority of lines.
                    if pat_bytes is not None:
                        if not pat_bytes.search(raw_line):
                            continue
                    elif not any(wb in line_lower for wb in words_bytes):
                        continue
                    # This line has a raw hit. When a role filter is active, parse
                    # it once and drop the whole line unless it's an allowed role
                    # (so a keyword that only appears in git log / tool output does
                    # not qualify the session).
                    obj = None
                    if roles is not None:
                        try:
                            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if not isinstance(obj, dict) or _classify_role(obj) not in roles:
                            continue
                    fallback: str | None = None
                    line_matches = False
                    is_phrase_line = False
                    if pat_bytes is not None:
                        line_matches = True
                        found.add(b"__regex__")
                    else:
                        for w_bytes, w_str, bp in zip(words_bytes, word_strs, bound_pats):
                            if w_bytes in line_lower:
                                if w_bytes not in found:
                                    found.add(w_bytes)
                                if w_bytes not in bounded_found:
                                    if bp is None or bp.search(line_lower):
                                        bounded_found.add(w_bytes)
                                if fallback is None:
                                    fallback = w_str
                                line_matches = True
                        if phrase_pat_bytes is not None and phrase_pat_bytes.search(line_lower):
                            is_phrase_line = True
                            phrase_hit = True
                    # Collect match candidates, keeping only the most RECENT few
                    # of each kind (rows are recency-ranked and the latest mention
                    # is usually the session's conclusion). Snippets are built
                    # after the loop, so a keyword in the final answer is never
                    # hidden by a first-N-then-truncate window.
                    if is_phrase_line:
                        phrase_cands.append((raw_line, obj, fallback or ""))
                        if len(phrase_cands) > _MAX_SNIPPETS_PER_FILE:
                            phrase_cands.pop(0)
                    elif line_matches:
                        word_cands.append((raw_line, obj, fallback or ""))
                        if len(word_cands) > _MAX_SNIPPETS_PER_FILE:
                            word_cands.pop(0)
        except OSError:
            continue
        if pat_bytes is not None:
            qualifies = b"__regex__" in found
        else:
            qualifies = len(found) == len(words_bytes)
        if qualifies:
            file_snips: list[dict] = []
            for cand_line, cand_obj, cand_fb in (phrase_cands + word_cands):
                snip = _make_snippet(cand_line, cand_fb, pat, phrase_pat, obj=cand_obj)
                if snip is not None:
                    file_snips.append(snip)
                if len(file_snips) >= _MAX_SNIPPETS_PER_FILE:
                    break
            # Suppress matches we cannot present to the user — when the keyword
            # only appears in opaque JSON fields (uuid/timestamp/promptId/etc.)
            # the snippet extractor returns nothing, and the row would render
            # with an unrelated last_prompt that looks like a false positive.
            # The client still surfaces metadata-only matches via localSearchIds.
            if not file_snips:
                continue
            sids.append(path.stem)
            snippets[path.stem] = file_snips
            if phrase_hit:
                scores[path.stem] = 3
            elif pat_bytes is None and len(bounded_found) == len(words_bytes):
                # All query words appear as standalone tokens somewhere in the
                # file — outranks substring-only hits like 'enter' inside 'center'.
                scores[path.stem] = 2
            else:
                scores[path.stem] = 1
    return sids, snippets, scores, truncated


def _message_blocks(obj: dict) -> list[dict]:
    """Split one jsonl line into renderable blocks: text / tool_use / tool_result.

    Each block has {kind, text} and optionally {name} for tool_use.
    Order matches the original content list. Empty blocks are dropped.
    """
    out: list[dict] = []
    msg = obj.get("message")
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            if c.strip():
                out.append({"kind": "text", "text": c})
        elif isinstance(c, list):
            for block in c:
                if not isinstance(block, dict):
                    continue
                t = block.get("type")
                if t == "text":
                    txt = (block.get("text") or "").strip()
                    if txt:
                        out.append({"kind": "text", "text": txt})
                elif t == "tool_use":
                    name = block.get("name") or ""
                    inp = block.get("input")
                    try:
                        body = json.dumps(inp, ensure_ascii=False, indent=2) if inp is not None else ""
                    except (TypeError, ValueError):
                        body = ""
                    out.append({"kind": "tool_use", "name": name, "text": body})
                elif t == "tool_result":
                    bc = block.get("content")
                    chunks: list[str] = []
                    if isinstance(bc, str):
                        chunks.append(bc)
                    elif isinstance(bc, list):
                        for sub in bc:
                            if isinstance(sub, dict) and sub.get("type") == "text":
                                chunks.append(sub.get("text") or "")
                    body = "\n".join(c for c in chunks if c)
                    if body.strip():
                        out.append({"kind": "tool_result", "text": body})
    if obj.get("type") == "summary" and isinstance(obj.get("summary"), str):
        out.append({"kind": "text", "text": obj["summary"]})
    if obj.get("type") == "custom-title" and isinstance(obj.get("customTitle"), str):
        out.append({"kind": "text", "text": obj["customTitle"]})
    if obj.get("type") == "attachment":
        att = obj.get("attachment")
        if isinstance(att, dict):
            ac = att.get("content")
            if isinstance(ac, str):
                if ac.strip():
                    out.append({"kind": "tool_result", "text": ac})
            elif isinstance(ac, list):
                for sub in ac:
                    if isinstance(sub, dict):
                        for k in ("text", "content"):
                            v = sub.get(k)
                            if isinstance(v, str) and v.strip():
                                out.append({"kind": "tool_result", "text": v})
    # toolUseResult duplicates message.content's tool_result on most lines;
    # only emit it when the inner content didn't already carry a result block.
    already_has_result = any(b["kind"] == "tool_result" for b in out)
    if not already_has_result:
        tur = obj.get("toolUseResult")
        if isinstance(tur, dict):
            chunks = []
            for k in ("stdout", "stderr", "content", "output"):
                v = tur.get(k)
                if isinstance(v, str) and v.strip():
                    chunks.append(v if k != "stderr" else f"[{k}]\n{v}")
            body = "\n".join(chunks)
            if body.strip():
                out.append({"kind": "tool_result", "text": body})
        elif isinstance(tur, str) and tur.strip():
            out.append({"kind": "tool_result", "text": tur})
    # cap each block's text size
    for b in out:
        if len(b["text"]) > _MESSAGE_TEXT_CAP:
            b["text"] = b["text"][:_MESSAGE_TEXT_CAP] + "\n…(truncated)"
    return out


def _session_messages(sid: str, end: int | None, count: int) -> dict | None:
    """Return a slice of human-readable messages from session `sid`.

    Layout is oldest-first (index 0 = first message, total-1 = newest).
    end=None means "newest" (== total). The returned slice covers
    [max(0, end - count), end). Each message: {idx, role, ts, blocks}.
    Returns None if the session jsonl can't be found.
    """
    if not sid or "/" in sid or ".." in sid:
        return None
    matches = list(PROJECTS.glob(f"*/{sid}.jsonl"))
    if not matches:
        return None
    path = matches[0]
    msgs: list[dict] = []
    try:
        with path.open("rb") as f:
            for idx, raw_line in enumerate(f):
                try:
                    obj = json.loads(raw_line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                blocks = _message_blocks(obj)
                if not blocks:
                    continue
                role = _classify_role(obj)
                ts: float | None = None
                raw_ts = obj.get("timestamp")
                if isinstance(raw_ts, str):
                    try:
                        ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        ts = None
                msgs.append({"idx": idx, "role": role, "ts": ts, "blocks": blocks})
    except OSError:
        return None
    total = len(msgs)
    if end is None or end > total:
        end = total
    if end < 0:
        end = 0
    start = max(0, end - max(1, count))
    return {
        "sid": sid,
        "total": total,
        "slice_start": start,
        "slice_end": end,
        "messages": msgs[start:end],
    }


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
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/styles/atom-one-dark.min.css">
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js" defer></script>
<script src="https://cdn.jsdelivr.net/npm/dompurify@3.1.7/dist/purify.min.js" defer></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.11.1/highlight.min.js" defer></script>
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
  :root {{ --zoom: 1; }}
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
    zoom: var(--zoom);
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
    align-items: flex-start;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    position: sticky;
    top: 0;
    z-index: 10;
  }}
  .toolbar > .btn, .toolbar > .count, .toolbar > .truncated-note {{ margin-top: 1px; }}
  .search-col {{
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 7px;
  }}
  .search-wrap {{
    position: relative;
  }}
  .role-filter {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
  }}
  .role-label {{
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
    margin-right: 2px;
  }}
  .role-chip {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-dim);
    padding: 3px 9px;
    cursor: pointer;
    font-family: inherit;
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: lowercase;
    white-space: nowrap;
    border-radius: 999px;
  }}
  .role-chip:hover {{ border-color: var(--border-strong); color: var(--fg); }}
  .role-chip.active {{
    border-color: var(--accent);
    background: var(--accent-soft);
    color: var(--accent);
  }}
  .role-chip .check {{ margin-right: 4px; color: var(--muted-soft); }}
  .role-chip.active .check {{ color: var(--accent); }}
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
  .truncated-note {{
    font-size: 10px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--accent);
    border: 1px solid var(--accent);
    padding: 1px 6px;
    margin-left: 8px;
    cursor: help;
  }}

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
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    position: sticky;
    top: var(--thead-top, 61px);
    background: var(--bg);
    font-size: 10px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
  }}
  thead th:first-child {{ padding-left: 24px; }}
  thead th:last-child {{ padding-right: 24px; text-align: right; }}
  tbody td {{
    padding: 6px 12px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  tbody td:first-child {{ padding-left: 24px; }}
  tbody td:last-child {{ padding-right: 24px; text-align: right; }}
  tbody tr {{ transition: background 0.08s ease; }}
  tbody tr:hover {{ background: var(--row-hover); }}

  /* ───── cells ───── */
  .cell-name {{ display: flex; align-items: center; gap: 10px; min-width: 0; }}
  .name-stack {{ display: flex; flex-direction: column; gap: 3px; min-width: 0; }}
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
  /* auto title: same shape, no accent — reads as generated, not authored.
     It is a sentence rather than a short alias, so allow two lines before
     ellipsizing; one line cuts most of them off mid-word. */
  .pill.ai {{
    background: transparent;
    color: var(--fg-dim);
    border-color: var(--border-strong);
    font-weight: 400;
    white-space: normal;
    line-height: 1.35;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
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
  .sid-copy {{ cursor: pointer; }}
  .sid-copy:hover {{ color: var(--accent); }}
  .sid-copy.copied {{ color: var(--green); }}

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
  /* the match the user is currently parked on (search nav in the viewer) */
  .modal-body mark.active-hit {{
    background: var(--accent);
    color: var(--bg);
    box-shadow: 0 0 0 2px var(--accent);
  }}
  .conv-hits {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
    margin-right: 8px;
  }}
  .conv-hit-pos {{
    font-size: 11px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.04em;
    min-width: 44px;
    text-align: center;
  }}
  .conv-hit-btn {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-dim);
    width: 24px;
    height: 24px;
    line-height: 1;
    cursor: pointer;
    font-family: inherit;
    font-size: 15px;
    border-radius: 4px;
  }}
  .conv-hit-btn:hover:not(:disabled) {{ border-color: var(--accent); color: var(--accent); }}
  .conv-hit-btn:disabled {{ opacity: 0.3; cursor: default; }}
  .conv-hit-label {{
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.03em;
    margin-right: 6px;
    white-space: nowrap;
  }}
  .conv-title-id {{
    color: var(--muted);
    font-weight: 500;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.9em;
  }}
  .conv-title-sep {{ color: var(--muted-soft); margin: 0 10px; }}
  .conv-title-ico {{ color: var(--muted); margin-right: 5px; font-weight: 400; }}
  .conv-title-q {{ font-weight: 400; font-size: 0.92em; }}
  #conv-title mark {{
    background: var(--accent-soft);
    color: var(--accent);
    padding: 0 4px;
    margin: 0 3px;
    border-radius: 2px;
    font-weight: 600;
  }}
  .conv-roles {{
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    padding: 8px 18px 10px;
    border-bottom: 1px solid var(--border);
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
  .modal-expand {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--fg-dim);
    font-size: 14px;
    width: 32px;
    height: 32px;
    cursor: pointer;
    line-height: 1;
  }}
  .modal-expand:hover {{ color: var(--accent); border-color: var(--accent); }}
  .modal-backdrop.fullscreen {{ padding: 0; }}
  .modal-backdrop.fullscreen .modal {{ width: 100%; max-width: none; max-height: 100vh; height: 100vh; }}
  .modal-backdrop.fullscreen .modal-expand {{ color: var(--accent); border-color: var(--accent); }}
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
  .conv-body {{ gap: 14px; }}
  .conv-msg {{
    padding: 12px 14px;
    border-left: 3px solid var(--border-strong);
    background: var(--surface);
    border-radius: 0 2px 2px 0;
  }}
  .conv-msg[data-role="user"] {{
    border-left-color: var(--accent);
    background: var(--accent-soft);
  }}
  .conv-msg[data-role="assistant"] {{
    border-left-color: var(--link);
    background: var(--surface-hi);
  }}
  .conv-msg[data-role="tool"] {{
    border-left-color: var(--muted-soft);
    background: var(--surface);
    padding: 8px 14px;
  }}
  .conv-msg[data-role="meta"] {{
    border-left-color: var(--muted-soft);
    background: transparent;
    padding: 6px 14px;
    font-size: 11px;
    color: var(--muted);
  }}
  /* role visibility — toggled by the viewer's `show` chips */
  .conv-body .conv-msg {{ display: none; }}
  .conv-body.show-user .conv-msg[data-role="user"],
  .conv-body.show-assistant .conv-msg[data-role="assistant"],
  .conv-body.show-tool .conv-msg[data-role="tool"],
  .conv-body.show-meta .conv-msg[data-role="meta"] {{ display: block; }}
  .conv-msg-meta {{
    display: flex;
    gap: 10px;
    align-items: baseline;
    margin-bottom: 6px;
    font-size: 10px;
    color: var(--muted);
  }}
  .conv-msg-role {{
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-weight: 700;
    font-size: 9.5px;
    padding: 2px 7px;
    background: var(--bg);
    color: var(--muted);
    border-radius: 2px;
  }}
  .conv-msg[data-role="user"] .conv-msg-role {{ color: var(--accent); background: rgba(0,0,0,0.25); }}
  .conv-msg[data-role="assistant"] .conv-msg-role {{ color: var(--link); background: rgba(0,0,0,0.25); }}
  .conv-msg[data-role="tool"] .conv-msg-role {{ color: var(--muted); }}
  .conv-msg-ts {{ font-variant-numeric: tabular-nums; }}
  .conv-msg-text {{
    font-size: 12.5px;
    line-height: 1.65;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--fg-dim);
  }}
  .conv-start, .conv-empty {{
    text-align: center;
    font-size: 11px;
    color: var(--muted);
    padding: 12px 0;
    letter-spacing: 0.06em;
  }}
  .conv-msg-blocks {{
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px 10px;
    background: rgba(127, 127, 127, 0.06);
    border-radius: 3px;
  }}
  .conv-block {{}}
  .conv-block-text {{ font-size: 12.5px; line-height: 1.65; color: var(--fg); }}
  /* markdown tables inside a message: keep the (inherited) header styling but
     give the body its own surface so rows read apart from the block bg */
  .conv-block-text table {{ table-layout: auto; }}
  .conv-block-text tbody {{ background: var(--surface); }}
  .conv-block-text tbody td {{ border-bottom: 1px solid var(--border); }}
  /* tool calls / results collapse by default; click summary to expand */
  details.conv-block-tool > summary {{
    cursor: pointer;
    list-style: none;
    user-select: none;
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 4px 8px;
    background: var(--surface-hi);
    border-left: 2px solid var(--border-strong);
    font-size: 11px;
  }}
  details.conv-block-tool > summary::-webkit-details-marker {{ display: none; }}
  details.conv-block-tool > summary::before {{
    content: "▸";
    color: var(--muted-soft);
    font-size: 9px;
    display: inline-block;
    width: 8px;
  }}
  details.conv-block-tool[open] > summary::before {{ content: "▾"; color: var(--accent); }}
  details.conv-block-tool[open] > summary {{ border-left-color: var(--accent); }}
  details.conv-block-tool > summary:hover {{ background: var(--row-hover); }}
  .conv-block-label {{
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--muted);
    font-weight: 600;
  }}
  details.conv-block-tool[open] .conv-block-label {{ color: var(--accent); }}
  .conv-tool-size {{ color: var(--muted-soft); font-variant-numeric: tabular-nums; }}
  .conv-tool-preview {{
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }}
  .conv-block-raw {{
    margin: 6px 0 0;
    padding: 10px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    overflow-x: auto;
    font-size: 11.5px;
    line-height: 1.55;
  }}
  .conv-block-raw code {{ font-family: "JetBrains Mono", monospace; color: var(--fg-dim); }}

  /* markdown content */
  .md {{ font-size: 12.5px; line-height: 1.7; }}
  .md > *:first-child {{ margin-top: 0; }}
  .md > *:last-child {{ margin-bottom: 0; }}
  .md p {{ margin: 0.5em 0; }}
  .md h1, .md h2, .md h3, .md h4 {{ margin: 0.8em 0 0.4em; line-height: 1.3; color: var(--fg); font-family: "Fraunces", serif; font-style: italic; font-weight: 500; }}
  .md h1 {{ font-size: 1.45em; }}
  .md h2 {{ font-size: 1.25em; }}
  .md h3 {{ font-size: 1.1em; }}
  .md ul, .md ol {{ margin: 0.4em 0; padding-left: 1.6em; }}
  .md li {{ margin: 0.15em 0; }}
  .md blockquote {{ margin: 0.5em 0; padding: 0.2em 0.9em; border-left: 2px solid var(--border-strong); color: var(--fg-dim); }}
  .md a {{ color: var(--link); text-decoration: underline; text-underline-offset: 2px; }}
  .md code {{ font-family: "JetBrains Mono", monospace; font-size: 0.92em; background: var(--surface-hi); padding: 1px 5px; border-radius: 2px; color: var(--accent); }}
  .md pre {{ margin: 0.5em 0; padding: 0; background: transparent; overflow-x: auto; }}
  .md pre code {{ display: block; padding: 10px 12px; background: var(--surface); border: 1px solid var(--border); font-size: 11.5px; line-height: 1.55; color: var(--fg-dim); border-radius: 0; }}
  .md hr {{ border: none; border-top: 1px solid var(--border); margin: 1em 0; }}
  .md-fallback {{ font-family: "JetBrains Mono", monospace; font-size: 12.5px; line-height: 1.65; white-space: pre-wrap; word-break: break-word; color: var(--fg-dim); }}
  .prompt-clickable {{ cursor: pointer; }}
  .prompt-clickable:hover {{ color: var(--accent); }}

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
      <div class="search-col">
        <div class="search-wrap">
          <input id="q" type="text" placeholder="search names, paths, ids, full content…" autofocus>
          <span id="search-spinner" class="search-spinner" aria-hidden="true"></span>
        </div>
        <div class="role-filter" id="role-filter" title="which message types the full-text search looks inside">
          <span class="role-label">search in</span>
          <button class="role-chip" data-role="user" type="button"><span class="check">■</span>you</button>
          <button class="role-chip" data-role="assistant" type="button"><span class="check">■</span>claude</button>
          <button class="role-chip" data-role="tool" type="button"><span class="check">□</span>tools</button>
          <button class="role-chip" data-role="meta" type="button"><span class="check">□</span>meta</button>
        </div>
      </div>
      <button id="regex-mode" class="btn" type="button" title="treat query as regex"><span class="check">□</span>regex</button>
      <button id="hide-desktop" class="btn" type="button" title="hide sessions launched by Claude Desktop (routines)"><span class="check">□</span>hide desktop</button>
      <button id="named-only" class="btn" type="button"><span class="check">□</span>named</button>
      <button id="group-cwd" class="btn" type="button"><span class="check">□</span>by dir</button>
      <button id="group-name" class="btn" type="button"><span class="check">□</span>by name</button>
      <button id="toggle-all" class="btn" style="display:none">collapse all</button>
      <span class="count"><span id="count-shown">0</span><span class="sep">/</span><span class="total" id="count-total">0</span></span>
      <span id="truncated-note" class="truncated-note" style="display:none" title="search stopped early to stay responsive — add another word to reach older sessions">partial · refine</span>
      <button id="zoom-out" class="btn" type="button" title="smaller text">A−</button>
      <button id="zoom-in"  class="btn" type="button" title="larger text">A+</button>
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

<div id="conv-backdrop" class="modal-backdrop" style="display:none">
  <div class="modal" role="dialog" aria-modal="true">
    <div class="modal-head">
      <div class="modal-title" id="conv-title"></div>
      <span id="conv-hits" class="conv-hits" style="display:none" title="next: Enter or → · prev: Shift+Enter or ←">
        <span class="conv-hit-label">use ← / → to step through matches:</span>
        <button id="conv-hit-prev" class="conv-hit-btn" type="button" title="previous match (← or Shift+Enter)">‹</button>
        <span id="conv-hit-pos" class="conv-hit-pos"></span>
        <button id="conv-hit-next" class="conv-hit-btn" type="button" title="next match (→ or Enter)">›</button>
      </span>
      <button class="modal-expand" data-backdrop="conv-backdrop" type="button" aria-label="toggle fullscreen" title="fullscreen (f)">⛶</button>
      <button class="modal-close" id="conv-close" type="button" aria-label="close">×</button>
    </div>
    <div class="modal-meta" id="conv-meta"></div>
    <div class="conv-roles" id="conv-roles" title="show or hide message kinds in this view">
      <span class="role-label">show</span>
      <button class="role-chip" data-vrole="user" type="button"><span class="check">■</span>you</button>
      <button class="role-chip" data-vrole="assistant" type="button"><span class="check">■</span>claude</button>
      <button class="role-chip" data-vrole="tool" type="button"><span class="check">□</span>tools</button>
      <button class="role-chip" data-vrole="meta" type="button"><span class="check">□</span>meta</button>
    </div>
    <div class="modal-body conv-body" id="conv-body"></div>
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
let SEARCH_REGEX_MODE = new URLSearchParams(window.location.search).get("regex") === "1" || localStorage.getItem("sessions-regex") === "1";
const ALL_ROLES = ["user", "assistant", "tool", "meta"];
const DEFAULT_ROLES = "user,assistant";
// Which message kinds the server full-text search looks inside. URL ?roles=
// wins (shareable links), else the saved preference, else the default of just
// the human conversation so git log / bash output never pollutes results.
let ROLE_FILTER = (function () {{
  const fromUrl = new URLSearchParams(window.location.search).get("roles");
  const raw = fromUrl !== null ? fromUrl : (localStorage.getItem("sessions-roles") ?? DEFAULT_ROLES);
  return new Set(raw.split(",").map(s => s.trim()).filter(r => ALL_ROLES.includes(r)));
}})();
function rolesParam() {{ return ALL_ROLES.filter(r => ROLE_FILTER.has(r)).join(","); }}
// Which message kinds the conversation viewer SHOWS — separate from the search
// scope above. you + Claude by default; tools/meta are toggled on demand (tool
// blocks render collapsed, so turning tools on stays low-noise).
let VIEW_ROLES = (function () {{
  const raw = localStorage.getItem("sessions-view-roles") ?? DEFAULT_ROLES;
  return new Set(raw.split(",").map(s => s.trim()).filter(r => ALL_ROLES.includes(r)));
}})();
function applyConvViewRoles() {{
  const body = document.getElementById("conv-body");
  if (body) ALL_ROLES.forEach(r => body.classList.toggle("show-" + r, VIEW_ROLES.has(r)));
}}

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
  // an explicit name wins; otherwise fall back to Claude Code's auto title
  // (dimmer pill, since it is a generated label and not a resume key)
  const namePart = r.custom_title
    ? `<span class="pill" title="${{escapeHtml(r.custom_title)}}">${{escapeHtml(r.custom_title)}}</span>`
    : r.ai_title
    ? `<span class="pill ai" title="auto title\n${{escapeHtml(r.ai_title)}}">${{escapeHtml(r.ai_title)}}</span>`
    : `<span class="dash">—</span>`;
  const name = `<div class="name-stack">${{namePart}}<span class="sid sid-copy" data-sid="${{escapeHtml(r.session_id)}}" title="click to copy full session id\n${{escapeHtml(r.session_id)}}">${{shortId(r.session_id)}}</span></div>`;
  const cwdPretty = r.cwd ? prettyPath(r.cwd) : "";
  const cwdCell = showCwd
    ? `<div class="cwd-path" title="${{escapeHtml(cwdPretty)}}">${{renderCwdPath(r.cwd)}}</div>`
    : `<div class="cwd-empty"></div>`;
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
  const title = r.last_prompt ? "click to view conversation" : "click to view conversation (no prompt preview)";
  return `<div class="prompt prompt-clickable" data-sid="${{escapeHtml(r.session_id)}}" title="${{title}}">${{prompt || "&mdash;"}}</div>`;
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
  const hideDesktop = document.getElementById("hide-desktop").classList.contains("active");
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
    if (hideDesktop && r.entrypoint === "claude-desktop") return false;
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
        ? (r.custom_title || r.ai_title || "(unnamed)")
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
  tbody.querySelectorAll(".sid-copy").forEach(el => {{
    el.addEventListener("click", (e) => {{
      e.stopPropagation();
      const sid = el.dataset.sid;
      if (!sid) return;
      navigator.clipboard.writeText(sid);
      const original = el.textContent;
      el.textContent = "copied";
      el.classList.add("copied");
      setTimeout(() => {{ el.textContent = original; el.classList.remove("copied"); }}, 1200);
    }});
  }});
  tbody.querySelectorAll(".snippets").forEach(cell => {{
    cell.addEventListener("click", (e) => {{
      e.stopPropagation();
      // open the full session viewer (not the 3-snippet preview) and jump to
      // the first hit — the preview snippets are list-only.
      openConvModal(cell.dataset.sid);
    }});
  }});
  tbody.querySelectorAll(".prompt-clickable").forEach(cell => {{
    cell.addEventListener("click", (e) => {{
      e.stopPropagation();
      openConvModal(cell.dataset.sid);
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

// Highlight query hits inside an already-rendered subtree (markdown + code)
// without corrupting tags: walk text nodes only and wrap matches in <mark>.
// Returns the first <mark> created so the caller can scroll it into view.
function highlightHitsIn(root, re) {{
  if (!re) return null;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {{
    acceptNode(n) {{
      if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      if (n.parentNode && n.parentNode.nodeName === "MARK") return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }}
  }});
  const targets = [];
  let n;
  while ((n = walker.nextNode())) {{
    re.lastIndex = 0;
    if (re.test(n.nodeValue)) targets.push(n);
  }}
  let first = null;
  for (const node of targets) {{
    re.lastIndex = 0;
    const text = node.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0, m;
    while ((m = re.exec(text)) !== null) {{
      if (m.index === re.lastIndex) {{ re.lastIndex++; continue; }}
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const mk = document.createElement("mark");
      mk.textContent = m[0];
      frag.appendChild(mk);
      if (!first) first = mk;
      last = m.index + m[0].length;
    }}
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }}
  return first;
}}

// Apply search highlighting to a freshly-rendered chunk of the conversation:
// wrap hits, open any collapsed tool block that contains one. Returns the first
// hit element (or null).
function highlightConvChunk(root, re) {{
  const first = highlightHitsIn(root, re);
  root.querySelectorAll("details.conv-block-tool mark").forEach(mk => {{
    const det = mk.closest("details");
    if (det) det.open = true;
  }});
  return first;
}}

// ───── conversation modal (full session, reverse-infinite scroll) ─────
let CONV_STATE = null;  // {{sid, sliceStart, loading, exhausted}}
const CONV_BATCH = 30;
const CONV_LOAD_NEAR_TOP_PX = 200;

function fmtMsgTs(ts) {{
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toISOString().slice(0, 19).replace("T", " ");
}}

function renderMd(rawText) {{
  // marked + DOMPurify load via CDN; if either is missing (offline), fall
  // back to escaped pre-wrap text so the modal still renders something.
  if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {{
    return `<div class="md-fallback">${{escapeHtml(rawText)}}</div>`;
  }}
  try {{
    const html = marked.parse(rawText, {{ breaks: true, gfm: true }});
    return `<div class="md">${{DOMPurify.sanitize(html)}}</div>`;
  }} catch (e) {{
    return `<div class="md-fallback">${{escapeHtml(rawText)}}</div>`;
  }}
}}

function renderConvBlock(b) {{
  const txt = b.text || "";
  if (b.kind === "tool_use" || b.kind === "tool_result") {{
    const firstLine = txt.split("\\n", 1)[0].slice(0, 100);
    const preview = escapeHtml(firstLine) + (txt.length > firstLine.length ? "…" : "");
    const label = b.kind === "tool_use" ? ("tool · " + escapeHtml(b.name || "")) : "tool · result";
    return `<details class="conv-block conv-block-tool">
      <summary class="conv-block-summary"><span class="conv-block-label">${{label}}</span><span class="conv-tool-size">${{txt.length}} chars</span><span class="conv-tool-preview">${{preview}}</span></summary>
      <pre class="conv-block-raw"><code>${{escapeHtml(txt)}}</code></pre>
    </details>`;
  }}
  // text block → markdown
  return `<div class="conv-block conv-block-text">${{renderMd(txt)}}</div>`;
}}

function highlightCodeIn(root) {{
  if (typeof hljs === "undefined") return;
  root.querySelectorAll("pre code").forEach(el => {{
    try {{ hljs.highlightElement(el); }} catch (e) {{}}
  }});
}}

function renderConvMessages(msgs) {{
  return msgs.map(m => {{
    const roleEsc = escapeHtml(m.role || "?");
    const ts = fmtMsgTs(m.ts);
    const blocks = (m.blocks || []).map(renderConvBlock).join("");
    return `<div class="conv-msg" data-role="${{roleEsc}}" data-idx="${{m.idx}}">
      <div class="conv-msg-meta"><span class="conv-msg-role">${{roleEsc}}</span><span class="conv-msg-ts">${{ts}}</span></div>
      <div class="conv-msg-blocks">${{blocks}}</div>
    </div>`;
  }}).join("");
}}

// Park on the i-th match: highlight it, open its tool block if collapsed,
// center it, and update the counter. Clamps to range.
function gotoHit(i) {{
  if (!CONV_STATE || !CONV_STATE.hits || !CONV_STATE.hits.length) return;
  i = Math.max(0, Math.min(CONV_STATE.hits.length - 1, i));
  CONV_STATE.hits.forEach(h => h.classList.remove("active-hit"));
  const mk = CONV_STATE.hits[i];
  const det = mk.closest("details");
  if (det && !det.open) det.open = true;
  mk.classList.add("active-hit");
  mk.scrollIntoView({{ block: "center", behavior: "instant" }});
  CONV_STATE.hitI = i;
  document.getElementById("conv-hit-pos").textContent = (i + 1) + " / " + CONV_STATE.hits.length;
  document.getElementById("conv-hit-prev").disabled = (i <= 0);
  document.getElementById("conv-hit-next").disabled = (i >= CONV_STATE.hits.length - 1);
}}

// step by ±1, stopping at the ends (no wrap-around)
function nextHit(delta) {{
  if (!CONV_STATE || !CONV_STATE.hits || !CONV_STATE.hits.length) return;
  gotoHit(CONV_STATE.hitI + delta);  // gotoHit clamps to [0, n-1]
}}

async function openConvModal(sid) {{
  const row = ROWS.get(sid);
  const name = row && (row.custom_title || row.ai_title);
  const q = document.getElementById("q").value.trim();
  // title: the session name if any, else the session id; when opened from a
  // search, append the query (highlighted) so it's clear what's marked below.
  let titleHtml = name ? escapeHtml(name)
                       : `<span class="conv-title-id">${{escapeHtml(shortId(sid))}}</span>`;
  if (q) {{
    titleHtml += `<span class="conv-title-sep">·</span>`
      + `<span class="conv-title-q"><span class="conv-title-ico">⌕</span>${{highlightText(q, buildHighlightRegex())}}</span>`;
  }}
  document.getElementById("conv-title").innerHTML = titleHtml;
  const cwd = row && row.cwd ? prettyPath(row.cwd) : "(unknown)";
  const meta = document.getElementById("conv-meta");
  meta.innerHTML = `<span>${{escapeHtml(cwd)}}</span><span class="sep">·</span><span>${{escapeHtml(shortId(sid))}}</span><span class="sep">·</span><span id="conv-pos">loading…</span>`;
  const body = document.getElementById("conv-body");
  body.innerHTML = "";
  const re = buildHighlightRegex();
  CONV_STATE = {{ sid, sliceStart: null, loading: false, exhausted: false, re, hits: [], hitI: -1 }};
  document.getElementById("conv-backdrop").style.display = "flex";
  applyModalFullscreen("conv-backdrop");
  // When searching, load the whole session in one shot so every match is in the
  // DOM and prev/next can reach all of them. Otherwise page in newest-first.
  const d = await fetchConv(sid, null, re ? 1000000 : CONV_BATCH);
  if (!d || CONV_STATE === null || CONV_STATE.sid !== sid) return;
  body.innerHTML = renderConvMessages(d.messages) || `<div class="conv-empty">(no readable messages)</div>`;
  applyConvViewRoles();
  highlightCodeIn(body);
  // Highlight + collect matches only inside the message kinds the search
  // covered (ROLE_FILTER), so the nav count matches the result list. Tool
  // output stays visible, it's just not part of the match walk.
  const roleSel = [...ROLE_FILTER].map(r => ".conv-msg[data-role='" + r + "']").join(",");
  if (re && roleSel) {{
    body.querySelectorAll(roleSel).forEach(sc => highlightConvChunk(sc, re));
  }}
  CONV_STATE.sliceStart = d.slice_start;
  CONV_STATE.exhausted = (d.slice_start === 0);
  document.getElementById("conv-pos").textContent = `${{d.slice_start}}–${{d.slice_end}} of ${{d.total}}`;
  const navEl = document.getElementById("conv-hits");
  CONV_STATE.hits = [...body.querySelectorAll("mark")];
  if (CONV_STATE.hits.length) {{
    // park on the LATEST match (rows + snippets are recency-first, so that's
    // what the user clicked toward); ‹ › walk to older / newer matches.
    navEl.style.display = "";
    requestAnimationFrame(() => gotoHit(CONV_STATE.hits.length - 1));
  }} else {{
    navEl.style.display = "none";
    requestAnimationFrame(() => {{ body.scrollTop = body.scrollHeight; }});
  }}
  // remove any listener left by a previous open (e.g. user clicks a second
  // session without closing the first) so scroll doesn't double-fire.
  body.removeEventListener("scroll", onConvScroll);
  body.addEventListener("scroll", onConvScroll);
}}

async function fetchConv(sid, end, count) {{
  const params = new URLSearchParams({{ sid, count: String(count || CONV_BATCH) }});
  if (end != null) params.set("end", String(end));
  try {{
    const res = await fetch("/messages?" + params.toString());
    if (!res.ok) return null;
    return await res.json();
  }} catch (e) {{
    return null;
  }}
}}

async function onConvScroll(e) {{
  const body = e.target;
  if (!CONV_STATE || CONV_STATE.loading || CONV_STATE.exhausted) return;
  if (body.scrollTop > CONV_LOAD_NEAR_TOP_PX) return;
  CONV_STATE.loading = true;
  const sid = CONV_STATE.sid;
  const end = CONV_STATE.sliceStart;
  const heightBefore = body.scrollHeight;
  const scrollBefore = body.scrollTop;
  const d = await fetchConv(sid, end);
  if (CONV_STATE === null || CONV_STATE.sid !== sid) return;  // modal replaced — drop
  if (!d) {{ CONV_STATE.loading = false; return; }}  // transient error — allow retry on next scroll
  if (!d.messages.length) {{
    CONV_STATE.exhausted = true;
    CONV_STATE.loading = false;
    return;
  }}
  // render + highlight the new chunk in a detached node, then prepend it, so
  // we don't re-walk (and re-wrap) the messages already in the DOM.
  const tmp = document.createElement("div");
  tmp.innerHTML = renderConvMessages(d.messages);
  highlightCodeIn(tmp);
  highlightConvChunk(tmp, CONV_STATE.re);
  const frag = document.createDocumentFragment();
  while (tmp.firstChild) frag.appendChild(tmp.firstChild);
  body.insertBefore(frag, body.firstChild);
  // preserve viewport: adjust scrollTop by added height
  body.scrollTop = scrollBefore + (body.scrollHeight - heightBefore);
  CONV_STATE.sliceStart = d.slice_start;
  CONV_STATE.exhausted = (d.slice_start === 0);
  document.getElementById("conv-pos").textContent = `${{d.slice_start}}–${{d.slice_end}} of ${{d.total}}`;
  if (CONV_STATE.exhausted) {{
    body.insertAdjacentHTML("afterbegin", `<div class="conv-start">(start of session)</div>`);
  }}
  CONV_STATE.loading = false;
}}

function closeConvModal() {{
  document.getElementById("conv-backdrop").style.display = "none";
  const body = document.getElementById("conv-body");
  body.removeEventListener("scroll", onConvScroll);
  body.innerHTML = "";
  CONV_STATE = null;
}}

// fullscreen toggle for both modals (preference persists)
function applyModalFullscreen(backdropId) {{
  const bd = document.getElementById(backdropId);
  const on = localStorage.getItem("sessions-modal-fullscreen") === "1";
  bd.classList.toggle("fullscreen", on);
}}
document.querySelectorAll(".modal-expand").forEach(btn => {{
  btn.addEventListener("click", (e) => {{
    e.stopPropagation();
    const bdId = btn.dataset.backdrop;
    const bd = document.getElementById(bdId);
    const on = !bd.classList.contains("fullscreen");
    localStorage.setItem("sessions-modal-fullscreen", on ? "1" : "0");
    document.querySelectorAll(".modal-backdrop").forEach(x => x.classList.toggle("fullscreen", on));
  }});
}});
// `f` key toggles fullscreen on whichever modal is open
document.addEventListener("keydown", (e) => {{
  if (e.key !== "f" || e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  const open = [...document.querySelectorAll(".modal-backdrop")].find(b => b.style.display !== "none");
  if (!open) return;
  const on = !open.classList.contains("fullscreen");
  localStorage.setItem("sessions-modal-fullscreen", on ? "1" : "0");
  document.querySelectorAll(".modal-backdrop").forEach(x => x.classList.toggle("fullscreen", on));
}});

document.getElementById("conv-close").addEventListener("click", closeConvModal);
document.getElementById("conv-backdrop").addEventListener("click", (e) => {{
  if (e.target.id === "conv-backdrop") closeConvModal();
}});
document.getElementById("conv-hit-prev").addEventListener("click", (e) => {{ e.stopPropagation(); nextHit(-1); }});
document.getElementById("conv-hit-next").addEventListener("click", (e) => {{ e.stopPropagation(); nextHit(1); }});
document.addEventListener("keydown", (e) => {{
  if (document.getElementById("conv-backdrop").style.display === "none") {{
    return;
  }}
  if (e.key === "Escape") {{
    closeConvModal();
    return;
  }}
  // step through matches while the viewer is open:
  //   Enter / →  next      Shift+Enter / ←  prev
  if (!CONV_STATE || !CONV_STATE.hits || !CONV_STATE.hits.length) return;
  if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
  if (e.key === "Enter") {{
    e.preventDefault();
    nextHit(e.shiftKey ? -1 : 1);
  }} else if (e.key === "ArrowRight") {{
    e.preventDefault();
    nextHit(1);
  }} else if (e.key === "ArrowLeft") {{
    e.preventDefault();
    nextHit(-1);
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

(function initHideDesktop() {{
  // Default ON: routine/scheduled sessions launched by Claude Desktop usually
  // aren't what the user is looking for.
  const stored = localStorage.getItem("sessions-hide-desktop");
  const on = stored === null ? true : stored === "1";
  const btn = document.getElementById("hide-desktop");
  const check = btn.querySelector(".check");
  if (on) {{
    btn.classList.add("active");
    if (check) check.textContent = "■";
  }}
  btn.addEventListener("click", () => {{
    const next = !btn.classList.contains("active");
    btn.classList.toggle("active", next);
    if (check) check.textContent = next ? "■" : "□";
    localStorage.setItem("sessions-hide-desktop", next ? "1" : "0");
    render();
  }});
}})();

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
      const hay = [r.custom_title, r.ai_title, r.cwd, r.session_id, r.last_prompt].filter(Boolean).join(" ");
      if (re.test(hay)) out.add(r.session_id);
    }}
    return out;
  }}
  const words = q.toLowerCase().split(/\\s+/).filter(Boolean);
  if (!words.length) return out;
  for (const r of DATA) {{
    const hay = [r.custom_title, r.ai_title, r.cwd, r.session_id, r.last_prompt].filter(Boolean).join(" ").toLowerCase();
    if (words.every(w => hay.includes(w))) out.add(r.session_id);
  }}
  return out;
}}

function syncSearchUrl(q) {{
  const url = new URL(window.location.href);
  if (q) {{
    url.searchParams.set("q", q);
    if (SEARCH_REGEX_MODE) url.searchParams.set("regex", "1");
    else url.searchParams.delete("regex");
    // Only pin roles in the URL when they differ from the default, so ordinary
    // searches keep a clean ?q= link.
    if (rolesParam() !== DEFAULT_ROLES) url.searchParams.set("roles", rolesParam());
    else url.searchParams.delete("roles");
  }} else {{
    url.searchParams.delete("q");
    url.searchParams.delete("regex");
    url.searchParams.delete("roles");
  }}
  // replaceState so typing doesn't bloat browser history; the URL still
  // becomes shareable / bookmarkable and survives reload.
  history.replaceState(null, "", url.pathname + (url.search ? url.search : ""));
}}

async function updateSearch() {{
  const q = document.getElementById("q").value.trim();
  syncSearchUrl(q);
  if (!q) {{
    SEARCH_RESULT = null;
    SEARCH_SNIPPETS = {{}};
    SEARCH_SCORES = {{}};
    document.body.classList.remove("searching");
    document.getElementById("truncated-note").style.display = "none";
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
    const url = "/search?q=" + encodeURIComponent(q) + (SEARCH_REGEX_MODE ? "&regex=1" : "") + "&roles=" + encodeURIComponent(rolesParam());
    const res = await fetch(url);
    if (reqId !== SEARCH_REQ_ID) return;
    const d = await res.json();
    const merged = new Set(SEARCH_RESULT);
    for (const sid of (d.session_ids || [])) merged.add(sid);
    SEARCH_RESULT = merged;
    SEARCH_SNIPPETS = d.snippets || {{}};
    SEARCH_SCORES = d.scores || {{}};
    document.getElementById("truncated-note").style.display = d.truncated ? "inline" : "none";
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

(function initRoleFilter() {{
  const wrap = document.getElementById("role-filter");
  if (!wrap) return;
  ALL_ROLES.forEach(role => {{
    const btn = wrap.querySelector('[data-role="' + role + '"]');
    if (!btn) return;
    const check = btn.querySelector(".check");
    function sync() {{
      const on = ROLE_FILTER.has(role);
      btn.classList.toggle("active", on);
      if (check) check.textContent = on ? "■" : "□";
    }}
    sync();
    btn.addEventListener("click", () => {{
      if (ROLE_FILTER.has(role)) ROLE_FILTER.delete(role); else ROLE_FILTER.add(role);
      localStorage.setItem("sessions-roles", rolesParam());
      sync();
      if (document.getElementById("q").value.trim()) updateSearch();
    }});
  }});
}})();

// viewer's `show` chips — toggle which message kinds are visible in the modal
(function initConvRoles() {{
  const wrap = document.getElementById("conv-roles");
  if (!wrap) return;
  ALL_ROLES.forEach(role => {{
    const btn = wrap.querySelector('[data-vrole="' + role + '"]');
    if (!btn) return;
    const check = btn.querySelector(".check");
    function sync() {{
      const on = VIEW_ROLES.has(role);
      btn.classList.toggle("active", on);
      if (check) check.textContent = on ? "■" : "□";
    }}
    sync();
    btn.addEventListener("click", () => {{
      if (VIEW_ROLES.has(role)) VIEW_ROLES.delete(role); else VIEW_ROLES.add(role);
      localStorage.setItem("sessions-view-roles", ALL_ROLES.filter(r => VIEW_ROLES.has(r)).join(","));
      sync();
      applyConvViewRoles();
    }});
  }});
}})();

// The role-filter row makes the toolbar taller, so pin the sticky table header
// just below it (and keep it correct across zoom / resize / wrapping).
(function syncTheadTop() {{
  const tb = document.querySelector(".toolbar");
  if (!tb) return;
  const apply = () => document.documentElement.style.setProperty("--thead-top", tb.offsetHeight + "px");
  apply();
  if (window.ResizeObserver) new ResizeObserver(apply).observe(tb);
  window.addEventListener("resize", apply);
}})();

document.getElementById("all-btn").addEventListener("click", () => {{
  SELECTED_PATH = "";
  localStorage.setItem("sessions-selected-path", "");
  renderTree();
  render();
}});

(function initZoom() {{
  const STEPS = [0.85, 1.0, 1.15, 1.3, 1.5, 1.75];
  let idx = STEPS.indexOf(parseFloat(localStorage.getItem("sessions-zoom") || "1"));
  if (idx < 0) idx = 1;
  function apply() {{
    document.documentElement.style.setProperty("--zoom", STEPS[idx]);
    localStorage.setItem("sessions-zoom", String(STEPS[idx]));
  }}
  apply();
  document.getElementById("zoom-in").addEventListener("click", () => {{
    if (idx < STEPS.length - 1) {{ idx++; apply(); }}
  }});
  document.getElementById("zoom-out").addEventListener("click", () => {{
    if (idx > 0) {{ idx--; apply(); }}
  }});
}})();

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

// Idle detection — the server only runs background indexing when no tab is
// visible+focused. We POST on every transition (visibilitychange / focus /
// blur), plus once on load to set the initial state.
function reportVisibility() {{
  const active = (document.visibilityState === "visible") && document.hasFocus();
  try {{
    fetch("/visibility?state=" + (active ? "active" : "inactive"),
          {{ method: "POST", keepalive: true }}).catch(() => {{}});
  }} catch (_) {{}}
}}
document.addEventListener("visibilitychange", reportVisibility);
window.addEventListener("focus", reportVisibility);
window.addEventListener("blur", reportVisibility);
reportVisibility();

(function initFromUrl() {{
  function applyParams() {{
    const params = new URLSearchParams(window.location.search);
    const q = params.get("q") || "";
    const regex = params.get("regex") === "1";
    if (regex !== SEARCH_REGEX_MODE) {{
      SEARCH_REGEX_MODE = regex;
      localStorage.setItem("sessions-regex", regex ? "1" : "0");
      const btn = document.getElementById("regex-mode");
      const check = btn.querySelector(".check");
      btn.classList.toggle("active", regex);
      if (check) check.textContent = regex ? "■" : "□";
    }}
    const input = document.getElementById("q");
    if (input.value !== q) input.value = q;
    updateSearch();
  }}
  applyParams();
  window.addEventListener("popstate", applyParams);
}})();

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

# Idle detection. The frontend POSTs /visibility on visibilitychange / focus
# / blur with state=active|inactive (active iff visible AND focused). The
# watcher only runs the index builder while no tab is active, so background
# indexing never competes with foreground browsing.
#
# Last-write-wins on a single bool: with two reclaude tabs in different
# windows, the most recently-fired event determines the state, which can
# briefly be wrong (active tab still up but inactive tab's blur arrived
# last). We accept this — the next visibilitychange/focus/blur on either
# tab self-corrects, and the worst-case outcome is one extra paused or
# resumed indexing tick. Single-tab use (the common case) is exact.
_user_active = False
_visibility_lock = threading.Lock()


def _is_user_active() -> bool:
    """True iff some browser tab is currently visible+focused on the page.

    Combined with subscriber count so a hard tab close (which we detect via
    SSE disconnect, not via a /visibility report) immediately flips us back
    to inactive even if the last reported state was active.
    """
    with _state_lock:
        if not _subscribers:
            return False
    with _visibility_lock:
        return _user_active


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
        # Idle indexing: skip while a browser tab is visible+focused on the
        # page. mtime delta detection above still runs every tick so live
        # updates never lag.
        if _is_user_active():
            continue
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
            if urlparse(self.path).path in ("/", "/index.html"):
                self._send_bytes(template_bytes, "text/html; charset=utf-8")
            elif self.path == "/events":
                self._stream_events()
            elif self.path.startswith("/search"):
                self._handle_search()
            elif self.path.startswith("/messages"):
                self._handle_messages()
            elif self.path.startswith("/visibility"):
                self._handle_visibility()
            else:
                self.send_error(404)

        def do_POST(self):
            if self.path.startswith("/visibility"):
                self._handle_visibility()
            else:
                self.send_error(404)

        def _handle_visibility(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            state = (qs.get("state") or [""])[0]
            global _user_active
            with _visibility_lock:
                _user_active = (state == "active")
            self.send_response(204)
            self.end_headers()

        def _handle_messages(self) -> None:
            qs = parse_qs(urlparse(self.path).query)
            sid = (qs.get("sid") or [""])[0]
            try:
                count = int((qs.get("count") or ["30"])[0])
            except ValueError:
                count = 30
            end_str = (qs.get("end") or [""])[0]
            end: int | None
            try:
                end = int(end_str) if end_str else None
            except ValueError:
                end = None
            # Cap as a memory guard. The viewer pages in 30 at a time, but a
            # search loads the whole session at once (so match nav can reach
            # every hit), so the ceiling has to cover a full conversation.
            count = max(1, min(count, 2000))
            result = _session_messages(sid, end, count)
            if result is None:
                self.send_error(404)
                return
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8")

        def _handle_search(self) -> None:
            # keep_blank_values so roles= (all chips off) is seen as "match
            # nothing" rather than collapsing to "param absent" -> default.
            qs = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            q = (qs.get("q") or [""])[0]
            use_regex = (qs.get("regex") or ["0"])[0] in ("1", "true", "yes")
            # roles absent (old client / deep link) -> default to the conversation
            # itself (user+assistant). Present but empty -> match nothing.
            if "roles" in qs:
                roles = {r for r in qs["roles"][0].split(",") if r}
            else:
                roles = {"user", "assistant"}
            sids, snippets, scores, truncated = _search_sessions(q, use_regex=use_regex, roles=roles)
            body = json.dumps(
                {
                    "q": q,
                    "regex": use_regex,
                    "roles": sorted(roles),
                    "session_ids": sids,
                    "snippets": snippets,
                    "scores": scores,
                    "truncated": truncated,
                },
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
    ap.add_argument("--version", action="version", version=f"reclaude {__version__}")
    args = ap.parse_args()
    serve(args.port)
