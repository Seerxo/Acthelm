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

- **Fact:** production containers run content-addressed image IDs that a founder `prepare` built and recorded
  in a root-owned build record keyed by the tree's image inputs (`inputs_digest`, which excludes
  `docker-compose.yml`). A release manifest names exactly those IDs; the host refuses anything else.
  `x-bound-images` in `docker-compose.yml` repeats them only for a host whose verifier predates binding
  (bootstrap); `tests/test_bound_pins_match_tree.py` fails if they do not name the tree's own inputs.

## FACT-03 · Desk build

- **Fact:** `app/static/desk.html` is the build output of `desk/` (outside the deployed paths). CI rebuilds it
  and fails on any byte difference (`frontend.yml`; a copy is in `tests/fixtures/ci_snapshot/`). Its
  `desk-build` meta tag is a hash of `desk/src`, so a source change shows up as that one line.

## FACT-04 · Host names and host-installed controls

- **Fact:** repository tooling names hosts only by generic SSH aliases (`company-os-host`), never by provider
  or address; the founder's SSH configuration maps the alias. Network controls that protect the host's
  co-hosted services are installed on the host, not shipped from this repository; see
  [`scripts/HOST_CONTROLS.md`](../scripts/HOST_CONTROLS.md).
