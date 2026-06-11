#!/usr/bin/env python3
"""Guard: fail if any em dash (U+2014) or en dash (U+2013) appears in the
site's text files. Only normal hyphens "-" are allowed. Run locally or in CI:

    python check_dashes.py

Exits non-zero and lists every offending file:line:col if a fancy dash is found.
"""
import glob
import sys

BAD = {"—": "em dash (—)", "–": "en dash (–)"}
PATTERNS = ("*.html", "*.md", "*.js", "*.css")

offenders = []
for pat in PATTERNS:
    for path in sorted(glob.glob(pat)):
        with open(path, encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                for ch, label in BAD.items():
                    col = line.find(ch)
                    if col != -1:
                        offenders.append((path, lineno, col + 1, label, line.strip()[:80]))

if offenders:
    print("Forbidden dashes found (use a normal hyphen '-' instead):\n")
    for path, lineno, col, label, snippet in offenders:
        print(f"  {path}:{lineno}:{col}  {label}  ->  {snippet}")
    print(f"\n{len(offenders)} occurrence(s). No em/en dashes are permitted.")
    sys.exit(1)

print("OK - no em/en dashes found.")
