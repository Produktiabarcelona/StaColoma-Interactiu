#!/usr/bin/env python3
"""
Genera el contingut de la web a partir de la presentació.

    python tools/build_assets.py

Entrades:
  Fondo.ppt (o Fondo.pptx)            -> textos reals, fotos originals, caselles i enllaços
  Fondo/Diapositiva1..11.PNG          -> només per a la portada (il·lustració) i les ones del fons
Sortides:
  assets/media/*.webp                 -> fotos i icones de la presentació (amb l'ombra ja aplicada)
  assets/sprites/*.webp               -> peces de la il·lustració de la portada
  assets/manifest.js                  -> tot el model (posicions, textos, perfils de les ones)

Quan canviï la presentació (p. ex. la foto que falta a "Espai públic"),
desa el PowerPoint i torna a executar aquest script.
Requereix: pip install numpy pillow scipy   ·   LibreOffice (per convertir .ppt a .pptx)
"""
import glob
import json
import os
import posixpath
import re
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageFilter
from scipy import ndimage as ndi

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'Fondo')
OUT = os.path.join(ROOT, 'assets')
SPR = os.path.join(OUT, 'sprites')
MED = os.path.join(OUT, 'media')
CACHE = os.path.join(ROOT, 'tools', 'cache')
W, H = 2560, 1440
EMU = 24384000 / W              # EMU per píxel (diapositiva de 2560 px)
STEP = 16                       # resolució dels perfils de les ones (px)

CREAM = np.array([249, 243, 228])
YEL = np.array([250, 190, 20])
PINK = np.array([232, 73, 106])
CYAN = np.array([31, 176, 221])
MAG = np.array([232, 62, 138])
NAVY = np.array([0, 49, 101])
CORAL = np.array([251, 90, 98])
FACE = np.array([248, 246, 229])

LOGO_ZONE = (40, 20, 520, 300)  # logo petit de dalt a l'esquerra

NS = {
    'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
    'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
    'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
}
RID = '{%s}id' % NS['r']
REMB = '{%s}embed' % NS['r']


# ============================================================ utilitats ====
def load(n):
    img = Image.open(os.path.join(SRC, f'Diapositiva{n}.PNG')).convert('RGB')
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)
    return np.asarray(img).astype(np.int16)


def cheb(a, c):
    """Distància de color (màxim per canal)."""
    return np.abs(a - c).max(-1)


def near(a, c, tol=30):
    return cheb(a, c) <= tol


def soft_alpha(d, lo=6, hi=40):
    return np.clip((d - lo) / float(hi - lo), 0, 1)


def bbox(mask, pad=4):
    ys, xs = np.nonzero(mask)
    return (int(max(0, xs.min() - pad)), int(max(0, ys.min() - pad)),
            int(min(W, xs.max() + 1 + pad)), int(min(H, ys.max() + 1 + pad)))


def matte(img, mask, palette):
    """
    Separa una forma de colors plans del fons crema sense deixar vora clara.
    Als píxels de la vora (barreja forma+crema) recupera el color pur i la
    transparència exacta, així no es veu cap halo en moure la peça.
    """
    pal = np.array(palette, float)
    C = img.astype(float)
    K = CREAM.astype(float)
    best_a = np.zeros(img.shape[:2])
    best_e = np.full(img.shape[:2], 1e9)
    best_c = np.zeros(img.shape)
    for F in pal:
        d = F - K
        a = np.clip(((C - K) * d).sum(-1) / (d * d).sum(), 0, 1)
        err = np.abs(C - (K + a[..., None] * d)).max(-1)
        better = err < best_e
        best_e[better], best_a[better], best_c[better] = err[better], a[better], F
    best_a[best_e > 45] = 0          # píxels d'un altre color (no pertanyen a la forma)
    rgb = np.where(mask[..., None], best_c, C)
    alpha = np.where(mask, best_a, 0.0)
    return rgb, alpha


def save_sprite(name, rgb, alpha, box):
    x0, y0, x1, y1 = box
    rgba = np.dstack([rgb[y0:y1, x0:x1].clip(0, 255).round().astype(np.uint8),
                      (alpha[y0:y1, x0:x1] * 255).round().astype(np.uint8)])
    Image.fromarray(rgba, 'RGBA').save(os.path.join(SPR, name + '.webp'),
                                       quality=92, alpha_quality=100, method=6)
    return {'src': f'assets/sprites/{name}.webp', 'x': int(x0), 'y': int(y0), 'w': int(x1 - x0), 'h': int(y1 - y0)}


# ================================================================== ones ====
def bottom_profile(mask, y_start):
    """Per a cada x, la y més alta on comença la capa (o fora de pantalla)."""
    out = []
    for x in list(range(0, W, STEP)) + [W - 1]:
        col = mask[y_start:, x]
        out.append(int(y_start + col.argmax()) if col.any() else H + 80)
    return ndi.median_filter(np.array(out), 3).tolist()


def right_profile(mask, x_start):
    """Per a cada y, la x més a l'esquerra on comença la taca (o fora)."""
    out = []
    for y in list(range(0, H, STEP)) + [H - 1]:
        row = mask[y, x_start:]
        out.append(int(x_start + row.argmax()) if row.any() else W + 80)
    return ndi.median_filter(np.array(out), 3).tolist()


def wave_profiles(img, y_start, x_start, tr_ymax, exclude=None):
    y, p, c, m = near(img, YEL), near(img, PINK), near(img, CYAN), near(img, MAG)
    yb, pb, cb = (y, p, c) if exclude is None else (y & ~exclude, p & ~exclude, c & ~exclude)
    yy = np.arange(H)[:, None]
    tr_zone = yy < tr_ymax
    return {
        'bl': {'yel': bottom_profile(yb | pb | cb, y_start),
               'pink': bottom_profile(pb | cb, y_start),
               'cyan': bottom_profile(cb, y_start)},
        'tr': {'yel': right_profile((y | m) & tr_zone, x_start),
               'mag': right_profile(m & tr_zone, x_start)},
    }


# =============================================================== portada ===
def build_cover(img, entries):
    d = cheb(img, CREAM)
    alpha = soft_alpha(d)
    wave = near(img, YEL, 28) | near(img, PINK, 28) | near(img, CYAN, 28)
    yy, xx = np.mgrid[0:H, 0:W]

    # Sol
    sun = near(img, YEL) & (yy < 480) & (xx > 860) & (xx < 1220)
    cy, cx = [float(v) for v in ndi.center_of_mass(sun)]
    r = (sun.sum() / np.pi) ** .5
    sun_zone = np.hypot(xx - cx, yy - cy) <= r + 3
    rgb, sun_a = matte(img, sun_zone & (alpha > 0), [[251, 191, 26]])
    entries.append(dict(save_sprite('cover_sun', rgb, sun_a, bbox(sun_a > .02)), kind='sun', layer='back'))

    # Rellotge (es redibuixa en viu): mesura l'esfera
    face = near(img, FACE, 2) & (xx > 600) & (xx < 1000) & (yy > 200) & (yy < 600)
    face = ndi.binary_fill_holes(ndi.binary_closing(face, iterations=3))
    lab, nl = ndi.label(face)
    biggest = 1 + int(np.argmax(ndi.sum(face, lab, range(1, nl + 1))))
    face = lab == biggest
    fcy, fcx = [float(v) for v in ndi.center_of_mass(face)]
    fr = float((face.sum() / np.pi) ** .5)
    clock = {'cx': round(fcx, 1), 'cy': round(fcy, 1), 'r': round(fr, 1)}

    # Skyline + torre (darrere de les ones), sense busques.
    wave_top = np.full(W, H)
    for x in range(W):
        col = wave[560:, x]
        if col.any():
            wave_top[x] = 560 + col.argmax()
    above = yy < wave_top[None, :] - 2
    sky_pal = np.array([[100, 166, 190], [140, 190, 205], [137, 197, 210],
                        [98, 168, 190], [21, 133, 175], [140, 192, 206]])
    is_pal = np.zeros((H, W), bool)
    for c in sky_pal:
        is_pal |= near(img, c, 14)
    is_pal |= near(img, FACE, 3) & (np.hypot(xx - fcx, yy - fcy) < fr * 1.6)
    sky = ndi.binary_dilation(is_pal, iterations=2) & (alpha > 0) & ~sun_zone \
        & (xx < 1010) & (yy > 40) & above
    # Vores amb el crema: transparència exacta (sense halo)
    edge = sky & ndi.binary_dilation(near(img, CREAM, 3), iterations=2)
    rgb, ea = matte(img, edge, sky_pal[:4])
    sky_a = np.where(edge, ea, np.where(sky, 1.0, 0.0))
    disc = np.hypot(xx - fcx, yy - fcy) <= fr + 1.5
    rgb[disc] = FACE
    sky_a[disc] = 1
    # Allarga cap avall els edificis que queden tallats per les ones
    for x in range(0, 1010):
        yb = wave_top[x]
        if yb >= H:
            continue
        src = None
        for y_ in range(yb - 3, yb - 30, -1):
            if is_pal[y_, x] and sky[y_, x]:
                dd = np.abs(sky_pal[:4] - rgb[y_, x]).max(1)
                src = sky_pal[dd.argmin()]
                break
        if src is None:
            continue
        rgb[y_:min(H, yb + 70), x] = src
        sky_a[y_:min(H, yb + 70), x] = 1
    entries.append(dict(save_sprite('cover_skyline', rgb, sky_a, bbox(sky_a > .02)), kind='skyline', layer='back'))

    # Logo gran: ANEM A / FONDO / cor / fletxa (colors plans -> retall net)
    zone = (xx > 1150) & (xx < 2400) & (yy > 150) & (yy < 880) & (alpha > 0)
    pal = np.stack([NAVY, CYAN, CORAL, YEL, MAG, CREAM])
    cls = np.abs(img[..., None, :] - pal[None, None]).sum(-1).argmin(-1)
    parts = {
        'cover_anema': ((cls == 0) & (yy < 500), NAVY),
        'cover_fondo': (cls == 1, CYAN),
        'cover_heart': (cls == 2, CORAL),
        'cover_arrow': ((cls == 0) & (yy >= 500), NAVY),
    }
    for name, (m, color) in parts.items():
        m = ndi.binary_dilation(zone & m, iterations=2) & zone
        exact = np.median(img[m & (cheb(img, color) < 20)], axis=0)
        rgb, a = matte(img, m, [exact])
        # Marge ampli i color pur a tot el voltant: cap vora de retall ni franja d'un píxel
        rgb[:] = exact
        entries.append(dict(save_sprite(name, rgb, a, bbox(a > .02, pad=28)), kind=name.split('_')[1]))
    return clock


# =============================================================== PowerPoint
def find_soffice():
    for c in [shutil.which('soffice'), shutil.which('soffice.exe'),
              r'C:\Program Files\LibreOffice\program\soffice.exe',
              r'C:\Program Files (x86)\LibreOffice\program\soffice.exe',
              '/Applications/LibreOffice.app/Contents/MacOS/soffice']:
        if c and os.path.exists(c):
            return c
    return None


def get_pptx():
    pptx = os.path.join(ROOT, 'Fondo.pptx')
    ppt = os.path.join(ROOT, 'Fondo.ppt')
    if os.path.exists(pptx) and (not os.path.exists(ppt) or os.path.getmtime(pptx) >= os.path.getmtime(ppt)):
        return pptx
    cached = os.path.join(CACHE, 'Fondo.pptx')
    if os.path.exists(cached) and os.path.getmtime(cached) >= os.path.getmtime(ppt):
        return cached
    soffice = find_soffice()
    if not soffice:
        raise SystemExit('Cal LibreOffice per convertir Fondo.ppt (o desa la presentació com a Fondo.pptx).')
    os.makedirs(CACHE, exist_ok=True)
    print('Convertint Fondo.ppt -> pptx ...')
    subprocess.run([soffice, '--headless', '--convert-to', 'pptx', '--outdir', CACHE, ppt], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return cached


class Deck:
    def __init__(self, path):
        self.z = zipfile.ZipFile(path)
        pres = ET.fromstring(self.z.read('ppt/presentation.xml'))
        sz = pres.find('p:sldSz', NS)
        self.sw, self.sh = int(sz.get('cx')), int(sz.get('cy'))
        self.theme = {}
        try:
            th = ET.fromstring(self.z.read('ppt/theme/theme1.xml'))
            for c in th.find('.//a:clrScheme', NS):
                tag = c.tag.split('}')[1]
                v = c.find('a:srgbClr', NS)
                s = c.find('a:sysClr', NS)
                self.theme[tag] = (v.get('val') if v is not None else s.get('lastClr', '000000'))
        except KeyError:
            pass
        self.theme.update(tx1=self.theme.get('dk1', '000000'), bg1=self.theme.get('lt1', 'FFFFFF'),
                          tx2=self.theme.get('dk2', '000000'), bg2=self.theme.get('lt2', 'FFFFFF'))

    def rels(self, n):
        path = f'ppt/slides/_rels/slide{n}.xml.rels'
        out = {}
        for r in ET.fromstring(self.z.read(path)):
            out[r.get('Id')] = r.get('Target')
        return out

    def color(self, node):
        if node is None:
            return None
        c = node.find('a:srgbClr', NS)
        if c is not None:
            return '#' + c.get('val')
        c = node.find('a:schemeClr', NS)
        if c is not None:
            return '#' + self.theme.get(c.get('val'), '000000')
        return None


FONTS = {
    'Poppins ExtraBold': ('Poppins', 800), 'Poppins Black': ('Poppins', 900),
    'Poppins Bold': ('Poppins', 700), 'Poppins SemiBold': ('Poppins', 600),
    'Poppins Medium': ('Poppins', 500), 'Poppins': ('Poppins', 400),
    'Poppins Light': ('Poppins', 300),
    'Helvetica Neue Medium': ('Helvetica Neue', 500), 'Helvetica Neue': ('Helvetica Neue', 400),
}


def font_of(rpr, default=('Poppins', 500)):
    if rpr is None:
        return default
    lat = rpr.find('a:latin', NS)
    name = lat.get('typeface') if lat is not None else None
    fam, wt = FONTS.get(name, (name, 400)) if name else default
    if rpr.get('b') == '1':
        wt = max(wt, 700)
    return fam, wt


def px(v):
    return round(int(v) / EMU, 2)


def xfrm_of(sppr):
    x = sppr.find('a:xfrm', NS) if sppr is not None else None
    if x is None:
        return None
    off, ext = x.find('a:off', NS), x.find('a:ext', NS)
    return {'x': int(off.get('x')), 'y': int(off.get('y')), 'w': int(ext.get('cx')), 'h': int(ext.get('cy')),
            'flipH': x.get('flipH') == '1', 'rot': int(x.get('rot', 0)) / 60000}


def parse_paragraphs(deck, txbody):
    paras = []
    for p in txbody.findall('a:p', NS):
        ppr = p.find('a:pPr', NS)
        para = {'al': 'l', 'mL': 0, 'ind': 0, 'ln': None, 'bef': 0, 'aft': 0, 'bu': None, 'runs': []}
        if ppr is not None:
            para['al'] = ppr.get('algn', 'l')
            para['mL'] = px(ppr.get('marL', 0))
            para['ind'] = px(ppr.get('indent', 0))
            ln = ppr.find('a:lnSpc', NS)
            if ln is not None:
                pct, pts = ln.find('a:spcPct', NS), ln.find('a:spcPts', NS)
                para['ln'] = {'pct': int(pct.get('val')) / 100000} if pct is not None else \
                             {'px': int(pts.get('val')) / 100 * 4 / 3}
            for key, tag in (('bef', 'a:spcBef'), ('aft', 'a:spcAft')):
                s = ppr.find(tag, NS)
                if s is not None and s.find('a:spcPts', NS) is not None:
                    para[key] = int(s.find('a:spcPts', NS).get('val')) / 100 * 4 / 3
            ch = ppr.find('a:buChar', NS)
            if ch is not None and ppr.find('a:buNone', NS) is None:
                c = ch.get('char')
                bfont = ppr.find('a:buFont', NS)
                if c in ('\uf0b7', '\u2022', '\uf06c', '\uf0a7') or (bfont is not None and bfont.get('typeface') == 'Symbol'):
                    c = '•'
                elif c == '-':
                    c = '–'
                para['bu'] = {'ch': c, 'c': deck.color(ppr.find('a:buClr', NS))}
        for node in p:
            tag = node.tag.split('}')[1]
            if tag in ('r', 'fld'):
                rpr = node.find('a:rPr', NS)
                t = node.find('a:t', NS)
                fam, wt = font_of(rpr)
                para['runs'].append({
                    't': t.text or '' if t is not None else '',
                    'sz': int(rpr.get('sz', 1800)) / 100 * 4 / 3 if rpr is not None else 24,
                    'f': fam, 'wt': wt,
                    'it': rpr is not None and rpr.get('i') == '1',
                    'c': deck.color(rpr.find('a:solidFill', NS)) if rpr is not None else None,
                })
            elif tag == 'br':
                para['runs'].append({'br': 1})
        end = p.find('a:endParaRPr', NS)
        para['esz'] = int(end.get('sz', 1800)) / 100 * 4 / 3 if end is not None else 24
        para['ef'] = font_of(end)[0] if end is not None else 'Arial'
        paras.append(para)
    return paras


def slide_link(deck, n, cnvpr):
    if cnvpr is None:
        return None
    h = cnvpr.find('a:hlinkClick', NS)
    if h is None:
        return None
    action = h.get('action', '')
    if 'nextslide' in action:
        return 'next'
    if 'previousslide' in action:
        return 'prev'
    rid = h.get(RID)
    if rid:
        tgt = deck.rels(n).get(rid, '')
        m = re.search(r'slide(\d+)\.xml', tgt)
        if m:
            return int(m.group(1))
    return None


def bake_picture(deck, n, idx, media_path, xf, shadow):
    """Retalla/escala la imatge a la mida final i hi aplica l'ombra de la presentació."""
    img = Image.open(deck.z.open(media_path)).convert('RGBA')
    w, h = max(1, round(xf['w'] / EMU)), max(1, round(xf['h'] / EMU))
    img = img.resize((w, h), Image.LANCZOS)
    if xf['flipH']:
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    pad = 0
    if shadow:
        blur = shadow['blur'] / 2.0
        pad = int(blur * 2.5 + shadow['dist'] + 2)
        canvas = Image.new('RGBA', (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        a = Image.new('L', canvas.size, 0)
        a.paste(img.getchannel('A'), (pad + round(shadow['dx']), pad + round(shadow['dy'])))
        a = a.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * shadow['op']))
        sh = Image.new('RGBA', canvas.size, shadow['rgb'] + (0,))
        sh.putalpha(a)
        canvas.alpha_composite(sh)
        canvas.alpha_composite(img, (pad, pad))
        img = canvas
    name = f's{n:02d}_{idx:02d}.webp'
    img.save(os.path.join(MED, name), quality=88, alpha_quality=90, method=6)
    return {'src': f'assets/media/{name}', 'x': px(xf['x']) - pad, 'y': px(xf['y']) - pad,
            'w': img.size[0], 'h': img.size[1]}


def parse_shadow(deck, sppr):
    s = sppr.find('a:effectLst/a:outerShdw', NS) if sppr is not None else None
    if s is None:
        return None
    blur = int(s.get('blurRad', 0)) / EMU
    dist = int(s.get('dist', 0)) / EMU
    ang = int(s.get('dir', 0)) / 60000 / 180 * np.pi
    col = deck.color(s) or '#000000'
    alpha = s.find('.//a:alpha', NS)
    op = int(alpha.get('val')) / 100000 if alpha is not None else 0.5
    rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
    return {'blur': blur, 'dist': dist, 'dx': dist * np.cos(ang), 'dy': dist * np.sin(ang), 'op': op, 'rgb': rgb}


def parse_tree(deck, n, tree, rels, out, counter, gx=None):
    """Recorre l'arbre de formes; gx = funció de transformació del grup pare."""
    tf = gx or (lambda x, y, w, h: (x, y, w, h))
    for node in tree:
        tag = node.tag.split('}')[1]
        if tag == 'pic':
            sppr = node.find('p:spPr', NS)
            xf = xfrm_of(sppr)
            cnv = node.find('p:nvPicPr/p:cNvPr', NS)
            link = slide_link(deck, n, cnv)
            if xf is None:
                continue
            x, y, w, h = tf(xf['x'], xf['y'], xf['w'], xf['h'])
            xf.update(x=x, y=y, w=w, h=h)
            # Fons de la diapositiva i botons de navegació: es fan a la web
            if x <= 0 and y <= 0 and x + w >= deck.sw and y + h >= deck.sh:
                continue
            if link is not None and abs(w - 795960) < 20000:
                continue
            blip = node.find('p:blipFill/a:blip', NS)
            media = posixpath.normpath(posixpath.join('ppt/slides', rels[blip.get(REMB)]))
            counter[0] += 1
            e = bake_picture(deck, n, counter[0], media, xf, parse_shadow(deck, sppr))
            e.update(t='pic', bw=px(xf['w']), bh=px(xf['h']))
            if link is not None:
                e['link'] = link
            out.append(e)
        elif tag == 'sp':
            sppr = node.find('p:spPr', NS)
            xf = xfrm_of(sppr)
            if xf is None:
                continue
            x, y, w, h = tf(xf['x'], xf['y'], xf['w'], xf['h'])
            geom = sppr.find('a:prstGeom', NS)
            fill = deck.color(sppr.find('a:solidFill', NS))
            e = {'t': 'text', 'x': px(x), 'y': px(y), 'w': px(w), 'h': px(h)}
            if fill:
                e['fill'] = fill
                if geom is not None and geom.get('prst') == 'roundRect':
                    gd = geom.find('a:avLst/a:gd', NS)
                    adj = int(gd.get('fmla').split()[1]) / 100000 if gd is not None else 0.16667
                    e['rad'] = round(adj * min(e['w'], e['h']), 2)
            body = node.find('p:txBody', NS)
            paras = []
            if body is not None:
                bp = body.find('a:bodyPr', NS)
                e['ins'] = [px(bp.get(k, d)) for k, d in
                            (('lIns', 91440), ('tIns', 45720), ('rIns', 91440), ('bIns', 45720))]
                e['anchor'] = bp.get('anchor', 't')
                e['wrap'] = bp.get('wrap', 'square')
                paras = parse_paragraphs(deck, body)
            has_text = any(r.get('t', '').strip() for p in paras for r in p['runs'])
            if not has_text and not fill:
                continue
            if has_text:
                e['paras'] = paras
            link = slide_link(deck, n, node.find('p:nvSpPr/p:cNvPr', NS))
            if link is not None:
                e['link'] = link
            out.append(e)
        elif tag == 'grpSp':
            gp = node.find('p:grpSpPr/a:xfrm', NS)
            off, ext = gp.find('a:off', NS), gp.find('a:ext', NS)
            choff, chext = gp.find('a:chOff', NS), gp.find('a:chExt', NS)
            ox, oy, ew, eh = int(off.get('x')), int(off.get('y')), int(ext.get('cx')), int(ext.get('cy'))
            cx0, cy0 = int(choff.get('x')), int(choff.get('y'))
            cw, ch = max(1, int(chext.get('cx'))), max(1, int(chext.get('cy')))
            sx, sy = ew / cw, eh / ch

            def child_tf(x, y, w, h, ox=ox, oy=oy, cx0=cx0, cy0=cy0, sx=sx, sy=sy):
                return tf(ox + (x - cx0) * sx, oy + (y - cy0) * sy, w * sx, h * sy)

            children = []
            parse_tree(deck, n, node, rels, children, counter, child_tf)
            if not children:
                continue
            gx0, gy0, gw, gh = tf(ox, oy, ew, eh)
            g = {'t': 'grp', 'x': px(gx0), 'y': px(gy0), 'w': px(gw), 'h': px(gh), 'kids': children}
            link = slide_link(deck, n, node.find('p:nvGrpSpPr/p:cNvPr', NS))
            link = link if link is not None else next((c['link'] for c in children if 'link' in c), None)
            if link is not None:
                g['link'] = link
            out.append(g)


def classify(n, e):
    if e['t'] == 'grp':
        return 'tile' if any(c.get('fill') for c in e['kids']) else 'group'
    if e['t'] == 'pic':
        big = max(e['bw'], e['bh'])
        if n == 3:
            return 'logo'
        if big < 330:
            return 'icon' if n == 2 else 'badge'
        return 'photo'
    sizes = [r['sz'] for p in e.get('paras', []) for r in p['runs'] if 'sz' in r]
    if sizes and max(sizes) >= 70 and e['y'] < 330:
        return 'title'
    return 'text'


def build_slides(deck):
    slides = {}
    for n in range(2, 12):
        root = ET.fromstring(deck.z.read(f'ppt/slides/slide{n}.xml'))
        tree = root.find('p:cSld/p:spTree', NS)
        items = []
        parse_tree(deck, n, tree, deck.rels(n), items, [0])
        # Fora de la diapositiva (elements aparcats al costat): no es mostren
        items = [e for e in items if e['x'] < W and e['y'] < H and e['x'] + e['w'] > 0 and e['y'] + e['h'] > 0]
        for e in items:
            e['kind'] = classify(n, e)
        slides[str(n)] = items
        print(f'diapo {n}:', ', '.join(e['kind'] for e in items))
    return slides


# ================================================================== main ===
def main():
    for d in (SPR, MED):
        os.makedirs(d, exist_ok=True)
        for f in glob.glob(os.path.join(d, '*.webp')):
            os.remove(f)

    S = {n: load(n) for n in [1, 2, 4, 5, 6, 7, 8, 9, 10, 11]}
    plate = np.median(np.stack([S[n] for n in [2, 4, 5, 6, 7, 8, 9, 10, 11]]), axis=0).astype(np.int16)
    yy, xx = np.mgrid[0:H, 0:W]
    logo_zone = (xx >= LOGO_ZONE[0]) & (xx < LOGO_ZONE[2]) & (yy >= LOGO_ZONE[1]) & (yy < LOGO_ZONE[3])

    manifest = {'W': W, 'H': H, 'step': STEP, 'slides': {}}

    # Logo petit (persistent), retall net
    lz = logo_zone & (cheb(plate, CREAM) > 3)
    rgb, la = matte(plate, ndi.binary_dilation(lz, iterations=1) & logo_zone, [NAVY, CYAN, CORAL])
    manifest['logoSmall'] = save_sprite('logo_small', rgb, la, bbox(la > .02))

    # Portada (il·lustració)
    cover = []
    manifest['clock'] = build_cover(S[1], cover)
    manifest['slides']['1'] = cover

    # Ones del fons
    waves_zone = ((xx < 1100) & (yy > 1150)) | ((xx > 2200) & (yy < 300))
    wc = near(plate, YEL, 14) | near(plate, PINK, 14) | near(plate, CYAN, 14) | near(plate, MAG, 14)
    keep = ndi.binary_dilation(wc, iterations=2) & waves_zone
    clean = np.broadcast_to(CREAM, (H, W, 3)).astype(np.int16).copy()
    clean[keep] = plate[keep]
    manifest['waves'] = {
        'corner': wave_profiles(clean, 900, 1900, 600),
        # a la portada, el logo gran té els mateixos colors que les ones
        'cover': wave_profiles(S[1], 560, 1900, 600, exclude=(xx > 1150) & (xx < 2400) & (yy < 860)),
    }

    # Diapositives 2-11: des del PowerPoint
    deck = Deck(get_pptx())
    manifest['slides'].update(build_slides(deck))

    with open(os.path.join(OUT, 'manifest.js'), 'w', encoding='utf8') as f:
        f.write('// Generat per tools/build_assets.py — no editar a mà\n')
        f.write('window.FONDO = ' + json.dumps(manifest, ensure_ascii=False, separators=(',', ':')) + ';\n')
    print('fet: assets/manifest.js')


if __name__ == '__main__':
    main()
