"""Every key app.py gives a log entry has its "srv.<key>" text in both UI languages (web/js/i18n.js)."""
import ast
import re
from pathlib import Path

import racelab

PKG = Path(racelab.__file__).parent


def srv_keys(dictionary: str) -> set[str]:
    src = (PKG / "web/js/i18n.js").read_text()
    body = re.search(rf"^const {dictionary} = \{{(.*?)^\}};", src, re.S | re.M).group(1)
    return set(re.findall(r'^\s*"(srv\.[^"]+)":', body, re.M))


def test_log_keys_translated():
    en, fr = srv_keys("en"), srv_keys("fr")
    assert en and en == fr, sorted(en ^ fr)
    tree = ast.parse((PKG / "app.py").read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_log"]
    assert calls
    for call in calls:
        assert len(call.args) >= 3, f"line {call.lineno}: _log without a key"
        for key, exact in heads(call.args[2]):
            ok = ("srv." + key in en) if exact else any(k.startswith("srv." + key) for k in en)
            assert ok, f"line {call.lineno}: {key}"


def heads(node):
    """(key, exact) for a literal key, (prefix, False) for a built one ("cu_selected." + transport)."""
    if isinstance(node, ast.Constant):
        return [(node.value, True)]
    if isinstance(node, ast.IfExp):
        return heads(node.body) + heads(node.orelse)
    if isinstance(node, ast.BinOp):
        return [(k, False) for k, _ in heads(node.left)]
    if isinstance(node, ast.JoinedStr) and isinstance(node.values[0], ast.Constant):
        return [(node.values[0].value, False)]
    raise AssertionError(f"line {node.lineno}: key not checkable")
