"""Stage 1: every decision XML in the DILA court archives (CASS, INCA, CAPP, JADE, CONSTIT: the full dump, then each
increment) -> one gzip JSONL per archive, parallel by archive. Records carry the archive stamp so build.py keeps the
latest version of each decision; deletion lists in the increments are recorded as {"deleted": id}.

  python parse.py <cache dir: <FUND>/*.tar.gz> <out dir> [--workers 20]"""
from __future__ import annotations

import argparse
import gzip
import html
import json
import re
import tarfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ONE = lambda tag: re.compile(r'<' + tag + r'\b[^>]*>(.*?)</' + tag + '>', re.S)
TAGS = {k: ONE(k) for k in ('ID', 'NATURE', 'TITRE', 'DATE_DEC', 'JURIDICTION', 'NUMERO', 'SOLUTION', 'FORMATION',
                              'ECLI', 'SIEGE_APPEL', 'DATE_DEC_ATT', 'TYPE_REC', 'URL_CC')}
AFF = re.compile(r'<NUMERO_AFFAIRE>([^<]*)</NUMERO_AFFAIRE>')
PUBLI_BULL = re.compile(r'<PUBLI_BULL\b[^>]*publie="([^"]*)"')
PUBLI_REC = re.compile(r'<PUBLI_RECUEIL>([^<]*)</PUBLI_RECUEIL>')
LIEN = re.compile(r'<LIEN\b([^>]*)>(.*?)</LIEN>', re.S)
ATTR = re.compile(r'(\w+)="([^"]*)"')
CONTENU = re.compile(r'<TEXTE>.*?<CONTENU>(.*?)</CONTENU>', re.S)
SOMMAIRE = re.compile(r'<SOMMAIRE>(.*?)</SOMMAIRE>', re.S)
BR = re.compile(r'<br\s*/?>|</p>|</div>|</h\d>|</li>|</tr>', re.I)
TAG = re.compile(r'<[^>]+>')
ID_RX = re.compile(r'\b((?:JURI|CETA|CONS)TEXT\d{12})\b')


def clean(x: str | None) -> str:
    if not x:
        return ''
    t = html.unescape(TAG.sub('', BR.sub('\n', x)))
    lines = [l.strip() for l in t.replace('\r', '').split('\n')]
    out, blank = [], False
    for l in lines:
        if not l:
            blank = bool(out)
            continue
        if blank:
            out.append('')
        out.append(l)
        blank = False
    return '\n'.join(out)


def parse(raw: bytes, fund: str) -> dict | None:
    s = raw.decode('utf-8', 'replace')
    g = {k: (lambda m: clean(m.group(1)) if m else None)(rx.search(s)) for k, rx in TAGS.items()}
    if not g['ID'] or not ID_RX.match(g['ID']):
        return None
    body = CONTENU.search(s)
    text = clean(body.group(1)) if body else ''
    if not text:
        return None
    liens = []
    for attrs, label in LIEN.findall(s):
        a = dict(ATTR.findall(attrs))
        liens.append({'cid': a.get('cidtexte', ''), 'id': a.get('id', ''), 'num': a.get('num', ''), 'nature': a.get('naturetexte', ''),
                      'type': a.get('typelien', ''), 'text': clean(label)})
    pb = PUBLI_BULL.search(s)
    pr = PUBLI_REC.search(s)
    som = SOMMAIRE.search(s)
    return {'id': g['ID'], 'fund': fund, 'nature': g['NATURE'], 'title': g['TITRE'], 'decided': g['DATE_DEC'],
            'court': g['JURIDICTION'], 'numero': g['NUMERO'], 'affaires': AFF.findall(s), 'solution': g['SOLUTION'],
            'formation': g['FORMATION'], 'ecli': g['ECLI'], 'siege_appel': g['SIEGE_APPEL'], 'date_dec_att': g['DATE_DEC_ATT'],
            'type_rec': g['TYPE_REC'], 'publication': ('Publié au bulletin' if pb and pb.group(1) == 'oui' else
                                                       ('Inédit' if pb else (clean(pr.group(1)) if pr else None))),
            'sommaire': clean(som.group(1)) if som else '', 'text': text, 'liens': liens}


def stamp_of(name: str) -> str:
    m = re.search(r'(\d{8}-\d{6})', name)
    return m.group(1) if m else '00000000-000000'


def one(tar_path: str, out: str) -> dict:
    p = Path(tar_path)
    fund = p.parent.name
    stamp = stamp_of(p.name) if 'global' not in p.name else '00000000-000000'
    n = kept = dele = 0
    dest = Path(out) / fund / f'{stamp}.jsonl.gz'
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size:
        return {'archive': p.name, 'fund': fund, 'xml': 0, 'with_text': 0, 'deleted': 0, 'skipped': True}
    tmp = dest.with_suffix('.part')
    with tarfile.open(tar_path, 'r:gz') as tf, gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=3) as fo:
        for mem in tf:
            if not mem.isfile():
                continue
            name = mem.name
            if name.endswith('.xml'):
                n += 1
                r = parse(tf.extractfile(mem).read(), fund)
                if r:
                    r['stamp'] = stamp
                    fo.write(json.dumps(r, ensure_ascii=False) + '\n')
                    kept += 1
            elif 'suppression' in name.lower():
                for did in ID_RX.findall(tf.extractfile(mem).read().decode('utf-8', 'replace')):
                    fo.write(json.dumps({'deleted': did, 'stamp': stamp}) + '\n')
                    dele += 1
    tmp.replace(dest)
    return {'archive': p.name, 'fund': fund, 'xml': n, 'with_text': kept, 'deleted': dele}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cache'), ap.add_argument('out'), ap.add_argument('--workers', type=int, default=20)
    a = ap.parse_args()
    tars = sorted(Path(a.cache).glob('*/*.tar.gz'), key=lambda p: -p.stat().st_size)
    stats = []
    with ProcessPoolExecutor(a.workers) as ex:
        for s in ex.map(one, [str(t) for t in tars], [a.out] * len(tars)):
            stats.append(s)
    (Path(a.out) / 'stats.json').write_text(json.dumps(stats, indent=0))
    by = {}
    for s in stats:
        b = by.setdefault(s['fund'], [0, 0, 0])
        b[0] += s['xml']; b[1] += s['with_text']; b[2] += s['deleted']
    print(json.dumps({k: {'xml': v[0], 'with_text': v[1], 'deleted': v[2]} for k, v in by.items()}), flush=True)


if __name__ == '__main__':
    main()
