"""Çalışma alanı: güvenli yol, bağlam, diff, yedek/geri alma, opsiyonel git."""
import difflib
import fnmatch
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

BACKUP_DIR = Path(__file__).parent / "backups"  # çalışma alanını kirletmesin
MANIFEST_NAME = ".orchestra-manifest.json"

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.pfx", "*.sqlite", "*.db"]
TEXT_EXT = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".c", ".cpp", ".h", ".java", ".go", ".rs",
    ".log", ".xml", ".sql", ".ps1", ".bat", ".env.example",
}


class WorkspaceError(Exception):
    pass


def is_secret(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def _secret_component(parts) -> str:
    """Yolun HERHANGİ bir bileşeni gizli desene uyuyor mu? (dizin adları dahil)"""
    for part in parts:
        if part in ("", ".", ".."):
            continue
        if is_secret(part):
            return part
    return ""


def safe_path(root: Path, rel: str) -> Path:
    """Güvenli yol çözümü.

    Korumalar:
      1) `..` / mutlak yol ile çalışma alanı dışına çıkma (resolve sonrası).
      2) Sembolik link/junction ile kök dışına çıkma (resolve hedefi kontrol edilir).
      3) Gizli dosya erişimi — yalnızca son bileşen değil, TÜM bileşenler
         (.env/ içindeki bir dosya da engellenir).
    """
    if not rel:
        raise WorkspaceError("Boş yol")
    root = root.resolve()
    raw_parts = Path(rel).parts

    # Sembolik link takip edilmeden önce ham bileşenleri kontrol et
    hit = _secret_component(raw_parts)
    if hit:
        raise WorkspaceError(f"Gizli dosyaya erişim engellendi: {rel}")

    # resolve() linkleri çözer → hedefin hâlâ kök içinde olduğunu doğrula
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise WorkspaceError(f"Çalışma alanı dışına çıkılamaz: {rel}")

    # Link hedefi gizli bir dosya/dizin olabilir
    hit = _secret_component(p.relative_to(root).parts)
    if hit:
        raise WorkspaceError(f"Gizli dosyaya erişim engellendi: {rel}")
    return p


def safe_read_path(root: Path, rel: str, extra_roots=None) -> Path:
    """Okuma yolu çözümü.

    `safe_path`'ten farkı: **mutlak** yolları da kabul eder. Güvenlik için:
      * gizli dosya bileşenleri her zaman reddedilir,
      * mutlak yol çalışma alanı içinde değilse yalnızca `extra_roots` altındaysa
        kabul edilir (örn. ORCHESTRA_MEDIA_ROOTS ile Downloads'a izin vermek).
    """
    if rel is None or not str(rel).strip():
        raise WorkspaceError("Boş yol")
    raw = Path(str(rel)).expanduser()
    if raw.is_absolute():
        p = raw.resolve()
        hit = _secret_component(p.parts)
        if hit:
            raise WorkspaceError(f"Gizli dosyaya erişim engellendi: {rel}")
        root = root.resolve()
        if p.is_relative_to(root):
            return p
        for extra in (extra_roots or []):
            try:
                if p.is_relative_to(Path(extra).expanduser().resolve()):
                    return p
            except (OSError, ValueError):
                continue
        raise WorkspaceError(
            f"Çalışma alanı dışındaki yola erişim engellendi: {rel}\n"
            f"İzin vermek için ORCHESTRA_MEDIA_ROOTS ortam değişkenine kökü ekleyin "
            f"(örn. 'C:\\Users\\<kullanıcı>\\Downloads')."
        )
    return safe_path(root, rel)


# -----------------------------------------------------------------------------
# .gitignore desteği
# -----------------------------------------------------------------------------
def load_gitignore(root: Path) -> list:
    """Kök .gitignore'u (ve varsa .git/info/exclude) basit kurallara çevirir.

    Desteklenen sözdizimi: `#` yorumları, `!` olumsuzlama, `/` ile köke sabitleme,
    `*`, `?`, `**`, sonda `/` (yalnızca dizin).
    """
    patterns = []
    for path in (root / ".gitignore", root / ".git" / "info" / "exclude"):
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                negated = s.startswith("!")
                if negated:
                    s = s[1:].strip()
                if not s:
                    continue
                dir_only = s.endswith("/")
                s = s.rstrip("/")
                anchored = s.startswith("/")
                s = s.lstrip("/")
                patterns.append({
                    "pattern": s,
                    "negated": negated,
                    "dir_only": dir_only,
                    "anchored": anchored,
                })
        except Exception:
            continue
    return patterns


def _match_one(rel: str, pat: dict) -> bool:
    pattern = pat["pattern"]
    if not pattern:
        return False
    # ** desteği: fnmatch zaten * ile / geçişine izin verir, ** için ek kontrol
    candidates = [rel]
    if not pat["anchored"]:
        # herhangi bir üst dizinle eşleşme: "build/" → "a/build/x"
        parts = rel.split("/")
        candidates += ["/".join(parts[i:]) for i in range(1, len(parts))]
    for cand in candidates:
        if fnmatch.fnmatch(cand, pattern):
            return True
        if pat["dir_only"] or "/" not in pattern:
            # dizin adı eşleşmesi: "node_modules" → "a/node_modules/b.py"
            if fnmatch.fnmatch(Path(cand).name, pattern):
                return True
            segs = cand.split("/")
            if any(fnmatch.fnmatch(s, pattern) for s in segs):
                return True
    return False


def is_ignored(rel: str, patterns: list) -> bool:
    """Sırayla uygula; son eşleşen kural kazanır (git davranışı)."""
    ignored = False
    for pat in patterns:
        if _match_one(rel, pat):
            ignored = not pat["negated"]
    return ignored


def iter_files(root: Path, use_gitignore: bool = True, apply_excludes: bool = True):
    """Çalışma alanındaki dosyaları (göreli posix yol) üretir.

    apply_excludes=False → yalnızca .git ve __pycache__ atlanır; `build/`,
    `dist/`, `node_modules/` gibi genel klasörler de taranır (glob ile
    "her şeyi göster" isteyen çağrılar için).
    """
    root = root.resolve()
    patterns = load_gitignore(root) if use_gitignore else []
    never = {".git", "__pycache__"}
    skip = EXCLUDE_DIRS if apply_excludes else never
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for d in sorted(dirnames):
            if d in skip:
                continue
            rel_d = (Path(dirpath) / d).relative_to(root).as_posix()
            if patterns and is_ignored(rel_d, patterns):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if is_secret(fn):
                continue
            if patterns and is_ignored(rel, patterns):
                continue
            yield p, rel


def repo_map(root: Path, max_files: int = 300, use_gitignore: bool = True) -> list:
    out = []
    try:
        for p, rel in iter_files(root, use_gitignore=use_gitignore):
            if p.suffix.lower() not in TEXT_EXT:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append(f"{rel} ({size} B)")
            if len(out) >= max_files:
                out.append("... (liste kısaltıldı)")
                break
    except OSError:
        pass
    return out


def glob_files(root: Path, pattern: str, max_results: int = 500,
               use_gitignore: bool = True, apply_excludes: bool = True) -> list:
    """Glob deseniyle dosya arar (ör: "**/*.py", "src/*.ts").

    `**` tüm alt dizinleri, `*` tek seviyeyi temsil eder. Gizli dosyalar ve
    .gitignore ile hariç tutulanlar atlanır.
    """
    import re

    def to_regex(pat: str):
        i, n, out = 0, len(pat), ["^"]
        while i < n:
            c = pat[i]
            if c == "*":
                if pat[i:i + 2] == "**":
                    # **/ → sıfır veya daha çok dizin
                    if pat[i:i + 3] == "**/":
                        out.append("(?:.*/)?")
                        i += 3
                        continue
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
                i += 1
                continue
            if c == "?":
                out.append("[^/]")
                i += 1
                continue
            if c == "[":
                j = pat.find("]", i)
                if j == -1:
                    out.append(re.escape(c))
                    i += 1
                    continue
                out.append(pat[i:j + 1])
                i = j + 1
                continue
            out.append(re.escape(c))
            i += 1
        out.append("$")
        return re.compile("".join(out))

    rx = to_regex(pattern.replace("\\", "/").lstrip("./"))
    hits = []
    for p, rel in iter_files(root, use_gitignore=use_gitignore, apply_excludes=apply_excludes):
        if rx.match(rel):
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            hits.append(f"{rel} ({size} B)")
            if len(hits) >= max_results:
                hits.append("... (sonuç kısaltıldı)")
                break
    return hits


def read_files(root: Path, rels: list, max_chars: int = 60_000) -> str:
    parts, used = [], 0
    for rel in rels:
        try:
            p = safe_path(root, rel)
        except WorkspaceError as e:
            parts.append(f"### {rel}\n[atlandı: {e}]")
            continue
        if not p.is_file():
            parts.append(f"### {rel}\n[dosya yok — yeni dosya olabilir]")
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if used + len(text) > max_chars:
            text = text[: max(0, max_chars - used)] + "\n[... kısaltıldı]"
        used += len(text)
        parts.append(f"### {rel}\n```\n{text}\n```")
    return "\n\n".join(parts)


def make_diff(root: Path, staged: dict) -> str:
    chunks = []
    for rel, new in staged.items():
        p = safe_path(root, rel)
        old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        d = difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}")
        chunks.append("".join(d))
    return "\n".join(c for c in chunks if c)


def write_with_backup(root: Path, staged: dict) -> dict:
    root = root.resolve()
    # Aynı saniye içinde iki yazma → klasör çakışmasın
    base = BACKUP_DIR / root.name / time.strftime("%Y%m%d-%H%M%S")
    bdir = base
    n = 1
    while bdir.exists():
        bdir = base.with_name(f"{base.name}-{n}")
        n += 1
    bdir.mkdir(parents=True, exist_ok=True)

    snapshot = {}
    manifest = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"), "workspace": str(root), "files": {}}
    for rel, content in staged.items():
        p = safe_path(root, rel)
        existed = p.exists()
        backup = bdir / rel
        if existed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, backup)
        snapshot[rel] = (existed, backup)
        manifest["files"][rel] = {"existed": existed}
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    # Manifest: orchestra_undo'nun hangi dosyaların sonradan oluşturulduğunu
    # bilmesini sağlar (var olmayan dosya geri alınırken silinir).
    try:
        (bdir / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return snapshot


def list_backups(root: Path, limit: int = 10) -> list:
    """Bu çalışma alanı için en yeni yedeklerden başlayarak döner."""
    root = root.resolve()
    base = BACKUP_DIR / root.name
    if not base.is_dir():
        return []
    dirs = [d for d in base.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: (d.stat().st_mtime, d.name), reverse=True)
    return dirs[:limit]


def undo_last(root: Path) -> dict:
    """En son yedek anını geri yükler.

    Döndürür: {"ok": bool, "dir": str, "restored": [...], "deleted": [...], "error": str}
    Manifest varsa yeni oluşturulmuş dosyalar silinir; yoksa (eski yedekler)
    yalnızca yedeklenmiş dosyalar geri yazılır.
    """
    root = root.resolve()
    backups = list_backups(root, limit=1)
    if not backups:
        return {"ok": False, "error": f"Bu çalışma alanı için yedek bulunamadı: {root.name}"}
    bdir = backups[0]
    restored, deleted = [], []

    manifest = {}
    mp = bdir / MANIFEST_NAME
    if mp.is_file():
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    entries = manifest.get("files") or {}
    if entries:
        for rel, info in entries.items():
            p = safe_path(root, rel)
            if info.get("existed"):
                src = bdir / rel
                if not src.is_file():
                    continue
                p.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, p)
                restored.append(rel)
            else:
                if p.exists():
                    p.unlink()
                    deleted.append(rel)
    else:
        # Eski yedek: klasördeki tüm dosyaları geri yaz
        for f in bdir.rglob("*"):
            if not f.is_file() or f.name == MANIFEST_NAME:
                continue
            rel = f.relative_to(bdir).as_posix()
            try:
                p = safe_path(root, rel)
            except WorkspaceError:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, p)
            restored.append(rel)

    return {"ok": True, "dir": str(bdir), "restored": restored, "deleted": deleted}


def rollback(root: Path, snapshot: dict) -> None:
    for rel, (existed, backup) in snapshot.items():
        p = safe_path(root, rel)
        if existed:
            shutil.copy2(backup, p)
        elif p.exists():
            p.unlink()


# -----------------------------------------------------------------------------
# Geçici kopyada uygula + test et (gerçek çalışma alanına dokunmadan)
# -----------------------------------------------------------------------------
STAGE_EXCLUDE_DIRS = EXCLUDE_DIRS | {
    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".nox", ".next",
    "target", "vendor", "coverage", "htmlcov", ".gradle", ".cache",
    "backups", "runs", ".orchestra", "__snapshots__",
}
STAGE_CONFIG_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "noxfile.py",
    "requirements.txt", "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "tsconfig.json", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Makefile",
    "ruff.toml", ".ruff.toml", "mypy.ini", ".flake8",
}
_STAGE_KEEP_HIDDEN = {".env.example", ".gitignore", ".editorconfig"}


def _stage_ignore(names):
    """copytree için ignore: hariç dizinler, gizli dosyalar, sırlar, bytecode."""
    ignored = []
    for n in names:
        if n in STAGE_EXCLUDE_DIRS:
            ignored.append(n)
        elif n.startswith(".") and n not in _STAGE_KEEP_HIDDEN:
            ignored.append(n)
        elif n in ("__pycache__",) or n.endswith((".pyc", ".pyo")):
            ignored.append(n)
        elif is_secret(n):
            ignored.append(n)
    return ignored


def _count_files(root: Path, limit: int) -> int:
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in STAGE_EXCLUDE_DIRS and not d.startswith(".")]
        n += len(filenames)
        if n > limit:
            return n
    return n


def _copy_minimal(root: Path, dst: Path, staged: dict) -> None:
    """Büyük projelerde yalnızca gerekli dosyaları kopyalar (config + tests + değişenler)."""
    rels = set(staged)
    # 1) Yapılandırma dosyaları (kök ve bir seviye alt)
    for p in root.iterdir():
        if p.is_file() and p.name in STAGE_CONFIG_NAMES:
            rels.add(p.name)
    # 2) tests/ dizini
    tests_dir = root / "tests"
    if tests_dir.is_dir():
        for p in tests_dir.rglob("*"):
            if p.is_file() and p.suffix == ".py":
                rels.add(p.relative_to(root).as_posix())
    # 3) Var olan test dosyaları (kök)
    for p in root.glob("test_*.py"):
        rels.add(p.name)
    # 4) Değişen dosyaların kopyalanması için üst dizin yapısını oluştur
    for rel in rels:
        src = root / rel
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, out)


def stage_to_temp(root: Path, staged: dict, max_files: int = 4000) -> tuple:
    """Projenin geçici bir kopyasını oluşturur ve `staged` değişikliklerini uygular.

    Döndürür: (geçici_dizin, mod) — mod: "full" | "minimal"
    Çağıran `try/finally` ile dizini silmelidir (`cleanup_stage`).
    """
    import tempfile

    root = root.resolve()
    tmp = Path(tempfile.mkdtemp(prefix="orchestra-stage-"))
    try:
        n = _count_files(root, max_files)
        mode = "full" if n <= max_files else "minimal"
        if mode == "full":
            shutil.copytree(root, tmp, dirs_exist_ok=True,
                            ignore=lambda _src, names: _stage_ignore(names), symlinks=False)
        else:
            _copy_minimal(root, tmp, staged)
        for rel, content in staged.items():
            p = safe_path(tmp, rel)  # geçici kök içinde kalma garantisi
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        return tmp, mode
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def cleanup_stage(tmp: Path) -> None:
    """Geçici kopyayı siler (hata olsa bile)."""
    try:
        if not tmp:
            return
        # Güvenlik: yalnızca orchestra-stage- önekli dizinleri sil
        if Path(tmp).name.startswith("orchestra-stage-"):
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception:
        pass



def _git(root: Path, *args):
    try:
        # stdin=DEVNULL: MCP stdio pipe'ı alt sürece miras kalırsa git kilitlenir.
        return subprocess.run(
            ["git", "-C", str(root), "--no-pager", *args],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def git_checkout_branch(root: Path, name: str) -> bool:
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    if r is None or r.returncode != 0:
        return False
    r = _git(root, "checkout", "-b", name)
    return r is not None and r.returncode == 0


def git_commit(root: Path, rels: list, msg: str) -> bool:
    if _git(root, "add", "--", *rels) is None:
        return False
    r = _git(root, "commit", "-m", msg)
    return r is not None and r.returncode == 0
