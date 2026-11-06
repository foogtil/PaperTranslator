"""Conservative algebra ranges independent of the PDF's font selection."""
from __future__ import annotations

import re

_ID = r"[A-Za-zα-ωΑ-Ω][A-Za-z0-9α-ωΑ-Ω_]{0,63}"
_NUMBER = r"\d+(?:\.\d+)?%?"
_CHAR = r"[A-Za-z0-9α-ωΑ-Ω_+−*/^=., \t-]"
_PAREN = rf"\((?:{_CHAR}{{0,120}}\({_CHAR}{{1,120}}\){_CHAR}{{0,120}}|{_CHAR}{{1,120}})\)"
_ATOM = rf"(?:{_ID}(?:\s*{_PAREN})?|{_NUMBER}|{_PAREN})(?:\s*\^\s*(?:{_ID}|{_NUMBER}|{_PAREN}))?"
_EXPR = rf"{_ATOM}(?:\s*[+−*/×÷-]\s*{_ATOM})*"
_BOUNDARY = r"(?<![A-Za-z0-9_α-ωΑ-Ω/])"
_FORMULA = re.compile(rf"{_BOUNDARY}{_EXPR}\s*(?:=|≤|≥|≠|≈|<|>)\s*{_EXPR}|{_BOUNDARY}{_ATOM}(?:\s*[+−*/×÷^-]\s*{_ATOM})+", re.UNICODE)


def math_ranges(text):
    identifiers = [m.span() for m in re.finditer(r'https?://[^\s<>]+|\b10\.\d{4,9}/[^\s<>]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', text)]
    for match in _FORMULA.finditer(text):
        if any(match.start() < end and match.end() > start for start, end in identifiers):
            continue
        value = match.group()
        if match.start() and text[match.start()-1].isalpha() and not '\u3400' <= text[match.start()-1] <= '\u9fff':
            continue  # Do not start midway through an accented natural word.
        if len(value) > 500 or value.count('(') != value.count(')'):
            continue
        # Natural-language compounds such as English-Chinese are not algebra.
        names = re.findall(_ID, value)
        relation = re.search(r"[=≤≥≠≈<>]", value)
        if relation:
            left, right = value[:relation.start()].strip(), value[relation.end():].strip()
            # A natural label such as "learning rate = 0.1" must still be
            # translated. Its number is protected by the ordinary token pass.
            if (re.fullmatch(r'[A-Za-z]{4,}', left)
                    and not re.search(r'[()+−*/×÷^_]|[α-ωΑ-Ω]', right)):
                continue
        if not relation and not any(len(n) == 1 or any('α' <= c <= 'ω' for c in n) for n in names):
            continue
        yield match.start(), match.end()


def math_values(text):
    return [text[a:b] for a, b in math_ranges(text)]


def unchanged_math(source, translated):
    """For saved/manual text without vector markers, require each formula intact."""
    normalize = lambda value: re.sub(r"\s+", "", value)
    from collections import Counter
    expected = Counter(normalize(v) for v in math_values(source))
    actual = Counter(normalize(v) for v in math_values(translated))
    return all(actual[value] >= count for value, count in expected.items())
