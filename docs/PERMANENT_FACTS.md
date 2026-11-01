# Permanent facts

This file contains only stable, founder-confirmed facts. Binding rules live in the
[constitution](../CLAUDE.md) and [Git workflow](GIT_WORKFLOW.md). Dated or mutable
execution state belongs in [`operations/ACTIVE_WORK.md`](operations/ACTIVE_WORK.md);
founder decisions and their status belong in [`operations/DECISIONS.md`](operations/DECISIONS.md).
Never store secrets, credentials, personal data, pending CI, mutable SHAs, or unverified
production claims here.

## FACT-01 · Repository responsibilities

- **Fact:** `Seerxo/seerxo-os` is the only active repository: development, CI and the
  production release chain (founder-signed ledger rebase, policy v4 naming
  `seerxo/seerxo-os`, 2026-10-31). `Seerxo/os` is archived and read-only; it has no release,
  migration or CI role.
- **Consequence for agents:** do not move routine engineering back to the previous private
  self-hosted CI path. Source migration does not establish production migration. See
  [DEC-001](operations/DECISIONS.md#dec-001-public-repository-is-the-development-path).

## FACT-02 · Image binding

- **Fact:** the binding rule for every new release: the host applies only content-addressed image IDs that a
  founder `prepare` built and recorded in a root-owned build record keyed by the tree's image inputs
  (`inputs_digest`, which excludes `docker-compose.yml`). A release manifest names exactly those IDs, or the
  running bound set when the image inputs are unchanged; the host refuses anything else. When present, `x-bound-images` in `docker-compose.yml`
  repeats them for a host whose verifier predates binding (bootstrap); they are written in a compose-only commit
  after `prepare`, and `tests/test_bound_pins_match_tree.py` fails if they do not name the tree's own inputs. A
  tree without pins claims nothing. (Design: `docs/security/RELEASE_GATEWAY.md`.)

## FACT-03 · Desk build

- **Fact:** `app/static/desk.html` is the build output of `desk/` (outside the deployed paths). CI rebuilds it
  and fails on any byte difference (`frontend.yml`; a copy is in `tests/fixtures/ci_snapshot/`). Its
  `desk-build` meta tag is a 12-hex sha256 of `desk/src` and `desk/index.html`, so every source change also
  changes that tag. (`desk/vite.config.ts`.)

## FACT-04 · Host names and host-installed controls

- **Fact:** repository tooling names hosts only by generic SSH aliases (`company-os-host`), never by provider
  or address; the founder's SSH configuration must map the alias. Network controls that protect the host's
  co-hosted services are host-installed and founder-maintained, not shipped from this repository; see
  [`scripts/HOST_CONTROLS.md`](../scripts/HOST_CONTROLS.md).

## FACT-05 · Product name

- **Fact:** The product is **Acthelm**, with the tagline **Agents work. You steer.**
  The founder approved the name, target repository `Seerxo/acthelm`, and CLI name
  `acthelm` on 2026-11-01.
- **Consequence for agents:** use Acthelm in product-facing text. Repository identity
  migration is a separate operation; see [DEC-009](operations/DECISIONS.md#dec-009--acthelm-product-name).
