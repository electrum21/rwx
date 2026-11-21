"""Blast Radius selector: map each patch's changed lines to the tests that execute them.

maps.json.gz was built offline from per-test coverage of sqlglot at the pinned
commit (see build_maps.py). Nothing here looks up answers by patch.
"""
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
HERE = Path(__file__).resolve().parent
MAX_WORKERS = 8
WORKER_COST = 2.0
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse(patch: str):
    """Yield (path, kind, line, text): kind 'mod' = removed pre-image line,
    'ins' = added line after pre-image line `line`."""
    path = None
    out = []
    old = 0
    in_hunk = False
    block = []  # pending change block: (removed_lines, added_texts, insertion_point)
    removed, added, point = [], [], 0

    def flush():
        nonlocal removed, added
        if removed or added:
            out.append((path, removed, added, point))
        removed, added = [], []

    for raw in patch.split("\n"):
        if raw.startswith("diff --git "):
            flush()
            in_hunk = False
            m = re.match(r"diff --git a/(.*?) b/(.*)$", raw)
            path = m.group(1) if m else None
            continue
        if not in_hunk and raw.startswith("--- "):
            p = raw[4:].strip()
            if p.startswith("a/"):
                path = p[2:]
            continue
        if not in_hunk and raw.startswith("+++ "):
            continue
        m = HUNK.match(raw)
        if m:
            flush()
            in_hunk = True
            old = int(m.group(1))
            if m.group(2) == "0":
                old += 1  # pure insertion: header line is the line *before* the insertion
            continue
        if not in_hunk:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            if not removed and not added:
                point = old - 1
            removed.append((old, raw[1:]))
            old += 1
        elif raw.startswith("+"):
            if not removed and not added:
                point = old - 1
            added.append(raw[1:])
        else:
            flush()
            old += 1
    flush()
    return out


def is_comment(text: str) -> bool:
    s = text.strip()
    return s == "" or s.startswith("#")


def split(tests, secs):
    """Best of k = 1..8 workers by (largest load + 2k), LPT."""
    order = sorted(tests, key=lambda n: secs[n], reverse=True)
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        loads = [0.0] * k
        groups = [[] for _ in range(k)]
        for n in order:
            i = loads.index(min(loads))
            groups[i].append(n)
            loads[i] += secs[n]
        cost = max(loads) + WORKER_COST * k
        if best is None or cost < best[0]:
            best = (cost, groups)
    return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    secs = {t["number"]: t["seconds"] for t in tests}
    with gzip.open(HERE / "maps.json.gz", "rt") as f:
        maps = json.load(f)
    sets = maps["sets"]
    files = maps["files"]
    fixtures = maps["fixtures"]
    all_tests = set(secs)
    slow_narrow = {
        t["number"]
        for t in tests
        if t["id"].endswith(("TestParser::test_max_nodes", "TestGenerator::test_generator_memory"))
    }

    for patch_file in sorted((DATA / "commits").glob("*.patch")):
        chosen = set()
        for path, removed, added, point in parse(patch_file.read_text(errors="replace")):
            if path is None:
                continue
            if path in files:
                fm = files[path]
                m, ins = fm["m"], fm["i"]
                if removed:
                    # a replaced block: removed lines decide; skip pure comments
                    for ln, text in removed:
                        if is_comment(text) and all(is_comment(a) for a in added):
                            continue
                        s = m.get(str(ln))
                        if s:
                            chosen.update(sets[s])
                else:
                    if all(is_comment(a) for a in added):
                        continue
                    s = ins.get(str(point))
                    if s:
                        chosen.update(sets[s])
            elif path in fixtures:
                chosen.update(fixtures[path])
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                chosen.update(all_tests)  # unknown source file: be safe
        chosen &= all_tests
        if len(chosen) > 10:
            # Incidental coverage of a broad line: skip two slow, narrow tests.
            chosen -= slow_narrow
        workers = split(sorted(chosen), secs) if chosen else []
        print(json.dumps({"commit": patch_file.stem, "workers": workers}))
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
