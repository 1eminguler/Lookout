"""Checks and runs extractors (site-reading code the agent wrote) in a separate, time-limited process."""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List

HELPERS = {"get", "get_json", "post_json", "browser_eval", "absolute"}
MODULES = {"json", "re", "html", "math", "time", "datetime", "itertools", "collections", "typing", "urllib.parse",
           "unicodedata", "string", "functools"}
BANNED_CALLS = {"eval", "exec", "compile", "open", "__import__", "globals", "locals", "vars", "getattr", "setattr",
                "delattr", "input", "breakpoint", "memoryview", "help"}
RUNNER = """import json, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, sys.argv[1])
import extractor
items = extractor.fetch()
if not isinstance(items, list):
    raise TypeError("fetch() must return a list")
print("@@LOOKOUT@@" + json.dumps(items, ensure_ascii=False, default=str))
"""


def check(code: str) -> None:
    """Raises ValueError if the code uses anything outside the allowed surface."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"syntax error: {e}") from None
    has_fetch = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in MODULES:
                    raise ValueError(f"import of '{a.name}' is not allowed")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "lookout_extract":
                bad = [a.name for a in node.names if a.name not in HELPERS]
                if bad:
                    raise ValueError(f"lookout_extract.{bad[0]} is not available")
            elif mod not in MODULES and mod.split(".")[0] not in {"datetime", "typing", "collections"}:
                raise ValueError(f"import from '{mod}' is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(f"access to '{node.attr}' is not allowed")
        elif isinstance(node, ast.Name) and (node.id in BANNED_CALLS or node.id.startswith("__")):
            raise ValueError(f"'{node.id}' is not allowed")
        elif isinstance(node, ast.FunctionDef) and node.name == "fetch":
            has_fetch = True
    if not has_fetch:
        raise ValueError("no fetch() function")


def run(code: str, timeout: int = 180) -> List[Dict[str, Any]]:
    check(code)
    work = Path(tempfile.gettempdir()) / "lookout_extractor" / uuid.uuid4().hex[:10]
    work.mkdir(parents=True, exist_ok=True)
    try:
        (work / "extractor.py").write_text(code, encoding="utf-8")
        (work / "runner.py").write_text(RUNNER, encoding="utf-8")
        shutil.copy(Path(__file__).with_name("lookout_extract.py"), work / "lookout_extract.py")
        r = subprocess.run([sys.executable, "-I", str(work / "runner.py"), str(work)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=str(work),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdin=subprocess.DEVNULL)
        marker = r.stdout.rfind("@@LOOKOUT@@")
        if r.returncode != 0 or marker < 0:
            err = (r.stderr or r.stdout).strip().splitlines()
            raise RuntimeError("extractor failed: " + (err[-1] if err else f"exit code {r.returncode}"))
        items = json.loads(r.stdout[marker + len("@@LOOKOUT@@"):])
        return [i for i in items if isinstance(i, dict)]
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"extractor took longer than {timeout}s") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def agrees(agent_items: List[Dict], code_items: List[Dict]) -> bool:
    """Does the code find (most of) what the agent found by browsing?"""
    if not agent_items:
        return True
    norm = lambda s: " ".join(str(s or "").lower().split())[:80]
    code_titles = {norm(i.get("title")) for i in code_items}
    code_urls = {str(i.get("url") or "").rstrip("/") for i in code_items}
    hits = sum(1 for a in agent_items if norm(a.get("title")) in code_titles or str(a.get("url") or "").rstrip("/") in code_urls)
    return hits >= max(1, int(len(agent_items) * 0.6))
