#!/usr/bin/env python3
"""
patch_branding.py - strip Zammad branding.

Exit codes: 0 ok, 3 validation failed (nothing written unless --partial), 1 usage/IO error.
"""
import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

SVG_NS = 'http://www.w3.org/2000/svg'

# --------------------------------------------------------------------------- #
# Scan configuration
# --------------------------------------------------------------------------- #
SKIP_DIRS = {'node_modules', 'builds', '.git', '__tests__', '__mocks__', 'tmp', 'log', 'vendor',
             'spec', 'test', 'tests', 'stories'}
SKIP_FILE = re.compile(r'(\.(orig|rej|bak|map|snap)$|\.(spec|test|stories|mocks)\.[a-z]+$)')
LEGACY_JS = 'app/assets/javascripts'
VUE_SRC = 'app/frontend'

# The Zammad brand colours - used to detect Zammad artwork.
BRAND = re.compile(r'#(?:CA2317|E84F83|E54011)\b', re.I)

# Stand-alone logo files are recognised by NAME (logo.svg, logo-flat.svg, full-logo.svg,
# logotype.svg, ...) inside the directories below. Third-party integration logos
# (github-logo, i-doit-logo-*, ...) do not match because the name must start with it.
LOGO_NAME = re.compile(r'^(?:full-)?logo(?:type|-flat)?(?:[-_].*)?\.svg$', re.I)
LOGO_DIRS = ('app/frontend', 'public/assets/images')
THIRD_PARTY_LOGO = re.compile(r'github|gitlab|i-doit|slack|google|microsoft|facebook|twitter|telegram|whatsapp|sipgate|clearbit|zendesk|freshdesk|kayako|otrs|sms', re.I)


def is_zammad_logo(fn, text):
    """Logo by name (logo.svg, logo-flat.svg, full-logo.svg, ...), or any *logo* file in Zammad's red."""
    if LOGO_NAME.match(fn):
        return True
    return 'logo' in fn.lower() and not THIRD_PARTY_LOGO.search(fn) and bool(BRAND.search(text))

# Sprite symbols that are Zammad logos.
SPRITE = 'public/assets/images/icons.svg'
SPRITE_LOGO_IDS = re.compile(r'^icon-(?:logo|full-logo|logotype|logo-flat|zammad[\w-]*)$')

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param',
        'source', 'track', 'wbr'}

# Audit markers (applied to the planned end state of all legacy + Vue sources)
A_POWERED = re.compile(r'''class\s*=\s*["'][^"']*\bpoweredBy\b|\$t\(\s*['"]Powered by['"]|@T\(\s*['"]Powered by['"]''')
A_PROMO_LINK = re.compile(r'''(?:href|link)\s*=\s*["']https?://(?:www\.)?zammad\.(?:org|com)/?["']''')
A_TRANSL = re.compile(r'translations\.zammad\.org|You can help translating|Did you know\? You can help')
A_TRANSL_ALLOW = re.compile(r'(?:^|/)useLocaleUpdate\.ts$')   # an unused constant, never rendered
A_JOBS = re.compile(r'zammad\.com/jobs|current job openings')


# --------------------------------------------------------------------------- #
# Tiny tag tokenizer / element pairing (HTML, .eco and Vue templates)
# --------------------------------------------------------------------------- #
TAG_RE = re.compile(
    r'<!--.*?-->|<%.*?%>|<(/?)([A-Za-z][\w.:-]*)((?:"[^"]*"|\'[^\']*\'|[^\'">])*)>', re.S)


class Tok:
    __slots__ = ('start', 'end', 'name', 'closing', 'selfclose', 'raw')

    def __init__(self, start, end, name, closing, selfclose, raw):
        self.start, self.end, self.name = start, end, name
        self.closing, self.selfclose, self.raw = closing, selfclose, raw


def tokenize(text, lo=0, hi=None):
    toks = []
    for m in TAG_RE.finditer(text, lo, len(text) if hi is None else hi):
        if m.group(2) is None:        # comment or <% eco code %>
            continue
        raw = m.group(3) or ''
        toks.append(Tok(m.start(), m.end(), m.group(2), bool(m.group(1)),
                        raw.rstrip().endswith('/'), raw))
    return toks


def pair_elements(toks):
    stack, pairs = [], []
    for t in toks:
        if t.name.lower() in VOID or t.selfclose:
            if not t.closing:
                pairs.append((t, t))
            continue
        if not t.closing:
            stack.append(t)
            continue
        for i in range(len(stack) - 1, -1, -1):
            if stack[i].name == t.name:
                pairs.append((stack[i], t))
                del stack[i:]
                break
    return pairs


def ancestors(pairs, pos):
    """Elements containing pos, innermost first."""
    found = [p for p in pairs if p[0] is not p[1] and p[0].start <= pos < p[1].end]
    found.sort(key=lambda p: p[1].end - p[0].start)
    return found


def remove_span(text, start, end):
    """Delete [start,end) plus the whole line if the element was alone on it."""
    s = start
    while s > 0 and text[s - 1] in ' \t':
        s -= 1
    line_start = (s == 0 or text[s - 1] == '\n')
    e = end
    while e < len(text) and text[e] in ' \t':
        e += 1
    if line_start and (e >= len(text) or text[e] == '\n'):
        return text[:s] + text[e + 1:]
    return text[:start] + text[end:]


def vue_region(text):
    m = re.search(r'^<template\b[^>]*>', text, re.M)
    if not m:
        return None
    closes = [c for c in re.finditer(r'^</template>', text, re.M)]
    if not closes:
        return None
    return m.end(), closes[-1].start()


# --------------------------------------------------------------------------- #
# Rules: which element to delete for a given marker
# --------------------------------------------------------------------------- #
def choose_powered_div(anc, text):
    for o, c in anc:
        if o.name == 'div' and re.search(r'\bpoweredBy\b', o.raw):
            return o, c
    return None


def choose_footer(anc, text):
    for o, c in anc:
        if o.name == 'footer':
            return o, c
    return None


def choose_p_or_helpslot(anc, text):
    for o, c in anc:
        if o.name == 'p' or (o.name == 'template' and re.search(r'#help|v-slot', o.raw)):
            return o, c
    return None


def choose_translators_eco(anc, text):
    """Nearest <p>; if its parent <div> then only holds a heading, drop the parent."""
    for i, (o, c) in enumerate(anc):
        if o.name != 'p':
            continue
        if i + 1 < len(anc):
            po, pc = anc[i + 1]
            if po.name == 'div':
                rest = text[po.end:o.start] + text[c.end:pc.start]
                rest = re.sub(r'\s+', '', rest)
                if re.fullmatch(r'<h[1-6][^>]*>.*?</h[1-6]>', rest, re.S):
                    return po, pc
        return o, c
    return None


def fix_empty_vue_template(text):
    """If removing the footer left <template> empty, keep a harmless hidden node."""
    return re.sub(r'^<template>\s*</template>',
                  '<template>\n  <div class="hidden" aria-hidden="true"></div>\n</template>',
                  text, count=1, flags=re.M)


RULES = [
    dict(name='poweredBy block (legacy .eco views)', base=LEGACY_JS, exts=('.eco',),
         marker=re.compile(r'''class\s*=\s*["'][^"']*\bpoweredBy\b'''), choose=choose_powered_div),
    dict(name='"Powered by" footer (Vue apps)', base=VUE_SRC, exts=('.vue',),
         marker=re.compile(r'''\$t\(\s*['"]Powered by['"]\s*\)'''), choose=choose_footer,
         region=vue_region, post=fix_empty_vue_template),
    dict(name='translators-wanted ad (legacy .eco)', base=LEGACY_JS, exts=('.eco',),
         marker=re.compile(r'translations\.zammad\.org'), choose=choose_translators_eco),
    dict(name='translators-wanted ad (Vue apps)', base=VUE_SRC, exts=('.vue',),
         marker=re.compile(r'You can help translating'), choose=choose_p_or_helpslot,
         region=vue_region),
]


def apply_rule(rule, text):
    """-> (new_text, ok). ok=False means the marker is still present afterwards."""
    for _ in range(50):
        m = rule['marker'].search(text)
        if not m:
            return text, True
        region = rule.get('region')
        lo, hi = (0, len(text))
        if region:
            r = region(text)
            if not r:
                return text, False
            lo, hi = r
        if not (lo <= m.start() < hi):
            return text, False
        pairs = pair_elements(tokenize(text, lo, hi))
        el = rule['choose'](ancestors(pairs, m.start()), text)
        if not el:
            return text, False
        text = remove_span(text, el[0].start, el[1].end)
        if rule.get('post'):
            text = rule['post'](text)
    return text, False


def coffee_remove_contribution_alert(text):
    """Remove the jQuery chain that builds the 'js-contribution-alert' translators ad."""
    if "js-contribution-alert" not in text or 'translations.zammad.org' not in text:
        return text, True
    lines = text.split('\n')
    idx = next((i for i, l in enumerate(lines) if ".addClass('js-contribution-alert')" in l), None)
    if idx is None:
        return text, False
    s = idx
    while s > 0 and lines[s].lstrip().startswith('.'):
        s -= 1
    indent = len(lines[s]) - len(lines[s].lstrip())
    e = idx
    while (e + 1 < len(lines) and lines[e + 1].strip()
           and len(lines[e + 1]) - len(lines[e + 1].lstrip()) > indent):
        e += 1
    chain = '\n'.join(lines[s:e + 1])
    if 'translations.zammad.org' not in chain:
        return text, False
    del lines[s:e + 1]
    return '\n'.join(lines), True


# --------------------------------------------------------------------------- #
# Logos
# --------------------------------------------------------------------------- #
def load_wordmark(path):
    src = open(path, encoding='utf-8').read()
    ET.fromstring(src)   # must be well formed
    m = re.search(r'<svg\b([^>]*)>(.*)</svg>', src, re.S)
    vb = re.search(r'viewBox="\s*([-\d.]+)[ ,]+([-\d.]+)[ ,]+([-\d.]+)[ ,]+([-\d.]+)\s*"', m.group(1))
    w, h = float(vb.group(3)), float(vb.group(4))
    inner = m.group(2).strip()
    sig = re.search(r'\bd="([^"]{40})', inner)
    return dict(w=w, h=h, inner=inner, sig=sig.group(1) if sig else inner[:40])


def load_logos(data_dir):
    wm = load_wordmark(os.path.join(data_dir, 'logo-wordmark.svg'))
    wm['sigs'] = [wm['sig']]
    wm['mark'] = None
    mark_path = os.path.join(data_dir, 'logo-mark.svg')   # optional square mark for icon-sized slots
    if os.path.isfile(mark_path):
        wm['mark'] = load_wordmark(mark_path)
        wm['sigs'].append(wm['mark']['sig'])
    return wm


def square_viewbox(wm):
    side = max(wm['w'], wm['h'])
    return '%s %s %s %s' % (0, -round((side - wm['h']) / 2, 3), wm['w'], side)


def pick(wm, orig_vb):
    """-> (viewBox, inner svg) for a slot whose original viewBox is orig_vb.
    Wide slots get the wordmark. Square-ish slots (favicon-like icons, avatars, nav icons) get
    data/logo-mark.svg if the company supplies a square mark, else the wordmark centred on a square."""
    try:
        parts = [float(x) for x in re.split(r'[ ,]+', orig_vb.strip())]
        aspect = parts[2] / parts[3]
    except Exception:
        aspect = 3
    if aspect < 2:
        mark = wm.get('mark')
        if mark:
            return '0 0 %s %s' % (mark['w'], mark['h']), mark['inner']
        return square_viewbox(wm), wm['inner']
    return '0 0 %s %s' % (wm['w'], wm['h']), wm['inner']


def make_logo_svg(wm, orig):
    vb = re.search(r'viewBox="([^"]+)"', orig)
    new_vb, inner = pick(wm, vb.group(1) if vb else '0 0 120 37')
    wh = ''
    for attr in ('width', 'height'):
        a = re.search(r'<svg\b[^>]*\s%s="([^"]+)"' % attr, orig)
        if a:
            wh += ' %s="%s"' % (attr, a.group(1))
    return ('<svg xmlns="http://www.w3.org/2000/svg"%s viewBox="%s">'
            '<g fill="currentColor">%s</g></svg>\n') % (wh, new_vb, wm['inner'])


SYMBOL_RE = re.compile(r'<symbol\b(?P<attrs>[^>]*)>(?P<body>.*?)</symbol>', re.S)


def patch_sprite(text, wm):
    """Replace Zammad logo <symbol>s by regex on the raw text. The output stays in the SVG
    namespace (the old XSLT emitted xmlns="" which made the symbols render as nothing)."""
    replaced = []

    def sub(m):
        sid = re.search(r'\bid="([^"]+)"', m.group('attrs'))
        if not sid:
            return m.group(0)
        sid = sid.group(1)
        if not (SPRITE_LOGO_IDS.match(sid)
                or (BRAND.search(m.group('body')) and 'logo' in sid
                    and not re.search(r'github|gitlab', sid))):
            return m.group(0)
        vb = re.search(r'viewBox="([^"]+)"', m.group('attrs'))
        new_vb, inner = pick(wm, vb.group(1) if vb else '0 0 120 37')
        new = '<symbol id="%s" viewBox="%s"><g fill="currentColor">%s</g></symbol>' % (sid, new_vb, inner)
        replaced.append(sid)
        return new

    return SYMBOL_RE.sub(sub, text), replaced


# --------------------------------------------------------------------------- #
# Virtual tree (disk + planned changes)
# --------------------------------------------------------------------------- #
class Tree:
    def __init__(self, root):
        self.root = root
        self.changes = {}     # rel -> bytes (planned content)
        self.created = set()

    def p(self, rel):
        return os.path.join(self.root, rel)

    def exists(self, rel):
        return rel in self.changes or os.path.isfile(self.p(rel))

    def read_bytes(self, rel):
        if rel in self.changes:
            return self.changes[rel]
        with open(self.p(rel), 'rb') as f:
            return f.read()

    def read(self, rel):
        return self.read_bytes(rel).decode('utf-8', 'surrogateescape')

    def put(self, rel, data):
        if isinstance(data, str):
            data = data.encode('utf-8', 'surrogateescape')
        if not os.path.isfile(self.p(rel)):
            self.created.add(rel)
        elif self.read_disk(rel) == data:
            self.changes.pop(rel, None)
            return False
        self.changes[rel] = data
        return True

    def read_disk(self, rel):
        with open(self.p(rel), 'rb') as f:
            return f.read()

    def walk(self, base, exts=None, include_skipped=False):
        top = self.p(base)
        for d, dirs, files in os.walk(top):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            for fn in files:
                if SKIP_FILE.search(fn) and not include_skipped:
                    continue
                if exts and not fn.endswith(exts):
                    continue
                yield os.path.relpath(os.path.join(d, fn), self.root).replace(os.sep, '/')


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self):
        self.lines, self.failures, self.warnings = [], [], []

    def info(self, s):
        self.lines.append(s)
        print(s)

    def fail(self, s):
        self.failures.append(s)
        print('  [FAIL] ' + s)

    def warn(self, s):
        self.warnings.append(s)
        print('  [WARN] ' + s)


def plan_markup(tree, rep):
    rep.info('== Markup patches (powered-by, translators-wanted)')
    counts = {r['name']: [0, 0] for r in RULES}   # [patched files, failed files]
    counts['translators-wanted ad (legacy .coffee)'] = [0, 0]
    for base in (LEGACY_JS, VUE_SRC):
        for rel in tree.walk(base, ('.eco', '.vue', '.coffee')):
            text = tree.read(rel)
            new = text
            for rule in RULES:
                if rel.startswith(rule['base']) and rel.endswith(rule['exts']) and rule['marker'].search(new):
                    new, ok = apply_rule(rule, new)
                    if not ok:
                        counts[rule['name']][1] += 1
                        rep.fail('%s: marker still present in %s (structure changed? needs a new rule)'
                                 % (rule['name'], rel))
                    else:
                        counts[rule['name']][0] += 1
            if rel.endswith('.coffee') and rel.startswith(LEGACY_JS):
                new2, ok = coffee_remove_contribution_alert(new)
                if not ok:
                    counts['translators-wanted ad (legacy .coffee)'][1] += 1
                    rep.fail('translators-wanted ad (legacy .coffee): could not remove chain in ' + rel)
                elif new2 != new:
                    counts['translators-wanted ad (legacy .coffee)'][0] += 1
                new = new2
            if new != text:
                tree.put(rel, new)
                rep.info('  [patch] ' + rel)
    for name, (ok, bad) in counts.items():
        rep.info('  %-45s patched=%d failed=%d' % (name, ok, bad))


def plan_banner(tree, rep, data_dir):
    rep.info('== JavaScript-console ASCII banner')
    stub_path = os.path.join(data_dir, 'hello_banner.coffee')
    stub = open(stub_path, 'rb').read()
    hits = [r for r in tree.walk(LEGACY_JS, ('.coffee',)) if os.path.basename(r) == 'hello_banner.coffee']
    for rel in hits:
        if b'console.log' in tree.read_bytes(rel):
            if tree.put(rel, stub):
                rep.info('  [patch] ' + rel)
        else:
            rep.info('  [clean] ' + rel)
    if not hits:
        rep.info('  hello_banner.coffee no longer exists (audit below still scans for the banner text)')


def plan_logos(tree, rep, wm):
    rep.info('== Stand-alone logo SVGs (discovered by name, not by path)')
    seen = 0
    for base in LOGO_DIRS:
        for rel in tree.walk(base, ('.svg',)):
            fn = os.path.basename(rel)
            if rel == SPRITE:
                continue
            orig = tree.read(rel)
            if not is_zammad_logo(fn, orig):
                continue
            seen += 1
            if tree.put(rel, make_logo_svg(wm, orig)):
                rep.info('  [patch] ' + rel)
            else:
                rep.info('  [clean] ' + rel)
    if not seen:
        rep.fail('no stand-alone logo SVG found at all - Zammad layout changed?')


def plan_sprite(tree, rep, wm):
    rep.info('== Icon sprite')
    if not tree.exists(SPRITE):
        rep.fail('%s not found' % SPRITE)
        return
    orig = tree.read(SPRITE)
    new, replaced = patch_sprite(orig, wm)
    if not replaced:
        rep.fail('no Zammad logo <symbol> found in sprite (ids changed?)')
        return
    rep.info('  symbols handled: ' + ', '.join(replaced))
    if tree.put(SPRITE, new):
        rep.info('  [patch] ' + SPRITE)
    else:
        rep.info('  [clean] ' + SPRITE)


def icon_svg(wm, size):
    """Square white canvas with the logo centred (the square mark if one is supplied)."""
    src = wm['mark'] or wm
    pad = 4 if size <= 64 else 10
    box = 100 - 2 * pad
    sc = min(box / src['w'], box / src['h'])
    ww, hh = src['w'] * sc, src['h'] * sc
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<rect width="100" height="100" fill="#fff"/>'
            '<svg x="%.3f" y="%.3f" width="%.3f" height="%.3f" viewBox="0 0 %s %s">%s</svg></svg>') % (
        (100 - ww) / 2, (100 - hh) / 2, ww, hh, src['w'], src['h'], src['inner'])


def rasterize(exe, svg, size):
    r = subprocess.run([exe, '-w', str(size), '-h', str(size), '-f', 'png'],
                       input=svg.encode('utf-8'), capture_output=True)
    if r.returncode != 0 or not r.stdout.startswith(b'\x89PNG'):
        raise RuntimeError('rsvg-convert failed: ' + r.stderr.decode('utf-8', 'replace')[:300])
    return r.stdout


def build_ico(pngs):
    """PNG-compressed ICO. pngs: list of (size, png_bytes)."""
    head = struct.pack('<HHH', 0, 1, len(pngs))
    off = 6 + 16 * len(pngs)
    entries, blob = b'', b''
    for size, data in pngs:
        entries += struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(data), off + len(blob))
        blob += data
    return head + entries + blob


def plan_icons(tree, rep, wm):
    rep.info('== Raster icons (favicon, apple-touch-icon, PWA app icons), rendered from the logo')
    targets = [r for r in ('public/favicon.ico', 'public/apple-touch-icon.png') if tree.exists(r)]
    targets += sorted(r for r in tree.walk('public/assets/frontend', ('.png',))
                      if re.match(r'app-icon-\d+\.png$', os.path.basename(r)))
    if not targets:
        rep.fail('no favicon / app icon found - Zammad layout changed?')
        return
    exe = shutil.which('rsvg-convert')
    if not exe:
        rep.fail('rsvg-convert not found (apt install librsvg2-bin) - needed to render the icons')
        return
    for rel in targets:
        cur = tree.read_disk(rel) if os.path.isfile(tree.p(rel)) else b''
        try:
            if rel.endswith('.ico'):
                try:
                    n = struct.unpack('<H', cur[4:6])[0]
                    sizes = sorted({(cur[6 + 16 * i] or 256) for i in range(n)})
                except Exception:
                    sizes = []
                sizes = sizes or [16, 32, 48, 64]
                new = build_ico([(sz, rasterize(exe, icon_svg(wm, sz), sz)) for sz in sizes])
            else:
                w, h = struct.unpack('>II', cur[16:24])
                if w != h:
                    rep.warn('%s is not square (%dx%d), left untouched' % (rel, w, h))
                    continue
                new = rasterize(exe, icon_svg(wm, w), w)
        except Exception as e:
            rep.fail('%s: %s' % (rel, e))
            continue
        rep.info(('  [patch] ' if tree.put(rel, new) else '  [clean] ') + rel)


def error_svg(wm, orig, name):
    """Generic error artwork: white card with the logo and 'Error NNN', readable on light and dark pages."""
    vb = re.search(r'viewBox="\s*[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)', orig)
    W, H = (float(vb.group(1)), float(vb.group(2))) if vb else (750.0, 650.0)
    code = re.search(r'error-(\d{3})\.svg$', name)
    label = 'Error %s' % code.group(1) if code else 'Error'
    cw = W * 0.8
    lw = cw * 0.78
    lh = lw * wm['h'] / wm['w']
    pad = W * 0.05
    fs = W * 0.07
    ch = pad + lh + fs * 2.6
    cy = (H - ch) / 2
    ly = cy + pad
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %s %s">'
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="%.2f" fill="#fff"/>'
            '<svg x="%.2f" y="%.2f" width="%.2f" height="%.2f" viewBox="0 0 %s %s">%s</svg>'
            '<text x="%.2f" y="%.2f" text-anchor="middle" font-family="Helvetica, Arial, sans-serif" '
            'font-size="%.2f" fill="#6b7378">%s</text></svg>\n') % (
        ('%g' % W), ('%g' % H), (W - cw) / 2, cy, cw, ch, W * 0.02,
        (W - lw) / 2, ly, lw, lh, wm['w'], wm['h'], wm['inner'],
        W / 2, ly + lh + fs * 1.7, fs, label)


def plan_error_art(tree, rep, wm):
    rep.info('== Error-page artwork (public/assets/error/*.svg, discovered by scanning)')
    n = 0
    for rel in tree.walk('public/assets/error', ('.svg',)):
        n += 1
        new = error_svg(wm, tree.read(rel), os.path.basename(rel))
        rep.info(('  [patch] ' if tree.put(rel, new) else '  [clean] ') + rel)
    if not n:
        rep.fail('no error artwork found in public/assets/error')


def badge_svg(wm, orig):
    """Logo on a white rounded card, same outer size as the illustration it replaces."""
    vb = re.search(r'viewBox="\s*[-\d.]+[ ,]+[-\d.]+[ ,]+([\d.]+)[ ,]+([\d.]+)', orig)
    wh = [re.search(r'<svg\b[^>]*\s%s="([\d.]+)' % a, orig) for a in ('width', 'height')]
    if vb:
        W, H = float(vb.group(1)), float(vb.group(2))
    elif all(wh):
        W, H = float(wh[0].group(1)), float(wh[1].group(1))
    else:
        W, H = 600.0, 170.0
    aspect = wm['w'] / wm['h']
    pad = H * 0.1
    lh = min(H - 2 * pad, (W * 0.8 - 2 * pad) / aspect)
    lw = lh * aspect
    cw, ch = lw + 2 * pad, lh + 2 * pad
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%g" height="%g" viewBox="0 0 %g %g">'
            '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" rx="%.2f" fill="#fff"/>'
            '<svg x="%.2f" y="%.2f" width="%.2f" height="%.2f" viewBox="0 0 %s %s">%s</svg></svg>\n') % (
        W, H, W, H, (W - cw) / 2, (H - ch) / 2, cw, ch, pad * 0.5,
        (W - lw) / 2, (H - lh) / 2, lw, lh, wm['w'], wm['h'], wm['inner'])


def plan_illustrations(tree, rep, wm):
    rep.info('== Zammad-coloured illustrations (BETA UI dialog banner, ...), discovered by name + colour')
    n = 0
    for rel in tree.walk(VUE_SRC, ('.svg',)):
        text = tree.read(rel)
        fn = os.path.basename(rel).lower()
        if 'illustration' in fn and (BRAND.search(text) or wm['sig'] in text):
            n += 1
            rep.info(('  [patch] ' if tree.put(rel, badge_svg(wm, text)) else '  [clean] ') + rel)
    if not n:
        rep.info('  none found')


PHOENIX = re.compile(r'\bthe\s+Phoenix\s+is\s+not\s+able\b')


def plan_error_text(tree, rep):
    rep.info('== Error-page wording (public/*.html)')
    for fn in sorted(os.listdir(tree.p('public'))):
        if not fn.endswith('.html'):
            continue
        rel = 'public/' + fn
        text = tree.read(rel)
        new = PHOENIX.sub('we are not able', text)
        if new != text:
            tree.put(rel, new)
            rep.info('  [patch] ' + rel)


# --------------------------------------------------------------------------- #
# Validation of planned content (before anything is written)
# --------------------------------------------------------------------------- #
def validate(tree, rep, root, data_dir, tools_root):
    rep.info('== Validating planned changes')
    by_kind = {'eco': [], 'coffee': [], 'vue': []}
    for rel, data in tree.changes.items():
        if rel.endswith('.svg'):
            try:
                ET.fromstring(data)
            except ET.ParseError as e:
                rep.fail('invalid SVG produced for %s: %s' % (rel, e))
                continue
            if rel == SPRITE:
                check_sprite(data.decode('utf-8'), rep)
        elif rel.endswith('.eco'):
            by_kind['eco'].append(rel)
        elif rel.endswith('.coffee'):
            by_kind['coffee'].append(rel)
        elif rel.endswith('.vue'):
            by_kind['vue'].append(rel)
    rep.info('  svg: ok' if not rep.failures else '  svg: see failures')

    tmp = tempfile.mkdtemp(prefix='branding-validate-')
    os.chmod(tmp, 0o755)
    try:
        staged = {}
        for kind, rels in by_kind.items():
            for i, rel in enumerate(rels):
                ext = os.path.splitext(rel)[1]
                path = os.path.join(tmp, '%s_%d%s' % (kind, i, ext))
                with open(path, 'wb') as f:
                    f.write(tree.changes[rel])
                os.chmod(path, 0o644)
                staged[path] = rel
        ruby_files = [p for p in staged if p.endswith(('.eco', '.coffee'))]
        vue_files = [p for p in staged if p.endswith('.vue')]
        zammad = shutil.which('zammad')
        if ruby_files:
            if zammad:
                r = subprocess.run([zammad, 'run', 'bundle', 'exec', 'ruby',
                                    os.path.join(data_dir, 'validate_eco_coffee.rb')] + ruby_files,
                                   capture_output=True, text=True)
                report_validator(r, staged, rep, 'eco/coffee compile')
            else:
                rep.warn('`zammad` command not found - skipped eco/coffee compile check')
        if vue_files:
            node = shutil.which('node') or os.path.join(tools_root, '.heroku/node/bin/node')
            if os.path.exists(node):
                env = dict(os.environ, ZAMMAD_ROOT=tools_root)
                r = subprocess.run([node, os.path.join(data_dir, 'validate_vue.cjs')] + vue_files,
                                   capture_output=True, text=True, env=env, cwd=tools_root)
                report_validator(r, staged, rep, 'vue parse+template compile')
            else:
                rep.warn('node not found - skipped Vue compile check')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def report_validator(r, staged, rep, label):
    out = (r.stdout or '') + (r.stderr or '')
    for path, rel in staged.items():
        out = out.replace(path, rel)
    if r.returncode == 0:
        rep.info('  %s: ok' % label)
    else:
        rep.fail('%s failed:\n    %s' % (label, out.strip().replace('\n', '\n    ')[:2000]))


def check_sprite(text, rep):
    ns = '{%s}' % SVG_NS
    root = ET.fromstring(text)
    syms = {s.get('id'): s for s in root.iter(ns + 'symbol')}
    for sid in list(syms):
        if SPRITE_LOGO_IDS.match(sid or ''):
            s = syms[sid]
            if not s.get('viewBox') or not any(e.tag != ns + 'title' and e is not s for e in s.iter()):
                rep.fail('sprite symbol %s would render blank' % sid)
    stray = [e.tag for e in root.iter() if e.tag == 'symbol']
    if stray:
        rep.fail('sprite contains <symbol> elements outside the SVG namespace (would be invisible)')


# --------------------------------------------------------------------------- #
# Audit of the planned end state
# --------------------------------------------------------------------------- #
def audit(tree, rep, wm, strict=True):
    rep.info('== Audit (anything left of Zammad branding in the planned end state)')
    nfound = 0
    for base, exts in ((LEGACY_JS, ('.eco', '.coffee', '.js', '.ts')),
                       (VUE_SRC, ('.vue', '.ts', '.js'))):
        for rel in tree.walk(base, exts):
            if rel.endswith('.min.js'):
                continue
            text = tree.read(rel)
            for label, rx in (('powered-by', A_POWERED), ('promo link', A_PROMO_LINK),
                              ('translators ad', A_TRANSL), ('careers banner', A_JOBS)):
                m = rx.search(text)
                if not m:
                    continue
                if label == 'translators ad' and A_TRANSL_ALLOW.search(rel):
                    continue
                nfound += 1
                line = text.count('\n', 0, m.start()) + 1
                rep.fail('%s still present: %s:%d' % (label, rel, line))
    # logo files that are still Zammad's
    for base in LOGO_DIRS:
        for rel in tree.walk(base, ('.svg',)):
            fn = os.path.basename(rel)
            text = tree.read(rel)
            if rel != SPRITE and is_zammad_logo(fn, text) and not any(sg in text for sg in wm['sigs']):
                rep.fail('logo file is not the company logo: ' + rel)
            elif rel != SPRITE and BRAND.search(text) and not is_zammad_logo(fn, text) and 'illustration' not in fn.lower():
                rep.warn('Zammad-coloured artwork (not a logo by name), left untouched: ' + rel)
    for r in tree.walk(VUE_SRC, ('.svg',)):
        if 'illustration' in os.path.basename(r).lower() and BRAND.search(tree.read(r)):
            rep.fail('Zammad-coloured illustration still present: ' + r)
    for r in tree.walk('public/assets/error', ('.svg',)):
        if wm['sig'] not in tree.read(r):
            rep.fail('error artwork is not generated from the company logo: ' + r)
    for fn in sorted(os.listdir(tree.p('public'))):
        if fn.endswith('.html') and re.search(r'Phoenix', tree.read('public/' + fn)):
            nfound += 1
            rep.fail('"Phoenix" wording still present: public/' + fn)
    if tree.exists(SPRITE):
        for sm in SYMBOL_RE.finditer(tree.read(SPRITE)):
            sid = re.search(r'\bid="([^"]+)"', sm.group('attrs'))
            if sid and BRAND.search(sm.group('body')):
                rep.fail('sprite symbol still has Zammad brand colours: ' + sid.group(1))
    rep.info('  audit finished: %d source finding(s)' % nfound)


# --------------------------------------------------------------------------- #
# Write / backup / restore
# --------------------------------------------------------------------------- #
def write_changes(tree, rep, backup_dir):
    if not tree.changes:
        rep.info('== Nothing to write (already up to date)')
        return
    manifest = {'root': tree.root, 'modified': [], 'created': []}
    os.makedirs(os.path.join(backup_dir, 'files'), exist_ok=True)
    try:
        for rel, data in sorted(tree.changes.items()):
            path = tree.p(rel)
            if os.path.isfile(path):
                st = os.stat(path)
                bk = os.path.join(backup_dir, 'files', rel)
                os.makedirs(os.path.dirname(bk), exist_ok=True)
                shutil.copy2(path, bk)
                manifest['modified'].append(dict(rel=rel, uid=st.st_uid, gid=st.st_gid, mode=st.st_mode & 0o7777))
                uid, gid, mode = st.st_uid, st.st_gid, st.st_mode & 0o7777
            else:
                pst = os.stat(os.path.dirname(path))
                manifest['created'].append(rel)
                uid, gid, mode = pst.st_uid, pst.st_gid, 0o644
            tmp = path + '.branding-tmp'
            try:
                with open(tmp, 'wb') as f:
                    f.write(data)
                os.chmod(tmp, mode)
                try:
                    os.chown(tmp, uid, gid)
                except PermissionError:
                    pass
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
    finally:
        with open(os.path.join(backup_dir, 'manifest.json'), 'w') as f:
            json.dump(manifest, f, indent=1)
    rep.info('== Wrote %d file(s); originals saved in %s' % (len(tree.changes), backup_dir))


def restore(backup_dir):
    if not os.path.isfile(os.path.join(backup_dir, 'manifest.json')):
        print('  no source changes were recorded in %s - nothing to restore' % backup_dir)
        return
    with open(os.path.join(backup_dir, 'manifest.json')) as f:
        m = json.load(f)
    root = m['root']
    for ent in m['modified']:
        src = os.path.join(backup_dir, 'files', ent['rel'])
        dst = os.path.join(root, ent['rel'])
        shutil.copy2(src, dst)
        try:
            os.chown(dst, ent['uid'], ent['gid'])
        except PermissionError:
            pass
        print('  restored ' + ent['rel'])
    for rel in m['created']:
        p = os.path.join(root, rel)
        if os.path.exists(p):
            os.unlink(p)
            print('  removed  ' + rel)


# --------------------------------------------------------------------------- #
# Verify the BUILT assets (run after rake assets:precompile)
# --------------------------------------------------------------------------- #
def verify_build(root):
    import glob
    rep = Report()
    rep.info('== Verifying compiled assets')
    bad = []
    for mf in sorted(glob.glob(os.path.join(root, 'public/assets/.sprockets-manifest-*.json'))):
        data = json.load(open(mf))
        for name in ('application.js',):
            fn = data.get('assets', {}).get(name)
            if not fn:
                rep.fail('%s not in sprockets manifest' % name)
                continue
            path = os.path.join(root, 'public/assets', fn)
            if not os.path.isfile(path):
                rep.fail('compiled %s missing: %s' % (name, fn))
                continue
            txt = open(path, encoding='utf-8', errors='replace').read()
            for needle in ('poweredBy', 'translations.zammad.org', 'zammad.com/jobs'):
                if needle in txt:
                    rep.fail('compiled %s still contains "%s"' % (fn, needle))
            rep.info('  legacy bundle %s: checked' % fn)
    vm = os.path.join(root, 'public/assets/frontend/vite/.vite/manifest.json')
    if os.path.isfile(vm):
        data = json.load(open(vm))
        wanted = {'LayoutPublicPage.vue': 'Powered by', 'LoginFooter.vue': 'Powered by',
                  'AccountOverview.vue': 'help translating', 'PersonalSettingLocale.vue': 'help translating'}
        for key, ent in data.items():
            for suffix, needle in wanted.items():
                if key.endswith(suffix):
                    path = os.path.join(root, 'public/assets/frontend/vite', ent['file'])
                    txt = open(path, encoding='utf-8', errors='replace').read() if os.path.isfile(path) else ''
                    if needle in txt:
                        rep.fail('compiled Vue chunk %s still contains "%s"' % (ent['file'], needle))
                    else:
                        rep.info('  vue chunk %s: checked' % ent['file'])
    else:
        rep.warn('vite manifest not found: Vue bundle not checked')
    sp = os.path.join(root, SPRITE)
    try:
        check_sprite(open(sp, encoding='utf-8').read(), rep)
        rep.info('  sprite: ok')
    except Exception as e:
        rep.fail('sprite unreadable: %s' % e)
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--root', default='/opt/zammad')
    ap.add_argument('--data', default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument('--tools-root', default=None, help='Zammad install providing node/ruby gems for validation')
    ap.add_argument('--backup-dir')
    ap.add_argument('--dry-run', action='store_true', help='plan + validate + audit, write nothing')
    ap.add_argument('--partial', action='store_true', help='write what could be patched even if some rules failed')
    ap.add_argument('--restore', metavar='BACKUP_DIR')
    ap.add_argument('--verify-build', action='store_true')
    ap.add_argument('--no-validate', action='store_true')
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    tools_root = a.tools_root or (root if os.path.isdir(os.path.join(root, '.heroku')) else '/opt/zammad')

    if a.restore:
        restore(a.restore)
        return 0
    if a.verify_build:
        return 3 if verify_build(root).failures else 0
    if not os.path.isdir(os.path.join(root, 'app')):
        print('%s does not look like a Zammad tree' % root, file=sys.stderr)
        return 1

    rep = Report()
    wm = load_logos(a.data)
    tree = Tree(root)
    plan_markup(tree, rep)
    plan_banner(tree, rep, a.data)
    plan_logos(tree, rep, wm)
    plan_sprite(tree, rep, wm)
    plan_icons(tree, rep, wm)
    plan_error_art(tree, rep, wm)
    plan_illustrations(tree, rep, wm)
    plan_error_text(tree, rep)
    if not a.no_validate:
        validate(tree, rep, root, a.data, tools_root)
    audit(tree, rep, wm)

    print('\nSUMMARY: %d file(s) to change, %d failure(s), %d warning(s)'
          % (len(tree.changes), len(rep.failures), len(rep.warnings)))
    if a.dry_run:
        print('(dry run - nothing written)')
        return 3 if rep.failures else 0
    if rep.failures and not a.partial:
        print('ABORTING: nothing was written. Fix the failures above (or use --partial).')
        return 3
    if not a.backup_dir:
        print('--backup-dir is required for a real run', file=sys.stderr)
        return 1
    write_changes(tree, rep, a.backup_dir)
    return 3 if rep.failures else 0


if __name__ == '__main__':
    sys.exit(main())
