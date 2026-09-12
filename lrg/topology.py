"""
Topology: which routes exist between two assets, given the pool list.

No costs here. This layer answers "what paths are there, and which pools sit on
each leg" from the discovered pool CSV alone. Costing those paths needs pool
state and lives in `curves`.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from typing import Dict, List, Optional


class Routes:
    def __init__(self, csv_path: str, tokens: Dict[str, str]):
        """tokens maps symbol -> coin type for the protocol's asset set."""
        with open(csv_path) as fh:
            self.rows = list(csv.DictReader(fh))
        self.tokens_in = dict(tokens)
        self.name = {v: k for k, v in tokens.items()}
        for r in self.rows:
            self.name.setdefault(r["coin_type_a"], r["symbol_a"])
            self.name.setdefault(r["coin_type_b"], r["symbol_b"])
        self.tok = {v: k for k, v in self.name.items()}
        self.tok.update(self.tokens_in)

        self.edges = defaultdict(lambda: defaultdict(list))   # tok -> tok -> [rows]
        for r in self.rows:
            a, b = r["coin_type_a"], r["coin_type_b"]
            self.edges[a][b].append(r)
            self.edges[b][a].append(r)
        for a in self.edges:
            for b in self.edges[a]:
                self.edges[a][b].sort(key=lambda r: -float(r["tvl_usd"]))

    def get(self, src, dst, n=3, max_hops=3):
        """Routes as [{'path': [...], 'hops': k, 'legs': [{...}]}], shortest first."""
        s, d = self.tok[src], self.tok[dst]
        found = []

        def walk(node, chain):
            if len(chain) - 1 >= max_hops:
                return
            for nxt in self.edges[node]:
                if nxt in chain:
                    continue
                if nxt == d:
                    found.append(chain + [nxt])
                else:
                    walk(nxt, chain + [nxt])

        walk(s, [s])
        # shortest first; among equals, the one whose thinnest leg is deepest
        def depth(chain):
            return min(float(self.edges[chain[i]][chain[i + 1]][0]["tvl_usd"])
                       for i in range(len(chain) - 1))
        found.sort(key=lambda c: (len(c), -depth(c)))

        out = []
        for chain in found[:n]:
            legs = []
            for i in range(len(chain) - 1):
                a, b = chain[i], chain[i + 1]
                legs.append({
                    "from": self.name[a], "to": self.name[b],
                    "pools": [{"venue": p["venue"], "type": p["pool_type"],
                               "tvl": float(p["tvl_usd"]),
                               "fee": p["fee_tier"] or None,
                               "id": p["pool_id"]}
                              for p in self.edges[a][b]],
                })
            out.append({"path": [self.name[t] for t in chain],
                        "hops": len(chain) - 1, "legs": legs})
        return out

    def find(self, src, dst, n=3, max_hops=3):
        rs = self.get(src, dst, n, max_hops)
        if not rs:
            print(f"{src} → {dst}: no route within {max_hops} hops")
            return
        for i, r in enumerate(rs, 1):
            print(f"{i}. {' → '.join(r['path'])}   ({r['hops']} hop"
                  f"{'s' if r['hops'] > 1 else ''})")
            for leg in r["legs"]:
                print(f"     {leg['from']} → {leg['to']}")
                for p in leg["pools"]:
                    fee = f"{float(p['fee'])*100:.2f}%" if p["fee"] else "?"
                    print(f"       {p['venue']:<17}{p['type']:<10}"
                          f"${p['tvl']:>12,.0f}  fee {fee:<7}{p['id']}")
            print()


def load(csv_path: str, tokens: Dict[str, str]) -> Routes:
    return Routes(csv_path, tokens)
