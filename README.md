# local-orchestra

Yerel (local-first) LLM orkestrasyonu. Bir görevi alır, **3 bağımsız kıdemli yazılım uzmanından (Council)** paralel öneri toplar, **Jev (TypeSafe System One)** ile kazananı seçer, ardından **alt coder** ile değişiklikleri üretir ve **statik doğrulama + test + hakem incelemesi** sonrasında diske yazar.

> Repo adı: `local-orchestra` (klasör adıyla aynı)

---

## 1. Amaç

`local-orchestra`, karmaşık kod görevlerini güvenli ve şeffaf bir boru hattıyla çözmek için tasarlanmış bir orkestratördür. Temel ilkeler:

- **Fail-closed (güvenli varsayım):** Hakem/Jev geçerli karar veremezse işlem durur (onay verilmez).
- **Şeffaflık:** Hangi model/sağlayıcının gerçekten yanıt verdiği (fallback dahil) loglanır; kurul karar dökümü tam olarak saklanır.
- **Belirleyicilik + denetlenebilirlik:** Her adım JSONL loglara (`runs/`) yazılır, yedek/geri alma (`backups/`) vardır.
- **Yerel çalışma:** Değişiklikler çalışma alanında uygulanır; dışarı çıkış güvenli yollarla sınırlandırılır.

---

## 2. Mimari Özet

Boru hattı kabaca şu sırayla ilerler:

1. **Router** (`router.py`) — Görevi sınıflandırır: `code | file_task | qa | writing`, karmaşıklık `simple | complex`, ilgili dosyalar.
2. **Council (Kurul)** (`council.py`) — **3 bağımsız kıdemli yazılım uzmanı** paralel olarak öneri üretir.
3. **Judge/Decider (Karar)** (`judge.py`) — **Jev (TypeSafe System One)** birincil karar verici. Geçerli yanıt veremezse **LLM yedeği** (`decider`) devreye girer (fail-closed).
4. **Expand (Genişletme)** — Kazanan üye, seçilen stratejiyi **coder**'ın doğrudan uygulayabileceği detaylı yönergeye dönüştürür.
5. **Coder (Alt Kodlayıcı)** (`coder.py`) — `FILE` / `EDIT` blokları üretir. Değişiklikler **bellekte** uygulanır (disk yazımı sonraki aşamada).
6. **Verify (Doğrulama)** (`verify.py`) — AST/JSON/YAML statik kontrol + `ruff` (E9,F63,F7,F82) + **pytest** testleri.
7. **Review (Hakem İncelemesi)** (`judge.review`) — Jev/LLM hakem diff'i değerlendirir (fail-closed: yanıt yoksa onaylanmaz).
8. **Workspace** (`workspace.py`) — Güvenli yol kontrolü, yedekleme (`backups/`), geri alma (`rollback`), opsiyonel Git commit.
9. **Tracker** (`tracker.py`) — Token/çağrı sayacı + JSONL çalışma günlüğü (`runs/`).

---

## 3. Kurul (Council) Mekanizması — Detaylı

`council.py`, **3 bağımsız kıdemli yazılım uzmanı** (`COUNCIL_ROLES = council_a, council_b, council_c`) tarafından **paralel** öneri üretir.

### 3.1 Paralel Çalışma

- `ThreadPoolExecutor` ile eşzamanlı üretim (bounded: 3 işçi).
- **Toplam süre ≈ en yavaş üyenin süresi** (sürelerin toplamı değil).
- API kotası dengeli kullanılır; bir üyenin gecikmesi diğerlerini bloklamaz.

### 3.2 Üye Kimliği & Fallback Şeffaflığı

Her üye `config.yaml`'deki rolüne göre çalışır. Öneriye eklenen alanlar:

- `author_role`, `author_label`
- `author_provider`, `author_model` — **gerçekten yanıt veren** sağlayıcı/model
- `fell_back` (bool) — birincil model yerine yedek model yanıt verdi mi?
- `duration_s` — üretim süresi

Birincil model (`_primary_model`) ile gerçek kullanılan model farklıysa `fell_back = true` olarak işaretlenir ve loglanır.

### 3.3 JSON Güvenilirliği

- Sistem prompt'u katı JSON şeması ister: `{"strategy","details","files","risks"}`
- JSON parse hatasında **alan kurtarma (`_salvage`)** devreye girer:
  - Regex ile `strategy/details/risks` alanları çıkarılır
  - `files` dizisi kurtarılmaya çalışılır
- Kurtarma başarılıysa üye kaybı önlenir; olay `council_json_salvaged` olarak loglanır.
- 2 deneme sonrası geçerli öneri üretilemezse o üye sessiz düşmez: `council_member_failed` / `council_member_no_output` loglanır.

### 3.4 Öneri Kalitesi

Kök neden odaklılık, mevcut mimari/kod stiline uyum, üretim kalitesi (tip ipuçları, hata yönetimi, kenar durumları), test edilebilirlik ve risklerin dürüstçe belirtilmesi vurgulanır. `details` kısa tutulur (~200 kelime yönlendirmesi); uzun kod bloğu istenmez.

### 3.5 Çıktı Şeffaflığı

Her öneri terminale özet olarak basılır (Strateji/Detay/Dosyalar/Riskler + süre + fallback notu). Tüm karşılaştırma `decision.comparison` içinde tam olarak saklanır.

---

## 4. Karar (Judge / Jev) Mekanizması — Detaylı

`judge.py` karar aşamasının çekirdeğidir. **Fail-closed** prensibi esastır.

### 4.1 Jev (TypeSafe System One) — Birincil

Jev, **chat modeli değil**; yapılandırılmış `choice` sorusu + `criteria` sözlüğü ile çalışır. Yanıt biçimi: `body["answers"][qid]["choice"]` (nesne).

- **Endpoint (birincil + yedek):**
  - Birincil: `https://openrouter.ai/api/alpha/decisions`
  - Yedek: `https://openrouter.ai/api/v1/systemone`
- **Model:** `typesafe/jev-1.14`
- **Kimlik doğrulama:** OpenRouter üzerinden `OPENROUTER_API_KEY`.
- **Smoke test:** `discover.py --ping` ile her iki endpoint test edilebilir.

### 4.2 Kurul Kararı (Decide)

`decide(goal, proposals)`:

- **Tek öneri kuralı:** `len(proposals) == 1` ise **karar çağrısı yapılmaz.** Dönüş: `("karar gerektirmedi (tek öneri)", "-", "Kurul tek öneri üretti")`. Gereksiz Jev çağrısını önler.
- **Jev ile karar:** `state = {goal, proposals: [_slim(p)...]}`, `criteria = {id: kısa özet}` ile `best_option_id` sorulur.
- **Fail-closed:** Jev geçerli öneri seçemezse (`answer not in ids`) `JevUnavailable` fırlatılır → orchestrator görevi durdurur ("Jev karar veremedi (fail-closed)").
- **Yakın karar tespiti (`_is_close`):**
  - `CLOSE_CONFIDENCE = 0.30` — Jev güveni bu altındaysa "yakın"
  - `CLOSE_PROB_GAP = 0.10` — birinci–ikinci olasılık farkı bu altındaysa "yakın"
- **Deep Protokol (4 adımlı)** — **Sadece yakın kararlarda** tetiklenir (`len(proposals) > 2` ve `_is_close`):
  1. **3 boyut** tek `jev_multi` çağrısında puanlanır: `kapsam`, `sadelik`, `sürdürülebilirlik` (`DIMENSIONS`)
  2. Boyut sonuçları `state2["boyut_puanlari"]` olarak nihai soruya eklenir
  3. **Nihai oylama** (`FINAL_Q_DEEP`) ile karar verilir
  4. Nihai karar hâlâ yakınsa **deterministik eşitlik bozucu** uygulanır
- **Deterministik Eşitlik Bozucu (`_deterministic_tiebreak`):**
  1. **Kural 1:** En az dış bağımlılık/varsayım (metinde `pip install`, `import`, `from ... import`, `requirements.txt` izleri sayılır — `_dep_count`)
  2. **Kural 2:** En sade ve kısa çözüm (dokunulan dosya sayısı, ardından metin uzunluğu)
- **LLM Yedek (`decide_with_jev=False`):** `decider` rolü JSON ile karar verir. Yanıt yoksa **dürüst etiket** ile ilk öneriyle devam edilir ve olay `decider_failed` olarak loglanır.

### 4.3 Kod İncelemesi (Review)

`review(goal, diff)` → `(status, reason)`; `status`: `APPROVED | REJECTED | UNAVAILABLE`

- Soru: Değişiklik hedefi tam karşılıyor mu, belirgin mantık/güvenlik hatası var mı?
- **Fail-closed:** `UNAVAILABLE` dönerse çağıran taraf onaylamaz; orchestrator ilgili durumda geri bildirimle yeniden dener.
- Diff kısaltılır (maks 20000); reddedilme gerekçesi ayrı üretilir.

### 4.4 Yardımcılar

- `choose`, `precheck` — plan ön denetimi/tavsiye amaçlı.
- `_jev_reason` — Jev kararını insan-okur gerekçeye çevirir (confidence, rationale vb.).
- `last_jev`, `last_jev_multi` — karar şeffaflığı için ham yanıt saklanır.

---

## 5. Kod Üretimi (Coder) & Uygulama

`coder.py` yapılandırılmış blok formatı kullanır; açıklama dışarıda yazılmaz, **sadece bloklar** döner.

### 5.1 Blok Formatı

```text
<<<FILE: yol/yeni.py>>>
tam dosya içeriği
<<<END>>>

<<<EDIT: yol/dosya.py>>>
<<<SEARCH>>>
eski kod (dosyada BİREBİR ve TEK kez geçmeli)
<<<REPLACE>>>
yeni kod
<<<END>>>
```

Kurallar: Mevcut dosya değişimi için EDIT, yeni/tamamen yeniden yazılacak küçük dosya için FILE. Her blok `<<<END>>>` ile kapatılmalı; yalnızca göreli yollar kullanılır. Güvensiz kod yürütme (`eval`, `new Function`) yasaktır.

### 5.2 Ayrıştırma & Bellekte Uygulama

- `parse_changes` blok sırasını korur (deterministik).
- `apply_in_memory(root, changes)` **diske yazmadan** yeni içerikleri hesaplar → `{rel: yeni_içerik}`.
- EDIT için `base.count(search) == 1` şartı (tam 1 kez bulunmalı); aksi halde `ApplyError`.
- Güvenli yol: `workspace.safe_path` (çalışma alanı dışı ve gizli dosyalar engellenir).

---

## 6. Doğrulama (Verify)

`verify.static_check(staged)`:

- `.py` → `ast.parse` (SyntaxError yakalanır)
- `.json` → `json.loads`
- `.yaml/.yml` → `yaml.safe_load`
- `.py` ve sorun yoksa **`ruff`** çalıştırılır (`--select E9,F63,F7,F82`, stdin); çıktı kısaltılarak raporlanır.

`verify.run_tests(root, command=None, timeout=180)`:

- Test yoksa (`tests/` dizini veya `test_*.py`) → `True, "test bulunamadı, atlandı"`
- Varsayılan: `python -m pytest -x -q -p no:cacheprovider`
- `PYTHONDONTWRITEBYTECODE=1` (bytecode yeniden yazım çatışmalarını azaltır)
- `returncode == 5` (hiç test toplanmadı) → atlandı; `No module named pytest` → atlandı.

---

## 7. Çalışma Alanı & Güvenlik (Workspace)

`workspace.py`:

- **Gizli dosya koruması** (`SECRET_PATTERNS`): `.env`, `.env.*`, `*.pem`, `*.key`, `id_rsa*`, `*.pfx`, `*.sqlite`, `*.db` → erişim/okuma engellenir (`WorkspaceError`).
- **Güvenli yol** (`safe_path`): `(root/rel).resolve()` + `is_relative_to(root)` kontrolü (path traversal engeli).
- **Repo haritası** (`repo_map`): Yalnızca metin uzantıları; gizli dosyalar ve `EXCLUDE_DIRS` (`.git, node_modules, __pycache__, .venv, venv, ...`) atlanır (maks 300 dosya).
- **Dosya okuma** (`read_files`): `max_chars = 60000`, kısaltma ve `errors="replace"`.
- **Diff** (`make_diff`): `difflib.unified_diff`.
- **Yedek + Geri Alma:** `write_with_backup` → her değiştirilen dosya için `backups/<repo>/<YYYYMMDD-HHMMSS>/<rel>` kopyalanır, `snapshot` döner; `rollback` ile geri alınır.
- **Opsiyonel Git:** `git_checkout_branch`, `git_commit` (CLI `--git`).

---

## 8. CLI (`main.py`)

```text
python main.py --workspace . [--auto] [--git] [--no-jev]
```

- `--auto`: Onay sormadan uygula (riskli).
- `--git`: Ayrı dal aç (`orchestra/YYYYMMDD-HHMMSS`) ve her görevi commit'le.
- `--no-jev`: Jev yerine yalnızca LLM hakem kullan (`decide_with_jev=False`).

---

## 9. MCP Server (`mcp_server.py`)

OpenCode ile stdio (JSON-RPC 2.0) üzerinden haberleşir. Araçlar (FastMCP):

| Araç | Açıklama |
|---|---|
| `orchestra_run_task` | Orkestra boru hattını çalıştırır (kurul + karar + kod + doğrulama + uygulama). `attach=` ile PDF/görsel eklenebilir. |
| `orchestra_read_media` | PDF / görsel / metin → metin. Dijital PDF'te metin katmanı, taranmışta Windows OCR. |
| `orchestra_ocr_status` | Windows OCR motorunu ve yüklü OCR dillerini raporlar. |
| `orchestra_ping` | Tüm modelleri test eder (config.yaml adayları) + Jev smoke testi. |
| `orchestra_status` | Oturum token/çağrı istatistiği (tracker). |
| `orchestra_read_file` | Dosyayı okur; PDF/görselleri otomatik metne çevirir. |
| `orchestra_apply_edit` | Tek SEARCH/REPLACE düzenlemesi (LLM'siz, `verify=true` ile test). |
| `orchestra_write_file` | Yeni dosya yaz / tamamen değiştir (LLM'siz, `verify=true` ile test). |
| `orchestra_read_many` | Birden çok dosyayı tek çağrıda okur. |
| `orchestra_list_files` | Proje dosya ağacını listeler. |
| `orchestra_glob` | Glob deseniyle dosya arar. |
| `orchestra_search` | Kod içinde regex arama. |
| `orchestra_run_tests` | Testleri çalıştırır (komut allowlist'i + otomatik algılama). |
| `orchestra_git` | `status` / `diff` / `log` / `commit`. |
| `orchestra_undo` | Son yazma işlemini yedekten geri alır. |
| `orchestra_review` | Sadece hakem incelemesi yapar (değişiklik uygulamaz). |

### Medya / OCR

`Windows.Media.Ocr` Windows 10/11'e **gömülüdür**: ücretsiz, çevrimdışı, API anahtarı
gerektirmez. OCR dil paketi kurulu değilse araç kurulum komutunu hata mesajında döner:

```powershell
Add-WindowsCapability -Online -Name "Language.OCR~~~tr-TR~0.0.1.0"
Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
```

Araçlar yalnızca çalışma alanı içindeki göreli yolları okur; başka klasörler için
`ORCHESTRA_MEDIA_ROOTS` (veya `config.yaml → media_roots`) ile açık izin verin.


`opencode.json` (lokal config) MCP sunucusunu bu script ile başlatır. Repoda **`opencode.example.json`** verilir; **`opencode.json` `.gitignore`'dadır** (her ortamın kendi yollarını kullanması için).

---

## 10. Kurulum

### Gereksinimler

- Python 3.11+
- Ortam değişkenleri: `.env` (asla repoya eklenmez)

### Adımlar

```bash
# 1. Sanal ortam
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/macOS
# source .venv/bin/activate

# 2. Bağımlılıklar
pip install -r requirements.txt

# 3. Ortam değişkenleri
Copy-Item .env.example .env   # Windows PowerShell
# cp .env.example .env        # bash

# 4. API anahtarlarını gir (.env)
# Gerekli:   EVREN_LLM_API_KEY (birincil sağlayıcı)
# Jev için:  OPENROUTER_API_KEY
# Opsiyonel: NVIDIA_API_KEY, GROQ_API_KEY, GEMINI_API_KEY

# 5. OpenCode config (lokal)
Copy-Item opencode.example.json opencode.json
# Kendi ortamına göre düzenle (genelde varsayılan uygundur)
```

> Not: `opencode.json` kişisel çalışma dizinine göre değişebileceğinden repoda **takip edilmez**.

---

## 11. Kullanım

```bash
# CLI (manuel onay modu)
python main.py --workspace .

# CLI otomatik (riskli)
python main.py --workspace . --auto

# CLI + Git (dal aç + commit)
python main.py --workspace . --git

# Model karşılaştırma
python compare.py general "Bu paragrafı daha sade Türkçe ile yeniden yaz."

# Ücretsiz model keşfi / sağlık kontrolü
python discover.py
python discover.py --ping     # tüm aday modeller + Jev smoke test

# Testler
.venv\Scripts\python.exe -m pytest tests/ -v
```

---

## 12. Gizlilik, Güvenlik & Temizlik

- **`.env` asla repoya eklenmez** (`.gitignore`). Yalnızca `.env.example` örnek olarak verilir.
- **API anahtarları kesinlikle gizli tutulur.** Hiçbir kaynak dosyada ham anahtar bulunmaz; yalnızca env referansı (`{env:...}`) kullanılır.
- **Kişisel yollar temizlendi.** `opencode.json` → `opencode.example.json`; `CONTEXT.md` içindeki mutlak yollar normalleştirildi.
- **Gizli dosya erişimi engellenir** (`workspace.is_secret`).
- **Path traversal koruması** (`safe_path` + `is_relative_to`).
- **Yedek + Geri Alma:** Her yazım öncesi yedek alınır (`backups/`); test başarısızsa otomatik rollback.
- **Fail-closed:** Jev/Hakem geçerli yanıt veremezse görev onaylanmaz/durdurulur.
- **Kod güvenliği:** Güvensiz kod yürütme (`eval`, `new Function`) üretimi sistem prompt'u ile yasaklanmıştır.

---

## 13. Loglar & İzlenebilirlik

- **Çalışma logları:** `runs/YYYYMMDD-HHMMSS.jsonl` (event bazlı JSONL). Paralel kurulda thread-safe yazım (`threading.Lock`).
- **Token istatistiği:** Oturum raporunda LLM (provider:model bazlı çağrı/prompt/completion) ve Jev (çağrı + tahmini token) gösterilir.
- **Karar şeffaflığı:** Kurulun **tam öneri dökümü** (`comparison`), kazanan, karar veren (Jev/decider), gerekçe, Jev meta (confidence/probabilities) ve gerekirse deep protokol sonuçları saklanır.

---

## 14. Lisans

MIT — bkz. [`LICENSE`](LICENSE).

---

## 15. Ölçüm (Eval Harness)

Boru hattındaki değişiklikleri veriye dayandırmak için 20 küçük görev
(`evals/tasks/`) ve tek komutla ölçüm altyapısı vardır:

```bash
python evals/make_tasks.py     # görev depolarını üret
python evals/run.py --stub     # LLM'siz (CI için, referans çözümlerle harness doğrulaması)
python evals/run.py            # gerçek LLM ile ölçüm
```

Metrikler: başarı oranı, deneme sayısı, süre, token/çağrı, düşülen aşama.
Sonuçlar `evals/RESULTS.md` tablosuna ve `evals/results/*.json` dosyalarına yazılır.

---

## 16. Değişiklik Günlüğü

Bkz. [`CHANGELOG.md`](CHANGELOG.md).


