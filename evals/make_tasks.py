"""Eval görevlerini diske yazar: evals/tasks/<id>/{task.md, repo/..., tests/test_*.py}"""
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from tasks_data import TASKS  # noqa: E402


def materialize(force: bool = False) -> int:
    root = HERE / "tasks"
    root.mkdir(exist_ok=True)
    count = 0
    for t in TASKS:
        d = root / t["id"]
        if d.exists() and not force:
            continue
        repo = d / "repo"
        for rel, content in t["files"].items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content if content.endswith("\n") else content + "\n",
                         encoding="utf-8", newline="\n")
        tests_dir = repo / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / f"test_{t['id'].replace('-', '_')}.py").write_text(
            t["tests"], encoding="utf-8", newline="\n")
        (d / "task.md").write_text(
            f"# {t['id']}\n\nKategori: `{t['category']}`\n\n## Hedef\n{t['goal']}\n",
            encoding="utf-8", newline="\n")
        (d / "solution.blocks").write_text(t["blocks"].rstrip() + "\n",
                                           encoding="utf-8", newline="\n")
        count += 1
    return count


if __name__ == "__main__":
    n = materialize(force="--force" in sys.argv)
    print(f"{n} görev yazıldı → {HERE / 'tasks'}")
