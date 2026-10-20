"""Görevi sınıflandırır: tip, karmaşıklık, ilgili dosyalar (ucuz/hızlı model)."""
from llm import LLMError, parse_json
from tracker import tracker

TYPES = {"code", "file_task", "qa", "writing"}

SYSTEM = """Kullanıcının görevini sınıflandır. SADECE JSON döndür:
{"type": "code|file_task|qa|writing", "complexity": "simple|complex", "files": ["göreli/yol"]}
- code: kod yazma / değiştirme / hata ayıklama
- file_task: kod olmayan dosya düzenleme veya dönüştürme
- qa: soru-cevap, açıklama, araştırma özeti
- writing: metin, e-posta, rapor, çeviri
- complexity=complex: birden çok dosya, mimari karar veya belirsiz gereksinim
- files: yalnızca verilen dosya listesinden, görevle ilgili en fazla 8 yol; emin değilsen boş bırak"""

DEFAULT = {"type": "qa", "complexity": "simple", "files": []}


def classify(llm, goal: str, rmap: list) -> dict:
    listing = "\n".join(rmap[:150])
    msgs = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"Dosya listesi:\n{listing}\n\nGörev: {goal}"},
    ]
    try:
        data = parse_json(llm.chat("router", msgs, json_mode=True))
    except (LLMError, ValueError) as e:
        tracker.log("router_failed", error=str(e))
        return dict(DEFAULT)
    if data.get("type") not in TYPES:
        data["type"] = DEFAULT["type"]
    if data.get("complexity") not in ("simple", "complex"):
        data["complexity"] = "simple"
    files = data.get("files")
    data["files"] = [f for f in files if isinstance(f, str)][:8] if isinstance(files, list) else []
    return {"type": data["type"], "complexity": data["complexity"], "files": data["files"]}
