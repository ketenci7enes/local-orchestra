"""FAZ 1-F / FAZ 2 kapsamındaki yeni davranışların testleri.

Kapsam: Jev parse + fail-closed, tek öneri kısayolu, dürüst decider atfı,
tutum sistemi, A1 (qa override), A3 (kesilme), A5 (cooldown), A6 (dry-run), C1 (cap)."""
import json
import time

import pytest

import council
import orchestrator
import workspace
from judge import Judge, JevUnavailable
from judge import requests as judge_requests  # judge modülünün kullandığı requests
from llm import LLMClient, LLMError, cooldown_wait


# ---------- ortak yardımcılar ----------
def _project(tmp_path):
    proj = tmp_path / "proje"
    proj.mkdir()
    (proj / "app.py").write_text("def topla(a, b):\n    return a - b\n", encoding="utf-8")
    (proj / "tests").mkdir()
    (proj / "tests" / "test_app.py").write_text(
        "from app import topla\n\ndef test_topla():\n    assert topla(2, 3) == 5\n", encoding="utf-8"
    )
    return proj


FIX = "<<<EDIT: app.py>>>\n<<<SEARCH>>>\n    return a - b\n<<<REPLACE>>>\n    return a + b\n<<<END>>>"


class FakeLLM:
    """Basit (simple) görevler için: router + coder."""

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


class FakeDeciderJudge(FakeJudge):
    def __init__(self, status="APPROVED", pick=1):
        super().__init__(status)
        self.pick = pick

    def decide(self, goal, proposals):
        winner = next(o for o in proposals if o["id"] == self.pick)
        return winner, "sahte decider", "sahte/model", "gerekçe"


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.text = json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._body


# ---------- Jev entegrasyonu ----------
def test_jev_choice_parse(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        return _Resp({"answers": {"best_option_id": {"choice": "2", "confidence": 0.9}}})

    monkeypatch.setattr(judge_requests, "post", fake_post)
    j = Judge(None, use_jev=True, decide_with_jev=True)
    props = [
        {"id": 1, "strategy": "a", "author_label": "Kurul 1", "author_model": "glm-5.3"},
        {"id": 2, "strategy": "b", "author_label": "Kurul 2", "author_model": "gemma-4-31b"},
    ]
    winner, by, model, reason = j.decide("hedef", props)
    assert winner["id"] == 2
    assert by == "Jev (TypeSafe)"
    assert model == "typesafe/jev-1.14"
    # Gövde şeması: choice + criteria SÖZLÜĞÜ
    _, payload = calls[0]
    q = payload["questions"]["best_option_id"]
    assert q["type"] == "choice"
    assert isinstance(q["criteria"], dict)
    assert payload["model"] == "typesafe/jev-1.14"


def test_jev_deep_protocol_runs_on_close_decision(monkeypatch):
    """Yakın kararda (güven 0.02) 3 boyut + nihai oylama devreye girer."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    seen_qids = []

    def fake_post(url, headers=None, json=None, timeout=None):
        qids = set(json["questions"].keys())
        seen_qids.append(qids)
        if "best_option_id" in qids:
            return _Resp({"answers": {"best_option_id": {
                "choice": "1", "confidence": 0.02, "probabilities": {"1": 0.51, "2": 0.49}}}})
        if "nihai" in qids:
            return _Resp({"answers": {"nihai": {
                "choice": "2", "confidence": 0.85, "probabilities": {"1": 0.1, "2": 0.9}}}})
        return _Resp({"answers": {
            "kapsam": {"choice": "2", "confidence": 0.9, "probabilities": {"1": 0.05, "2": 0.95}},
            "sadelik": {"choice": "1", "confidence": 0.4, "probabilities": {"1": 0.7, "2": 0.3}},
            "surdurulebilirlik": {"choice": "2", "confidence": 0.8, "probabilities": {"1": 0.1, "2": 0.8}},
        }})

    monkeypatch.setattr(judge_requests, "post", fake_post)
    j = Judge(None, use_jev=True, decide_with_jev=True)
    props = [
        {"id": 1, "strategy": "a", "details": "da", "files": ["a.py"]},
        {"id": 2, "strategy": "b", "details": "db", "files": ["b.py"]},
        {"id": 3, "strategy": "c", "details": "dc", "files": ["c.py"]},
    ]
    winner, by, model, reason = j.decide("hedef", props)

    assert winner["id"] == 2            # nihai oylama hızlı kararı (1) geçersiz kıldı
    deep = winner["jev_deep"]
    assert deep["triggered"] is True
    assert deep["scores"]["kapsam"]["choice"] == "2"
    assert deep["scores"]["sadelik"]["choice"] == "1"
    assert deep["tie_break"] is None
    # 3 çağrı: hızlı karar + boyutlar + nihai
    assert len(seen_qids) == 3
    assert {"kapsam", "sadelik", "surdurulebilirlik"} in seen_qids


def test_jev_skips_deep_protocol_when_confident(monkeypatch):
    """Güven yüksekse derin protokol çalışmaz (tek çağrı)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(set(json["questions"].keys()))
        return _Resp({"answers": {"best_option_id": {
            "choice": "2", "confidence": 0.95, "probabilities": {"1": 0.02, "2": 0.96}}}})

    monkeypatch.setattr(judge_requests, "post", fake_post)
    j = Judge(None, use_jev=True, decide_with_jev=True)
    props = [{"id": i, "strategy": s, "details": "", "files": []} for i, s in ((1, "a"), (2, "b"), (3, "c"))]
    winner, *_ = j.decide("hedef", props)

    assert winner["id"] == 2
    assert winner["jev_deep"] is None
    assert len(calls) == 1


def test_jev_deep_tiebreak_prefers_fewer_dependencies(monkeypatch):
    """Nihai oylama da yakınsa eşitlik bozucu KURAL-1 (en az bağımlılık) kodda uygulanır."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def fake_post(url, headers=None, json=None, timeout=None):
        qids = set(json["questions"].keys())
        if "best_option_id" in qids:
            return _Resp({"answers": {"best_option_id": {
                "choice": "1", "confidence": 0.02, "probabilities": {"1": 0.5, "2": 0.5}}}})
        if "nihai" in qids:
            return _Resp({"answers": {"nihai": {
                "choice": "1", "confidence": 0.05, "probabilities": {"1": 0.45, "2": 0.4}}}})
        return _Resp({"answers": {
            "kapsam": {"choice": "1", "confidence": 0.6, "probabilities": {"1": 0.6, "2": 0.4}},
            "sadelik": {"choice": "2", "confidence": 0.6, "probabilities": {"1": 0.4, "2": 0.6}},
            "surdurulebilirlik": {"choice": "1", "confidence": 0.5, "probabilities": {"1": 0.5, "2": 0.5}},
        }})

    monkeypatch.setattr(judge_requests, "post", fake_post)
    j = Judge(None, use_jev=True, decide_with_jev=True)
    props = [
        {"id": 1, "strategy": "requests ile yap", "details": "import requests kullan", "files": ["a.py", "b.py"]},
        {"id": 2, "strategy": "stdlib ile yap", "details": "ek bagimlilik yok", "files": ["c.py"]},
        {"id": 3, "strategy": "c", "details": "from os import path", "files": ["d.py"]},
    ]
    winner, *_ = j.decide("hedef", props)

    assert winner["jev_deep"]["tie_break"].startswith("kural-1")
    assert winner["id"] == 2  # en az dış bağımlılık


def test_jev_wrong_schema_is_invalid(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    # Eski (jev-ai.pro) şema: answers[qid] düz string → geçersiz sayılmalı
    monkeypatch.setattr(judge_requests, "post", lambda *a, **k: _Resp({"answers": {"best_option_id": "2"}}))
    j = Judge(None, use_jev=True, decide_with_jev=True)
    with pytest.raises(JevUnavailable):
        j.decide("hedef", [{"id": 1, "strategy": "a"}, {"id": 2, "strategy": "b"}])


def test_jev_fail_closed_on_network_error(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    def boom(*a, **k):
        raise RuntimeError("ağ hatası")

    monkeypatch.setattr(judge_requests, "post", boom)
    j = Judge(None, use_jev=True, decide_with_jev=True)
    with pytest.raises(JevUnavailable):
        j.decide("hedef", [{"id": 1, "strategy": "a"}, {"id": 2, "strategy": "b"}])


def test_single_proposal_skips_decision(monkeypatch):
    calls = []
    monkeypatch.setattr(judge_requests, "post", lambda *a, **k: calls.append(1))
    j = Judge(None, use_jev=True, decide_with_jev=True)
    winner, by, model, reason = j.decide("hedef", [{"id": 1, "strategy": "tek"}])
    assert winner["id"] == 1
    assert calls == []  # hiçbir çağrı yapılmadı
    assert "tek öneri" in by


class _FailingDeciderLLM:
    def __init__(self):
        self.cfg = {"roles": {"decider": {"label": "gpt-oss-120b (yedek)", "models": [{"model": "openai/gpt-oss-120b"}]}}}

    def chat(self, role, messages, json_mode=False, **kw):
        raise LLMError("decider yanıt vermedi")


def test_decider_failed_is_honest():
    j = Judge(_FailingDeciderLLM(), use_jev=False)
    winner, by, model, reason = j.decide("hedef", [{"id": 1, "strategy": "a"}, {"id": 2, "strategy": "b"}])
    assert winner["id"] == 1  # ilk öneriyle devam
    assert "karar verilemedi" in by
    assert model == "openai/gpt-oss-120b"
    assert "yanıt vermedi" in reason


# ---------- Kurul: tek tip kıdemli uzman (tutum sistemi kaldırıldı) ----------
class _SeniorLLM:
    def __init__(self):
        self.prompts = []
        self.cfg = {
            "roles": {
                "council_a": {"label": "Kurul 1 · GLM 5.3", "models": [{"model": "glm-5.3"}]},
                "council_b": {"label": "Kurul 2 · Gemma 4 31B", "models": [{"model": "gemma-4-31b"}]},
                "council_c": {"label": "Kurul 3 · DeepSeek V4.1", "models": [{"model": "deepseek-v4.1-flash"}]},
            }
        }

    def chat(self, role, messages, json_mode=False, **kw):
        self.prompts.append(messages[0]["content"])
        return '{"strategy": "s", "details": "d", "risks": "r"}'


def test_council_salvages_malformed_json():
    """Bozuk JSON (`{"}strategy": ...`) yüzünden kurul üyesi kaybedilmemeli."""

    class _BadJSONLLM:
        cfg = {
            "roles": {
                "council_a": {"label": "A", "models": [{"model": "m1"}]},
                "council_b": {"label": "B", "models": [{"model": "m2"}]},
                "council_c": {"label": "C", "models": [{"model": "m3"}]},
            }
        }

        def chat(self, role, messages, json_mode=False, **kw):
            return '{"}strategy": "S", "details": "D", "risks": "R", "files": ["a.py"]}'

    props = council.generate_proposals(_BadJSONLLM(), "g", "ctx")
    assert len(props) == 3
    assert props[0]["strategy"] == "S"
    assert props[0]["details"] == "D"
    assert props[0]["files"] == ["a.py"]


def test_council_all_members_are_senior_experts():
    llm = _SeniorLLM()
    props = council.generate_proposals(llm, "g", "ctx")
    assert len(props) == 3
    # Üç üye de aynı "kıdemli yazılım uzmanı" personasını aldı
    assert all("kıdemli" in p.lower() for p in llm.prompts)
    assert all("tutum" not in p.lower() for p in llm.prompts)
    # Tutum anahtarı artık üretilmiyor
    assert all("stance" not in p for p in props)
    # Model kimliği öneriye iliştirildi
    assert [p["author_model"] for p in props] == ["glm-5.3", "gemma-4-31b", "deepseek-v4.1-flash"]
    assert [p["id"] for p in props] == [1, 2, 3]


# ---------- A1: qa override ----------
def test_a1_qa_simple_returns_answer(tmp_path):
    class _Qa:
        def __init__(self):
            self.cfg = {"roles": {}}

        def chat(self, role, messages, json_mode=False, **kw):
            if role == "router":
                return '{"type": "qa", "complexity": "simple", "files": []}'
            return "kısa yanıt"

    res = orchestrator.run_task(_Qa(), None, tmp_path, "bu nedir?", auto=True)
    assert res.get("answer") == "kısa yanıt"


def test_a1_qa_override_complex_engages_council(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "BACKUP_DIR", tmp_path / "_backups")
    proj = _project(tmp_path)

    class QaCouncilLLM:
        def __init__(self):
            self.coder_outputs = [FIX]
            self.seen = []
            self.cfg = {
                "roles": {
                    "council_a": {"label": "A", "models": [{"model": "a"}]},
                    "council_b": {"label": "B", "models": [{"model": "b"}]},
                    "council_c": {"label": "C", "models": [{"model": "c"}]},
                }
            }

        def chat(self, role, messages, json_mode=False, **kw):
            self.seen.append(role)
            if role == "router":
                return '{"type": "qa", "complexity": "simple", "files": ["app.py"]}'
            if role in ("council_a", "council_b", "council_c"):
                return json.dumps({"strategy": role, "details": "d", "risks": "r"})
            if role == "coder":
                return self.coder_outputs.pop(0)
            raise AssertionError(role)

    llm = QaCouncilLLM()
    res = orchestrator.run_task(
        llm, FakeDeciderJudge(), proj, "topla hatasını düzelt", auto=True, complexity_override="complex"
    )
    assert res["ok"], res
    assert any(r.startswith("council_") for r in llm.seen)  # kurul devreye girdi


# ---------- A3: max_tokens kesilmesi ----------
def test_a3_finish_reason_length_raises(monkeypatch):
    class _Trunc:
        def __init__(self):
            self.chat = self
            self.completions = self

        def create(self, **kw):
            msg = type("M", (), {"content": "kısmi çıktı"})()
            choice = type("C", (), {"message": msg, "finish_reason": "length"})()
            return type("R", (), {"choices": [choice], "usage": None})()

    llm = LLMClient()
    monkeypatch.setattr(llm, "_client", lambda p: _Trunc())
    with pytest.raises(LLMError):
        llm.chat("router", [{"role": "user", "content": "x"}])


# ---------- A5: cooldown ----------
def test_a5_cooldown_wait_reports_remaining():
    llm = LLMClient()
    assert cooldown_wait(llm, "coder") == 0.0
    first = llm.cfg["roles"]["coder"]["models"][0]
    llm._cooldown[(first["provider"], first["model"])] = time.time() + 30
    w = cooldown_wait(llm, "coder")
    assert 20 < w <= 30


def test_a5_coder_error_does_not_consume_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "BACKUP_DIR", tmp_path / "_backups")
    proj = _project(tmp_path)
    llm = FakeLLM([FIX])
    llm.cfg = {"roles": {"coder": {"models": [{"provider": "x", "model": "y"}]}}}
    llm._cooldown = {("x", "y"): time.time() + 5}

    from coder import generate as real_generate
    calls = {"n": 0}

    def flaky(llm_, plan_text, files_ctx, feedback=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LLMError("429 rate limited")
        return real_generate(llm_, plan_text, files_ctx, feedback)

    monkeypatch.setattr("coder.generate", flaky)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda s: None)

    res = orchestrator.run_task(llm, FakeJudge(), proj, "topla hatasını düzelt", auto=True)
    assert res["ok"], res
    assert calls["n"] == 2  # hata deneme harcamadan tekrarlandı


# ---------- A6: dry-run ----------
def test_a6_dry_run_writes_nothing_but_returns_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "BACKUP_DIR", tmp_path / "_backups")
    proj = _project(tmp_path)
    before = (proj / "app.py").read_text()
    res = orchestrator.run_task(FakeLLM([FIX]), FakeJudge(), proj, "düzelt", auto=True, dry_run=True)
    assert res["ok"] and res.get("dry_run")
    assert "a + b" in res["diff"]
    assert (proj / "app.py").read_text() == before  # diske yazılmadı
    assert not list((tmp_path / "_backups").rglob("app.py"))  # yedek/rollback yok


# ---------- C1: qa cevap üst sınırı ----------
def test_c1_answer_uses_max_tokens_1500(tmp_path):
    class _Cap:
        def __init__(self):
            self.cfg = {"roles": {}}
            self.kw = None

        def chat(self, role, messages, json_mode=False, **kw):
            if role == "router":
                return '{"type": "qa", "complexity": "simple", "files": []}'
            self.kw = kw
            return "cevap"

    llm = _Cap()
    res = orchestrator.run_task(llm, None, tmp_path, "soru", auto=True)
    assert res["answer"] == "cevap"
    assert llm.kw.get("max_tokens") == 1500
