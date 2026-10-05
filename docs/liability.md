# Where the responsibility sits

This is software you run. It is published under the MIT License, which provides it without
warranty of any kind, and nothing here changes that. The lines below say, in plain
words, where the decisions are made and by whom, so that nobody is surprised later.

- **The sandbox is the default.** Replay, paper and shadow run against a simulated venue. No
  key is read, no order leaves the machine, and every figure the gates produce is a figure
  about recorded data. Most of what this project is for happens here.
- **Live orders need what you supply.** A trade key you created on the venue and placed in your
  environment, a signed `MANDATE.md` for venue mode, and a ladder you promoted to the canary tier
  with evidence and your signature. The package never holds a key, never connects to a venue
  unless you run it in live or venue mode, and never operates execution for anyone.
- **Promotion is a human act.** Every step up the ladder is a receipt naming the evidence and
  the operator who signed it. Steps down happen on a reason, and a role may take them. No role,
  no model and no tool on the MCP surface can promote.
- **The caps are yours.** The canary limits have no defaults; the session refuses to start until
  you have written them down. What they should be depends on what a defect would cost you, and
  only you know that.
- **A model may decide; code executes and refuses.** The controls, the caps, the ladder and the
  kill switch are code, tested, and they fail closed. An agent with full access acts through
  them. If they refuse, the refusal is recorded and nothing is sent.
- **Nothing here is advice.** The example cores are examples of the mechanism, measured and
  closed in the research this project descends from. A verdict from a gate is a statement about
  a dataset and a registration, not a recommendation to trade.

`docs/boundaries.md` is the other half of this page: what the software will not do, by design,
with the reason beside each one.

If you deploy this against capital, you are the operator: you chose the venue, the key, the
caps, the tier and the moment. The software's job is to make each of those choices explicit,
recorded and hard to make by accident.
