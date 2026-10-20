"""Beyin model: karmaşık görevler için alternatif stratejiler üretir."""
from llm import LLMError, parse_json

SYSTEM = """Sen kıdemli bir yazılım mimarısın. Görevi ve bağlamı analiz et, birbirinden FARKLI 3 uygulama stratejisi üret.
Her strateji: id (1-3), strategy (tek cümle), details (adım adım plan; hangi dosyalar değişecek), risks (kısa).
SADECE şu JSON'u döndür: {"options": [{"id": 1, "strategy": "...", "details": "...", "risks": "..."}]}"""


def generate_options(llm, goal: str, context: str) -> list:
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Bağlam:\n{context}\n\nGörev: {goal}"},
    ]
    for _ in range(2):
        try:
            raw = llm.chat("brain", msgs, json_mode=True)
        except LLMError:
            return []
        try:
            opts = parse_json(raw)["options"]
            opts = [o for o in opts if isinstance(o, dict) and o.get("strategy")]
            if opts:
                for i, o in enumerate(opts[:3], 1):
                    o["id"] = i
                return opts[:3]
            err = "options boş"
        except (ValueError, KeyError, TypeError) as e:
            err = str(e)
        msgs += [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": f"Çıktı geçersiz ({err}). Yalnızca belirtilen JSON şemasını döndür."},
        ]
    return []
