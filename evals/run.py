"""Eval harness (A): görevleri boru hattıyla çalıştırır ve ölçüm üretir.

Kullanım
--------
    python evals/run.py                 # gerçek LLM ile
    python evals/run.py --stub          # LLM'siz (referans çözümle harness doğrulaması)
    python evals/run.py --only slugify  # tek görev
    python evals/run.py --list          # görevleri listele

Ölçülenler: başarı oranı, deneme sayısı, süre, token/çağrı, rollback (diske
yazılmayan) sayısı ve hangi aşamada düşüldüğü.
"""
import argparse
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import coder            # noqa: E402
import orchestrator     # noqa: E402
import router           # noqa: E402
import verify           # noqa: E402
from tracker import tracker  # noqa: E402


class StubJudge:
    def review(self, goal, diff):
        return "APPROVED", "stub"


class StubLLM:
    def chat(self, role, msgs, **kw):
        return ""


def discover(only=None):
    tasks_dir = HERE / "tasks"
    out = []
    if not tasks_dir.is_dir():
        return out
    for d in sorted(tasks_dir.iterdir()):
        if not d.is_dir() or not (d / "repo").is_dir():
            continue
        if only and only not in d.name:
            continue
        out.append(d)
    return out


def run_one(task_dir: Path, stub: bool, timeout: int) -> dict:
    meta = {"task": task_dir.name, "ok": False, "attempts": 0, "duration_s": 0.0,
            "tokens": 0, "calls": 0, "stages": [], "error": ""}
    goal_line = ""
    for line in (task_dir / "task.md").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and not line.startswith("Kategori") \
                and not line.startswith("##"):
            goal_line = line.strip()
            break
    repo = task_dir / "repo"
    work = Path(tempfile.mkdtemp(prefix="eval-"))
    shutil.copytree(repo, work, dirs_exist_ok=True)

    # --- orijinal fonksiyonları sakla ---
    orig_generate = coder.generate
    orig_classify = router.classify
    blocks = (task_dir / "solution.blocks").read_text(encoding="utf-8")
    attempts = {"n": 0}
    tok0 = dict(tracker.llm)

    def counting_generate(llm, plan, ctx, feedback=""):
        attempts["n"] += 1
        return blocks

    t0 = time.time()
    try:
        if stub:
            coder.generate = counting_generate
            router.classify = lambda llm, goal, rmap: {
                "type": "code", "complexity": "simple", "files": []}
            judge = StubJudge()
        else:
            judge = None

            def real_generate(llm, plan, ctx, feedback=""):
                attempts["n"] += 1
                return orig_generate(llm, plan, ctx, feedback)
            coder.generate = real_generate

        from llm import LLMClient
        if stub:
            llm = StubLLM()
        else:
            from judge import Judge
            llm = LLMClient()
            judge = Judge(llm, use_jev=True, decide_with_jev=True)
        res = orchestrator.run_task(
            llm, judge, work, goal_line or task_dir.name,
            auto=True, complexity_override="simple",
        )
        meta["stages"] = res.get("verification", [])
        meta["result_reason"] = str(res.get("reason", ""))[:400]
        meta["needs_confirmation"] = bool(res.get("needs_confirmation"))
        # Yazma olduysa testleri bağımsız doğrula
        tests_ok, tests_out = verify.run_tests(work, timeout=min(timeout, 180))
        meta["tests_ok"] = tests_ok
        meta["ok"] = bool(res.get("ok")) and bool(tests_ok)
        if not tests_ok:
            meta["error"] = (tests_out or "")[-1500:]
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
        meta["trace"] = traceback.format_exc()[-2000:]
    finally:
        coder.generate = orig_generate
        router.classify = orig_classify
        meta["attempts"] = attempts["n"]
        meta["duration_s"] = round(time.time() - t0, 2)
        delta_tok = 0
        delta_calls = 0
        for k, v in tracker.llm.items():
            base = tok0.get(k, {"prompt": 0, "completion": 0, "calls": 0})
            delta_tok += (v["prompt"] + v["completion"]) - (base["prompt"] + base["completion"])
            delta_calls += v["calls"] - base["calls"]
        meta["tokens"] = delta_tok
        meta["calls"] = delta_calls
        # rollback = doğrulama/uygulama başarısız denemeler
        meta["failed_stages"] = [s["stage"] for s in meta["stages"] if not s.get("ok")]
        shutil.rmtree(work, ignore_errors=True)
    return meta


def write_results(rows, stub: bool) -> Path:
    out = HERE / "RESULTS.md"
    total = len(rows)
    ok = sum(1 for r in rows if r["ok"])
    lines = [
        "# Eval Sonuçları",
        "",
        f"Mod: **{'stub (LLM yok)' if stub else 'gerçek LLM'}** · "
        f"Tarih: {time.strftime('%Y-%m-%d %H:%M')}",
        "",
        f"**Başarı: {ok}/{total} ({100*ok/total if total else 0:.0f}%)** · "
        f"ort. süre {sum(r['duration_s'] for r in rows)/total if total else 0:.1f}s · "
        f"ort. deneme {sum(r['attempts'] for r in rows)/total if total else 0:.1f}",
        "",
        "| # | Görev | Başarı | Deneme | Süre (s) | Token | Çağrı | Düşülen aşama |",
        "|---|-------|--------|--------|----------|-------|-------|----------------|",
    ]
    for i, r in enumerate(rows, 1):
        stages = ", ".join(r.get("failed_stages") or []) or "—"
        lines.append(
            f"| {i} | `{r['task']}` | {'✅' if r['ok'] else '❌'} | {r['attempts']} | "
            f"{r['duration_s']} | {r['tokens']} | {r['calls']} | {stages} |")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (HERE / "results").mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (HERE / "results" / f"{stamp}.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stub", action="store_true", help="LLM'siz referans çözümle çalıştır")
    ap.add_argument("--only", default="", help="yalnızca adı eşleşen görev(ler)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--timeout", type=int, default=180)
    args = ap.parse_args()

    tasks = discover(args.only or None)
    if args.list:
        for t in tasks:
            print(t.name)
        return 0
    if not tasks:
        print("Görev bulunamadı. Önce: python evals/make_tasks.py")
        return 2

    rows = []
    print(f"{len(tasks)} görev · mod: {'stub' if args.stub else 'LLM'}")
    for t in tasks:
        r = run_one(t, args.stub, args.timeout)
        rows.append(r)
        mark = "✅" if r["ok"] else "❌"
        extra = "" if r["ok"] else (r.get("error") or "")[:80]
        print(f"  {mark} {r['task']:<24} {r['duration_s']:>6.2f}s  "
              f"deneme={r['attempts']}  {extra}")

    path = write_results(rows, args.stub)
    ok = sum(1 for r in rows if r["ok"])
    print(f"\nBaşarı: {ok}/{len(rows)} → {path}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
