# local-orchestra TODO / Uygulama Planı — 2026-10-17 (REVİZE-1)

> ## ✅ DURUM (2026-11-05) — Tarihsel plan tamamlandı
> Bu dosya **tarihsel referans** olarak korunur. Güncel durum ve tamamlanan işler için
> [`CONTEXT.md`](CONTEXT.md) §2.b ve [`CHANGELOG.md`](CHANGELOG.md) dosyalarına bakın.
>
> Tamamlananlar (özet): medya/OCR (`media.py` + `orchestra_read_media` + `run_task attach=`),
> eval harness (20 görev), geçici kopyada doğrulama, güçlendirilmiş `verify`, sembol haritası,
> esnek EDIT eşleşmesi, güvenlik kapıları (allowlist/onay/prompt-injection/log maskeleme),
> paketleme (pyproject, LICENSE, scripts/, CI).
>
> **Kalan iş fikirleri:** gerçek LLM ile periyodik eval koşusu (maliyet/kota nedeniyle manuel),
> Jev karar kalitesinin ölçümü, çoklu depo (monorepo) bağlam seçimi iyileştirmeleri.


> ## ⚠️ REVİZYON-2 (2026-10-17) — ÜCRETLİ MODEL YASAĞI (tek istisna: Jev)
> Aşağıdaki §0 kararları ve aday tabloları **artık geçersiz**: tüm ücretli OpenRouter modelleri
> (`z-ai/glm-5.3-flashx`, `deepseek/deepseek-v4.1-flash`) ve `groq`/`gemini` sağlayıcıları
> **kaldırıldı**. Geçerli yapılandırma `config.yaml`'dadır ve **yalnızca ücretsiz** kaynakları kullanır:
> **NVIDIA NIM + OpenRouter `:free`**; tek ücretli istisna **Jev** (`typesafe/jev-1.14`).
> Güncel rol→model tablosu için `CONTEXT.md` §3.c'ye bak. Bu dosya **tarihsel plan** olarak korunur.

> **Durum:** PLAN — revize edildi. **Kod değişikliği HENÜZ BAŞLATILMADI.**
> Revizyon gerekçesi: plan ile kod deposu birebir karşılaştırıldı; doküman doğrulaması yapıldı;
> 9 bulgu (1 P0: A6 dry_run) + **FAZ 1-F: kurul tutum (persona) sistemi** eklendi (bkz. §0 kararlar 5-7).
> Bu dosya uygulama rehberidir. Projeye taşımak isteyen `TODO.md` adıyla
> `C:\Users\enesk\local-orchestra\` altına kopyalayabilir.
>
> **Kapsam (kullanıcı kararı):** Rol rotasyonu + Jev decider + tüm P0/P1/P2 (A+B+C+D)
> + kurul tutum sistemi — tek geçişte uygulanacak.

---

## 0) Kullanıcı kararları (onaylandı)

1. **Deep rotasyon:**
   - `decider` → **Jev** (`typesafe/jev-1.14`, OpenRouter, `OPENROUTER_API_KEY`)
   - `council_b` → **GLM 5.3 FlashX** (eski decider; DeepSeek kuruldan çıkar)
   - `coder` → **DeepSeek V4.1 Flash** (birincil) + eski coder'lar yedek
   - `council_a` → Nemotron Ultra (aynen)
2. **coder adayları:** DeepSeek V4.1 Flash ilk, cohere/poolside yedek olarak kalır.
3. **Jev fallback YOK — fail-closed:** Jev geçerli karar veremezse görev durur (ok:False).
   (İstisna: tek öneri durumunda karar zaten atlanır — bkz. karar 6.)
4. **Kapsam:** A (P0) + B (P1) + C (P2) + D aday tablosu + rol rotasyonu + **F (tutum sistemi)**, hepsi.
5. **Jev endpoint** (onay: 17.10.2026): smoke test **her iki endpoint'i** dener; cevap döneni
   `judge.py`'deki `JEV_URL` sabitine yazılır:
   1) `POST https://openrouter.ai/api/alpha/decisions` — düz gövde, resmi tutorial'da curl ile
      dokümante → **birincil tercihen**
   2) `POST https://openrouter.ai/api/v1/systemone` — TypeSafe SDK yolu; çıplak HTTP gövde
      şekli dokümante değil (düz gövde muhtemeldir, smoke testle doğrulanır).
6. **Tek öneri = kararsızlık yok:** `len(proposals)==1` → Jev'e/LLM'e sorulMAZ;
   `tracker.log("decision_skipped_single_proposal")` + görünen uyarı; Jev fail-closed
   yalnızca 2+ öneride devrede.
7. **Kurul 3 üye / 3 tutum (FAZ 1-F):** `council_a=agresif`, `council_b=güvenli`,
   `council_c=profesyonel`. Tetik: complex görevlerde otomatik (`stance=auto`) +
   `orchestra_run_task(..., stance=...)` MCP parametresi. Açık tutum verilirse **3 üye de o
   tutumda** üretir (tutum içi strateji karşılaştırması). Çıktı: karşılaştırma tablosu + kazanan + gerekçe.

---

## 1) Doğrulanmış mevcut durum (stres testi + kod incelemesi)

- Test: canlı stres (temp `mcpsi-lab` projesi, `council_driver.py`, complexity=complex)
- Log: `runs/20261017-141532.jsonl`
- GÖREV FAIL: `LLMError` yakalanmadan dışarı taşındı (A5). Disk fail-closed doğru çalıştı.
- Router: Nemotron 3.5 Lightning her iki sağlayıcıda da down (probe ile doğrulandı).
- Kurul: `council_a` (nvidia 503) düştü → tek üye (`council_b`) kaldı, sessizce devam etti;
  **karar tek öneriyle dejenere verildi** (B1 + karar 6) — kod: `council.py:37-38` doğrulandı.
- Coder deneme 2 çıktısı `<<<END>>>`'siz ortadan kesilmiş → A3/A4 kanıtı ✅.
- **Yeni bulgular (kod depusuyla birebir doğrulandı):**
  - **A6 (P0):** `orchestrator.py:122` `write_with_backup` koşulsuz → **`dry_run` diske YAZIYOR**;
    `dry_run` ancak yazım sonrası kontrol ediliyor (`:161-163`) ve `result.get("staged", {})`
    `_implement` hiç döndürmediğinden (`:129`) dry-run diff **daima BOŞ**.
  - **GLM fail-open:** `_llm_judge` `None` dönerse `judge.py:83` sessizce `proposals[0]` seçip
    `decided_by = <decider label>` ile sunuyor → karar yanlış atfediliyor.
  - `council_a max_tokens: 4000` → `expand_prompt` kazananın rolüyle çalıştığından
    (`council.py:72-87`) Nemotron kazanırsa kesilme riski sürer (sadece council_b artmak yetmez).
  - `judge.py` durumu doğrulandı: `:11` `jev-ai.pro`, `:17` `JEV_AI_API_KEY`, `:27` `multiple_choice`+`choices`,
    `:37` düz string parse (bkz. §3 kritik bug).
  - `discover.py:46-61` Jev ping hâlâ `jev-ai.pro` + yanlış şema → FAZ 5/D2'de düzeltilir.
  - `main.py:29` `--no-jev` → `use_jev`; Jev decider için `decide_with_jev` olmalı.
  - `orchestrator.py:80` `for attempt` döngüsü → "attempt harcamadan continue" imkânsız; A5 `while`'a çevrilecek.
  - **OpenCode dokümanı (doğrulandı):** `opencode.json` MCP `timeout` yalnızca **tools/list** süresi
    (default 5000ms); tool call süresi için ayrı zaman aşımı YOK → eski "60s bekleme şartı" riski GEÇERSİZ.
  - `council_driver.py` depoda YOK (tüm kullanıcı ağacı tarandı) → stres harness'i §6'da yeniden yazılır.

---

## 2) Yeni mimari / hedef akış

```
router(groq/openrouter) → [complex]
   → kurul (3 üye / 3 tutum):
       council_a = Agresif     (Nemotron Ultra)
       council_b = Güvenli     (GLM 5.3 FlashX)
       council_c = Profesyonel (groq/openai/gpt-oss-120b)
   → Jev karar verir (OpenRouter, FAIL-CLOSED; tek öneride karar atlanır)
   → kazanan üye, tutumunu koruyarak coder için detaylı prompt yazar
   → coder(DeepSeek V4.1 Flash) → static check → judge review → backup/write → test → rollback
```

Karar bloğunda: "🧑‍⚖️ Karar veren: Jev (TypeSafe)" + karşılaştırma tablosu (FAZ 1-F).

---

## 3) Jev entegrasyonu — teknik gerçekler (resmi OpenRouter dokümanı ile doğrulandı)

- Jev, TypeSafe'in **System One** karar modelidir; OpenRouter üzerinden sunulur.
- Model ID: `typesafe/jev-1.14` (alias `~typesafe/jev-latest`; `typesafe/jev-1.13` de var).
- **İki HTTP yüzeyi** (aynı anahtar + aynı billing):
  | Yüzey | Endpoint | Not |
  |---|---|---|
  | Decisions API | `POST https://openrouter.ai/api/alpha/decisions` | düz gövde; curl örnekli resmi tutorial → **BİRİNCİL** |
  | System One API | `POST https://openrouter.ai/api/v1/systemone` | TypeSafe SDK yolu; çıplak HTTP gövdesi dokümante değil |
- Gövde: `{"model": "typesafe/jev-1.14", "state": <obj>, "questions": {qid: {...}}}`
- `choice` sorusu: `"type": "choice", "instructions": "...", "criteria": {<label>: "<açıklama>"}`
  — criteria KELİME→AÇIKLAMA **sözlüğüdür**, `choices` listesi DEĞİL.
- **Parse:** `body["answers"][qid]["choice"]` (yanıt nesnedir; `confidence`/`probabilities` yan alanlar).
- Çıktı tip verisidir; metin/gerekçe döndürmez → `reason=""`.
- Ücret: input token başına (model sayfasından doğrulanmalı; çıktı ücretsiz). Tek karar = tek çağrı.
- **State boyutu:** metin+questions için ~32 KiB sınırları raporlanıyor (sağlayıcıya göre değişebilir)
  → state kısaltılır: `goal ≤ 4000`, öneri başına `strategy/details/risks ≤ 1500` karakter.
- Anahtar: `OPENROUTER_API_KEY` (coder/council_b için de zaten zorunlu; ayrı Jev anahtarı GEREKMEZ).
- **Chat modeli DEĞİL** → `roles.decider` altına normal chat adayı olarak TANIMLANAMAZ; `_jev` yolu kullanılır.

### Kritik bug (mevcut kod — koddan doğrulandı)
`judge.py` şu an `https://jev-ai.pro/api/v1/systemone` + `JEV_AI_API_KEY` kullanıyor; payload
`"type": "multiple_choice"` + `"choices": [...]`; yanıt `body["answers"][qid]` düz string sanılıyor.
OpenRouter yanıtı NESNEdir → `answers[qid].choice`. Düzeltilmezse Jev sessizce hep `None` döner ve
LLM'e düşer (fail-open).

---

## 4) Uygulama planı — fazlar

### FAZ 0 — Sağlayıcı/ortam altyapısı (`config.yaml`, `.env.example`)

`config.yaml` → `providers` bloğuna ekle:
```yaml
  groq:
    base_url: https://api.groq.com/openai/v1
    api_key_env: GROQ_API_KEY
  gemini:
    base_url: https://generativelanguage.googleapis.com/v1beta/openai/
    api_key_env: GEMINI_API_KEY
```
- Kod değişikliği gerekmez; `llm.py` OpenAI-uyumlu istemciyle çalışır.
- `.env.example` → `GROQ_API_KEY`, `GEMINI_API_KEY` satırları + "Jev `OPENROUTER_API_KEY` kullanır" notu.
- `JEV_AI_API_KEY` satırı ".env'de kalabilir ama kullanılmaz" olarak işaretlenir.
- (Opsiyonel) `opencode.json` provider bloklarına groq/gemini; MCP için zorunlu değil.

---

### FAZ 1 — Rol rotasyonu + Jev decider + Kurul tutumları (FAZ-F dahil)

**1a. `judge.py`**
- `JEV_URL`: FAZ 5/D2 smoke test'in sonucundan gelen çalışan endpoint (varsayılan öncelik
  `https://openrouter.ai/api/alpha/decisions`, yedek `/api/v1/systemone`).
- Sabit `JEV_MODEL = "typesafe/jev-1.14"`.
- `__init__`: `self.key = os.getenv("OPENROUTER_API_KEY") if use_jev else None`
  (`JEV_AI_API_KEY` artık okunmaz).
  **Varsayılan parametreler DEĞİŞMEZ:** `use_jev=True, decide_with_jev=False`
  (`test_council_flow_reports_decision_author` buna bağlı).
- `_jev(state, qid, instructions, criteria)` yeniden yaz:
  - gövde:
    ```json
    {"model": "typesafe/jev-1.14",
     "state": <state>,
     "questions": {"<qid>": {"type": "choice", "instructions": "<...>",
                             "criteria": {"<label1>": "<açıklama>", "<label2>": "<açıklama>"}}}}
    ```
  - **parse:** `body["answers"][qid]["choice"]` (gerekirse `.value` uyumluluk fallback'i).
  - `tracker.add_jev(...)` + `tracker.log("jev", ...)` korunur; her türlü hata → `None` döndürmeye devam.
- Yeni istisna: `class JevUnavailable(Exception)`.

- `decide(goal, proposals)`:
  - **Tek öneri kısayolu (karar 6):** `len(proposals) == 1` → hiçbir çağrı yapılmaz;
    `winner = proposals[0]`, `decided_by = "karar gerektirmedi (tek öneri)"`,
    `reason = "Kurul tek öneri üretti."`, `tracker.log("decision_skipped_single_proposal", ...)`.
  - `decide_with_jev=True` (2+ öneri) → `_jev({goal, proposals(kısaltılmış)}, "best_option_id", q, criteria)`
    — criteria = `{id: "<tutum etiketi> — strateji özeti"}`.
    Geçerli cevap yoksa **`JevUnavailable` fırlat** (proposals[0]'a düşme YOK).
    Başarılıysa: `decided_by="Jev (TypeSafe)"`, `decider_model="typesafe/jev-1.14"`, `reason=""`.
  - `else` (Jev kapalı / CLI `--no-jev` / test) → mevcut LLM decider yolu **şu düzeltmeyle**:
    `ans is None` durumunda `proposals[0]` SESSİZ seçimi KALDIRILIR →
    `decided_by="karar verilemedi (decider model yanıt vermedi)"`, `decider_model=<cfg'den>`,
    `reason="decider yanıt vermedi; ilk öneriyle devam edildi"`, `tracker.log("decider_failed", ...)`
    (dürüst atıf; sessiz sahte "GLM kararı" kalmaz).
- `review()`: `_jev` yolu (`type=choice`, `criteria={"APPROVED": "...", "REJECTED": "..."}`);
  Jev `None` dönerse mevcut `_llm_judge` fallback (davranış korunur).
- **C2:** gerekçe boşsa raw yanıtı `tracker.log("decider_raw", raw=...)`; fallback `"(sebep yok)"`.

**1b. Çağrı yerleri**
- `mcp_server.py:89` → `_judge = Judge(_llm, use_jev=True, decide_with_jev=True)`
  (`__import__("os")` inline'u `os` import'u ile değiştirilir).
- `main.py:29` → `--no-jev` artık `Judge(llm, use_jev=not args.no_jev, decide_with_jev=not args.no_jev)`.
- `orchestrator.py _make_plan`: `judge.decide(...)` `try/except JevUnavailable` ile sarılır →
  `run_task` döner: `{"ok": False, "reason": "Jev karar veremedi (fail-closed)"}` (crash yok).

**1c. Kurul tutum (persona) sistemi — karar 7**
- `council.py`:
  - `COUNCIL_ROLES = ("council_a", "council_b", "council_c")` — eski "COUNCIL_ROLES değişmez"
    kuralı bu kararla GÜNCELLENDİ.
  - `STANCES` sözlüğü (kanonik anahtar → (etiket, talimat_metni)):
    - `aggressive` / "Agresif": büyük refaktör ve düzenlemeye açık, yeni bağımlılık kabul,
      en kısa yol, riski göze alan; hız ve etki öncelikli.
    - `safe` / "Güvenli": minimal değişim, geri uyumluluk, defansif kontrol ve hata mesajları,
      rollback düşünerek küçük adımlar; risk minimizasyonu öncelikli.
    - `professional` / "Profesyonel": temiz mimari ve best practice, test/typing/dokümantasyon,
      uzun vadeli sürdürülebilirlik öncelikli.
  - `STANCE_OF_ROLE = {council_a: "aggressive", council_b: "safe", council_c: "professional"}`.
  - Türkçe alias kabul: `agresif | guvenli | güvenli | profesyonel` (ve EN ekvivalanları) → kanonik anahtar;
    tanınamazsa `auto`'ya düşer + `tracker.log("stance_invalid")`.
  - `_one_proposal(llm, role, goal, context, stance_key)`: PROPOSAL_SYSTEM korunur; tutum talimatı
    user mesajına eklenir; dönen veriye `data["stance"] = stance_key` iliştirilir.
  - `generate_proposals(llm, goal, context, stance="auto")`:
    - `auto` → her üye `STANCE_OF_ROLE[role]` tütumuyla → **3 FARKLI tutum** aynı anda karşılaştırılır.
    - açık tutum → **3 üye de o tutumla** üretir (tutum içi strateji çeşitliliği).
  - `expand_prompt(llm, winner, goal, context)`: kazananın `stance` bilgisi yönergeye eklenir
    ("Bu çözümü <etiket> tutumda uygula: <talimat özeti>") + `llm.chat(..., max_tokens=8000)`
    explicit override (kazanan rolü hangisi olursa olsun kesilme riski kapanır).
- `config.yaml`:
  - `council_a`: `max_tokens: 4000 → 8000` (B2 mantığı tüm üyelere), `label: "Agresif · Nemotron Ultra"`.
  - `council_b`: `max_tokens: 4000 → 8000`, `model: z-ai/glm-5.3-flashx` (DeepSeek çıkar),
    `label: "Güvenli · GLM 5.3 FlashX"`.
  - YENİ rol `council_c`: `label: "Profesyonel · gpt-oss-120b"`, `max_tokens: 8000`,
    adaylar: `groq/openai/gpt-oss-120b` → `gemini/gemini-2.5-flash` (D1 tablosuyla tutarlı).
- `orchestrator.py`:
  - `run_task(..., stance="auto")` parametresi → `_make_plan` → `generate_proposals(..., stance=stance)`.
  - Karar logu zenginleşir: `tracker.log("decision", ..., stance=<winning stance>)` ve
    `decision["comparison"] = [{stance, author_label, author_model, strategy, risks}, ...]`
    (`_decision_of` bunun üzerinden geçirir).
  - "👥 Kurul fikir üretiyor..." logu ve decision_block metinleri 3 üye/tutuma göre günceller.
- `mcp_server.py`:
  - `orchestra_run_task(goal, files, complexity_hint, dry_run, stance="auto")` —
    **tutum-belirtme tetikleyicisi bu MCP parametresidir** (skill değil). Docstring:
    `"auto | agresif | guvenli | profesyonel"` (+ EN alias'lar).
  - `_decision_block`'a markdown **karşılaştırma tablosu**:
    `| # | Tutum | Üye (model) | Strateji | Riskler |` → altında Karar veren / Kazanan / Gerekçe.

**1d. Kurul görünürlüğü (FAZ 3-B1 ile birleşik)**
- `_one_proposal` `LLMError` yakalayınca: `console.log` uyarısı +
  `tracker.log("council_member_failed", role=role, error=str(e))` (3. üye dahil).
- `generate_proposals`: tek öneri kalırsa görünür uyarı (karar 6 kısayoluyla uyumlu);
  **0 öneri** kalırsa `tracker.log("council_empty")` + görünür uyarı (complex görev sessizce
  simple'a düşerken logta iz bırakır).

---

### FAZ 2 — P0 (crash / yanlış sınıflama / kesilme / dry-run)

**A1 | qa kontrolü override'ı bypass ediyor** — `orchestrator.py:150-153`
- Şimdi: `if route["type"] in ("qa","writing"): return answer` → override'dan ÖNCE.
- Çözüm: override'ı önce uygula; kısayol yalnız `complexity != "complex"` iken çalışsın:
  ```python
  if complexity_override in ("simple", "complex"):
      route["complexity"] = complexity_override
  if route["type"] in ("qa", "writing") and route["complexity"] != "complex":
      return {"ok": True, "answer": _answer(llm, goal, route, files_ctx)}
  ```
- Kabul: qa route + override=complex → kurul devreye giriyor.

**A2 | router tek model ailesiyle failover** — `config.yaml:16-20`
- İki aday da Nemotron 3.5 Lightning → nvidia+openrouter down iken router ölü.
- Çözüm: FAZ 5 tablosundaki farklı aile adaylarını ekle.
- Kabul: nvidia down iken router hâlâ çalışıyor.

**A3 | max_token kesilmesi sessiz geçiyor** — `llm.py:78-91`
- Yanıt sonrası `resp.choices[0].finish_reason == "length"` →
  `tracker.log("llm_truncated", ...)` + `raise LLMError("çıktı max_tokens'ta kesildi")`
  (mevcut `except LLMError` bloğu sıradaki adaya geçirir).
- Kabul: 7997/8000 kesilmesi sessiz kalmaz; tüm adaylar kesilirse feedback coder'a "kesildi" bilgisi gider.

**A4 | coder token bütçesi + talimat** — `config.yaml:53-58` + `coder.py SYSTEM`
- coder `max_tokens: 8000 → 16000`.
- `coder.py SYSTEM`'a: "Çok dosyalı görevde öncelik sırası: önce zorunlu/kritik dosyaları eksiksiz yaz;
  bağlam riski varsa en önemlilere öncelik ver; her bloğu mutlaka `<<<END>>>` ile kapat."
- Kabul: yarım blok ne `ApplyError` ne sessiz blok düşümü üretsin.

**A5 | LLMError 429'da crash + `for` döngüsü yapısı** — `orchestrator.py:78-137`
- `llm.py`'ye helper: `cooldown_wait(role) -> float` (rolün adayları arasındaki en kısa kalan cooldown).
- `_implement`: `for attempt in range(...)` → **`while` döngüsü + ayrı sayaçlar**
  (`attempts_done`, `cooldown_waits`) — "attempt harcamadan devam etmenin" tek yolu bu.
- `coder.generate` çağrısı `try/except LLMError` ile sarılır:
  - hata + cooldown > 0 → logla, `time.sleep(min(wait, 65))`, **attempt harcamadan** devam;
    sonsuz döngü koruması: en fazla 5 cooldown beklemesi, aşılırsa `ok:False`.
  - hata + cooldown = 0 → return `{"ok": False, "reason": ...}`.
- Kabul: 429'da traceback yok; bekleme + devam var.
- **Not:** OpenCode MCP `timeout` yalnızca tools/list içindir (bkz. §7) → 65s üst sınır zorunluluk
  değil, UX tercihidir.

**A6 (YENİ, P0) | dry_run diske yazıyor + boş diff** — `orchestrator.py:122,129,161-163`
- `_implement(..., dry_run=False)` parametresi eklenir: `dry_run=True` iken
  `workspace.write_with_backup` ve `verify.run_tests` ÇAĞRILMAZ;
  sonuç `{"ok": True, "dry_run": True, "files": list(staged), "diff": diff}` (diff satır 98'de
  zaten pre-write hesaplanıyor).
- `run_task:161-163`: ölü `result.get("staged", {})` ifadesi kaldırılır; dry-run bloğu
  `result["diff"]` döndürür (`decision` dahil).
- Kabul: `dry_run=true` → sıfır dosya değişikliği + dolu diff + backup/rollback yok.

---

### FAZ 3 — P1 (kurul / görünürlük)

**B1 | üye düşüşleri sessiz** — FAZ 1d'de işlendi (3 üye + `council_empty`).
**B2 | expand prompt max_tokens** — FAZ 1c'de işlendi (tüm üyeler 8000 + "kısa ve eksiksiz").
**B3 | başarısız/rotasyonlu çağrılar loglanmıyor** — `llm.py`
- Failover noktalarında (`RateLimitError`, timeout/conn, 5xx, `break`) →
  `tracker.log("llm_fail", role=role, provider=cand["provider"], model=cand["model"], error=str(e)[:300])`.
- Kabul: JSONL'de down/failover nedenleri görünür.

---

### FAZ 4 — P2 (küçükler)

- **C1** | qa cevap cap — `orchestrator.py:22-25` `_answer` → `llm.chat(role, msgs, max_tokens=1500)`.
- **C2** | decider boş reason — FAZ 1a'da işlendi (`decider_raw` log + `"(sebep yok)"`).

---

### FAZ 5 — D1 Görev bazlı sağlayıcı planı (`config.yaml`)

API kod değişikliği YOK; sadece `config.yaml` + `.env` (`llm.py` OpenAI-uyumlu).
**D0 — Anahtar alım (kartsız, ücretsiz):** `GROQ_API_KEY` (console.groq.com/keys),
`GEMINI_API_KEY` (aistudio.google.com/apikey).

**D1 — Rol bazlı aday tablosu (sıra = failover sırası):**

| Rol | Mevcut sorun | Adaylar (sıra) |
|-----|--------------|----------------|
| router | İki aday da down → qa'ya düştü | 1) groq/llama-3.1-8b-instant 2) groq/openai/gpt-oss-20b 3) openrouter/deepseek-v4.1-flash |
| coder | 429 + max_token kesilme | 1) deepseek/deepseek-v4.1-flash 2) groq/openai/gpt-oss-120b 3) groq/moonshotai/kimi-k2-instruct 4) gemini/gemini-2.5-flash (yedek: cohere/north-mini-code:free, poolside/laguna-s-2.1:free, laguna-xs-2.1:free) |
| judge | coder ile aile benzerliği (coder=deepseek) | 1) groq/llama-3.3-70b-versatile 2) gemini/gemini-2.5-flash |
| council_a | nvidia 503, openrouter free yetersiz | 1) groq/openai/gpt-oss-120b 2) gemini/gemini-2.5-flash |
| council_b | GLM'e taşınıyor | z-ai/glm-5.3-flashx |
| **council_c (YENİ)** | — | 1) groq/openai/gpt-oss-120b 2) gemini/gemini-2.5-flash |
| brain | council_a ile aynı zincir | 1) groq/openai/gpt-oss-120b 2) gemini/gemini-2.5-flash |
| decider | Jev'e taşınıyor (fallback) | groq/openai/gpt-oss-120b |
| general | — | 1) groq/llama-3.3-70b-versatile |

**D2 — Doğrulama:**
- `discover.py --ping` / `orchestra_ping` → yeni adayların tümü onaylı (model ID'ler hızlı değişir).
- `llm.py` `json_mode`: groq/gemini `response_format` desteği; 400 → non-json fallback zaten var.
- **Jev smoke test (YENİ):** `discover.py` (veya tek atımlık script) her iki endpoint'i düz gövdeyle
  dener (`choice` sorusu, `criteria` sözlüğü), `answers[qid].choice` parse'ını doğrular;
  çalışan endpoint `judge.py` `JEV_URL` sabitine yazılır.
- `discover.py:46-61` Jev ping bloğu: `jev-ai.pro` + `JEV_AI_API_KEY` + `multiple_choice/choices`
  → OpenRouter + `OPENROUTER_API_KEY` + `choice/criteria` şemasına çevrilir.
- `orchestra_ping`'e tek bir Jev karar çağrısı satırı eklenir (chat modelleri Jev'i DOĞRULAYAMAZ).

---

### FAZ 6 — Son doğrulama (tüm işler bitince) — orijinal §6

1. `.venv\Scripts\python.exe -m pytest tests/ -v` → 21/21 (+ yeni testler)
2. `.venv\Scripts\python.exe discover.py --ping` → tüm adaylar + Jev smoke onaylı
3. Stres tekrarı (temp proje; `council_driver.py` YOK → **harness yeniden yazılır veya
   `main.py --auto` + temp proje kullanılır**):
   - 3 üye görünür (Agresif/Güvenli/Profesyonel), karşılaştırma tablosu sonuçta
   - tek üye/tek öneri durumlarında görünen uyarılar
   - kesilme sinyali coder feedback'ine ulaşır; 429'da bekleme + devam (crash yok)
   - `dry_run=true` → diske yazım yok + dolu diff
   - disk yazım/yedek/rollback mekanizması yeşil; hedef: FAIL yerine OK
4. `CONTEXT.md` güncelle (§2/§5: 3 üye + tutumlar + `stance` parametresi + Jev/OpenRouter + A6).
5. **opencode'u kapat-yeniden aç** — MCP subprocess'i çalışırken kod değişikliklerini yüklemez.
6. Bu dosya istenirse `TODO.md` olarak projeye taşınır.

---

## 5) Testler

**Mevcut koruma notu:** `Judge` varsayılanları değişmeyeceği için (`use_jev=True, decide_with_jev=False`)
`test_council_flow_reports_decision_author` LLM decider yolundan geçer; yalnızca 3. üye
(`council_c`) beklentisiyle **güncellenir** (`FakeCouncilLLM.cfg`'ye `council_c`,
`seen`'e kontrollü kayıt).

**Yeni testler:**
- Jev parse (mock `requests.post`, `answers[qid].choice`; yanlış şema → `None` → JevUnavailable).
- Fail-closed: Jev geçersiz/boş yanıt → `ok:False` (crash yok, proposals[0]'a düşme yok).
- İki-endpoint smoke mock'u: birincil down → ikincil denenir; işe yarayanı `JEV_URL`'e yazma logic'i.
- Tek öneri: decide çağrısı atlanır + `decision_skipped_single_proposal` logu.
- GLM/LLM decider `None` → dürüst `decided_by` etiketi (`decider_failed` logu).
- A1: qa route + override=complex → kurul çağrıldı.
- A3: `finish_reason="length"` → LLMError/feedback.
- A5: coder LLMError + cooldown > 0 → attempt harcamadan devam (while yapısı).
- A6: `dry_run=True` → disk değişmez (`app.py` içeriği korunur) + diff dolu.
- B1/B1d: tek/0 üye uyarıları (`council_member_failed`, `council_empty`).
- C1: qa cevabı max_tokens=1500 override.
- **Tutum sistemi:** (a) `stance="auto"` → 3 öneride 3 FARKLI `stance`; (b) `stance="agresif"` →
  3 önerinin hepsi `stance="aggressive"`; (c) `result["decision"]["comparison"]` 3 satır dolu;
  (d) tanınmayan `stance` → `auto` + `stance_invalid` logu.

---

## 6) Riskler / açık noktalar

- **Jev ücretli:** input token başına (çıktı ücretsiz); karar tek çağrı → maliyet çok düşük.
  Fiyat model sayfasından doğrulanır (kaynaklarda ~$0.042/M girdi).
- **Jev gerekçe döndürmez** → karar bloğunda `"(sebep yok)"` + `decider_raw` logu.
- **MCP timeout (DÜZELTİLDİ):** eski plan "MCP `timeout: 60000` tool çağrısını sınırlar → bekleme
  ≤60s" diyordu; OpenCode dokümanına göre `timeout` **yalnızca tools/list** süresidir
  (default 5000ms), tool call süresi için ayrı sınır YOK → A5'te 65s üst sınır zorunluluk değil,
  UX tercihidir (maks 5×~65s bekleme bütçesi korunur).
- **Jev anahtar yoksa fail-closed** → görev durur (kullanıcı onayı). `OPENROUTER_API_KEY`
  coder(deepseek) + council_b(glm) için de zaten zorunlu.
- **3 üye = +1 çağrı/latency** — ücretsiz modellerle maliyet ~0; latency artışı why **E-parallel
  artık öncelikli** bir fikir haline gele (bkz. §8).
- **Jev state 32 KiB** → state kısaltma (§3) uygulanır.
- **Model ID kırılganlığı:** groq/gemini/deepseek ID'leri hızlı değişir → `--ping` + Jev smoke ile doğrula.
- **`council_c` model down olursa:** B1/B3 logları görünürlük sağlar; 2 öneriyle karar işlemi devam eder.
- **Jev/`stance` etkileşimi:** Jev `criteria` metni tutum etiketini içerir → karar gerekçesiz bile
  atıfta bulunulabilir; `comparison` tablosu insan okur.
- **E fazı (dry-test, tartışma, bütçe)** bu plana dahil değil; ayrı tasdik bekler.

---

## 7) E) Faz 2 fikirler (uygulama değil, tasdik)

- **E-parallel:** `council.py` üyeleri `ThreadPoolExecutor` ile eşzamanlı çağrılır →
  **3 üyeyle artık ÖNCELİKLİ** (latency ~%50-60 azalır). Tracker/console çıktılarının thread
  güvenliğine dikkat.
- **E-budget:** coder'a plan + files_ctx için dosya başına uzunluk limiti (büyük bağlam riski).
- **E-drytest:** dry_run'da staged içeriği geçici klasörde pytest (A6 sonrası ek膨胀; rollback sayısı azalır).
- **E-debate:** council v2 "gerçek tartışma" — kazananın önerisini kaybeden eleştirir, 2. turda final plan (ek ~2-3 çağrı).
- **E-skill (opsiyonel):** opencode global skill `~/.config/opencode/skills/local-orchestra/SKILL.md`
  — "ne zaman run_task (dry_run önce), ne zaman apply_edit/write_file; önce ping" kullanım rehberi.
  (Kurul tutumları `stance` parametresiyle artık MCP'de; skill yalnızca kullanım tavsiyesi verir.)

---

## 8) Uygulama sırası (özet清单)

1. FAZ 0 (providers + .env.example) → config taban
2. FAZ 5/D0-D2 ön-hazırlık: anahtarlar + Jev smoke (endpoint sabitlenir) + ping doğrulama
3. FAZ 1a-1d (Jev + rotasyon + tutumlar + kurul görünürlüğü) — tek config/council geçişi
4. FAZ 2 (A1-A6) — P0'lar
5. FAZ 3-4 (B3, C1) — kalan P1/P2
6. §5 test paketi → FAZ 6 doğrulama → CONTEXT.md → opencode restart
