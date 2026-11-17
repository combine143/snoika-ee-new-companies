#!/usr/bin/env python3
"""Find newly registered Estonian companies with a given EMTAK code.

Source: Äriregister open data (refreshed daily by RIK):
  - general data (JSON, ~230 MB zip / ~4.6 GB unzipped): registration date, EMTAK, email
  - persons on registry card (JSON, ~46 MB zip / ~1 GB unzipped): board members' names
Both files are STREAMED from the zip and never extracted; memory stays tiny and no big
disk space is needed. The persons file is only read when there are matches.

Examples
  # test on files you already downloaded
  python3 ariregister_new_companies.py --zip-path ettevotja_rekvisiidid__yldandmed_json.zip \
      --persons-zip-path ettevotja_rekvisiidid__kaardile_kantud_isikud_json.zip \
      --emtak 73111 --days 30 --csv new_companies.csv

  # daily run: download, filter last 3 days, send to an n8n webhook
  python3 ariregister_new_companies.py --emtak 73111 --days 3 \
      --webhook https://YOUR-N8N/webhook/ee-new-companies

Contact name: one active natural-person board member (role JUHL) gives that first name. With
2-3 board members the one matching the mailbox name is used, otherwise all are joined with
" ja ". A legal entity on the board or more than 3 people leaves first_name empty and the
email falls back to a company greeting.

Dedup is NOT done here: the --days window overlaps between runs on purpose, so the
receiving workflow must drop registry codes it has already processed.
"""
import argparse
import csv
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

BASE = "https://avaandmed.ariregister.rik.ee/sites/default/files/avaandmed/"
URL = BASE + "ettevotja_rekvisiidid__yldandmed.json.zip"
PERSONS_URL = BASE + "ettevotja_rekvisiidid__kaardile_kantud_isikud.json.zip"
CODE_RE = re.compile(r'"ariregistri_kood"\s*:\s*"?(\d+)')

# EMTAK code prefixes to skip when --emtak is empty. The register uses EMTAK 2025, so the
# old (EMTAK 2008) codes of the avoid list are translated, e.g.
#   62.01 -> 6210 (programming), 62.02 -> 622, 70.22 -> 7020, 74.90 -> 7499,
#   49.32 taxi -> 4932 + 4933 (ride-hailing / on-demand with driver), 96.02 -> 9621 + 9622,
#   68.1 -> 681 (incl. building project development), 46.1 -> 461, 46.90 -> 4690.
DEFAULT_EXCLUDE = (
    # holding, property and shell companies
    "642", "643", "649", "66", "681", "682", "701", "8299",
    # gig workers and solo trades
    "4932", "4933", "532", "4941", "41", "43", "812", "9602", "9621", "9622",
    # no fit
    "01", "02", "03", "05", "06", "07", "08", "09", "35", "36", "37", "38", "39",
    "84", "94", "97", "98", "99",
    # mixed (deprioritized): freelancers, consultants, trading shells
    "6210", "622", "7020", "7499", "461", "4690",
)


def pdate(s):
    try:
        return datetime.strptime(s, "%d.%m.%Y").date() if s else None
    except ValueError:
        return None


def iter_records(fh):
    """Yield company dicts from the pretty-printed top-level JSON array, one at a time."""
    buf, inrec = [], False
    for raw in fh:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not inrec:
            if line == "    {":
                inrec, buf = True, ["{"]
            continue
        if line in ("    },", "    }"):
            buf.append("}")
            inrec = False
            try:
                yield json.loads("".join(buf))
            except ValueError:
                pass
            continue
        buf.append(line)


def iter_wanted(fh, wanted):
    """Like iter_records, but only parses records whose registry code is in `wanted`."""
    left = set(wanted)
    buf, inrec, skip = [], False, None
    for raw in fh:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not inrec:
            if line == "    {":
                inrec, skip, buf = True, None, ["{"]
            continue
        if line in ("    },", "    }"):
            inrec = False
            if not skip:
                buf.append("}")
                try:
                    rec = json.loads("".join(buf))
                except ValueError:
                    rec = None
                if rec is not None:
                    left.discard(str(rec.get("ariregistri_kood")))
                    yield rec
                if not left:
                    return
            continue
        if skip is None:
            m = CODE_RE.search(line)
            skip = not (m and m.group(1) in left)
        if not skip:
            buf.append(line)


def nice_name(word):
    word = (word or "").strip()
    if word and (word.isupper() or word.islower()):
        return "-".join(p.capitalize() for p in word.split("-"))
    return word


def ascii_low(text):
    text = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def email_score(person, local):
    """2 points when a first-name token is in the mailbox name, 1 when a surname token is."""
    score = 0
    for tok in re.split(r"[\s-]+", ascii_low(person.get("eesnimi"))):
        if len(tok) >= 3 and tok in local:
            score = 2
    if score < 2:
        for tok in re.split(r"[\s-]+", ascii_low(person.get("nimi_arinimi"))):
            if len(tok) >= 3 and tok in local:
                score = max(score, 1)
    return score


def person_first(p):
    return nice_name((p["eesnimi"].split() or [""])[0])


def person_full(p):
    return (nice_name(p["eesnimi"]) + " " + nice_name(p.get("nimi_arinimi"))).strip()


def contact_from(rec, emails=()):
    """Return (first_name, full_name, board_members) for one record of the persons file.

    One active natural-person board member (role JUHL): that person. Several: the one whose
    name matches the mailbox (e.g. maarouf.sara@ -> Sara Maarouf), otherwise all of them
    joined with " ja " (up to 3). With more than 3 people only a mailbox match counts. A legal
    entity on the board, or no match among more than 3 people: empty.
    """
    people, seen = [], set()
    for p in rec.get("kaardile_kantud_isikud") or []:
        if p.get("isiku_roll") != "JUHL" or p.get("lopp_kpv"):
            continue
        key = (p.get("isiku_tyyp"), p.get("eesnimi"), p.get("nimi_arinimi"))
        if key in seen:
            continue
        seen.add(key)
        people.append(p)
    count = len(people)
    if not people or any(p.get("isiku_tyyp") != "F" or not p.get("eesnimi") for p in people):
        return "", "", count
    if count == 1:
        return person_first(people[0]), person_full(people[0]), count
    for em in emails:
        local = ascii_low(em.split("@")[0])
        scores = [email_score(p, local) for p in people]
        best = max(scores)
        if best > 0 and scores.count(best) == 1:
            p = people[scores.index(best)]
            return person_first(p), person_full(p), count
    if count > 3:
        return "", "", count
    firsts = [person_first(p) for p in people]
    first = " ja ".join([", ".join(firsts[:-1]), firsts[-1]] if count == 3 else firsts)
    return first, ", ".join(person_full(p) for p in people), count


def pick(rec, codes, forms, cutoff, exclude=()):
    g = rec.get("yldandmed") or {}
    if g.get("staatus") != "R" or g.get("oiguslik_vorm") not in forms:
        return None
    reg = pdate(g.get("esmaregistreerimise_kpv"))
    if not reg or reg < cutoff:
        return None
    acts = [a for a in (g.get("teatatud_tegevusalad") or [])
            if not a.get("lopp_kpv") and a.get("emtak_kood")]
    if not acts:
        return None
    main = next((a for a in acts if a.get("on_pohitegevusala")), acts[0])
    if codes:
        hit = [a for a in acts if a.get("emtak_kood") in codes]
    else:
        hit = [] if str(main["emtak_kood"]).startswith(tuple(exclude)) else [main]
    if not hit:
        return None
    emails, www = [], []
    for c in g.get("sidevahendid") or []:
        if c.get("lopp_kpv"):
            continue
        if c.get("liik") == "EMAIL" and c.get("sisu"):
            emails.append(c["sisu"].strip())
        elif c.get("liik") == "WWW" and c.get("sisu"):
            www.append(c["sisu"].strip())
    addr = [a for a in (g.get("aadressid") or []) if not a.get("lopp_kpv")]
    return {
        "registry_code": rec.get("ariregistri_kood"),
        "name": rec.get("nimi"),
        "legal_form": g.get("oiguslik_vorm"),
        "registered": reg.isoformat(),
        "emtak": hit[0]["emtak_kood"],
        "emtak_text": hit[0].get("emtak_tekstina"),
        "email": emails[0] if emails else "",
        "other_emails": emails[1:],
        "website": www[0] if www else "",
        "address": (addr[0].get("aadress_ads__ads_normaliseeritud_taisaadress") if addr else "") or "",
        "register_url": f"https://ariregister.rik.ee/eng/company/{rec.get('ariregistri_kood')}",
        "first_name": "",
        "contact_name": "",
        "board_members": 0,
    }


def download(url):
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    print(f"downloading {url.rsplit('/', 1)[-1]} ...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "snoika-ee-new-companies/1.0"})
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp.name, "wb") as out:
        shutil.copyfileobj(resp, out)
    return tmp.name


def first_json(zf):
    return next(n for n in zf.namelist() if n.endswith(".json"))


def add_contacts(results, persons_path):
    wanted = {str(r["registry_code"]) for r in results}
    emails = {str(r["registry_code"]): [r["email"]] + list(r["other_emails"]) for r in results if r["email"]}
    found = {}
    with zipfile.ZipFile(persons_path) as zf:
        with zf.open(first_json(zf)) as fh:
            for rec in iter_wanted(fh, wanted):
                code = str(rec.get("ariregistri_kood"))
                found[code] = contact_from(rec, emails.get(code, ()))
    for r in results:
        first, full, count = found.get(str(r["registry_code"]), ("", "", 0))
        r["first_name"], r["contact_name"], r["board_members"] = first, full, count
    named = sum(1 for r in results if r["first_name"])
    print(f"first name found for {named} of {len(results)} companies", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emtak", default="",
                    help="comma-separated EMTAK codes; empty = every code except the avoid list")
    ap.add_argument("--exclude", default=",".join(DEFAULT_EXCLUDE),
                    help="comma-separated EMTAK prefixes to skip when --emtak is empty")
    ap.add_argument("--no-exclude", action="store_true", help="do not skip any EMTAK code")
    ap.add_argument("--days", type=int, default=3, help="registered within last N days")
    ap.add_argument("--forms", default="OÜ", help="comma-separated legal forms (OÜ,UÜ,AS,...)")
    ap.add_argument("--zip-path", help="use a local general-data zip instead of downloading")
    ap.add_argument("--persons-zip-path", help="use a local persons zip instead of downloading")
    ap.add_argument("--no-persons", action="store_true", help="skip looking up board members")
    ap.add_argument("--csv", help="write results to this CSV file")
    ap.add_argument("--webhook", help="POST the results as JSON to this URL (e.g. n8n)")
    a = ap.parse_args()

    codes = {c.strip() for c in a.emtak.split(",") if c.strip()}
    exclude = () if a.no_exclude else tuple(p.strip() for p in a.exclude.split(",") if p.strip())
    forms = {f.strip() for f in a.forms.split(",")}
    cutoff = date.today() - timedelta(days=a.days)

    tmp_files = []
    path = a.zip_path
    if not path:
        path = download(URL)
        tmp_files.append(path)

    results, scanned = [], 0
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open(first_json(zf)) as fh:
                for rec in iter_records(fh):
                    scanned += 1
                    row = pick(rec, codes, forms, cutoff, exclude)
                    if row:
                        results.append(row)

        results.sort(key=lambda r: r["registered"], reverse=True)
        scope = ",".join(sorted(codes)) if codes else f"all except {len(exclude)} avoided prefixes"
        print(f"scanned {scanned} companies, matched {len(results)} "
              f"(EMTAK {scope}, forms {','.join(sorted(forms))}, since {cutoff})",
              file=sys.stderr)

        if results and not a.no_persons:
            ppath = a.persons_zip_path
            if not ppath:
                ppath = download(PERSONS_URL)
                tmp_files.append(ppath)
            add_contacts(results, ppath)
    finally:
        for f in tmp_files:
            os.unlink(f)

    if a.csv:
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()) if results else ["registry_code"])
            w.writeheader()
            for r in results:
                r = dict(r, other_emails="; ".join(r["other_emails"]))
                w.writerow(r)
    if a.webhook:
        req = urllib.request.Request(
            a.webhook, data=json.dumps({"companies": results}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(f"webhook responded {resp.status}", file=sys.stderr)
    if not a.csv and not a.webhook:
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
