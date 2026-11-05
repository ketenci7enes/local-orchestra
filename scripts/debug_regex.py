import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
import re

_THINK_RE = re.compile(
    r"(?:[\U0001F9E0\U0001F4AD]?<\|begin_of_thought\|>.*?<\|end_of_thought\|>[\U0001F9E0\U0001F4AD]?)"
    r"|(?:^[\U0001F9E0\U0001F4AD]?<\|begin_of_thought\|>)",
    re.S
)

with open(_ROOT / 'tests/test_core.py', 'r', encoding='utf-8') as f:
    content = f.read()

import re as re2
m = re2.search(r'_Ok\((.+?)\)', content)
if m:
    test_str = m.group(1).strip('"')
    print('Found test string:', repr(test_str))
    print('Full bytes:', [hex(ord(c)) for c in test_str])