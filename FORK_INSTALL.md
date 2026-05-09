# Installing the `ouroboros-loop` Fork

This is a fork of [Q00/ouroboros](https://github.com/Q00/ouroboros) (MIT) with additional
compounding-mode work layered on top (Phase-2 serial executor, postmortem chain
redactor, Q4.1 mode-resolution, etc.). Use these instructions to install the fork
directly — the upstream `ouroboros-ai` package on PyPI does **not** include these
features.

> **Audit evidence:** Two controlled comparisons of `--compounding` vs default
> parallel execution live in this repo:
>
> - **v1** (5-AC, fully-specified contracts):
>   [`examples/parking-lot-audit/REPORT.md`](examples/parking-lot-audit/REPORT.md).
>   Compounding did **not** outperform parallel — its postmortem chain
>   had nothing to carry because the seed pinned every contract.
>
> - **v2** (8-AC, deliberately ambiguous contracts):
>   [`examples/parking-lot-audit-v2/REPORT.md`](examples/parking-lot-audit-v2/REPORT.md).
>   Drift hypothesis still unfalsified — agents converged on identical
>   "Pythonic default" picks across all 5 ambiguity axes in both modes.
>   Compounding *was* cheaper on tool/message counts when it completed
>   (~18-59% reductions) but ~17% slower on wall and 2/5 runs hit a
>   13s fast-fail (likely Claude API rate-limit). v3 design notes are
>   in the v2 report.
>
> Harness, score script, and metrics extractor are in
> [`scripts/audit/`](scripts/audit/) — reusable for anyone who wants
> to reproduce or extend the experiment.

## Recommended: `uv tool install` from git

```bash
uv tool install git+https://github.com/KeithMoc/ouroboros-loop.git
ouroboros setup
```

Pin to a tag or branch:

```bash
uv tool install 'git+https://github.com/KeithMoc/ouroboros-loop.git@<tag-or-branch>'
```

With extras (mirror upstream's optional groups):

```bash
uv tool install 'ouroboros-ai[claude] @ git+https://github.com/KeithMoc/ouroboros-loop.git'
uv tool install 'ouroboros-ai[mcp]    @ git+https://github.com/KeithMoc/ouroboros-loop.git'
uv tool install 'ouroboros-ai[all]    @ git+https://github.com/KeithMoc/ouroboros-loop.git'
```

## Alternative: `pip install` from git

```bash
pip install 'git+https://github.com/KeithMoc/ouroboros-loop.git'
ouroboros setup
```

With extras:

```bash
pip install 'ouroboros-ai[claude] @ git+https://github.com/KeithMoc/ouroboros-loop.git'
```

## Alternative: Claude Code plugin marketplace

Install as a Claude Code plugin directly from the fork slug:

```bash
claude plugin marketplace add KeithMoc/ouroboros-loop
claude plugin install ouroboros@ouroboros
```

`claude plugin marketplace add` keys on the GitHub repository slug, so it works
without any manifest rebranding. The plugin still reports `Q00` as the author
inside `plugin.json` — that field is preserved deliberately to keep the fork
byte-identical with upstream where possible.

## Alternative: Build a wheel

```bash
git clone https://github.com/KeithMoc/ouroboros-loop.git
cd ouroboros-loop
uv build
# Distribute or install dist/ouroboros_ai-*.whl:
pip install dist/ouroboros_ai-*.whl
```

## Verifying the install

```bash
ouroboros --version
ouroboros setup        # registers MCP server with detected runtime
```

## Why not PyPI?

The PyPI package name `ouroboros-ai` belongs to the upstream maintainer
([Q00](https://github.com/Q00)). Publishing the fork under that name is not
possible without coordination. The git-install routes above sidestep PyPI
entirely.

## License

Upstream is MIT-licensed (see [`LICENSE`](LICENSE)). The MIT license permits
use, copy, modify, merge, publish, distribute, sublicense, and sale, provided
the copyright notice and license text are retained in all copies or substantial
portions. This fork preserves the upstream `LICENSE` file unchanged.

If you redistribute the fork (wheel, tarball, mirror), keep `LICENSE` intact.
