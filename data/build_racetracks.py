#!/usr/bin/env python3
"""
Regenerates data/racetracks.json from Wikidata: every item that is an instance
(or subclass instance) of Q2338524 "motorsport racing track" with coordinates (P625).
Run this occasionally to pick up newly-added circuits. Needs internet; the resulting
racetracks.json is what gopro_videofixer.py actually reads at runtime (fully offline).
"""

import json
import re
import subprocess
from pathlib import Path

QUERY = """
SELECT ?item ?itemLabel ?coord WHERE {
  ?item wdt:P31/wdt:P279* wd:Q2338524 .
  ?item wdt:P625 ?coord .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""

OUT_PATH = Path(__file__).parent / "racetracks.json"

# Wikidata coordinate corrections for entries found to be wrong (e.g. copy-paste
# errors) during spot-checking. Verified against the track's own website/other
# sources -- see git history / conversation for how each was found.
COORD_FIXES = {
    # Q105636202 "Tor Łódź": Wikidata's P625 (52.4178, 16.8058) is literally Tor
    # Poznań's coordinate, ~200km away -- P131 correctly says Gmina Stryków (near
    # Łódź) so only the coordinate is wrong. Corrected from the track's own site
    # (tor-lodz.pl/kontakt): 51°52'38.9"N, 19°32'01.0"E.
    "Q105636202": (51.877472, 19.533611),
}


def main():
    proc = subprocess.run(
        [
            "curl", "-s", "-G", "https://query.wikidata.org/sparql",
            "--data-urlencode", "format=json",
            "--data-urlencode", f"query={QUERY}",
            "-H", "User-Agent: gopro-videofixer/1.0 (personal script)",
        ],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(proc.stdout)

    seen = {}
    for row in data["results"]["bindings"]:
        qid = row["item"]["value"].rsplit("/", 1)[-1]
        name = row["itemLabel"]["value"]
        if re.match(r"^Q\d+$", name):
            continue  # no real label available
        m = re.match(r"Point\(([-\d.]+) ([-\d.]+)\)", row["coord"]["value"])
        if not m:
            continue
        lon, lat = float(m.group(1)), float(m.group(2))
        if qid in COORD_FIXES:
            lat, lon = COORD_FIXES[qid]
        seen[qid] = {"name": name, "lat": lat, "lon": lon}

    tracks = sorted(seen.values(), key=lambda t: t["name"])
    OUT_PATH.write_text(json.dumps(tracks, ensure_ascii=False, separators=(",", ":")))
    print(f"wrote {len(tracks)} racetracks to {OUT_PATH}")


if __name__ == "__main__":
    main()
