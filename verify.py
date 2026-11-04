"""Deterministik doğrulama: statik kontroller ve testler (LLM hakemden ÖNCE çalışır)."""
import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


def static_check(staged: dict) -> list:
    problems = []
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
            r = subprocess.run(
                ["ruff", "check", "--select", "E9,F63,F7,F82", "--stdin-filename", rel, "-"],
                input=content, text=True, capture_output=True, timeout=30, encoding="utf-8",
            )
            if r.returncode != 0 and r.stdout.strip():
                file_problems.append(r.stdout.strip()[:1500])
        problems += file_problems
    return problems


def run_tests(root: Path, command=None, timeout: int = 180):
    """(başarılı_mı, çıktı) döner. Test yoksa/pytest kurulu değilse atlar."""
    if command is None:
        if not ((root / "tests").is_dir() or list(root.glob("test_*.py"))):
            return True, "test bulunamadı, atlandı"
        command = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
    try:
        # .pyc önbelleği: aynı saniyede, aynı boyutta yeniden yazılan dosyada eski bytecode'un
        # çalışmasına yol açar (geri alma + yeniden deneme senaryosu). Bu yüzden kapatıyoruz.
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        r = subprocess.run(
            command, cwd=root, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace"
        )
    except subprocess.TimeoutExpired:
        return False, f"Testler {timeout}s içinde bitmedi"
    except FileNotFoundError as e:
        return False, f"Komut bulunamadı: {e}"
    out = (r.stdout + r.stderr)[-4000:]
    if "No module named pytest" in out:
        return True, "pytest kurulu değil, atlandı"
    if r.returncode == 5:  # pytest: hiç test toplanmadı
        return True, "test toplanmadı, atlandı"
    return r.returncode == 0, out
