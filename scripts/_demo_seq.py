"""Tek seferlik demo: 'kurul neden siralı calisiyor' -> Nemotron analizi -> kurula soru."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import os
import sys
import time

os.chdir(_ROOT)


import council  # noqa: E402
from llm import LLMClient  # noqa: E402

OUT = []
MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
CODE = """
def generate_proposals(llm, goal: str, context: str) -> list:
    proposals = []
    for role in COUNCIL_ROLES:              # "council_a", "council_b", "council_c"
        data = _one_proposal(llm, role, goal, context)   # BLOKLAYAN cagri, dongude SIRALI
        if not data:
            continue
        ...
    return proposals
"""


def emit(s=""):
    print(s, flush=True)
    OUT.append(str(s))


llm = LLMClient()

PROMPT = f"""Sen kidemli bir yazilim mimarisin. Asagida bir orkestrasyon sisteminin "kurul" adimi var.
PROBLEM: Kurul uyeleri (3 ayri model) PARALEL degil, SIRALI calisiyor; toplam gecikme uc cagrinin sureleri toplamina esit.
KOD:
{CODE}
1) Bunun teknik KOK NEDENINI kod seviyesinde analiz et.
2) Olculebilir sonuclarini (gecikme, hata izolasyonu, kaynak kullanimi) maddele.
3) Simdilik kod degistirme; sadece teshis ve kanit sun."""

emit("=" * 100)
emit(f"1) ANALIZ — model: {MODEL} (OpenRouter)")
emit("=" * 100)
t0 = time.time()
r = llm._client("openrouter").chat.completions.create(
    model=MODEL, messages=[{"role": "user", "content": PROMPT}], max_tokens=2000, timeout=120
)
analysis = (r.choices[0].message.content or "").strip()
emit(analysis)
emit(f"\n[analiz suresi: {time.time()-t0:.1f}s]")


class TimedLLM:
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    @property
    def cfg(self):
        return self.inner.cfg

    @property
    def last_used(self):
        return self.inner.last_used

    def chat(self, role, messages, **kw):
        t = time.time()
        out = self.inner.chat(role, messages, **kw)
        self.calls.append((role, time.time() - t, dict(self.inner.last_used.get(role, {}))))
        return out


timed = TimedLLM(llm)
goal = ("Asagidaki analiz, kurul adiminin NEDEN sirali calistigini acikliyor. KURUL SORUSU: "
        "Bu problem nasil cozulmeli? Somut, dosya/fonksiyon seviyesinde cozum onerisi uret.")
context = f"PROBLEM ANALIZI ({MODEL} tarafindan):\n{analysis}\n\n--- ILGILI KOD ---\n{CODE}"

emit("\n" + "=" * 100)
emit("2) ANALIZ CIKTISI KURULA GONDERILDI — soru: 'Bu problem nasil cozulmeli?'")
emit("=" * 100)
t1 = time.time()
props = council.generate_proposals(timed, goal, context)
total = time.time() - t1

emit("\n" + "=" * 100)
emit(f"3) KURUL CEVAPLARI (toplam duvar suresi: {total:.1f}s)")
emit("=" * 100)
for i, (role, dt, used) in enumerate(timed.calls, 1):
    emit(f"  Cagri {i} [{role}] sure={dt:6.1f}s  gercek model={used.get('provider')}/{used.get('model')}")
emit(f"  --> Cagri sureleri toplami = {sum(c[1] for c in timed.calls):.1f}s | toplam = {total:.1f}s "
     f"(SIRALI kaniti: toplam ≈ sureler toplami)")
emit()
for p in props:
    emit(f"\n### Oneri {p['id']} — {p['author_label']} (`{p['author_model']}`)")
    if p.get("fell_back"):
        emit("  !! Birincil model yanit vermedi, YEDEK model uretti.")
    emit(f"STRATEJI : {p.get('strategy','')}")
    emit(f"DETAY    :\n{p.get('details','')}")
    if p.get("files"):
        emit(f"DOSYALAR : {', '.join(map(str, p['files']))}")
    emit(f"RISKLER  : {p.get('risks','')}")

with open(os.path.join("runs", "seq_analysis_demo.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
