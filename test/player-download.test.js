const assert = require("assert");
const vm = require("vm");
const fs = require("fs");
const source = fs.readFileSync("webui/remote-download.js", "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));
function player() {
  const posts = [], gets = [], timers = [];
  const selectedEnglish = { id: "chosen", lang: "en-US", url: "https://subs.example/chosen" };
  const scope = { selected: {
    item: { title: () => "Example" },
    stream: { stream: { url: "https://video.example/file" }, behaviorHints: { filename: "original.mkv" } },
    subtitles: { all: [{ id: "first", lang: "eng", url: "https://subs.example/first" }, selectedEnglish], preferred: { "en-US": "chosen" } },
  }, info: { releaseInfo: "2026" }, $watch(_, fn) { this.changed = fn; fn(); }, $on(_, fn) { this.destroy = fn; } };
  let job = { status: "queued", message: "Queued" }, postResponse = () => Promise.resolve({ data: { id: "a".repeat(32) } });
  let pollError;
  const timeout = fn => { timers.push(fn); return fn; };
  timeout.cancel = id => { const index = timers.indexOf(id); if (index >= 0) timers.splice(index, 1); };
  const http = {
    get(url, options) {
      gets.push({ url, options });
      return url.endsWith("config") ? Promise.resolve({ data: { enabled: true } }) : pollError ? Promise.reject(pollError) : Promise.resolve({ data: job });
    },
    post(url, body, options) { posts.push({ url, body, options }); return postResponse(); },
  };
  let directive;
  vm.runInNewContext(source, { angular: { module: () => ({ directive(_, factory) { directive = factory[factory.length - 1](http, timeout, { getJSON: () => "session-key" }); } }) } });
  directive.link(scope);
  return { scope, posts, gets, timers, selectedEnglish,
    save() { scope.saveRemote({ stopPropagation() {} }); },
    job(value) { job = value; },
    postResponse(value) { postResponse = value; },
    pollError(value) { pollError = value; },
    async poll() { timers.shift()(); await tick(); },
  };
}
async function main() {
  let p = player();
  p.save(); p.save(); await tick();
  assert.strictEqual(p.posts.length, 1, "Repeated clicks while queuing must share the job");
  assert.strictEqual(p.posts[0].body.subtitleUrl, p.selectedEnglish.url);
  assert.strictEqual(p.posts[0].body.filename, "original.mkv");
  assert.strictEqual(p.scope.remoteLabel, "Queued…");
  p.job({ status: "downloading", message: "Saving video" }); await p.poll();
  assert.strictEqual(p.scope.remoteLabel, "Saving…");
  p.job({ status: "complete", message: "Saved" }); await p.poll();
  assert.strictEqual(p.scope.remoteLabel, "Saved to compute-alt");
  assert.strictEqual(p.scope.remoteBusy, false);
  assert.strictEqual(p.timers.length, 0);

  p = player();
  p.scope.selected.subtitles.current = () => p.selectedEnglish;
  p.scope.selected.subtitles.currentDelay = () => 250;
  p.scope.selected.video = { season: 2, episode: 3 };
  p.job({ status: "failed", message: "Source expired" });
  p.save(); await tick();
  assert.strictEqual(p.posts[0].body.subtitleDelay, 250);
  assert.strictEqual(p.posts[0].body.title, "Example (2026) S02E03");
  assert.strictEqual(p.scope.remoteLabel, "Retry save");
  assert.strictEqual(p.scope.remoteMessage, "Source expired");

  p = player();
  p.scope.selected.subtitles = { all: [{ lang: "fre", url: "https://subs/fr" }] };
  p.save(); await tick();
  assert.strictEqual(p.posts.length, 0);
  assert(p.scope.remoteMessage.includes("No English subtitle"));
  p.scope.selected.stream = null; p.save();
  assert.strictEqual(p.posts.length, 0);

  p = player();
  let resolve;
  p.postResponse(() => new Promise(r => resolve = r));
  p.save();
  p.scope.changed();
  resolve({ data: { id: "b".repeat(32) } }); await tick();
  assert.strictEqual(p.gets.length, 1, "An old movie's POST must not start polling on the next movie");
  assert.strictEqual(p.scope.remoteBusy, false);
  assert.strictEqual(p.scope.remoteLabel, "Save to compute-alt");

  p = player(); p.save(); await tick();
  p.scope.destroy();
  assert.strictEqual(p.timers.length, 0);

  p = player();
  p.pollError({ data: { error: "Service temporarily unavailable" } });
  p.save(); await tick();
  assert.strictEqual(p.scope.remoteBusy, false);
  assert.strictEqual(p.scope.remoteMessage, "Service temporarily unavailable");
  assert.strictEqual(p.timers.length, 0);
  console.log("Player queue lifecycle, subtitle preference, retries and stale-response checks passed");
}
main().catch(error => { console.error(error); process.exitCode = 1; });
