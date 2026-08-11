#!/usr/bin/env python3
"""Build docs/screenshot.png from synthetic rows. NEVER use real session data —
this repo is public.

  curl -s http://127.0.0.1:9999/ > /tmp/sample-for-screenshot.html
  python3 tools/make-demo-screenshot.py
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless \
    --disable-gpu --hide-scrollbars --force-device-scale-factor=2 \
    --window-size=1600,810 --screenshot=docs/screenshot.png \
    file:///tmp/sample-for-screenshot-demo.html

The page's own script opens an EventSource on a relative /events, which fails
under file://; we then call applySnapshot() ourselves with the demo rows.
"""
import hashlib
import json
import pathlib
import re
import time

SRC = pathlib.Path("/tmp/sample-for-screenshot.html")
DST = pathlib.Path("/tmp/sample-for-screenshot-demo.html")
FAKE_HOME = "/Users/dev"


def sid(seed: str) -> str:
    h = hashlib.sha256(seed.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# (name, ai_title, dir, prompt, minutes-ago, active)
DEMO = [
    ("auth-refactor", None, "Code/acme/auth-service", "extract the token rotation logic into its own module", 2, True),
    ("mobile-onboarding", None, "Code/acme/mobile-app", "why is the FCM token not being registered on cold start?", 6, True),
    ("billing-v2", None, "Code/acme/billing", "draft the migration plan from stripe v3 to v4", 14, True),
    ("ingest-backpressure", None, "Code/acme/analytics-ingest", "the consumer lag is growing again, pull last 24h metrics", 22, False),
    ("docs-refresh", None, "Code/acme/docs-site", "add a guide for the new /sessions endpoint", 35, False),
    (None, "dead-letter queue design", "Code/acme/analytics-ingest", "add a dead-letter queue for malformed events", 41, False),
    ("eval-harness", None, "Code/acme/ml-eval", "run the new golden set against the v3 model", 58, False),
    (None, "tokenizer profiling", "Code/acme/ml-eval", "the eval timings look off, profile the tokenizer", 63, False),
    (None, None, "Code/acme/auth-service", "write unit tests for the new refresh path", 71, False),
    ("pgvector-poc", None, "poc-pgvector", "spike: can we use pgvector for the embedding store?", 78, False),
    ("admin-permissions", None, "Code/acme/web-admin", "the permission matrix UI crashes when a role has no seats", 96, False),
    (None, "flaky checkout test triage", "Code/acme/billing", "checkout_spec fails once every ~20 CI runs, find the race", 112, False),
    ("sdk-release", None, "Code/acme/sdk-js", "cut the 2.4 release notes from the merged PRs", 130, False),
    (None, None, "Code/acme/search", "why does the synonym filter drop single-character tokens?", 147, False),
    ("infra-costs", None, "Code/acme/infra", "break down last month's spend by service and flag outliers", 165, False),
    (None, "index rebuild runbook", "Code/acme/search", "write the runbook for a zero-downtime index rebuild", 183, False),
    ("payments-report", None, "Code/acme/payments-report", "reconcile the refunds table against the ledger export", 201, False),
    (None, None, "Code/acme/mobile-app", "the deep link opens the wrong tab on android 15", 224, False),
    ("scheduler-retry", None, "Code/acme/scheduler", "jobs retry forever when the worker dies mid-lease", 246, False),
    (None, "webhook signature check", "Code/acme/billing", "verify the stripe webhook signature before parsing", 268, False),
    ("docs-search", None, "Code/acme/docs-site", "the site search returns nothing for hyphenated terms", 295, False),
    (None, None, "Code/acme/web-admin", "add an empty state to the audit log table", 318, False),
    ("ml-eval-cache", None, "Code/acme/ml-eval", "cache embeddings between eval runs, keyed by model id", 340, False),
    (None, "grafana alert cleanup", "Code/acme/infra", "half the alerts are noise, prune the ones nobody acts on", 372, False),
    ("sdk-types", None, "Code/acme/sdk-js", "the generated types lose optionality on nested unions", 401, False),
    (None, None, "personal/notes", "summarise what I changed across acme this week", 438, False),
    ("search-relevance", None, "Code/acme/search", "tune BM25 so exact title matches outrank body matches", 470, False),
    (None, "auth latency spike", "Code/acme/auth-service", "p99 on /token doubled after the pool change", 505, False),
    ("analytics-schema", None, "Code/acme/analytics-ingest", "the event schema needs a version field before we fan out", 540, False),
    (None, None, "scratch", "quick script to diff two json exports field by field", 585, False),
]

now = time.time()
rows = []
for i, (name, ai_title, d, prompt, mins, active) in enumerate(DEMO):
    cwd = f"/tmp/{d}" if d in ("poc-pgvector", "scratch") else f"{FAKE_HOME}/{d}"
    rows.append({
        "session_id": sid(f"demo-{i}"),
        "cwd": cwd,
        "custom_title": name,
        "ai_title": ai_title,
        "last_prompt": prompt,
        "entrypoint": "cli",
        "mtime": now - mins * 60,
        "size": 40_000 + i * 3_100,
        "lines": 120 + i * 17,
        "active": active,
    })

generated = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(now))
html = SRC.read_text()
# The server bakes the real $HOME into the page; swap it so no real path can
# reach the committed screenshot.
html, n = re.subn(r'const HOME = "[^"]*";', f'const HOME = "{FAKE_HOME}";', html, count=1)
assert n == 1, "HOME constant not found — did build_html() change?"
# Expand the directory tree so the screenshot shows it doing its job; the page
# reads this key on load, so it has to be set before the module script runs.
# Tree paths are the PRETTY segments the sidebar renders ("~/Code/acme"), not
# the real cwd — see buildTree()/prettySegs().
tree_open = json.dumps(["~", "~/Code", "~/Code/acme"])
preload = (
    "<script>\n"
    f"localStorage.setItem('sessions-tree-expanded', {json.dumps(tree_open)});\n"
    "</script>\n"
)
assert "<script>" in html
html = html.replace("<script>", preload + "<script>", 1)
inject = (
    "<script>\n"
    "// screenshot fixture: synthetic rows, no real session data\n"
    f"applySnapshot({json.dumps(rows, ensure_ascii=False)}, {json.dumps(generated)});\n"
    "</script>\n"
)
assert "</body>" in html
html = html.replace("</body>", inject + "</body>")
DST.write_text(html)
print(f"wrote {DST} ({len(html)} bytes, {len(rows)} rows)")
