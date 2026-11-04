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
   Server mevcut 12 aracı listeler.

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
  orchestra_run_task     → Orkestra boru hattını çalıştır, dosyaları değiştir (timeout + ilerleme)
  orchestra_ping         → Modelleri paralel test et (TTL önbellekli, role filtreli)
  orchestra_status       → Oturumun token ve çağrı istatistiğini göster
  orchestra_workspace    → Çalışma alanını göster / değiştir
  orchestra_read_file    → Dosya oku (offset/limit veya satır aralığı)
  orchestra_read_many    → Birden çok dosyayı tek çağrıda oku
  orchestra_list_files   → Proje dosya ağacını listele (.gitignore uyumlu)
  orchestra_glob         → Glob deseniyle dosya ara ("**/*.py")
  orchestra_search       → Kod içinde regex arama yap
  orchestra_apply_edit   → Tek SEARCH/REPLACE düzenlemesi (dry_run destekli)
  orchestra_write_file   → Dosya yaz / tamamen değiştir (dry_run destekli)
  orchestra_undo         → Son yedek işlemini geri al / yedekleri listele
  orchestra_run_tests    → Proje testlerini çalıştır
  orchestra_git          → Git status/diff/log/commit
  orchestra_review       → Sadece hakem incelemesi yap (uygulamaz)

Tüm okuma/inceleme araçları `structured=true` verildiğinde metnin yanında
`structuredContent` (JSON) de döndürür. Varsayılan davranış değişmez (düz metin).

GÜVENLİK
--------
- safe_path: çalışma alanı dışına çıkma, sembolik link ile kaçış ve gizli
  dosya erişimi (yolun HER bileşeni kontrol edilir; `.env/` içindeki dosyalar dahil).
- Her yazma öncesi `backups/<proje>/<zaman>/` altına yedek + manifest alınır.
- `.gitignore` kuralları listeleme/arama/glob'da uygulanır.

ÖNEMLİ (Windows): MCP sunucusunun stdin'i açık bir pipe'tır. Alt süreçlere
stdin miras kalırsa git/pytest gibi programlar EOF bekleyip kilitlenir.
Bu yüzden tüm subprocess çağrılarında stdin=subprocess.DEVNULL kullanılır (_run).
"""

import argparse
import inspect
import json
import os
import shlex
import subprocess
import sys
import time
from typing import Any

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
from mcp.types import CallToolResult, TextContent


# -----------------------------------------------------------------------------
# MCP SDK yarış düzeltmesi: "Request already responded to"
# -----------------------------------------------------------------------------
# NEDEN GEREKLİ?
# mcp/shared/session.py `_receive_loop` bir CancelledNotification aldığında
# doğrudan `responder.cancel()` çağırır. `cancel()` hem `_completed = True` yapar
# hem de "Request cancelled" yanıtını gönderir. Aynı anda araç fonksiyonu normal
# şekilde dönmüşse `_handle_request` satırı `await message.respond(response)`
# çalışır ve `assert not self._completed` PATLAR:
#
#     AssertionError: Request already responded to
#     -> istisna anyio task group'tan yukarı çıkar, süreç exit code 1 ile ölür
#     -> OpenCode "MCP server: local-orchestra Status: failed" gösterir
#
# Bu pencere, uzun süren bir araç çağrısı TAM BİTİŞ ANINDA iptal edilirse
# (kullanıcı ESC / "abort") oluşur. SDK'nın kendi kodu bunu yutmadığı için
# tek bir iptal, tüm MCP sunucusunu düşürüyor.
#
# ÇÖZÜM: `respond()`'u idempotent yap. Yanıt zaten gönderilmişse (iptal edilmiş
# ya da tamamlanmışsa) sessizce dön; sunucu ayakta kalsın.
def _patch_mcp_double_respond() -> bool:
    """RequestResponder.respond() ikinci çağrıda çökmek yerine sessizce dönsün."""
    try:
        from mcp.shared.session import RequestResponder
    except Exception:  # SDK sürümü değişirse zarar verme
        return False

    if getattr(RequestResponder, "_orchestra_respond_patched", False):
        return True

    original = RequestResponder.respond

    async def respond(self, response):  # type: ignore[no-untyped-def]
        # İptal/gönderim çoktan olduysa yeni bir yanıt göndermeye çalışma.
        if getattr(self, "_completed", False):
            rid = getattr(self, "request_id", "?")
            try:
                print(f"[orchestra-mcp] ℹ️ yanıt zaten gönderilmiş (id={rid}); "
                      f"mükerrer yanıt yutuldu", file=sys.stderr, flush=True)
            except Exception:
                pass
            return None
        return await original(self, response)

    RequestResponder.respond = respond          # type: ignore[method-assign]
    RequestResponder._orchestra_respond_patched = True  # type: ignore[attr-defined]
    return True


_MCP_RESPOND_PATCHED = _patch_mcp_double_respond()

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
# Çalışma alanı çözümleme sırası:
#   1) --workspace <yol>          (opencode.json'daki "command" ile verilir)
#   2) ORCHESTRA_WORKSPACE ortam değişkeni
#   3) server'ın başlatıldığı dizin (cwd)
_ap = argparse.ArgumentParser(add_help=False)
_ap.add_argument("--workspace", default=os.getenv("ORCHESTRA_WORKSPACE", "."))
_args, _ = _ap.parse_known_args()


def _resolve_workspace(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve()


WORKSPACE = _resolve_workspace(_args.workspace)

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


def _result(text: str, payload: dict = None, structured: bool = False) -> Any:
    """Araç dönüşü: normalde düz metin, `structured=True` ise metin + JSON.

    Neden `-> Any`? FastMCP dönüş tipinden otomatik bir outputSchema üretiyor;
    `-> str` olduğunda `structuredContent` göndermek şemaya uymuyor. `Any`
    seçildiğinde şema üretilmez ve hem `content` (metin) hem `structuredContent`
    (JSON) birlikte dönebilir. Varsayılan davranış (structured=False) DEĞİŞMEZ:
    düz metin döner.
    """
    if structured and payload is not None:
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structuredContent=payload,
            isError=False,
        )
    return text


# -----------------------------------------------------------------------------
# orchestra_ping önbelleği (TTL)
# -----------------------------------------------------------------------------
_PING_CACHE: dict = {"ts": 0.0, "lines": []}
_PING_TTL = 300  # saniye

# anyio 4: abandon_on_cancel, anyio 3: cancellable. Timeout'ta thread'i beklemeden
# dönmek için gerekli (varsayılan davranış thread bitene kadar bekler).
_TO_THREAD_KW = (
    {"abandon_on_cancel": True}
    if "abandon_on_cancel" in inspect.signature(anyio.to_thread.run_sync).parameters
    else {"cancellable": True}
)


# -----------------------------------------------------------------------------
# Alt süreç yardımcıları
# -----------------------------------------------------------------------------
# ÖNEMLİ: MCP stdio sunucusunun stdin'i OpenCode'un açık tuttuğu bir pipe'tır.
# Alt süreçlere bu pipe miras kalırsa `git` gibi programlar stdin'den EOF
# bekleyip SONSUZA DEK kilitlenir (mcp_server.py: subprocess._communicate →
# reader thread join). Bu yüzden HER alt sürece stdin=DEVNULL verilmelidir.
def _run(cmd, cwd=None, timeout: int = 120):
    """Alt süreci güvenli şekilde çalıştırır: stdin kapalı, UTF-8 çıktı."""
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdin=subprocess.DEVNULL,      # <-- kilitlenmeyi önleyen kritik satır
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _split_command(command: str) -> list:
    """Kullanıcı komutunu tırnak/dizin boşluklarını bozmadan parçalar.

    Windows'ta `shlex.split(posix=True)` ters bölüleri yutar ("C:\\a\\b" bozulur),
    `posix=False` ise tırnakları token içinde bırakır. İkisinin de sorununu
    gideren küçük bir sarmalayıcı.
    """
    tokens = shlex.split(command, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]


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
async def orchestra_run_task(goal: str, files: str = "", complexity_hint: str = "auto",
                             dry_run: bool = False, timeout: int = 900) -> Any:
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
    timeout         : Saniye cinsinden üst sınır (varsayılan 900). Aşılırsa görev
                      iptal edilir ve kısmi bilgi döner.

    Döndürür
    --------
    Uygulanan değişikliklerin birleşik diff'i + kurulun TÜM önerileri + Jev'in karar gerekçesi
    (+ ham Jev yanıtı) + özet rapor.

    Not: Uzun süren görevlerde istemciye periyodik ilerleme bildirimi gönderilir
    (notifications/progress); böylece istemci "dondu" sanmaz.
    """
    file_list = files.split() if files.strip() else []

    # complexity_hint varsa, router sonucunu override edeceğiz
    override_complexity = complexity_hint if complexity_hint in ("simple", "complex") else None

    # İlerleme bildirimi: istemci progressToken verdiyse periyodik "hâlâ çalışıyorum" yayını
    try:
        ctx = mcp.get_context()
    except Exception:
        ctx = None

    # NOT: Heartbeat ayrı bir görev olarak çalışır ama kendi cancel scope'unu
    # İPTAL ETMEZ. Eskiden `tg.cancel_scope.cancel()` kullanılıyordu; bu, aracın
    # dönüş anını istemcinin iptal bildirimiyle aynı pencereye sokup SDK'daki
    # "Request already responded to" yarışını büyütüyordu. Artık durdurma sinyali
    # (anyio.Event) ile nazikçe kapatılıyor.
    stop = anyio.Event()

    async def heartbeat():
        started = time.time()
        while True:
            with anyio.move_on_after(10):   # 10s bekle VEYA durdurma sinyali
                await stop.wait()
            if stop.is_set():
                return
            elapsed = int(time.time() - started)
            if ctx is None:
                continue
            try:
                await ctx.report_progress(
                    progress=float(elapsed), total=float(timeout),
                    message=f"Orkestra çalışıyor… {elapsed}s",
                )
            except Exception:
                pass

    result = None
    error_text = None
    timed_out = False

    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat)
        try:
            with anyio.move_on_after(timeout) as scope:
                result = await anyio.to_thread.run_sync(
                    lambda: orchestrator.run_task(
                        _llm, _judge, WORKSPACE,
                        goal=goal,
                        files=file_list,
                        auto=True,           # MCP modunda onay sormadan uygula
                        git=False,
                        complexity_override=override_complexity,
                        dry_run=dry_run,
                    ),
                    **_TO_THREAD_KW,
                )
            timed_out = scope.cancelled_caught
        except Exception as e:
            error_text = f"❌ Orkestra hatası: {type(e).__name__}: {e}"
        stop.set()   # heartbeat'i nazikçe durdur (task group normal kapanır)

    if timed_out:
        return (f"⏱️ Görev {timeout}s içinde bitmedi (zaman aşımı).\n"
                f"Not: Arka plandaki iş parçacığı iptal edilemez; model çağrısı "
                f"kendi zaman aşımında sonlanacak.\n"
                f"İpucu: `timeout` değerini artırın, `complexity_hint=\"simple\"` "
                f"kullanın veya görevi daha küçük parçalara bölün.")
    if error_text:
        return error_text
    if result is None:
        return "❌ Orkestra beklenmedik şekilde sonuç üretmedi."

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
async def orchestra_ping(role: str = "", max_age: int = 300, refresh: bool = False,
                         structured: bool = False) -> Any:
    """
    config.yaml'daki model adaylarına küçük bir istek göndererek
    hangileri çalışıyor, hangilerinde hata var gösterir.

    Parametreler
    ------------
    role       : Yalnızca belirli bir rolü test et (örn: "coder"). Boş = tümü.
                 Tümü paralel test edilir (8 iş parçacığı).
    max_age    : Önbellek geçerlilik süresi, saniye (varsayılan 300). 0 = önbelleği atla.
    refresh    : True ise önbelleği yok say, yeniden test et.
    structured : True ise ek olarak JSON (structuredContent) döndürür.
    """
    lines, cached, age = await anyio.to_thread.run_sync(
        lambda: _ping_impl(role=role, max_age=max_age, refresh=refresh)
    )
    payload = {
        "role": role or "all",
        "cached": cached,
        "cache_age_s": round(age, 1),
        "lines": lines,
        "ok": sum(1 for ln in lines if "✅" in ln),
        "failed": sum(1 for ln in lines if "❌" in ln),
        "skipped": sum(1 for ln in lines if "⚠️" in ln),
    }
    header = "## Model Sağlık Kontrolü\n"
    if cached:
        header += f"_(önbellekten, {age:.0f}s önce; `refresh=true` ile yenile)_\n"
    text = "\n".join([header] + lines)
    return _result(text, payload, structured)


def _ping_impl(role: str = "", max_age: int = 300, refresh: bool = False):
    """(satırlar, önbellekten_mi, yaş) döner."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    cache_key = role or "__all__"
    cache = _PING_CACHE.get(cache_key)
    now = time.time()
    if isinstance(cache, dict) and not refresh and max_age > 0:
        age = now - cache.get("ts", 0)
        if age < max_age:
            return cache["lines"], True, age

    jobs = []
    for role_name, spec in _llm.cfg["roles"].items():
        if role and role_name != role:
            continue
        for cand in spec["models"]:
            jobs.append((role_name, cand))

    if not jobs:
        return [f"❌ Bilinmeyen rol: {role} (mevcut: {', '.join(_llm.cfg['roles'])})"], False, 0.0

    def probe(job):
        role_name, cand = job
        t0 = time.time()
        try:
            # Gerçek kontrol: boş yanıt / max_tokens'ta kesilme de hata sayılır.
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
        return f"[**{role_name}**] `{cand['provider']}/{cand['model'].split('/')[-1]}`: {status}"

    def run_all():
        with ThreadPoolExecutor(max_workers=8) as ex:
            return list(ex.map(probe, jobs))

    lines = list(run_all())
    # D2: chat modelleri Jev'i DOĞRULAYAMAZ → tek bir gerçek karar çağrısı dene.
    lines.append(_jev_ping_line())
    _PING_CACHE[cache_key] = {"ts": time.time(), "lines": lines}
    return lines, False, 0.0


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
async def orchestra_status(structured: bool = False) -> Any:
    """
    Bu MCP oturumunda yapılan tüm model çağrılarının
    token kullanım istatistiğini döndürür.
    Oturum boyunca ne kadar context tüketildiğini takip etmek için kullanın.

    Parametreler
    ------------
    structured : True ise ek olarak JSON (structuredContent) döndürür.
    """
    payload = {
        "workspace": str(WORKSPACE),
        "logfile": str(tracker.logfile),
        "models": {
            name: {"calls": s["calls"], "prompt": s["prompt"], "completion": s["completion"]}
            for name, s in tracker.llm.items()
        },
        "totals": {
            "calls": sum(s["calls"] for s in tracker.llm.values()),
            "prompt": sum(s["prompt"] for s in tracker.llm.values()),
            "completion": sum(s["completion"] for s in tracker.llm.values()),
        },
    }
    if not tracker.llm:
        return _result("Henüz hiç model çağrısı yapılmadı.", payload, structured)

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
    lines.append(f"\n📂 Çalışma alanı: `{WORKSPACE}`")
    return _result("\n".join(lines), payload, structured)


# =============================================================================
# ARAÇ 3b: orchestra_workspace
# =============================================================================
@mcp.tool()
async def orchestra_workspace(path: str = "", structured: bool = False) -> Any:
    """
    Çalışma alanını (tüm dosya araçlarının kök dizini) gösterir veya değiştirir.

    MCP sunucusu OpenCode tarafından başlatıldığında çalışma alanı, oturumun
    proje dizinidir. Farklı bir projede çalışmak istiyorsanız bu araçla
    yolu mutlak olarak verin.

    Parametreler
    ------------
    path       : Yeni çalışma alanı (mutlak yol). Boş = mevcut yolu göster.
    structured : True ise ek olarak JSON (structuredContent) döndürür.
    """
    global WORKSPACE
    if not path.strip():
        payload = {"workspace": str(WORKSPACE), "changed": False}
        return _result(f"📂 Çalışma alanı: `{WORKSPACE}`", payload, structured)
    p = _resolve_workspace(path)
    if not p.is_dir():
        payload = {"workspace": str(WORKSPACE), "changed": False, "error": f"Dizin bulunamadı: {p}"}
        return _result(f"❌ Dizin bulunamadı: {p}", payload, structured)
    WORKSPACE = p
    payload = {"workspace": str(WORKSPACE), "changed": True}
    return _result(f"✅ Çalışma alanı değiştirildi: `{WORKSPACE}`", payload, structured)


# =============================================================================
# ARAÇ 4: orchestra_read_file
# =============================================================================
@mcp.tool()
async def orchestra_read_file(path: str, offset: int = 0, limit: int = 30000,
                              start_line: int = 0, end_line: int = 0,
                              structured: bool = False) -> Any:
    """
    Çalışma alanındaki bir dosyanın içeriğini okur.
    Gizli dosyalara (.env, *.pem vb.) erişim engellenir.

    Parametreler
    ------------
    path       : Çalışma alanına göre göreli dosya yolu. Örn: "src/app.py"
    offset     : Okumaya başlanacak KARAKTER konumu (0 = başlangıç)
    limit      : Maksimum okunacak KARAKTER sayısı (varsayılan 30K)
    start_line : (opsiyonel) 1 tabanlı başlangıç satırı; verilirse offset yok sayılır
    end_line   : (opsiyonel) 1 tabanlı bitiş satırı (dahil). 0 = dosya sonuna kadar
    structured : True ise ek olarak JSON (structuredContent) döndürür.

    İpucu: "şu dosyanın 40-80. satırları" gibi isteklerde start_line/end_line kullanın.
    """
    try:
        p = ws.safe_path(WORKSPACE, path)
    except ws.WorkspaceError as e:
        return _result(f"❌ Erişim engellendi: {e}",
                       {"path": path, "error": str(e)}, structured)
    if not p.is_file():
        msg = f"❌ Dosya bulunamadı: {path} (çalışma alanı: {WORKSPACE})"
        return _result(msg, {"path": path, "error": "not_found", "workspace": str(WORKSPACE)}, structured)
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return _result(f"❌ Okuma hatası: {e}", {"path": path, "error": str(e)}, structured)

    total_lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)

    if start_line or end_line:
        lines = content.splitlines()
        s = max(1, start_line or 1) - 1
        e = len(lines) if not end_line else min(len(lines), end_line)
        if s >= len(lines):
            msg = f"### {path}\n```\n(start_line dosya sonundan büyük; dosya {len(lines)} satır)\n```"
            return _result(msg, {"path": path, "error": "start_line_out_of_range",
                                 "total_lines": len(lines)}, structured)
        chunk = "\n".join(lines[s:e])
        tail = "" if e >= len(lines) else f"\n... ({len(lines) - e} satır kaldı)"
        text = f"### {path} [satır {s + 1}-{e} / {len(lines)}]\n```\n{chunk}{tail}\n```"
        payload = {
            "path": path, "content": chunk, "start_line": s + 1, "end_line": e,
            "total_lines": len(lines), "truncated": e < len(lines),
        }
        return _result(text, payload, structured)

    if offset < 0:
        offset = 0
    if offset >= len(content):
        msg = f"### {path}\n```\n(offset dosya sonundan büyük; {len(content)} karakter)\n```"
        return _result(msg, {"path": path, "error": "offset_out_of_range",
                             "total_chars": len(content)}, structured)
    end = min(offset + limit, len(content))
    chunk = content[offset:end]
    more = "" if end >= len(content) else f"\n... ({len(content) - end} karakter kaldı)"
    text = (f"### {path} [{offset}:{end} / {len(content)} karakter, {total_lines} satır]\n"
            f"```\n{chunk}{more}\n```")
    payload = {
        "path": path, "content": chunk, "offset": offset, "end": end,
        "total_chars": len(content), "total_lines": total_lines,
        "truncated": end < len(content),
    }
    return _result(text, payload, structured)


# =============================================================================
# ARAÇ 5: orchestra_apply_edit
# =============================================================================
@mcp.tool()
async def orchestra_apply_edit(path: str, search: str, replace: str,
                               dry_run: bool = False, structured: bool = False) -> Any:
    """
    Mevcut bir dosyada tek bir SEARCH/REPLACE düzenlemesi uygular.
    LLM kullanmaz; doğrudan coder.apply_in_memory + write_with_backup kullanır.
    Hızlı, deterministik ve ücretsizdir.

    Parametreler
    ------------
    path       : Düzenlenecek dosya yolu (göreli)
    search     : Dosyada BİREBİR ve TEK kez geçen eski metin
    replace    : Yeni metin (boş = silme)
    dry_run    : True ise diske YAZMAZ, yalnızca diff önizlemesi döndürür.
    structured : True ise ek olarak JSON (structuredContent) döndürür.
    """
    try:
        p = ws.safe_path(WORKSPACE, path)
    except ws.WorkspaceError as e:
        return _result(f"❌ Erişim engellendi: {e}",
                       {"path": path, "applied": False, "error": str(e)}, structured)
    if not p.is_file():
        msg = f"❌ Dosya bulunamadı (yeni dosya için orchestra_write_file kullanın): {path}"
        return _result(msg, {"path": path, "applied": False, "error": "not_found"}, structured)

    # In-memory uygula
    try:
        staged = coder.apply_in_memory(WORKSPACE, [coder.Change(path, "edit", search=search, replace=replace)])
    except coder.ParseError as e:
        return _result(f"❌ Biçim hatası: {e}",
                       {"path": path, "applied": False, "error": str(e)}, structured)
    except coder.ApplyError as e:
        return _result(f"❌ Uygulama hatası: {e}",
                       {"path": path, "applied": False, "error": str(e)}, structured)

    # Diske yaz + diff
    try:
        diff = ws.make_diff(WORKSPACE, staged)
        if not dry_run:
            ws.write_with_backup(WORKSPACE, staged)
    except Exception as e:
        return _result(f"❌ Yazma hatası: {e}",
                       {"path": path, "applied": False, "error": str(e)}, structured)

    payload = {"path": path, "applied": not dry_run, "dry_run": dry_run, "diff": diff}
    if dry_run:
        return _result(f"🔍 Dry-run — düzenleme UYGULANMADI: {path}\n\n### Diff\n```diff\n{diff}\n```",
                       payload, structured)
    return _result(f"✅ Düzenleme uygulandı: {path}\n\n### Diff\n```diff\n{diff}\n```", payload, structured)


# =============================================================================
# ARAÇ 6: orchestra_write_file
# =============================================================================
@mcp.tool()
async def orchestra_write_file(path: str, content: str, dry_run: bool = False,
                               structured: bool = False) -> Any:
    """
    Yeni dosya oluşturur veya mevcut dosyayı tamamen değiştirir.
    LLM kullanmaz; doğrudan write_with_backup kullanır.

    Parametreler
    ------------
    path       : Dosya yolu (göreli)
    content    : Tam dosya içeriği
    dry_run    : True ise diske YAZMAZ, yalnızca diff önizlemesi döndürür.
    structured : True ise ek olarak JSON (structuredContent) döndürür.
    """
    try:
        p = ws.safe_path(WORKSPACE, path)
    except ws.WorkspaceError as e:
        return _result(f"❌ Erişim engellendi: {e}",
                       {"path": path, "written": False, "error": str(e)}, structured)

    # Parent dizinleri oluştur
    if not dry_run:
        p.parent.mkdir(parents=True, exist_ok=True)

    # In-memory staged (boş içerik bozulmasın: sadece doluysa son satır sonu ekle)
    rel = p.relative_to(WORKSPACE).as_posix()
    body = content if (not content or content.endswith("\n")) else content + "\n"
    staged = {rel: body}

    try:
        diff = ws.make_diff(WORKSPACE, staged)
        if not dry_run:
            ws.write_with_backup(WORKSPACE, staged)
    except Exception as e:
        return _result(f"❌ Yazma hatası: {e}",
                       {"path": path, "written": False, "error": str(e)}, structured)

    payload = {
        "path": path, "written": not dry_run, "dry_run": dry_run,
        "bytes": len(body.encode("utf-8")), "diff": diff,
    }
    if dry_run:
        return _result(f"🔍 Dry-run — dosya YAZILMADI: {path}\n\n### Diff\n```diff\n{diff}\n```",
                       payload, structured)
    return _result(f"✅ Dosya yazıldı: {path}\n\n### Diff\n```diff\n{diff}\n```", payload, structured)


# =============================================================================
# ARAÇ 7: orchestra_list_files
# =============================================================================
@mcp.tool()
async def orchestra_list_files(subdir: str = "", max_files: int = 300,
                               use_gitignore: bool = True, structured: bool = False) -> Any:
    """
    Proje dosya ağacını listeler (gizli/hariç tutulan klasörler atlanır).

    Parametreler
    ------------
    subdir        : Alt dizin filtresi (boş = kök)
    max_files     : Maksimum dosya sayısı
    use_gitignore : .gitignore kurallarına uy (varsayılan True)
    structured    : True ise ek olarak JSON (structuredContent) döndürür.
    """
    if subdir:
        try:
            subdir_path = ws.safe_path(WORKSPACE, subdir)
        except ws.WorkspaceError as e:
            return _result(f"❌ Erişim engellendi: {e}",
                           {"files": [], "error": str(e)}, structured)
        if not subdir_path.is_dir():
            return _result(f"❌ Dizin bulunamadı: {subdir}",
                           {"files": [], "error": "not_found"}, structured)

    # Her zaman WORKSPACE'ten repo_map al, sonra filtrele
    files = ws.repo_map(WORKSPACE, max_files=max_files, use_gitignore=use_gitignore)
    if subdir:
        prefix = subdir.rstrip("/") + "/"
        files = [f for f in files if f.startswith(prefix)]
    text = "\n".join(files) if files else "(boş)"
    payload = {
        "workspace": str(WORKSPACE),
        "subdir": subdir,
        "count": len(files),
        "files": files,
    }
    return _result(text, payload, structured)


# =============================================================================
# ARAÇ 8: orchestra_search
# =============================================================================
@mcp.tool()
async def orchestra_search(pattern: str, include: str = "", max_results: int = 100,
                            ignore_case: bool = False, max_file_kb: int = 2048,
                            use_gitignore: bool = True, structured: bool = False) -> Any:
    """
    Proje dosyalarında regex arama yapar (gizli dosyalar atlanır).

    Parametreler
    ------------
    pattern       : Python regex deseni
    include       : Dosya uzantısı filtresi (örn: ".py" veya ".py,.js") - boş = tüm metin dosyaları
    max_results   : Maksimum sonuç sayısı
    ignore_case   : Büyük/küçük harf duyarsız arama
    max_file_kb   : Bu boyuttan büyük dosyalar atlanır (varsayılan 2 MB)
    use_gitignore : .gitignore kurallarına uy (varsayılan True)
    structured    : True ise ek olarak JSON (structuredContent) döndürür.
    """
    import re
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return _result(f"❌ Geçersiz regex: {e}",
                       {"pattern": pattern, "results": [], "error": str(e)}, structured)

    include_exts = set()
    if include:
        include_exts = {e if e.startswith(".") else "." + e for e in include.split(",")}

    max_bytes = max(1, max_file_kb) * 1024
    results = []          # metin satırları
    records = []          # yapılandırılmış kayıtlar

    def run():
        for p, rel in ws.iter_files(WORKSPACE, use_gitignore=use_gitignore):
            suffix = p.suffix.lower()
            # Uzantı verilmediyse yalnızca metin uzantılarını tara
            if include_exts:
                if suffix not in include_exts:
                    continue
            elif suffix not in ws.TEXT_EXT:
                continue
            try:
                if p.stat().st_size > max_bytes:
                    continue
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    stripped = line.strip()
                    results.append(f"{rel}:{i}: {stripped[:300]}")
                    records.append({"file": rel, "line": i, "text": stripped[:300]})
                    if len(results) >= max_results:
                        return
        return

    await anyio.to_thread.run_sync(run)
    text = "\n".join(results) if results else "(eşleşme yok)"
    payload = {
        "pattern": pattern,
        "include": include,
        "count": len(records),
        "results": records,
        "truncated": len(records) >= max_results,
    }
    return _result(text, payload, structured)


# =============================================================================
# ARAÇ 7b: orchestra_glob
# =============================================================================
@mcp.tool()
async def orchestra_glob(pattern: str, max_results: int = 500,
                         use_gitignore: bool = True, structured: bool = False) -> Any:
    """
    Glob deseniyle dosya arar. `orchestra_list_files`'tan farkı: desenle filtreler.

    Parametreler
    ------------
    pattern       : Glob deseni. Örn: "**/*.py", "src/*.ts", "tests/test_*.py"
                    `**` tüm alt dizinler, `*` tek seviye, `?` tek karakter.
    max_results   : Maksimum sonuç sayısı
    use_gitignore : .gitignore kurallarına uy. False ise hem .gitignore yok sayılır
                    hem de build/, dist/, node_modules/ gibi genel klasörler taranır.
    structured    : True ise ek olarak JSON (structuredContent) döndürür.
    """
    hits = await anyio.to_thread.run_sync(
        lambda: ws.glob_files(WORKSPACE, pattern, max_results=max_results,
                              use_gitignore=use_gitignore,
                              apply_excludes=use_gitignore)
    )
    text = "\n".join(hits) if hits else "(eşleşme yok)"
    payload = {
        "pattern": pattern,
        "workspace": str(WORKSPACE),
        "count": len([h for h in hits if not h.startswith("...")]),
        "files": hits,
    }
    return _result(text, payload, structured)


# =============================================================================
# ARAÇ 7c: orchestra_read_many
# =============================================================================
@mcp.tool()
async def orchestra_read_many(paths: str, max_chars: int = 60000,
                              max_chars_per_file: int = 20000,
                              structured: bool = False) -> Any:
    """
    Birden çok dosyayı TEK çağrıda okur (round-trip ve token tasarrufu).

    Parametreler
    ------------
    paths              : Dosya yolları. Boşluk, virgül veya satır sonu ile ayrılır;
                         JSON listesi de kabul edilir. Örn: "src/a.py, src/b.py"
    max_chars          : Tüm dosyalar için toplam karakter bütçesi
    max_chars_per_file : Tek dosya için karakter bütçesi
    structured         : True ise ek olarak JSON (structuredContent) döndürür.
    """
    raw = (paths or "").strip()
    if not raw:
        return _result("❌ `paths` boş", {"files": [], "error": "empty_paths"}, structured)

    rels = []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                rels = [str(x) for x in parsed]
        except Exception:
            rels = []
    if not rels:
        rels = [t for t in raw.replace(",", " ").replace("\n", " ").split() if t]

    # Tekrarları at, sırayı koru
    seen, unique = set(), []
    for r in rels:
        if r not in seen:
            seen.add(r)
            unique.append(r)

    parts, records, used = [], [], 0
    for rel in unique:
        try:
            p = ws.safe_path(WORKSPACE, rel)
        except ws.WorkspaceError as e:
            parts.append(f"### {rel}\n[atlandı: {e}]")
            records.append({"path": rel, "error": str(e)})
            continue
        if not p.is_file():
            parts.append(f"### {rel}\n[dosya yok — yeni dosya olabilir]")
            records.append({"path": rel, "error": "not_found"})
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            parts.append(f"### {rel}\n[okuma hatası: {e}]")
            records.append({"path": rel, "error": str(e)})
            continue

        budget = max(0, min(max_chars_per_file, max_chars - used))
        if budget <= 0:
            parts.append(f"### {rel}\n[toplam bütçe doldu]")
            records.append({"path": rel, "error": "budget_exhausted"})
            continue
        chunk = text[:budget]
        used += len(chunk)
        parts.append(f"### {rel} ({len(chunk)}/{len(text)} karakter)\n```\n{chunk}\n```")
        records.append({
            "path": rel, "content": chunk,
            "total_chars": len(text), "truncated": len(chunk) < len(text),
        })

    text = "\n\n".join(parts)
    payload = {"count": len(records), "files": records, "chars_used": used}
    return _result(text, payload, structured)


# =============================================================================
# ARAÇ 7d: orchestra_undo
# =============================================================================
@mcp.tool()
async def orchestra_undo(action: str = "undo", structured: bool = False) -> Any:
    """
    Yedeklerden geri alma.

    Her yazma işleminde (write_file / apply_edit / run_task) dosyaların önceki
    hâli `backups/<proje>/<zaman>/` altına kopyalanır. Bu araç o yedeği geri yükler.

    Parametreler
    ------------
    action     : "undo"  → en son yedeği geri yükle
                 "list"  → son yedekleri listele (değişiklik yapmaz)
    structured : True ise ek olarak JSON (structuredContent) döndürür.
    """
    if action == "list":
        backups = await anyio.to_thread.run_sync(lambda: ws.list_backups(WORKSPACE, limit=10))
        if not backups:
            payload = {"backups": [], "workspace": str(WORKSPACE)}
            return _result(f"ℹ️ Bu çalışma alanı için yedek yok: `{WORKSPACE}`", payload, structured)
        lines = ["## Son yedekler (yeniden eskiye)\n"]
        records = []
        for d in backups:
            files = [f.relative_to(d).as_posix() for f in d.rglob("*")
                     if f.is_file() and f.name != ws.MANIFEST_NAME]
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(d.stat().st_mtime))
            lines.append(f"- `{d.name}` — {ts} — {len(files)} dosya: {', '.join(files[:8])}"
                         + (" …" if len(files) > 8 else ""))
            records.append({"dir": str(d), "name": d.name, "files": files})
        payload = {"backups": records, "workspace": str(WORKSPACE)}
        return _result("\n".join(lines), payload, structured)

    if action != "undo":
        return _result(f"❌ Bilinmeyen action: {action} (undo|list)",
                       {"error": f"unknown action: {action}"}, structured)

    res = await anyio.to_thread.run_sync(lambda: ws.undo_last(WORKSPACE))
    if not res.get("ok"):
        return _result(f"❌ Geri alınamadı: {res.get('error')}",
                       {"undone": False, **res}, structured)

    restored = res.get("restored") or []
    deleted = res.get("deleted") or []
    lines = [f"✅ Geri alındı — yedek: `{Path(res['dir']).name}`"]
    if restored:
        lines.append(f"- Geri yüklenen dosyalar ({len(restored)}): {', '.join(restored)}")
    if deleted:
        lines.append(f"- Silinen dosyalar ({len(deleted)}, sonradan oluşturulmuştu): {', '.join(deleted)}")
    if not restored and not deleted:
        lines.append("- (Yedekte geri yüklenecek dosya yoktu)")
    payload = {"undone": True, "backup": res.get("dir"),
               "restored": restored, "deleted": deleted}
    return _result("\n".join(lines), payload, structured)


# =============================================================================
# ARAÇ 9: orchestra_run_tests
# =============================================================================
@mcp.tool()
async def orchestra_run_tests(command: str = "", timeout: int = 300,
                              structured: bool = False) -> Any:
    """
    Proje testlerini çalıştırır.

    Parametreler
    ------------
    command    : Test komutu (boş = otomatik pytest tespiti)
    timeout    : Saniye cinsinden üst sınır (varsayılan 300)
    structured : True ise ek olarak JSON (structuredContent) döndürür.

    Not: Alt sürece stdin kapalı verilir; aksi halde stdin bekleyen testler
    MCP sunucusunu kilitler.
    """
    if command:
        cmd = _split_command(command)
        if not cmd:
            return _result("❌ Boş komut", {"passed": False, "error": "empty command"}, structured)
    else:
        # Otomatik tespit: tests/ klasörü veya test_*.py varsa pytest
        if (WORKSPACE / "tests").is_dir() or list(WORKSPACE.glob("test_*.py")):
            cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
        else:
            msg = "⚠️  Test komutu belirtilmedi ve proje pytest yapısına sahip değil."
            return _result(msg, {"passed": None, "error": "no test setup"}, structured)

    try:
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        r = await anyio.to_thread.run_sync(
            lambda: subprocess.run(
                cmd, cwd=str(WORKSPACE), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=timeout, env=env,
                encoding="utf-8", errors="replace",
            )
        )
        out = ((r.stdout or "") + (r.stderr or ""))[-4000:]
        ok = r.returncode == 0
        payload = {"command": cmd, "passed": ok, "returncode": r.returncode,
                   "output_tail": out}
        if ok:
            return _result(f"✅ Testler geçti\n\n```\n{out}\n```", payload, structured)
        return _result(f"❌ Testler başarısız (code={r.returncode})\n\n```\n{out}\n```",
                       payload, structured)
    except subprocess.TimeoutExpired:
        return _result(f"❌ Testler {timeout}s içinde bitmedi",
                       {"passed": False, "error": "timeout"}, structured)
    except FileNotFoundError as e:
        return _result(f"❌ Komut bulunamadı: {e}",
                       {"passed": False, "error": str(e)}, structured)
    except Exception as e:
        return _result(f"❌ Hata: {type(e).__name__}: {e}",
                       {"passed": False, "error": str(e)}, structured)


# =============================================================================
# ARAÇ 10: orchestra_git
# =============================================================================
@mcp.tool()
async def orchestra_git(action: str, message: str = "", structured: bool = False) -> Any:
    """
    Git işlemleri: status, diff, log, commit.

    Parametreler
    ------------
    action     : "status" | "diff" | "log" | "commit"
    message    : Commit mesajı (action="commit" için zorunlu)
    structured : True ise ek olarak JSON (structuredContent) döndürür.

    Not: Alt süreçlere stdin kapalı verilir (MCP pipe'ı kilitlenmesin diye).
    """
    if not (WORKSPACE / ".git").exists():
        msg = f"⚠️  Bu çalışma alanı bir git deposu değil: {WORKSPACE}"
        return _result(msg, {"action": action, "is_repo": False, "workspace": str(WORKSPACE)},
                       structured)

    base = ["git", "-C", str(WORKSPACE), "--no-pager"]
    try:
        if action == "status":
            r = await anyio.to_thread.run_sync(
                lambda: _run(base + ["status", "--porcelain"], timeout=30)
            )
            out = (r.stdout or "").rstrip("\n")
            entries = [ln for ln in out.splitlines() if ln.strip()]
            payload = {"action": "status", "is_repo": True, "dirty": bool(entries),
                       "entries": entries, "output": out}
            return _result(out or "(temiz)", payload, structured)
        elif action == "diff":
            r = await anyio.to_thread.run_sync(
                lambda: _run(base + ["diff", "--no-color"], timeout=30)
            )
            out = (r.stdout or "").rstrip("\n")
            payload = {"action": "diff", "is_repo": True, "empty": not out, "output": out}
            return _result(out or "(değişiklik yok)", payload, structured)
        elif action == "log":
            r = await anyio.to_thread.run_sync(
                lambda: _run(base + ["log", "--oneline", "-n", "20"], timeout=30)
            )
            out = (r.stdout or r.stderr or "").rstrip("\n")
            payload = {"action": "log", "is_repo": True,
                       "commits": out.splitlines() if out else []}
            return _result(out or "(kayıt yok)", payload, structured)
        elif action == "commit":
            if not message:
                return _result("❌ Commit mesajı gerekli",
                               {"action": "commit", "committed": False,
                                "error": "message required"}, structured)
            add = await anyio.to_thread.run_sync(
                lambda: _run(base + ["add", "-A"], timeout=30)
            )
            if add.returncode != 0:
                err = add.stderr.strip()[:500]
                return _result(f"❌ git add başarısız: {err}",
                               {"action": "commit", "committed": False, "error": err},
                               structured)
            r = await anyio.to_thread.run_sync(
                lambda: _run(base + ["commit", "-m", message], timeout=30)
            )
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            ok = r.returncode == 0
            payload = {"action": "commit", "committed": ok, "returncode": r.returncode,
                       "message": message, "output": out[:1000]}
            if ok:
                return _result(f"✅ Commit yapıldı\n\n```\n{out[:1000]}\n```", payload, structured)
            return _result(f"❌ Commit başarısız (code={r.returncode})\n\n```\n{out[:1000]}\n```",
                           payload, structured)
        else:
            return _result(f"❌ Bilinmeyen action: {action} (status|diff|log|commit)",
                           {"action": action, "error": "unknown action"}, structured)
    except subprocess.TimeoutExpired:
        return _result("❌ Git komutu 30s içinde bitmedi",
                       {"action": action, "error": "timeout"}, structured)
    except FileNotFoundError:
        return _result("❌ git bulunamadı (PATH'te yok)",
                       {"action": action, "error": "git not found"}, structured)
    except Exception as e:
        return _result(f"❌ Git hatası: {type(e).__name__}: {e}",
                       {"action": action, "error": str(e)}, structured)


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