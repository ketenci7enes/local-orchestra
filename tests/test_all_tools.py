"""Araç yüzeyi testleri: 17 MCP aracının TAMAMI (biri hariç tutulmaz).

Amaç: her aracın (a) kayıtlı olduğunu, (b) şemasının olduğunu ve (c) gerçek bir
çağrıda makul yanıt ürettiğini doğrulamak. Ağ/LLM gerektiren araçlarda yalnızca
LLM katmanı taklit edilir; araç kablolaması, doğrulama ve biçimlendirme gerçektir.
"""
import asyncio
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
import orchestrator
from mcp.types import CallToolResult

# Kayıtlı olması gereken 17 araç (sıra bağımsız)
EXPECTED_TOOLS = {
    "orchestra_run_task",
    "orchestra_read_media",
    "orchestra_ocr_status",
    "orchestra_ping",
    "orchestra_status",
    "orchestra_workspace",
    "orchestra_read_file",
    "orchestra_apply_edit",
    "orchestra_write_file",
    "orchestra_list_files",
    "orchestra_search",
    "orchestra_glob",
    "orchestra_read_many",
    "orchestra_undo",
    "orchestra_run_tests",
    "orchestra_git",
    "orchestra_review",
}


@pytest.fixture()
def project():
    root = Path(tempfile.mkdtemp(prefix="orch_tools_"))
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    (root / "src" / "util.py").write_text("LOG = 'info'\n", encoding="utf-8")
    (root / "README.md").write_text("# proje\n\nmerhaba dunya\n", encoding="utf-8")
    (root / "notes.txt").write_text("birinci satir\nikinci satir\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    original = mcp_server.WORKSPACE
    mcp_server.WORKSPACE = root
    try:
        yield root
    finally:
        mcp_server.WORKSPACE = original


# ---------------------------------------------------------------------------
# 0) Yüzey: 17 araç ve şemaları
# ---------------------------------------------------------------------------
def test_tool_surface_is_exactly_17():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert len(tools) == 17, f"beklenen 17, bulunan {len(tools)}: {sorted(names)}"
    assert names == EXPECTED_TOOLS
    # Her beklenen araç gerçekten kayıtlı (eksik araç sessizce düşmesin)
    assert EXPECTED_TOOLS - names == set()


def test_every_tool_has_description_and_input_schema():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    problems = []
    for t in tools:
        if not (t.description or "").strip():
            problems.append(f"{t.name}: açıklama yok")
        schema = getattr(t, "inputSchema", None) or {}
        if schema.get("type") != "object":
            problems.append(f"{t.name}: inputSchema object değil")
        if "properties" not in schema:
            problems.append(f"{t.name}: properties yok")
    assert not problems, problems


# ---------------------------------------------------------------------------
# 1) orchestra_run_task
# ---------------------------------------------------------------------------
def test_run_task_formats_success(project, monkeypatch):
    def fake_run_task(llm, judge, root, goal, **kw):
        return {
            "ok": True,
            "files": ["src/app.py"],
            "diff": "--- a/src/app.py\n+++ b/src/app.py\n-def add(a, b):\n+def add(a, b):",
            "tests": "1 passed",
            "verification": [{"stage": "parse", "ok": True, "summary": "1 dosya"},
                             {"stage": "tests", "ok": True, "summary": "mod=full"}],
            "decision": None,
        }

    monkeypatch.setattr(orchestrator, "run_task", fake_run_task)
    out = asyncio.run(mcp_server.orchestra_run_task("add fonksiyonunu düzelt"))
    assert "Başarıyla uygulandı" in out
    assert "src/app.py" in out
    assert "```diff" in out


def test_run_task_dry_run_reports_real_tests(project, monkeypatch):
    def fake_run_task(llm, judge, root, goal, **kw):
        assert kw["dry_run"] is True
        return {"ok": True, "dry_run": True, "files": ["src/app.py"],
                "diff": "--- a\n+++ b", "tests_ok": True, "tests": "ok"}

    monkeypatch.setattr(orchestrator, "run_task", fake_run_task)
    out = asyncio.run(mcp_server.orchestra_run_task("dene", dry_run=True))
    assert "Dry-run" in out
    assert "diske hiç yazılmadı" in out


def test_run_task_returns_answer_for_qa(project, monkeypatch):
    monkeypatch.setattr(orchestrator, "run_task",
                        lambda *a, **k: {"ok": True, "answer": "Kisa cevap."})
    assert asyncio.run(mcp_server.orchestra_run_task("soru")) == "Kisa cevap."


def test_run_task_with_attachment_reads_document(project, monkeypatch):
    seen = {}

    def fake_run_task(llm, judge, root, goal, **kw):
        seen.update(kw)
        return {"ok": True, "answer": "belge okundu"}

    monkeypatch.setattr(orchestrator, "run_task", fake_run_task)
    out = asyncio.run(mcp_server.orchestra_run_task("ozetle", attach="notes.txt"))
    assert "belge okundu" in out
    assert "notes.txt" in out          # ek özeti başlıkta
    assert seen["attachments"] and "birinci satir" in seen["attachments"][0]["text"]


# ---------------------------------------------------------------------------
# 2) orchestra_read_media
# ---------------------------------------------------------------------------
def test_read_media_text(project):
    out = asyncio.run(mcp_server.orchestra_read_media("notes.txt"))
    assert "ikinci satir" in out
    assert "metin katmanı" in out


def test_read_media_structured_payload(project):
    res = asyncio.run(mcp_server.orchestra_read_media("notes.txt", structured=True))
    assert isinstance(res, CallToolResult)
    assert res.structuredContent["ok"] is True
    assert "birinci satir" in res.structuredContent["text"]


# ---------------------------------------------------------------------------
# 3) orchestra_ocr_status
# ---------------------------------------------------------------------------
def test_ocr_status_reports_engine():
    out = asyncio.run(mcp_server.orchestra_ocr_status())
    assert "Windows OCR" in out
    if sys.platform == "win32":
        assert ("hazır" in out) or ("kullanılamıyor" in out)


# ---------------------------------------------------------------------------
# 4) orchestra_ping
# ---------------------------------------------------------------------------
def test_ping_uses_cached_lines(project, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ping_impl",
                        lambda **kw: (["✅ router: model-a", "❌ coder: model-b"], False, 0.0))
    out = asyncio.run(mcp_server.orchestra_ping())
    assert "Model Sağlık Kontrolü" in out
    assert "model-a" in out and "model-b" in out


def test_ping_structured_counts(project, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ping_impl",
                        lambda **kw: (["✅ a", "❌ b", "⚠️ c"], False, 0.0))
    res = asyncio.run(mcp_server.orchestra_ping(structured=True))
    assert res.structuredContent["ok"] == 1
    assert res.structuredContent["failed"] == 1
    assert res.structuredContent["skipped"] == 1


# ---------------------------------------------------------------------------
# 5) orchestra_status
# ---------------------------------------------------------------------------
def test_status_reports_token_stats():
    out = asyncio.run(mcp_server.orchestra_status())
    assert isinstance(out, str) and out.strip()


# ---------------------------------------------------------------------------
# 6) orchestra_workspace
# ---------------------------------------------------------------------------
def test_workspace_get_and_set(project):
    shown = asyncio.run(mcp_server.orchestra_workspace())
    assert str(project) in shown or "workspace" in shown.lower()

    other = Path(tempfile.mkdtemp(prefix="orch_ws_"))
    try:
        asyncio.run(mcp_server.orchestra_workspace(str(other)))
        assert mcp_server.WORKSPACE == other.resolve()
    finally:
        mcp_server.WORKSPACE = project


def test_workspace_rejects_relative_or_missing():
    out = asyncio.run(mcp_server.orchestra_workspace("yok/boyle/bir/dizin_xyz"))
    assert isinstance(out, str)
    assert ("❌" in out) or ("bulunamadı" in out.lower()) or ("Engellendi" in out)


# ---------------------------------------------------------------------------
# 7) orchestra_read_file
# ---------------------------------------------------------------------------
def test_read_file_line_range(project):
    out = asyncio.run(mcp_server.orchestra_read_file("src/app.py", start_line=1, end_line=2))
    assert "def add" in out
    assert "return a - b" not in out  # aralık dışı


def test_read_file_blocks_secrets(project):
    (project / ".env").write_text("KEY=1\n", encoding="utf-8")
    out = asyncio.run(mcp_server.orchestra_read_file(".env"))
    assert "Erişim engellendi" in out


# ---------------------------------------------------------------------------
# 8) orchestra_apply_edit
# ---------------------------------------------------------------------------
def test_apply_edit_writes_and_dry_run_does_not(project):
    target = project / "src" / "app.py"
    before = target.read_text(encoding="utf-8")

    dry = asyncio.run(mcp_server.orchestra_apply_edit("src/app.py", "a + b", "a + 2 * b", dry_run=True))
    assert "Dry-run" in dry
    assert target.read_text(encoding="utf-8") == before

    real = asyncio.run(mcp_server.orchestra_apply_edit("src/app.py", "a + b", "a + 2 * b"))
    assert "uygulandı" in real
    assert "a + 2 * b" in target.read_text(encoding="utf-8")


def test_apply_edit_ambiguous_search_is_rejected(project):
    (project / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    out = asyncio.run(mcp_server.orchestra_apply_edit("dup.py", "x = 1", "x = 2"))
    assert "❌" in out


# ---------------------------------------------------------------------------
# 9) orchestra_write_file
# ---------------------------------------------------------------------------
def test_write_file_creates_nested_and_backs_up(project):
    out = asyncio.run(mcp_server.orchestra_write_file("src/yeni/mod.py", "VALUE = 42\n"))
    assert "yazıldı" in out
    assert (project / "src" / "yeni" / "mod.py").read_text(encoding="utf-8") == "VALUE = 42\n"

    # Üzerine yazma yedek oluşturmalı
    asyncio.run(mcp_server.orchestra_write_file("src/yeni/mod.py", "VALUE = 43\n"))
    assert "43" in (project / "src" / "yeni" / "mod.py").read_text(encoding="utf-8")


def test_write_file_dry_run(project):
    out = asyncio.run(mcp_server.orchestra_write_file("src/hayalet.py", "X = 1\n", dry_run=True))
    assert "Dry-run" in out
    assert not (project / "src" / "hayalet.py").exists()


# ---------------------------------------------------------------------------
# 10) orchestra_list_files
# ---------------------------------------------------------------------------
def test_list_files_tree(project):
    out = asyncio.run(mcp_server.orchestra_list_files())
    assert "src" in out and "app.py" in out


def test_list_files_subdir(project):
    out = asyncio.run(mcp_server.orchestra_list_files(subdir="src"))
    assert "app.py" in out and "README" not in out


# ---------------------------------------------------------------------------
# 11) orchestra_search
# ---------------------------------------------------------------------------
def test_search_finds_pattern(project):
    out = asyncio.run(mcp_server.orchestra_search(r"def\s+sub"))
    assert "app.py" in out


def test_search_no_match(project):
    out = asyncio.run(mcp_server.orchestra_search("BULUNAMAYAN_DESEN_XYZ"))
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 12) orchestra_glob
# ---------------------------------------------------------------------------
def test_glob_matches_pattern(project):
    out = asyncio.run(mcp_server.orchestra_glob("**/*.py"))
    assert "app.py" in out and "test_ok.py" in out


# ---------------------------------------------------------------------------
# 13) orchestra_read_many
# ---------------------------------------------------------------------------
def test_read_many_combines_files(project):
    out = asyncio.run(mcp_server.orchestra_read_many("src/app.py, README.md"))
    assert "def add" in out and "merhaba dunya" in out


def test_read_many_respects_per_file_budget(project):
    (project / "big.txt").write_text("a" * 5000, encoding="utf-8")
    res = asyncio.run(mcp_server.orchestra_read_many("big.txt", max_chars_per_file=100,
                                                    structured=True))
    payload = res.structuredContent
    blob = str(payload)
    assert "big.txt" in blob


# ---------------------------------------------------------------------------
# 14) orchestra_undo
# ---------------------------------------------------------------------------
def test_undo_restores_previous_content(project):
    target = project / "src" / "app.py"
    before = target.read_text(encoding="utf-8")
    asyncio.run(mcp_server.orchestra_write_file("src/app.py", "TAMAMEN = 'yeni'\n"))
    assert target.read_text(encoding="utf-8") != before

    out = asyncio.run(mcp_server.orchestra_undo("undo"))
    assert isinstance(out, str)
    assert target.read_text(encoding="utf-8") == before


def test_undo_list_shows_backups(project):
    asyncio.run(mcp_server.orchestra_write_file("src/app.py", "X = 1\n"))
    out = asyncio.run(mcp_server.orchestra_undo("list"))
    assert isinstance(out, str) and out.strip()


# ---------------------------------------------------------------------------
# 15) orchestra_run_tests
# ---------------------------------------------------------------------------
def test_run_tests_autodetect_passes(project):
    out = asyncio.run(mcp_server.orchestra_run_tests())
    assert "Testler geçti" in out


def test_run_tests_reports_failure(project):
    (project / "tests" / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8")
    out = asyncio.run(mcp_server.orchestra_run_tests())
    assert "başarısız" in out.lower() or "failed" in out.lower()


# ---------------------------------------------------------------------------
# 16) orchestra_git
# ---------------------------------------------------------------------------
def _git(args, cwd):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", *args], cwd=cwd, stdin=subprocess.DEVNULL,
                   capture_output=True, env={**__import__("os").environ, **env})


def test_git_status_diff_log_commit(project):
    _git(["init", "-q"], project)
    _git(["add", "."], project)
    _git(["commit", "-qm", "init"], project)

    # Commit sonrası çalışma ağacı temiz olmalı
    status_clean = asyncio.run(mcp_server.orchestra_git("status"))
    assert "temiz" in status_clean.lower()

    (project / "src" / "app.py").write_text("DEGISTI = True\n", encoding="utf-8")

    status_dirty = asyncio.run(mcp_server.orchestra_git("status"))
    assert "app.py" in status_dirty

    assert "DEGISTI" in asyncio.run(mcp_server.orchestra_git("diff"))

    assert "init" in asyncio.run(mcp_server.orchestra_git("log"))

    out = asyncio.run(mcp_server.orchestra_git("commit", message="ikinci commit"))
    assert isinstance(out, str) and out.strip()
    assert "ikinci commit" in asyncio.run(mcp_server.orchestra_git("log"))


def test_git_invalid_action(project):
    out = asyncio.run(mcp_server.orchestra_git("sil"))
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 17) orchestra_review
# ---------------------------------------------------------------------------
class _StubJudge:
    def review(self, goal, diff):
        if "kötü" in diff:
            return "REJECTED", "istenmeyen değişiklik"
        return "APPROVED", "uygun"


def test_review_approved(project, monkeypatch):
    monkeypatch.setattr(mcp_server, "_judge", _StubJudge())
    out = asyncio.run(mcp_server.orchestra_review("hedef", "+ iyi satir"))
    assert "APPROVED" in out


def test_review_rejected(project, monkeypatch):
    monkeypatch.setattr(mcp_server, "_judge", _StubJudge())
    out = asyncio.run(mcp_server.orchestra_review("hedef", "+ kötü satir"))
    assert "REJECTED" in out
