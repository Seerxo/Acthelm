# Threat model

Acthelm runs AI agents that read untrusted text, write code and prepare releases. This document
lists the trust boundaries, the threats at each one, the control that addresses it, and whether that
control is **tested** in this repository or is an **assumption** about the deployment.

A test in this repository is a tripwire: anyone who can change the repository can change the test. The
real boundaries are the ones enforced outside it (GitHub permissions, the host verifier, the signed
policy, credential separation). See [RELEASE_GATEWAY.md](RELEASE_GATEWAY.md).

## Boundaries

```
public source → CI runner → artifact → release gateway → production host → database
                                                              ↘ agent sandbox → model provider
```

## 1. Public source → CI runner

| Threat | Control | Evidence |
|---|---|---|
| A malicious pull request runs code on company infrastructure | Every job runs on a fresh GitHub-hosted `ubuntu-24.04` VM; no self-hosted label is allowed | Tested: `tests/test_trust_boundary.py::test_every_workflow_runs_on_a_github_hosted_runner` |
| A pull request gains secrets or write access | No workflow names a secret; every token is read-only; privileged triggers (`pull_request_target`, `workflow_run`, comment triggers) are forbidden | Tested: `test_secrets_are_named_only_where_allowed`, `test_write_scopes_are_only_the_allowed_ones`, `test_no_privileged_trigger` |
| A compromised or moved action tag | Every third-party action is pinned to a full commit SHA | Tested: `test_every_action_is_pinned_to_a_full_commit` |
| Checkout credentials persist into later steps | `persist-credentials: false` on checkouts | Tested for the readiness workflow; reviewed for the others |
| Secrets committed by a contributor | TruffleHog on every pull request and push; `.gitignore` covers keys, `.env`, dumps | Workflow `secret-scan`; GitHub push protection is an **assumption** until enabled on the repository |
| Vulnerable dependencies | OSV scan on dependency changes and weekly; pinned versions | Workflow `dependency-scan` |

## 2. CI result → merge

| Threat | Control | Evidence |
|---|---|---|
| A change merges without passing tests | Required checks `python-tests`, `frontend-tests` and `trufflehog` on the exact head commit, enforced by branch protection on `main`; `scripts/merge-pr` refuses pending, failed or other-commit evidence | Tested: `tests/test_ci_status_gate.py`, `tests/test_merge_pr.py`. Branch protection is a repository setting, not tested here |
| An agent merges its own work | The coding agent's account is read-only on the repository and publishes only to a fork | Verified at runtime by the Stage C preflight (`app/stage_c_preflight.py`, `app/stage_c_credential_boundary.py`) |
| An unexpected push to `main` | `app/main_guard.py` detects commits that did not come through a recorded merge | Detection only; prevention depends on branch protection |

## 3. Merge → production (release gateway)

| Threat | Control | Evidence |
|---|---|---|
| A merged change deploys itself | Merging deploys nothing. Production applies only a manifest the host-side verifier checks against the founder-signed policy and the ledger | Tested: `tests/test_release_gateway.py`, `tests/test_release_host.py`, `tests/test_release_acceptance.py` |
| A low-risk release changes executable images | A gateway (non-founder) release may not change the running image identities | Tested: `tests/test_release_acceptance.py` (image drift refusals) |
| Replay or rollback of an old manifest | Sequence numbers, parent SHA and expiry are part of the signed manifest; the ledger is append-only | Tested in the release host suite |
| Interrupted release | Journalled phases, anchored rollback images, recovery refuses to continue on an inconsistent ledger | Tested: `tests/test_release_acceptance.py`, `tests/test_deploy_fence_and_rollback_anchor.py` |

## 4. Agent sandbox → model provider

| Threat | Control | Evidence |
|---|---|---|
| Prompt injection in repository files or task text steers the coder | The coder has no GitHub credentials, writes only to a scoped path list, and its output is reviewed by a different provider before publication | Scope and fingerprint tested in `tests/test_stage_c_*.py`; review quality is an **assumption** |
| Network exfiltration from the coder | The coder container reaches only the model provider hosts through an egress proxy | Verified at runtime by preflight acceptance checks (`acceptance:egress`, `direct_egress_blocked`, `random_hosts_blocked`) |
| Credential theft from the sandbox | Provider logins are mounted separately from GitHub credentials; the coder cannot read the worker token | Runtime acceptance check `credential_isolation` |
| Private task text or credentials leak through a public fork | The pre-push privacy gate in `app/stage_c_runner.py` scans every commit new to the target branch relative to upstream main, including commit objects, full changed blobs and paths, plus public PR/commit text. Errors and unscannable content stop publication; the scan binds to the pushed SHA and remote lease. Repairs and missing-PR recovery are gated too | Coverage: `tests/test_stage_c_privacy_gate.py`. Not verified: semantic privacy or arbitrary secrets; homoglyphs, encodings, rewording, translation, junk-word insertion and fragments below both the 8-word and 64-character thresholds can pass. Indexed fields exclude the title and generated `improvement` evidence; unrelated private facts and already published history are not protected. Contiguous base Python code, excluding comments/literals, is exempt from overlap matching. No live fork acceptance is established by these local tests |
| Runaway cost | No metered API keys; a quota limit means waiting; no automatic purchases | Policy in `agents/cost_policy.yaml`, enforced by provider configuration |

## 5. API and Desk

| Threat | Control | Evidence |
|---|---|---|
| Unauthenticated access to the API or Desk | Production is reachable only through an authenticated tunnel; routes have scoped policies; founder-only actions require a verified identity assertion | Tested: `tests/test_api_auth.py`, `tests/test_approval_identity.py` |
| Model/provider text exposes data through the Desk | `app/redact.py` masks credential shapes at write and read boundaries, with fixed placeholders on errors or oversized input; `app/worker.py` redacts stored output and audits, and `app/desk.py` filters legacy reads and allowlists task-detail context/artifacts. `app/main.py` sets security headers and a script-hash CSP, discovering inline scripts with `html.parser` | Coverage: `tests/test_desk_data_exposure.py`. Not verified: complete detection of secrets or private prose, every browser's CSP enforcement, or live deployment configuration. Inline styles remain allowed; parser/browser script-data escape differences are bounded by a built-bundle test, not a general HTML parsing guarantee |
| Cross-site writes or DNS rebinding inherit a trusted peer's authority | `app/api_auth.py` checks Origin/Sec-Fetch-Site on writes before dispatch, including log mode. Browser writes require the configured Desk Host or a fixed loopback endpoint, so a matching attacker Host/Origin pair is refused | Coverage: `tests/test_api_auth.py`, `tests/test_desk_data_exposure.py`. Not verified: live DNS, tunnel/proxy configuration or real-browser behavior. Requests with neither browser header use caller authentication instead; same-origin malicious scripts are outside this control |
| An agent or operator tampers with goals to steer planning | `app/main.py` requires the verified founder for goal creation and editing and audits that principal, treating supplied actor fields as metadata; `app/api_auth.py` enforces founder scope even in log mode. The agent pause also stops `app/business_reconciler.py` and `app/experiment_scheduler.py` | Coverage: `tests/test_founder_authorization.py`, `tests/test_goals.py`, `tests/test_business_reconciler.py`, `tests/test_experiment_scheduler.py`. Not verified: compromised founder sessions, direct database writes, production identity configuration or live pause behavior; read access to goals is not made founder-only |
| SQL injection | SQLAlchemy ORM; the two raw SQL statements in `app/` use bound parameters | Reviewed: no SQL in `app/` is built with f-strings, `%` or `.format` |
| Path traversal in release archives | Archive paths are validated; mode and scope are checked | Tested: `tests/test_release_archive_permissions.py`, `tests/test_release_host_hardening.py` |

## 6. Data at rest

| Threat | Control | Evidence |
|---|---|---|
| Backup disclosure | Backups are encrypted with `age` before they leave the host; the private key is held offline by the owner | Tested: `tests/test_backup_encryption.py`; restore drills are run by the owner |
| Data loss | Daily dumps, off-site copies, weekly restore tests into a disposable database | Tested: `tests/test_restore_test.py`, `tests/test_backup_*.py` |

## Known residual risks

- **GitHub settings are not code.** Branch protection on `main` requires `python-tests`,
  `frontend-tests` and `trufflehog`, an up-to-date branch and one approving review, but it is not
  versioned here and no test detects a change to it. The review setting, secret scanning push protection and Actions approval
  policies are not verified from this repository.
- **Single owner.** The founder login is the final merge and release authority; a compromise of that
  account or its signing key bypasses the human gate. The gateway key can release low-risk paths on its own.
- **Third-party apps.** Any GitHub App with write access to the repository can push to it. Keep app
  permissions minimal.
- **Release driver timeout.** The host verifier runs the deploy driver without its own time limit; a hung
  driver leaves the release `pending` until the owner reconciles it (fail-closed, but not self-healing).
- **Model review is not a proof.** Independent review lowers, but does not remove, the risk of a
  harmful change in a scoped path.
