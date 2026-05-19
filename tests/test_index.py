"""End-to-end test for the bigram index + search integration."""
import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import reclaude


def make_jsonl(path: pathlib.Path, texts: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for t in texts:
            f.write(json.dumps({"type": "user", "message": {"content": t}}, ensure_ascii=False) + "\n")


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="reclaude-test-"))
    try:
        projects = tmp / "projects"
        cache = tmp / "cache"
        # Patch module state to point at the temp dirs.
        reclaude.PROJECTS = projects
        reclaude._index_db_path = lambda: cache / "reclaude" / "index.db"
        # Reset thread-local connection so the patched path takes effect.
        reclaude._index_local = type(reclaude._index_local)()

        a = projects / "proj-a" / "session-aaa.jsonl"
        b = projects / "proj-b" / "session-bbb.jsonl"
        c = projects / "proj-c" / "session-ccc.jsonl"
        make_jsonl(a, ["검색기능 추가", "한국어로 작성된 메모"])
        make_jsonl(b, ["search function added", "english content here"])
        make_jsonl(c, ["mixed 검색 with english function", "another line"])

        # Index everything via the same idle-pick mechanism.
        for _ in range(10):
            picked = reclaude._index_pick_pending()
            if picked is None:
                break
            p, mt, sz = picked
            ok = reclaude._index_file(p, mt, sz)
            assert ok, f"index_file failed for {p}"
        else:
            raise AssertionError("indexing did not converge")

        def assert_match(query: str, expected_paths: set[pathlib.Path], use_regex: bool = False) -> None:
            sids, snippets, scores, _truncated = reclaude._search_sessions(query, use_regex=use_regex)
            got_sids = set(sids)
            expected_sids = {p.stem for p in expected_paths}
            assert got_sids == expected_sids, (
                f"query={query!r}: got {got_sids}, expected {expected_sids}"
            )
            print(f"  ok: {query!r} -> {sorted(got_sids)}")

        # Korean-only query: should only match a (검색기능) and c (검색).
        assert_match("검색", {a, c})
        # Korean phrase that is fully present in a but not c.
        assert_match("검색기능", {a})
        # Korean substring that crosses no whitespace, only in a.
        assert_match("색기", {a})
        # English-only query, should match b and c.
        assert_match("function", {b, c})
        # Multi-word AND, only b has both.
        assert_match("search function", {b})
        # Mixed Korean + English, only c has both.
        assert_match("검색 function", {c})
        # Regex mode (bypasses index, should still work).
        assert_match("한국어", {a}, use_regex=False)
        assert_match(r"한국어|english content", {a, b}, use_regex=True)

        # Index lookup direct API for spot-checks.
        cands = reclaude._index_lookup("검색")
        assert cands is not None and {pathlib.Path(p) for p in cands} == {a, c}, cands
        cands = reclaude._index_lookup("검색기능")
        assert cands is not None and {pathlib.Path(p) for p in cands} == {a}, cands
        # 1-char query: cannot filter, should return None.
        assert reclaude._index_lookup("검") is None

        # Stale invalidation: rewrite file b with new content, re-index, ensure
        # old terms no longer match.
        make_jsonl(b, ["completely different line"])
        for _ in range(5):
            picked = reclaude._index_pick_pending()
            if picked is None:
                break
            p, mt, sz = picked
            reclaude._index_file(p, mt, sz)
        assert_match("function", {c})  # b no longer matches
        assert_match("different", {b})

        # Deleted file purge: remove c and step the indexer once.
        c.unlink()
        for _ in range(3):
            picked = reclaude._index_pick_pending()
            if picked is None:
                break
        assert_match("검색", {a})  # c gone

        # Partial-index correctness: add a brand-new file and search BEFORE
        # the indexer has a chance to process it. The unknown-files fallback
        # should keep search correct.
        d = projects / "proj-d" / "session-ddd.jsonl"
        make_jsonl(d, ["완전히 새로운 검색 콘텐츠"])
        # NOTE: do NOT call _index_pick_pending here.
        assert_match("검색", {a, d})
        # Stale-file correctness: rewrite an already-indexed file with new
        # content and search BEFORE re-indexing.
        make_jsonl(a, ["전혀 다른 메모"])
        # Bump mtime explicitly in case content size is identical to before.
        import time as _t; _t.sleep(0.01); a.touch()
        assert_match("다른 메모", {a})
        # And: 검색 should no longer match a (after content change), but the
        # index still says it does. Verification must reject it.
        assert_match("검색", {d})

        # Idle detection: _is_user_active reflects (subscribers>0 AND visible+focused).
        import queue as _q
        # No subscribers: always inactive regardless of visibility flag.
        with reclaude._visibility_lock:
            reclaude._user_active = True
        assert reclaude._is_user_active() is False, "no subscribers must be inactive"
        # Subscribers present but reported inactive: inactive.
        fake_sub: "_q.Queue[dict]" = _q.Queue()
        with reclaude._state_lock:
            reclaude._subscribers.append(fake_sub)
        try:
            with reclaude._visibility_lock:
                reclaude._user_active = False
            assert reclaude._is_user_active() is False, "reported inactive must be inactive"
            with reclaude._visibility_lock:
                reclaude._user_active = True
            assert reclaude._is_user_active() is True, "reported active w/ subscriber must be active"
        finally:
            with reclaude._state_lock:
                reclaude._subscribers.remove(fake_sub)
            with reclaude._visibility_lock:
                reclaude._user_active = False
        print("  ok: _is_user_active gating")

        print("ALL OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
