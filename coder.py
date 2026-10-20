"""Alt coder: yapılandırılmış (FILE / EDIT) çıktı üretir; ayrıştırma ve bellekte uygulama burada."""
import re
from dataclasses import dataclass
from pathlib import Path

from workspace import safe_path

SYSTEM = """Sen titiz bir alt programcısın. Verilen PLAN'ı uygula.
KURALLAR:
- Mevcut bir dosyayı DEĞİŞTİRMEK için EDIT bloğu kullan. SEARCH metni dosyada BİREBİR ve TEK kez geçmeli.
- Yeni dosya (veya tamamen yeniden yazılacak küçük dosya) için FILE bloğu kullan.
- Blokların dışında hiçbir açıklama yazma. Yalnızca göreli yollar kullan.
- Kodları eksiksiz tamamla, asla yarıda bırakma ve her bloğu mutlaka <<<END>>> ile kapat.
- Çok dosyalı görevde öncelik sırası: önce zorunlu/kritik dosyaları eksiksiz yaz; bağlam riski
  varsa en önemlilere öncelik ver ve her bloğu MUTLAKA <<<END>>> ile kapat.
- Güvenlik kurallarına tam uy: eval() veya new Function() gibi güvensiz kod yürütme mekanizmalarını asla kullanma.
BİÇİM:
<<<EDIT: yol/dosya.py>>>
<<<SEARCH>>>
eski kod
<<<REPLACE>>>
yeni kod
<<<END>>>

<<<FILE: yol/yeni.py>>>
tam dosya içeriği
<<<END>>>"""

FILE_RE = re.compile(r"<{1,3}FILE:\s*(?P<path>[^\n>]+?)\s*>{1,3}\s*\n(?P<body>.*?)\n?<{1,3}END>{1,3}", re.S)
EDIT_RE = re.compile(
    r"<{1,3}EDIT:\s*(?P<path>[^\n>]+?)\s*>{1,3}\s*\n<{1,3}SEARCH>{1,3}\s*\n(?P<search>.*?)\n<{1,3}REPLACE>{1,3}\s*\n(?P<replace>.*?)\n?<{1,3}END>{1,3}",
    re.S,
)


class ParseError(Exception):
    pass


class ApplyError(Exception):
    pass


@dataclass
class Change:
    path: str
    kind: str  # "file" | "edit"
    content: str = ""
    search: str = ""
    replace: str = ""


def generate(llm, plan_text: str, files_ctx: str, feedback: str = "") -> str:
    user = f"PLAN:\n{plan_text}\n\nMEVCUT DOSYALAR:\n{files_ctx or '(yok)'}"
    if feedback:
        user += f"\n\nÖNCEKİ DENEME BAŞARISIZ OLDU. Aşağıdaki sorunu düzelt:\n{feedback}"
    return llm.chat("coder", [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}])


def parse_changes(raw: str) -> list:
    found = []
    for m in FILE_RE.finditer(raw):
        found.append((m.start(), Change(m["path"].strip(), "file", content=m["body"])))
    for m in EDIT_RE.finditer(raw):
        found.append((m.start(), Change(m["path"].strip(), "edit", search=m["search"], replace=m["replace"])))
    if not found:
        raise ParseError("Yanıtta geçerli <<<FILE>>> veya <<<EDIT>>> bloğu yok")
    return [c for _, c in sorted(found, key=lambda x: x[0])]


def apply_in_memory(root: Path, changes: list) -> dict:
    """Diske YAZMADAN yeni içerikleri hesaplar: {göreli_yol: yeni_içerik}."""
    root = root.resolve()
    staged = {}
    for ch in changes:
        p = safe_path(root, ch.path)
        rel = p.relative_to(root).as_posix()
        if ch.kind == "file":
            staged[rel] = ch.content.rstrip("\n") + "\n"
            continue
        if rel in staged:
            base = staged[rel]
        elif p.is_file():
            base = p.read_text(encoding="utf-8")
        else:
            raise ApplyError(f"{rel}: EDIT için dosya yok (yeni dosya için FILE bloğu kullan)")
        n = base.count(ch.search)
        if n != 1:
            raise ApplyError(f"{rel}: SEARCH metni {n} kez bulundu (tam 1 olmalı)")
        staged[rel] = base.replace(ch.search, ch.replace, 1)
    return staged
