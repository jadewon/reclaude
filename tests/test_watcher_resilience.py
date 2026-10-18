"""The watcher thread must survive a failing tick.

A project dir removed between the glob and the open, or any other exception
inside a tick, used to end `_watcher_loop` for good: live rows and the search
index then froze until the daemon restarted.
"""
import json
import pathlib
import queue
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import reclaude

INTERVAL = 0.05
TIMEOUT = 5.0


def make_jsonl(path: pathlib.Path, texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for t in texts:
            f.write(json.dumps({"type": "user", "message": {"content": t}}) + "\n")


def wait_for(cond, what: str, watcher: threading.Thread) -> None:
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if cond():
            return
        assert watcher.is_alive(), f"watcher thread died while waiting for: {what}"
        time.sleep(INTERVAL)
    raise AssertionError(f"timed out waiting for: {what}")


def upserted_ids(events: "queue.Queue[dict]") -> set[str]:
    ids: set[str] = set()
    while True:
        try:
            ev = events.get_nowait()
        except queue.Empty:
            return ids
        ids.update(r["session_id"] for r in ev.get("upsert", []))


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="reclaude-test-"))
    orig_scan_path = reclaude._scan_path
    orig_compute_diff = reclaude._compute_diff
    orig_pick_pending = reclaude._index_pick_pending
    events: "queue.Queue[dict]" = queue.Queue()
    try:
        projects = tmp / "projects"
        projects.mkdir()
        reclaude.PROJECTS = projects
        reclaude.SESSIONS_DIR = tmp / "sessions"  # absent: no active sessions
        reclaude._index_db_path = lambda: tmp / "cache" / "index.db"
        reclaude._index_local = type(reclaude._index_local)()

        # A scan of a file that vanished must report "gone", not raise.
        assert reclaude._scan_file(projects / "nope" / "missing.jsonl") is None
        print("  ok: _scan_file on a missing path returns None")

        # A project dir whose listing fails is skipped; its siblings still list.
        # Path.glob is patched because newer Pythons swallow a vanished dir
        # inside glob, so a real rmtree would not exercise the guard.
        keep = projects / "proj-keep" / "sess-keep.jsonl"
        make_jsonl(keep, ["synthetic keep"])
        make_jsonl(projects / "proj-broken" / "sess-broken.jsonl", ["synthetic broken"])
        orig_glob = pathlib.Path.glob

        def glob_failing_on_broken(self, pattern, *args, **kwargs):
            if self.name == "proj-broken":
                raise FileNotFoundError(str(self))
            return orig_glob(self, pattern, *args, **kwargs)

        pathlib.Path.glob = glob_failing_on_broken
        try:
            listed = reclaude._session_files()
        finally:
            pathlib.Path.glob = orig_glob
        assert listed == [keep], f"expected only the healthy dir's file, got {listed}"
        shutil.rmtree(keep.parent)
        shutil.rmtree(projects / "proj-broken")
        print("  ok: _session_files skips a project dir whose listing fails")

        # A subscriber makes _is_user_active() False (no focused tab), so the
        # idle-indexing step runs too.
        with reclaude._state_lock:
            reclaude._subscribers.append(events)
        watcher = threading.Thread(target=reclaude._watcher_loop, args=(INTERVAL,), daemon=True)

        # Case 1: the project dir is removed after the watcher listed and
        # stat-ed its file but before the file is opened.
        gone = projects / "proj-gone" / "sess-gone.jsonl"
        make_jsonl(gone, ["synthetic gone"])
        removed = threading.Event()

        def scan_path_then_vanish(path, active):
            if path == gone and not removed.is_set():
                shutil.rmtree(gone.parent)
                removed.set()
            return orig_scan_path(path, active)

        reclaude._scan_path = scan_path_then_vanish
        watcher.start()
        wait_for(removed.is_set, "the vanishing file to be scanned", watcher)
        time.sleep(INTERVAL * 6)  # let a few more ticks run
        assert watcher.is_alive(), "watcher died when a project dir vanished before open"
        ok1 = projects / "proj-ok" / "sess-ok1.jsonl"
        make_jsonl(ok1, ["synthetic first survivor"])
        wait_for(lambda: "sess-ok1" in upserted_ids(events), "sess-ok1 upsert after the vanish", watcher)
        print("  ok: project dir vanished before open; watcher alive, later file published")

        # Case 2: an arbitrary exception in the diff step of one tick.
        calls = {"n": 0}

        def flaky_compute_diff():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("synthetic diff failure")
            return orig_compute_diff()

        reclaude._compute_diff = flaky_compute_diff
        wait_for(lambda: calls["n"] >= 2, "a tick after the failing one", watcher)
        assert watcher.is_alive(), "watcher died on an exception in _compute_diff"
        ok2 = projects / "proj-ok" / "sess-ok2.jsonl"
        make_jsonl(ok2, ["synthetic second survivor"])
        wait_for(lambda: "sess-ok2" in upserted_ids(events), "sess-ok2 upsert after a diff failure", watcher)
        print("  ok: _compute_diff raised once; watcher alive, later file published")

        # Case 3: an arbitrary exception in the indexing step of one tick.
        picks = {"n": 0}

        def flaky_pick_pending():
            picks["n"] += 1
            if picks["n"] == 1:
                raise RuntimeError("synthetic index failure")
            return orig_pick_pending()

        reclaude._index_pick_pending = flaky_pick_pending
        wait_for(lambda: picks["n"] >= 2, "an indexing tick after the failing one", watcher)
        assert watcher.is_alive(), "watcher died on an exception in _index_pick_pending"
        ok3 = projects / "proj-ok" / "sess-ok3.jsonl"
        make_jsonl(ok3, ["synthetic third survivor"])

        def indexed() -> bool:
            row = reclaude._index_conn().execute(
                "SELECT 1 FROM files WHERE path = ?", (str(ok3),)
            ).fetchone()
            return row is not None

        # _index_conn() is thread-local: this queries from the main thread
        # through its own connection to the same temp db file.
        wait_for(indexed, "sess-ok3 to be indexed after an indexing failure", watcher)
        print("  ok: _index_pick_pending raised once; watcher alive, later file indexed")

        print("ALL OK")
    finally:
        reclaude._scan_path = orig_scan_path
        reclaude._compute_diff = orig_compute_diff
        reclaude._index_pick_pending = orig_pick_pending
        with reclaude._state_lock:
            if events in reclaude._subscribers:
                reclaude._subscribers.remove(events)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
