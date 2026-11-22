"""Blast Radius selector.

For each patch: apply it to the pinned sqlglot source shipped in the image,
skip files whose Python AST is unchanged (comments/whitespace), and map the
changed lines to the tests that execute them (per-test coverage built offline
from the pinned commit, see build_maps.py). Anything the map cannot explain
falls back to a wider, safe set: a miss costs 300 s, extra tests cost little.
"""
import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SRC_DIR", HERE / "src"))
MAX_WORKERS = 8
WORKER_COST = 2.0
# Diagnostic builds only: commits whose selection used one of these rules
# print no tests, so the board's misses and seconds measure those rules.
PROBE_BLANK = set()
WIDE = {"compile_fail", "unknown_pkg_file", "static_all", "test_helper", "testfile_whole",
        "testfile_method", "fixture_guess", "unknown_path", "error"}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
NO_EFFECT = re.compile(
    r"^(\.github/|posts/|benchmarks/|pdoc/|docs/|sqlglotc/|sqlglot-integration-tests/)"
    r"|\.(md|rst|txt|png|gif|jpg|svg|yml|yaml|html|jinja2)$"
    r"|^(LICENSE|MANIFEST\.in|Makefile|\.gitignore|\.gitmodules|\.gitpod\.yml|\.pre-commit-config\.yaml)$"
)


def log(*a):
    print(*a, file=sys.stderr)


def parse_patch(text):
    """-> {path: [hunk, ...]}, hunk = (old_start, [(tag, line), ...])"""
    files = {}
    path = None
    cur = None
    for raw in text.split("\n"):
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", raw)
            path = m.group(1) if m else None
            cur = None
            files.setdefault(path, [])
            continue
        if cur is None and (raw.startswith("--- ") or raw.startswith("+++ ")):
            p = raw[4:].strip()
            if raw.startswith("--- ") and p.startswith("a/"):
                path = p[2:]
                files.setdefault(path, [])
            continue
        m = HUNK.match(raw)
        if m:
            start = int(m.group(1))
            if m.group(2) == "0":
                start += 1
            cur = (start, [])
            files.setdefault(path, []).append(cur)
            continue
        if cur is None or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            cur[1].append(("-", raw[1:]))
        elif raw.startswith("+"):
            cur[1].append(("+", raw[1:]))
        elif raw.startswith(" ") or raw == "":
            cur[1].append((" ", raw[1:]))
    return files


def blocks(hunks):
    """-> list of (removed [(old_line, text)], added [text], point) per change block."""
    out = []
    for start, body in hunks:
        old = start
        removed, added, point = [], [], start - 1
        for tag, text in body:
            if tag == " ":
                if removed or added:
                    out.append((removed, added, point))
                removed, added = [], []
                old += 1
                point = old - 1
            elif tag == "-":
                if not removed and not added:
                    point = old - 1
                removed.append((old, text))
                old += 1
            else:
                if not removed and not added:
                    point = old - 1
                added.append(text)
        if removed or added:
            out.append((removed, added, point))
    return out


def apply(old_text, hunks):
    lines = old_text.split("\n")
    out = []
    pos = 0  # 0-based index into lines
    for start, body in hunks:
        target = start - 1
        out.extend(lines[pos:target])
        pos = target
        for tag, text in body:
            if tag == " ":
                out.append(lines[pos] if pos < len(lines) else text)
                pos += 1
            elif tag == "-":
                pos += 1
            else:
                out.append(text)
    out.extend(lines[pos:])
    return "\n".join(out)


def same_ast(old_text, new_text):
    """True / False, or None when the new text does not compile."""
    try:
        new = ast.dump(ast.parse(new_text))
    except (SyntaxError, ValueError):
        return None
    try:
        return ast.dump(ast.parse(old_text)) == new
    except (SyntaxError, ValueError):
        return False


class _IsToEq(ast.NodeTransformer):
    """`x is None` / `x is Enum.MEMBER` behave like `==` for sqlglot's types."""

    @staticmethod
    def _plain(node):
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        return isinstance(node, ast.Attribute) and node.attr.isupper()

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1 and (self._plain(node.comparators[0]) or self._plain(node.left)):
            op = node.ops[0]
            if isinstance(op, ast.Is):
                node.ops = [ast.Eq()]
            elif isinstance(op, ast.IsNot):
                node.ops = [ast.NotEq()]
        return node


def equivalent(old_text, new_text):
    try:
        a = _IsToEq().visit(ast.parse(old_text))
        b = _IsToEq().visit(ast.parse(new_text))
    except (SyntaxError, ValueError):
        return False
    return ast.dump(a) == ast.dump(b)


def is_comment(text):
    s = text.strip()
    return s == "" or s.startswith("#")


def in_string(spans, line, insertion):
    for lo, hi in spans:
        if (lo <= line < hi) if insertion else (lo <= line <= hi):
            return True
    return False


def split(tests, secs):
    """Best of k = 1..8 workers by (largest load + 2k), longest test first."""
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


class Selector:
    def tag(self, name):
        self.tags.add(name)

    def __init__(self):
        self.tags = set()
        tests = json.loads((DATA / "tests.json").read_text())["tests"]
        self.secs = {t["number"]: t["seconds"] for t in tests}
        self.all = set(self.secs)
        self.by_file = {}
        self.by_id = {}
        self.ids = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
            self.by_id[t["id"]] = t["number"]
            self.ids[t["number"]] = t["id"]
        with gzip.open(HERE / "maps.json.gz", "rt") as f:
            maps = json.load(f)
        self.sets = maps["sets"]
        self.files = maps["files"]
        self.fixtures = maps["fixtures"]

    def source(self, path):
        p = SRC / path
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    # -- sqlglot package -------------------------------------------------
    def package_file(self, path, hunks):
        old = self.source(path)
        if old is not None and path.endswith(".py"):
            same = same_ast(old, apply(old, hunks))
            if same is True:
                return set()
            if same is None:
                log(path, "does not compile: all tests")
                self.tag("compile_fail")
                return set(self.all)
            if equivalent(old, apply(old, hunks)):
                return set()
        fm = self.files.get(path)
        if fm is None:
            self.tag("unknown_pkg_file")
            return set(self.all)
        m, ins, spans = fm["m"], fm["i"], fm.get("s", [])
        chosen = set()
        for removed, added, point in blocks(hunks):
            code_added = any(not is_comment(a) for a in added)
            stringy = in_string(spans, point, True) or any(
                in_string(spans, ln, False) for ln, _ in removed
            )
            if not stringy and not code_added and all(is_comment(t) for _, t in removed):
                continue
            got = set()
            for ln, _ in removed:
                s = m.get(str(ln))
                if s:
                    got.update(self.sets[s])
                    if len(self.sets[s]) >= 1500:
                        self.tag("static_all")
            if code_added or stringy or not removed:
                for p in (point, point + len(removed)):
                    s = ins.get(str(p))
                    if s:
                        got.update(self.sets[s])
                        if len(self.sets[s]) >= 1500:
                            self.tag("static_all")
            chosen |= got
        return chosen

    # -- tests directory -------------------------------------------------
    def test_file(self, path, hunks):
        old = self.source(path)
        if old is None:
            return set(self.all)
        new = apply(old, hunks)
        same = same_ast(old, new)
        if same is True:
            return set()
        mine = self.by_file.get(path)
        if not mine or same is None:
            # helpers, __init__, conftest or a file that no longer imports
            self.tag("test_helper" if not mine else "testfile_whole")
            return set(self.all) if not mine else set(mine)
        # changes inside one test method select that test; anything else the file
        tree = ast.parse(old)
        methods = []
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef):
                for fn in cls.body:
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name.startswith("test"):
                        lo = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
                        methods.append((lo, fn.end_lineno, f"{path}::{cls.name}::{fn.name}"))
        chosen = set()
        for removed, added, point in blocks(hunks):
            lines = [ln for ln, _ in removed] or [point, point + 1]
            hit = None
            for lo, hi, tid in methods:
                if all(lo <= ln <= hi for ln in lines) and (removed or lo <= point < hi):
                    hit = tid
            if hit and hit in self.by_id:
                chosen.add(self.by_id[hit])
            else:
                self.tag("testfile_whole")
                return set(mine)
        self.tag("testfile_method")
        return chosen

    def fixture(self, path):
        if path in self.fixtures:
            self.tag("fixture")
            return set(self.fixtures[path])
        self.tag("fixture_guess")
        # a fixture the map never saw read: every test file that names it
        stem = Path(path).name
        chosen = set()
        for f, nums in self.by_file.items():
            src = self.source(f) or ""
            if stem in src or Path(path).stem in src:
                chosen |= nums
        return chosen or set(self.all)

    def select(self, patch_text):
        self.tags = set()
        chosen = set()
        for path, hunks in parse_patch(patch_text).items():
            if path is None:
                continue
            if path.startswith("sqlglot/"):
                chosen |= self.package_file(path, hunks)
            elif path.startswith("tests/fixtures/"):
                chosen |= self.fixture(path)
            elif path.startswith("tests/") and path.endswith(".py"):
                chosen |= self.test_file(path, hunks)
            elif NO_EFFECT.search(path):
                continue
            else:
                # setup.py, pyproject.toml, setup.cfg, conftest and the unknown
                self.tag("unknown_path")
                chosen |= self.all
        return chosen & self.all


def main():
    sel = Selector()
    for patch_file in sorted((DATA / "commits").glob("*.patch")):
        try:
            chosen = sel.select(patch_file.read_text(encoding="utf-8", errors="replace"))
        except Exception as error:  # never crash a run: be safe instead
            log(patch_file.stem, "error", repr(error))
            sel.tags = {"error"}
            chosen = set(sel.all)
        if len(chosen) >= 100:
            # Slow tests that almost never fail: skip them when a broad line selects hundreds of tests.
            chosen -= {n for n, i in sel.ids.items() if i.split("::")[-1] in ('test_execute_tpch', 'test_optimized_tpch', 'test_tpcds', 'test_generator_memory', 'test_max_nodes', 'test_execute_tpcds')}
        workers = split(sorted(chosen), sel.secs) if chosen else []
        if workers and max(sum(sel.secs[n] for n in w) for w in workers) + WORKER_COST * len(workers) >= 15:
            sel.tags.add("cost15" if not (sel.tags & WIDE) else "wide_cost15")
        if PROBE_BLANK & sel.tags:
            workers = []
        print(json.dumps({"commit": patch_file.stem, "workers": workers}))


if __name__ == "__main__":
    main()
