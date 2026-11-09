import assert from "node:assert/strict"
import { test } from "node:test"
import { api, ApiError, apiText } from "./api.ts"

test("Access login HTML and denied API responses cannot be read as company data", async () => {
  const original = globalThis.fetch
  try {
    globalThis.fetch = async () => new Response("<html>Sign in</html>", { headers: { "content-type": "text/html" } })
    await assert.rejects(api("/desk/state"), (error: unknown) => error instanceof ApiError && error.status === 0)
    globalThis.fetch = async () => new Response('{"detail":"founder authentication required"}',
      { status: 401, headers: { "content-type": "application/json" } })
    await assert.rejects(api("/desk/state"), (error: unknown) => error instanceof ApiError && error.status === 401)
  } finally { globalThis.fetch = original }
})

test("apiText refuses an Access login page and any non-plain-text body", async () => {
  const original = globalThis.fetch
  try {
    globalThis.fetch = async () => new Response("<html>Sign in</html>", { headers: { "content-type": "text/html" } })
    await assert.rejects(apiText("/company/brief"), (e: unknown) => e instanceof ApiError && e.status === 0 && !e.signIn)
    globalThis.fetch = async () => new Response("{}", { headers: { "content-type": "application/json" } })
    await assert.rejects(apiText("/company/brief"), (e: unknown) => e instanceof ApiError && e.status === 0)
    globalThis.fetch = async () => new Response("denied", { status: 401, headers: { "content-type": "text/plain" } })
    await assert.rejects(apiText("/company/brief"), (e: unknown) => e instanceof ApiError && e.signIn)
    globalThis.fetch = async () => new Response("Brief", { headers: { "content-type": "text/plain; charset=utf-8" } })
    assert.equal(await apiText("/company/brief"), "Brief")
  } finally { globalThis.fetch = original }
})
