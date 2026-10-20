from types import SimpleNamespace

import pytest
from openai import RateLimitError

import coder
import orchestrator
import verify
import workspace
from llm import LLMClient, LLMError, parse_json
from workspace import WorkspaceError


# ---------- yardımcılar ----------
def test_parse_json_fenced_and_noisy():
    assert parse_json('Tabii!\n```json\n{"a": 1}\n```\nbitti') == {"a": 1}
    assert parse_json('önce {"x": [1, 2]} sonra') == {"x": [1, 2]}
    with pytest.raises(ValueError):
        parse_json("json yok")


def test_safe_path_blocks_escape_and_secrets(tmp_path):
    with pytest.raises(WorkspaceError):
        workspace.safe_path(tmp_path, "../disari.txt")
    with pytest.raises(WorkspaceError):
        workspace.safe_path(tmp_path, "/etc/passwd")
    with pytest.raises(WorkspaceError):
        workspace.safe_path(tmp_path, ".env")
    assert workspace.safe_path(tmp_path, "src/a.py").name == "a.py"


# ---------- coder ayrıştırma / uygulama ----------
EDIT = "<<<EDIT: app.py>>>\n<<<SEARCH>>>\nx = 1\n<<<REPLACE>>>\nx = 2\n<<<END>>>"


def test_edit_and_file_blocks(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\nprint(x)\n", encoding="utf-8")
    raw = EDIT + "\n\n<<<FILE: yeni.py>>>\nprint('merhaba')\n<<<END>>>"
    staged = coder.apply_in_memory(tmp_path, coder.parse_changes(raw))
    assert staged["app.py"] == "x = 2\nprint(x)\n"
    assert staged["yeni.py"] == "print('merhaba')\n"
    assert (tmp_path / "app.py").read_text() == "x = 1\nprint(x)\n"  # diske yazılmadı


def test_edit_empty_replace_deletes(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    raw = "<<<EDIT: a.py>>>\n<<<SEARCH>>>\nb = 2\n<<<REPLACE>>>\n<<<END>>>"
    staged = coder.apply_in_memory(tmp_path, coder.parse_changes(raw))
    assert staged["a.py"] == "a = 1\n\n"


def test_edit_search_must_be_unique(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    with pytest.raises(coder.ApplyError):
        coder.apply_in_memory(tmp_path, coder.parse_changes(EDIT))


def test_no_blocks_is_parse_error():
    with pytest.raises(coder.ParseError):
        coder.parse_changes("işte kod: print(1)")


def test_edit_path_traversal_blocked(tmp_path):
    raw = "<<<FILE: ../kotu.py>>>\nprint(1)\n<<<END>>>"
    with pytest.raises(WorkspaceError):
        coder.apply_in_memory(tmp_path, coder.parse_changes(raw))


# ---------- doğrulama ----------
def test_static_check_catches_syntax_error():
    problems = verify.static_check({"a.py": "def f(:\n  pass\n", "ok.json": '{"a": 1}', "bad.json": "{"})
    assert any("a.py" in p for p in problems)
    assert any("bad.json" in p for p in problems)
    assert not any("ok.json" in p for p in problems)


# ---------- LLM failover ----------
class _Boom:
    def __init__(self, exc):
        self.exc = exc
        self.chat = self
        self.completions = self

    def create(self, **kw):
        raise self.exc


class _Ok:
    def __init__(self, text):
        self.text = text
        self.chat = self
        self.completions = self

    def create(self, **kw):
        msg = type("M", (), {"content": self.text})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice], "usage": None})()


def _rate_limit():
    # SDK'nın httpx sürümünden bağımsız olsun diye minimal sahte yanıt nesnesi
    resp = SimpleNamespace(status_code=429, headers={}, request=SimpleNamespace())
    return RateLimitError("rate limited", response=resp, body=None)


def test_failover_on_rate_limit(monkeypatch):
    llm = LLMClient()
    calls = []

    def fake_client(provider):
        calls.append(provider)
        return _Boom(_rate_limit()) if len(calls) == 1 else _Ok("<think>x</think>merhaba")

    monkeypatch.setattr(llm, "_client", fake_client)
    assert llm.chat("router", [{"role": "user", "content": "selam"}]) == "merhaba"
    assert len(calls) == 2


def test_all_candidates_fail(monkeypatch):
    llm = LLMClient()
    monkeypatch.setattr(llm, "_client", lambda p: _Boom(_rate_limit()))
    with pytest.raises(LLMError):
        llm.chat("router", [{"role": "user", "content": "selam"}])


# ---------- uçtan uca (sahte LLM + sahte hakem) ----------
class FakeLLM:
    def __init__(self, coder_outputs):
        self.coder_outputs = list(coder_outputs)

    def chat(self, role, messages, json_mode=False, **kw):
        if role == "router":
            return '{"type": "code", "complexity": "simple", "files": ["app.py"]}'
        if role == "coder":
            return self.coder_outputs.pop(0)
        raise AssertionError(f"beklenmeyen rol: {role}")


class FakeJudge:
    def __init__(self, status="APPROVED"):
        self.status = status

    def review(self, goal, diff):
        return self.status, "gerekçe"


def _project(tmp_path):
    (tmp_path / "app.py").write_text("def topla(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import topla\n\ndef test_topla():\n    assert topla(2, 3) == 5\n", encoding="utf-8"
    )


FIX = "<<<EDIT: app.py>>>\n<<<SEARCH>>>\n    return a - b\n<<<REPLACE>>>\n    return a + b\n<<<END>>>"
WRONG = "<<<EDIT: app.py>>>\n<<<SEARCH>>>\n    return a - b\n<<<REPLACE>>>\n    return a * b\n<<<END>>>"


def test_end_to_end_success_with_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "BACKUP_DIR", tmp_path / "_backups")
    proj = tmp_path / "proje"
    proj.mkdir()
    _project(proj)
    res = orchestrator.run_task(FakeLLM([FIX]), FakeJudge(), proj, "topla hatasını düzelt", auto=True)
    assert res["ok"], res
    assert "a + b" in (proj / "app.py").read_text()
    assert list((tmp_path / "_backups").rglob("app.py"))  # yedek alındı


def test_failing_tests_roll_back_then_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "BACKUP_DIR", tmp_path / "_backups")
    proj = tmp_path / "proje"
    proj.mkdir()
    _project(proj)
    res = orchestrator.run_task(FakeLLM([WRONG, FIX]), FakeJudge(), proj, "topla hatasını düzelt", auto=True)
    assert res["ok"], res
    assert "a + b" in (proj / "app.py").read_text()  # 1. deneme geri alındı, 2.si geçti


def test_judge_unavailable_is_fail_closed_in_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "BACKUP_DIR", tmp_path / "_backups")
    proj = tmp_path / "proje"
    proj.mkdir()
    _project(proj)
    llm = FakeLLM([FIX, FIX, FIX])
    res = orchestrator.run_task(llm, FakeJudge("UNAVAILABLE"), proj, "düzelt", auto=True)
    assert not res["ok"]
    assert "a - b" in (proj / "app.py").read_text()  # dosya değişmedi


# ---------- kurul (council) + GLM karar akışı ----------
class FakeCouncilLLM:
    """router=complex, 3 kurul üyesi (3 tutum) öneri üretir, decider karar verir,
    kazanan üye detaylı promptu yazar, coder kodu üretir."""

    def __init__(self, coder_outputs):
        self.coder_outputs = list(coder_outputs)
        self.seen = []
        self.cfg = {
            "roles": {
                "council_a": {"label": "Kurul 1 · GLM 5.3", "models": [{"model": "glm-5.3"}]},
                "council_b": {"label": "Kurul 2 · Gemma 4 31B", "models": [{"model": "gemma-4-31b"}]},
                "council_c": {"label": "Kurul 3 · DeepSeek V4.1 Flash", "models": [{"model": "deepseek-v4.1-flash"}]},
                "decider": {"label": "gpt-oss-120b (yedek)", "models": [{"model": "openai/gpt-oss-120b"}]},
            }
        }

    def chat(self, role, messages, json_mode=False, **kw):
        self.seen.append(role)
        if role == "router":
            return '{"type": "code", "complexity": "complex", "files": ["app.py"]}'
        if role == "council_a":
            return '{"strategy": "A stratejisi", "details": "a detay", "risks": "yok"}'
        if role == "council_b":
            if self.seen.count("council_b") == 1:
                return '{"strategy": "B stratejisi", "details": "b detay", "risks": "yok"}'
            return "DETAYLI YÖNERGE: app.py içindeki çıkarma işlemini toplamaya çevir."
        if role == "council_c":
            return '{"strategy": "C stratejisi", "details": "c detay", "risks": "yok"}'
        if role == "decider":
            return '{"answer": "2", "reason": "B en uygulanabilir"}'
        if role == "judge":
            return '{"answer": "APPROVED", "reason": "uygun"}'
        if role == "coder":
            return self.coder_outputs.pop(0)
        raise AssertionError(f"beklenmeyen rol: {role}")


def test_council_flow_reports_decision_author(tmp_path, monkeypatch):
    from judge import Judge

    monkeypatch.setattr(workspace, "BACKUP_DIR", tmp_path / "_backups")
    proj = tmp_path / "proje"
    proj.mkdir()
    _project(proj)
    llm = FakeCouncilLLM([FIX])
    judge = Judge(llm, use_jev=False)  # LLM decider yolu (jev çağrılmaz)

    res = orchestrator.run_task(llm, judge, proj, "topla hatasını düzelt", auto=True)

    assert res["ok"], res
    assert "a + b" in (proj / "app.py").read_text()
    # Kurul: üç üye de öneri üretti, decider karar verdi
    assert "council_a" in llm.seen and "council_b" in llm.seen and "council_c" in llm.seen
    assert "decider" in llm.seen
    # Karar sahibi bilgisi sonuca yansıdı
    dec = res["decision"]
    assert dec["decided_by"] == "gpt-oss-120b (yedek)"
    assert dec["decider_model"] == "openai/gpt-oss-120b"
    assert dec["winner_author"] == "council_b"  # decider "2" numaralı öneriyi (Kurul 2) seçti
    assert dec["winner_label"] == "Kurul 2 · Gemma 4 31B"
    # 3 önerinin tam dökümü (tutum kolonu kaldırıldı, detay/dosya alanları eklendi)
    assert len(dec["comparison"]) == 3
    assert [c["author_role"] for c in dec["comparison"]] == ["council_a", "council_b", "council_c"]
    assert all("stance" not in c for c in dec["comparison"])
    assert dec["winner_id"] == 2
