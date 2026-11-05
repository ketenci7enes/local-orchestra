"""Hakem: Jev (TypeSafe System One, OpenRouter) birincil, LLM yedek.
- Kurul kararı: Jev fail-closed (Jev geçerli karar veremezse görev durur).
- Kod incelemesi (review): fail-closed (hakem yanıt vermezse onay YOK).
- Jev bir chat modeli DEĞİLDİR; `choice` sorusu + `criteria` sözlüğü kullanır ve
  yanıtı `body["answers"][qid]["choice"]` biçiminde nesne olarak döner."""
import json
import os
import re

import requests

from llm import LLMError, parse_json
from tracker import tracker

# Jev, OpenRouter üzerinden sunulur. FAZ 5/D2 smoke testi çalışan endpoint'i sabitler.
# Birincil: resmi tutorial'da curl ile dokümante edilen düz gövde endpoint'i.
JEV_URL = "https://openrouter.ai/api/alpha/decisions"
# Yedek: TypeSafe SDK yolu (çıplak HTTP gövdesi dokümante değil).
JEV_FALLBACK_URL = "https://openrouter.ai/api/v1/systemone"
JEV_MODEL = "typesafe/jev-1.14"

# --- Yakın karar eşikleri: Jev'in hızlı kararı bunların altındaysa derin protokol ---
CLOSE_CONFIDENCE = 0.30   # Jev güveni bunun altındaysa karar "yakın"
CLOSE_PROB_GAP = 0.10     # birinci ve ikinci olasılık farkı bunun altındaysa "yakın"

# Kullanıcıya uyarı gösterilecek güven eşiği (karar geçerli, ama sınırda).
LOW_CONFIDENCE = float(os.getenv("ORCHESTRA_LOW_CONFIDENCE", "0.60"))

# 4 adımlı protokolün Jev'e devredilebilen boyutları (saf Jev: metin yok, sadece oy).
DIMENSIONS = (
    ("kapsam", "Kapsamlılık: problemdeki TÜM gereksinimleri eksiksiz karşılayan öneri hangisi?"),
    ("sadelik", "Sadelik ve verimlilik: gereksiz karmaşadan uzak, en az dış bağımlılık ve varsayımla "
                "sonuca ulaşan öneri hangisi?"),
    ("surdurulebilirlik", "Sürdürülebilirlik: gelecekte değiştirilmeye ve ölçeklenmeye en uygun öneri hangisi?"),
)
FINAL_Q = "Hangi öneri hedef için teknik olarak en güvenli, uygulanabilir ve doğru?"
FINAL_Q_DEEP = (
    "Hangi öneri hedef için teknik olarak en güvenli, uygulanabilir ve doğru? "
    "Boyut puanlarını (kapsamlılık, sadelik, sürdürülebilirlik) birlikte tart. "
    "Puanlar eşitse EŞİTLİK BOZUCU KURAL: (1) en az dış bağımlılık/varsayım, "
    "(2) en sade ve kısa çözüm."
)


class JevUnavailable(Exception):
    """Jev geçerli bir karar veremediğinde fırlatılır (fail-closed)."""


def _slim(proposal: dict) -> dict:
    """Jev state boyutunu (~32 KiB) aşmamak için öneriyi kısaltır."""
    return {
        "id": proposal.get("id"),
        "author": proposal.get("author_label", ""),
        "model": proposal.get("author_model", ""),
        "strategy": (proposal.get("strategy") or "")[:1500],
        "details": (proposal.get("details") or "")[:1500],
        "risks": (proposal.get("risks") or "")[:1500],
    }


def confidence_of(meta: dict):
    """Jev meta'sından güven değeri (yoksa None)."""
    return Judge._confidence(meta or {})


def low_confidence_warning(meta: dict, threshold: float = None) -> str:
    """Güven eşiğin altındaysa insan-okur uyarı metni, aksi halde ''."""
    conf = confidence_of(meta)
    if conf is None:
        return ""
    limit = LOW_CONFIDENCE if threshold is None else float(threshold)
    if conf >= limit:
        return ""
    probs = (meta.get("answer_obj") or {}).get("probabilities") or {}
    dist = ", ".join(f"{k}={v}" for k, v in probs.items()) if probs else "-"
    return (f"⚠️ Düşük karar güveni: {conf:.2f} < eşik {limit:.2f} — seçim sınırda "
            f"(olasılıklar: {dist}). Sonucu gözden geçirin.")


class Judge:
    def __init__(self, llm, use_jev: bool = True, decide_with_jev: bool = False):
        self.llm = llm
        # Jev artık OpenRouter üzerinden çalışır: OPENROUTER_API_KEY kullanılır.
        self.key = os.getenv("OPENROUTER_API_KEY") if use_jev else None
        self.decide_with_jev = decide_with_jev
        # Karar şeffaflığı: son Jev çağrısının ham yanıtı (hangi uç, hangi güven, ham gövde).
        self.last_jev: dict | None = None
        self.last_jev_multi: dict | None = None

    @staticmethod
    def _parse_answer(body: dict, qid: str):
        """OpenRouter Jev yanıtı nesnedir: answers[qid].choice (uyumluluk: .value).
        Yanlış şema (düz string vb.) geçersiz sayılır → None."""
        ans = body.get("answers", {}).get(qid)
        if not isinstance(ans, dict):
            return None
        ans = ans.get("choice", ans.get("value"))
        if ans is None:
            return None
        ans = str(ans).strip()
        return ans or None

    def _jev(self, state: dict, qid: str, instructions: str, criteria: dict):
        """Jev'e `choice` sorusu sorar. Geçerli yanıt yoksa None döner.

        `criteria`: {etiket: açıklama} sözlüğüdür (choices listesi DEĞİL)."""
        if not self.key:
            return None
        payload = {
            "model": JEV_MODEL,
            "state": state,
            "questions": {
                qid: {"type": "choice", "instructions": instructions, "criteria": criteria}
            },
        }
        for url in (JEV_URL, JEV_FALLBACK_URL):
            try:
                r = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=45,
                )
                r.raise_for_status()
                body = r.json()
                ans_obj = body.get("answers", {}).get(qid)
                ans = self._parse_answer(body, qid)
                tracker.add_jev(len(json.dumps(payload, ensure_ascii=False)))
                tracker.log("jev", url=url, qid=qid, answer=ans, raw=str(body)[:500])
                if ans is not None:
                    # Şeffaflık: kararın hangi uçtan, hangi ham yanıtla geldiğini sakla.
                    self.last_jev = {"url": url, "answer": ans, "answer_obj": ans_obj, "raw": body}
                    return ans
            except Exception as e:  # ağ, HTTP, JSON: hepsi "yanıt yok" demektir
                tracker.log("jev_error", url=url, error=str(e))
        return None

    def _jev_multi(self, state: dict, questions: dict) -> dict:
        """Tek çağrıda birden fazla `choice` sorusu sorar; {qid: yanıt} döndürür.

        Jev'in derin protokolü buradan yürür: 3 boyut tek istekte puanlanır.
        """
        if not self.key:
            return {}
        payload = {"model": JEV_MODEL, "state": state, "questions": questions}
        for url in (JEV_URL, JEV_FALLBACK_URL):
            try:
                r = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=45,
                )
                r.raise_for_status()
                body = r.json()
                answers = body.get("answers") or {}
                ok = {
                    qid: a for qid, a in answers.items()
                    if isinstance(a, dict) and (a.get("choice") or a.get("value")) is not None
                }
                tracker.add_jev(len(json.dumps(payload, ensure_ascii=False)))
                tracker.log("jev_multi", url=url, qids=list(ok), raw=str(body)[:800])
                if ok:
                    self.last_jev_multi = {"url": url, "answers": ok, "raw": body}
                    return ok
            except Exception as e:
                tracker.log("jev_error", url=url, error=str(e))
        return {}

    @staticmethod
    def _confidence(meta: dict):
        obj = (meta or {}).get("answer_obj")
        if isinstance(obj, dict):
            c = obj.get("confidence")
            try:
                return float(c)
            except (TypeError, ValueError):
                return None
        return None

    @staticmethod
    def _prob_gap(meta: dict) -> float:
        """En yüksek iki olasılık arasındaki fark (belirsizlik ölçüsü)."""
        obj = (meta or {}).get("answer_obj")
        probs = (obj or {}).get("probabilities") or {}
        vals = sorted((float(v) for v in probs.values() if isinstance(v, (int, float))), reverse=True)
        if len(vals) < 2:
            return 1.0
        return vals[0] - vals[1]

    @classmethod
    def _is_close(cls, meta: dict) -> bool:
        """Karar 'yakın' mı? (düşük güven veya birinci-ikinci olasılık farkı küçük)"""
        conf = cls._confidence(meta)
        if conf is None:
            return False  # güven bilgisi yoksa hızlı yolu bozma
        return conf < CLOSE_CONFIDENCE or cls._prob_gap(meta) < CLOSE_PROB_GAP

    @staticmethod
    def _dep_count(text: str) -> int:
        """Metinde geçen dış bağımlılık izlerini sayar (eşitlik bozucu kural 1)."""
        t = text or ""
        patterns = (
            r"pip\s+install",
            r"\bimport\s+[A-Za-z_]",
            r"\bfrom\s+[A-Za-z_][\w.]*\s+import\b",
            r"\brequirements\.txt\b",
        )
        return sum(len(re.findall(p, t)) for p in patterns)

    def _deterministic_tiebreak(self, proposals: list, ids: list) -> dict:
        """Jev hâlâ kararsızsa eşitlik bozucu kuralları KOD uygular.

        Kural 1: en az dış bağımlılık/varsayım (import/pip izleri).
        Kural 2: en sade ve kısa çözüm (dokunulan dosya sayısı, sonra metin uzunluğu).
        """
        def cost(p):
            text = f"{p.get('strategy', '')}\n{p.get('details', '')}\n{p.get('risks', '')}"
            return (
                self._dep_count(text),
                len(p.get("files") or []),
                len(f"{p.get('strategy', '')}{p.get('details', '')}"),
            )

        ranked = sorted(proposals, key=cost)
        best = ranked[0]
        a, b = cost(best), cost(ranked[1]) if len(ranked) > 1 else (None, None, None)
        rule = "kural-1 (en az dış bağımlılık)" if a[0] != b[0] else "kural-2 (en sade/kısa çözüm)"
        tracker.log("jev_tiebreak", winner_id=best.get("id"), rule=rule, costs=[cost(p) for p in ranked])
        return {"winner": str(best.get("id")), "rule": rule}

    def _deep_evaluate(self, state: dict, criteria: dict, proposals: list, ids: list) -> dict | None:
        """Yakın karar → 4 adımlı protokolün Jev tarafı (3 boyut + nihai oylama).

        Metin üretilmez; yalnızca oy, olasılık ve güven döner.
        """
        questions = {
            dim: {"type": "choice", "instructions": txt, "criteria": criteria}
            for dim, txt in DIMENSIONS
        }
        answers = self._jev_multi(state, questions)
        if not answers:
            return None

        scores = {}
        for qid, a in answers.items():
            choice = str(a.get("choice", a.get("value"))).strip()
            probs = a.get("probabilities") or {}
            scores[qid] = {
                "choice": choice,
                "probabilities": {k: round(float(v), 3) for k, v in probs.items()
                                  if isinstance(v, (int, float))},
                "confidence": a.get("confidence"),
            }

        # Nihai oylama: boyut sonuçları state'e eklenir (Jev kendi puanlarını görsün).
        summary = {qid: {"kazanan": v["choice"], "olasilik": v["probabilities"]} for qid, v in scores.items()}
        state2 = {**state, "boyut_puanlari": summary}
        ans = self._jev(state2, "nihai", FINAL_Q_DEEP, criteria)
        final_meta = self.last_jev or {}
        tie = None
        if ans not in ids:
            return None
        if self._is_close(final_meta):
            tb = self._deterministic_tiebreak(proposals, ids)
            tie = tb["rule"]
            ans = tb["winner"]
        return {
            "triggered": True,
            "scores": scores,
            "final": {
                "choice": ans,
                "probabilities": (final_meta.get("answer_obj") or {}).get("probabilities") or {},
                "confidence": (final_meta.get("answer_obj") or {}).get("confidence"),
            },
            "tie_break": tie,
            "meta": final_meta,
        }

    @staticmethod
    def _jev_reason(meta: dict, winner: dict) -> str:
        """Jev'in seçimini insan-okur biçimde gerekçelendirir (ham yanıttan)."""
        obj = meta.get("answer_obj")
        parts = [f"Jev, öneri {winner.get('id')} numaralı çözümü seçti"]
        if isinstance(obj, dict):
            conf = obj.get("confidence")
            if conf is not None:
                parts.append(f"güven={conf}")
            for key in ("reason", "rationale", "why", "explanation"):
                if obj.get(key):
                    parts.append(str(obj[key]))
                    break
        if len(parts) == 1:
            parts.append(f"ham yanıt: {str(meta.get('raw'))[:300]}")
        return " | ".join(parts)

    def _llm_judge(self, question: str, content: str, choices: list, role: str = "judge"):
        sys_msg = (
            "Bağımsız ve titiz bir hakemsin. SADECE JSON döndür: "
            '{"answer": "' + "|".join(choices) + '", "reason": "kısa gerekçe"}'
        )
        msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": f"{question}\n\n{content}"}]
        try:
            data = parse_json(self.llm.chat(role, msgs, json_mode=True))
        except (LLMError, ValueError):
            return None, ""
        ans = str(data.get("answer", "")).strip()
        return (ans if ans in choices else None), str(data.get("reason", ""))

    def decide(self, goal: str, proposals: list):
        """Kurul önerilerinden kazananı seçer.

        Döndürür: (kazanan_öneri, karar_veren_etiket, karar_veren_model, gerekçe)
        Jev fail-closed: geçerli karar veremezse `JevUnavailable` fırlatır.
        Tek öneri varsa karar gerektirmez (çağrı yapılmaz)."""
        ids = [str(o["id"]) for o in proposals]
        q = ("Hangi öneri hedef için teknik olarak en güvenli, uygulanabilir ve doğru? "
             "Diğer önerilerin belirttiği geçerli riskler bu öneride GİDERİLMİŞ olmalı; "
             "giderilmemiş ciddi bir risk taşıyan öneriyi seçme.")

        # Karar 6: tek öneri = kararsızlık yok (Jev/LLM'e sorulmaz).
        if len(proposals) == 1:
            tracker.log("decision_skipped_single_proposal", proposal_id=proposals[0].get("id"))
            return (
                proposals[0],
                "karar gerektirmedi (tek öneri)",
                "-",
                "Kurul tek öneri üretti.",
            )

        if self.decide_with_jev:
            state = {"goal": (goal or "")[:4000], "proposals": [_slim(p) for p in proposals]}
            # Kriterlerde yalnızca strateji değil RİSKLER de yer alır: Jev, bir öneriyi
            # seçerken diğer üyelerin işaret ettiği risklerin giderilip giderilmediğini
            # değerlendirebilsin (yoksa risk uyarıları karara hiç girmez).
            criteria = {
                str(o["id"]): (
                    f"Öneri {o['id']} — {o.get('author_label', '')} (`{o.get('author_model', '')}`): "
                    f"{(o.get('strategy') or '')[:1200]}\n"
                    f"Belirtilen riskler: {((o.get('risks') or '').strip() or 'belirtilmedi')[:800]}"
                )
                for o in proposals
            }
            ans = self._jev(state, "best_option_id", q, criteria)
            if ans not in ids:
                raise JevUnavailable(f"Jev geçerli bir öneri seçemedi (yanıt: {ans!r})")
            meta = self.last_jev or {}
            deep = None
            # 4 adımlı protokol YALNIZCA yakın kararlarda çalışır (hız/maliyet koruması):
            # düşük güven veya birinci-ikinci olasılık farkı küçükse.
            if len(proposals) > 2 and self._is_close(meta):
                deep = self._deep_evaluate(state, criteria, proposals, ids)
                if deep:
                    ans = deep["final"]["choice"]
                    meta = deep["meta"]
                    tracker.log("jev_deep", winner_id=ans, scores=deep["scores"],
                                tie_break=deep["tie_break"])
            winner = dict(next(o for o in proposals if str(o["id"]) == ans))
            winner["jev_meta"] = meta
            winner["jev_deep"] = deep
            reason = self._jev_reason(meta, winner)
            if deep and deep.get("tie_break"):
                reason += f" | eşitlik bozucu uygulandı: {deep['tie_break']}"
            return winner, "Jev (TypeSafe)", JEV_MODEL, reason

        # Jev kapalı (CLI --no-jev / test): LLM decider yolu.
        body = f"Hedef: {goal}\nKurul önerileri: {json.dumps([_slim(p) for p in proposals], ensure_ascii=False)}"
        ans, reason = self._llm_judge(q, body, ids, role="decider")
        spec = self.llm.cfg["roles"].get("decider", {})
        decider_model = spec.get("models", [{}])[0].get("model", "decider")
        if ans is None:
            # Sessiz sahte atıf YOK: dürüst etiket + log.
            tracker.log("decider_failed", role="decider", reason="decider model yanıt vermedi")
            return (
                proposals[0],
                "karar verilemedi (decider model yanıt vermedi)",
                decider_model,
                "decider yanıt vermedi; ilk öneriyle devam edildi",
            )
        winner = next((o for o in proposals if str(o["id"]) == ans), proposals[0])
        if not reason:
            tracker.log("decider_raw", raw="(sebep yok)")
        decided_by = spec.get("label", "decider")
        return winner, decided_by, decider_model, (reason or "(sebep yok)")

    def choose(self, goal: str, options: list) -> list:
        """Seçenekleri en iyiden başlayarak sıralı döndürür."""
        ids = [str(o["id"]) for o in options]
        q = "Hangi seçenek hedef için teknik olarak en güvenli, uygulanabilir ve doğru?"
        criteria = {str(o["id"]): (o.get("strategy") or "")[:1500] for o in options}
        ans = self._jev({"goal": goal, "options": options}, "best_option_id", q, criteria)
        if ans not in ids:
            ans = None
        if ans is None:
            body = f"Hedef: {goal}\nSeçenekler: {json.dumps(options, ensure_ascii=False)}"
            ans, _ = self._llm_judge(q, body, ids)
        best = [o for o in options if str(o["id"]) == ans]
        return best + [o for o in options if o not in best]

    def precheck(self, goal: str, option: dict) -> bool:
        """Plan kodlayıcıya verilmeden önce net/uygulanabilir mi? Tavsiye niteliğinde: yanıt yoksa geçer."""
        q = "Bu plan hedefe ulaşmak için yeterince net, eksiksiz ve uygulanabilir mi?"
        criteria = {"APPROVED": "net, eksiksiz ve uygulanabilir", "REJECTED": "eksik veya uygulanamaz"}
        ans = self._jev({"goal": goal, "plan": option}, "plan_status", q, criteria)
        if ans not in ("APPROVED", "REJECTED"):
            ans = None
        if ans is None:
            body = f"Hedef: {goal}\nPlan: {json.dumps(option, ensure_ascii=False)}"
            ans, _ = self._llm_judge(q, body, ["APPROVED", "REJECTED"])
        return ans != "REJECTED"

    def review(self, goal: str, diff: str):
        """(durum, gerekçe) döner; durum: APPROVED | REJECTED | UNAVAILABLE (fail-closed çağıran tarafta)."""
        q = "Bu değişiklik (diff) hedefi tam karşılıyor mu ve belirgin bir mantık/güvenlik hatası içeriyor mu?"
        criteria = {
            "APPROVED": "Değişiklik hedefi karşılıyor ve belirgin hata yok",
            "REJECTED": "Belirgin mantık/güvenlik hatası var veya hedefi karşılamıyor",
        }
        diff = diff[:20_000]
        ans = self._jev({"goal": (goal or "")[:4000], "diff": diff}, "code_status", q, criteria)
        if ans not in ("APPROVED", "REJECTED"):
            ans = None
        reason = ""
        body = f"Hedef: {goal}\n\nDiff:\n{diff}"
        if ans is None:
            ans, reason = self._llm_judge(q, body, ["APPROVED", "REJECTED"])
        elif ans == "REJECTED":
            _, reason = self._llm_judge("Bu değişikliğin reddedilme sebebini özetle.", body, ["APPROVED", "REJECTED"])
        if ans == "APPROVED":
            return "APPROVED", ""
        if ans == "REJECTED":
            return "REJECTED", reason or "Hakem değişikliği reddetti."
        return "UNAVAILABLE", "Hakem yanıt veremedi."
