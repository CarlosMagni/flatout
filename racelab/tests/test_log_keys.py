"""Every key a log entry gets, and every reason code a refusal carries, has its text in both UI languages
(web/js/i18n.js): "srv.<key>" for the log entries of app.py, hwtest.py and cu/firmware.py, "hw.why.<code>"
for the refusals of the hardware tests."""
import ast
import re
from pathlib import Path

import racelab

PKG = Path(racelab.__file__).parent


LOG_SOURCES = ("app.py", "hwtest.py", "cu/firmware.py")  # where `_log` is called


def i18n_keys(dictionary: str, prefix: str) -> set[str]:
    src = (PKG / "web/js/i18n.js").read_text()
    body = re.search(rf"^const {dictionary} = \{{(.*?)^\}};", src, re.S | re.M).group(1)
    return set(re.findall(rf'^\s*"({re.escape(prefix)}[^"]+)":', body, re.M))


def srv_keys(dictionary: str) -> set[str]:
    return i18n_keys(dictionary, "srv.")


def test_log_keys_translated():
    en, fr = srv_keys("en"), srv_keys("fr")
    assert en and en == fr, sorted(en ^ fr)
    for source in LOG_SOURCES:
        tree = ast.parse((PKG / source).read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_log"]
        assert calls, source
        for call in calls:
            assert len(call.args) >= 3, f"{source} line {call.lineno}: _log without a key"
            for key, exact in heads(call.args[2]):
                ok = ("srv." + key in en) if exact else any(k.startswith("srv." + key) for k in en)
                assert ok, f"{source} line {call.lineno}: {key}"


def test_refusal_codes_translated():
    en, fr = i18n_keys("en", "hw.why."), i18n_keys("fr", "hw.why.")
    assert en == fr, sorted(en ^ fr)
    codes = set()
    for source in ("app.py", "hwtest.py"):
        tree = ast.parse((PKG / source).read_text())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "OpError" and len(n.args) >= 2:
                assert isinstance(n.args[1], ast.Constant), f"{source} line {n.lineno}: code not checkable"
                codes.add(n.args[1].value)
    assert codes >= {"link", "bots", "session", "mode", "estop", "transport", "bots_active", "busy", "slot_api"}
    for code in codes:
        assert f"hw.why.{code}" in en, code


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
