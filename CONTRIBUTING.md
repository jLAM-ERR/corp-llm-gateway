# Contributing to corp-llm-gateway

Technical how-to (adding detectors, sinks, providers, profiles):
see [`docs/extending.md`](docs/extending.md).

## Legal terms — read before your first commit

- The core is licensed under the [Apache License 2.0](LICENSE).
- All contributions are accepted under the **Contributor License
  Agreement** — [`LEGAL/CLA.md`](LEGAL/CLA.md). Key points: you keep
  authorship; you grant the project owner an exclusive license to
  your contribution (including commercial use); you confirm you have
  the right to contribute (see CLA §5 if you are contributing in an
  employment context).

## Sign-off (required)

Every commit must carry a sign-off trailer:

```
Signed-off-by: Full Name <email>
```

Add it automatically with:

```bash
git commit -s
```

By signing off you accept the CLA. CI rejects pull requests whose
commits lack the trailer; fix a missed one with
`git commit --amend -s` and force-push your branch.

## Policy summary

- Contributor policy: [`LEGAL/CONTRIBUTORS.md`](LEGAL/CONTRIBUTORS.md)
- Ownership / IP notices: [`LEGAL/IP-NOTICE.md`](LEGAL/IP-NOTICE.md)
- Commercial offerings (enterprise plugins, support):
  [`LEGAL/COMMERCIAL-LICENSING.md`](LEGAL/COMMERCIAL-LICENSING.md)
