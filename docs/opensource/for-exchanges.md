---
description: "For exchanges: how TRA reaches a venue, what is verified before a venue is listed as run end to end, how orders identify themselves, and how to reach Bijective about integration."
---

# For exchanges

This page is for exchanges and their integration and partnership teams. It says how this software
reaches a venue, what we check before we say a venue works, and how orders sent through it identify
themselves.

## How it reaches a venue

The software is open source and runs on the user's own machine, with the user's own keys. It reaches
every venue through [ccxt](https://github.com/ccxt/ccxt), the open-source library most trading
software uses, so a venue that ccxt supports is reachable without code of its own. Where an exchange
differs from ccxt's unified calls, the difference lives in one small module per concern
(`trading/venues/` in the source), never in the core.

Every order it sends carries a client order id of its own, so the same decision cannot be sent
twice. Beyond that:

- an entry is a limit order, immediate-or-cancel for a taker and post-only for a maker, priced
  within a bound of the venue's own book read at the moment of sending;
- the stop and take profit go out with the entry, or follow it at once as reduce-only orders where
  the venue does not take them together;
- closing a futures position is a reduce-only order, which can only shrink it;
- an entry is refused before it is sent when the account cannot carry it, the book is stale, the
  position mode is hedged, or the leverage and margin mode cannot be confirmed by reading them
  back.

## What "run end to end" means

A venue is listed as run end to end only after the same script has passed on its practice
environment: an entry with its exits, the position read back, a second entry refused, a reduce-only
close that leaves nothing resting, a post-only order cancelled at its timeout, a position closed by
the venue's own exit, and the kill path. The current list, with what each venue needed, is on
[Venues](/opensource/venues) and [Exchange accounts and keys](/opensource/exchange-keys).

## Broker and channel ids

Orders do not carry a Bijective tag today. Where ccxt adds its own identifier, that is listed on
[Venues](/opensource/venues#broker-tags) for every venue.

Bijective intends to take part in exchanges' broker programmes as a software vendor. When an exchange
issues an id, it will be:

- set the way the exchange documents it, through the venue's own field;
- shown by `tra check`, so every user sees it;
- replaceable or removable by the user in one line of configuration;
- never a cost to the user beyond the exchange's own fee. A programme that charges users extra for
  the vendor's share is one we do not use as a default.

The venue is named on this page first, and only after the agreement is in place.

## What helps an integration

| From the exchange | Why |
|---|---|
| a practice environment reachable through the same API as live | every venue is run there first, by us and by every user |
| the stop and take profit accepted with the entry, or as reduce-only orders | a position must never stand without its exits |
| leverage, margin mode and position mode readable through the API | the desk confirms them before every first order instead of trusting a reply |
| a timestamp on the order book | an order is not priced against a book older than 30 seconds |
| a broker or channel id that adds no cost to the user | see above |

## Contact

Integration and partnership questions: [bijectivelabs.dev/contact](https://www.bijectivelabs.dev/contact).
Technical issues with a venue are best opened on
[GitHub](https://github.com/BijectiveLabs/trading-research-agents/issues), where users can follow
them.
