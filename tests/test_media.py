"""Medya metin çıkarma testleri (media.py)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import media  # noqa: E402


def test_text_file_utf8(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("Merhaba dünya\nikinci satır", encoding="utf-8")
    res = media.extract_sync(p)
    assert res["ok"] and res["kind"] == "text"
    assert "Merhaba dünya" in res["text"]
    assert res["chars"] > 0


def test_text_file_cp1254(tmp_path):
    p = tmp_path / "tr.txt"
    p.write_bytes("Şğüöç İI".encode("cp1254"))
    res = media.extract_sync(p)
    assert res["ok"]
    assert "Şğüöç" in res["text"]


def test_missing_file(tmp_path):
    res = media.extract_sync(tmp_path / "yok.pdf")
    assert res["ok"] is False
    assert "bulunamadı" in res["error"].lower()


def test_unsupported_extension(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\x00\x01")
    res = media.extract_sync(p)
    assert res["ok"] is False
    assert "Desteklenmeyen" in res["error"]


def test_invalid_mode(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    res = media.extract_sync(p, mode="yanlis")
    assert res["ok"] is False


def test_max_chars_truncation(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("a" * 5000, encoding="utf-8")
    res = media.extract_sync(p, max_chars=100)
    assert res["ok"]
    assert len(res["text"]) <= 100
    assert res["truncated"] is True


def test_cache(tmp_path):
    media.clear_cache()
    p = tmp_path / "c.txt"
    p.write_text("önbellek testi", encoding="utf-8")
    first = media.extract_sync(p)
    second = media.extract_sync(p)
    assert first["cached"] is False
    assert second["cached"] is True
    media.clear_cache()


def test_summary_missing():
    txt = media.summarize({"ok": False, "error": "bozuk"})
    assert "bozuk" in txt


# -------------------------------------------------------------------------
# OCR (Windows'ta ve dil paketi varsa çalışır, aksi halde atlanır)
# -------------------------------------------------------------------------
def _ocr_ready() -> bool:
    return sys.platform == "win32" and media.ocr_status()["ok"]


def test_ocr_status_shape():
    st = media.ocr_status()
    assert {"ok", "langs", "error"} <= set(st)


@pytest.mark.skipif(not _ocr_ready(), reason="Windows OCR / dil paketi yok")
def test_image_ocr(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1200, 300), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=64)
    except TypeError:  # eski Pillow
        font = ImageFont.load_default()
    d.text((40, 100), "ORCHESTRA OCR TEST 2468", fill="black", font=font)
    p = tmp_path / "ocr.png"
    img.save(p)

    res = media.extract_sync(p, languages="en")
    assert res["ok"] and res["kind"] == "image" and res["method"] == "ocr"
    assert "ORCHESTRA" in res["text"].upper()


@pytest.mark.skipif(not _ocr_ready(), reason="Windows OCR / dil paketi yok")
def test_image_pdf_falls_back_to_ocr(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1200, 400), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=56)
    except TypeError:
        font = ImageFont.load_default()
    d.text((50, 150), "TARANMIS PDF TEST 1357", fill="black", font=font)
    p = tmp_path / "scan.pdf"
    img.save(p, "PDF", resolution=150.0)

    res = media.extract_sync(p, languages="en")
    assert res["ok"] and res["kind"] == "pdf"
    assert res["method"] in ("ocr", "mixed")
    assert res["pages"] == 1


def test_pdf_page_range_validation(tmp_path):
    """page_from toplam sayfayı aşarsa anlaşılır hata."""
    if not _ocr_ready():
        pytest.skip("pypdfium2 render + OCR yok")
    from PIL import Image

    p = tmp_path / "one.pdf"
    Image.new("RGB", (200, 200), "white").save(p, "PDF")
    res = media.extract_sync(p, page_from=5)
    assert res["ok"] is False
    assert "aşıyor" in res["error"]


def test_text_mode_without_text_layer(tmp_path):
    """mode=text: görsel PDF'te metin katmanı yoksa uyarı verir."""
    from PIL import Image

    p = tmp_path / "img.pdf"
    Image.new("RGB", (300, 200), "white").save(p, "PDF")
    res = media.extract_sync(p, mode="text")
    assert res["ok"] is True
    assert any("metin katmanı yok" in w for w in res["warnings"])
