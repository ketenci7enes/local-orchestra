"""Tek OpenAI-uyumlu istemci: rol bazlı model listesi, retry, failover."""
import json
import os
import random
import re
import time
from pathlib import Path

import yaml
from dotenv import load_dotenv

from console import log
from tracker import tracker

# `openai` SDK'si ~3.5 s'de yükleniyor (binlerce tip dosyası). MCP stdio sunucusu
# açılışta buna takılıp OpenCode'nun startup timeout'una düşüyordu. Bu yüzden
# tembel yüklenir: yalnızca ilk gerçek model çağrısında içe aktarılır.
_OPENAI_MOD = None


def _openai():
    global _OPENAI_MOD
    if _OPENAI_MOD is None:
        import openai as _mod
        _OPENAI_MOD = _mod
    return _OPENAI_MOD

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

_THINK_RE = re.compile(r"(?:<think>.*?</think>)|(?:^<think>)", re.S)


class LLMError(RuntimeError):
    pass


def parse_json(text: str) -> dict:
    """Modelin çıktısından JSON nesnesini ayıklar (```json çiti ve ön/son metin tolere edilir)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("JSON bulunamadı")
    return json.loads(t[start : end + 1])


class LLMClient:
    def __init__(self, config_path=None):
        path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
        with open(path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        self._clients = {}
        self._cooldown = {}  # (provider, model) -> bu zamana kadar atla
        self.last_used = {}  # role -> {"provider", "model"} : gerçekte yanıt veren aday (şeffaflık)

    def _client(self, provider):
        if provider not in self._clients:
            p = self.cfg["providers"][provider]
            key = os.getenv(p["api_key_env"])
            if not key:
                raise LLMError(f"{p['api_key_env']} tanımlı değil (.env)")
            self._clients[provider] = _openai().OpenAI(
                base_url=p["base_url"],
                api_key=key,
                # Sağlayıcı bazlı override (ör. EVREN'de yavaş ama güçlü modeller için 150s)
                timeout=p.get("timeout", self.cfg.get("timeout", 90)),
                max_retries=0,  # retry/failover'ı biz yönetiyoruz
                default_headers=p.get("headers") or {},
            )
        return self._clients[provider]

    def chat(self, role: str, messages: list, json_mode: bool = False, **overrides) -> str:
        spec = self.cfg["roles"][role]
        params = {**spec.get("params", {}), **overrides}
        retries = int(self.cfg.get("retries", 2))
        last_err = None

        for cand in spec["models"]:
            key = (cand["provider"], cand["model"])
            if self._cooldown.get(key, 0) > time.time():
                continue
            use_json = json_mode
            for attempt in range(retries + 1):
                try:
                    kwargs = dict(model=cand["model"], messages=messages, **params)
                    if use_json:
                        kwargs["response_format"] = {"type": "json_object"}
                    resp = self._client(cand["provider"]).chat.completions.create(**kwargs)
                    choice = resp.choices[0]
                    # A3: max_tokens'ta sessiz kesilmeyi yakala → sıradaki adaya geç.
                    if getattr(choice, "finish_reason", None) == "length":
                        u = getattr(resp, "usage", None)
                        tracker.log(
                            "llm_truncated", role=role, provider=cand["provider"], model=cand["model"],
                            completion=getattr(u, "completion_tokens", 0) or 0,
                        )
                        raise LLMError(f"[{cand['provider']}/{cand['model']}] çıktı max_tokens'ta kesildi")
                    raw_content = choice.message.content or ""
                    # Düşünce (thinking) bloğu varsa yakala ve terminale bas
                    th_match = _THINK_RE.search(raw_content)
                    if th_match:
                        th_text = th_match.group(0).replace("x", "").replace("x", "").strip()
                        if th_text:
                            log(f"\n🧠 [{role}] Düşünce (Thinking):\n" + "\n".join(f"   │ {l}" for l in th_text.splitlines()[:15]))
                            if len(th_text.splitlines()) > 15:
                                log("   │ ... (düşünce kısaltıldı)")
                            log()

                    text = _THINK_RE.sub("", raw_content).strip()
                    if not text:
                        raise LLMError("boş yanıt (max_tokens düşük olabilir)")
                    u = getattr(resp, "usage", None)
                    p_tok = getattr(u, "prompt_tokens", 0) or 0
                    c_tok = getattr(u, "completion_tokens", 0) or 0
                    tracker.add_llm(cand["provider"], cand["model"], p_tok, c_tok)
                    tracker.log("llm", role=role, provider=cand["provider"], model=cand["model"])
                    # Şeffaflık: bu rol için gerçekte hangi sağlayıcı/model yanıt verdi?
                    self.last_used[role] = {"provider": cand["provider"], "model": cand["model"]}

                    total_calls = sum(s["calls"] for s in tracker.llm.values())
                    log(f"📊 [{cand['provider']}/{cand['model'].split('/')[-1]}] -> Context: {p_tok} tkn | Yanıt: {c_tok} tkn | Toplam Çağrı: {total_calls}")
                    return text
                except _openai().RateLimitError as e:
                    msg = str(e).lower()
                    self._cooldown[key] = time.time() + (3600 if "day" in msg else 60)
                    last_err = e
                    tracker.log("llm_fail", role=role, provider=cand["provider"], model=cand["model"], error=str(e)[:300])
                    break  # sıradaki aday modele geç
                except _openai().APITimeoutError as e:
                    # Zaman aşımında AYNI adayı tekrar denemek sadece süreyi ikiye katlar:
                    # doğrudan sıradaki adaya geç (ör. yavaş glm-5.3 -> hızlı deepseek).
                    last_err = e
                    tracker.log("llm_fail", role=role, provider=cand["provider"], model=cand["model"], error=str(e)[:300])
                    log(f"⏱️  [{cand['provider']}/{cand['model'].split('/')[-1]}] zaman aşımı → sıradaki adaya geçiliyor")
                    break
                except _openai().APIConnectionError as e:
                    last_err = e
                    tracker.log("llm_fail", role=role, provider=cand["provider"], model=cand["model"], error=str(e)[:300])
                    time.sleep(2**attempt + random.random())
                except _openai().APIStatusError as e:
                    last_err = e
                    if e.status_code == 400 and use_json:
                        use_json = False  # model response_format desteklemiyor olabilir
                        continue
                    tracker.log("llm_fail", role=role, provider=cand["provider"], model=cand["model"], error=str(e)[:300])
                    if e.status_code >= 500 or e.status_code == 408:
                        time.sleep(2**attempt + random.random())
                        continue
                    break  # 401/403/404 vb.: bu aday kullanılamaz
                except (LLMError, IndexError, AttributeError, TypeError) as e:
                    last_err = e
                    tracker.log("llm_fail", role=role, provider=cand["provider"], model=cand["model"], error=str(e)[:300])
                    break
        raise LLMError(f"'{role}' rolü için tüm adaylar başarısız: {last_err}")


def cooldown_wait(llm, role: str) -> float:
    """Bir rolün adayları arasındaki en kısa kalan cooldown süresini (saniye) döndürür.

    A5: orchestrator 429 sonrası ne kadar bekleyeceğini buna göre belirler."""
    cfg = getattr(llm, "cfg", None)
    cooldown = getattr(llm, "_cooldown", None)
    if not cfg or cooldown is None:
        return 0.0
    spec = cfg.get("roles", {}).get(role, {})
    now = time.time()
    waits = [cooldown.get((c["provider"], c["model"]), 0) - now for c in spec.get("models", [])]
    waits = [w for w in waits if w > 0]
    return min(waits) if waits else 0.0