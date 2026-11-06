"""Stage 2: one French case-law repo from the parsed DILA archives, straight into git fast-import.

  python build.py <group> <parsed dir> <out repo dir> --today YYYY-MM-DD --updated YYYY-MM-DD [--law-index fr_index.json]

One commit per decision (Kind: court), dated the day decided (noon Paris); then one Lexifina documentation commit with
README (with the credit the Licence Ouverte 2.0 requires: the producer, DILA, and the date of the last update),
REPRODUCE.md, corpus.json and the judgment overlay judgments/france/national/acts/<path>.jsonl, each row pinned to the
cited article's own version in france-law on the day decided. Deterministic for the same input."""
from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import unicodedata

import regex  # per-match timeout: a few decisions made re backtrack for tens of minutes
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'nl-caselaw'))
from reconcile import fix_row, versions  # noqa: E402  (law_commit = the article's own version)

EPOCH_NOON = 11 * 3600  # 1970-01-01 12:00 Paris (UTC+1), as the Swiss and Dutch builds


def email_local(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')[:60] or 'decision'

LAW_CORPUS = 'france/national/acts'
PARIS = timezone(timedelta(hours=1))
COMMITTER = b'Lexifina <corpus@lexifina.com>'
ISO = re.compile(r'^\d{4}-\d{2}-\d{2}$')
GROUPS = {
    'cour-de-cassation': {'funds': ('CASS', 'INCA'), 'name': 'the Cour de cassation',
                          'producer': 'Cour de cassation', 'bases': 'CASS (decisions published in the Bulletin) and INCA (unpublished decisions)'},
    'cours-d-appel': {'funds': ('CAPP',), 'name': "the cours d'appel (courts of appeal)", 'producer': 'Cour de cassation',
                      'bases': "CAPP (decisions of the cours d'appel and of first-instance courts)"},
    'conseil-d-etat': {'funds': ('JADE',), 'court': lambda c: (c or '').lower().startswith('conseil'), 'name': "the Conseil d'État",
                       'producer': "Conseil d'État", 'bases': 'JADE'},
    'cours-administratives-d-appel': {'funds': ('JADE',), 'court': lambda c: 'administrative d' in (c or '').lower(),
                                      'name': "the cours administratives d'appel", 'producer': "Conseil d'État", 'bases': 'JADE'},
    'tribunaux-administratifs': {'funds': ('JADE',), 'court': lambda c: not (c or '').lower().startswith('conseil') and 'administrative d' not in (c or '').lower(),
                                 'name': 'the tribunaux administratifs and other administrative courts', 'producer': "Conseil d'État", 'bases': 'JADE'},
    'conseil-constitutionnel': {'funds': ('CONSTIT',), 'name': 'the Conseil constitutionnel', 'producer': 'Conseil constitutionnel', 'bases': 'CONSTIT'},
}
LF = {'JURI': 'juri', 'CETA': 'ceta', 'CONS': 'cons'}


def ascii_slug(s: str) -> str:  # france-law tools/build.py's rule
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower()
    return s or 'x'


def article_slug(num: str) -> str:  # france-law tools/build.py's rule ('L. 123-4' -> L123-4)
    s = unicodedata.normalize('NFKD', num).encode('ascii', 'ignore').decode()
    s = re.sub(r'\b([LRDA])\s*\.?\s*\*?\s*(?=\d)', r'\1', s)
    s = re.sub(r'[^A-Za-z0-9\-]+', '-', s).strip('-')
    return s[:80]


ART_TOKEN = r'(?:[LRDA]\s*\.?\s*\*?\s*)?\d+(?:-\d+)*(?:\s+(?:bis|ter|quater|quinquies|sexies))?'
CITE = regex.compile(r'\b[Aa]rt(?:icle)?s?\.?\s+((?:' + ART_TOKEN + r')(?:\s*(?:,|et|à)\s*(?:' + ART_TOKEN + r'))*)'
                  r'(?:,?\s+(?:alinéas?|al\.)\s+\d+(?:\s*(?:,|et)\s*\d+)*)?'
                  r'\s+(?:du|de\s+la|de\s+l\'|des|de)\s+((?:code|Code|Constitution)[^.;:,()\n]{0,90})')
TOKEN = re.compile(ART_TOKEN)
ART_START = re.compile(r'\b[Aa]rt(?:icle)?s?\b')


class Resolver:
    def __init__(self, idx: dict):
        self.legitext = idx['legitext']           # LEGITEXT id -> folder
        self.codes = idx['codes']                 # slug of the code's name -> folder
        self.arts = idx['arts']                   # folder -> {article slug -> path}
        self.timeouts = 0

    def folder_from_name(self, phrase: str) -> str | None:
        words = phrase.split()
        if words and words[0].lower().startswith('constitution'):
            return 'constitution/constitution-1958'
        best = None
        for k in range(1, min(len(words), 14) + 1):
            s = ascii_slug(' '.join(words[:k]))
            if s in self.codes:
                best = self.codes[s]
        return best

    def path(self, folder: str, num: str) -> str | None:
        return self.arts.get(folder, {}).get(article_slug(num)) if folder else None

    def from_lien(self, l: dict) -> list[tuple[str, str]]:
        out = []
        if l['cid'] in self.legitext and l['num']:
            p = self.path(self.legitext[l['cid']], l['num'])
            if p:
                return [(p, l['text'] or l['num'])]
        for p, how in self.from_text(l['text']):
            out.append((p, how))
        return out

    def from_text(self, text: str) -> list[tuple[str, str]]:
        """CITE tried only where "art" starts a word, on a 600-character window: a decision with a long run of numbers
        made CITE.finditer over the whole text backtrack for tens of minutes (seen 2026-11-06 in JADE)."""
        out = []
        text = text or ''
        for s in ART_START.finditer(text):
            try:
                m = CITE.match(text[s.start():s.start() + 600], timeout=0.05)
            except TimeoutError:
                self.timeouts += 1
                continue
            if not m:
                continue
            folder = self.folder_from_name(m.group(2))
            if not folder:
                continue
            for t in TOKEN.findall(m.group(1)):
                p = self.path(folder, t)
                if p:
                    out.append((p, re.sub(r'\s+', ' ', m.group(0)).strip()[:200]))
        return out


def link_of(did: str) -> str:
    return f"https://www.legifrance.gouv.fr/{LF.get(did[:4], 'juri')}/id/{did}"


def render(r: dict, day: str, G: dict, res: Resolver, ver: dict, updated: str, extra: tuple = ()):
    """One decision as (path, file bytes, author, email, commit message bytes, [(law path, overlay row)],
    [references for decision-index.tsv], unlinked LIEN count). Shared by the full build and the nightly appends.
    `extra` trailers go last; a Corrected: one makes the commit Kind: correction."""
    court = r['court'] or G['producer']
    path = f"decisions/{ascii_slug(court)[:40]}/{day[:4]}/{r['id']}.md"
    docket = ', '.join(r['affaires']) if r['affaires'] else (r['numero'] or r['id'])
    cites, unlinked = [], 0
    for l in r['liens']:
        got = res.from_lien(l)
        if not got and l['text']:
            unlinked += 1
        cites += got
    cites += res.from_text(r['text'])
    url = link_of(r['id'])
    refs = sorted(x for x in {*r['affaires'], r['numero'] or '', r['ecli'] or '', r['id']} if x)
    fm = {'id': r['id'], 'ecli': r['ecli'], 'court': court, 'formation': r['formation'], 'docket': docket,
          'decided': r['decided'], 'nature': r['nature'], 'solution': r['solution'], 'publication': r['publication'],
          'siege_appel': r['siege_appel'], 'appealed_decision_date': r['date_dec_att'], 'source_url': url,
          'source': f"DILA, base {r['fund']} ({G['producer']})", 'licence': 'Licence Ouverte 2.0 (Etalab)',
          'source_updated': updated}
    lines = ['---'] + [f'{k}: {json.dumps(v, ensure_ascii=False)}' for k, v in fm.items() if v not in (None, '')]
    uniq = sorted({p for p, _ in cites})
    if uniq:
        lines.append('cites_law: ' + json.dumps([f'{LAW_CORPUS}/{p}' for p in uniq], ensure_ascii=False))
    lines += ['---', '', f"# {(r['title'] or r['id']).strip()}", '']
    if r['sommaire']:
        lines += ['## Sommaire', '', r['sommaire'].strip(), '']
    lines += ['## Texte', '', r['text'].strip(), '']
    body = '\n'.join(lines).encode('utf-8')
    author = f'{court} {docket}'.replace('<', '').replace('>', '')[:200]
    email = f"{email_local(r['id'])}@caselaw.invalid"
    kind = 'correction' if any(t.startswith('Corrected:') for t in extra) else 'court'
    tr = [f'Kind: {kind}', f'In-force: {day}', f'Decided: {day}', f'Court: {court}', f'Docket: {docket}', f"Id: {r['id']}"]
    if r['ecli']:
        tr.append(f"ECLI: {r['ecli']}")
    tr.append(f'Source: {url}')
    seen = set()
    for p, how in cites:
        if p not in seen and len(seen) < 60:
            seen.add(p)
            tr.append(f'Cites: {LAW_CORPUS}/{p} ({how[:120]})')
    tr += [t for t in extra if t and not t.rstrip().endswith(':')]
    what = (r['solution'] or r['nature'] or 'décision').strip()
    msg = (f'{court} {docket}: {what}'[:200] + '\n\n' + '\n'.join(tr) + '\n').encode('utf-8')
    counts = {}
    for p, _ in cites:
        counts[p] = counts.get(p, 0) + 1
    rows, done = [], set()
    for p, how in cites:
        if p in done:
            continue
        done.add(p)
        row = {'decided': day, 'name': f'{court} {docket}', 'court': court, 'citation': r['ecli'] or r['id'], 'url': url,
               'how': how[:200], 'mentions': counts[p], 'decision': path}
        rows.append((p, fix_row(row, p, ver)))
    return path, body, author, email, msg, rows, refs, unlinked


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('group'), ap.add_argument('parsed'), ap.add_argument('out')
    ap.add_argument('--today', required=True), ap.add_argument('--updated', required=True, help='date of the latest DILA archive used')
    ap.add_argument('--law-index', default=str(HERE / 'fr_index.json'))
    ap.add_argument('--law-repo', default=str(Path.home() / 'Documents' / 'caselaw-law-mirrors' / 'france-law.git'))
    ap.add_argument('--chunk-bytes', type=int, default=2_500_000_000)
    a = ap.parse_args()
    G = GROUPS[a.group]
    out = Path(a.out)
    res = Resolver(json.load(open(a.law_index, encoding='utf-8')))
    ver = versions(Path(a.law_repo), ref='main')
    latest, deleted = {}, {}
    for fund in G['funds']:
        for f in sorted((Path(a.parsed) / fund).glob('*.jsonl.gz')):
            with gzip.open(f, 'rt', encoding='utf-8') as fh:
                for line in fh:
                    r = json.loads(line)
                    if 'deleted' in r:
                        deleted[r['deleted']] = max(deleted.get(r['deleted'], ''), r['stamp'])
                        continue
                    if 'court' in G and not G['court'](r['court']):
                        continue
                    old = latest.get(r['id'])
                    if not old or r['stamp'] >= old['stamp']:
                        latest[r['id']] = r
    recs = [r for r in latest.values() if not (r['id'] in deleted and deleted[r['id']] >= r['stamp'])]
    recs.sort(key=lambda r: ((r['decided'] or '9999')[:10], r['court'] or '', r['id']))
    print(f'{a.group}: {len(recs)} decisions ({len(deleted)} deletions seen)', file=sys.stderr, flush=True)

    if out.exists() and any(out.iterdir()):
        raise SystemExit(f'{out} is not empty')
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', '-b', 'main', str(out)], check=True)
    fi = subprocess.Popen(['git', '-C', str(out), 'fast-import', '--quiet', '--done', f'--export-marks={out / ".git" / "marks"}',
                          '--big-file-threshold=64m'], stdin=subprocess.PIPE)
    w = fi.stdin.write
    credit = (f"Source: DILA, base {G['bases']}; producer: {G['producer']}; Licence Ouverte 2.0 (Etalab); "
              f"last update {a.updated}")
    overlay, unlinked = {}, 0
    dindex = []
    mark = raw = clamp = 0
    checkpoints, undated, first = [], [], None
    for r in recs:
        day = (r['decided'] or '')[:10]
        if not ISO.match(day):
            undated.append(r)
            continue
        first = first or day
        path, body, author, email, msg, rows, refs, unl = render(r, day, G, res, ver, a.updated)
        unlinked += unl
        dindex += [ref + chr(9) + path for ref in refs]
        raw += len(body)
        d0 = date.fromisoformat(day)
        if d0.year < 1970:
            clamp += 1
            ts = EPOCH_NOON + clamp - 3600
        else:
            ts = int(datetime(d0.year, d0.month, d0.day, 12, tzinfo=PARIS).timestamp())
        mark += 1
        w(b'commit refs/heads/main\nmark :%d\n' % mark)
        w(b'author %s <%s> %d +0100\ncommitter %s %d +0100\n' % (author.encode('utf-8'), email.encode(), ts, COMMITTER, ts))
        w(b'data %d\n%s\n' % (len(msg), msg))
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (path.encode('utf-8'), len(body), body))
        if raw >= (len(checkpoints) + 1) * a.chunk_bytes:
            checkpoints.append(mark)
        for p, row in rows:
            overlay.setdefault(p, []).append(row)

    docs = {}
    for p, rows in overlay.items():
        rows.sort(key=lambda x: (x['decided'], x['citation']))
        docs[f'judgments/{LAW_CORPUS}/{p[:-3]}.jsonl'] = ''.join(json.dumps(x, ensure_ascii=False, sort_keys=True) + '\n' for x in rows)
    for r in undated:
        docs[f"decisions/undated/{r['id']}.md"] = f"---\nid: {json.dumps(r['id'])}\nsource_url: {json.dumps(link_of(r['id']))}\n---\n\n# {r['title'] or r['id']}\n\n{r['text']}\n"
    n_refs = sum(len(v) for v in overlay.values())
    docs['decision-index.tsv'] = 'reference' + chr(9) + 'path' + chr(10) + ''.join(x + chr(10) for x in sorted(set(dindex)))
    gl_path = f'france/{a.group}/caselaw'
    docs['corpus.json'] = json.dumps({
        'corpus': f'fr/{a.group}/caselaw', 'path': gl_path, 'name': f"Case law: France, {G['name']}",
        'label': f"France case law ({G['name']})", 'language': 'fr', 'kind': 'judgments', 'history': 'in-force',
        'coverage': f'{mark} decisions with text from the DILA base(s) {G["bases"]}, one file and one commit each, dated the day decided; {n_refs} citations of law linked to france-law.',
        'history_from': first, 'record_to': a.updated, 'checked_on': a.today,
        'record_note': 'Court decisions, not laws: every commit is Kind: court. In-force is the day decided. Commits before 1970 are dated 1970-01-01, a second apart; In-force holds the real day.',
        'in_force_trailer': 'In-force', 'correction_trailer': 'Corrected',
        'dates': {'commit': 'decided', 'time': '12:00 Europe/Paris'},
        'source': {'publisher': "Direction de l'information légale et administrative (DILA)", 'producer': G['producer'],
                   'dataset': G['bases'], 'url': 'https://echanges.dila.gouv.fr/OPENDATA/', 'licence': 'Licence Ouverte / Open Licence 2.0 (Etalab)',
                   'updated': a.updated, 'last_update': a.updated},
        'licence': 'Licence Ouverte / Open Licence 2.0 (Etalab)', 'credit': credit,
        'overlay': {'path': f'judgments/{LAW_CORPUS}/<path>.jsonl', 'law_corpus': LAW_CORPUS},
    }, ensure_ascii=False, indent=2) + '\n'
    repo = f'france-{a.group}-caselaw'
    docs['README.md'] = f"""# French case law: {G['name']}

Part of [lexifina/global-law](https://github.com/lexifina/global-law), one git history for every country's law: this is the case law of {G['name']}, France, which global-law reads at `{gl_path}/`.

Every decision of {G['name']} that DILA publishes with its text, as a git history: one file per decision, one commit
per decision, dated the day it was decided. {mark:,} decisions from {first} to {a.updated}.

## Source and licence

{credit}.

The decisions are the open data of the {G['producer']}, distributed by the Direction de l'information légale et
administrative (DILA) at https://echanges.dila.gouv.fr/OPENDATA/ under the Licence Ouverte / Open Licence 2.0 (Etalab),
https://www.etalab.gouv.fr/licence-ouverte-open-licence. Changes made here: each decision converted from XML to Markdown
(markup removed, text unchanged) and dated by its decision day; links to the law added in `judgments/`. Each decision
file names its base, producer, licence and update date in its front matter.

## Questions as git commands

| Question | Command |
|---|---|
| A decision | `cat decisions/<court>/<year>/<id>.md` |
| By ECLI or number | `git log --grep='^ECLI: ECLI:FR:CCASS:2019:C100123$' --stat` · `git log --grep='^Docket: 18-12345' --stat` |
| Decisions given in a window | `git log --since=2024-03-01 --until=2024-03-31 --format='%ad %an' --date=short` |
| Decisions applying art. 1240 of the Code civil | `cat judgments/france/national/acts/codes/code-civil/1240.jsonl` |
| The article as the court applied it | each overlay row's `law_commit` is the article's own version in france-law on the day decided |
| Who wrote a line? | `git blame decisions/...`: each line is credited to the decision (court and number) |

## Dates and authors
The commit date is the day the decision was given, at noon Paris (`In-force:` and `Decided:`). Git cannot hold dates
before 1970, so earlier decisions are committed on 1970-01-01, a second apart, and `In-force:` holds the real day. The
author is the court and the case number, at an address that cannot exist. These are court rulings, not laws: every
commit is `Kind: court`.

## What it does not show
Decisions DILA does not publish, and pseudonymisation is the courts' own.
"""
    docs['REPRODUCE.md'] = f"""# Reproducing this repository

Input: the DILA archives of the base(s) {G['bases']} (the full dump `Freemium_*_global_20250713-140000.tar.gz` and every
increment after it, up to {a.updated}), from https://echanges.dila.gouv.fr/OPENDATA/; france-law `main` for the overlay.

```
python tools/parse.py <archives> <parsed>
python tools/build.py {a.group} <parsed> <empty folder> --today {a.today} --updated {a.updated}
```

Gives {mark:,} decision commits and one documentation commit.
"""
    docs['LICENSE'] = ('Decisions: Licence Ouverte / Open Licence 2.0 (Etalab), https://www.etalab.gouv.fr/licence-ouverte-open-licence\n'
                       f'{credit}.\n\nThe arrangement of this repository (layout, commits, overlay, tools): CC0 1.0.\n')
    for f in ('parse.py', 'build.py', 'fr_index.py'):
        if (HERE / f).exists():
            docs[f'tools/{f}'] = (HERE / f).read_text(encoding='utf-8')
    ts = int(datetime.fromisoformat(a.today + 'T12:00:00+01:00').timestamp())
    msg = f'Documentation: README, LICENSE (Licence Ouverte 2.0 credit), REPRODUCE, corpus.json and the judgment overlay ({n_refs} citations)\n'.encode()
    w(b'commit refs/heads/main\nauthor Lexifina <corpus@lexifina.com> %d +0100\ncommitter %s %d +0100\n' % (ts, COMMITTER, ts))
    w(b'data %d\n%s\n' % (len(msg), msg))
    for p in sorted(docs):
        b = docs[p].encode('utf-8')
        w(b'M 100644 inline %s\ndata %d\n%s\n' % (p.encode('utf-8'), len(b), b))
    w(b'done\n')
    fi.stdin.close()
    if fi.wait():
        raise SystemExit('fast-import failed')
    marks = dict(l.split() for l in (out / '.git' / 'marks').read_text().splitlines())
    (out / '.git' / 'push-checkpoints').write_text(''.join(marks[f':{m}'] + '\n' for m in checkpoints))
    print(json.dumps({'group': a.group, 'decisions': mark, 'undated': len(undated), 'overlay_rows': n_refs,
                      'unlinked_liens': unlinked, 'raw_gb': round(raw / 1e9, 2), 'checkpoints': len(checkpoints)}), flush=True)


if __name__ == '__main__':
    main()
