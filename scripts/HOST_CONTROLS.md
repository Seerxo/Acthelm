# Host-installed security controls (not shipped from this repository)

The production host also runs services managed outside Company OS. Two host network controls protect them:

| Control | Purpose |
|---|---|
| a host firewall chain in DOCKER-USER | keeps the co-hosted platform's management ports closed on the public interface |
| an SSH forward unit | exposes the platform's internal SSH forward on the internal address only |

They were removed from the public repository with the public release because their configuration is
host-specific. They are installed and maintained on the host from the founder's host runbook; the release
driver neither installs nor removes them, and nothing in the shipped tree calls them, so a release does not
change them. A host rebuild must re-install both before the co-hosted platform is exposed.
