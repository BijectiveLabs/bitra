---
description: "The documented failures this software is built against: language models trading real money, wrong numbers in financial answers, instructions hidden in data, agents with authority over funds, and automation without pre-trade controls. Each with its source and what the software does about it."
---

# Why it is built this way

This software did not start from a trend. It started from what went wrong when language models
were given money, numbers and authority, as reported by the people who ran the tests, by
researchers and by regulators, and from what we found building it ourselves. Each design decision
here answers one of those problems. None of them is an argument against language models: they read
a market's context well and quickly, and that is what they are used for here. The problems come
from asking them to do what they are not reliable at, and from giving them authority nothing
checks.

Every figure on this page is the source's own, with the date it was reported. The sources are
listed at the end.

## Language models trading real money

**What happened.** In Alpha Arena Season 1, run by the research lab Nof1 in October 2025, six
frontier models were each given $10,000 to trade crypto perpetual futures on Hyperliquid on their
own. When it ended on November 3, 2025, four of the six had lost money: Claude Sonnet 4.5 finished
at $5,799, Gemini 2.5 Pro at $5,445, Grok at $4,208 and GPT-5 at $4,126. Qwen3 Max finished at
$12,231 and DeepSeek at $10,489.

**What the software does.** A model proposes a direction, a setup and the levels that bound the
idea. It does not size the trade, set the leverage, decide whether the trade is worth its cost or
send the order: code does, from limits the operator declared, and every gate that refuses records
why. Whether the model's side is any good is measured, not assumed: `tra hindsight` replays every
decision against its exact reverse. Desks start on a demo or testnet API, and real capital needs a
signed mandate and evidence on the ladder.

## Trading too much

**What happened.** In the Moscow Exchange's AI hackathon (May 13 to June 10, 2026), 50 teams ran
trading agents on 1 million virtual rubles each. The worst agent made almost 7,700 trades with a
turnover of 1.8 billion virtual rubles, and commissions ate a significant part of its capital. The
exchange's own conclusion: more trading did not mean more profit, and the better agents traded far
less, under risk management.

**What the software does.** The model is not asked unless the market has moved enough to be worth
asking (`[trigger]`). A trade is refused when its expected edge does not clear its full round trip
by the multiple the operator declared (`[targets]`), when the day's count of round trips is reached,
or when a cooldown since the last entry has not passed. The cost of every trade is priced before it
is sent, fees, spread, slippage and funding together.

## Wrong numbers in financial answers

**What happened.** Saturn, which builds AI software for financial advisers, tested 18
popular AI models on 121 financial advice questions in September 2026. On average the models were
wrong 57% of the time; on harder questions 88%, with some models wrong 99% of the time. Free models
were wrong in 63% of cases and paid ones in 49%. The failures reported include miscalculated pension
tax and rules for debt and student loans that do not exist. Earlier, in November 2023, Patronus AI's
FinanceBench found that GPT-4-Turbo used with a retrieval system answered 81% of its questions about
public companies' filings incorrectly or refused to answer them.

**What the software does.** No model calculates. Code pulls the data, computes every figure the
models read (returns, volatility, market structure, the regime, the cost of a trade) and records it.
A model answers in a closed schema and cites the figures it relied on by name and value; code checks
each citation against the brief and discards the ones that do not hold, and a deciding answer with
nothing left is refused. Sizes, stops, costs and limits are arithmetic in code, the same every time.

## Instructions hidden in data

**What happened.** In 2023 Greshake and colleagues showed that instructions planted in content a
model reads later, a web page, a document, an email, are followed as if the user had given them:
indirect prompt injection. In 2025 researchers from Princeton University and the Sentient
Foundation showed the same against crypto agents built on ElizaOS, through their memory: a message
planted in an agent's stored history could lead it to send funds to an attacker, and prompt-based
defences gave only limited protection once that stored context was corrupted. OWASP ranks prompt injection first among the risks of LLM
applications (LLM01:2025).

**What the software does.** The role that proposes a direction reads numbers computed by code, not
text from outside. Headlines and notes reach only the reading analysts, whose labels do not decide
anything, and every prompt says that text inside a brief is material to read, never an instruction
to follow. There is no memory a model writes for itself: what a role sees is built by code for each
call and recorded. And whatever a model says, it cannot send, cancel or change an order, or reach a
key.

## Agents with authority over money

**What happened.** On November 29, 2024, an AI agent called Freysa, set up as a game never to release
its prize pool, was talked into sending about $47,000 by a participant's crafted message (OECD AI
Incidents Monitor). On March 18, 2025, an attacker queued fraudulent prompts into the autonomous
crypto agent AIXBT through its dashboard, and the agent sent 55.5 ETH from its wallet (AI Incident
Database, incident 1003).

**What the software does.** No role holds a key or can move funds. Keys are read from the
environment by the one module that sends orders, are never in a prompt, a brief or a record, and any
message that reaches a record has them removed first. An order leaves only through that module,
after every gate, under a mandate a named person signed. The key guide asks for trading permissions
only, on every venue, and before a live tier session a Bybit key that can move funds is refused.

## Automation without pre-trade controls

**What happened.** On August 1, 2012, a router at Knight Capital running discontinued code sent more
than 4 million orders in 45 minutes trying to fill 212 retail orders, and the firm lost over $460
million. In 2013 the SEC fined Knight $12 million in the first case under its market access rule,
for not having adequate safeguards to limit the risks of its access to the markets.

**What the software does.** One decision sends one order, under a client order id of its own, so it
cannot be sent twice. Nothing is retried blindly. Every order is capped (`max_notional`), the desk
sends at most a declared number of orders a minute, no new entry is taken past a daily loss or a
drawdown limit, and the kill switch flattens and stops. Before an order goes out, leverage and margin
mode are read back from the venue, and an order is refused against a book older than 30 seconds.

## Who stays responsible

**What regulators say.** ESMA's public statement of May 30, 2024 on AI in investment services says a
firm's decisions remain the responsibility of its management body whether they are taken by people
or by AI tools. IOSCO's consultation report of March 2025 on AI in capital markets names
hallucination, explainability, and autonomy and control among the risks that may emerge.

**What the software does.** A named person signs the mandate before any order is sent. Every
decision is written down with the figures it rested on, what each gate decided and why, and can be
recomputed from the raw inputs. The auditors report and propose; only a person changes the
configuration.

## What we found building it

The same habit applied to our own runs on demo and testnet APIs found problems no specification
mentions:

| Found | What changed |
|---|---|
| Validators asked side by side voted their assignment: pro supported 63 times in 64, contra opposed 61 times in 63, and a neutral asked alone abstained in 33 of 37 cycles | pro and contra build their cases apart and neutral judges the checked cases ([the validators](/opensource/cycle#the-validators)) |
| Reading analysts cited figures by names the brief did not use, 27 times in four days | a naming rule in the prompts and a check that resolves names; none discarded in the week of runs that followed |
| A demo API served an order book 3.2 hours old, 35 bps from where its own trades printed | orders are refused against a book older than 30 seconds |
| A library reported a fee the venue charged as a rebate | fees are read as the venue charged them |
| A venue's leverage call answered "success" while the setting stayed where it was | leverage and margin mode are confirmed by reading them back |
| The technical analyst could see the other analysts' conclusions | every analyst reads only its own data; code weighs the readings once |

The details are on [Venues](/opensource/venues), [Agents](/opensource/agents) and
[Working with language models](/opensource/llm).

## Sources

- Alpha Arena Season 1 results: [ForkLog, "Four out of six AI models suffer losses in trading tournament"](https://forklog.com/en/four-out-of-six-ai-models-suffer-losses-in-trading-tournament/), quoting Nof1's founder; [South China Morning Post](https://www.scmp.com/tech/tech-trends/article/3329784/deepseek-outperforms-ai-rivals-real-money-real-market-crypto-showdown).
- Moscow Exchange AI hackathon: [report of the results](https://www1.ru/en/news/2026/06/30/mosbirza-dala-ii-million-robot-prokrutil-18-milliarda-i-proigral.amp.html), citing the exchange via Vedomosti.
- Saturn, "Artificial Authority": [the report](https://www.saturnos.com/report/artificial-authority); [Financial Reporter coverage](https://www.financialreporter.co.uk/ai-models-give-wrong-financial-advice-57-of-the-time-saturn-research-finds.html).
- FinanceBench: [Islam et al., "FinanceBench", arXiv:2311.11944](https://arxiv.org/abs/2311.11944v1); [Patronus AI announcement](https://patronus.ai/announcements/patronus-ai-launches-financebench-the-industrys-first-benchmark-for-llm-performance-on-financial-questions).
- Indirect prompt injection: [Greshake et al., arXiv:2302.12173](https://arxiv.org/abs/2302.12173v2), AISec 2023.
- Memory injection in Web3 agents: [Patlan et al., "Real AI Agents with Fake Memories", arXiv:2503.16248](https://arxiv.org/abs/2503.16248).
- [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/).
- Freysa: [OECD AI Incidents Monitor](https://oecd.ai/en/incidents/2024-11-29-2e31).
- AIXBT: [AI Incident Database, incident 1003](https://incidentdatabase.ai/cite/1003).
- Knight Capital: [SEC press release 2013-222](https://www.sec.gov/news/press-release/2013-222).
- [ESMA public statement on AI and investment services, May 30, 2024](https://www.esma.europa.eu/sites/default/files/2024-05/ESMA35-335435667-5924__Public_Statement_on_AI_and_investment_services.pdf).
- [IOSCO consultation report on AI in capital markets, March 2025](https://iosco.org/news/pdf/IOSCONEWS761.pdf).
