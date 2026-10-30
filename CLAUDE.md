# Working rules for this repository

These rules cover the Trakt Suggested engine, the police catalogues and the refresh workflows. They hold for every change here.

## Answers and writing
- Lead advice and analysis with the verdict in the first two sentences, including any uncertainty that changes it. Then give the evidence and next step.
- Write in clear UK English, with no Oxford commas.
- Separate verified results from hypotheses and from things not yet tested.

## Changes and verification
- Read the actual files before editing, and make the smallest change that fixes the cause.
- Prove each change on the real path: a workflow run, the published rows or the backend's own logs. A passing test or a saved file is not enough on its own.
- Preview a ranking or filter change against the live rows before merging to main. Merge only without preview files.
- Remove preview and diagnostic branches once their result is used, and disable their workflows.
- Keep profile changes scoped. Ste's and Jenny's rules are separate, and a change for one profile must not alter the other's rows.

## Efficiency
- Batch independent reads and checks into one step.
- Diagnose from evidence first, then change.
- Report failures and anything not checked, without overstating the cause.

## Keep private data out of this repository
- Do not commit personal details, tokens or keys. Secrets live only in GitHub Actions secrets.
