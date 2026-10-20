"""Kurul (Council): 3 bağımsız KIDEMLİ YAZILIM UZMANI **paralel** öneri üretir.

Tutum (agresif / güvenli / profesyonel) ayrımı yoktur: üç üye de aynı
"kıdemli yazılım uzmanı" personasıyla, üretim kalitesinde ve uygulanabilir
çözüm üretir. Farklılık, her üyenin farklı bir model olmasından gelir.

Öneriler ThreadPoolExecutor ile EŞZAMANLI toplanır (bounded: 3 işçi).
Böylece toplam süre = sürelerin toplamı değil, en yavaş üyenin süresi olur.
Kazanan önerinin sahibi, coder için detaylı uygulama promptunu yazar.
"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from console import log
from llm import LLMError, parse_json
from tracker import tracker

# Kurul üyeleri (config.yaml rollerine karşılık gelir).
COUNCIL_ROLES = ("council_a", "council_b", "council_c")

PROPOSAL_SYSTEM = """Sen kıdemli (senior) bir yazılım uzmanı ve mimarısın; bir KURUL üyesisin.
Verilen görevi ve bağlamı, uzun yıllara dayanan üretim tecrübenle analiz et ve görevi
NASIL yapacağına dair TEK, eksiksiz ve doğrudan uygulanabilir bir öneri üret.

Beklentiler:
- Kök nedeni doğru teşhis et; semptomu değil sorunu çöz.
- Mevcut mimariye, kod stiline ve isimlendirmeye uy; gereksiz bağımlılık veya
  gereksiz büyük refaktör önerme.
- Üretim kalitesinde çözüm öner: tip ipuçları, hata yönetimi, kenar durumları.
- Çözüm test edilebilir olsun; doğrulama için çalıştırılacak test/komutu belirt.
- Emin olmadığın noktayı riskler alanında dürüstçe belirt.
- KISA YAZ: "details" en fazla 200 kelime olsun. Uzun kod bloğu YAPMA;
  dosya + fonksiyon + değişiklik tarifi ver.

SADECE şu JSON'u döndür:
{"strategy": "tek cümle yaklaşım", "details": "adım adım plan; hangi dosyalar değişecek", "files": ["göreli/yol"], "risks": "kısa risk notu"}"""

EXPAND_SYSTEM = """Sen kıdemli bir yazılım uzmanı ve mimarısın. KURUL'un seçtiği stratejiyi, alt coder modelinin
doğrudan uygulayabileceği DETAYLI bir yönergye dönüştür.
- Hangi dosyada ne değişeceğini açıkça belirt.
- Mevcut kodu değiştiriyorsan hedef kod parçalarını tarif et.
- Gereksinimleri eksiksiz ve net sırala. JSON veya biçim kısıtı YOK; serbest metin yaz."""


def _label(llm, role: str) -> str:
    return llm.cfg["roles"].get(role, {}).get("label", role)


def _model_id(llm, role: str) -> str:
    """Rolün birincil (config'de ilk) modeli."""
    cands = llm.cfg["roles"].get(role, {}).get("models", [])
    return cands[0]["model"] if cands else role


_primary_model = _model_id


_FIELD_RE = re.compile(r'"?\b(strategy|details|risks)\b"?\s*:\s*"((?:[^"\\]|\\.)*)"', re.S)
_FILES_RE = re.compile(r'"?\bfiles\b"?\s*:\s*\[(.*?)\]', re.S)


def _salvage(raw: str) -> dict:
    """Bozuk JSON'dan alanları kurtarır.

    Bazı modeller `{"}strategy": ...` gibi fazladan tırnak/parantez üretiyor ve
    json.loads patlıyor. Bu durumda üyeyi tamamen kaybetmek yerine alanları
    regex ile çıkarıp öneriyi kullanılabilir hale getiriyoruz.
    """
    out = {}
    for m in _FIELD_RE.finditer(raw):
        key, val = m.group(1), m.group(2)
        if key in out:
            continue
        try:
            out[key] = json.loads(f'"{val}"')
        except ValueError:
            out[key] = val.replace("\\n", "\n")
    fm = _FILES_RE.search(raw)
    if fm:
        try:
            files = json.loads("[" + fm.group(1) + "]")
            if isinstance(files, list):
                out["files"] = [str(f) for f in files]
        except ValueError:
            pass
    return out


def _stamp_author(llm, role: str, data: dict) -> dict:
    """Öneriye, GERÇEKTE yanıt veren sağlayıcı/modeli işler (şeffaflık)."""
    used = getattr(llm, "last_used", {}).get(role)
    if used:
        data["author_provider"] = used.get("provider")
        data["author_model"] = used.get("model")
        data["fell_back"] = used.get("model") != _primary_model(llm, role)
    return data


def _one_proposal(llm, role: str, goal: str, context: str) -> dict | None:
    msgs = [
        {"role": "system", "content": PROPOSAL_SYSTEM},
        {"role": "user", "content": f"Bağlam:\n{context}\n\nGörev: {goal}"},
    ]
    for _ in range(2):
        try:
            raw = llm.chat(role, msgs, json_mode=True)
        except LLMError as e:
            # FAZ 1d: üye düşüşleri sessiz kalmamalı.
            tracker.log("council_member_failed", role=role, error=str(e)[:300])
            log(f"⚠️  Kurul üyesi düştü: {role} ({str(e)[:120]})")
            return None

        err = None
        try:
            data = parse_json(raw)
            if data.get("strategy"):
                return _stamp_author(llm, role, data)
            err = "strategy alanı boş"
        except (ValueError, TypeError) as e:
            err = str(e)

        # Bozuk/eksik JSON: alanları kurtarmayı dene, olmazsa modele tekrar sor.
        salvaged = _salvage(raw)
        if salvaged.get("strategy"):
            tracker.log("council_json_salvaged", role=role, error=str(err)[:200])
            log(f"🩹 Kurul üyesi {role}: bozuk JSON kurtarıldı.")
            return _stamp_author(llm, role, salvaged)

        tracker.log("council_invalid_json", role=role, error=str(err)[:200], raw=raw[:600])
        msgs += [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": f"Çıktı geçersiz ({err}). Yalnızca belirtilen JSON şemasını döndür."},
        ]
    log(f"⚠️  Kurul üyesi {role} geçerli öneri üretemedi (2 deneme).")
    tracker.log("council_member_no_output", role=role)
    return None


def _timed_one(llm, role: str, goal: str, context: str):
    t0 = time.time()
    data = _one_proposal(llm, role, goal, context)
    return role, data, time.time() - t0


def generate_proposals(llm, goal: str, context: str) -> list:
    """Her kurul üyesinden birer öneri toplar — PARALEL (bounded concurrency).

    Toplam süre ≈ en yavaş üyenin süresi (süreler toplamı değil). Sonuçlar
    her zaman COUNCIL_ROLES sırasına göre dizilir; yazar kimliği ve gerçekte
    yanıt veren model öneriye iliştirilir; her öneri terminale basılır.
    """
    roles = list(COUNCIL_ROLES)
    results = {}
    durations = {}

    if len(roles) > 1:
        # Sınırlı eşzamanlılık: aynı anda en fazla 3 istek (API kotasını korur).
        with ThreadPoolExecutor(max_workers=len(roles), thread_name_prefix="council") as pool:
            futures = {
                pool.submit(_timed_one, llm, role, goal, context): role for role in roles
            }
            for fut in as_completed(futures):
                role = futures[fut]
                try:
                    role, data, dt = fut.result()
                except Exception as e:  # beklenmeyen hata diğer üyeleri düşürmesin
                    tracker.log("council_member_crashed", role=role, error=str(e)[:300])
                    log(f"⚠️  Kurul üyesi çöktü: {role} ({str(e)[:120]})")
                    data, dt = None, 0.0
                results[role] = data
                durations[role] = dt
    else:
        for role in roles:
            role, data, dt = _timed_one(llm, role, goal, context)
            results[role] = data
            durations[role] = dt

    proposals = []
    for role in roles:  # sıra korunur (deterministik çıktı)
        data = results.get(role)
        if not data:
            continue
        data = dict(data)
        data["author_role"] = role
        data["author_label"] = _label(llm, role)
        data.setdefault("author_model", _model_id(llm, role))
        data["duration_s"] = round(durations.get(role, 0.0), 1)
        proposals.append(data)

    if not proposals:
        tracker.log("council_empty")
        log("⚠️  Kurul hiç öneri üretemedi (tüm üyeler başarısız).")
    elif len(proposals) == 1:
        log("⚠️  Kurul yalnızca tek öneri üretebildi; karşılaştırma sınırlı.")

    for i, p in enumerate(proposals, 1):
        p["id"] = i
        fallback_note = " ⚠️ (birincil model yanıt vermedi, YEDEK model üretti)" if p.get("fell_back") else ""
        log(f"🧩 Öneri {i} — {p['author_label']} (`{p['author_model']}`, {p.get('duration_s')}s){fallback_note}")
        log(f"   Strateji : {p.get('strategy', '')}")
        log(f"   Detay    : {str(p.get('details', ''))[:900]}")
        if p.get("files"):
            log(f"   Dosyalar : {', '.join(map(str, p['files']))}")
        log(f"   Riskler  : {str(p.get('risks', ''))}")
        log("")
    return proposals


def expand_prompt(llm, winner: dict, goal: str, context: str) -> str:
    """Kazanan önerinin sahibi, kıdemli uzman personaasıyla coder için detaylı promptu yazar."""
    role = winner["author_role"]
    msgs = [
        {"role": "system", "content": EXPAND_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Görev: {goal}\n\nBağlam:\n{context}\n\n"
                f"SEÇİLEN STRATEJİ:\n{winner.get('strategy', '')}\n"
                f"DETAY: {winner.get('details', '')}\n"
                f"RİSKLER: {winner.get('risks', '')}\n\n"
                "Bunu coder modelinin uygulayacağı detaylı bir yönergye dönüştür."
            ),
        },
    ]
    try:
        # B2/A4: kazanan üye ne olursa olsun kesilme riskini kapat (explicit 8000).
        return llm.chat(role, msgs, max_tokens=8000)
    except LLMError:
        # Genişletme başarısız olursa önerinin kendisini kullan
        return f"Strateji: {winner.get('strategy', '')}\nDetay: {winner.get('details', '')}\nRiskler: {winner.get('risks', '')}"
