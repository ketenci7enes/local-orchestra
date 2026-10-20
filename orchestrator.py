"""Ana boru hattı: yönlendir → kurul → karar → kodla → doğrula → onayla → yaz → test."""
import time

import coder
import council
import router
import verify
import workspace
from coder import ApplyError, ParseError
from console import log
from judge import JevUnavailable
from llm import LLMError, cooldown_wait
from tracker import tracker
from workspace import WorkspaceError

MAX_ATTEMPTS = 3
MAX_COOLDOWN_WAITS = 5
GENERAL_SYSTEM = "Sen yardımsever bir asistansın. Kullanıcının dilinde, net ve doğru yanıt ver."


def _plan_text(plan: dict) -> str:
    if plan.get("detailed_prompt"):
        return plan["detailed_prompt"]
    return f"Strateji: {plan['strategy']}\nDetay: {plan.get('details', '')}\nRiskler: {plan.get('risks', '')}"


def _answer(llm, goal, route, files_ctx) -> str:
    role = "brain" if route["complexity"] == "complex" else "general"
    user = goal if not files_ctx else f"{goal}\n\nİlgili dosyalar:\n{files_ctx}"
    # C1: qa/writing cevapları için makul bir üst sınır.
    return llm.chat(
        role,
        [{"role": "system", "content": GENERAL_SYSTEM}, {"role": "user", "content": user}],
        max_tokens=1500,
    )


def _make_plan(llm, judge, goal, route, context) -> dict:
    simple = {"strategy": goal, "details": "", "risks": ""}
    if route["complexity"] != "complex":
        return simple

    # 1) KURUL: 3 bağımsız kıdemli yazılım uzmanı ayrı ayrı öneri üretir
    log("👥 Kurul fikir üretiyor (3 bağımsız kıdemli yazılım uzmanı)...")
    proposals = council.generate_proposals(llm, goal, context)
    if not proposals:
        log("⚠️  Öneri üretilemedi, görev doğrudan uygulanacak.")
        return simple

    if len(proposals) == 1:
        log("⚠️  Kurul tek öneri üretti; karar aşaması atlanıyor (tek öneri = karar gerektirmez).")
    else:
        log("⚖️  Jev (TypeSafe) kurul kararını veriyor...")

    # 2) KARAR: Jev fail-closed (tek öneride karar atlanır → judge.decide içinde)
    winner, decided_by, decider_model, reason = judge.decide(goal, proposals)

    comparison = [
        {
            "id": p.get("id"),
            "author_role": p.get("author_role"),
            "author_label": p.get("author_label"),
            "author_model": p.get("author_model"),
            "strategy": p.get("strategy"),
            "details": p.get("details"),
            "files": p.get("files") or [],
            "risks": p.get("risks"),
            "fell_back": bool(p.get("fell_back")),
            "duration_s": p.get("duration_s"),
        }
        for p in proposals
    ]
    tracker.log(
        "decision",
        decided_by=decided_by,
        decider_model=decider_model,
        winner_id=winner.get("id"),
        winner_author=winner.get("author_role"),
        winner_label=winner.get("author_label"),
        winner_model=winner.get("author_model"),
        reason=reason,
        jev_meta=winner.get("jev_meta"),
        jev_deep=winner.get("jev_deep"),
        comparison=comparison,
    )
    log(f"🎯 Jev'in seçimi: Öneri {winner.get('id')} — {winner.get('author_label')} "
        f"(`{winner.get('author_model')}`) | Karar veren: {decided_by}")
    log(f"💬 Gerekçe: {reason or '(sebep belirtilmedi)'}")

    # 3) Kazanan üye, kıdemli uzman personaasıyla coder için detaylı promptu yazar
    log(f"✍️  {winner.get('author_label')} coder için detaylı prompt yazıyor...")
    winner["detailed_prompt"] = council.expand_prompt(llm, winner, goal, context)
    winner["decided_by"] = decided_by
    winner["decider_model"] = decider_model
    winner["decision_reason"] = reason
    winner["comparison"] = comparison
    return winner


def _decision_of(plan: dict) -> dict | None:
    """Plan kurul kararı içeriyorsa, karar sahibi bilgisini ayıklar."""
    if not plan.get("decided_by"):
        return None
    return {
        "decided_by": plan.get("decided_by"),
        "decider_model": plan.get("decider_model"),
        "winner_id": plan.get("id"),
        "winner_author": plan.get("author_role"),
        "winner_label": plan.get("author_label"),
        "winner_model": plan.get("author_model"),
        "reason": plan.get("decision_reason"),
        "jev_meta": plan.get("jev_meta"),
        "jev_deep": plan.get("jev_deep"),
        "comparison": plan.get("comparison", []),
    }


def _implement(llm, judge, root, goal, plan, files_ctx, auto, dry_run=False) -> dict:
    feedback = ""
    attempts_done = 0
    cooldown_waits = 0

    # A5: `for` yerine `while` + ayrı sayaçlar → "attempt harcamadan devam" mümkün.
    while attempts_done < MAX_ATTEMPTS:
        log(f"✍️  Alt coder yazıyor (deneme {attempts_done + 1}/{MAX_ATTEMPTS})...")
        try:
            raw = coder.generate(llm, _plan_text(plan), files_ctx, feedback)
        except LLMError as e:
            # A5: 429 gibi durumlarda crash yok; cooldown varsa bekle ve aynı denemeyi tekrarla.
            wait = cooldown_wait(llm, "coder")
            if wait > 0 and cooldown_waits < MAX_COOLDOWN_WAITS:
                cooldown_waits += 1
                sleep_for = min(wait, 65)
                log(f"   ⏳ Coder kota sınırı. {sleep_for:.0f}s bekleniyor, deneme harcanmadan tekrarlanacak "
                    f"({cooldown_waits}/{MAX_COOLDOWN_WAITS})...")
                tracker.log("coder_cooldown", wait=round(sleep_for, 1), error=str(e)[:300])
                time.sleep(sleep_for)
                continue
            return {"ok": False, "reason": f"Coder modeli yanıt veremedi: {e}"}

        attempts_done += 1
        tracker.log("coder_raw", attempt=attempts_done, raw=raw[:6000])

        try:
            staged = coder.apply_in_memory(root, coder.parse_changes(raw))
        except (ParseError, ApplyError, WorkspaceError) as e:
            feedback = f"Biçim/uygulama hatası: {e}"
            log(f"   ↩️  {feedback}")
            continue

        problems = verify.static_check(staged)
        if problems:
            feedback = "Statik kontrol hataları:\n" + "\n".join(problems)
            log("   ↩️  Statik kontrol başarısız")
            continue

        diff = workspace.make_diff(root, staged)
        status, reason = judge.review(goal, diff)
        tracker.log("review", status=status, reason=reason)
        if status == "REJECTED" or (status == "UNAVAILABLE" and auto):
            feedback = f"Hakem reddetti: {reason}"  # UNAVAILABLE + auto = fail-closed
            log(f"   ↩️  {feedback}")
            continue
        if status == "UNAVAILABLE":
            log("⚠️  Hakem yanıt vermedi; diff'i kendin değerlendireceksin.")

        # A6: dry-run → diske YAZMADAN, testleri ÇALIŞTIRMADAN diff'i döndür.
        if dry_run:
            return {"ok": True, "dry_run": True, "files": list(staged), "diff": diff}

        if not auto:
            log("\n" + diff)
            user_choice = input("\nBu değişiklik uygulansın mı? [e: Evet / h: Hayır / y: Yeni talimat ver / q: Görevi bitir]: ").strip().lower()
            if user_choice in ("q", "quit", "cikis", "çıkış"):
                return {"ok": False, "reason": "kullanıcı görevi sonlandırdı"}
            elif user_choice in ("y", "yeni", "düzelt", "duzelt") or user_choice not in ("e", "evet"):
                if user_choice in ("y", "yeni", "düzelt", "duzelt"):
                    user_note = input("💬 Modele iletilecek yeni talimat veya düzeltme notunuz: ").strip()
                else:
                    user_note = input("💬 Değişiklik reddedildi. Modele ne yapmasını istersiniz? (boş = varsayılan ret): ").strip()
                feedback = f"Kullanıcı değişikliği beğenmedi/reddetti. Kullanıcı talimatı: {user_note or 'Lütfen farklı ve daha doğru bir çözüm üret.'}"
                log("   ↩️  Geri bildiriminiz coder modeline aktarılıyor...")
                continue

        snapshot = workspace.write_with_backup(root, staged)
        ok, out = verify.run_tests(root)
        if not ok:
            workspace.rollback(root, snapshot)
            feedback = f"Testler başarısız (değişiklik geri alındı):\n{out}"
            log("   ↩️  Testler başarısız, geri alındı")
            continue
        return {"ok": True, "files": list(staged), "tests": out, "diff": diff}

    if not auto:
        log(f"\n⚠️  {MAX_ATTEMPTS} otomatik deneme tamamlandı ancak görev henüz sonuçlandırılamadı.")
        user_choice = input("Devam etmek için ek talimat girmek ister misiniz? (Enter = Görevi bitir, veya yeni talimat yazın): ").strip()
        if user_choice and user_choice.lower() not in ("q", "quit", "h", "hayır"):
            return _implement(llm, judge, root, f"{goal} (Ek kullanıcı talimatı: {user_choice})", plan, files_ctx, auto, dry_run=dry_run)

    return {"ok": False, "reason": f"{MAX_ATTEMPTS} denemede başarılamadı. Son geri bildirim: {feedback[:500]}"}


def run_task(llm, judge, root, goal, files=None, auto=False, git=False, complexity_override=None,
             dry_run=False) -> dict:
    root = root.resolve()
    rmap = workspace.repo_map(root)
    route = router.classify(llm, goal, rmap)
    if files:
        route["files"] = files
    tracker.log("route", **route)
    log(f"🧭 Tip: {route['type']} | karmaşıklık: {route['complexity']}")
    files_ctx = workspace.read_files(root, route["files"])

    # A1: override kısayoldan ÖNCE uygulanır; qa/writing yalnız karmaşık DEĞİLSE kısa yoldan döner.
    if complexity_override in ("simple", "complex"):
        route["complexity"] = complexity_override
    if route["type"] in ("qa", "writing") and route["complexity"] != "complex":
        return {"ok": True, "answer": _answer(llm, goal, route, files_ctx)}

    context = "Dosya listesi:\n" + "\n".join(rmap) + "\n\n" + files_ctx
    try:
        plan = _make_plan(llm, judge, goal, route, context)
    except JevUnavailable as e:
        tracker.log("jev_unavailable", error=str(e))
        return {"ok": False, "reason": "Jev karar veremedi (fail-closed)"}
    decision = _decision_of(plan)
    result = _implement(llm, judge, root, goal, plan, files_ctx, auto, dry_run=dry_run)
    if decision:
        result["decision"] = decision
    if dry_run and result.get("ok"):
        # A6: dry-run diff'i _implement'tan gelir (staged zaten ölçülmüştür).
        return {"ok": True, "dry_run": True, "diff": result.get("diff", ""),
                "files": result.get("files", []), "decision": decision}
    if result["ok"] and git:
        workspace.git_commit(root, result["files"], f"orchestra: {goal[:60]}")
    return result
