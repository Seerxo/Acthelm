# Active work checkpoint

- **Verified at (UTC):** 2026-11-01 19:25
- **Repository / base main SHA:** `Seerxo/seerxo-os` / `6a038822dacbcff2b39e252014dbe78614ccfc53` (rebased to resolve the permanent-facts conflict after PR #44)
- **Active task:** Introduce the founder-approved Acthelm product name and compatible `scripts/acthelm` CLI entry point on branch `chore/acthelm-brand`.
- **Implementation:** README/tagline, contributor entry points, API title, Desk and PWA text updated; committed frontend assets rebuilt. `scripts/acthelm` shares the existing `companyctl` implementation.
- **Local checks after rebase:** 156 Python tests passed, 2 expected failures; 153 frontend tests passed. Frontend build matches committed assets. Lint (warnings, no errors), docs and branch-name checks passed. Before rebase, 366 additional release-boundary tests passed; those are baseline evidence, not a claim about this head.
- **Review / CI:** Independent Standards and Spec reviews found no remaining must-fix issues. Refresh hosted checks on the exact PR head before merge; local checks and model reviews are not merge authorization or hosted CI evidence.
- **Repository rename:** PENDING. `Seerxo/acthelm` is the approved target; the current signed release identity and authorization comparisons still use `seerxo/seerxo-os`. Follow [DEC-009](DECISIONS.md#dec-009--acthelm-product-name) before changing the GitHub name.
- **Production:** NOT CHANGED by this task. No live host evidence was inspected; the new branding and CLI are not claimed deployed.
- **Founder-only actions:** Required merge authorization and any signed identity migration or production release. Domain purchase and DNS changes are outside this task.
- **Exact next action:** Review the Acthelm branding PR on `chore/acthelm-brand`, require exact-head CI, and authorize its merge. Coordinate repository identity migration separately.

This checkpoint is mutable current state. Refresh GitHub before use. A merged PR, successful CI, or public source SHA does not prove production deployment.
