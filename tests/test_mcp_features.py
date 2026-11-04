"""Yeni MCP özellikleri: dry-run, undo, glob, read_many, gitignore, structured, ping cache."""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server
import workspace as ws
from mcp_server import (
    orchestra_apply_edit,
    orchestra_glob,
    orchestra_git,
    orchestra_list_files,
    orchestra_read_many,
    orchestra_search,
    orchestra_status,
    orchestra_undo,
    orchestra_workspace,
    orchestra_write_file,
    _ping_impl,
    _PING_CACHE,
)


@pytest.fixture()
def project():
    root = Path(tempfile.mkdtemp(prefix="orch_feat_"))
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "src" / "util.py").write_text("LOG = 'info'\n", encoding="utf-8")
    (root / "README.md").write_text("# proje\n", encoding="utf-8")
    (root / "build").mkdir()
    (root / "build" / "out.py").write_text("x = 1\n", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text("var a=1;\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n*.log\n", encoding="utf-8")
    (root / "debug.log").write_text("hata\n", encoding="utf-8")
    original = mcp_server.WORKSPACE
    mcp_server.WORKSPACE = root
    try:
        yield root
    finally:
        mcp_server.WORKSPACE = original
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------- dry_run
def test_write_file_dry_run_does_not_touch_disk(project):
    target = project / "src" / "new.py"
    result = asyncio.run(orchestra_write_file("src/new.py", "print(1)\n", dry_run=True))
    assert "Dry-run" in result
    assert not target.exists()


def test_apply_edit_dry_run_does_not_touch_disk(project):
    target = project / "src" / "app.py"
    before = target.read_text(encoding="utf-8")
    result = asyncio.run(orchestra_apply_edit("src/app.py", "a + b", "a - b", dry_run=True))
    assert "Dry-run" in result
    assert "+    return a - b" in result
    assert target.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------- undo
def test_undo_restores_modified_file(project):
    target = project / "src" / "app.py"
    before = target.read_text(encoding="utf-8")
    asyncio.run(orchestra_write_file("src/app.py", "TAMAMEN FARKLI\n"))
    assert target.read_text(encoding="utf-8") != before

    listing = asyncio.run(orchestra_undo("list"))
    assert "app.py" in listing

    result = asyncio.run(orchestra_undo("undo"))
    assert "✅" in result
    assert target.read_text(encoding="utf-8") == before


def test_undo_deletes_newly_created_file(project):
    asyncio.run(orchestra_write_file("src/gecici.py", "x = 1\n"))
    assert (project / "src" / "gecici.py").exists()
    result = asyncio.run(orchestra_undo("undo"))
    assert "✅" in result
    assert not (project / "src" / "gecici.py").exists()


def test_undo_without_backup_reports_error(project):
    # Bu proje için hiç yazma yapılmadı
    result = asyncio.run(orchestra_undo("undo"))
    assert "❌" in result


def test_undo_unknown_action(project):
    result = asyncio.run(orchestra_undo("sil"))
    assert "❌" in result


# ---------------------------------------------------------------- glob
def test_glob_py_files(project):
    result = asyncio.run(orchestra_glob("**/*.py"))
    assert "src/app.py" in result
    assert "src/util.py" in result
    # build/ .gitignore'da
    assert "build/out.py" not in result


def test_glob_respects_gitignore_toggle(project):
    with_ignore = asyncio.run(orchestra_glob("**/*.py", use_gitignore=True))
    without = asyncio.run(orchestra_glob("**/*.py", use_gitignore=False))
    assert "build/out.py" not in with_ignore
    assert "build/out.py" in without


def test_glob_single_level(project):
    result = asyncio.run(orchestra_glob("src/*.py"))
    assert "src/app.py" in result
    assert "src/util.py" in result


def test_glob_no_match(project):
    assert "(eşleşme yok)" in asyncio.run(orchestra_glob("**/*.rs"))


# ---------------------------------------------------------------- read_many
def test_read_many_comma_and_space(project):
    result = asyncio.run(orchestra_read_many("src/app.py, src/util.py README.md"))
    assert "def add" in result
    assert "LOG = 'info'" in result
    assert "# proje" in result


def test_read_many_json_list(project):
    result = asyncio.run(orchestra_read_many(json.dumps(["src/app.py", "src/util.py"])))
    assert "def add" in result and "LOG = 'info'" in result


def test_read_many_reports_missing_and_secret(project):
    result = asyncio.run(orchestra_read_many("yok.py .env"))
    assert "dosya yok" in result
    assert "Erişim engellendi" in result or "Gizli" in result


def test_read_many_budget(project):
    result = asyncio.run(orchestra_read_many("src/app.py, src/util.py", max_chars_per_file=5))
    assert len(result) < 500


def test_read_many_empty(project):
    assert "❌" in asyncio.run(orchestra_read_many(""))


# ---------------------------------------------------------------- gitignore
def test_list_files_respects_gitignore(project):
    listed = asyncio.run(orchestra_list_files())
    assert "src/app.py" in listed
    assert "debug.log" not in listed


def test_search_respects_gitignore(project):
    hit = asyncio.run(orchestra_search("hata"))
    assert "(eşleşme yok)" in hit

    hit = asyncio.run(orchestra_search("hata", use_gitignore=False))
    assert "debug.log" in hit


def test_search_structured_payload(project):
    from mcp.types import CallToolResult
    res = asyncio.run(orchestra_search("def add", structured=True))
    assert isinstance(res, CallToolResult)
    payload = res.structuredContent
    assert payload["count"] == 1
    assert payload["results"][0]["file"] == "src/app.py"
    assert payload["results"][0]["line"] == 1
    # metin içeriği de korunur
    assert "def add" in res.content[0].text


def test_structured_is_opt_in(project):
    plain = asyncio.run(orchestra_search("def add"))
    assert isinstance(plain, str)


def test_status_structured(project):
    from mcp.types import CallToolResult
    res = asyncio.run(orchestra_status(structured=True))
    assert isinstance(res, CallToolResult)
    assert res.structuredContent["workspace"] == str(project)
    assert "totals" in res.structuredContent


# ---------------------------------------------------------------- güvenlik
def test_secret_inside_secret_dir_blocked(project):
    secret_dir = project / ".env"
    secret_dir.mkdir()
    (secret_dir / "not_secret_name.txt").write_text("TOKEN=abc\n", encoding="utf-8")
    result = asyncio.run(orchestra_read_many(".env/not_secret_name.txt"))
    assert "engellendi" in result.lower() or "Erişim" in result

    with pytest.raises(ws.WorkspaceError):
        ws.safe_path(project, ".env/not_secret_name.txt")


def test_traversal_blocked(project):
    with pytest.raises(ws.WorkspaceError):
        ws.safe_path(project, "../kacis.txt")


def test_gitignore_matcher_negation():
    pats = ws.load_gitignore
    import fnmatch

    patterns = [
        {"pattern": "*.log", "negated": False, "dir_only": False, "anchored": False},
        {"pattern": "onemli.log", "negated": True, "dir_only": False, "anchored": False},
    ]
    assert ws.is_ignored("a/hata.log", patterns) is True
    assert ws.is_ignored("a/onemli.log", patterns) is False


# ---------------------------------------------------------------- ping cache
def test_ping_cache_hit_avoids_network(project, monkeypatch):
    _PING_CACHE.clear()
    calls = {"n": 0}

    class FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls["n"] += 1
                    raise RuntimeError("ağ yok (test)")

    monkeypatch.setattr(mcp_server._llm, "cfg", {"roles": {"coder": {"models": [
        {"provider": "evren", "model": "fake-model"}]}}})
    monkeypatch.setattr(mcp_server._llm, "_client", lambda provider: FakeClient())
    monkeypatch.setattr(mcp_server, "_jev_ping_line", lambda: "[judge] atlandı")

    lines1, cached1, _ = _ping_impl(role="coder", max_age=300)
    assert cached1 is False
    assert calls["n"] == 1
    assert any("❌" in ln for ln in lines1)

    lines2, cached2, age = _ping_impl(role="coder", max_age=300)
    assert cached2 is True
    assert calls["n"] == 1          # ağa tekrar gidilmedi
    assert lines2 == lines1
    assert age >= 0

    _ping_impl(role="coder", max_age=300, refresh=True)
    assert calls["n"] == 2          # refresh önbelleği atlar
    _PING_CACHE.clear()


def test_ping_unknown_role(project, monkeypatch):
    monkeypatch.setattr(mcp_server._llm, "cfg", {"roles": {"coder": {"models": []}}})
    lines, cached, _ = _ping_impl(role="yok-boyle-rol")
    assert any("Bilinmeyen rol" in ln for ln in lines)


# ---------------------------------------------------------------- run_task timeout
def test_run_task_timeout_returns_message(project, monkeypatch):
    import time as _time

    def slow(*a, **kw):
        _time.sleep(3)
        return {"ok": True, "files": []}

    monkeypatch.setattr(mcp_server.orchestrator, "run_task", slow)
    result = asyncio.run(mcp_server.orchestra_run_task("test", timeout=1))
    assert "zaman aşımı" in result.lower()


def test_run_task_propagates_exception(project, monkeypatch):
    def boom(*a, **kw):
        raise ValueError("patladı")

    monkeypatch.setattr(mcp_server.orchestrator, "run_task", boom)
    result = asyncio.run(mcp_server.orchestra_run_task("test", timeout=10))
    assert "Orkestra hatası" in result and "patladı" in result


# ---------------------------------------------------------------- workspace
def test_workspace_structured(project):
    from mcp.types import CallToolResult
    res = asyncio.run(orchestra_workspace(structured=True))
    assert isinstance(res, CallToolResult)
    assert res.structuredContent["workspace"] == str(project)
    assert res.structuredContent["changed"] is False


def test_git_structured_payload(project):
    from mcp.types import CallToolResult
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=project, stdin=subprocess.DEVNULL,
                       capture_output=True, env=env)
    (project / "src" / "app.py").write_text("degisti\n", encoding="utf-8")

    res = asyncio.run(orchestra_git("status", structured=True))
    assert isinstance(res, CallToolResult)
    assert res.structuredContent["is_repo"] is True
    assert res.structuredContent["dirty"] is True
    assert any("src/app.py" in e for e in res.structuredContent["entries"])
