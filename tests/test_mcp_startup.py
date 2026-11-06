"""Açılış süresi bütçesi: MCP stdio sunucusu OpenCode startup timeout'una düşmemeli.

`openai` SDK'si ~3.5 s'de yükleniyordu; MCP el sıkışması buna takılınca OpenCode
"Request timed out" veriyordu. Bu test regresyonu yakalar.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server.py"

# Ölçülen: 1.0-1.8s (tembel openai import). Bol pay bırakılıyor (yavaş CI makineleri).
STARTUP_BUDGET_S = 10.0


def _handshake():
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)], cwd=str(ROOT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        t0 = time.time()
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "timing", "version": "1"}},
        }) + "\n")
        proc.stdin.flush()
        init_line = proc.stdout.readline()
        init_s = time.time() - t0

        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        }) + "\n")
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }) + "\n")
        proc.stdin.flush()
        tools_line = proc.stdout.readline()
        tools_s = time.time() - t0
        return init_line, init_s, tools_line, tools_s
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_startup_within_budget():
    init_line, init_s, tools_line, tools_s = _handshake()
    assert init_line.strip(), "initialize yanıtı boş"
    assert init_s < STARTUP_BUDGET_S, f"initialize {init_s:.2f}s > {STARTUP_BUDGET_S}s"
    assert tools_s < STARTUP_BUDGET_S, f"tools/list {tools_s:.2f}s > {STARTUP_BUDGET_S}s"
    assert len(json.loads(tools_line)["result"]["tools"]) == 17


def test_server_reports_instructions():
    init_line, _, _, _ = _handshake()
    init = json.loads(init_line)["result"]
    text = init.get("instructions") or ""
    assert "orchestra_read_file" in text
    assert "orchestra_run_task" in text
    # NOT: "ÖNCELİKLİ".lower() birleşik nokta (U+0307) üretir — projenin kendi
    # düzelttiği tuzak. Karşılaştırmadan önce birleşik işaretleri temizle.
    flat = text.lower().replace("\u0307", "")
    assert "öncelikli" in flat


def test_openai_import_is_lazy():
    """mcp_server import edildiğinde openai SDK'si yüklenmemiş olmalı."""
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import mcp_server;"
        "print('openai' in sys.modules)"
    ) % ROOT
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                         stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "openai tembel yüklenmiyor (açılış yavaşlar)"
