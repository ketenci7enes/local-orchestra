# Changelog

Tüm önemli değişiklikler bu dosyada tutulur. Biçim: [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/),
sürümleme: [Semantic Versioning](https://semver.org/lang/tr/).

## [0.2.2] — 2026-11-06

### Düzeltildi
- **MCP açılış gecikmesi / "Request timed out":** `openai` SDK'si import anında ~3.5 s
  yükleniyordu ve stdio el sıkışması OpenCode startup timeout'una düşüyordu. `llm.py` artık
  SDK'yi tembel yükler (ilk gerçek model çağrısında). Açılış 3–8 s → **1.0–1.8 s**.
- `mcp.timeout.startup/catalog` 60 s → 20 s: takılan bir deneme saatlerce kuyrukta beklemez.
- `experimental.location_idle_timeout: false` (global config): 60 dakikalık unload/reconnect
  döngüsü kapatıldı (loglarda saatlik `mcp connected` + `Request timed out` bunun belirtisiydi).

### Eklendi
- **Sunucu talimatı** (`FastMCP(instructions=...)`): OpenCode bunu system prompt'a ekler;
  hangi iş için hangi aracın kullanılacağını ve hata durumunda built-in'e sessizce
  düşülmemesini söyler.
- `scripts/mcp_smoke.py`: el sıkışma + katalog + 2 gerçek çağrıyı 10 s bütçesiyle doğrular.
  CI'da da çalışır; "MCP tetiklenmiyor" dendiğinde ilk teşhis komutu.
- `tests/test_mcp_startup.py`: açılış süresi bütçesi, talimat varlığı ve `openai` import'unun
  tembel olduğunu doğrulayan regresyon testleri.

## [0.2.1] — 2026-11-06

### Eklendi
- **Araç yüzeyi testleri** (`tests/test_all_tools.py`): 17 MCP aracının tamamı için
  (yüzey + gerçek çağrı) 35 test; LLM gereken 3 araçta yalnızca LLM/Jev katmanı taklit edilir.
- **Karar kalitesi testleri** (`tests/test_decision_quality.py`): risk-farkında Jev
  kriterleri, düşük güven uyarısı ve Türkçe `İ` regresyonu.
- Eval görevi `edge-turkish-i` (21. görev): `snake_case` ve birleşik nokta (U+0307) tuzağı.

### Değişti
- **Jev kriterleri artık riskleri de içerir:** her öneri için `risks` alanı kritere eklenir
  ve soru, diğer üyelerin işaret ettiği risklerin giderilmiş olmasını şart koşar.
- **Düşük güven uyarısı:** karar güveni `ORCHESTRA_LOW_CONFIDENCE` (varsayılan 0.60)
  altındaysa `run_task` çıktısında ve `_decision_block` içinde `⚠️` uyarısı gösterilir;
  olasılık dağılımı da yazılır.
- **Türkçe harf tuzağı prompt'lara işlendi:** coder ve kurul prompt'larında ASCII'ye
  indirmede "önce `str.translate`, sonra `.lower()`" kuralı (aksi halde `İ` → `i` + U+0307).

### Düzeltildi
- Canlı orkestra koşusunda yakalanan `snake_case("İstanbul")` → `"i̇stanbul"` hatası,
  referans çözüm ve regresyon testiyle kapatıldı.

## [0.2.0] — 2026-11-05

### Eklendi
- **Medya okuma** (`media.py`, `orchestra_read_media`): PDF metin katmanı (pypdfium2) +
  taranmış PDF/görsel için **Windows.Media.Ocr** (gömülü, çevrimdışı, ücretsiz, TR+EN).
- `orchestra_run_task(attach=...)`: belge/görsel ekleyerek görev çalıştırma.
- `orchestra_read_file`: PDF ve görselleri otomatik metne çevirir.
- `orchestra_ocr_status`: OCR motoru ve yüklü dilleri raporlar.
- **Eval harness** (`evals/`): 21 görev, tek komutla ölçüm, `RESULTS.md` tablosu;
  LLM'siz `--stub` modu CI'da çalışır.
- **Geçici kopyada doğrulama** (`workspace.stage_to_temp`): testler gerçek çalışma
  alanına yazmadan önce izole kopyada koşar; `dry_run` artık gerçek test sonucu döner.
- **Sembol haritası** (`symbols.py`): AST tabanlı Python + regex tabanlı çok dilli
  sembol özeti, ilgili gövde seçimi, router dosya seçimi doğrulaması.
- **EDIT sağlamlığı** (`coder.py`): exact → normalized → fuzzy eşleşme, çoklu eşleşme
  için satır bilgisi, kesilmiş blok kurtarma, hedefli yeniden deneme geri bildirimi.
- `verify.py`: çok dilli test komutu algılama (pytest/npm/cargo/go/make), ruff `F+B`,
  opsiyonel mypy, yapılandırılmış hata özeti, sırlardan arındırılmış ortam.
- Güvenlik: `run_tests` komut allowlist'i, onay kapısı (yıkıcı/hassas değişiklikler),
  prompt injection ayırıcıları, log maskeleme, `safe_read_path`.
- `orchestra_write_file` / `orchestra_apply_edit`: `verify=true` ile statik kontrol + test.
- Paketleme: `pyproject.toml`, `LICENSE` (MIT), `scripts/` klasörü, GitHub Actions CI.

### Düzeltildi
- MCP stdio kilitlenmesi (`git`, `pytest` alt süreçleri stdin miras alıyordu).
- `AssertionError: Request already responded to` çökmesi (iptal/yanıt yarışı).
- `dry_run` diske yazma hatası (A6).

## [0.1.0] — 2026-10-20

- İlk sürüm: kurul (3 üye) + Jev kararı + coder/verify/review boru hattı ve MCP sunucusu.
