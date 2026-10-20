"""Token/çağrı sayacı ve JSONL çalışma günlüğü."""
import json
import threading
import time
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).parent
_LOCK = threading.Lock()  # kurul paralel çalıştığında dosya yazımı karışmasın


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
        row = {"t": round(time.time(), 1), "event": event, **data}
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
