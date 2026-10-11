const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const http = require("http");

let decorate;
vm.runInNewContext(fs.readFileSync(path.join(__dirname, "../webui/suppress-notifications.js"), "utf8"), {
  angular: {
    module(name) {
      assert.strictEqual(name, "stremio");
      return {
        config(config) {
          config[config.length - 1]({
            decorator(service, factory) {
              assert.strictEqual(service, "toasts");
              decorate = factory[factory.length - 1];
            },
          });
        },
      };
    },
  },
});

const calls = [];
const toasts = {};
for (const level of ["error", "warning", "info", "success"]) {
  toasts[level] = function (...args) {
    calls.push({ level, args, context: this });
    return "shown";
  };
}
assert.strictEqual(decorate(toasts), toasts);
for (const level of Object.keys(toasts)) {
  for (const title of ["ERR_CASTING_UPDATE_TITLE", "TRAKT_EXPIRED"]) {
    toasts[level](title, "details", { onTap() {} });
  }
}
assert.strictEqual(calls.length, 0, "Suppressed notifications must never reach the renderer");
const options = { onTap() {} };
for (const level of Object.keys(toasts)) {
  assert.strictEqual(toasts[level]("ERR_SUBS_LOADING_TITLE", "details", options), "shown");
}
assert.strictEqual(calls.length, 4);
for (const call of calls) {
  assert.deepStrictEqual(call.args, ["ERR_SUBS_LOADING_TITLE", "details", options]);
  assert.strictEqual(call.context, toasts);
}

const serveWebUI = require("../webui");
function request(server, pathname, method = "GET") {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: "127.0.0.1", port: server.address().port, path: pathname, method }, (res) => {
      const chunks = [];
      res.on("data", (chunk) => chunks.push(chunk));
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }));
    });
    req.on("error", reject);
    req.end();
  });
}

async function testHTTP() {
  const server = http.createServer(serveWebUI);
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  try {
    const index = await request(server, "/?streamingServer=https%3A%2F%2Fexample.com");
    assert.strictEqual(index.status, 200);
    assert.strictEqual(index.headers["cache-control"], "no-cache");
    assert(index.body.includes('href="https://app.strem.io/shell-v4.4/blob.css?ver=d81e11"'));
    assert(index.body.indexOf('<script src="blob.js?ver=d81e11"></script>') < index.body.indexOf('$provide.decorator("toasts"'));
    assert(index.body.indexOf('$provide.decorator("toasts"') < index.body.indexOf("</head>"));
    const bundle = await request(server, "/blob.js?ver=d81e11");
    assert.strictEqual(bundle.status, 200);
    assert(bundle.body.includes('factory("toasts"'));
    const head = await request(server, "/", "HEAD");
    assert.strictEqual(head.status, 200);
    assert.strictEqual(head.body, "");
    assert.strictEqual(head.headers["content-length"], index.headers["content-length"]);
    assert.strictEqual((await request(server, "/", "POST")).status, 405);
    assert.strictEqual((await request(server, "/../server.js")).status, 404);
    assert.strictEqual((await request(server, "/images/../../server.js.png")).status, 404);
    assert.strictEqual((await request(server, "/images/https://example.com/logo.png")).status, 404);
    assert.strictEqual((await request(server, "/toString")).status, 404);
    console.log("Notification filtering and web interface HTTP checks passed");
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}
testHTTP().catch((error) => { console.error(error); process.exitCode = 1; });
