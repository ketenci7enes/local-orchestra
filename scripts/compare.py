"""Bir rolün tüm aday modellerini AYNI istemle dener; süre ve çıktıyı yan yana gösterir.
  python compare.py general "Bu paragrafı daha sade Türkçe ile yeniden yaz: ..."
Model seçimini tahmine değil, kendi görevlerinle ölçtüğün sonuca göre yapmak için kullan."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from llm import LLMClient


def main():
    if len(sys.argv) < 3:
        sys.exit('Kullanım: python compare.py <rol> "<istem>"')
    role, prompt = sys.argv[1], " ".join(sys.argv[2:])
    llm = LLMClient()
    for cand in llm.cfg["roles"][role]["models"]:
        t0 = time.time()
        try:
            r = llm._client(cand["provider"]).chat.completions.create(
                model=cand["model"], messages=[{"role": "user", "content": prompt}], max_tokens=800
            )
            out = (r.choices[0].message.content or "").strip() or "(boş yanıt)"
        except Exception as e:  # karşılaştırma aracı: hatayı da sonuç say
            out = f"HATA: {type(e).__name__}: {str(e)[:120]}"
        print(f"\n=== {cand['provider']}/{cand['model']} ({time.time() - t0:.1f}s) ===\n{out[:1200]}")


if __name__ == "__main__":
    main()
