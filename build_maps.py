"""Offline: build maps.json.gz from per-test coverage of sqlglot at the pinned commit."""
import ast, coverage, gzip, json, sys
from collections import defaultdict

SRC = "/tmp/sqlglot"
TESTS = "/mnt/user-data/uploads/Buildathon/sample/tests.json"
tests = json.load(open(TESTS))["tests"]
idnum = {t["id"]: t["number"] for t in tests}

d = coverage.CoverageData(SRC + "/.coverage"); d.read()
raw = {}  # rel -> {line: (set(tests), static)}
for f in d.measured_files():
    rel = f.replace(SRC + "/", "")
    if not rel.startswith("sqlglot/") or not rel.endswith(".py"):
        continue
    m = {}
    for ln, cs in d.contexts_by_lineno(f).items():
        s = set(); static = False
        for c in cs:
            if c == "":
                static = True; continue
            c = c.rsplit("|", 1)[0]
            if c in idnum: s.add(idnum[c])
        m[ln] = (s, static)
    raw[rel] = m

sets = [[]]; setidx = {(): 0}
def sid(s):
    k = tuple(sorted(s))
    if k not in setidx:
        setidx[k] = len(sets); sets.append(list(k))
    return setidx[k]

out = {"files": {}, "fixtures": {}}


import glob, os, re
_children = {}
for p in glob.glob(SRC + "/sqlglot/dialects/*.py"):
    me = os.path.basename(p)[:-3]
    for m_ in re.finditer(r"from sqlglot\.dialects\.(\w+) import", open(p).read()):
        _children.setdefault(m_.group(1), set()).add(me)
def descendants(x):
    seen = {x}; todo = [x]
    while todo:
        y = todo.pop()
        for c in _children.get(y, ()):
            if c not in seen: seen.add(c); todo.append(c)
    return seen
FAMDIRS = ("dialects", "parsers", "generators", "typing")
def family_files(rel):
    parts = rel.split("/")
    if len(parts) == 3 and parts[1] in FAMDIRS and parts[2] not in ("__init__.py", "dialect.py"):
        x = parts[2][:-3]
        return [f"sqlglot/{d}/{m}.py" for m in descendants(x) for d in FAMDIRS]
    return None

ALL = set(idnum.values())
_inst = json.load(open("/tmp/inst.json"))
IMPORT_CLASSES = set(_inst.pop("IMPORT", []))
INST = defaultdict(set)
for tid, names in _inst.items():
    if tid in idnum:
        for n in names: INST[n].add(idnum[tid])
# classes whose arg_types other modules read at import or by name
ARG_TYPES_READ = {"Select", "TableFromRows", "MakeInterval"}

class Info:
    pass

def analyze(rel):
    path = f"{SRC}/{rel}"
    src = open(path).read()
    nlines = src.count("\n") + 2
    tree = ast.parse(src)
    cov = raw.get(rel, {})
    stmt_start = {}      # line -> first line of innermost stmt
    stmt_end = {}
    func_of = {}         # line -> (def_lineno, end_lineno) innermost function
    class_of = {}        # line -> (lineno, end) outermost class
    header = {}          # line -> (func lineno,end) if the line is in def header/decorators
    def visit(node, func, cls):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.stmt):
                lo = ch.lineno; hi = ch.end_lineno
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    decos = [x.lineno for x in ch.decorator_list]
                    top = min([lo] + decos)
                    bodystart = ch.body[0].lineno
                    for l in range(top, bodystart):
                        stmt_start[l] = top
                    nf = func; nc = cls
                    if isinstance(ch, ast.ClassDef):
                        if cls is None: nc = (top, ch.end_lineno)
                    else:
                        nf = (top, ch.end_lineno)
                        for l in range(top, bodystart): header[l] = nf
                        # header line numbers inside func region too
                    for l in range(top, ch.end_lineno + 1):
                        if nf: func_of[l] = nf if isinstance(ch,(ast.FunctionDef,ast.AsyncFunctionDef)) or l in func_of or True else None
                        if nc: class_of[l] = nc
                    visit(ch, nf, nc)
                elif hasattr(ch, "body") and isinstance(getattr(ch, "body"), list):
                    # compound: header lines until first body line
                    first_body = ch.body[0].lineno
                    for l in range(lo, first_body):
                        if l not in stmt_start:
                            stmt_start[l] = lo; stmt_end[l] = first_body - 1
                    # elif/else handled in their own children (orelse are stmts)
                    for l in range(lo, hi + 1):
                        pass
                    visit(ch, func, cls)
                    for attr in ("orelse", "finalbody", "handlers"):
                        pass
                else:
                    for l in range(lo, hi + 1):
                        stmt_start[l] = lo; stmt_end[l] = hi
                    visit(ch, func, cls)
            else:
                visit(ch, func, cls)
    visit(tree, None, None)
    # fix func_of: reset to innermost properly
    func_of2 = {}
    def vf(node, func):
        for ch in ast.iter_child_nodes(node):
            nf = func
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decos = [x.lineno for x in ch.decorator_list]
                nf = (min([ch.lineno] + decos), ch.end_lineno)
                for l in range(nf[0], nf[1] + 1): func_of2[l] = nf
            vf(ch, nf)
    vf(tree, None)

    argt = []  # (lo, hi, tests) for arg_types assignments in expression classes
    if rel.startswith("sqlglot/expressions/"):
        modname = rel[:-3].replace("/", ".")
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for st in cls.body:
                tgt = st.targets[0] if isinstance(st, ast.Assign) and len(st.targets) == 1 else getattr(st, "target", None)
                if isinstance(st, (ast.Assign, ast.AnnAssign)) and isinstance(tgt, ast.Name) and tgt.id == "arg_types":
                    q = f"{modname}.{cls.name}"
                    if q in IMPORT_CLASSES or cls.name in ARG_TYPES_READ:
                        ts = ALL
                    else:
                        ts = INST.get(q, set())
                    argt.append((st.lineno, st.end_lineno, ts))

    def static_set(rel):
        # import-time code: dialect families use the family's tests, leaf
        # optimizer/executor modules their file's, shared modules every test
        fam = family_files(rel)
        if fam:
            u = set()
            for ff in fam:
                for e in raw.get(ff, {}).values(): u |= e[0]
            return u or ALL
        if rel.startswith(("sqlglot/optimizer/", "sqlglot/executor/")) and not rel.endswith("__init__.py"):
            return file_union or ALL
        return ALL

    def rt(l):
        e = cov.get(l); return e[0] if e else set()
    def covered(l):
        return l in cov
    def union_range(lo, hi):
        s = set()
        for l in range(lo, hi + 1):
            e = cov.get(l)
            if e: s |= e[0]
        return s
    file_union = union_range(1, nlines)

    def mod(l):
        s0 = stmt_start.get(l, l)
        T = set(rt(l)) | set(rt(s0))
        if l in stmt_end:
            T |= union_range(s0, stmt_end[l])
        if l in header or s0 in header:
            f = header.get(l) or header.get(s0)
            return union_range(*f)
        if T:
            return T
        stat = (l in cov and cov[l][1]) or (s0 in cov and cov[s0][1])
        if stat:
            for lo, hi, ts in argt:
                if lo <= l <= hi:
                    return ts
            return static_set(rel)
        return set()

    def ins(n):
        # insertion after pre-image line n
        f = func_of2.get(n + 1) if func_of2.get(n) is None else func_of2.get(n)
        # strictly inside a function?
        f = func_of2.get(n)
        if f and n < f[1]:
            lo, hi = f
            prev = None
            for l in range(n, lo - 1, -1):
                if covered(l) and l not in header: prev = l; break
            nxt = None
            for l in range(n + 1, hi + 1):
                if covered(l): nxt = l; break
            s = set()
            if prev is not None: s |= rt(prev)
            if nxt is not None: s |= rt(nxt)
            if prev is None and nxt is None: return set()
            return s
        # module / class level static insertion
        for lo, hi, ts in argt:
            if lo <= n < hi:
                return ts
        return static_set(rel)

    m = {}; i = {}
    for l in range(1, nlines):
        a = sid(mod(l))
        if a: m[l] = a
    for n in range(0, nlines):
        a = sid(ins(n))
        if a: i[n] = a
    spans = []
    docs = set()
    for nd in ast.walk(tree):
        if isinstance(nd, ast.Expr) and isinstance(nd.value, ast.Constant) and isinstance(nd.value.value, str):
            docs.add(id(nd.value))
    for nd in ast.walk(tree):
        if isinstance(nd, (ast.Constant, ast.JoinedStr)) and id(nd) not in docs:
            if isinstance(nd, ast.Constant) and not isinstance(nd.value, (str, bytes)):
                continue
            if nd.end_lineno > nd.lineno:
                spans.append([nd.lineno, nd.end_lineno])
    return {"m": m, "i": i, "s": spans}

import glob, os
for p in sorted(glob.glob(SRC + "/sqlglot/**/*.py", recursive=True)):
    rel = os.path.relpath(p, SRC)
    out["files"][rel] = analyze(rel)

opens = json.load(open("/tmp/opens2.json"))
fx = defaultdict(set)
for tid, fs in opens.items():
    if tid.startswith("SETUP "):
        # class-level setup (setUpClass): every test of that class uses it
        cls = tid[6:].rsplit("::", 1)[0] + "::"
        nums = {n for i, n in idnum.items() if i.startswith(cls)}
    else:
        nums = {idnum[tid]} if tid in idnum else set()
    for f in fs:
        fx[f] |= nums
out["fixtures"] = {k: sorted(v) for k, v in fx.items()}
out["sets"] = sets
json.dump(out, gzip.open("/home/claude/work/maps.json.gz", "wt", compresslevel=9), separators=(",", ":"))
print(len(sets), os.path.getsize("/home/claude/work/maps.json.gz"))
