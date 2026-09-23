"""Reverse import closure of packages/common/provider_atomic.py over the repo; marked tests.
Edges: `import a.b.c` -> a, a.b, a.b.c; `from a.b import c` -> a, a.b, and a.b.c when that is a module;
relative imports resolved against the importing module's package. Only repo modules count."""

import ast
import re
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
skip = {".venv", "node_modules", ".git", "apps/frontend"}
files = [p for p in root.rglob("*.py") if not any(str(p.relative_to(root)).startswith(s) for s in skip)]
mod_of, file_of = {}, {}
for p in files:
    rel = p.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        continue
    m = ".".join(parts)
    mod_of[p] = m
    file_of[m] = p


def deps(p):
    m = mod_of[p]
    pkg = m if p.name == "__init__.py" else m.rpartition(".")[0]
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    out = set()

    def add(name):
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            if ".".join(parts[:i]) in file_of:
                out.add(".".join(parts[:i]))

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                add(a.name)
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                base = pkg.split(".") if pkg else []
                base = base[: len(base) - (n.level - 1)] if n.level > 1 else base
                modname = ".".join(base + ([n.module] if n.module else []))
            else:
                modname = n.module or ""
            if modname:
                add(modname)
            for a in n.names:
                if modname and f"{modname}.{a.name}" in file_of:
                    add(f"{modname}.{a.name}")
    return out


rev = {}
for p in files:
    if p not in mod_of:
        continue
    for d in deps(p):
        rev.setdefault(d, set()).add(mod_of[p])
target = "packages.common.provider_atomic"
seen = {target}
stack = [target]
while stack:
    for u in rev.get(stack.pop(), ()):
        if u not in seen:
            seen.add(u)
            stack.append(u)
tests = sorted(
    str(file_of[m].relative_to(root)) for m in seen if m.startswith("tests.") and file_of[m].name.startswith("test_")
)
pat = re.compile(r"pytest\.mark\.(e2e|grib|integration|real_disk)\b")
marked = [t for t in tests if pat.search((root / t).read_text(encoding="utf-8"))]
print(f"closure_modules={len(seen)} test_files={len(tests)} marked={len(marked)}")
for t in marked:
    print(t)
