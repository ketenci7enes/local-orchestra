"""MCP medya araçları + run_tests güvenlik kapısı testleri."""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
import media
from mcp_server import (
    orchestra_ocr_status,
    orchestra_read_file,
    orchestra_read_media,
    orchestra_run_tests,
    validate_test_command,
)
from mcp.types import CallToolResult


@pytest.fixture()
def project():
    root = Path(tempfile.mkdtemp(prefix="orch_media_"))
    (root / "docs").mkdir()
    (root / "docs" / "not.txt").write_text("Merhaba medya testi\nikinci satır\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n",
                                               encoding="utf-8")
    original = mcp_server.WORKSPACE
    mcp_server.WORKSPACE = root
    try:
        yield root
    finally:
        mcp_server.WORKSPACE = original


# ---------------------------------------------------------------- read_media
def test_read_media_text(project):
    out = asyncio.run(orchestra_read_media("docs/not.txt"))
    assert "Merhaba medya testi" in out
    assert "metin katmanı" in out


def test_read_media_structured(project):
    res = asyncio.run(orchestra_read_media("docs/not.txt", structured=True))
    assert isinstance(res, CallToolResult)
    assert res.structuredContent["ok"] is True
    assert "Merhaba" in res.structuredContent["text"]


def test_read_media_missing(project):
    out = asyncio.run(orchestra_read_media("docs/yok.txt"))
    assert "bulunamadı" in out.lower()


def test_read_media_secret_denied(project):
    (project / ".env").write_text("KEY=1", encoding="utf-8")
    out = asyncio.run(orchestra_read_media(".env"))
    assert "Erişim engellendi" in out


def test_read_media_absolute_outside_denied(project):
    outside = Path(tempfile.mkdtemp(prefix="orch_dis_")) / "x.txt"
    outside.write_text("gizli", encoding="utf-8")
    out = asyncio.run(orchestra_read_media(str(outside)))
    assert "Erişim engellendi" in out


def test_read_media_max_chars(project):
    (project / "docs" / "big.txt").write_text("a" * 5000, encoding="utf-8")
    res = asyncio.run(orchestra_read_media("docs/big.txt", max_chars=100, structured=True))
    assert res.structuredContent["truncated"] is True
    assert len(res.structuredContent["text"]) <= 100


def test_read_file_delegates_pdf(project):
    if not (sys.platform == "win32" and media.ocr_status()["ok"]):
        pytest.skip("Windows OCR yok")
    from PIL import Image

    p = project / "docs" / "img.pdf"
    Image.new("RGB", (300, 200), "white").save(p, "PDF")
    out = asyncio.run(orchestra_read_file("docs/img.pdf"))
    assert "img.pdf" in out


def test_ocr_status_tool():
    out = asyncio.run(orchestra_ocr_status())
    assert "Windows OCR" in out


# ---------------------------------------------------------------- run_tests
def test_validate_command_allows_pytest():
    assert validate_test_command([sys.executable, "-m", "pytest"]) == ""
    assert validate_test_command(["pytest", "-q"]) == ""
    assert validate_test_command(["npm", "test"]) == ""


def test_validate_command_rejects_unknown():
    assert "izinli listede değil" in validate_test_command(["evil_binary", "x"])


def test_validate_command_rejects_shell_operators():
    assert "güvenlik dışı" in validate_test_command(["pytest", "&&", "rm", "-rf", "/"])
    assert "güvenlik dışı" in validate_test_command(["pytest", "|", "curl", "http://x"])


def test_run_tests_auto_detects_pytest(project):
    out = asyncio.run(orchestra_run_tests())
    assert "Testler geçti" in out


def test_run_tests_rejects_dangerous_command(project):
    out = asyncio.run(orchestra_run_tests(command="pytest && del /f important.txt"))
    assert "reddedildi" in out.lower()


def test_run_tests_reports_failure(project):
    (project / "tests" / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8")
    out = asyncio.run(orchestra_run_tests())
    assert "başarısız" in out.lower() or "failed" in out.lower()
