# snoika-ee-new-companies

Every day at 12:00 UTC a GitHub Action downloads the Äriregister open-data file (general data, JSON),
streams it without unpacking, picks Estonian companies registered in the last few days with the given
EMTAK code, looks up the board member's first name in the "persons on registry card" file (only when the
company has exactly one active natural-person board member) and POSTs them to an n8n webhook. n8n removes ones it has already seen and stores new ones in
the data table `ee_new_companies`.

## Setup
1. Add the repository secret `N8N_WEBHOOK_URL` (Settings, Secrets and variables, Actions).
2. Check the run: Actions tab, workflow "EE new companies", Run workflow.

## Change what is searched
- Default EMTAK is `73111` (advertising agencies). Change it in the manual run form or edit `daily.yml`.
- Window is 3 days; days overlap on purpose and n8n deduplicates by registry code.
- Legal forms: `--forms` (default `OÜ`).
- Contact name: fields `first_name`, `contact_name`, `board_members`; `--no-persons` skips the lookup.

## Run locally
    python ariregister_new_companies.py --zip-path file.zip --emtak 73111 --days 30 --csv out.csv

Source data: https://avaandmed.ariregister.rik.ee/en/downloading-open-data (public data; files refresh once a day).
