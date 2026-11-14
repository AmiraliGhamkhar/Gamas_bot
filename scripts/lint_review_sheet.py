#!/usr/bin/env python3
"""Lint a review-sheet HTML built from the template. Usage: python lint_review_sheet.py file.html [expected_minutes]"""
import re, sys

def main(path, expected=None):
    s = open(path, encoding="utf-8").read()
    errs, warns = [], []

    secs = re.findall(r'<section class="sec[^"]*" id="(s\d+)" data-t="(\d+)" data-n="([^"]+)"', s)
    body = re.findall(r'<section class="sec.*?</section>', s, flags=re.S)
    if not secs:
        errs.append("no <section class='sec'> found")
    # ids sequential
    ids = [i for i, _, _ in secs]
    if ids != [f"s{k}" for k in range(1, len(ids) + 1)]:
        errs.append(f"section ids not sequential: {ids}")
    # time budget
    total = sum(int(t) for _, t, _ in secs)
    if expected and total != int(expected):
        errs.append(f"sum(data-t)={total} != expected {expected}")
    print(f"sections={len(secs)} sum_minutes={total}")
    # quiz section last
    if body and 'class="sec quiz"' not in body[-1].split(">")[0] + ">":
        warns.append("last section lacks class 'quiz' (timeline color override)")
    if body and "<details" not in body[-1]:
        errs.append("last section has no <details> self-test")
    for (i, t, n), b in zip(secs, body):
        if 'class="done"' not in b:
            errs.append(f"{i}: missing .done button")
        if f"⏱ {int(t):d}" not in b.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")):
            warns.append(f"{i}: <small> minutes text differs from data-t")
        if "--c:" not in b.split(">")[0] or "--cs:" not in b.split(">")[0]:
            errs.append(f"{i}: missing --c/--cs inline vars")
        if not re.search(r'class="(?:tw|g2|card|mono|zone|spec|lad|fig|key|trap|tip|details)', b) and "<details" not in b:
            warns.append(f"{i}: prose only, no structured component")
    # Latin outside <e>
    t = re.sub(r"<style.*?</style>|<script.*?</script>|<svg.*?</svg>", "", s, flags=re.S)
    t = re.sub(r"<e>.*?</e>", "", t, flags=re.S)
    t = re.sub(r'<span class="eq">.*?</span>', "", t, flags=re.S)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"https?://\S+", "", t)
    latin = sorted(set(re.findall(r"[A-Za-z][A-Za-z0-9\-]{1,}", t)))
    if latin:
        warns.append(f"Latin text outside <e>: {latin[:15]}")
    # Latin digits in prose
    digs = sorted(set(re.findall(r"[0-9]+", t)))
    if digs:
        warns.append(f"Latin digits outside <e> (use Persian digits): {digs[:15]}")
    # hard-coded colors in sections
    for (i, _, _), b in zip(secs, body):
        hard = re.findall(r"#[0-9a-fA-F]{3,6}", re.sub(r'class="sw"[^>]*>', "", b))
        hard = [h for h in hard if True]
        sw = re.findall(r'class="sw" style="[^"]*"', b)
        if len(hard) > len(sw) * 2:
            warns.append(f"{i}: hard-coded hex colors outside .sw swatches")
    # tags balance
    for tag in ("section", "div", "table", "details", "ul", "span"):
        o = len(re.findall(rf"<{tag}[\s>]", s)); c = len(re.findall(rf"</{tag}>", s))
        if o != c:
            errs.append(f"unbalanced <{tag}>: open={o} close={c}")
    if "{{" in s:
        errs.append("unreplaced {{placeholder}} left")
    for w in warns: print("WARN ", w)
    for e in errs: print("ERROR", e)
    print("RESULT:", "FAIL" if errs else "PASS")
    sys.exit(1 if errs else 0)

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
