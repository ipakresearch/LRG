"""
LRG — Liquidation Route Graph.

A route-adequacy framework for lending markets: what a liquidation actually
costs to execute on-chain, and what that implies for risk parameters.

Layers, bottom up:

    pool          the adapter interface every venue implements
    coins         coin identity: normalized coin types
    clmm          concentrated liquidity (Cetus, Bluefin, Momentum, Turbos)
    venues        weighted AMM (Aftermath), order book (DeepBook), LST minting
    discovery     building the pool universe from public indexers
    book          pool state, fetched once and cached
    topology      which routes exist between two assets
    curves        cost composition across legs, split execution
    graph         the route graph and its network view
    matrix        capacity matrix and parameter curves
    analysis      reading the matrix

Protocol-specific inputs — asset sets, risk parameters, venue selection — live
in `lrg.protocols` and are passed in, never imported by the engine.

    from lrg import graph, matrix, analysis
    from lrg.protocols import navi

    data = graph.build("data/navi_pools.csv", navi.COIN_TYPES, navi.VENUES)
    M = matrix.build("data/navi_pools.csv", navi.COIN_TYPES, navi.ASSETS,
                     navi.VENUES)
    analysis.summary(M)

Copyright 2026 Ipak Systems. Licensed under Apache-2.0.
"""

__version__ = "0.1.0"
