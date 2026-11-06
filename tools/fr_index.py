"""france-law index for the overlay: LEGITEXT id -> text folder (from each folder's index.md), the slug of each code's
name -> folder, and every article file the repo has ever held (current or not) keyed by its file stem.
Writes fr_index.json.   python fr_index.py <france-law clone (bare is fine)> [ref]"""
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

repo = sys.argv[1]
ref = sys.argv[2] if len(sys.argv) > 2 else 'main'


def ascii_slug(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode()
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-').lower() or 'x'


def git(*a, inp=None):
    return subprocess.run(['git', '-C', repo, *a], input=inp, capture_output=True).stdout


head = git('ls-tree', '-r', '--name-only', ref).decode('utf-8').split('\n')
indexes = [p for p in head if p.endswith('/index.md')]
raw = git('cat-file', '--batch', inp=''.join(f'{ref}:{p}\n' for p in indexes).encode())
legitext, codes, i = {}, {}, 0
for p in indexes:
    nl = raw.index(b'\n', i)
    size = int(raw[i:nl].split()[2])
    blob = raw[nl + 1:nl + 1 + size].decode('utf-8', 'replace')
    i = nl + 2 + size
    folder = p.rsplit('/', 1)[0]
    m = re.search(r'(LEGITEXT\d{12})', blob)
    if m:
        legitext.setdefault(m.group(1), folder)
    t = re.match(r'#\s*(.+)', blob)
    if t and folder.startswith('codes/'):
        codes.setdefault(ascii_slug(t.group(1).strip()), folder)
        codes.setdefault(folder.split('/', 1)[1], folder)
ever = set(git('log', ref, '--format=', '--name-only', '--no-renames').decode('utf-8').split('\n')) | set(head)
arts = {}
for p in ever:
    if not p.endswith('.md') or p.endswith(('/index.md', 'README.md', 'REPRODUCE.md')):
        continue
    folder, _, name = p.rpartition('/')
    if folder:
        arts.setdefault(folder, {})[name[:-3]] = p
json.dump({'legitext': legitext, 'codes': codes, 'arts': arts}, open(Path(__file__).with_name('fr_index.json'), 'w', encoding='utf-8'), ensure_ascii=False)
print(len(legitext), 'texts with a LEGITEXT id;', len(codes), 'code names;', sum(len(v) for v in arts.values()), 'article files (ever)')
for k in ('code-civil', 'code-du-travail', 'code-de-procedure-civile', 'code-penal'):
    print(k, codes.get(k), len(arts.get(codes.get(k) or '', {})))
