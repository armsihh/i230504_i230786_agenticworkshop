"""Undo common text obfuscation before running safety checks.

Attackers write "ign​ore", "ｉｇｎｏｒｅ", "i g n o r e" or "1gn0re" to slip past keyword filters.
normalize() folds all of these to plain "ignore". Use it on a COPY of the text for matching only;
never change what the customer actually wrote.
"""
import re
import unicodedata

_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁤﻿­]")
_SPACED_LETTERS = re.compile(r"\b(?:[A-Za-z][ .\-_*]){2,}[A-Za-z]\b")   # "i g n o r e", "g.u.a.r.d"
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)                 # full-width and styled letters -> ASCII
    text = _INVISIBLE.sub("", text)                            # zero-width and direction-override characters
    text = "".join(c if c.isprintable() or c in "\n\t" else " " for c in text)
    text = _SPACED_LETTERS.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group()), text)
    return re.sub(r"\s+", " ", text).strip()


def deleet(text: str) -> str:
    """'1gn0re' -> 'ignore'. Only for matching words, so digits in real numbers don't matter."""
    return re.sub(r"\b[\w@$!]+\b", lambda m: m.group().translate(_LEET) if re.search(r"[a-z]", m.group(), re.I)
                  else m.group(), text)
