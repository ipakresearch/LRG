# Open items

- [x] 1. Oracle prices (2026-10-15: `lrg/protocols/navi_oracle.py`; see inconsistencies.md)
- [x] 2. On-chain config reader (run.fetch_navi_live: LTV, LT, LB, caps, treasury factor, borrowable; E-Mode in positions.read_state)
- [x] 3. Turbos tick reader (see 13)
- [x] 4. Cetus/Bluefin failed reads (empty pools, see 14)
- [x] 5. Rerun and recheck MBTC and SUI->USDC (see 22)
- [ ] 6. Coverage gate (coverage status per pair and run-level share exist; no threshold that withholds a run yet)
- [ ] 7. Validation against realized liquidations (bonus split checked on 40 events; capacity not yet validated)
- [x] 8. Positions reader and signal 2

## Review 2026-10-15 (see agent/REVIEW_2026-10-15.md, agent/FIX_PROMPTS.md)

- [x] 9. Liquidation bonus split (per-reserve treasury_factor; the premise of finding 1 was reversed, see inconsistencies.md)
- [x] 10. Coin-type keying
- [x] 11. Swap direction
- [x] 12. Pool universe (DeepBook, Aftermath, discovery; TVL now on chain)
- [x] 13. Turbos reader (tick_map bitmap + keyed reads)
- [x] 14. Empty pools (EmptyPool status; zero depth, not failed)
- [x] 15. Read robustness (strict gql, truncation checks + re-read, reserve cap, leg slots; tests/)
- [x] 16. Curve interpolation (cost interpolation + exact refinement; saturation flagged)
- [x] 17. Path ranking (paths by route cost at $1k/$10k/$250k; pools per leg by output at $10k)
- [x] 18. Matrix completeness and borrowable debts (open: close factor applies to collateral value, see inconsistencies.md)
- [x] 19. Coverage flag (complete/incomplete per pair; overtaking test; coverage share)
- [x] 20. Stress corrections (borrowing-power split, CF cap on collateral, E-Mode, beta, unpriced/bad debt, 5% grid, LST depeg)
- [x] 21. LST mint exit (SUI->LST mint venue; redemption not modelled by decision)
- [x] 22. Rerun (2026-10-16; see inconsistencies.md)
- [x] 23. Stress materiality floors (dust positions < $100, pair Q >= $1,000, first failing shock in stress_breaks.csv, monotonicity flag, Q* over material debts; see inconsistencies.md)
- [x] 24. Stress rerunnable from a saved run (run.py --from-run; matrix.json and positions.json.gz saved per run)
