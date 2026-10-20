import tempfile
import os
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
import coder
import workspace as ws
import verify
from mcp_server import (
    orchestra_apply_edit,
    orchestra_write_file,
    orchestra_list_files,
    orchestra_search,
    orchestra_run_tests,
    orchestra_git,
    orchestra_review,
    orchestra_read_file,
)
from judge import Judge
from llm import LLMClient
from tracker import tracker


def _setup_temp_project():
    tmp = tempfile.mkdtemp()
    root = Path(tmp)
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "src" / "utils.py").write_text("LOG_LEVEL = 'INFO'\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_app.py").write_text(
        "from src.app import add\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    return root


def _patch_workspace(root):
    """mcp_server.WORKSPACE'i test geçici dizinine yönlendir"""
    original = mcp_server.WORKSPACE
    mcp_server.WORKSPACE = root
    return original


def test_apply_edit_and_write_file():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        # orchestra_write_file - yeni dosya
        result = asyncio.run(orchestra_write_file("src/new.py", "print('hello')\n"))
        assert "✅" in result
        assert "new.py" in result
        assert (root / "src" / "new.py").read_text() == "print('hello')\n"

        # orchestra_apply_edit - mevcut dosyayı düzenle
        result = asyncio.run(orchestra_apply_edit("src/app.py", "return a + b", "return a + b + 1"))
        assert "✅" in result
        assert "app.py" in result
        assert "return a + b + 1" in (root / "src" / "app.py").read_text()

        # SEARCH metni tekrar eden dosyada hata vermeli
        (root / "src" / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
        result = asyncio.run(orchestra_apply_edit("src/dup.py", "x = 1", "x = 2"))
        assert "❌" in result or "hata" in result.lower()
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_list_files_and_search():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        # list_files
        result = asyncio.run(orchestra_list_files())
        assert "src/app.py" in result
        assert "tests/test_app.py" in result

        result = asyncio.run(orchestra_list_files("src"))
        assert "src/app.py" in result
        assert "tests" not in result

        # search
        result = asyncio.run(orchestra_search("def add", include=".py"))
        assert "src/app.py" in result
        assert "add" in result

        result = asyncio.run(orchestra_search("LOG_LEVEL", include=".py"))
        assert "src/utils.py" in result
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_run_tests():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        # Otomatik pytest tespiti
        result = asyncio.run(orchestra_run_tests())
        assert "✅" in result or "❌" in result  # test sonucu herhangi biri
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_git_status():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        # Git repo init et
        import subprocess
        subprocess.run(["git", "init"], cwd=root, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=root, capture_output=True)

        result = asyncio.run(orchestra_git("status"))
        assert "src/app.py" in result or "(temiz)" in result

        result = asyncio.run(orchestra_git("diff"))
        assert isinstance(result, str)
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_review():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        diff = """--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a - b
"""
        result = asyncio.run(orchestra_review("toplama fonksiyonu düzelt", diff))
        # Jev AI key yoksa LLM judge döner, ya APPROVED ya REJECTED ya UNAVAILABLE
        assert any(x in result for x in ["APPROVED", "REJECTED", "UNAVAILABLE"])
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_read_file_offset_limit():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        # Büyük dosya oluştur
        big_content = "\n".join([f"line {i}" for i in range(500)])
        (root / "big.txt").write_text(big_content, encoding="utf-8")

        result = asyncio.run(orchestra_read_file("big.txt", offset=0, limit=100))
        assert "line 0" in result
        assert "line 500" not in result

        result = asyncio.run(orchestra_read_file("big.txt", offset=100, limit=50))
        # offset byte-based, sadece kısmi içerik döndüğünü doğrula
        assert "line" in result
        assert len(result) < len(big_content)  # kısaltılmış
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_mcp_server_import():
    # mcp_server import edilebilmeli (bağımlılık çözümlemesi)
    import mcp_server
    assert mcp_server.mcp is not None
    assert mcp_server._llm is not None
    assert mcp_server._judge is not None