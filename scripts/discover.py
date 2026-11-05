"""Ücretsiz model keşfi ve sağlık kontrolü.
  python discover.py          -> OpenRouter ücretsiz + NVIDIA NIM model listesi
  python discover.py --ping   -> config.yaml'daki her modele küçük bir istek atar"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import requests

from llm import LLMClient


def openrouter_free():
    r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    r.raise_for_status()
    for m in r.json()["data"]:
        pr = m.get("pricing") or {}
        if str(pr.get("prompt")) == "0" and str(pr.get("completion")) == "0":
            tools = "tools" in (m.get("supported_parameters") or [])
            yield m["id"], m.get("context_length"), tools


def nim_models():
    key = os.getenv("NVIDIA_API_KEY", "")
    r = requests.get("https://integrate.api.nvidia.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    r.raise_for_status()
    for m in r.json()["data"]:
        yield m["id"]


def ping_all():
    llm = LLMClient()
    for role, spec in llm.cfg["roles"].items():
        for cand in spec["models"]:
            try:
                llm._client(cand["provider"]).chat.completions.create(
                    model=cand["model"], messages=[{"role": "user", "content": "ping"}], max_tokens=16, timeout=15
                )
                status = "✅"
            except Exception as e:  # teşhis aracı: her hatayı raporla
                status = f"❌ {type(e).__name__}: {str(e)[:80]}"
            print(f"[{role}] {cand['provider']}/{cand['model']}: {status}", flush=True)

    jev_ping()



def jev_ping():
    """Jev (TypeSafe System One) smoke testi: her iki endpoint'i düz gövdeyle dener,
    `answers[qid].choice` parse'ını doğrular. Çalışan endpoint judge.py JEV_URL'e yazılır."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        print("[judge] Jev: ⚠️ OPENROUTER_API_KEY yok, atlandı", flush=True)
        return
    payload = {
        "model": "typesafe/jev-1.14",
        "state": {"ping": True},
        "questions": {"q": {"type": "choice", "instructions": "Sağlık kontrolü: yanıt ver.",
                            "criteria": {"OK": "çalışıyor", "NO": "çalışmıyor"}}},
    }
    endpoints = [
        "https://openrouter.ai/api/alpha/decisions",   # birincil (resmi tutorial)
        "https://openrouter.ai/api/v1/systemone",      # yedek (TypeSafe SDK yolu)
    ]
    for url in endpoints:
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=20,
            )
            if r.status_code < 400:
                choice = None
                try:
                    ans = r.json().get("answers", {}).get("q")
                    choice = ans.get("choice") if isinstance(ans, dict) else ans
                except Exception:
                    pass
                print(f"[judge] Jev {url}: ✅ (HTTP {r.status_code}, choice={choice})", flush=True)
            else:
                print(f"[judge] Jev {url}: ❌ HTTP {r.status_code}: {r.text[:80]}", flush=True)
        except Exception as e:
            print(f"[judge] Jev {url}: ❌ {type(e).__name__}: {str(e)[:80]}", flush=True)



if __name__ == "__main__":
    if "--ping" in sys.argv:
        ping_all()
    else:
        print("== OpenRouter ücretsiz modeller ==")
        for mid, ctx, tools in sorted(openrouter_free()):
            print(f"{mid}  ctx={ctx}  tools={tools}")
        print("\n== NVIDIA NIM modeller ==")
        for mid in sorted(nim_models()):
            print(mid)
