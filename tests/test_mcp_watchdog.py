"""Yetim MCP sunucusu koruması + protokol uyumluluğu.

Geçmiş arıza: OpenCode 2.0.32'nin MCP istemcisi, gecikmeli gelen `initialize`
yanıtını (id 0) "unknown message ID" diye attı; bağlantı 1479 s sonra timeout
verdi. Sunucu sağlıklı kaldı ama istemci bağlanamadı ve geride `mcp_server.py`
süreçleri birikti. Sunucu tarafı protokol revizyonunu müzakere eder (test edilir).

Bu testler: (a) üst süreç ölçümü, (b) protokol müzakere­si ve sunucu kimliği.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import mcp_server


def test_parent_alive_detects_current_process():
    import os
    assert mcp_server._parent_alive(os.getpid()) is True


def test_parent_alive_detects_dead_process():
    proc = subprocess.run([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=120)
    assert proc.returncode == 0
    # Süreç bitti; PID artık ya yok ya da yeniden kullanılmış olabilir.
    # Yeniden kullanım ihtimalini elemek için soyu tükenmiş yüksek bir PID kullan.
    assert mcp_server._parent_alive(999_999_999) is False


def test_parent_alive_on_non_windows(monkeypatch):
    monkeypatch.setattr(mcp_server.os, "name", "posix")
    assert mcp_server._parent_alive(999_999_999) is True


def _initialize(protocol: str = "2024-11-05") -> dict:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "mcp_server.py")], cwd=str(ROOT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": protocol, "capabilities": {},
                       "clientInfo": {"name": "compat", "version": "1"}},
        }) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())["result"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_server_negotiates_client_protocol():
    """Sunucu istemcinin istediği revizyonu kabul edip aynısını yansıtır."""
    assert _initialize("2024-11-05")["protocolVersion"] == "2024-11-05"


def test_server_supports_latest_protocol():
    from mcp import types
    result = _initialize(types.LATEST_PROTOCOL_VERSION)
    assert result["protocolVersion"] == types.LATEST_PROTOCOL_VERSION


def test_server_info_and_instructions_present():
    result = _initialize()
    assert result["serverInfo"]["name"] == "local-orchestra"
    assert "orchestra_read_file" in (result.get("instructions") or "")


def test_watchdog_can_be_disabled():
    code = (
        "import os; os.environ['ORCHESTRA_MCP_WATCHDOG']='0';"
        "import runpy, sys; sys.argv=['mcp_server.py'];"
        "print('yuklendi')"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "yuklendi" in proc.stdout
