#!/usr/bin/env python3
"""Build-time check: every name a function reads as a global must exist as a global or a builtin.

Why this exists: a reference to a name that is not in scope compiles cleanly and only fails when the code
path runs. The streaming overlays are constructed deep inside engine start-up, so the first time such a bug
speaks is a failed boot -- minutes of work, on a host that may be serving. A real bug of exactly this shape
reached a built image once (`embedding` referenced inside a constructor whose parameter list has only a
table), and the existing textual assertions passed because they check that code is *present*, not that it
resolves.

Restricted to LOAD_GLOBAL for names that are neither module globals, nor builtins, nor anything bound
anywhere in the enclosing function chains (parameters, assignments, imports, comprehensions, args/kwargs),
which keeps the false-positive rate near zero without a full scope analyser. Function-level `import` inside
a try block counts as bound; that is the pattern these overlays use to reach their sibling modules.
"""
import ast
import builtins
import sys
from pathlib import Path


def bound_names(fn: ast.AST) -> set[str]:
    """Everything the function can resolve locally, including nested-scope bindings."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg is not None:
                    names.add(arg.arg)
        elif isinstance(node, ast.Lambda):
            for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                        node.args.vararg, node.args.kwarg]:
                if arg is not None:
                    names.add(arg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.Global):
            names.update(node.names)
    return names


def check_module(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    # module dunders are always bound; __name__/__file__ appear in the overlays' sibling-import idiom
    module_names = set(dir(builtins)) | {'__file__', '__name__', '__doc__', '__spec__',
                                      '__loader__', '__package__', '__builtins__'}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            module_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                module_names.update(n.id for n in ast.walk(tgt) if isinstance(n, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                module_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            module_names.add(node.target.id)
        elif isinstance(node, (ast.Try,)):
            # module-level try/except imports: whatever binds inside counts as defined
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        module_names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    module_names.add(sub.id)
        elif isinstance(node, (ast.If, ast.With)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        module_names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    module_names.add(sub.id)

    problems = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]:
        local = bound_names(fn)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
               and node.id not in local and node.id not in module_names:
                # an enclosing function may bind it (closures); walk outwards
                if any(node.id in bound_names(outer) for outer in _enclosing(tree, fn)):
                    continue
                problems.append(f"{path.name}:{node.lineno}: {node.id}")
    return problems


def _enclosing(tree, target):
    """Yield functions lexically containing `target`, innermost first."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if sub is target and node is not target:
                    yield node


def main(argv):
    here = Path(__file__).resolve().parent
    files = [Path(a) for a in argv] or sorted(here.glob("qwen_*.py"))
    bad = []
    for f in files:
        bad += check_module(f)
    for line in bad:
        print(f"  FAIL  {line}")
    print(f"  checked {len(files)} overlay files for unresolvable global reads: "
          f"{'clean' if not bad else str(len(bad)) + ' PROBLEM(S)'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
