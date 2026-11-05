"""Ana boru hattı: yönlendir → kurul → karar → kodla → doğrula → onayla → yaz → test."""
import os
import time

import coder
import council
import router
import symbols
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

# Onay kapısı: --auto modunda bile kullanıcı onayı isteyen değişiklikler
CONFIRM_MAX_FILES = int(os.getenv("ORCHESTRA_CONFIRM_MAX_FILES", "8"))
SENSITIVE_NAMES = {
    "requirements.txt", "package.json", "package-lock.json", "pnpm-lock.yaml",
    "yarn.lock", "pyproject.toml", "setup.py", "Cargo.toml", "go.mod", "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml", "Makefile", ".gitignore",
    "opencode.json", "mcp_server.py", "config.yaml",
}
SENSITIVE_SUFFIX = {".yml", ".yaml", ".toml", ".ini", ".cfg", ".env"}


def _confirmation_gate(root, staged) -> str:
    """Yıkıcı/hassas değişiklikler için gerekçe metni döndürür (yoksa '')."""
    reasons = []
    if len(staged) > CONFIRM_MAX_FILES:
        reasons.append(f"{len(staged)} dosya birden değişiyor (eşik: {CONFIRM_MAX_FILES})")
    emptied = [rel for rel, content in staged.items() if not content.strip()]
    if emptied:
        reasons.append("boşaltılan/silinecek dosyalar: " + ", ".join(emptied[:5]))
    sensitive = []
    for rel in staged:
        name = rel.rsplit("/", 1)[-1]
        if (name in SENSITIVE_NAMES or name.startswith(".github")
                or rel.startswith(".github/") or rel.startswith(".gitlab-ci")
                or name.endswith(".github") or (os.path.splitext(name)[1].lower() in SENSITIVE_SUFFIX)):
            sensitive.append(rel)
    if sensitive:
        reasons.append("yapılandırma/CI dosyaları: " + ", ".join(sensitive[:5]))
    if not reasons:
        return ""
    return ("⚠️ Onay gerekiyor — " + "; ".join(reasons)
            + "\nDeğişiklik otomatik modda UYGULANMADI. Onaylamak için `dry_run=true` ile "
              "diff'i inceleyip manuel uygulayın veya etkileşimli modu kullanın.")


def attachments_context(attachments) -> str:
    """Ekler (PDF/görsel/metin) için modele verilecek bağlam metni."""
    if not attachments:
        return ""
    parts = ["### EK BELGELER (kullanıcı tarafından eklenen veriler)"]
    used = 0
    for a in attachments[:10]:
        text = (a.get("text") or "").strip()
        if not text:
            continue
        if used + len(text) > 120_000:
            text = text[: max(0, 120_000 - used)]
            parts.append("\n[…ek bütçesi doldu, kalan belgeler kısaltıldı]")
        used += len(text)
        parts.append(
            f"\n--- EK: {a.get('path')} ({a.get('kind')}, yöntem: {a.get('method')}) ---\n"
            f"<<<BELGE_VERISI_BASLANGIC>>>\n{text}\n<<<BELGE_VERISI_SON>>>"
        )
    return "\n".join(parts) if len(parts) > 1 else ""



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
    verification = []   # C: aşama bazlı doğrulama kaydı

    def stage(name, ok, summary=""):
        verification.append({"stage": name, "ok": bool(ok), "summary": str(summary)[:1000]})

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
            changes = coder.parse_changes(raw)
        except ParseError as e:
            feedback = f"Biçim hatası: {e}"
            log(f"   ↩️  {feedback}")
            stage("parse", False, e)
            continue
        raw_warnings = coder.parse_warnings(raw)
        try:
            staged, match_warnings = coder.apply_in_memory(root, changes, collect_warnings=True)
        except (ApplyError, WorkspaceError) as e:
            msg = str(e)
            failed = next((c.path for c in changes if msg.startswith(c.path + ":")),
                          changes[0].path if changes else "")
            feedback = coder.targeted_feedback(root, failed, e)
            log(f"   ↩️  Uygulama hatası ({failed}): {str(e)[:200]}")
            stage("apply", False, e)
            continue
        for w in raw_warnings + match_warnings:
            log(f"   ℹ️  {w}")
            tracker.log("edit_warning", detail=w)
        stage("parse", True, f"{len(staged)} dosya" + (f" | {len(raw_warnings)} uyarı" if raw_warnings else ""))

        problems = verify.static_check(
            staged, root, typecheck=os.getenv("ORCHESTRA_TYPECHECK", "") == "1"
        )
        if problems:
            feedback = "Statik kontrol hataları:\n" + "\n".join(problems)
            log("   ↩️  Statik kontrol başarısız")
            stage("static", False, problems)
            continue
        stage("static", True)

        diff = workspace.make_diff(root, staged)
        status, reason = judge.review(goal, diff)
        tracker.log("review", status=status, reason=reason)
        if status == "REJECTED" or (status == "UNAVAILABLE" and auto):
            feedback = f"Hakem reddetti: {reason}"  # UNAVAILABLE + auto = fail-closed
            log(f"   ↩️  {feedback}")
            stage("review", False, reason)
            continue
        if status == "UNAVAILABLE":
            log("⚠️  Hakem yanıt vermedi; diff'i kendin değerlendireceksin.")
            stage("review", True, "hakem yok (uyarı)")
        else:
            stage("review", True)

        # F: yıkıcı/hassas değişiklik kapısı (auto modda bile)
        gate = _confirmation_gate(root, staged)
        if gate:
            stage("confirmation", False, gate)
            log("   ⛔ " + gate.splitlines()[0])
            return {"ok": False, "reason": gate, "needs_confirmation": True,
                    "files": list(staged), "diff": diff, "verification": verification}

        # A6 + B: geçici kopyada GERÇEK testler; diske hiç yazmadan.
        tmp = None
        try:
            tmp, stage_mode = workspace.stage_to_temp(root, staged)
            tests_ok, tests_out = verify.run_tests(tmp)
        except Exception as e:
            tests_ok, tests_out, stage_mode = False, f"Geçici kopya oluşturulamadı: {e}", "error"
        finally:
            if tmp is not None:
                workspace.cleanup_stage(tmp)

        if not tests_ok:
            feedback = ("Testler geçici kopyada başarısız (diske YAZILMADI):\n"
                        + verify.summarize_failure(tests_out))
            log("   ↩️  Testler başarısız (geçici kopya), diske yazılmadı")
            stage("tests", False, verify.summarize_failure(tests_out))
            continue
        stage("tests", True, f"mod={stage_mode}")

        # A6: dry_run → sıfır disk yazımı, dolu diff, GERÇEK test sonucu
        if dry_run:
            return {"ok": True, "dry_run": True, "files": list(staged), "diff": diff,
                    "tests": tests_out, "tests_ok": tests_ok, "staged_in_temp": True,
                    "verification": verification}

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

        # B: doğrulama geçti → ŞİMDİ gerçek çalışma alanına yaz
        try:
            workspace.write_with_backup(root, staged)
        except Exception as e:
            feedback = f"Yazma hatası: {e}"
            log(f"   ↩️  {feedback}")
            stage("write", False, e)
            continue
        stage("write", True, f"{len(staged)} dosya")
        return {"ok": True, "files": list(staged), "tests": tests_out, "diff": diff,
                "verification": verification}

    if not auto:
        log(f"\n⚠️  {MAX_ATTEMPTS} otomatik deneme tamamlandı ancak görev henüz sonuçlandırılamadı.")
        user_choice = input("Devam etmek için ek talimat girmek ister misiniz? (Enter = Görevi bitir, veya yeni talimat yazın): ").strip()
        if user_choice and user_choice.lower() not in ("q", "quit", "h", "hayır"):
            return _implement(llm, judge, root, f"{goal} (Ek kullanıcı talimatı: {user_choice})", plan, files_ctx, auto, dry_run=dry_run)

    return {"ok": False, "reason": f"{MAX_ATTEMPTS} denemede başarılamadı. Son geri bildirim: {feedback[:500]}",
            "verification": verification}


def run_task(llm, judge, root, goal, files=None, auto=False, git=False, complexity_override=None,
             dry_run=False, attachments=None) -> dict:
    root = root.resolve()
    rmap = workspace.repo_map(root)
    route = router.classify(llm, goal, rmap)
    if files:
        route["files"] = files

    # D: Router'ın dosya seçimini doğrula; ilgisizse anahtar kelimeyle öner
    validated, warn = symbols.validate_selection(root, route["files"], goal)
    if warn:
        log(f"⚠️  {warn}")
        tracker.log("file_selection_weak", detail=warn)
        if not validated:
            validated = symbols.suggest_files(root, goal)
            if validated:
                log(f"   ↪️  Önerilen dosyalar: {', '.join(validated[:5])}")
    route["files"] = validated
    if not route["files"]:
        route["files"] = symbols.suggest_files(root, goal, limit=5)
    tracker.log("route", **route)
    log(f"🧭 Tip: {route['type']} | karmaşıklık: {route['complexity']}")
    files_ctx = workspace.read_files(root, route["files"])

    # D: sembol haritası + ilgili gövdeler (bağlam bütçesi: harita → gövde → tam dosya)
    smap = symbols.symbol_map(root, max_files=150, max_chars=8_000)
    bodies = []
    used_bodies = 0
    for rel in route["files"][:3]:
        chunk = symbols.relevant_bodies(root, rel, goal, max_lines=100)
        if not chunk or used_bodies + len(chunk) > 6_000:
            continue
        used_bodies += len(chunk)
        bodies.append(chunk)
    if smap:
        files_ctx = ("### SEMBOL HARİTASI (dosya → tanımlar)\n" + smap +
                     ("\n\n### İLGİLİ GÖVDELER\n" + "\n\n".join(bodies) if bodies else "") +
                     "\n\n" + files_ctx)

    # Ekler (PDF/görsel/metin): kullanıcının açıkça verdiği belgeler. Router
    # sınıflandırması kirlenmesin diye yalnızca içerik bağlamına eklenir.
    attach_ctx = attachments_context(attachments)
    if attach_ctx:
        files_ctx = (files_ctx + "\n\n" + attach_ctx).strip()
        log(f"📎 {len(attachments)} ek belge bağlama eklendi")

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
