# Installing the `ouroboros-loop` Fork

This is a fork of [Q00/ouroboros](https://github.com/Q00/ouroboros) (MIT) with additional
compounding-mode work layered on top (Phase-2 serial executor, postmortem chain
redactor, Q4.1 mode-resolution, etc.). Use these instructions to install the fork
directly — the upstream `ouroboros-ai` package on PyPI does **not** include these
features.

> **Audit evidence:** A controlled comparison of `--compounding` vs default
> parallel execution on a 5-AC project lives at
> [`examples/parking-lot-audit/REPORT.md`](examples/parking-lot-audit/REPORT.md).
> The audit's headline finding: on tasks where every cross-AC contract is
> spelled out in the seed, compounding does **not** outperform parallel — its
> postmortem chain has nothing to carry. The harness, score script, and the
> v2 audit design (deliberately ambiguous contracts, longer chain) are all
> in [`scripts/audit/`](scripts/audit/) for anyone who wants to reproduce
> or extend the experiment.

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
