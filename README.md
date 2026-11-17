# snoika-ee-new-companies

Every day at 12:00 UTC a GitHub Action downloads the Äriregister open-data file (general data, JSON),
streams it without unpacking, picks Estonian companies registered in the last few days that are not on the avoid list (or have the given EMTAK code), looks up the board member's first name in the "persons on registry card" file (always one person: the sole
active board member, or with 2+ members the one whose name matches the email address, else the first listed) and POSTs them to an n8n webhook. n8n removes ones it has already seen and stores new ones in
the data table `ee_new_companies`.

## Setup
1. Add the repository secret `N8N_WEBHOOK_URL` (Settings, Secrets and variables, Actions).
2. Check the run: Actions tab, workflow "EE new companies", Run workflow.

## Change what is searched
- By default every new OÜ is taken EXCEPT those whose main EMTAK code is on the avoid list (`DEFAULT_EXCLUDE` in
  the script: holding/property/shell companies, gig workers and solo trades, no-fit sectors, freelancer-type
  IT/consulting/trading codes). The register uses EMTAK 2025, so the list is translated from the old codes.
- To target specific codes instead, pass them in the manual run form (`emtak`, e.g. `73111`); the avoid list is then ignored.
- Window is 3 days; days overlap on purpose and n8n deduplicates by registry code.
- Legal forms: `--forms` (default `OÜ`).
- Contact name: fields `first_name`, `contact_name`, `board_members`; `--no-persons` skips the lookup.

## Run locally
    python ariregister_new_companies.py --zip-path file.zip --emtak 73111 --days 30 --csv out.csv

Source data: https://avaandmed.ariregister.rik.ee/en/downloading-open-data (public data; files refresh once a day).
