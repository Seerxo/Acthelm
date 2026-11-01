# Active work checkpoint

- **Verified at (UTC):** 2026-11-02
- **Repository / base main SHA:** `Seerxo/Acthelm` (renamed from `Seerxo/seerxo-os` on 2026-11-01, same repository) / `199644a82d0f2300bd47596a97a8ac4fd070de3d` (#47)
- **Active task:** repository identity: code identities name `seerxo/acthelm` (Stage C, merge gate, credential boundary, control, main guard, CI usage, `scripts/merge-pr` by repository id). The publish fork stays `seerxo-agent/seerxo-os`.
- **Production (2026-11-02):** runs `c4be8a5` under policy v4, whose signed identity is still `seerxo/seerxo-os` (release-host and gateway configs, ledger rebase of 2026-10-31). The identity migration (policy v5 naming `seerxo/acthelm`, host configs, ledger rebase) is PENDING and founder-signed; a merged code change does not perform it.
- **Before deploying the identity change:** the production `company-os` Project row must name `seerxo/acthelm` (preflight `db_project_repository` fails closed otherwise); Stage C grants naming `seerxo/seerxo-os` are refused and must be re-issued; no task may be in PR_OPEN with a `seerxo/seerxo-os` PR URL (it would no longer be polled or merged).
- **Founder-only actions:** merge authorization, policy v5 signature, ledger rebase, prepare, production release.
- **Exact next action:** exact-head CI and founder merge decision for the identity PR; then rebuild the Stage C pilot from the merged main.

This checkpoint is mutable current state. Refresh GitHub before use. A merged PR, successful CI, or public source SHA does not prove production deployment.
