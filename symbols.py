"""Sembol haritası ve bağlam seçimi (D).

Amaç: modele "neyin nerede tanımlı olduğunu" göstermek ve dev dosyalarda tüm
içeriği göndermek yerine yalnızca ilgili gövdeleri vermek.

* Python: `ast` (kesin) — sınıf/fonksiyon/metot imzaları + satır aralıkları.
* Diğer diller (JS/TS/Rust/Go/Java/C#): düzenli ifade tabanlı yaklaşık çıkarım.
* Önbellek: dosya mtime+size değişmedikçe yeniden çıkarılmaz.
"""
from __future__ import annotations

import ast
import re
import time
from pathlib import Path

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "target", "vendor", "backups", "runs",
}
CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".rs", ".go", ".java",
    ".cs", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".kt", ".swift",
}
MAX_FILE_BYTES = 512_000

# (regex, tür) — yaklaşık çıkarım için
GENERIC_PATTERNS = [
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\("), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+(\w+)"), "class"),
    (re.compile(r"^\s*(?:export\s+)?interface\s+(\w+)"), "interface"),
    (re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*="), "type"),
    (re.compile(r"^\s*def\s+(\w+)"), "function"),
    (re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)"), "function"),
    (re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)"), "struct"),
    (re.compile(r"^\s*(?:pub\s+)?enum\s+(\w+)"), "enum"),
    (re.compile(r"^\s*(?:func|type)\s+(?:\([^)]*\)\s*)?(\w+)"), "func"),
    (re.compile(r"^\s*(?:public|private|protected|internal)\s+[\w<>\[\],\s]+\s(\w+)\s*\("), "method"),
]

_CACHE: dict = {}
CACHE_TTL = 300  # saniye


def _py_symbols(text: str) -> list:
    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out

    def walk(node, cls=""):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in child.bases[:3] if hasattr(ast, "unparse"))
                out.append({
                    "name": child.name, "kind": "class", "line": child.lineno,
                    "end": getattr(child, "end_lineno", child.lineno),
                    "sig": f"class {child.name}" + (f"({bases})" if bases else ""),
                    "doc": (ast.get_docstring(child) or "").split("\n")[0][:100],
                })
                walk(child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in child.args.args][:6]
                prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                out.append({
                    "name": child.name, "kind": "method" if cls else "function",
                    "line": child.lineno, "end": getattr(child, "end_lineno", child.lineno),
                    "sig": f"{prefix} {child.name}({', '.join(args)})",
                    "doc": (ast.get_docstring(child) or "").split("\n")[0][:100],
                })
                walk(child, cls)

    walk(tree)
    return out


def _generic_symbols(text: str) -> list:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 300:
            continue
        for rx, kind in GENERIC_PATTERNS:
            m = rx.match(line)
            if m:
                out.append({"name": m.group(1), "kind": kind, "line": i, "end": i,
                            "sig": line.strip()[:120], "doc": ""})
                break
    return out


def file_symbols(path: Path) -> list:
    """Bir dosyanın sembolleri (önbellekli)."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = str(path.resolve())
    ent = _CACHE.get(key)
    if ent and ent["mtime"] == st.st_mtime_ns and ent["size"] == st.st_size:
        return ent["symbols"]
    if st.st_size > MAX_FILE_BYTES:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    syms = _py_symbols(text) if path.suffix.lower() == ".py" else _generic_symbols(text)
    _CACHE[key] = {"mtime": st.st_mtime_ns, "size": st.st_size, "symbols": syms, "ts": time.time()}
    return syms


def iter_code_files(root: Path, max_files: int = 500):
    root = Path(root)
    n = 0
    for p in root.rglob("*"):
        if n >= max_files:
            break
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if p.suffix.lower() in CODE_EXT and not p.name.startswith("."):
            n += 1
            yield p


def symbol_map(root: Path, max_files: int = 200, max_chars: int = 18_000) -> str:
    """Tüm proje için kompakt sembol haritası metni."""
    root = Path(root)
    lines = []
    used = 0
    for p in sorted(iter_code_files(root, max_files)):
        rel = p.relative_to(root).as_posix()
        syms = file_symbols(p)
        if not syms:
            continue
        header = f"{rel}:"
        body = [f"  {s['kind'][:4]}:{s['name']} @{s['line']}" + (f"  # {s['doc']}" if s.get("doc") else "")
                for s in syms[:40]]
        block = header + "\n" + "\n".join(body)
        if used + len(block) > max_chars:
            lines.append(f"... (sembol haritası {max_chars} karakterde kesildi)")
            break
        used += len(block)
        lines.append(block)
    return "\n".join(lines)


def _keywords(goal: str) -> list:
    stop = {"the", "and", "for", "with", "bir", "ve", "ile", "için", "olan", "olarak",
            "ekle", "düzelt", "güncelle", "yap", "bir", "bu", "şu", "daha", "kod", "dosya"}
    words = re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü_][A-Za-z0-9_ÇĞİÖŞÜçğıöşü]{2,}", goal or "")
    out = []
    for w in words:
        lw = w.lower()
        if lw in stop or lw in out:
            continue
        out.append(lw)
    return out[:12]


def score_file(path: Path, keywords: list) -> int:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except Exception:
        return 0
    return sum(text.count(k) for k in keywords)


def validate_selection(root: Path, files: list, goal: str) -> tuple:
    """Router'ın seçtiği dosyalar hedefle gerçekten ilgili mi?

    Döndürür: (doğrulanmış_dosyalar, uyarı)
    """
    root = Path(root)
    if not files:
        return files, ""
    kws = _keywords(goal)
    if not kws:
        return files, ""
    keep, weak = [], []
    for rel in files:
        p = root / rel
        if not p.is_file():
            keep.append(rel)
            continue
        (keep if score_file(p, kws) > 0 else weak).append(rel)
    if not weak:
        return keep, ""
    return (keep or files), (f"Router'ın seçtiği {len(weak)} dosyada hedef anahtar kelimeleri "
                             f"geçmiyor: {', '.join(weak[:5])}")


def suggest_files(root: Path, goal: str, limit: int = 8) -> list:
    """Anahtar kelimelere göre en olası dosyaları önerir (router doğrulaması için)."""
    root = Path(root)
    kws = _keywords(goal)
    if not kws:
        return []
    scored = []
    for p in iter_code_files(root, 400):
        s = score_file(p, kws)
        if s:
            scored.append((s, p.relative_to(root).as_posix()))
    scored.sort(reverse=True)
    return [rel for _, rel in scored[:limit]]


def relevant_bodies(root: Path, rel: str, goal: str, max_lines: int = 120) -> str:
    """Bir dosyadan hedefle en ilgili sembol gövdelerini çıkarır (tam dosya yerine)."""
    root = Path(root)
    p = root / rel
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    lines = text.splitlines()
    syms = file_symbols(p)
    if not syms:
        return ""
    kws = _keywords(goal)
    def score(s):
        blob = (s["name"] + " " + s.get("doc", "") + " " + s.get("sig", "")).lower()
        return sum(1 for k in kws if k in blob)
    ranked = sorted(syms, key=score, reverse=True)
    chosen = [s for s in ranked if score(s) > 0][:3] or ranked[:2]
    out = []
    for s in chosen:
        a = max(1, s["line"] - 2)
        b = min(len(lines), (s["end"] or s["line"]) + 2)
        if b - a > max_lines:
            b = a + max_lines
        out.append(f"# {rel}:{a}-{b} ({s['kind']} {s['name']})\n" + "\n".join(lines[a - 1:b]))
    return "\n\n".join(out)


def clear_cache() -> None:
    _CACHE.clear()
