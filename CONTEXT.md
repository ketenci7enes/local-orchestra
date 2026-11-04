# local-orchestra — Context Handoff / Devam Rehberi

> **Amaç:** Context penceresi dolduğunda yeni bir oturumun işi kaldığı yerden sürdürebilmesi için özet.

---

## 1. Proje Özeti
**local-orchestra** — OpenCode MCP entegrasyonu ile çalışan, ücretsiz/yerel LLM'leri (NVIDIA Nemotron, OpenRouter) kullanarak kod yazma/inceleme/uygulama yapan bir orkestrasyon motoru.

**Akış:** `router(nvidia/openrouter) → [complex: kurul(3 üye/3 tutum) → karar(Jev, fail-closed)] → coder(detaylı prompt) → verify(statik+test) → judge(review, fail-closed) → write(backup+rollback) → diff`

**Kurul (Council):** Yalnızca `complexity=complex` görevlerde devreye girer. 3 üye / 3 tutum:
`council_a` (Agresif · Nemotron Ultra 550B), `council_b` (Güvenli · Nemotron 3.5 Super VL), `council_c`
(Profesyonel · Nemotron 3 Super) ayrı ayrı öneri üretir → **Jev (TypeSafe, OpenRouter)** kazananı seçer
(fail-closed) → kazanan üye, tutumunu koruyarak coder için detaylı yönergyi yazar.
`stance` MCP parametresiyle tüm üyeler aynı tutumda da üretebilir. Karar + karşılaştırma tablosu
`orchestra_run_task` sonucunda gösterilir.

---

## 2. Mimari Dosya Haritası

| Dosya | Rol |
|-------|-----|
| `mcp_server.py` | OpenCode MCP server (stdio transport), 11 araç exposé eder |
| `orchestrator.py` | Ana pipeline: route → plan → implement → verify → review → write |
| `llm.py` | Tek OpenAI-uyumlu istemci: rol bazlı model listesi, retry, failover, cooldown |
| `coder.py` | LLM çıktısını `<<<EDIT/FILE>>>` bloklarına parse eder, in-memory apply |
| `workspace.py` | Güvenli yol (`safe_path`), repo map, diff, backup/rollback, git helper |
| `verify.py` | Statik kontrol (syntax/ruff), test çalıştırma (pytest) |
| `router.py` | Görevi sınıflandırır: type (code/qa/...), complexity, ilgili dosyalar |
| `council.py` | **Kurul:** 3 üye / 3 tutum öneri üretir; kazanan üye (tutumunu koruyarak) detaylı promptu yazar |
| `brain.py` | (Eski) Karmaşık görevler için 3 alternatif strateji üretir — kurul akışında kullanılmıyor |
| `judge.py` | Hakem + `decide()`: **Jev (TypeSafe, OpenRouter) birincil**; Jev fail-closed (`JevUnavailable`). Review fail-closed |
| `tracker.py` | Token/çağrı sayacı + JSONL çalışma günlüğü (`runs/*.jsonl`) |
| `console.py` | **Stderr log helper** — MCP stdout protokolünü korur |
| `config.yaml` | Rol → model adayları (failover sırası), timeout, retry |
| `opencode.json` | OpenCode MCP + provider + permission konfigürasyonu |

---

## 3. Durum Tablosu (2025-10-12)

| # | Madde | Durum | Not |
|---|-------|-------|-----|
| 1 | `opencode.json` MCP şeması düzeltildi | ✅ | `servers` sarmalayıcı kaldırıldı, `cwd`, `permission glob`, `provider→npm/options` |
| 2 | `console.py` stderr log helper | ✅ | `print → log` yönlendirmesi için |
| 3 | `llm.py` / `orchestrator.py` stdout→stderr | ✅ | Tüm `print` → `console.log` |
| 4 | `orchestrator.py` complexity_override, dry_run, diff | ✅ | `run_task(..., complexity_override, dry_run)` + `result["diff"]` |
| 5 | `mcp_server.py` 11 araç + thread offload | ✅ | `anyio.to_thread.run_sync`, `complexity_hint` bağlandı, diff döndürür |
| 6 | `judge.py` (Jev AI) korunuyor | ✅ | Hiç dokunulmadı, `JEV_AI_API_KEY` eklendiğinde devreye girer |
| 7 | Testler (core + mcp_tools) | ✅ | 21/21 PASSED |
| 8 | MCP server import/bağlantı | ✅ | 11 araç kayıtlı, stdio transport çalışır |
| 9 | Global config tüm klasörlerde çalışır | ✅ | `~/.config/opencode/opencode.jsonc`: `mcp.local-orchestra`, `cwd: "."` (workspace'ten çözülür), `provider` düzeltildi |
| 10 | Kurul (council) yapısı | ✅ | `council.py` + config `council_a`/`council_b`/`decider`; sadece complex görevler |
| 11 | Karar sahibi görünürlüğü | ✅ | `orchestra_run_task` sonuç bloğu: Karar veren / Kazanan öneri / Detaylı promptu yazan |

---

### 3.b Durum Tablosu (2026-10-17) — `todo-local-orchestra.md` planı uygulandı

| # | Madde | Durum | Not |
|---|-------|-------|-----|
| A | FAZ 0 sağlayıcılar | ✅ | `config.yaml` + `opencode.json`: `groq`, `gemini` eklendi |
| B | FAZ 5/D1 rol rotasyonu | ✅ | router/coder/judge/brain/council_a/council_c/general yeni adaylar |
| C | FAZ 1a Jev entegrasyonu | ✅ | `OPENROUTER_API_KEY`, `choice`+`criteria`, `answers[qid].choice`, `JevUnavailable`, `decide_with_jev` |
| D | FAZ 1a tek öneri | ✅ | `len(proposals)==1` → karar atlanır (`decision_skipped_single_proposal`) |
| E | FAZ 1a dürüst decider | ✅ | LLM decider `None` → `decider_failed` (sessiz sahte atıf yok) |
| F | FAZ 1c/1-F tutum sistemi | ✅ | `council_a=agresif`, `council_b=güvenli`, `council_c=profesyonel`; `stance` MCP parametresi |
| G | FAZ 1d görünürlük | ✅ | `council_member_failed`, `council_empty`, tek-öneri uyarısı |
| H | FAZ 2 A1 | ✅ | override kısayoldan önce; qa/writing yalnız karmaşık değilse kısa yol |
| I | FAZ 2 A3 | ✅ | `finish_reason=="length"` → `llm_truncated` + `LLMError` |
| J | FAZ 2 A4 | ✅ | coder `max_tokens: 16000` + SYSTEM öncelik talimatı |
| K | FAZ 2 A5 | ✅ | `_implement` `while` + `cooldown_wait` (attempt harcamadan bekleme) |
| L | FAZ 2 A6 | ✅ | `dry_run` diske yazmaz/test çalıştırmaz; dolu diff döndürür |
| M | FAZ 3 B3 | ✅ | `llm_fail` failover logları |
| N | FAZ 4 C1/C2 | ✅ | qa cevabı `max_tokens=1500`; `decider_raw` |
| O | FAZ 5/D2 Jev smoke | ✅ | `discover.py` + `orchestra_ping` her iki endpoint'i dener |
| P | Testler | ✅ | **36/36 PASSED** (21 mevcut + 15 yeni `tests/test_plan_v2.py`) |
| Q | Bu dosya (`TODO.md` olarak taşınır) | ⏳ | İstenirse projeye kopyalanır |

---

### 3.c Durum Tablosu (2026-10-17) — Ücretsiz-only model yapılandırması (NVIDIA + OpenRouter `:free`)

**Kural:** Ücretli tek istisna **Jev** (`typesafe/jev-1.14`, OpenRouter). Diğer tüm roller yalnızca
**ücretsiz** kaynaklardan beslenir: NVIDIA NIM + OpenRouter `:free`.

| # | Madde | Durum | Not |
|---|-------|-------|-----|
| A | Ücretli OpenRouter modelleri kaldırıldı | ✅ | `z-ai/glm-5.3-flashx`, `deepseek/deepseek-v4.1-flash` artık kullanılmıyor |
| B | `groq` / `gemini` sağlayıcıları devre dışı | ✅ | Anahtarları geçersiz/kapsam dışı; `config.yaml`'da yorumda |
| C | Rol → ücretsiz model eşlemesi | ✅ | router/brain/council_*/decider/general/coder/judge yeniden yazıldı |
| D | `timeout` 60 → 120 | ✅ | NVIDIA uçları yavaş |
| E | Testler | ✅ | **36/36 PASSED** (A5 cooldown testi config'den türetilecek şekilde sağlamlaştırıldı) |
| F | `__pycache__` temizliği | ✅ | Taşınan projede bayat `.pyc` eski mutlak yolu taşıyordu → temizlendi |
| G | `coder` birincil = **DeepSeek (NVIDIA)** | ⚠️ | `deepseek-ai/deepseek-v4.1-flash` şu an **yanıt vermiyor** (180s timeout, tek token yok); yalnızca kullanıcı isteğiyle birincil. Timeout sonrası ücretsiz yedeklere düşer |
| H | `council_c` = **Nemotron 3 Super** | ✅ | `label: "Profesyonel · Nemotron 3 Super"`; adaylar: nvidia `super-120b` → OR `:free` → `gemma-4:free` |

**Bu hesapta NVIDIA gerçeği:** Yalnızca `nemotron-3-super-120b-a12b`,
`nemotron-3.5-super-vl-120b-a12b`, `nemotron-3-nano-omni-30b-a3b-reasoning` erişilebilir/çalışıyor.
`deepseek-ai/deepseek-v4.1-flash` katalogda **var ama yanıt vermiyor** (timeout). Diğerleri ya `404`
(hesapta yok) ya `timeout`/`503` (geçici). Bu yüzden 3 Nemotron modeli birden çok rolde yedek.

---

## 4. Yeni MCP Araçları (11 toplam)

| Araç | Açıklama |
|------|----------|
| `orchestra_run_task(goal, files?, complexity_hint?, dry_run?, stance?)` | Tam pipeline (LLM tabanlı). `stance`: auto\|agresif\|guvenli\|profesyonel |
| `orchestra_ping()` | Tüm modelleri test et |
| `orchestra_status()` | Token/çağrı istatistikleri |
| `orchestra_read_file(path, offset?, limit?)` | Dosya oku (sayfalama) |
| `orchestra_apply_edit(path, search, replace)` | Tek SEARCH/REPLACE (LLM'siz, anında) |
| `orchestra_write_file(path, content)` | Yeni dosya/tam yazım (LLM'siz) |
| `orchestra_list_files(subdir?, max_files?)` | Dosya ağacı listele |
| `orchestra_search(pattern, include?, max_results?)` | Regex arama |
| `orchestra_run_tests(command?)` | Test çalıştır (pytest auto-detect) |
| `orchestra_git(action, message?)` | git status/diff/commit |
| `orchestra_review(goal, diff)` | Sadece hakem incelemesi (uygulamaz) |

---

## 5. Kritik Teknik Notlar

- **MCP stdio transport:** `stdout` = JSON-RPC protokol kanalı. **Hiçbir `print` stdout'a yazmamalı.** `console.log` (stderr) kullanılır.
- **FastMCP sync tool'ları thread'e ATMAZ** (v1.30). Bloklayan işler `await anyio.to_thread.run_sync(...)` ile sarılmalı.
- **`opencode.json` şeması:** `mcp: { "local-orchestra": {...} }` (servers yok), `cwd: "."`, `permission: "local-orchestra_*": "allow"`, `provider` alanı `npm: "@ai-sdk/openai-compatible"`, `options.apiKey: "{env:NVIDIA_API_KEY}"`.
- **`--workspace ${cwd}` ÇALIŞMAZ** — opencode `${cwd}` değişkenini desteklemez. Server `Path.cwd()` kullanır.
- **Jev (TypeSafe System One):** OpenRouter üzerinden `typesafe/jev-1.14`. Endpoint öncelik: `https://openrouter.ai/api/alpha/decisions` (yedek `/api/v1/systemone`). Şema: `choice` sorusu + `criteria` sözlüğü; yanıt `answers[qid].choice` (NESNE). Anahtar: `OPENROUTER_API_KEY`. Kurul kararı **fail-closed**; geçersiz yanıt → `JevUnavailable` → görev durur. `Judge(llm, decide_with_jev=True)` ile açılır (varsayılan `False`).
- **Kurul kararı / tutumlar:** `config.yaml` → 3 üye: `council_a` (Agresif·Nemotron Ultra 550B), `council_b` (Güvenli·Nemotron 3.5 Super VL), `council_c` (Profesyonel·Nemotron 3 Super). `stance=auto` → 3 farklı tutum; açık tutum → 3 üye de o tutumda. Yalnızca `complex` görevlerde çalışır. Tek öneri üretilirse karar atlanır.
- **`decider` rolü:** yalnızca Jev kapalıyken LLM yedeği (nvidia/nemotron-3-super-120b-a12b). Jev devredeyken kullanılmaz.
- **Thinking regex:** Nemotron `<|begin_of_thought|>...<|end_of_thought|>` pattern'i + test mock uyumu.

---

## 6. Çalıştırma Komutları

```bash
# Testler
cd C:\Users\enesk\local-orchestra
.venv\Scripts\python.exe -m pytest tests/ -v

# Server manuel başlatma (OpenCode bağlar)
.venv\Scripts\python.exe mcp_server.py

# Model sağlık kontrolü (API key gerekir)
.venv\Scripts\python.exe discover.py --ping
```

---

## 7. Ortam / Anahtarlar

`.env` dosyasında (proje kökünde):
```env
OPENROUTER_API_KEY=sk-or-v1-...   # Jev (System One) + tüm ':free' modeller için ZORUNLU
NVIDIA_API_KEY=nvapi-...          # NVIDIA NIM (ücretsiz) — router/brain/council/decider/general/coder/judge
# GROQ_API_KEY=...                # GEÇERSİZ (xai-token her iki uçta da reddedildi) → devre dışı
# GEMINI_API_KEY=...              # Kapsam dışı (ücretsiz seçim NVIDIA + OpenRouter ile sınırlandı)
# JEV_AI_API_KEY artık KULLANILMIYOR (Jev OpenRouter üzerinden OPENROUTER_API_KEY kullanır).
```

**Ücretli kural:** Yalnızca **Jev** ücretlidir. Tüm diğer roller **ücretsiz** (NVIDIA NIM + OpenRouter `:free`).
`config.yaml` model kimlikleri: yalnızca bu hesapta erişilebilir NVIDIA Nemotron 3.x + OpenRouter `:free` modelleri.
Model ID'leri sağlayıcıda değişebilir → `discover.py --ping` / `orchestra_ping` ile doğrula.

---

## 8. Bilinen Riskler / Bekleyenler

- `config.yaml` model ID'leri sağlayıcıda yoksa `orchestra_ping` başarısız olur — bu kod değil, model/anahtar konusudur.
- `ruff` kurulu değilse statik kontrol atlanır (opsiyonel).
- Windows'ta `timeout` komutu yok; testlerde `subprocess.run(timeout=...)` kullanılır.

---

## 9. Context Dolunca Ne Yapılacak (Yeni Oturum İçin)

1. Bu dosyayı oku (`CONTEXT.md`)
2. Durum tablosundan **son bekleyen maddeyi** belirle
3. `pytest tests/ -v` çalıştır — hepsi geçmeli
4. Eksik/bozuk maddeyi tamamla
4. `CONTEXT.md`'yi güncelle (yeni tarih, durum değişiklikleri)
5. Commit / devam et

---

## 10. Sık Sorulan Sorunlar (Troubleshooting)

| Sorun | Çözüm |
|-------|-------|
| `test_failover_on_rate_limit` fail | `_THINK_RE` regex'i test mock'una uygun mu? (`x` pattern) |
| `orchestra_list_files(src)` boş dönüyor | `repo_map(WORKSPACE)` + filter mantığı doğru mu? |
| MCP server stdout hatası | `console.log` import edilip `print` yerinde kullanılmış mı? |
| OpenCode araçları görünmüyor | `opencode.json` şeması (`mcp` -> `servers` yok), `cwd`, `permission glob` doğru mu? |
| Jev AI çalışmıyor | Jev `OPENROUTER_API_KEY` kullanır; yoksa/geçersizse fail-closed. LLM yedeği için CLI'da `--no-jev`. |