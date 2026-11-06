#!/usr/bin/env python
"""MCP stdio smoke testi: el sıkışma + araç kataloğu + 2 gerçek araç çağrısı.

Kullanım:
    python scripts/mcp_smoke.py
    python scripts/mcp_smoke.py --json

Çıkış kodu 0 = sunucu sağlıklı, 1 = sorun var. "MCP tetiklenmiyor" dendiğinde
ilk çalıştırılacak komut budur; OpenCode tarafındaki sorunu sunucu tarafından ayırır.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "mcp_server.py"
STARTUP_BUDGET_S = 10.0


class Smoke:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            cwd=str(ROOT),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._id = 0

    def _send(self, payload: dict) -> None:
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def _request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self._send({"jsonrpc": "2.0", "id": self._id, "method": method,
                    "params": params or {}})
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"{method}: sunucu yanıt vermeden kapandı")
        msg = json.loads(line)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg["result"]

    def initialize(self) -> dict:
        res = self._request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-smoke", "version": "1"},
        })
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return res

    def call(self, name: str, arguments: dict) -> str:
        res = self._request("tools/call", {"name": name, "arguments": arguments})
        return "\n".join(c.get("text", "") for c in res.get("content", [])
                         if c.get("type") == "text")

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="JSON rapor")
    args = ap.parse_args()

    report: dict = {"ok": False, "checks": []}
    smoke = None
    started = time.time()
    try:
        smoke = Smoke()
        init = smoke.initialize()
        startup = time.time() - started
        report["startup_s"] = round(startup, 2)
        report["server"] = init.get("serverInfo", {})
        report["instructions"] = bool(init.get("instructions"))
        ok_startup = startup <= STARTUP_BUDGET_S
        report["checks"].append({
            "name": "initialize",
            "ok": ok_startup,
            "detail": f"{startup:.2f}s (bütçe {STARTUP_BUDGET_S:.0f}s)",
        })
        if not ok_startup:
            raise RuntimeError(
                f"açılış {startup:.1f}s > {STARTUP_BUDGET_S:.0f}s — OpenCode startup "
                "timeout'una düşer")

        tools = smoke._request("tools/list")["tools"]
        names = {t["name"] for t in tools}
        report["tool_count"] = len(tools)
        report["checks"].append({
            "name": "tools/list",
            "ok": len(tools) == 17,
            "detail": f"{len(tools)} araç",
        })

        for tool in ("orchestra_ocr_status", "orchestra_workspace"):
            if tool not in names:
                report["checks"].append({"name": tool, "ok": False, "detail": "kayıtlı değil"})
                continue
            text = smoke.call(tool, {})
            report["checks"].append({
                "name": tool,
                "ok": bool(text.strip()),
                "detail": text.strip().splitlines()[0][:90] if text.strip() else "boş yanıt",
            })

        report["ok"] = all(c["ok"] for c in report["checks"])
    except Exception as e:  # noqa: BLE001 - teşhis aracı: her hatayı raporla
        report["error"] = f"{type(e).__name__}: {e}"
    finally:
        if smoke:
            smoke.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"MCP smoke — {ROOT}")
        for c in report["checks"]:
            print(f"  {'OK  ' if c['ok'] else 'FAIL'}  {c['name']:24s} {c['detail']}")
        if report.get("error"):
            print(f"  HATA  {report['error']}")
        print("SONUÇ:", "SAĞLIKLI" if report["ok"] else "SORUNLU")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
