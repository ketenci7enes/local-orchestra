"""Çalışma alanı: güvenli yol, bağlam, diff, yedek/geri alma, opsiyonel git."""
import difflib
import fnmatch
import os
import shutil
import subprocess
import time
from pathlib import Path

BACKUP_DIR = Path(__file__).parent / "backups"  # çalışma alanını kirletmesin

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build"}
SECRET_PATTERNS = [".env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.pfx", "*.sqlite", "*.db"]
TEXT_EXT = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".csv",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh", ".c", ".cpp", ".h", ".java", ".go", ".rs",
}


class WorkspaceError(Exception):
    pass


def is_secret(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in SECRET_PATTERNS)


def safe_path(root: Path, rel: str) -> Path:
    root = root.resolve()
    p = (root / rel).resolve()
    if not p.is_relative_to(root):
        raise WorkspaceError(f"Çalışma alanı dışına çıkılamaz: {rel}")
    if is_secret(p.name):
        raise WorkspaceError(f"Gizli dosyaya erişim engellendi: {rel}")
    return p


def repo_map(root: Path, max_files: int = 300) -> list:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if is_secret(fn) or p.suffix.lower() not in TEXT_EXT:
                continue
            out.append(f"{p.relative_to(root).as_posix()} ({p.stat().st_size} B)")
            if len(out) >= max_files:
                out.append("... (liste kısaltıldı)")
                return out
    return out


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
    bdir = BACKUP_DIR / root.name / time.strftime("%Y%m%d-%H%M%S")
    snapshot = {}
    for rel, content in staged.items():
        p = safe_path(root, rel)
        existed = p.exists()
        backup = bdir / rel
        if existed:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, backup)
        snapshot[rel] = (existed, backup)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    return snapshot


def rollback(root: Path, snapshot: dict) -> None:
    for rel, (existed, backup) in snapshot.items():
        p = safe_path(root, rel)
        if existed:
            shutil.copy2(backup, p)
        elif p.exists():
            p.unlink()


def _git(root: Path, *args):
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30)
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
