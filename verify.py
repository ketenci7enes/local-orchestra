"""Deterministik doğrulama: statik kontroller ve testler (LLM hakemden ÖNCE çalışır).

Tasarım
-------
* `static_check` sözdizimi + ruff + (opsiyonel, uyarı düzeyinde) tip kontrolü yapar.
* `detect_test_command` projeye bakarak doğru test komutunu bulur:
  Python / Node / Rust / Go / Makefile.
* `run_tests` alt süreci **kısıtlı ortamla** çalıştırır: API anahtarları testlere
  sızmaz, `stdin=DEVNULL` (MCP stdio pipe'ı kilitlenmesin).
* `summarize_failure` başarısız test çıktısını bir sonraki coder denemesine
  verilecek kısa, yapılandırılmış geri bildirime çevirir.
"""
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

# Test alt süreçlerine geçmemesi gereken ortam değişkenleri (sır sızıntısı önlemi)
SECRET_ENV_KEYS = {
    "EVREN_LLM_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_API_KEY", "NVIDIA_API_KEY_2",
    "NVIDIA_API_KEY_3", "GEMINI_API_KEY", "GROQ_API_KEY", "JEV_AI_API_KEY",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY", "HF_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "GH_TOKEN",
}

# ruff: proje kendi yapılandırmasına sahipse ona bırak, yoksa bu seçim uygulanır.
DEFAULT_RUFF_SELECT = "E9,F,B"          # F=tüm pyflakes (tanımsız isim, kullanılmayan import…)
RUFF_MAX_ISSUES = 40


def find_project_config(root: Path) -> dict:
    """Projede ruff/pytest/... yapılandırması var mı? (özet bayraklar)"""
    root = Path(root)
    return {
        "ruff": any((root / n).is_file() for n in
                    ("ruff.toml", ".ruff.toml", "pyproject.toml", "setup.cfg", "tox.ini")),
        "pytest": any((root / n).is_file() for n in
                      ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")),
    }


def clean_env(extra: dict = None) -> dict:
    """Alt süreçler için sırlardan arındırılmış ortam."""
    env = {k: v for k, v in os.environ.items()
           if k.upper() not in SECRET_ENV_KEYS and not k.upper().endswith("_API_KEY")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # .pyc önbelleği: aynı saniyede aynı boyutta yeniden yazılan dosyada eski
    # bytecode'un çalışmasına yol açar (geri alma + yeniden deneme senaryosu).
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _run_cmd(cmd, cwd, timeout=60, input_text=None, env=None):
    return subprocess.run(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=timeout, env=env or clean_env(), encoding="utf-8", errors="replace",
        input=input_text,
    )


def _ruff_cmd(root: Path) -> list:
    """Proje kendi ruff ayarını kullanıyorsa --select verme."""
    cfg = find_project_config(root)["ruff"]
    issues = []
    if not cfg:
        issues = ["--select", os.getenv("ORCHESTRA_RUFF_SELECT", DEFAULT_RUFF_SELECT)]
    return ["ruff", "check", *issues]


def static_check(staged: dict, root: Path = None, typecheck: bool = False) -> list:
    """Bellekteki içerikleri statik olarak denetler; sorun listesi döndürür."""
    problems = []
    root = Path(root) if root else None
    for rel, content in staged.items():
        suffix = Path(rel).suffix.lower()
        file_problems = []
        try:
            if suffix == ".py":
                ast.parse(content, filename=rel)
            elif suffix == ".json":
                json.loads(content)
            elif suffix in (".yaml", ".yml"):
                yaml.safe_load(content)
        except SyntaxError as e:
            file_problems.append(f"{rel}: SyntaxError (satır {e.lineno}): {e.msg}")
        except Exception as e:  # json/yaml hataları
            file_problems.append(f"{rel}: geçersiz içerik: {e}")

        if suffix == ".py" and not file_problems and shutil.which("ruff"):
            try:
                r = _run_cmd(
                    [*_ruff_cmd(root or Path.cwd()), "--stdin-filename", rel, "--output-format", "concise", "-"],
                    cwd=root or Path.cwd(), timeout=45, input_text=content,
                )
                if r.returncode != 0:
                    out = (r.stdout or "").strip()
                    if out:
                        lines = out.splitlines()
                        file_problems.append(
                            "ruff:\n" + "\n".join(lines[:RUFF_MAX_ISSUES])
                            + ("\n… (kısaltıldı)" if len(lines) > RUFF_MAX_ISSUES else "")
                        )
            except subprocess.TimeoutExpired:
                file_problems.append(f"{rel}: ruff zaman aşımı")
        problems += file_problems

    if typecheck:
        problems += _typecheck(staged, root)
    return problems


def _typecheck(staged: dict, root: Path) -> list:
    """Değişen .py dosyaları için mypy (varsa). Uyarı düzeyinde eklenir."""
    if not shutil.which("mypy") or not root:
        return []
    py_files = [rel for rel in staged if rel.endswith(".py")]
    if not py_files:
        return []
    out = []
    for rel in py_files[:10]:
        p = Path(root) / rel
        if not p.is_file():
            continue
        try:
            r = _run_cmd(["mypy", "--ignore-missing-imports", "--no-error-summary",
                          "--show-error-codes", str(p.name)],
                         cwd=p.parent, timeout=120)
            text = (r.stdout or "").strip()
            if r.returncode != 0 and text:
                out.append(f"tip (mypy):\n{text[:1200]}")
        except subprocess.TimeoutExpired:
            out.append(f"tip: mypy zaman aşımı ({rel})")
    return out


# -----------------------------------------------------------------------------
# Test komutu otomatik algılama
# -----------------------------------------------------------------------------
def detect_test_command(root: Path, prefer=None) -> tuple:
    """(komut|None, açıklama) döndürür."""
    root = Path(root)
    if prefer:
        return list(prefer) if not isinstance(prefer, str) else [prefer], "açıkça verildi"

    # Python
    if (root / "tests").is_dir() or list(root.glob("test_*.py")):
        return [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"], "pytest"
    if (root / "pyproject.toml").is_file() or (root / "pytest.ini").is_file():
        return [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"], "pytest (yapılandırma)"

    # Node
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            scripts = data.get("scripts") or {}
            if "test" in scripts:
                if (root / "pnpm-lock.yaml").exists():
                    return ["pnpm", "test", "--", "--run"], "package.json → pnpm test"
                if (root / "yarn.lock").exists():
                    return ["yarn", "test"], "package.json → yarn test"
                return ["npm", "test", "--silent"], "package.json → npm test"
        except Exception:
            pass

    # Rust
    if (root / "Cargo.toml").is_file():
        return ["cargo", "test", "--quiet"], "cargo test"

    # Go
    if (root / "go.mod").is_file():
        return ["go", "test", "./..."], "go test ./..."

    # Makefile
    mk = root / "Makefile"
    if mk.is_file():
        try:
            if re.search(r"^test:", mk.read_text(encoding="utf-8", errors="replace"), re.M):
                return ["make", "test"], "Makefile → make test"
        except Exception:
            pass

    return None, "tanınamadı"


def run_tests(root: Path, command=None, timeout: int = 180, env_extra: dict = None):
    """(başarılı_mı, çıktı) döner. Test komutu/araç yoksa atlar (True, 'atlandı')."""
    root = Path(root)
    if command is None:
        cmd, why = detect_test_command(root)
        if cmd is None:
            return True, "test komutu bulunamadı, atlandı"
    elif isinstance(command, str):
        import shlex
        cmd = shlex.split(command, posix=False)
        why = "özel komut"
    else:
        cmd = list(command)
        why = "özel komut"

    # Araç gerçekten var mı? (ör. npm yok)
    if shutil.which(cmd[0]) is None and cmd[0] != sys.executable:
        return True, f"araç bulunamadı ({cmd[0]}), atlandı"

    try:
        r = _run_cmd(cmd, cwd=root, timeout=timeout, env=clean_env(env_extra))
    except subprocess.TimeoutExpired:
        return False, f"Testler {timeout}s içinde bitmedi ({why})"
    except FileNotFoundError as e:
        return False, f"Komut bulunamadı: {e}"

    out = ((r.stdout or "") + (r.stderr or ""))[-6000:]
    low = out.lower()
    if "no module named pytest" in low:
        return True, "pytest kurulu değil, atlandı"
    if r.returncode == 5:  # pytest: hiç test toplanmadı
        return True, "test toplanmadı, atlandı"
    if r.returncode == 127:
        return True, f"komut yok ({cmd[0]}), atlandı"
    return r.returncode == 0, out


def summarize_failure(out: str, max_lines: int = 25) -> str:
    """Test çıktısından bir sonraki denemeye verilecek kısa geri bildirim üretir."""
    if not out:
        return "Testler başarısız (çıktı yok)"
    lines = [ln.rstrip() for ln in out.splitlines()]
    # Başarısız test adları / hata satırları öncelikli
    prio = [ln for ln in lines if re.search(
        r"(FAILED|ERROR|AssertionError|Traceback|E\s+assert|Error:|panicked at|not ok|\bFAIL\b)", ln)]
    tail = [ln for ln in lines[-max_lines:] if ln.strip()]
    chosen = []
    for ln in prio[-12:] + tail:
        if ln and ln not in chosen:
            chosen.append(ln)
    text = "\n".join(chosen[-max_lines:])
    return text[-4000:]
