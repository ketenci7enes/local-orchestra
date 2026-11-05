# Changelog

Tüm önemli değişiklikler bu dosyada tutulur. Biçim: [Keep a Changelog](https://keepachangelog.com/tr/1.1.0/),
sürümleme: [Semantic Versioning](https://semver.org/lang/tr/).

## [0.2.0] — 2026-11-05

### Eklendi
- **Medya okuma** (`media.py`, `orchestra_read_media`): PDF metin katmanı (pypdfium2) +
  taranmış PDF/görsel için **Windows.Media.Ocr** (gömülü, çevrimdışı, ücretsiz, TR+EN).
- `orchestra_run_task(attach=...)`: belge/görsel ekleyerek görev çalıştırma.
- `orchestra_read_file`: PDF ve görselleri otomatik metne çevirir.
- `orchestra_ocr_status`: OCR motoru ve yüklü dilleri raporlar.
- **Eval harness** (`evals/`): 20 görev, tek komutla ölçüm, `RESULTS.md` tablosu;
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
