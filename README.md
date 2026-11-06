# French case law: the tribunaux administratifs and other administrative courts

Part of [lexifina/global-law](https://github.com/lexifina/global-law), one git history for every country's law: this is the case law of the tribunaux administratifs and other administrative courts, France, which global-law reads at `france/tribunaux-administratifs/caselaw/`.

Every decision of the tribunaux administratifs and other administrative courts that DILA publishes with its text, as a git history: one file per decision, one commit
per decision, dated the day it was decided. 175,843 decisions from 1873-02-08 to 2026-11-05.

## Source and licence

Source: DILA, base JADE; producer: Conseil d'État; Licence Ouverte 2.0 (Etalab); last update 2026-11-05.

The decisions are the open data of the Conseil d'État, distributed by the Direction de l'information légale et
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
