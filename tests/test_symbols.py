"""Sembol haritası testleri (symbols.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import symbols  # noqa: E402


def _proj(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "core.py").write_text(
        "import os\n\n\n"
        "class Engine:\n"
        '    """Motor."""\n\n'
        "    def start(self):\n"
        "        return 1\n\n"
        "    def stop(self):\n"
        "        return 0\n\n\n"
        "def helper(value):\n"
        '    """Yardımcı."""\n'
        "    return value + 1\n",
        encoding="utf-8",
    )
    (tmp_path / "web.ts").write_text(
        "export class Widget {\n}\n"
        "export function render(node) {\n}\n",
        encoding="utf-8",
    )
    return tmp_path


def test_python_symbols(tmp_path):
    root = _proj(tmp_path)
    syms = symbols.file_symbols(root / "pkg" / "core.py")
    names = {s["name"] for s in syms}
    assert {"Engine", "start", "stop", "helper"} <= names
    engine = next(s for s in syms if s["name"] == "Engine")
    assert engine["kind"] == "class"
    helper = next(s for s in syms if s["name"] == "helper")
    assert helper["kind"] == "function"
    assert helper["line"] < helper["end"] + 1


def test_generic_symbols(tmp_path):
    root = _proj(tmp_path)
    syms = symbols.file_symbols(root / "web.ts")
    names = {s["name"] for s in syms}
    assert {"Widget", "render"} <= names


def test_symbol_map_contains_files(tmp_path):
    root = _proj(tmp_path)
    smap = symbols.symbol_map(root)
    assert "pkg/core.py" in smap
    assert "helper" in smap


def test_symbol_map_budget(tmp_path):
    root = _proj(tmp_path)
    smap = symbols.symbol_map(root, max_chars=60)
    assert len(smap) <= 200  # kesme satırı dahil küçük kalır


def test_validate_selection(tmp_path):
    root = _proj(tmp_path)
    files, warn = symbols.validate_selection(root, ["pkg/core.py"], "helper fonksiyonunu düzelt")
    assert files == ["pkg/core.py"]
    assert warn == ""

    files, warn = symbols.validate_selection(root, ["web.ts"], "helper fonksiyonunu düzelt")
    assert "web.ts" in warn


def test_suggest_files(tmp_path):
    root = _proj(tmp_path)
    got = symbols.suggest_files(root, "Engine start motor")
    assert got and got[0].endswith("core.py")


def test_relevant_bodies(tmp_path):
    root = _proj(tmp_path)
    body = symbols.relevant_bodies(root, "pkg/core.py", "start motor", max_lines=20)
    assert "def start" in body


def test_cache_invalidation(tmp_path):
    root = _proj(tmp_path)
    p = root / "pkg" / "core.py"
    assert any(s["name"] == "helper" for s in symbols.file_symbols(p))
    p.write_text("def yeni():\n    return 2\n", encoding="utf-8")
    assert any(s["name"] == "yeni" for s in symbols.file_symbols(p))
