"""Exact-phrase (`"quoted"`) search behaviour."""
import json
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import reclaude


def make_jsonl(path: pathlib.Path, msgs: list[tuple[str, str]]) -> None:
    """msgs: (type, text) — type is the raw jsonl `type` field (user/assistant)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for typ, t in msgs:
            f.write(json.dumps({"type": typ, "message": {"content": t}}, ensure_ascii=False) + "\n")


def check_parse() -> None:
    cases = [
        ('"a b" c', (["a b"], ["c"])),
        ('"a b', (["a b"], [])),           # unclosed quote closes at end of input
        ('a b', ([], ["a", "b"])),
        ('"  spaced   out  "', (["spaced out"], [])),
        ('"" x', ([], ["x"])),             # empty phrase is dropped
        ('"A B"', (["a b"], [])),          # lowercased
    ]
    for q, want in cases:
        got = reclaude._parse_query(q)
        assert got == want, f"_parse_query({q!r}) -> {got}, want {want}"
        print(f"  ok: _parse_query({q!r})")


def main() -> None:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="reclaude-phrase-test-"))
    try:
        check_parse()

        projects = tmp / "projects"
        cache = tmp / "cache"
        reclaude.PROJECTS = projects
        reclaude._index_db_path = lambda: cache / "reclaude" / "index.db"
        reclaude._index_local = type(reclaude._index_local)()

        # ordered: contains the phrase verbatim. scrambled: has every word, in a
        # different order — the exact-phrase filter must reject it.
        ordered = projects / "proj" / "session-ordered.jsonl"
        scrambled = projects / "proj" / "session-scrambled.jsonl"
        split = projects / "proj" / "session-split.jsonl"
        make_jsonl(ordered, [("user", "메일 제목: 숙소 상태 정보 검토 안내 입니다")])
        make_jsonl(scrambled, [("user", "안내 검토 정보 상태 숙소 순서가 뒤바뀐 문장")])
        # every phrase word is in the file, but spread across two messages —
        # a phrase must land inside ONE message, so this must not match either.
        make_jsonl(split, [("user", "숙소 상태"), ("assistant", "정보 검토 안내")])

        def ids(q: str, roles: set[str] | None = None) -> set[str]:
            sids, _snips, _scores, _trunc = reclaude._search_sessions(q, roles=roles)
            return set(sids)

        def expect(q: str, want: set[str], roles: set[str] | None = None) -> None:
            got = ids(q, roles)
            assert got == want, f"{q!r} -> {sorted(got)}, want {sorted(want)}"
            print(f"  ok: {q!r} -> {sorted(got)}")

        # Unquoted: word-AND, so every file holding all the words matches.
        expect("숙소 상태 정보 검토 안내", {"session-ordered", "session-scrambled", "session-split"})
        # Quoted: only the file with the verbatim run.
        expect('"숙소 상태 정보 검토 안내"', {"session-ordered"})
        # Phrase + bare word are ANDed; the bare word may live anywhere in file.
        expect('"숙소 상태" 입니다', {"session-ordered"})
        expect('"숙소 상태" 뒤바뀐', set())
        # A phrase that exists nowhere.
        expect('"상태 숙소 정보"', set())
        # Role filter still applies to the message the phrase landed in:
        # "정보 검토 안내" is verbatim in ordered's user line and in split's
        # assistant line, so each role sees only its own file.
        expect('"정보 검토 안내"', {"session-ordered"}, {"user"})
        expect('"정보 검토 안내"', {"session-split"}, {"assistant"})

        # A verbatim hit is ranked as a phrase hit (3), above word-AND noise.
        _sids, _snips, scores, _t = reclaude._search_sessions('"숙소 상태 정보 검토 안내"')
        assert scores.get("session-ordered") == 3, scores
        print("  ok: exact phrase scores 3")

        # Snippets point at the phrase, not at some unrelated single word.
        _sids, snips, _scores, _t = reclaude._search_sessions('"숙소 상태 정보 검토 안내"')
        assert snips["session-ordered"][0]["match"] == "숙소 상태 정보 검토 안내", snips
        print("  ok: snippet match is the phrase")

        # The bigram index must narrow on the phrase's words, not on the raw
        # query (whose quoted tokens would produce bigrams like '"숙').
        while reclaude._index_pick_pending():
            p, mtime, size = reclaude._index_pick_pending()
            reclaude._index_file(p, mtime, size)
        expect('"숙소 상태 정보 검토 안내"', {"session-ordered"})
        print("  ok: exact phrase survives a fully-built index")

        print("ALL OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
