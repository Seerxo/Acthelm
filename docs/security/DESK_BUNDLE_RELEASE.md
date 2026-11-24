# Desk bundle in releases

The Desk SPA ships as source. The release images build it: the Dockerfile's `desk` stage runs `npm ci` (every
package verified against the integrity hashes in `desk/package-lock.json`) and `npm run build` on `desk/`, and the
`api` stage copies `app/static` from that stage only. `.dockerignore` keeps the checked-in `app/static` (and local
`desk/node_modules`, `desk/dist`) out of the build context, so those bytes are never built into an image or served.

`app/static` in git stays at the bytes of release caab3d3. It is generated code (one minified ~1.3 MB
`desk.html`), and keeping it frozen means a release diff never contains it.

## What the release gateway reviews

The ordinary release diff from the running release (`git_release_diff` with `diff_scope`): the whole tree, which
contains the `desk/` sources, `package-lock.json` and the Dockerfile that builds them. Nothing is excluded or
replaced by a header, and no rebuild evidence is required: what the image runs is built from exactly the reviewed
sources at the reviewed commit.

`desk` and `.dockerignore` are in `DEPLOY_PATHS` and in the deploy driver's `PATHS` (pinned equal by a test), so
both are part of the payload digest and the image inputs; a change to either is a new build.

## Limits

- The founder-signed policy auto-releases only low-risk paths (`docs/*` today), so a `desk/` change still needs
  the founder's signature in addition to the independent review.
- The image build runs `npm run build` (agent-written `vite.config.ts` and the locked npm packages) on the build
  host, the same trust the CI frontend build already has; it never runs in the production containers.
- The node base image is pinned by digest in the Dockerfile; bump tag and digest together.
