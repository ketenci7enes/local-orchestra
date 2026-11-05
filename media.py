"""Medya metin çıkarma: düz metin, dijital PDF (metin katmanı), taranmış PDF/görsel (Windows OCR).

Tasarım notları
---------------
* PDF metin katmanı `pypdfium2` (libpdfium) ile okunur — dijital PDF'lerde OCR'dan
  hem çok daha hızlı hem hatasızdır.
* Metin katmanı olmayan sayfalar (taranmış/fotoğraf) `Windows.Media.Ocr` ile
  OCR'lanır. Bu motor Windows 10/11'e gömülüdür: ücretsiz, çevrimdışı, API
  anahtarı gerektirmez. Python bağlantısı `winocr` + `winrt-*` (PyWinRT).
* Karar **sayfa bazında** verilir (auto): metin varsa metin, yoksa OCR. Böylece
  karışık belgelerde (kapak taranmış, geri kalanı dijital) en iyi sonuç alınır.
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import threading
import time
from collections import OrderedDict
from pathlib import Path

# -----------------------------------------------------------------------------
# Sabitler
# -----------------------------------------------------------------------------
TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".htm", ".css", ".scss",
    ".sh", ".ps1", ".bat", ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs",
    ".xml", ".sql", ".env.example",
}
PDF_EXT = {".pdf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}

# Bir sayfada bu kadar karakterden az metin varsa "metin katmanı yok" sayılır.
MIN_TEXT_CHARS = 20
DEFAULT_DPI = 200
MAX_IMAGE_PIXELS = 40_000_000  # ~40 MP: bellek koruması


class MediaError(Exception):
    """Kullanıcıya gösterilebilir medya hatası."""


# -----------------------------------------------------------------------------
# OCR (Windows.Media.Ocr)
# -----------------------------------------------------------------------------
_OCR_LOCK = threading.Lock()
_OCR_STATE: dict = {"checked": False, "ok": False, "error": "", "langs": [], "chosen": ""}


def ocr_status(force: bool = False) -> dict:
    """OCR motorunun kullanılabilirliğini döndürür (ilk çağrıda yoklar, sonra önbellek)."""
    if _OCR_STATE["checked"] and not force:
        return dict(_OCR_STATE)
    _OCR_STATE.update(checked=True)
    if sys.platform != "win32":
        _OCR_STATE.update(ok=False, error="Windows OCR yalnızca Windows'ta kullanılabilir")
        return dict(_OCR_STATE)
    try:
        from winrt.windows.media.ocr import OcrEngine

        tags = [L.language_tag for L in OcrEngine.available_recognizer_languages]
        _OCR_STATE.update(ok=bool(tags), langs=tags)
        if not tags:
            _OCR_STATE["error"] = (
                "Windows OCR dil paketi bulunamadı. Kurulum (PowerShell, yönetici):\n"
                '  Add-WindowsCapability -Online -Name "Language.OCR~~~tr-TR~0.0.1.0"\n'
                '  Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"'
            )
        else:
            _OCR_STATE["error"] = ""
            _OCR_STATE["chosen"] = _pick_language(tags, "")
    except Exception as e:  # winrt kurulu değil / import hatası
        _OCR_STATE.update(ok=False, error=f"OCR motoru yüklenemedi: {type(e).__name__}: {e}")
    return dict(_OCR_STATE)


def _pick_language(available: list, requested: str) -> str:
    """İstenen dilleri (virgülle) mevcutlarla eşleştir; yoksa ilk mevcut dili döndür."""
    req = [s.strip().lower() for s in str(requested or "").replace(";", ",").split(",") if s.strip()]
    for want in req:
        for tag in available:
            t = tag.lower()
            if t == want or t.split("-")[0] == want.split("-")[0]:
                return tag
    # Kullanıcı profili dilleri (Türkçe varsa onu tercih et)
    for tag in available:
        if tag.lower().startswith("tr"):
            return tag
    return available[0] if available else ""


def _ocr_pil_sync(img, lang: str):
    """PIL görselini Windows OCR'dan geçirir (senkron; kendi event loop'unda çalışır)."""
    try:
        import winocr
    except Exception as e:  # pragma: no cover - ortam hatası
        raise MediaError(f"winocr yüklenemedi: {e}") from e

    async def _run():
        return await winocr.recognize_pil(img, lang)

    try:
        return asyncio.run(_run())
    except RuntimeError as e:
        # Zaten çalışan bir loop varsa (beklenmez: thread'de çağrılıyor)
        if "event loop" not in str(e).lower():
            raise
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()


def _ocr_image(img, lang: str = "", scale: float = 1.0) -> tuple:
    """Görseli OCR'lar; (metin, kullanılan_dil) döndürür."""
    from PIL import Image  # yerel import: Pillow yoksa net hata

    if scale and scale != 1.0:
        new_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        img = img.resize(new_size, Image.LANCZOS)
    if img.width * img.height > MAX_IMAGE_PIXELS:
        raise MediaError(f"Görsel çok büyük: {img.width}x{img.height}")
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")

    st = ocr_status()
    if not st["ok"]:
        raise MediaError(st["error"] or "OCR kullanılamıyor")
    chosen = _pick_language(st["langs"], lang or "")
    if not chosen:
        raise MediaError("Uygun OCR dili bulunamadı")
    # Windows OCR en az ~40 px yükseklik ister
    if img.height < 40:
        factor = 40 / max(1, img.height)
        img = img.resize((max(1, int(img.width * factor)), 40), Image.LANCZOS)
    with _OCR_LOCK:  # WinRT nesneleri iş parçacıkları arasında paylaşılmamalı
        res = _ocr_pil_sync(img, chosen)
    return (getattr(res, "text", "") or "").strip(), chosen


# -----------------------------------------------------------------------------
# PDF
# -----------------------------------------------------------------------------
def _pdf_pages(path: Path, page_from: int, page_to: int, max_pages: int) -> tuple:
    """Açar ve sayfa aralığını doğrular: (doc, [0-tabanlı sayfa indeksleri])."""
    try:
        import pypdfium2 as pdfium
    except Exception as e:  # pragma: no cover
        raise MediaError(f"pypdfium2 yüklenemedi: {e}") from e
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception as e:
        raise MediaError(f"PDF açılamadı ({path.name}): {e}") from e
    try:
        total = len(doc)
    except Exception as e:
        doc.close()
        raise MediaError(f"PDF sayfa sayısı okunamadı: {e}") from e
    start = max(1, int(page_from or 1))
    end = total if not page_to else min(total, int(page_to))
    if start > total:
        doc.close()
        raise MediaError(f"page_from={start} toplam sayfayı ({total}) aşıyor")
    if end < start:
        doc.close()
        raise MediaError(f"Geçersiz aralık: {start}-{end}")
    idx = list(range(start - 1, end))
    if len(idx) > max_pages:
        idx = idx[:max_pages]
    return doc, idx, total


def _pdf_page_text(page) -> str:
    try:
        tp = page.get_textpage()
    except Exception:
        return ""
    try:
        return (tp.get_text_range() or "").replace("\r\n", "\n").replace("\r", "\n")
    except Exception:
        return ""
    finally:
        try:
            tp.close()
        except Exception:
            pass


def _pdf_page_render(page, dpi: int):
    scale = max(1.0, float(dpi)) / 72.0
    return page.render(scale=scale).to_pil()


# -----------------------------------------------------------------------------
# Düz metin
# -----------------------------------------------------------------------------
def _read_text_file(path: Path, max_chars: int) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1254", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - latin-1 her zaman çözer
        text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text[:max_chars]


# -----------------------------------------------------------------------------
# Önbellek
# -----------------------------------------------------------------------------
_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_MAX = 8


def _cache_key(path: Path, **kw) -> str:
    try:
        st = path.stat()
        sig = f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        sig = "?"
    blob = f"{path}|{sig}|" + "|".join(f"{k}={v}" for k, v in sorted(kw.items()))
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _cache_get(key: str):
    item = _CACHE.get(key)
    if item is None:
        return None
    _CACHE.move_to_end(key)
    return dict(item)


def _cache_put(key: str, value: dict) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def clear_cache() -> None:
    _CACHE.clear()


# -----------------------------------------------------------------------------
# Ana API
# -----------------------------------------------------------------------------
def extract_sync(path, *, mode: str = "auto", languages: str = "", page_from: int = 1,
                 page_to: int = 0, max_pages: int = 50, max_chars: int = 50_000,
                 dpi: int = DEFAULT_DPI, use_cache: bool = True) -> dict:
    """Dosyadan metin çıkarır (senkron; thread içinde çağrılmalı).

    mode: "auto" (metin katmanı varsa onu kullan) | "text" | "ocr"
    """
    started = time.time()
    path = Path(path)
    if mode not in ("auto", "text", "ocr"):
        return {"ok": False, "error": f"Geçersiz mod: {mode} (auto|text|ocr)", "path": str(path)}
    if not path.is_file():
        return {"ok": False, "error": f"Dosya bulunamadı: {path}", "path": str(path)}

    key = _cache_key(path, mode=mode, languages=languages, page_from=page_from,
                     page_to=page_to, max_pages=max_pages, max_chars=max_chars, dpi=dpi)
    if use_cache:
        hit = _cache_get(key)
        if hit is not None:
            hit["cached"] = True
            hit["elapsed_ms"] = int((time.time() - started) * 1000)
            return hit

    suffix = path.suffix.lower()
    base = {"ok": True, "path": str(path), "name": path.name, "size": path.stat().st_size}

    try:
        if suffix in TEXT_EXT:
            text = _read_text_file(path, max_chars)
            out = {**base, "kind": "text", "method": "text",
                   "pages": 1, "page_map": [], "text": text,
                   "chars": len(text), "truncated": len(text) >= max_chars,
                   "warnings": [], "languages": "", "total_pages": 1}
        elif suffix in PDF_EXT:
            out = _extract_pdf(path, base, mode=mode, languages=languages,
                               page_from=page_from, page_to=page_to,
                               max_pages=max_pages, max_chars=max_chars, dpi=dpi)
        elif suffix in IMAGE_EXT:
            out = _extract_image(path, base, mode=mode, languages=languages, max_chars=max_chars)
        elif mode == "ocr":
            out = _extract_image(path, base, mode=mode, languages=languages, max_chars=max_chars)
        else:
            raise MediaError(
                f"Desteklenmeyen uzantı: {suffix or '(yok)'}. "
                f"Metin: {', '.join(sorted(TEXT_EXT))[:120]}… | PDF | "
                f"Görsel: {', '.join(sorted(IMAGE_EXT))}")
    except MediaError as e:
        return {"ok": False, "error": str(e), "path": str(path), "name": path.name}
    except Exception as e:  # beklenmeyen: süreç ölmesin
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "path": str(path), "name": path.name}

    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["cached"] = False
    if use_cache and out.get("ok"):
        _cache_put(key, out)
    return out


def _extract_pdf(path: Path, base: dict, *, mode: str, languages: str,
                 page_from: int, page_to: int, max_pages: int,
                 max_chars: int, dpi: int) -> dict:
    doc, idx, total = _pdf_pages(path, page_from, page_to, max_pages)
    warnings = []
    chunks, page_map = [], []
    used = 0
    truncated = False
    langs_used = set()
    try:
        for i in idx:
            try:
                page = doc[i]
            except Exception as e:
                warnings.append(f"sayfa {i + 1}: açılamadı ({type(e).__name__})")
                continue
            text, method = "", ""
            if mode != "ocr":
                text = _pdf_page_text(page).strip()
                if len(text) >= MIN_TEXT_CHARS:
                    method = "text"
                elif mode == "text":
                    text = ""
            if mode == "text" and not text:
                warnings.append(f"sayfa {i + 1}: metin katmanı yok (mode=text)")
            if not text and mode != "text":
                try:
                    img = _pdf_page_render(page, dpi)
                    text, lang = _ocr_image(img, languages)
                    method = "ocr"
                    langs_used.add(lang)
                except MediaError as e:
                    warnings.append(f"sayfa {i + 1}: OCR başarısız — {e}")
                    text = ""
            try:
                page.close()
            except Exception:
                pass
            if not text:
                text = "[bu sayfada metin bulunamadı]"
            elif used + len(text) > max_chars:
                text = text[: max(0, max_chars - used)] + "\n[… karakter sınırı: kısaltıldı]"
                truncated = True
            used += len(text)
            chunks.append(f"--- sayfa {i + 1} ---\n{text}")
            page_map.append({"page": i + 1, "method": method, "chars": len(text)})
            if used >= max_chars:
                truncated = True
                if i + 1 < idx[-1]:
                    warnings.append(f"Karakter sınırı ({max_chars}) doldu; kalan sayfalar okunmadı")
                break
    finally:
        try:
            doc.close()
        except Exception:
            pass

    text = "\n\n".join(chunks)
    methods = {m["method"] for m in page_map if m["method"]}
    method = "text" if methods == {"text"} else ("ocr" if methods == {"ocr"} else ("mixed" if methods else "none"))
    return {**base, "kind": "pdf", "method": method, "pages": len(page_map),
            "total_pages": total, "page_map": page_map, "text": text,
            "chars": len(text), "truncated": truncated,
            "warnings": warnings, "languages": ",".join(sorted(langs_used))}


def _extract_image(path: Path, base: dict, *, mode: str, languages: str, max_chars: int) -> dict:
    try:
        from PIL import Image
    except Exception as e:  # pragma: no cover
        raise MediaError(f"Pillow yüklenemedi: {e}") from e
    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        raise MediaError(f"Görsel açılamadı: {e}") from e
    text, lang = _ocr_image(img, languages)
    truncated = len(text) > max_chars
    return {**base, "kind": "image", "method": "ocr", "pages": 1, "total_pages": 1,
            "page_map": [{"page": 1, "method": "ocr", "chars": len(text)}],
            "text": text[:max_chars], "chars": min(len(text), max_chars),
            "truncated": truncated, "warnings": [], "languages": lang}


async def extract_async(path, **kw) -> dict:
    """`extract_sync`'i iptal edilebilir bir iş parçacığında çalıştırır."""
    import anyio

    kw.setdefault("_TO_THREAD_KW", None)
    to_thread_kw = kw.pop("_TO_THREAD_KW", None) or {}
    return await anyio.to_thread.run_sync(
        lambda: extract_sync(path, **kw), **to_thread_kw
    )


def summarize(res: dict, max_text: int = 4000) -> str:
    """Kullanıcıya gösterilecek kısa özet metni üretir."""
    if not res.get("ok"):
        return f"❌ Medya okunamadı: {res.get('error', 'bilinmeyen hata')}"
    method_label = {"text": "metin katmanı", "ocr": "Windows OCR",
                    "mixed": "metin + OCR", "none": "metin yok"}.get(res.get("method"), res.get("method"))
    head = (f"📄 {res.get('name')} — {res.get('kind')} | "
            f"{res.get('pages')}/{res.get('total_pages')} sayfa | yöntem: {method_label} | "
            f"{res.get('chars')} karakter | {res.get('elapsed_ms')} ms")
    if res.get("cached"):
        head += " (önbellek)"
    if res.get("languages"):
        head += f" | dil: {res['languages']}"
    lines = [head]
    if res.get("truncated"):
        lines.append("⚠️ İçerik kısaltıldı (max_chars sınırı).")
    for w in (res.get("warnings") or [])[:8]:
        lines.append(f"⚠️ {w}")
    text = res.get("text") or ""
    if len(text) > max_text:
        lines.append(f"\n{text[:max_text]}\n\n[… metin kısaltıldı, toplam {len(text)} karakter]")
    else:
        lines.append("\n" + text)
    return "\n".join(lines)
