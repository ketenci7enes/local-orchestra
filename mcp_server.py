"""
local-orchestra → OpenCode MCP Server (stdio transport)
========================================================
Bu dosya orkestrayı OpenCode'un anlayacağı MCP protokolüne çevirir.

NASIL ÇALIŞIR?
--------------
1. OpenCode, opencode.json'daki tanıma bakarak bu scripti bir subprocess olarak başlatır:
       python mcp_server.py

2. OpenCode ile aramızda iletişim stdin/stdout üzerinden JSON-RPC 2.0 mesajlarıyla olur:
       OpenCode  ──(stdin)──►  mcp_server.py  ──(stdout)──►  OpenCode
   HTTP server ya da port açmak gerekmez.

3. Server başladığında OpenCode "hangi araçlar var?" diye sorar (tools/list).
   Server mevcut 11 aracı listeler.

4. Kullanıcı OpenCode chat'ine "login sayfası yap" yazdığında OpenCode,
   orchestra_run_task aracını argümanlarla çağırır (tools/call).

5. mcp_server.py içindeki araç fonksiyonu:
       router → (kurul: Nemotron+DeepSeek) → (karar: GLM) → coder → verify → write → diff
   zincirini çalıştırır ve sonucu OpenCode'a text olarak döndürür.
   Karmaşık görevlerde sonuç metnine "🧑‍⚖️ Karar veren" özeti eklenir.

6. Kullanıcı orkestradan bağımsız, --auto modunda çalışır (onay istenmez);
   yapılan değişikliklerin diff'i doğrudan OpenCode ekranında görünür.

ARAÇLAR (Tools)
---------------
  orchestra_run_task     → Orkestra boru hattını çalıştır, dosyaları değiştir
  orchestra_ping         → Tüm modelleri test et, hangisi çalışıyor göster
  orchestra_status       → Oturumun token ve çağrı istatistiğini göster
  orchestra_read_file    → Çalışma alanındaki bir dosyanın içeriğini oku
  orchestra_apply_edit   → Tek bir SEARCH/REPLACE düzenlemesi uygula (LLM'siz, anında)
  orchestra_write_file   → Yeni dosya yaz veya tamamen değiştir (LLM'siz, anında)
  orchestra_list_files   → Proje dosya ağacını listele
  orchestra_search       → Kod içinde regex arama yap
  orchestra_run_tests    → Proje testlerini çalıştır
  orchestra_git          → Git status/diff/commit işlemleri
  orchestra_review       → Sadece hakem incelemesi yap (uygulamaz)
"""

import argparse
import json
import os
import sys
import anyio
from pathlib import Path

# Windows konsolunda Türkçe/emoji için
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# orkestra modüllerinin bulunduğu dizini ekle
sys.path.insert(0, str(Path(__file__).parent))

# ---- MCP SDK import ----
# FastMCP: en sade MCP server tanımlama yöntemi.
# @mcp.tool() ile decore ettiğimiz her async fonksiyon otomatik olarak
# bir MCP aracına dönüşür; argümanlar Python type annotation'dan çıkarılır.
from mcp.server.fastmcp import FastMCP

# ---- orkestra modülleri ----
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import orchestrator
import workspace as ws
import coder
from judge import Judge
from llm import LLMClient
from tracker import tracker

# ---- Argüman: çalışma alanı klasörü ----
# OpenCode, opencode.json'daki "cwd" alanından çalışma dizinini belirler.
# Geçirilmezse server'ın başlatıldığı dizin (cwd) kullanılır.
_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--workspace", default=".")
_args, _ = _ap.parse_known_args()
WORKSPACE = Path(_args.workspace).resolve()

# ---- LLM ve Hakem nesneleri (tek seferlik oluşturulur) ----
# LLMClient config.yaml'ı okur; .env'den API keyleri yükler.
# Judge: Jev AI anahtarı .env'de yoksa otomatik LLM hakeme geçer.
_llm = LLMClient()
_judge = Judge(_llm, use_jev=True, decide_with_jev=True)

# ---- MCP Server tanımı ----
# "local-orchestra" → OpenCode'un araçlar listesinde görünecek isim
mcp = FastMCP("local-orchestra")


def _clip(text, limit: int = 1500) -> str:
    """Uzun alanları kısaltır (MCP yanıtı 10 MB sınırını aşmasın)."""
    s = str(text or "").replace("\r", "")
    return s if len(s) <= limit else s[:limit] + " …(kısaltıldı)"


def _decision_block(decision) -> str:
    """Kurul kararının tam dökümünü döndürür: 3 önerinin tamamı + Jev'in nasıl seçtiği."""
    if not decision:
        return ""
    winner_id = decision.get("winner_id")
    winner = decision.get("winner_label") or decision.get("winner_author") or "?"
    winner_model = decision.get("winner_model") or "?"
    decided_by = decision.get("decided_by") or "?"
    decider_model = decision.get("decider_model") or "?"
    lines = []

    comparison = decision.get("comparison") or []
    if comparison:
        lines.append("")
        lines.append("### 🧩 Kurulun tam öneri dökümü (3 bağımsız kıdemli yazılım uzmanı)")
        for i, c in enumerate(comparison, 1):
            pid = c.get("id", i)
            label = c.get("author_label") or "?"
            model = c.get("author_model") or "?"
            mark = " ✅ **SEÇİLDİ**" if str(pid) == str(winner_id) else ""
            lines.append("")
            dur = c.get("duration_s")
            dur_txt = f" — {dur}s" if dur is not None else ""
            lines.append(f"#### Öneri {pid} — {label} (`{model}`){dur_txt}{mark}")
            if c.get("fell_back"):
                lines.append("- ⚠️ **Not:** Birincil model yanıt vermedi; bu öneriyi **yedek model** üretti.")
            lines.append(f"- **Strateji:** {_clip(c.get('strategy'), 600)}")
            lines.append(f"- **Detay:** {_clip(c.get('details'))}")
            files = c.get("files") or []
            if files:
                lines.append(f"- **Dokunulacak dosyalar:** {', '.join(map(str, files))}")
            lines.append(f"- **Riskler:** {_clip(c.get('risks'), 600)}")

    lines.append("")
    lines.append("### ⚖️ Jev'in kararı")
    lines.append(f"- **Karar veren:** {decided_by} (`{decider_model}`)")

    deep = decision.get("jev_deep") or {}
    if deep.get("scores"):
        dims = [("kapsam", "Kapsamlılık"), ("sadelik", "Sadelik"), ("surdurulebilirlik", "Sürdürülebilirlik")]
        lines.append("")
        lines.append("### 📊 Çok boyutlu puanlama (yakın karar → derin protokol)")
        lines.append("| Öneri | " + " | ".join(label for _, label in dims) + " |")
        lines.append("|---" * (len(dims) + 1) + "|")
        row_ids = [str(c.get("id")) for c in comparison] or ["1", "2", "3"]
        for pid in row_ids:
            cells = []
            for key, _ in dims:
                probs = (deep["scores"].get(key) or {}).get("probabilities") or {}
                p = probs.get(pid)
                cells.append(f"{float(p) * 10:.1f}" if isinstance(p, (int, float)) else "-")
            lines.append(f"| {pid} | " + " | ".join(cells) + " |")
        for key, label in dims:
            s = deep["scores"].get(key) or {}
            if s.get("choice"):
                lines.append(f"- **{label} kazananı:** Öneri {s['choice']} (güven: {s.get('confidence')})")
        fin = deep.get("final") or {}
        if fin:
            lines.append(f"- **Nihai oylama:** Öneri {fin.get('choice')} "
                         f"(olasılıklar: {fin.get('probabilities')}, güven: {fin.get('confidence')})")
        lines.append(f"- **Eşitlik bozucu:** {deep.get('tie_break') or 'uygulanmadı'}")

    lines.append(f"- **Seçilen öneri:** Öneri {winner_id} — {winner} (`{winner_model}`)")
    lines.append(f"- **Jev'in gerekçesi:** {_clip(decision.get('reason') or '(sebep yok)', 800)}")
    meta = decision.get("jev_meta") or {}
    if meta:
        lines.append(f"- **Kullanılan uç:** {meta.get('url', '?')}")
        obj = meta.get("answer_obj")
        if obj is not None:
            lines.append(f"- **Jev ham yanıtı:** `{_clip(json.dumps(obj, ensure_ascii=False), 600)}`")
    lines.append("")
    lines.append("✍️ **Coder'a detaylı promptu yazan:** " + str(winner) + f" (`{winner_model}`)")
    return "\n".join(lines) + "\n"


# =============================================================================
# ARAÇ 1: orchestra_run_task
# =============================================================================
@mcp.tool()
async def orchestra_run_task(goal: str, files: str = "", complexity_hint: str = "auto", dry_run: bool = False) -> str:
    """
    Orkestranın tam boru hattını çalıştırır:
    Sınıflandır → Kurul (3 bağımsız kıdemli yazılım uzmanı) → Jev kararı → Kod yaz → Syntax kontrol → Hakem onayla → Diske yaz

    Parametreler
    ------------
    goal            : Yapılacak işin açık tanımı.
                      Örn: "index.html'e hero section ekle ve style.css'i güncelle"
    files           : Bağlam olarak verilecek dosya yolları (boşlukla ayrılmış, göreli).
                      Boş bırakılırsa Router modeli otomatik seçer.
    complexity_hint : "simple" | "complex" | "auto"
                      "complex" → Kurul (3 üye) öneri üretir, Jev seçer.
                      "simple"  → Doğrudan Coder'a gider, hızlıdır.
                      "auto"    → Router modeli karar verir.
    dry_run         : True ise değişiklikleri diske yazmaz, sadece diff önizlemesi döndürür.

    Döndürür
    --------
    Uygulanan değişikliklerin birleşik diff'i + kurulun TÜM önerileri + Jev'in karar gerekçesi
    (+ ham Jev yanıtı) + özet rapor.
    """
    file_list = files.split() if files.strip() else []

    # complexity_hint varsa, router sonucunu override edeceğiz
    override_complexity = complexity_hint if complexity_hint in ("simple", "complex") else None

    try:
        # Bloklayan işi thread pool'da çalıştır (event loop bloke olmasın)
        result = await anyio.to_thread.run_sync(
            lambda: orchestrator.run_task(
                _llm, _judge, WORKSPACE,
                goal=goal,
                files=file_list,
                auto=True,           # MCP modunda onay sormadan uygula
                git=False,
                complexity_override=override_complexity,
                dry_run=dry_run,
            )
        )
    except Exception as e:
        return f"❌ Orkestra hatası: {type(e).__name__}: {e}"

    # ---- Sonuç formatlama ----
    if "answer" in result:
        # qa / writing tipi: cevap metni döner
        return result["answer"]

    if result.get("ok"):
        decision_block = _decision_block(result.get("decision"))
        if result.get("dry_run"):
            return f"🔍 Dry-run diff önizlemesi:\n{decision_block}\n{result.get('diff', '(diff yok)')}"

        changed = result.get("files", [])
        diff = result.get("diff", "")
        # Değişen dosyaların diff'ini döndür (içerik yerine)
        lines = [f"✅ Başarıyla uygulandı — {len(changed)} dosya değişti\n"]
        if decision_block:
            lines.append(decision_block)
        lines.append(f"📁 Değişen dosyalar: {', '.join(changed)}\n")
        if diff:
            lines.append(f"\n---\n### Diff\n```diff\n{diff}\n```")

        # Token raporu
        total_p = sum(s["prompt"] for s in tracker.llm.values())
        total_c = sum(s["completion"] for s in tracker.llm.values())
        total_calls = sum(s["calls"] for s in tracker.llm.values())
        lines.append(f"\n---\n📊 Token: {total_p} prompt + {total_c} completion | {total_calls} çağrı")
        return "\n".join(lines)
    else:
        reason = result.get("reason", "bilinmeyen hata")
        return f"❌ Görev başarısız: {reason}"


# =============================================================================
# ARAÇ 2: orchestra_ping
# =============================================================================
@mcp.tool()
async def orchestra_ping() -> str:
    """
    config.yaml'daki tüm model adaylarına küçük bir istek göndererek
    hangileri çalışıyor, hangilerinde hata var gösterir.
    Kullanmadan önce modellerin erişilebilir olup olmadığını doğrulamak için çalıştırın.
    """
    import time
    lines = ["## Model Sağlık Kontrolü\n"]
    for role, spec in _llm.cfg["roles"].items():
        for cand in spec["models"]:
            t0 = time.time()
            try:
                # Gerçek kontrol: boş yanıt / max_tokens'ta kesilme de hata sayılır.
                # (Eskiden sadece HTTP 200'e bakılıyordu → ölü model "✅" görünüyordu.)
                resp = _llm._client(cand["provider"]).chat.completions.create(
                    model=cand["model"],
                    messages=[{"role": "user", "content": 'Sadece su JSON ile yanit ver: {"ok": true}'}],
                    max_tokens=256,
                    timeout=60,
                )
                dt = time.time() - t0
                choice = resp.choices[0]
                text = (getattr(choice.message, "content", None) or "").strip()
                finish = getattr(choice, "finish_reason", None)
                if not text:
                    status = f"❌ boş yanıt (finish={finish}, {dt:.1f}s)"
                elif finish == "length":
                    status = f"⚠️ max_tokens'ta kesildi ({dt:.1f}s) — çıktı bütçesi artırılmalı"
                else:
                    status = f"✅ ({dt:.1f}s) {text[:40]}"
            except Exception as e:
                status = f"❌ {type(e).__name__}: {str(e)[:60]}"
            lines.append(f"[**{role}**] `{cand['provider']}/{cand['model'].split('/')[-1]}`: {status}")

    # D2: chat modelleri Jev'i DOĞRULAYAMAZ → tek bir gerçek karar çağrısı dene.
    lines.append(_jev_ping_line())
    return "\n".join(lines)


def _jev_ping_line() -> str:
    """Jev (TypeSafe System One) için tek bir `choice` karar çağrısı yapar."""
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        return "[**judge**] `jev` (typesafe/jev-1.14): ⚠️ OPENROUTER_API_KEY yok, atlandı"
    from judge import JEV_FALLBACK_URL, JEV_MODEL, JEV_URL
    import requests
    payload = {
        "model": JEV_MODEL,
        "state": {"ping": True},
        "questions": {"q": {"type": "choice", "instructions": "Sağlık kontrolü: yanıt ver.",
                            "criteria": {"OK": "çalışıyor", "NO": "çalışmıyor"}}},
    }
    parts = []
    for url in (JEV_URL, JEV_FALLBACK_URL):
        try:
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload, timeout=20,
            )
            if r.status_code < 400:
                try:
                    ans = r.json().get("answers", {}).get("q")
                    choice = ans.get("choice") if isinstance(ans, dict) else ans
                except Exception:
                    choice = None
                parts.append(f"{url} ✅ (HTTP {r.status_code}, choice={choice})")
            else:
                parts.append(f"{url} ❌ HTTP {r.status_code}")
        except Exception as e:
            parts.append(f"{url} ❌ {type(e).__name__}: {str(e)[:60]}")
    return "[**judge**] `jev` (typesafe/jev-1.14): " + " | ".join(parts)


# =============================================================================
# ARAÇ 3: orchestra_status
# =============================================================================
@mcp.tool()
async def orchestra_status() -> str:
    """
    Bu MCP oturumunda yapılan tüm model çağrılarının
    token kullanım istatistiğini döndürür.
    Oturum boyunca ne kadar context tüketildiğini takip etmek için kullanın.
    """
    if not tracker.llm:
        return "Henüz hiç model çağrısı yapılmadı."

    lines = ["## Oturum Token İstatistikleri\n"]
    total_p, total_c, total_calls = 0, 0, 0
    for name, s in tracker.llm.items():
        lines.append(
            f"- `{name}`: **{s['calls']}** çağrı | "
            f"{s['prompt']:,} prompt tkn + {s['completion']:,} completion tkn"
        )
        total_p += s["prompt"]
        total_c += s["completion"]
        total_calls += s["calls"]

    lines.append(f"\n**Toplam:** {total_calls} çağrı | {total_p:,} prompt + {total_c:,} completion token")
    lines.append(f"\n📄 Log: `{tracker.logfile}`")
    return "\n".join(lines)


# =============================================================================
# ARAÇ 4: orchestra_read_file
# =============================================================================
@mcp.tool()
async def orchestra_read_file(path: str, offset: int = 0, limit: int = 30000) -> str:
    """
    Çalışma alanındaki bir dosyanın içeriğini okur.
    Gizli dosyalara (.env, *.pem vb.) erişim engellenir.

    Parametreler
    ------------
    path   : Çalışma alanına göre göreli dosya yolu. Örn: "src/app.py"
    offset : Okumaya başlanacak bayt konumu (0 = başlangıç)
    limit  : Maksimum okunacak bayt sayısı (varsayılan 30KB)
    """
    try:
        p = ws.safe_path(WORKSPACE, path)
    except ws.WorkspaceError as e:
        return f"❌ Erişim engellendi: {e}"
    if not p.is_file():
        return f"❌ Dosya bulunamadı: {path}"
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"❌ Okuma hatası: {e}"
    if offset < 0:
        offset = 0
    if offset >= len(content):
        return f"### {path}\n```\n(offset dosya sonundan büyük)\n```"
    end = min(offset + limit, len(content))
    chunk = content[offset:end]
    more = "" if end >= len(content) else f"\n... ({len(content) - end} bayt kaldı)"
    return f"### {path} [{offset}:{end}]\n```\n{chunk}{more}\n```"


# =============================================================================
# ARAÇ 5: orchestra_apply_edit
# =============================================================================
@mcp.tool()
async def orchestra_apply_edit(path: str, search: str, replace: str) -> str:
    """
    Mevcut bir dosyada tek bir SEARCH/REPLACE düzenlemesi uygular.
    LLM kullanmaz; doğrudan coder.apply_in_memory + write_with_backup kullanır.
    Hızlı, deterministik ve ücretsizdir.

    Parametreler
    ------------
    path   : Düzenlenecek dosya yolu (göreli)
    search : Dosyada BİREBİR ve TEK kez geçen eski metin
    replace: Yeni metin (boş = silme)
    """
    try:
        p = ws.safe_path(WORKSPACE, path)
    except ws.WorkspaceError as e:
        return f"❌ Erişim engellendi: {e}"
    if not p.is_file():
        return f"❌ Dosya bulunamadı (yeni dosya için orchestra_write_file kullanın): {path}"

    # In-memory uygula
    try:
        staged = coder.apply_in_memory(WORKSPACE, [coder.Change(path, "edit", search=search, replace=replace)])
    except coder.ParseError as e:
        return f"❌ Biçim hatası: {e}"
    except coder.ApplyError as e:
        return f"❌ Uygulama hatası: {e}"

    # Diske yaz + diff
    try:
        diff = ws.make_diff(WORKSPACE, staged)
        ws.write_with_backup(WORKSPACE, staged)
    except Exception as e:
        return f"❌ Yazma hatası: {e}"

    return f"✅ Düzenleme uygulandı: {path}\n\n### Diff\n```diff\n{diff}\n```"


# =============================================================================
# ARAÇ 6: orchestra_write_file
# =============================================================================
@mcp.tool()
async def orchestra_write_file(path: str, content: str) -> str:
    """
    Yeni dosya oluşturur veya mevcut dosyayı tamamen değiştirir.
    LLM kullanmaz; doğrudan write_with_backup kullanır.

    Parametreler
    ------------
    path    : Dosya yolu (göreli)
    content : Tam dosya içeriği
    """
    try:
        p = ws.safe_path(WORKSPACE, path)
    except ws.WorkspaceError as e:
        return f"❌ Erişim engellendi: {e}"

    # Parent dizinleri oluştur
    p.parent.mkdir(parents=True, exist_ok=True)

    # In-memory staged
    rel = p.relative_to(WORKSPACE).as_posix()
    staged = {rel: content.rstrip("\n") + "\n"}

    try:
        diff = ws.make_diff(WORKSPACE, staged)
        ws.write_with_backup(WORKSPACE, staged)
    except Exception as e:
        return f"❌ Yazma hatası: {e}"

    return f"✅ Dosya yazıldı: {path}\n\n### Diff\n```diff\n{diff}\n```"


# =============================================================================
# ARAÇ 7: orchestra_list_files
# =============================================================================
@mcp.tool()
async def orchestra_list_files(subdir: str = "", max_files: int = 300) -> str:
    """
    Proje dosya ağacını listeler (gizli/hariç tutulan klasörler atlanır).

    Parametreler
    ------------
    subdir    : Alt dizin filtresi (boş = kök)
    max_files : Maksimum dosya sayısı
    """
    if subdir:
        try:
            subdir_path = ws.safe_path(WORKSPACE, subdir)
        except ws.WorkspaceError as e:
            return f"❌ Erişim engellendi: {e}"
        if not subdir_path.is_dir():
            return f"❌ Dizin bulunamadı: {subdir}"

    # Her zaman WORKSPACE'ten repo_map al, sonra filtrele
    files = ws.repo_map(WORKSPACE, max_files=max_files)
    if subdir:
        prefix = subdir.rstrip("/") + "/"
        files = [f for f in files if f.startswith(prefix)]
    return "\n".join(files) if files else "(boş)"


# =============================================================================
# ARAÇ 8: orchestra_search
# =============================================================================
@mcp.tool()
async def orchestra_search(pattern: str, include: str = "", max_results: int = 100) -> str:
    """
    Proje dosyalarında regex arama yapar (gizli dosyalar atlanır).

    Parametreler
    ------------
    pattern     : Python regex deseni
    include     : Dosya uzantısı filtresi (örn: ".py" veya ".py,.js") - boş = hepsi
    max_results : Maksimum sonuç sayısı
    """
    import re
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"❌ Geçersiz regex: {e}"

    include_exts = set()
    if include:
        include_exts = {e if e.startswith(".") else "." + e for e in include.split(",")}

    results = []
    for dirpath, dirnames, filenames in os.walk(WORKSPACE):
        dirnames[:] = [d for d in dirnames if d not in ws.EXCLUDE_DIRS]
        for fn in filenames:
            if ws.is_secret(fn):
                continue
            if include_exts and Path(fn).suffix.lower() not in include_exts:
                continue
            p = Path(dirpath) / fn
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = p.relative_to(WORKSPACE).as_posix()
                    results.append(f"{rel}:{i}: {line.strip()}")
                    if len(results) >= max_results:
                        break
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    return "\n".join(results) if results else "(eşleşme yok)"


# =============================================================================
# ARAÇ 9: orchestra_run_tests
# =============================================================================
@mcp.tool()
async def orchestra_run_tests(command: str = "") -> str:
    """
    Proje testlerini çalıştırır.

    Parametreler
    ------------
    command : Test komutu (boş = otomatik pytest tespiti)
    """
    import os
    import subprocess
    import sys

    if command:
        cmd = command.split()
    else:
        # Otomatik tespit: tests/ klasörü veya test_*.py varsa pytest
        if (WORKSPACE / "tests").is_dir() or list(WORKSPACE.glob("test_*.py")):
            cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
        else:
            return "⚠️  Test komutu belirtilmedi ve proje pytest yapısına sahip değil."

    try:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        r = await anyio.to_thread.run_sync(
            lambda: subprocess.run(cmd, cwd=WORKSPACE, capture_output=True, text=True, timeout=180, env=env, encoding="utf-8", errors="replace")
        )
        out = r.stdout + r.stderr
        if r.returncode == 0:
            return f"✅ Testler geçti\n\n```\n{out[-4000:]}\n```"
        else:
            return f"❌ Testler başarısız (code={r.returncode})\n\n```\n{out[-4000:]}\n```"
    except subprocess.TimeoutExpired:
        return f"❌ Testler 180s içinde bitmedi"
    except FileNotFoundError as e:
        return f"❌ Komut bulunamadı: {e}"
    except Exception as e:
        return f"❌ Hata: {type(e).__name__}: {e}"


# =============================================================================
# ARAÇ 10: orchestra_git
# =============================================================================
@mcp.tool()
async def orchestra_git(action: str, message: str = "") -> str:
    """
    Git işlemleri: status, diff, commit.

    Parametreler
    ------------
    action  : "status" | "diff" | "commit"
    message : Commit mesajı (action="commit" için zorunlu)
    """
    import subprocess
    try:
        if action == "status":
            r = await anyio.to_thread.run_sync(
                lambda: subprocess.run(["git", "-C", str(WORKSPACE), "status", "--porcelain"], capture_output=True, text=True, timeout=30)
            )
            return r.stdout or "(temiz)"
        elif action == "diff":
            r = await anyio.to_thread.run_sync(
                lambda: subprocess.run(["git", "-C", str(WORKSPACE), "diff"], capture_output=True, text=True, timeout=30)
            )
            return r.stdout or "(değişiklik yok)"
        elif action == "commit":
            if not message:
                return "❌ Commit mesajı gerekli"
            r = await anyio.to_thread.run_sync(
                lambda: subprocess.run(["git", "-C", str(WORKSPACE), "add", "-A"], capture_output=True, text=True, timeout=30)
            )
            r = await anyio.to_thread.run_sync(
                lambda: subprocess.run(["git", "-C", str(WORKSPACE), "commit", "-m", message], capture_output=True, text=True, timeout=30)
            )
            return r.stdout or r.stderr or "✅ Commit yapıldı"
        else:
            return f"❌ Bilinmeyen action: {action} (status|diff|commit)"
    except Exception as e:
        return f"❌ Git hatası: {type(e).__name__}: {e}"


# =============================================================================
# ARAÇ 11: orchestra_review
# =============================================================================
@mcp.tool()
async def orchestra_review(goal: str, diff: str) -> str:
    """
    Verilen diff'i hedef doğrultusunda hakem (Jev AI veya LLM) ile inceler.
    Değişiklik uygulamaz; sadece onay/red kararını döndürür.

    Parametreler
    ------------
    goal : Görev tanımı
    diff : İncelenecek unified diff metni
    """
    status, reason = _judge.review(goal, diff)
    if status == "APPROVED":
        return f"✅ **APPROVED** — Değişiklik hedefe uygun."
    elif status == "REJECTED":
        return f"❌ **REJECTED** — {reason}"
    else:
        return f"⚠️ **UNAVAILABLE** — Hakem yanıt veremedi: {reason}"


# =============================================================================
# SERVER BAŞLATMA
# =============================================================================
if __name__ == "__main__":
    print(f"[orchestra-mcp] workspace={WORKSPACE}", file=sys.stderr, flush=True)
    mcp.run(transport="stdio")