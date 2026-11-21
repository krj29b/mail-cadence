# Security policy

## Supported versions

Only the latest release receives fixes. Please check that a problem still occurs on the
newest version from [Releases](https://github.com/krj29b/HardDaysEngine/releases) or
[PyPI](https://pypi.org/project/harddaysengine/) before reporting it.

## Reporting a vulnerability

Please **do not open a public issue** for security problems.

Report privately through GitHub instead:
[Report a vulnerability](https://github.com/krj29b/HardDaysEngine/security/advisories/new)
(or the **Security** tab, then **Report a vulnerability**).

Include the version, your operating system, steps to reproduce, and what an attacker could
achieve. A crafted file that makes the engine crash, hang, or run code is in scope, for
example a malicious ADF image, `config.toml`, or replay file.

## What to expect

This is a hobby project maintained on a best-effort basis. I aim to acknowledge reports
within a week and to publish a fixed release and a GitHub security advisory once the issue
is resolved. Credit is given in the advisory unless you prefer to stay anonymous.

## Out of scope

- Problems in third-party dependencies (pygame-ce, ModernGL, numpy and others): please report
  them upstream. Dependabot alerts here track known issues in the versions we lock.
- Obtaining or distributing original game data. The project ships none, and requests for it
  are not security issues.
