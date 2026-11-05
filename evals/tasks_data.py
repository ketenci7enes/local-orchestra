"""Eval görev tanımları (A).

Her görev:
  id       : klasör adı
  category : bugfix | feature | refactor | multifile | edge
  goal     : modele verilecek hedef metni
  files    : başlangıç deposu {göreli_yol: içerik}
  tests    : pytest test dosyası (repo/tests/test_x.py)
  blocks   : çözümün coder çıktısı (stub modda birebir kullanılır)
"""


def F(path, content):
    return f"<<<FILE: {path}>>>\n{content.rstrip()}\n<<<END>>>"


def E(path, search, replace):
    return (f"<<<EDIT: {path}>>>\n<<<SEARCH>>>\n{search.rstrip()}\n"
            f"<<<REPLACE>>>\n{replace.rstrip()}\n<<<END>>>")


def T(name, body):
    return f'"""Eval testi: {name}"""\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n\n{body.strip()}\n'


TASKS = []


def add(**kw):
    TASKS.append(kw)


# 1 -------------------------------------------------------------------------
add(
    id="bugfix-offbyone",
    category="bugfix",
    goal="sum_upto(n) fonksiyonu 1..n toplamını döndürmeli ama n'i atlıyor. Düzelt.",
    files={"calc.py": "def sum_upto(n):\n    total = 0\n    for i in range(1, n):\n        total += i\n    return total\n"},
    tests=T("offbyone", """
from calc import sum_upto

def test_small():
    assert sum_upto(1) == 1
    assert sum_upto(5) == 15
    assert sum_upto(0) == 0
"""),
    blocks=E("calc.py", "range(1, n)", "range(1, n + 1)"),
)

# 2 -------------------------------------------------------------------------
add(
    id="bugfix-none",
    category="bugfix",
    goal="normalize(name) None veya boş gelirse boş string döndürmeli; şu an çöküyor.",
    files={"text.py": "def normalize(name):\n    return name.strip().lower()\n"},
    tests=T("none", """
from text import normalize

def test_none():
    assert normalize(None) == ""
    assert normalize("  AbC ") == "abc"
    assert normalize("") == ""
"""),
    blocks=E("text.py", 'return name.strip().lower()',
             'if not name:\n        return ""\n    return name.strip().lower()'),
)

# 3 -------------------------------------------------------------------------
add(
    id="feature-slugify",
    category="feature",
    goal="slugify(text) ekle: küçük harfe çevir, Türkçe karakterleri ASCII'ye indir, boşlukları - yap.",
    files={"slug.py": "def slugify(text):\n    raise NotImplementedError\n"},
    tests=T("slugify", """
from slug import slugify

def test_slug():
    assert slugify("Merhaba Dünya") == "merhaba-dunya"
    assert slugify("  Çok   Boşluk ") == "cok-bosluk"
    assert slugify("A_B C") == "a_b-c"
"""),
    blocks=F("slug.py", '''
_TR = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")


def slugify(text):
    if not text:
        return ""
    flat = str(text).translate(_TR).lower()
    return "-".join(flat.split())
'''),
)

# 4 -------------------------------------------------------------------------
add(
    id="feature-clamp",
    category="feature",
    goal="clamp(value, low, high) ekle: değeri aralığa sıkıştır, low > high ise hata ver.",
    files={"nums.py": "def clamp(value, low, high):\n    raise NotImplementedError\n"},
    tests=T("clamp", """
import pytest
from nums import clamp

def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
    with pytest.raises(ValueError):
        clamp(1, 10, 0)
"""),
    blocks=F("nums.py", '''
def clamp(value, low, high):
    if low > high:
        raise ValueError("low > high")
    return max(low, min(high, value))
'''),
)

# 5 -------------------------------------------------------------------------
add(
    id="refactor-area",
    category="refactor",
    goal="Şekil alanı hesabını area(shape, **kw) fonksiyonuna çıkar; circle ve rect onu kullansın.",
    files={"shapes.py": (
        "import math\n\n"
        "def circle_area(r):\n    return math.pi * r * r\n\n"
        "def rect_area(w, h):\n    return w * h\n")},
    tests=T("area", """
import math
from shapes import area, circle_area, rect_area

def test_area():
    assert abs(area("circle", r=2) - math.pi * 4) < 1e-9
    assert area("rect", w=2, h=3) == 6
    assert circle_area(1) == area("circle", r=1)
    assert rect_area(2, 2) == 4
"""),
    blocks=F("shapes.py", '''
import math


def area(shape, **kw):
    if shape == "circle":
        r = kw["r"]
        return math.pi * r * r
    if shape == "rect":
        return kw["w"] * kw["h"]
    raise ValueError(f"bilinmeyen şekil: {shape}")


def circle_area(r):
    return area("circle", r=r)


def rect_area(w, h):
    return area("rect", w=w, h=h)
'''),
)

# 6 -------------------------------------------------------------------------
add(
    id="edge-empty-list",
    category="edge",
    goal="average(values) boş listede ZeroDivisionError vermesin, None döndürsün.",
    files={"stats.py": "def average(values):\n    return sum(values) / len(values)\n"},
    tests=T("empty", """
from stats import average

def test_empty():
    assert average([]) is None
    assert average([2, 4]) == 3
"""),
    blocks=E("stats.py", 'return sum(values) / len(values)',
             'if not values:\n        return None\n    return sum(values) / len(values)'),
)

# 7 -------------------------------------------------------------------------
add(
    id="multifile-import",
    category="multifile",
    goal="utils/mathx.py içine gcd ekle ve app.py'de kullan.",
    files={
        "utils/__init__.py": "",
        "utils/mathx.py": "def add(a, b):\n    return a + b\n",
        "app.py": "from utils.mathx import add\n\n\ndef total(a, b):\n    return add(a, b)\n",
    },
    tests=T("multifile", """
from utils.mathx import gcd
from app import total, ratio_gcd

def test_gcd():
    assert gcd(12, 18) == 6
    assert gcd(7, 13) == 1

def test_usage():
    assert total(1, 2) == 3
    assert ratio_gcd(12, 18) == 6
"""),
    blocks=(F("utils/mathx.py", '''
def add(a, b):
    return a + b


def gcd(a, b):
    a, b = abs(a), abs(b)
    while b:
        a, b = b, a % b
    return a
''') + "\n" + F("app.py", '''
from utils.mathx import add, gcd


def total(a, b):
    return add(a, b)


def ratio_gcd(a, b):
    return gcd(a, b)
''')),
)

# 8 -------------------------------------------------------------------------
add(
    id="feature-json-io",
    category="feature",
    goal="save_json(path, data) ve load_json(path, default) ekle; hatalı/eksik dosyada default dönsün.",
    files={"io_utils.py": "def save_json(path, data):\n    raise NotImplementedError\n\n\ndef load_json(path, default=None):\n    raise NotImplementedError\n"},
    tests=T("jsonio", """
import json
from io_utils import save_json, load_json

def test_roundtrip(tmp_path):
    p = tmp_path / "a.json"
    save_json(p, {"x": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"x": 1}
    assert load_json(p) == {"x": 1}
    assert load_json(tmp_path / "missing.json", {"d": 1}) == {"d": 1}

def test_broken(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{bozuk", encoding="utf-8")
    assert load_json(p, []) == []
"""),
    blocks=F("io_utils.py", '''
import json
from pathlib import Path


def save_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
'''),
)

# 9 -------------------------------------------------------------------------
add(
    id="feature-csv-line",
    category="feature",
    goal="parse_csv_line(line) ekle: tırnaklı alanları ve kaçışlı tırnakları desteklesin.",
    files={"csvx.py": "def parse_csv_line(line):\n    raise NotImplementedError\n"},
    tests=T("csv", """
from csvx import parse_csv_line

def test_plain():
    assert parse_csv_line("a,b,c") == ["a", "b", "c"]

def test_quoted():
    assert parse_csv_line('a,"b,c",d') == ["a", "b,c", "d"]

def test_escaped():
    raw = '"he said ' + '""hi""' + '",x'
    assert parse_csv_line(raw) == ['he said "hi"', "x"]
"""),
    blocks=F("csvx.py", '''
def parse_csv_line(line):
    out, cur, i, in_q = [], [], 0, False
    while i < len(line):
        ch = line[i]
        if in_q:
            if ch == '"':
                if i + 1 < len(line) and line[i + 1] == '"':
                    cur.append('"')
                    i += 2
                    continue
                in_q = False
                i += 1
                continue
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            in_q = True
        elif ch == ",":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return out
'''),
)

# 10 ------------------------------------------------------------------------
add(
    id="bugfix-dataclass-eq",
    category="bugfix",
    goal="Point sınıfı iki nokta aynı koordinattaysa eşit sayılmalı (__eq__ ekle).",
    files={"geom.py": "class Point:\n    def __init__(self, x, y):\n        self.x = x\n        self.y = y\n"},
    tests=T("point", """
from geom import Point

def test_eq():
    assert Point(1, 2) == Point(1, 2)
    assert Point(1, 2) != Point(2, 1)
    assert len({Point(0, 0), Point(0, 0)}) == 1
"""),
    blocks=F("geom.py", '''
class Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __eq__(self, other):
        if not isinstance(other, Point):
            return NotImplemented
        return (self.x, self.y) == (other.x, other.y)

    def __hash__(self):
        return hash((self.x, self.y))
'''),
)

# 11 ------------------------------------------------------------------------
add(
    id="bugfix-format",
    category="bugfix",
    goal="greet(name, city) çıktısı 'Merhaba {name}, {city}!' olmalı; format string bozuk.",
    files={"greet.py": "def greet(name, city):\n    return 'Merhaba {name}, {city}!'\n"},
    tests=T("greet", """
from greet import greet

def test_greet():
    assert greet("Ali", "Ankara") == "Merhaba Ali, Ankara!"
"""),
    blocks=E("greet.py", "return 'Merhaba {name}, {city}!'",
             "return f'Merhaba {name}, {city}!'"),
)

# 12 ------------------------------------------------------------------------
add(
    id="feature-deep-merge",
    category="feature",
    goal="deep_merge(base, extra) ekle: sözlükleri özyinelemeli birleştir, extra kazanır, base'i değiştirme.",
    files={"merge.py": "def deep_merge(base, extra):\n    raise NotImplementedError\n"},
    tests=T("merge", """
from merge import deep_merge

def test_merge():
    a = {"x": 1, "n": {"p": 1, "q": 2}}
    b = {"n": {"q": 9, "r": 3}, "y": 2}
    out = deep_merge(a, b)
    assert out == {"x": 1, "n": {"p": 1, "q": 9, "r": 3}, "y": 2}
    assert a == {"x": 1, "n": {"p": 1, "q": 2}}
"""),
    blocks=F("merge.py", '''
import copy


def deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out
'''),
)

# 13 ------------------------------------------------------------------------
add(
    id="feature-stable-sort",
    category="feature",
    goal="sort_records(records, key) ekle: kararlı sıralama, eşitlerde özgün sıra korunsun, girdiyi değiştirmesin.",
    files={"records.py": "def sort_records(records, key):\n    raise NotImplementedError\n"},
    tests=T("sort", """
from records import sort_records

def test_sort():
    data = [{"k": 2, "i": 1}, {"k": 1, "i": 2}, {"k": 2, "i": 3}]
    out = sort_records(data, "k")
    assert [r["i"] for r in out] == [2, 1, 3]
    assert data[0]["i"] == 1
"""),
    blocks=F("records.py", '''
def sort_records(records, key):
    return sorted(records, key=lambda r: r[key])
'''),
)

# 14 ------------------------------------------------------------------------
add(
    id="feature-email",
    category="feature",
    goal="is_valid_email(value) ekle: tek @, nokta içeren alan adı, boşluk yok.",
    files={"valid.py": "def is_valid_email(value):\n    raise NotImplementedError\n"},
    tests=T("email", """
from valid import is_valid_email

def test_valid():
    assert is_valid_email("a@b.com")
    assert is_valid_email("x.y@sub.domain.org")

def test_invalid():
    for bad in ["", "a", "a@b", "a b@c.com", "a@@b.com", "@b.com"]:
        assert not is_valid_email(bad), bad
"""),
    blocks=F("valid.py", '''
def is_valid_email(value):
    if not isinstance(value, str) or not value or " " in value:
        return False
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    if not local or not domain:
        return False
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        return False
    return True
'''),
)

# 15 ------------------------------------------------------------------------
add(
    id="feature-lru",
    category="feature",
    goal="LRU(maxsize) sınıfı ekle: get/set, en eski kaydı at.",
    files={"cache.py": "class LRU:\n    def __init__(self, maxsize=2):\n        raise NotImplementedError\n"},
    tests=T("lru", """
from cache import LRU

def test_lru():
    c = LRU(2)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1
    c.set("c", 3)
    assert c.get("b") is None
    assert c.get("a") == 1
    assert c.get("c") == 3
"""),
    blocks=F("cache.py", '''
from collections import OrderedDict


class LRU:
    def __init__(self, maxsize=2):
        self.maxsize = max(1, int(maxsize))
        self.data = OrderedDict()

    def get(self, key):
        if key not in self.data:
            return None
        self.data.move_to_end(key)
        return self.data[key]

    def set(self, key, value):
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.maxsize:
            self.data.popitem(last=False)
'''),
)

# 16 ------------------------------------------------------------------------
add(
    id="feature-retry",
    category="feature",
    goal="retry(times) dekoratörü ekle: hata olursa tekrar dene, son hatayı yükselt.",
    files={"retrylib.py": "def retry(times=2):\n    raise NotImplementedError\n"},
    tests=T("retry", """
import pytest
from retrylib import retry

def test_retry_ok():
    calls = {"n": 0}

    @retry(3)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("x")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3

def test_retry_fail():
    @retry(2)
    def always():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        always()
"""),
    blocks=F("retrylib.py", '''
import functools


def retry(times=2):
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last = None
            for _ in range(max(1, int(times))):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:  # noqa: BLE001
                    last = e
            raise last
        return wrapper
    return deco
'''),
)

# 17 ------------------------------------------------------------------------
add(
    id="feature-duration",
    category="feature",
    goal="parse_duration(text) ekle: '90m' → 5400, '1h30m' → 5400, '45s' → 45.",
    files={"duration.py": "def parse_duration(text):\n    raise NotImplementedError\n"},
    tests=T("duration", """
import pytest
from duration import parse_duration

def test_duration():
    assert parse_duration("90m") == 5400
    assert parse_duration("1h30m") == 5400
    assert parse_duration("45s") == 45
    assert parse_duration("2h") == 7200

def test_bad():
    with pytest.raises(ValueError):
        parse_duration("abc")
"""),
    blocks=F("duration.py", '''
import re

_UNITS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text):
    if not text:
        raise ValueError("boş süre")
    parts = re.findall(r"(\\d+)\\s*([hms])", str(text).lower())
    if not parts:
        raise ValueError(f"geçersiz süre: {text}")
    return sum(int(n) * _UNITS[u] for n, u in parts)
'''),
)

# 18 ------------------------------------------------------------------------
add(
    id="feature-chunk",
    category="feature",
    goal="chunk(items, size) ekle: listeyi size'lık parçalara böl, size<=0 ise ValueError.",
    files={"chunks.py": "def chunk(items, size):\n    raise NotImplementedError\n"},
    tests=T("chunk", """
import pytest
from chunks import chunk

def test_chunk():
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
    assert chunk([], 3) == []
    with pytest.raises(ValueError):
        chunk([1], 0)
"""),
    blocks=F("chunks.py", '''
def chunk(items, size):
    if size <= 0:
        raise ValueError("size > 0 olmalı")
    return [list(items[i:i + size]) for i in range(0, len(items), size)]
'''),
)

# 19 ------------------------------------------------------------------------
add(
    id="feature-roman",
    category="feature",
    goal="to_roman(n) ekle: 1..3999 arası Roma rakamı, aralık dışında ValueError.",
    files={"roman.py": "def to_roman(n):\n    raise NotImplementedError\n"},
    tests=T("roman", """
import pytest
from roman import to_roman

def test_roman():
    assert to_roman(1) == "I"
    assert to_roman(9) == "IX"
    assert to_roman(1994) == "MCMXCIV"
    assert to_roman(3999) == "MMMCMXCIX"
    for bad in (0, -1, 4000):
        with pytest.raises(ValueError):
            to_roman(bad)
"""),
    blocks=F("roman.py", '''
_VALUES = [
    (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
    (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
    (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
]


def to_roman(n):
    if not isinstance(n, int) or n < 1 or n > 3999:
        raise ValueError("1..3999 aralığında olmalı")
    out = []
    for value, sym in _VALUES:
        while n >= value:
            out.append(sym)
            n -= value
    return "".join(out)
'''),
)

# 20 ------------------------------------------------------------------------
add(
    id="feature-binary-search",
    category="feature",
    goal="bsearch(items, target) ekle: sıralı listede indeks veya -1, O(log n).",
    files={"searchx.py": "def bsearch(items, target):\n    raise NotImplementedError\n"},
    tests=T("bsearch", """
from searchx import bsearch

def test_bsearch():
    data = [1, 3, 5, 7, 9]
    assert bsearch(data, 7) == 3
    assert bsearch(data, 1) == 0
    assert bsearch(data, 4) == -1
    assert bsearch([], 1) == -1
"""),
    blocks=F("searchx.py", '''
def bsearch(items, target):
    lo, hi = 0, len(items) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if items[mid] == target:
            return mid
        if items[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
'''),
)


# 21 ------------------------------------------------------------------------
# REGRESYON: Türkçe "İ".lower() birleşik nokta (U+0307) üretir. ASCII'ye indirme
# GEREKİYORSA önce str.translate, SONRA .lower() yapılmalı. Canlı orkestra
# koşusunda yakalanan gerçek hata.
add(
    id="edge-turkish-i",
    category="edge",
    goal=("snake_case(text) ekle: Türkçe karakterleri ASCII'ye indirip boşlukları alt çizgi yap. "
          "'İstanbul Çok Güzel' -> 'istanbul_cok_guzel'. Dikkat: 'İ'.lower() birleşik nokta "
          "(U+0307) üretir; birleşik işaret ÜRETME."),
    files={"names.py": "def snake_case(text):\n    raise NotImplementedError\n"},
    tests=T("turkish_i", """
from names import snake_case


def test_istanbul():
    out = snake_case("İstanbul Çok Güzel")
    assert out == "istanbul_cok_guzel", repr(out)


def test_no_combining_marks():
    for s in ("İstanbul", "Iğdır", "Şişli", "ÇĞİÖŞÜ"):
        out = snake_case(s)
        bad = [hex(ord(c)) for c in out if 0x300 <= ord(c) <= 0x36F]
        assert not bad, (s, repr(out), bad)


def test_ascii_and_edges():
    assert snake_case("Merhaba Dünya") == "merhaba_dunya"
    assert snake_case(None) == ""
    assert snake_case("") == ""
    assert snake_case("  Çok   Boşluk ") == "cok_bosluk"
"""),
    blocks=F("names.py", '''
# ÖNEMLİ: önce translate, SONRA lower. Ters sıra "İ" -> "i" + U+0307 üretir.
_TR_ASCII = str.maketrans({
    "Ç": "C", "Ğ": "G", "İ": "I", "Ö": "O", "Ş": "S", "Ü": "U",
    "ç": "c", "ğ": "g", "ı": "i", "ö": "o", "ş": "s", "ü": "u",
})


def snake_case(text):
    if not text:
        return ""
    s = str(text).translate(_TR_ASCII).lower()
    return "_".join(s.split())
'''),
)
