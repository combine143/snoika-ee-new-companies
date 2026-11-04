#!/usr/bin/env python3
"""Find newly registered Estonian companies with a given EMTAK code.

Source: Äriregister open data (general data, JSON, refreshed daily by RIK).
The zip is ~230 MB and the JSON inside is ~4.6 GB, so the file is STREAMED from the
zip and never extracted; memory stays tiny and no big disk space is needed.

Examples
  # test on a file you already downloaded
  python3 ariregister_new_companies.py --zip-path ettevotja_rekvisiidid__yldandmed_json.zip \
      --emtak 73111 --days 30 --csv new_companies.csv

  # daily run: download, filter last 3 days, send to an n8n webhook
  python3 ariregister_new_companies.py --emtak 73111 --days 3 \
      --webhook https://YOUR-N8N/webhook/ee-new-companies

Dedup is NOT done here: the --days window overlaps between runs on purpose, so the
receiving workflow must drop registry codes it has already processed.
"""
import argparse
import csv
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

URL = ("https://avaandmed.ariregister.rik.ee/sites/default/files/avaandmed/"
       "ettevotja_rekvisiidid__yldandmed.json.zip")


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
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emtak", default="73111", help="comma-separated EMTAK codes")
    ap.add_argument("--days", type=int, default=3, help="registered within last N days")
    ap.add_argument("--forms", default="OÜ", help="comma-separated legal forms (OÜ,UÜ,AS,...)")
    ap.add_argument("--zip-path", help="use a local zip instead of downloading")
    ap.add_argument("--csv", help="write results to this CSV file")
    ap.add_argument("--webhook", help="POST the results as JSON to this URL (e.g. n8n)")
    a = ap.parse_args()

    codes = {c.strip() for c in a.emtak.split(",")}
    forms = {f.strip() for f in a.forms.split(",")}
    cutoff = date.today() - timedelta(days=a.days)

    tmp = None
    path = a.zip_path
    if not path:
        tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp.close()
        print("downloading registry file...", file=sys.stderr)
        req = urllib.request.Request(URL, headers={"User-Agent": "snoika-ee-new-companies/1.0"})
        with urllib.request.urlopen(req, timeout=300) as resp, open(tmp.name, "wb") as out:
            shutil.copyfileobj(resp, out)
        path = tmp.name

    results, scanned = [], 0
    try:
        with zipfile.ZipFile(path) as zf:
            name = next(n for n in zf.namelist() if n.endswith(".json"))
            with zf.open(name) as fh:
                for rec in iter_records(fh):
                    scanned += 1
                    row = pick(rec, codes, forms, cutoff)
                    if row:
                        results.append(row)
    finally:
        if tmp:
            os.unlink(tmp.name)

    results.sort(key=lambda r: r["registered"], reverse=True)
    print(f"scanned {scanned} companies, matched {len(results)} "
          f"(EMTAK {','.join(sorted(codes))}, forms {','.join(sorted(forms))}, since {cutoff})",
          file=sys.stderr)

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
