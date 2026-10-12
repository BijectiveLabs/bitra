---
title: Security
description: What a security report covers, and how to send one.
---

# Security

## Scope

This package reads market data, asks language models for opinions, computes verdicts and writes
local files. In venue mode, which is off until you sign a mandate and meet its conditions, it also
holds your venue keys in memory and sends orders. Models never see a key and cannot send, change or
cancel an order; only the venue hand does, after every gate.

Things that are in scope for a security report:

- A path where a credential reaches a log, a record, a brief, a model or a message in clear text.
- A path where a model's answer, or a value from an untrusted file (a ledger row, an envelope, a
  dataset manifest, a fetched series), is executed, deserialised unsafely, or used to reach outside
  the intended directory.
- A way to send an order without the mandate, the caps, the ladder tier or the gates that are
  supposed to stand in front of it, or to loosen a control from inside a model's answer.
- A gate that can be made to issue a verdict its rules forbid, or to skip a rule silently.
- A fetch that follows a redirect off https or sends a key to a host the operator did not name.
- A dependency with a known vulnerability that this project pins.

## Reporting

Use GitHub's private vulnerability reporting on the repository ("Security" tab, "Report a
vulnerability"). Do not open a public issue for a security problem.

Include the version or commit, the smallest reproduction you have, and what you believe the impact
is. You will get an acknowledgement, and a fix or a reasoned response before any public disclosure.

## Supported versions

Alpha (0.x). Only the latest release on PyPI, the matching tag on `master`, and the current
`develop` receive fixes.

## Where the responsibility sits

[Liability](/opensource/liability) says plainly who is responsible for what when real capital is
involved.
