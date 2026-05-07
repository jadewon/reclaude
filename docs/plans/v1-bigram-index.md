# v1: Idle background indexing (bigram inverted index)

Status: draft, reviewed against SQLite 3.53.x (2026-05) capabilities.

## Goal

Speed up `_search_sessions` (reclaude.py:223) which currently re-scans every
`~/.claude/projects/*/*.jsonl` file from disk on every query. Use the always-on
local server's idle time to maintain a persistent search index.

## Constraints

- Single-file project, Python 3.11+, **no third-party packages** (Python stdlib
  only — `sqlite3` is allowed).
- Local single-user, no concurrent access.
- Memory cost must stay small (rules out a full in-memory inverted index).
- Must support **Korean and English** uniformly. Korean has no whitespace word
  boundaries → word-token indexes don't work. Character n-grams (bigrams) do.

## Decision

Disk-based **character bigram inverted index** in SQLite (Python stdlib
`sqlite3`).

### Why disk over in-memory

- SQLite default page cache ~2 MB + small Python overhead. Orders of magnitude
  less RAM than an in-memory `dict[gram, set[sid]]`.
- During incremental indexing, peak memory is one file's bigram set.
- **Index persists across restarts** — for an always-on local server, idle
  indexing accumulates value over time. In-memory would re-index on every boot.

### Why manual bigram table over FTS5

Considered and rejected:

- **FTS5 `trigram` tokenizer** (SQLite 3.34+, requires `SQLITE_ENABLE_FTS5`
  build flag): treats CJK characters the same as Latin, sliding a 3-codepoint
  window. In Korean, 1–2 syllables typically constitute a semantic unit, so
  3-syllable windows produce poor recall on short queries. The community
  `better-trigram` extension fixes this by tokenizing each CJK char
  individually, but it is a third-party loadable extension — out of bounds
  given the no-third-party rule.
- **FTS5 `unicode61` tokenizer**: groups consecutive letter codepoints into a
  single token, so a Korean run like `검색기능` becomes one token rather than
  four syllables. Phrase queries can recover some of this but the ergonomics
  and recall are worse than a direct bigram index.
- **SQLite 3.46+ `contentless_unindexed=1` / `contentless_delete=1` / `fts5_tokenizer_v2` (locale-aware)**:
  useful FTS5 quality-of-life features, but none of them solve the underlying
  CJK tokenization mismatch above.

Manual bigram in regular SQLite tables sidesteps all of the above:
- Works on any SQLite build (no FTS5 build-flag dependency).
- Matches Korean naturally: a 2-syllable bigram is the right unit.
- Works for English by intersecting many bigrams (more selective).
- Code is not significantly longer than the FTS5 setup would have been.

If we ever need BM25 ranking or richer query syntax, a follow-up can layer
FTS5 (with `unicode61` + phrase queries, or runtime-detected trigram for
non-CJK) on top — but for v1 the manual path is strictly better for our
Korean+English mix.

## Schema

Stored at `~/.cache/reclaude/index.db` (respect `XDG_CACHE_HOME`).

```sql
CREATE TABLE files (
  path  TEXT PRIMARY KEY,
  mtime REAL NOT NULL,
  size  INTEGER NOT NULL
);

CREATE TABLE postings (
  gram TEXT NOT NULL,
  path TEXT NOT NULL,
  PRIMARY KEY (gram, path)
) WITHOUT ROWID;

CREATE INDEX idx_postings_path ON postings(path);  -- per-file delete
```

`PRIMARY KEY (gram, path)` clusters lookups by gram. The path index supports
the per-file invalidation path (`DELETE FROM postings WHERE path = ?`).

## Bigram extraction

- Read the JSONL file as UTF-8 text (or decode line-by-line to tolerate bad
  bytes), `casefold()` (better than `lower()` for Unicode case mapping where
  it matters; ASCII behaves the same).
- Walk maximal **non-whitespace runs**. For each run of length ≥ 2, emit every
  consecutive 2-codepoint slice. Single-codepoint runs contribute nothing.
- Deduplicate per file (set), then bulk insert.

Examples:
- `검색기능` → `검색`, `색기`, `기능`
- `search` → `se`, `ea`, `ar`, `rc`, `ch`
- `검색 function` → `검색` + `fu`, `un`, `nc`, `ct`, `ti`, `io`, `on`

## Search path

1. Lowercase/casefold the query, split on whitespace.
2. For each non-whitespace word of length ≥ 2, extract its bigrams.
3. For each bigram, `SELECT path FROM postings WHERE gram = ?`. Intersect the
   resulting path sets (Python sets, or SQL `INTERSECT`). Single-bigram queries
   skip intersection.
4. **Verify** each candidate file by running the existing line-scan logic from
   `_search_sessions` — this filters bigram false positives ("검색" bigrams
   could come from "색검색" too) and produces the snippet.
5. Edge cases that bypass the index:
   - Single-codepoint query → no bigrams → fall back to candidate scan over
     all files (or short-circuit with a `LIKE '%c%'` style check).
   - Regex mode → fall back to current full scan. Regex acceleration is
     generally hard and out of scope for v1.

## Idle build / invalidation

Hook into `_watcher_loop` (reclaude.py:1726). After processing mtime-driven
deltas each cycle, do **at most one indexing unit of work**:

1. Find a path that is missing from `files`, or whose `(mtime, size)` differs
   from disk.
2. Open it, extract unique bigrams.
3. In one transaction:
   - `DELETE FROM postings WHERE path = ?`
   - `INSERT OR IGNORE INTO postings(gram, path) VALUES (?, ?)` (executemany)
   - `INSERT OR REPLACE INTO files(path, mtime, size) VALUES (?, ?, ?)`
4. Also: detect deleted files (`files` row has no on-disk counterpart) and
   purge their postings.

One file per cycle keeps CPU/IO bounded. The 2 s watcher tick is plenty for
catch-up after a fresh start; a cold start indexing 10k files takes ~5 hours
worst case, but is invisible to the user.

## Connection / threading

`sqlite3` connections are not thread-safe by default. Options:

- One connection in the watcher thread for writes.
- A separate per-request connection in the HTTP handler for reads. SQLite
  supports concurrent readers + 1 writer with WAL mode.
- Set `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL` (this is local,
  recoverability of the index is not critical — we can rebuild from JSONL).

## Open questions to resolve before coding

- Should we also index session metadata (cwd, name, last prompt) in the same
  DB, or keep search separate? Current scan-time matching for metadata is
  cheap because that data already lives in memory.
- Should the verification pass parallelize across candidate files? Probably
  not for v1 — most queries should narrow to a small candidate set.

## References

- SQLite 3.53.0 release log (2026-04-09): https://sqlite.org/releaselog/3_53_0.html
- SQLite FTS5 docs: https://www.sqlite.org/fts5.html
- "Building a Search System with SQLite FTS5 and CJK Support" (2025) — confirms
  built-in trigram's CJK limitation.
- streetwriters/sqlite-better-trigram — third-party extension that solves CJK
  by per-char tokenization (rejected: external dep).

## Out of scope for v1

- BM25 ranking / relevance scoring (current code uses phrase-vs-word boost).
- Cross-file phrase search (current `_search_sessions` is line-scoped anyway).
- Live SSE push of indexing progress.
