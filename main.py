"""CLI: python main.py --workspace ../benim-projem [--auto] [--git] [--no-jev]"""
import argparse
import sys
import time
from pathlib import Path

import orchestrator
from judge import Judge
from llm import LLMClient, LLMError
from tracker import tracker
from workspace import git_checkout_branch


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows konsolunda emoji/Türkçe için
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", default=".", help="Üzerinde çalışılacak proje klasörü")
    ap.add_argument("--auto", action="store_true", help="Onay sormadan uygula (riskli!)")
    ap.add_argument("--git", action="store_true", help="Ayrı dal aç ve her görevi commit'le")
    ap.add_argument("--no-jev", action="store_true", help="Jev AI yerine yalnızca LLM hakem kullan")
    args = ap.parse_args()

    root = Path(args.workspace).resolve()
    if not root.is_dir():
        sys.exit(f"Klasör yok: {root}")

    llm = LLMClient()
    judge = Judge(llm, use_jev=not args.no_jev, decide_with_jev=not args.no_jev)
    if args.git:
        branch = f"orchestra/{time.strftime('%Y%m%d-%H%M%S')}"
        print(f"🌿 Git dalı: {branch}" if git_checkout_branch(root, branch) else "⚠️  Git dalı açılamadı, devam ediliyor")

    print(f"🤖 Lokal Orkestra v2 | çalışma alanı: {root}")
    while True:
        goal = input("\nGörev ('q' = çıkış): ").strip()
        if goal.lower() in ("q", "quit", "exit"):
            break
        if not goal:
            continue
        files = input("Dosyalar (boşlukla ayır, boş = otomatik): ").split()
        try:
            res = orchestrator.run_task(llm, judge, root, goal, files, auto=args.auto, git=args.git)
        except LLMError as e:
            print(f"❌ Model hatası: {e}")
        except KeyboardInterrupt:
            print("\n⏹️  İptal edildi")
        else:
            if "answer" in res:
                print("\n" + res["answer"])
            elif res["ok"]:
                print(f"✅ Uygulandı: {', '.join(res['files'])}")
            else:
                print(f"❌ Başarısız: {res['reason']}")
        tracker.report()


if __name__ == "__main__":
    main()
