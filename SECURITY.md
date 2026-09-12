# Security

## Scope

This library reads public market data archives, computes verdicts, and writes local files.
It holds no exchange credentials, opens no trading connection, and places no orders. A
report that it does any of those things is a bug report of the highest severity.

Things that are in scope for a security report:

- A path where a value from an untrusted file (a ledger row, an envelope, a dataset manifest)
  is executed, deserialised unsafely, or used to reach outside the intended directory.
- A gate that can be made to issue a verdict its rules forbid, or to skip a rule silently.
- A dependency with a known vulnerability that this project pins.

## Reporting

Use GitHub's private vulnerability reporting on this repository ("Security" tab, "Report a
vulnerability"). Do not open a public issue for a security problem.

Include the version or commit, the smallest reproduction you have, and what you believe the
impact is. You will get an acknowledgement, and a fix or a reasoned response before any
public disclosure.

## Supported versions

Pre-release. Only the current `develop` and the latest tag on `master` receive fixes.
