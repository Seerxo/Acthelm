# Host-installed security controls (not shipped from this repository)

The production host also runs services managed outside Company OS. Two host controls protect them:

| Control | Purpose |
|---|---|
| a host firewall rule set | keeps the co-hosted platform's administrative interfaces off the public network |
| a host service unit | keeps one platform-internal channel off the public network |

Their configuration is host-specific, so it is not kept in this repository. The founder installs and maintains
them from the host runbook; their presence is not verifiable from this tree. The release driver neither installs
nor removes them and nothing in the shipped tree calls them, so a release does not change them. A host rebuild
must re-install both before the co-hosted platform is exposed.
