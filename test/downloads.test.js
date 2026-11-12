const assert = require("assert");
const http = require("http");
const vm = require("vm");
const fs = require("fs");
const createDownloads = require("../webui/downloads");

async function main() {
  const upstream = [];
  const handle = createDownloads({ CINEMA_DOWNLOAD_API: "https://compute-alt.example", CINEMA_DOWNLOAD_TOKEN: "server-only-secret", CINEMA_DOWNLOAD_USERS: "owner@example.com" }, async (url, method, headers, body) => {
    upstream.push({ url: String(url), method, headers, body });
    if (String(url).includes("api.strem.io")) return { status: 200, body: { result: { email: JSON.parse(body).authKey === "owner-key" ? "owner@example.com" : "stranger@example.com" } } };
    return { status: 202, body: { id: "a".repeat(32), status: "queued" } };
  });
  const server = http.createServer((req, res) => {
    // Exercise Stremio's already-parsed body, which is consumed upstream.
    if (req.url.includes("?parsed")) {
      let chunks = "";
      req.on("data", c => chunks += c);
      req.on("end", () => { req.body = JSON.parse(chunks); handle(req, res); });
    } else handle(req, res);
  });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  function request(path, method = "GET", token, payload) {
    return new Promise((resolve, reject) => {
      const req = http.request({ hostname: "127.0.0.1", port: server.address().port, path, method, headers: { ...(token ? { Authorization: "Bearer " + token } : {}), "Content-Type": "application/json" } }, res => {
        let body = ""; res.on("data", c => body += c); res.on("end", () => resolve({ status: res.statusCode, body: JSON.parse(body) }));
      });
      req.on("error", reject); req.end(payload && JSON.stringify(payload));
    });
  }
  try {
    assert.strictEqual((await request("/downloads/config")).body.enabled, true);
    assert.strictEqual((await request("/downloads", "POST", null, {})).status, 401);
    assert.strictEqual((await request("/downloads", "POST", "other-key", {})).status, 403);
    assert.strictEqual(upstream.filter(x => !x.url.includes("api.strem.io")).length, 0);
    for (const suffix of ["", "?parsed"]) {
      assert.strictEqual((await request("/downloads" + suffix, "POST", "owner-key", { title: "Example" })).status, 202);
      const call = upstream[upstream.length - 1];
      assert.strictEqual(call.headers.Authorization, "Bearer server-only-secret");
      assert.strictEqual(JSON.parse(call.body).title, "Example");
    }
    assert.strictEqual((await request("/downloads/" + "a".repeat(32), "GET", "owner-key")).status, 202);
    assert.strictEqual((await request("/downloads/not-a-job", "GET", "owner-key")).status, 404);
  } finally { await new Promise(r => server.close(r)); }

  let directive;
  const requests = [];
  const scope = {
    selected: { item: { title: () => "Arrival" }, stream: { stream: { url: "https://video.example/movie" } }, subtitles: { all: [{ id: "fr", lang: "fre", url: "https://subs/fr" }, { id: "en", lang: "eng", url: "https://subs/en" }] } },
    info: { releaseInfo: "2016" }, $watch(_, fn) { fn(); }, $on() {},
  };
  const timeout = () => {}; timeout.cancel = () => {};
  const httpMock = {
    get(url) { return Promise.resolve({ data: url.endsWith("config") ? { enabled: true } : { status: "complete", message: "Saved" } }); },
    post(url, body, options) { requests.push({ url, body, options }); return Promise.resolve({ data: { id: "a".repeat(32) } }); },
  };
  vm.runInNewContext(fs.readFileSync("webui/remote-download.js", "utf8"), { angular: { module: () => ({ directive(name, factory) { directive = factory[factory.length - 1](httpMock, timeout, { getJSON: () => "owner-key" }); } }) } });
  directive.link(scope);
  scope.saveRemote({ stopPropagation() {} });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(requests[0].body.subtitleUrl, "https://subs/en");
  assert.strictEqual(requests[0].body.title, "Arrival (2016)");
  assert.strictEqual(requests[0].options.headers.Authorization, "Bearer owner-key");
  assert.strictEqual(scope.remoteLabel, "Saved to compute-alt");
  scope.selected.subtitles.current = () => ({ id: "selected", lang: "eng", url: "https://subs/chosen" });
  scope.selected.subtitles.currentDelay = () => 250;
  scope.saveRemote({ stopPropagation() {} });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(requests[1].body.subtitleUrl, "https://subs/chosen");
  console.log("Download authorization, proxy and player subtitle selection checks passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
