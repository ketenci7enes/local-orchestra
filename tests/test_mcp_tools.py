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


def test_respond_after_cancel_does_not_crash():
    """REGRESYON: 'Request already responded to' sunucuyu öldürmemeli.

    Senaryo: istemci uzun bir araç çağrısını tam bitiş anında iptal eder.
    SDK `responder.cancel()` ile `_completed=True` yapar ve "Request cancelled"
    gönderir; araç fonksiyonu da normal dönüş yapıp `respond()` çağırınca SDK
    `assert not self._completed` ile AssertionError atar. Bu istisna anyio task
    group'tan yukarı çıkıp süreci exit code 1 ile öldürüyordu.

    mcp_server import edildiğinde respond() idempotent hale getirilir; ikinci
    çağrı sessizce döner.
    """
    import asyncio

    import mcp_server  # noqa: F401  (patch'i uygular)
    from mcp import types
    from mcp.shared.session import RequestResponder

    assert getattr(RequestResponder, "_orchestra_respond_patched", False) is True

    sent = []

    class FakeSession:
        async def _send_response(self, request_id, response):
            sent.append((request_id, type(response).__name__))

    req = types.ClientRequest(
        types.CallToolRequest(method="tools/call",
                              params=types.CallToolRequestParams(name="x", arguments={}))
    )

    async def scenario():
        r = RequestResponder(request_id=7, request_meta=None, request=req,
                             session=FakeSession(), on_complete=lambda x: None)
        with r:
            await r.cancel()                      # iptal yanıtı gönderildi
            assert r._completed is True
            # Normal dönüş yolu: patlamamalı, ikinci yanıt göndermemeli
            await r.respond(types.ErrorData(code=0, message="normal", data=None))
        return r

    asyncio.run(scenario())
    assert sent == [(7, "ErrorData")], f"mükerrer yanıt gönderilmemeli, gelen: {sent}"


def test_respond_first_time_still_works():
    """Patch normal yanıtı engellememeli."""
    import asyncio

    import mcp_server  # noqa: F401
    from mcp import types
    from mcp.shared.session import RequestResponder

    sent = []

    class FakeSession:
        async def _send_response(self, request_id, response):
            sent.append((request_id, type(response).__name__))

    req = types.ClientRequest(
        types.CallToolRequest(method="tools/call",
                              params=types.CallToolRequestParams(name="x", arguments={}))
    )

    async def scenario():
        r = RequestResponder(request_id=9, request_meta=None, request=req,
                             session=FakeSession(), on_complete=lambda x: None)
        with r:
            await r.respond(types.ErrorData(code=0, message="ok", data=None))

    asyncio.run(scenario())
    assert sent == [(9, "ErrorData")]


def test_split_command_keeps_quoted_paths():
    """Windows yolları ve tırnaklı argümanlar bozulmadan ayrılmalı."""
    from mcp_server import _split_command
    assert _split_command("pytest -q -k 'a b'") == ["pytest", "-q", "-k", "a b"]
    assert _split_command(r'"C:\Program Files\x\python.exe" -m pytest') == [
        r"C:\Program Files\x\python.exe", "-m", "pytest"
    ]
    assert _split_command(r"python C:\a\b\test.py") == ["python", r"C:\a\b\test.py"]


def test_write_file_keeps_empty_content():
    """Boş içerik '\\n'e dönüşmemeli (eskiden rstrip+newline bozuyordu)."""
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        result = asyncio.run(orchestra_write_file("bos.txt", ""))
        assert "✅" in result
        assert (root / "bos.txt").read_text(encoding="utf-8") == ""
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_read_file_line_range():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        (root / "lines.txt").write_text("\n".join(f"L{i}" for i in range(1, 21)), encoding="utf-8")
        result = asyncio.run(orchestra_read_file("lines.txt", start_line=3, end_line=5))
        assert "L3" in result and "L5" in result
        assert "L2" not in result and "L6" not in result
        assert "satır 3-5" in result
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_workspace_switch_tool():
    root = _setup_temp_project()
    original = _patch_workspace(root)
    try:
        from mcp_server import orchestra_workspace

        before = asyncio.run(orchestra_workspace())
        assert str(root) in before

        result = asyncio.run(orchestra_workspace(str(root / "src")))
        assert "✅" in result
        assert mcp_server.WORKSPACE == (root / "src").resolve()

        bad = asyncio.run(orchestra_workspace(str(root / "yok-boyle-bir-dizin")))
        assert "❌" in bad
    finally:
        mcp_server.WORKSPACE = original
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_git_tool_does_not_hang_with_pipe_stdin():
    """REGRESYON: MCP stdio'da stdin açık bir pipe'tır.

    Alt süreçlere stdin miras kalırsa git EOF bekleyip sonsuza kadar kilitlenir
    (belirti: tools/call yanıtı hiç gelmez). Bu test sunucuyu gerçek stdio
    pipe'ıyla başlatıp orchestra_git'in zamanında yanıt verdiğini doğrular.
    """
    import json
    import subprocess
    import time
    import queue as _queue
    import threading

    root = _setup_temp_project()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, stdin=subprocess.DEVNULL, capture_output=True, env=env)

    srv = str(Path(__file__).resolve().parent.parent / "mcp_server.py")
    proc = subprocess.Popen(
        [sys.executable, srv, "--workspace", str(root)], cwd=str(root),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
    )
    q = _queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in proc.stdout], daemon=True).start()
    try:
        def send(o):
            proc.stdin.write(json.dumps(o) + "\n")
            proc.stdin.flush()

        def wait(rid, timeout):
            t0 = time.time()
            while time.time() - t0 < timeout:
                try:
                    msg = json.loads(q.get(timeout=0.5))
                except Exception:
                    continue
                if msg.get("id") == rid:
                    return msg
            return None

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "t", "version": "1"}}})
        assert wait(1, 30) is not None
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "orchestra_git", "arguments": {"action": "status"}}})

        msg = wait(2, 15)
        assert msg is not None, "orchestra_git 15s içinde yanıt vermedi (stdin kilitlenmesi?)"
        text = msg["result"]["content"][0]["text"]
        assert "(temiz)" in text or ".py" in text
    finally:
        proc.kill()
        try:
            proc.wait(5)
        except Exception:
            pass
        import shutil
        shutil.rmtree(root, ignore_errors=True)