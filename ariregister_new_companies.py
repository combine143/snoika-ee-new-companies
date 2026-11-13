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

Contact name: the first name is filled only when the company has exactly one active
natural-person board member (role JUHL). With several board members, or a legal entity on
the board, first_name stays empty and the email falls back to a company greeting.

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
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

BASE = "https://avaandmed.ariregister.rik.ee/sites/default/files/avaandmed/"
URL = BASE + "ettevotja_rekvisiidid__yldandmed.json.zip"
PERSONS_URL = BASE + "ettevotja_rekvisiidid__kaardile_kantud_isikud.json.zip"
CODE_RE = re.compile(r'"ariregistri_kood"\s*:\s*"?(\d+)')


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


def contact_from(rec):
    """Return (first_name, full_name, board_members) for one record of the persons file."""
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
    if count == 1 and people[0].get("isiku_tyyp") == "F" and people[0].get("eesnimi"):
        p = people[0]
        first = nice_name((p["eesnimi"].split() or [""])[0])
        full = (nice_name(p["eesnimi"]) + " " + nice_name(p.get("nimi_arinimi"))).strip()
        return first, full, count
    return "", "", count


def pick(rec, codes, forms, cutoff):
    g = rec.get("yldandmed") or {}
    if g.get("staatus") != "R" or g.get("oiguslik_vorm") not in forms:
        return None
    reg = pdate(g.get("esmaregistreerimise_kpv"))
    if not reg or reg < cutoff:
        return None
    acts = [a for a in (g.get("teatatud_tegevusalad") or []) if not a.get("lopp_kpv")]
    hit = [a for a in acts if a.get("emtak_kood") in codes]
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
    found = {}
    with zipfile.ZipFile(persons_path) as zf:
        with zf.open(first_json(zf)) as fh:
            for rec in iter_wanted(fh, wanted):
                found[str(rec.get("ariregistri_kood"))] = contact_from(rec)
    for r in results:
        first, full, count = found.get(str(r["registry_code"]), ("", "", 0))
        r["first_name"], r["contact_name"], r["board_members"] = first, full, count
    named = sum(1 for r in results if r["first_name"])
    print(f"first name found for {named} of {len(results)} companies", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emtak", default="73111", help="comma-separated EMTAK codes")
    ap.add_argument("--days", type=int, default=3, help="registered within last N days")
    ap.add_argument("--forms", default="OÜ", help="comma-separated legal forms (OÜ,UÜ,AS,...)")
    ap.add_argument("--zip-path", help="use a local general-data zip instead of downloading")
    ap.add_argument("--persons-zip-path", help="use a local persons zip instead of downloading")
    ap.add_argument("--no-persons", action="store_true", help="skip looking up board members")
    ap.add_argument("--csv", help="write results to this CSV file")
    ap.add_argument("--webhook", help="POST the results as JSON to this URL (e.g. n8n)")
    a = ap.parse_args()

    codes = {c.strip() for c in a.emtak.split(",")}
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
                    row = pick(rec, codes, forms, cutoff)
                    if row:
                        results.append(row)

        results.sort(key=lambda r: r["registered"], reverse=True)
        print(f"scanned {scanned} companies, matched {len(results)} "
              f"(EMTAK {','.join(sorted(codes))}, forms {','.join(sorted(forms))}, since {cutoff})",
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
