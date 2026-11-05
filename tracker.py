"""Token/çağrı sayacı ve JSONL çalışma günlüğü."""
import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).parent
_LOCK = threading.Lock()  # kurul paralel çalıştığında dosya yazımı karışmasın

# Log hijyeni (F): günlüklere/yedeklere sızabilecek sırları maskele
SECRET_RX = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(nvapi-[A-Za-z0-9_\-]{10,})"),
    re.compile(r"(AIza[0-9A-Za-z_\-]{20,})"),
    re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),
    re.compile(r"(gsk_[A-Za-z0-9]{20,})"),
    re.compile(r"((?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s,;\"']{8,})", re.I),
]


def mask_secrets(text) -> str:
    """Bilinen API anahtar kalıplarını maskeler."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for rx in SECRET_RX:
        if rx.groups >= 2:
            out = rx.sub(lambda m: m.group(1) + "***", out)
        else:
            out = rx.sub("***", out)
    return out


def mask_deep(obj):
    """İç içe yapılarda metin alanlarını maskeler."""
    if isinstance(obj, str):
        return mask_secrets(obj)
    if isinstance(obj, dict):
        return {k: mask_deep(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_deep(v) for v in obj]
    return obj



class Tracker:
    def __init__(self):
        self.llm = defaultdict(lambda: {"calls": 0, "prompt": 0, "completion": 0})
        self.jev = {"calls": 0, "est_tokens": 0}
        runs = BASE_DIR / "runs"
        runs.mkdir(exist_ok=True)
        self.logfile = runs / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"

    def add_llm(self, provider, model, prompt_t, completion_t):
        with _LOCK:
            s = self.llm[f"{provider}:{model}"]
            s["calls"] += 1
            s["prompt"] += prompt_t or 0
            s["completion"] += completion_t or 0

    def add_jev(self, payload_chars):
        with _LOCK:
            self.jev["calls"] += 1
            self.jev["est_tokens"] += payload_chars // 4  # kaba tahmin

    def log(self, event, **data):
        row = {"t": round(time.time(), 1), "event": event, **mask_deep(data)}
        with _LOCK:
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def report(self):
        print("\n--- 📊 OTURUM RAPORU (ücretsiz modeller: maliyet $0) ---")
        for name, s in self.llm.items():
            print(f"{name}: {s['calls']} çağrı | {s['prompt']} prompt + {s['completion']} completion token")
        if self.jev["calls"]:
            print(f"Jev AI: {self.jev['calls']} çağrı | ~{self.jev['est_tokens']} token (tahmini)")
        print(f"Günlük: {self.logfile}")
        print("--------------------------------------------------------\n")


tracker = Tracker()
