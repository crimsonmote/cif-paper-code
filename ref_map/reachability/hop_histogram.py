"""Histogram of minimum citation hops from the seed set (SM fig. S5).

Counts Work nodes by their minimum-hop property, written to the graph by the
reachability step (node_reachability.py), together with the total number of
works. Hop 0 is the seed set itself; works without the property are not
reachable from any seed paper.

Writes a JSON file that the figure script reads:
    {"hops_property": ..., "total_works": N, "reachable_works": R,
     "histogram": {"0": n0, "1": n1, ...}}

Credentials come from NEO4J_URI / NEO4J_USER / NEO4J_PASS or the OS keyring.
"""
from __future__ import annotations

import argparse
import json
import os

from neo4j import GraphDatabase

from ref_map.common.neo4j_tokens import validate_property_key
from ref_map.export.neo4j_export import _resolve_neo4j_config


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hops-property", default="startup_min_hops",
                    help="Work property holding the minimum hop count")
    ap.add_argument("--label", default="Work")
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "output", "hop_histogram.json"))
    args = ap.parse_args()
    prop = validate_property_key(args.hops_property)

    uri, user, password = _resolve_neo4j_config()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    with driver.session() as session:
        total = session.run(f"MATCH (w:{args.label}) RETURN count(w) AS n").single()["n"]
        rows = session.run(
            f"MATCH (w:{args.label}) WHERE w.`{prop}` IS NOT NULL "
            f"RETURN toInteger(w.`{prop}`) AS h, count(*) AS n ORDER BY h")
        hist = {str(r["h"]): r["n"] for r in rows}
    driver.close()

    out = {"hops_property": prop, "total_works": total,
           "reachable_works": sum(hist.values()), "histogram": hist}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=1)
    print(f"wrote {args.out}: {out['reachable_works']:,} of {total:,} works reachable; "
          f"hops 0..{max(map(int, hist))}")


if __name__ == "__main__":
    main()
