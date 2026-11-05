"""Boru hattı iyileştirmeleri: coder eşleşme, geçici kopya, verify, güvenlik."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import coder  # noqa: E402
import orchestrator  # noqa: E402
import tracker  # noqa: E402
import verify  # noqa: E402
import workspace  # noqa: E402


# ---------------------------------------------------------------------------
# coder: exact / normalized / fuzzy / çoklu eşleşme
# ---------------------------------------------------------------------------
def _base():
    return "def f():\n    return 1\n\n\ndef g():\n    return 2\n"


def test_exact_match():
    new, how = coder.apply_change(_base(), coder.Change("x.py", "edit", search="return 1", replace="return 10"))
    assert how == "exact"
    assert "return 10" in new


def test_normalized_match_indentation():
    ch = coder.Change("x.py", "edit", search="def f():\nreturn 1", replace="def f():\n    return 99")
    new, how = coder.apply_change(_base(), ch)
    assert how == "normalized"
    assert "return 99" in new


def test_fuzzy_match_small_typo():
    ch = coder.Change("x.py", "edit",
                      search="def g():\n    retrun 2",
                      replace="def g():\n    return 22")
    new, how = coder.apply_change(_base(), ch)
    assert how.startswith("fuzzy")
    assert "return 22" in new


def test_multi_match_error_lists_lines():
    base = "x = 1\nx = 1\n"
    with pytest.raises(coder.ApplyError) as ei:
        coder.apply_change(base, coder.Change("x.py", "edit", search="x = 1", replace="x = 2"))
    assert "2 kez" in str(ei.value)
    assert "satır" in str(ei.value)


def test_missing_search_error():
    with pytest.raises(coder.ApplyError):
        coder.apply_change(_base(), coder.Change("x.py", "edit", search="tamamen yok", replace="x"))


def test_parse_warnings_unclosed_block():
    raw = "<<<FILE: a.py>>>\nprint(1)\n<<<END>>>\n\n<<<FILE: b.py>>>\nprint(2)\n"
    assert coder.parse_changes(raw)
    warns = coder.parse_warnings(raw)
    assert warns and "END" in warns[0]


def test_targeted_feedback_contains_snippet(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("\n".join(f"line{i}" for i in range(1, 31)), encoding="utf-8")
    fb = coder.targeted_feedback(tmp_path, "a.py", coder.ApplyError("satırlar: 10"), context_lines=10)
    assert "a.py" in fb
    assert "line10" in fb


def test_apply_in_memory_warns_non_exact(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("def f():\n    return 1\n", encoding="utf-8")
    staged, warns = coder.apply_in_memory(
        tmp_path, [coder.Change("a.py", "edit", search="def f():\nreturn 1", replace="def f():\n    return 2")],
        collect_warnings=True)
    assert "return 2" in staged["a.py"]
    assert warns and "normalized" in warns[0]


# ---------------------------------------------------------------------------
# workspace: güvenli okuma + geçici kopya
# ---------------------------------------------------------------------------
def test_safe_read_path_relative(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert workspace.safe_read_path(tmp_path, "a.txt").name == "a.txt"


def test_safe_read_path_absolute_inside(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("x", encoding="utf-8")
    assert workspace.safe_read_path(tmp_path, str(p)) == p.resolve()


def test_safe_read_path_absolute_outside_denied(tmp_path):
    outside = tmp_path.parent / "baska"
    outside.mkdir(exist_ok=True)
    (outside / "b.txt").write_text("x", encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError):
        workspace.safe_read_path(tmp_path / "proj", str(outside / "b.txt"))


def test_safe_read_path_extra_root_allowed(tmp_path):
    allowed = tmp_path / "indirilenler"
    allowed.mkdir()
    f = allowed / "belge.txt"
    f.write_text("x", encoding="utf-8")
    got = workspace.safe_read_path(tmp_path / "proj", str(f), extra_roots=[allowed])
    assert got == f.resolve()


def test_safe_read_path_secret_denied(tmp_path):
    (tmp_path / ".env").write_text("KEY=1", encoding="utf-8")
    with pytest.raises(workspace.WorkspaceError):
        workspace.safe_read_path(tmp_path, str(tmp_path / ".env"))


def test_stage_to_temp_does_not_touch_source(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("old = 1\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("x", encoding="utf-8")

    tmp, mode = workspace.stage_to_temp(root, {"a.py": "new = 2\n"})
    try:
        assert mode == "full"
        assert (tmp / "a.py").read_text(encoding="utf-8") == "new = 2\n"
        assert not (tmp / ".git").exists(), ".git kopyalanmamalı"
        assert (root / "a.py").read_text(encoding="utf-8") == "old = 1\n"
    finally:
        workspace.cleanup_stage(tmp)
    assert not tmp.exists()


def test_stage_to_temp_minimal_mode(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n", encoding="utf-8")
    tmp, mode = workspace.stage_to_temp(root, {"main.py": "x = 2\n"}, max_files=0)
    try:
        assert mode == "minimal"
        assert (tmp / "main.py").read_text(encoding="utf-8") == "x = 2\n"
        assert (tmp / "tests" / "test_a.py").is_file()
    finally:
        workspace.cleanup_stage(tmp)


# ---------------------------------------------------------------------------
# verify: komut algılama, ortam temizliği, hata özeti
# ---------------------------------------------------------------------------
def test_detect_python(tmp_path):
    (tmp_path / "tests").mkdir()
    cmd, why = verify.detect_test_command(tmp_path)
    assert cmd and "pytest" in " ".join(cmd)


def test_detect_node(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    cmd, why = verify.detect_test_command(tmp_path)
    assert cmd[0] == "npm" and "package.json" in why


def test_detect_rust(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    cmd, _ = verify.detect_test_command(tmp_path)
    assert cmd[:2] == ["cargo", "test"]


def test_detect_go(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    cmd, _ = verify.detect_test_command(tmp_path)
    assert cmd[:2] == ["go", "test"]


def test_detect_none(tmp_path):
    cmd, why = verify.detect_test_command(tmp_path)
    assert cmd is None and "tanın" in why


def test_clean_env_strips_secrets(monkeypatch):
    monkeypatch.setenv("EVREN_LLM_API_KEY", "gizli")
    monkeypatch.setenv("CUSTOM_API_KEY", "gizli2")
    monkeypatch.setenv("NORMAL_VAR", "ok")
    env = verify.clean_env()
    assert "EVREN_LLM_API_KEY" not in env
    assert "CUSTOM_API_KEY" not in env
    assert env.get("NORMAL_VAR") == "ok"


def test_summarize_failure_prioritizes_errors():
    out = "toplama...\n" + "x" * 100 + "\nFAILED tests/test_a.py::test_b - assert 1 == 2\nE   assert 1 == 2\n"
    txt = verify.summarize_failure(out)
    assert "FAILED" in txt


def test_static_check_syntax_error():
    problems = verify.static_check({"a.py": "def f(:\n"})
    assert problems and "SyntaxError" in problems[0]


def test_static_check_json_error():
    problems = verify.static_check({"a.json": "{bozuk"})
    assert problems


# ---------------------------------------------------------------------------
# orchestrator: onay kapısı + ek bağlamı
# ---------------------------------------------------------------------------
def test_confirmation_gate_many_files(tmp_path):
    staged = {f"f{i}.py": "x = 1\n" for i in range(20)}
    gate = orchestrator._confirmation_gate(tmp_path, staged)
    assert "Onay gerekiyor" in gate


def test_confirmation_gate_sensitive(tmp_path):
    gate = orchestrator._confirmation_gate(tmp_path, {"config.yaml": "a: 1\n"})
    assert "Onay gerekiyor" in gate


def test_confirmation_gate_allows_normal(tmp_path):
    assert orchestrator._confirmation_gate(tmp_path, {"app.py": "x = 1\n"}) == ""


def test_confirmation_gate_empty_file(tmp_path):
    gate = orchestrator._confirmation_gate(tmp_path, {"app.py": "   \n"})
    assert "boşaltılan" in gate


def test_attachments_context():
    ctx = orchestrator.attachments_context([
        {"path": "a.pdf", "kind": "pdf", "method": "text", "text": "Belge içeriği"},
    ])
    assert "a.pdf" in ctx and "Belge içeriği" in ctx
    assert "VERISI" in ctx or "VERİSİ" in ctx.replace("İ", "I")


def test_attachments_context_budget():
    big = "x" * 200_000
    ctx = orchestrator.attachments_context([{"path": "b.txt", "kind": "text", "method": "text", "text": big}])
    assert len(ctx) < 140_000


# ---------------------------------------------------------------------------
# tracker: sır maskeleme
# ---------------------------------------------------------------------------
def test_mask_secrets():
    assert "sk-" not in tracker.mask_secrets("anahtar sk-abcdef1234567890abcd")
    assert "nvapi-" not in tracker.mask_secrets("x nvapi-abcdef1234567890")
    assert "***" in tracker.mask_secrets("api_key=supersecret123")


def test_mask_deep():
    out = tracker.mask_deep({"a": ["sk-abcdef1234567890abcd"], "b": 1})
    assert "sk-" not in str(out) and out["b"] == 1
