const assert = require("assert");
const http = require("http");
const createDownloads = require("../webui/downloads");

async function scenario(env, upstream, checks) {
  const handler = createDownloads(env, upstream);
  const server = http.createServer((req, res) => {
    if (req.url.includes("?parsed")) {
      let data = "";
      req.on("data", chunk => data += chunk);
      req.on("end", () => { req.body = JSON.parse(data); handler(req, res); });
    } else handler(req, res);
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  async function request(path = "/downloads", method = "POST", body = "{}", token = "owner-key") {
    return new Promise((resolve, reject) => {
      const req = http.request({ hostname: "127.0.0.1", port: server.address().port, path, method,
        headers: { "Content-Type": "application/json", ...(token ? { Authorization: "Bearer " + token } : {}) } }, res => {
        let data = "";
        res.on("data", chunk => data += chunk);
        res.on("end", () => resolve({ status: res.statusCode, body: JSON.parse(data), headers: res.headers }));
      });
      req.on("error", reject);
      req.end(method === "POST" ? body : undefined);
    });
  }
  try { await checks(request); }
  finally { await new Promise(resolve => server.close(resolve)); }
}

async function main() {
  const env = { CINEMA_DOWNLOAD_API: "https://downloads.example", CINEMA_DOWNLOAD_TOKEN: "never-in-the-browser", CINEMA_DOWNLOAD_USERS: " OWNER@example.com , second@example.com " };
  let authCalls = 0, remoteCalls = 0;
  const upstream = async (url, method, headers, body) => {
    if (String(url).includes("api.strem.io")) {
      authCalls++;
      assert.strictEqual(headers.Authorization, undefined);
      assert.strictEqual(JSON.parse(body).authKey, "owner-key");
      return { status: 200, body: { result: { email: "owner@example.com" } } };
    }
    remoteCalls++;
    assert.strictEqual(headers.Authorization, "Bearer never-in-the-browser");
    return { status: 400, body: { error: "Invalid download source" } };
  };
  await scenario(env, upstream, async request => {
    assert.strictEqual((await request("/downloads", "DELETE")).status, 405);
    assert.strictEqual((await request("/downloads", "POST", "{}", null)).status, 401);
    assert.strictEqual((await request("/downloads", "POST", "{}", "key extra")).status, 401);
    assert.strictEqual((await request("/downloads", "POST", "not JSON")).status, 400);
    for (const suffix of ["", "?parsed"]) {
      assert.strictEqual((await request("/downloads" + suffix, "POST", JSON.stringify({ title: "界".repeat(12000) }))).status, 413);
    }
    assert.strictEqual(remoteCalls, 0);
    const result = await request();
    assert.strictEqual(result.status, 400);
    assert.strictEqual(result.body.error, "Invalid download source");
    assert.strictEqual(result.headers["cache-control"], "no-store");
    assert(!JSON.stringify(result).includes(env.CINEMA_DOWNLOAD_TOKEN));
    assert.strictEqual(authCalls, 1, "Progress and repeat requests should reuse short-lived validation");
  });
  await scenario({}, async () => { throw new Error("should not call upstream"); }, async request => {
    assert.strictEqual((await request()).status, 503);
    assert.strictEqual((await request("/downloads/config", "GET")).body.enabled, false);
  });
  for (const result of [null, {}, { error: { message: "expired" } }, { result: { email: 42 } }, { result: { email: "other@example.com" } }]) {
    await scenario(env, async () => ({ status: 200, body: result }), async request => {
      assert.strictEqual((await request()).status, 403);
    });
  }
  await scenario(env, async () => { throw new Error("private service details"); }, async request => {
    const result = await request();
    assert.strictEqual(result.status, 502);
    assert(!JSON.stringify(result).includes("private service details"));
  });
  await scenario({ ...env, CINEMA_DOWNLOAD_API: "http://insecure.example" }, upstream, async request => {
    assert.strictEqual((await request()).status, 502);
  });
  const originalNow = Date.now;
  let now = originalNow(), validations = 0;
  Date.now = () => now;
  try {
    await scenario(env, async url => {
      if (String(url).includes("api.strem.io")) {
        validations++;
        return { status: 200, body: { result: { email: validations === 1 ? "owner@example.com" : "revoked@example.com" } } };
      }
      return { status: 202, body: { id: "a".repeat(32) } };
    }, async request => {
      assert.strictEqual((await request()).status, 202);
      assert.strictEqual((await request()).status, 202);
      now += 60001;
      assert.strictEqual((await request()).status, 403);
      assert.strictEqual(validations, 2);
    });
  } finally { Date.now = originalNow; }
  console.log("Download limits, account validation, failures and session expiry checks passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
