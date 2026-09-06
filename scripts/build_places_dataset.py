#!/usr/bin/env python3
"""Regenerate the bundled offline place dataset from the GeoNames dump.

Run manually when refreshing the data; the outputs are committed to the repo.

    uv run python scripts/build_places_dataset.py

Source: https://download.geonames.org/export/dump/ - licensed CC-BY 4.0.
cities500 is the smallest population floor that contains villages, which the
feature requires: cities15000 omits places like Giethoorn (pop ~2,800).
"""

import csv
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://download.geonames.org/export/dump"
OUT = Path(__file__).resolve().parents[1] / "src" / "gpstitch" / "data"


def _fetch(name: str) -> bytes:
    url = f"{BASE}/{name}"
    print(f"fetching {url}", file=sys.stderr)
    with urllib.request.urlopen(url) as r:  # noqa: S310 - fixed, trusted host
        return r.read()


def build_cities() -> None:
    """Trim cities500 to the seven columns we use; ~12.9MB download -> ~2.3MB gz."""
    raw = _fetch("cities500.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read("cities500.txt").decode("utf-8")

    rows = 0
    with gzip.open(OUT / "cities.tsv.gz", "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n", quoting=csv.QUOTE_NONE, escapechar="\\")
        for line in text.splitlines():
            c = line.split("\t")
            if len(c) < 15:
                continue
            # GeoNames columns: 1 name, 4 lat, 5 lon, 8 country, 10 admin1,
            # 11 admin2, 14 population
            w.writerow([c[1], c[4], c[5], c[14], c[8], c[10], c[11]])
            rows += 1
    print(f"cities.tsv.gz: {rows:,} rows", file=sys.stderr)


def build_admin(source: str, out_name: str, gz: bool) -> None:
    """admin1CodesASCII.txt / admin2Codes.txt are 'key<TAB>name<TAB>...' files."""
    text = _fetch(source).decode("utf-8")
    rows = 0
    path = OUT / out_name
    f = gzip.open(path, "wt", encoding="utf-8", newline="") if gz else open(path, "w", encoding="utf-8", newline="")
    with f:
        for line in text.splitlines():
            c = line.split("\t")
            if len(c) < 2:
                continue
            f.write(f"{c[0]}\t{c[1]}\n")
            rows += 1
    print(f"{out_name}: {rows:,} rows", file=sys.stderr)


def build_countries() -> None:
    text = _fetch("countryInfo.txt").decode("utf-8")
    rows = 0
    with open(OUT / "countries.tsv", "w", encoding="utf-8", newline="") as f:
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            c = line.split("\t")
            if len(c) < 5:
                continue
            f.write(f"{c[0]}\t{c[4]}\n")  # ISO code, country name
            rows += 1
    print(f"countries.tsv: {rows:,} rows", file=sys.stderr)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    build_cities()
    build_admin("admin1CodesASCII.txt", "admin1.tsv", gz=False)
    build_admin("admin2Codes.txt", "admin2.tsv.gz", gz=True)
    build_countries()
    print("done", file=sys.stderr)


if __name__ == "__main__":
    main()
