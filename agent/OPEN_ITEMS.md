# Open items

- [x] 1. Oracle prices (2026-10-15: `lrg/protocols/navi_oracle.py`; see inconsistencies.md)
- [ ] 2. On-chain config reader
- [ ] 3. Turbos tick reader
- [ ] 4. Cetus/Bluefin failed reads
- [ ] 5. Rerun and recheck MBTC and SUI->USDC
- [ ] 6. Coverage gate
- [ ] 7. Validation against realized liquidations
- [ ] 8. Positions reader and signal 2

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
- [ ] 22. Rerun
