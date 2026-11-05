"""Karar kalitesi: risk-farkında Jev kriterleri + düşük güven uyarısı + Türkçe İ regresyonu."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import judge as judge_mod
import orchestrator


# ---------------------------------------------------------------- #3 güven uyarısı
def _meta(conf, probs=None):
    obj = {}
    if conf is not None:
        obj["confidence"] = conf
    if probs is not None:
        obj["probabilities"] = probs
    return {"answer_obj": obj}


def test_low_confidence_warning_below_threshold():
    msg = judge_mod.low_confidence_warning(
        _meta(0.52, {"1": 0.68, "2": 0.14, "3": 0.18}), threshold=0.6)
    assert "Düşük karar güveni" in msg
    assert "0.52" in msg and "0.60" in msg
    assert "1=0.68" in msg  # dağılım görünür


def test_low_confidence_warning_above_threshold():
    assert judge_mod.low_confidence_warning(_meta(0.91), threshold=0.6) == ""


def test_low_confidence_warning_missing_confidence_is_silent():
    assert judge_mod.low_confidence_warning({}, threshold=0.6) == ""
    assert judge_mod.low_confidence_warning(_meta(None), threshold=0.6) == ""


def test_low_confidence_uses_env_default(monkeypatch):
    monkeypatch.setattr(judge_mod, "LOW_CONFIDENCE", 0.75)
    assert judge_mod.low_confidence_warning(_meta(0.70))
    assert judge_mod.low_confidence_warning(_meta(0.80)) == ""


def test_confidence_of():
    assert judge_mod.confidence_of(_meta(0.52)) == 0.52
    assert judge_mod.confidence_of(_meta("bozuk")) is None
    assert judge_mod.confidence_of({}) is None


def test_decision_of_includes_confidence_and_warning():
    plan = {
        "decided_by": "Jev (TypeSafe)",
        "decider_model": "typesafe/jev-1.14",
        "id": 1,
        "author_label": "Kurul 1",
        "author_model": "m",
        "decision_reason": "seçti",
        "jev_meta": _meta(0.52, {"1": 0.68, "2": 0.14, "3": 0.18}),
        "comparison": [],
    }
    d = orchestrator._decision_of(plan)
    assert d["confidence"] == 0.52
    assert "Düşük karar güveni" in d["low_confidence_warning"]


def test_decision_of_high_confidence_no_warning():
    plan = {"decided_by": "Jev", "id": 1, "jev_meta": _meta(0.95)}
    d = orchestrator._decision_of(plan)
    assert d["confidence"] == 0.95
    assert d["low_confidence_warning"] == ""


# ---------------------------------------------------------------- #2 risk-farkında kriterler
class _Captured(Exception):
    pass


def _judge_capturing(monkeypatch):
    """Jev çağrısını yakalayan sahte Judge (ağ yok)."""
    captured = {}

    class FakeJudge(judge_mod.Judge):
        def __init__(self):  # gerçek __init__ (config/LLM) çalışmasın
            self.decide_with_jev = True
            self.last_jev = {}

        def _jev(self, state, qid, instructions, criteria):
            captured.update({"state": state, "qid": qid,
                             "instructions": instructions, "criteria": criteria})
            raise _Captured

    return FakeJudge(), captured


def test_jev_criteria_include_risks(monkeypatch):
    fake, captured = _judge_capturing(monkeypatch)
    proposals = [
        {"id": 1, "author_label": "A", "author_model": "m1",
         "strategy": "translate tablosu", "details": "", "risks": "İ birleşik nokta riski"},
        {"id": 2, "author_label": "B", "author_model": "m2",
         "strategy": "regex", "details": "", "risks": ""},
        {"id": 3, "author_label": "C", "author_model": "m3",
         "strategy": "elle", "details": "", "risks": None},
    ]
    with pytest.raises(_Captured):
        fake.decide("hedef", proposals)

    crit = captured["criteria"]
    assert "İ birleşik nokta riski" in crit["1"]
    assert "belirtilmedi" in crit["2"]      # boş risk açıkça işaretlenir
    assert "belirtilmedi" in crit["3"]      # None -> güvenli metin
    # Soru, risklerin giderilmesini şart koşmalı
    assert "risk" in captured["instructions"].lower()
    assert "gideril" in captured["instructions"].lower()


def test_jev_state_still_has_risks(monkeypatch):
    fake, captured = _judge_capturing(monkeypatch)
    proposals = [
        {"id": 1, "strategy": "s1", "details": "", "risks": "r1"},
        {"id": 2, "strategy": "s2", "details": "", "risks": "r2"},
    ]
    with pytest.raises(_Captured):
        fake.decide("hedef", proposals)
    assert captured["state"]["proposals"][0]["risks"] == "r1"


# ---------------------------------------------------------------- #1/#4 Türkçe İ görevi
def _load_task(task_id):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
    import importlib
    import tasks_data
    importlib.reload(tasks_data)
    return next(t for t in tasks_data.TASKS if t["id"] == task_id)


def test_eval_task_turkish_i_exists():
    task = _load_task("edge-turkish-i")
    assert "snake_case" in task["goal"]
    assert "names.py" in task["files"]
    # Referans çözüm: translate ÖNCE, lower SONRA
    assert "translate(_TR_ASCII).lower()" in task["blocks"]
    # Test birleşik işareti yasaklıyor
    assert "0x300" in task["tests"]


def test_eval_task_turkish_solution_is_correct(tmp_path):
    task = _load_task("edge-turkish-i")
    # Referans çözümü gerçekten çalıştır (projenin kendi parser'ıyla)
    import coder
    changes = coder.parse_changes(task["blocks"])
    code = next(c.content for c in changes if c.path == "names.py")
    ns = {}
    exec(compile(code, "names.py", "exec"), ns)
    out = ns["snake_case"]("İstanbul Çok Güzel")
    assert out == "istanbul_cok_guzel", repr(out)
    assert not any(0x300 <= ord(c) <= 0x36F for c in out)


def test_eval_task_turkish_test_catches_wrong_order(tmp_path):
    """Referans test, hatalı sırayı (lower -> translate) gerçekten yakalar."""
    import subprocess
    task = _load_task("edge-turkish-i")
    (tmp_path / "names.py").write_text(
        "_TR = str.maketrans({\n"
        "    'Ç': 'C', 'Ğ': 'G', 'İ': 'I', 'Ö': 'O', 'Ş': 'S', 'Ü': 'U',\n"
        "    'ç': 'c', 'ğ': 'g', 'ı': 'i', 'ö': 'o', 'ş': 's', 'ü': 'u',\n"
        "})\n\n\n"
        "def snake_case(text):\n"
        "    if not text:\n"
        "        return ''\n"
        "    s = str(text).lower().translate(_TR)   # YANLIŞ SIRA\n"
        "    return '_'.join(s.split())\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_turkish_i.py").write_text(task["tests"], encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path, stdin=subprocess.DEVNULL, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert proc.returncode != 0, "hatalı sıra test tarafından yakalanmalıydı"
    assert "istanbul" in (proc.stdout or "")


def test_decision_block_shows_low_confidence_warning():
    import mcp_server
    decision = {
        "decided_by": "Jev (TypeSafe)", "decider_model": "typesafe/jev-1.14",
        "winner_id": 1, "winner_label": "Kurul 1", "winner_model": "m",
        "reason": "seçildi", "confidence": 0.52,
        "low_confidence_warning": "⚠️ Düşük karar güveni: 0.52 < eşik 0.60",
        "comparison": [],
    }
    block = mcp_server._decision_block(decision)
    assert "Karar güveni:** 0.52" in block
    assert "Düşük karar güveni" in block


def test_turkish_pitfall_documented_in_coder_prompt():
    import coder
    assert "U+0307" in coder.SYSTEM
    assert "translate" in coder.SYSTEM


def test_turkish_pitfall_documented_in_council_prompt():
    import council
    assert "U+0307" in council.PROPOSAL_SYSTEM
    assert "translate" in council.PROPOSAL_SYSTEM


def test_eval_task_count_is_21():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
    import importlib
    import tasks_data
    importlib.reload(tasks_data)
    assert len(tasks_data.TASKS) == 21
    ids = [t["id"] for t in tasks_data.TASKS]
    assert len(ids) == len(set(ids)), "görev kimlikleri benzersiz olmalı"
