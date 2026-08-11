# reclaude

Live browser for your Claude Code sessions.

After a terminal crash, a laptop reboot, or just losing track of where you were,
`reclaude` shows every past session at a glance — custom names (`/rename`),
working directories, last prompt, last activity — so you know exactly where to
`claude --resume`.

![reclaude screenshot](docs/screenshot.png)

## Why

`~/.claude/projects/*/*.jsonl` holds every session you've ever run. It's
durable and complete, but not browsable. `reclaude` reads it live and
surfaces the signal: what did I name this session, which directory was it in,
what was I doing last?

## Keep the history

Claude Code deletes session files older than `cleanupPeriodDays` at startup —
**30 days by default**, which quietly caps how far back `reclaude` can look.
Raise it in `~/.claude/settings.json` before the sessions you'd want are gone:

```json
{ "cleanupPeriodDays": 3650 }
```

## Install

### Homebrew (macOS, recommended)

```sh
brew tap jadewon/tap
brew install reclaude
```

### From source

Requires Python 3.11+. No third-party packages.

```sh
git clone git@github.com:jadewon/reclaude.git ~/.reclaude
```

## Run

Homebrew install:

```sh
reclaude --port 9999
# or run as a launchd-managed background service:
brew services start reclaude
```

From source:

```sh
python3 ~/.reclaude/reclaude.py
```

Then open http://127.0.0.1:9999/.

Options:

| flag | default | note |
|---|---|---|
| `--port` | `9999` | HTTP port |

## How it works

- **Server** scans `~/.claude/projects/*/*.jsonl` at startup and keeps an
  in-memory map keyed by session ID.
- **Watcher thread** polls file mtimes every 2 s. On change, rescans only the
  affected file(s).
- **Transport** is [Server-Sent Events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events).
  - `event: snapshot` is pushed once on connect (full state).
  - `event: delta` is pushed on change, carrying only `{upsert, remove}` by
    session ID — no full payload re-sends.
- **Client** keeps a `Map` keyed by session ID, applies deltas, re-renders.
- **Search index** is a SQLite-backed character-bigram inverted index at
  `~/.cache/reclaude/index.db`. The watcher builds it lazily — one file per
  tick, only while no browser tab is visible+focused (Page Visibility API
  + window focus). The index handles Korean and English uniformly; results
  stay correct during the initial build because un-indexed and stale files
  fall through to the line-scan verification.

The browser POSTs `/visibility` on visibility/focus changes. Other than that
there are no REST endpoints, no polling from the browser. One long-lived SSE
stream per tab.

## UI

- Left sidebar: directory tree (filters to that subtree on click)
- Toolbar: full-text search (`/`) across session bodies with word-AND fuzzy + `regex` toggle; wrap a run in double quotes (`"exact wording"`) to require it verbatim inside one message, and mix it with bare words to AND the two; a `search in` row scopes which message kinds are matched (`you` / `claude` / `tools` / `meta`, defaulting to your + Claude's messages so tool and git output don't drown the results). Plus a `named` filter and `by dir` / `by name` grouping.
- Match rows preview the surrounding context with the hit highlighted. Click one to open the full session viewer: it loads the whole conversation, jumps to the latest match (the latest *phrase* match when the query is quoted), and highlights every match. Message kinds the search covered are shown even if the viewer's `show` chips had them off, so a hit in tool output isn't hidden. Step through them with the `‹ ›` buttons, or **`Enter` / `→`** (next) and **`Shift+Enter` / `←`** (previous). **`f`** toggles fullscreen, **`Esc`** closes.
- Dark + light themes (auto by OS, manual toggle persists)
- Each row: `copy` button writes `cd "<cwd>" && claude --resume <session-id>`
  to clipboard (always the id — `--resume` resumes by id, and a name is only a
  search term for its interactive picker)

Preferences (`search in` roles, `named-only`, `group-mode`, `theme`, tree
expansion, selected directory) persist in `localStorage`. A search is also
shareable: `?q=` (and `?regex=1` / `?roles=`) in the URL reruns it on load.

## Run as a background daemon (macOS)

Copy the example plist, edit the path to match your clone, load it:

```sh
cp examples/com.github.jadewon.reclaude.plist ~/Library/LaunchAgents/
# edit paths inside the plist if your clone isn't at ~/.reclaude

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.github.jadewon.reclaude.plist
```

Restart after editing the code:

```sh
launchctl kickstart -k gui/$(id -u)/com.github.jadewon.reclaude
```

Stop:

```sh
launchctl bootout gui/$(id -u)/com.github.jadewon.reclaude
```

Logs: `/tmp/reclaude.log`, `/tmp/reclaude.err`.

## Data

Read-only. `reclaude` never writes to `~/.claude/`. It only scans for:

- `type: "user"` messages → last prompt
- `type: "custom-title"` / `type: "agent-name"` → session name
- `type: "ai-title"` → auto-generated title, shown when there is no session name
- file mtime → activity time
- `cwd` field → working directory
- `~/.claude/sessions/*.json` → active PID detection (green dot)

## License

MIT. See [LICENSE](LICENSE).
