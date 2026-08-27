# -*- coding: utf-8 -*-
"""Синтаксис всех страниц кабинета: JS внутри <script> и парность тегов."""
import io, os, re, sys, subprocess, glob
sys.stdout.reconfigure(encoding="utf-8")
import pathlib as _pl
ROOT = _pl.Path(__file__).resolve().parents[1]
WEB = str(ROOT / "web")

bad = []
for path in sorted(glob.glob(os.path.join(WEB, "*.html"))):
    src = io.open(path, encoding="utf-8").read()
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", src, re.S)
    for i, b in enumerate(blocks):
        tmp = os.path.join(os.environ["TEMP"], "velor_check.js")
        io.open(tmp, "w", encoding="utf-8").write(b)
        r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
        if r.returncode:
            bad.append((os.path.basename(path), i, r.stderr.strip().splitlines()[:4]))

    # грубая проверка парности главных контейнеров
    for tag in ("main", "header", "section", "form"):
        o = len(re.findall(r"<%s[\s>]" % tag, src))
        c = len(re.findall(r"</%s>" % tag, src))
        if o != c:
            bad.append((os.path.basename(path), tag, "открыто %d, закрыто %d" % (o, c)))

if bad:
    for b in bad:
        print("FAIL", b)
    sys.exit(1)
print("JS и разметка во всех страницах OK")
