const https = require("https");
const { createHash } = require("crypto");

function jsonRequest(url, method, headers, body) {
  return new Promise((resolve, reject) => {
    const req = https.request(url, { method, headers }, (res) => {
      let data = "";
      res.on("data", (chunk) => {
        data += chunk;
        if (data.length > 1024 * 1024) req.destroy(new Error("Response too large"));
      });
      res.on("end", () => {
        try { resolve({ status: res.statusCode, body: JSON.parse(data) }); }
        catch (_) { reject(new Error("Invalid service response")); }
      });
      res.on("error", reject);
    });
    req.setTimeout(15000, () => req.destroy(new Error("Download service timed out")));
    req.on("error", reject);
    req.end(body);
  });
}

module.exports = function createDownloads(env = process.env, request = jsonRequest) {
  const allowed = (env.CINEMA_DOWNLOAD_USERS || "").toLowerCase().split(",").filter(Boolean);
  const enabled = !!(env.CINEMA_DOWNLOAD_API && env.CINEMA_DOWNLOAD_TOKEN && allowed.length);
  const sessions = new Map();
  function send(res, status, body) {
    res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
    res.end(JSON.stringify(body));
  }
  return async function downloads(req, res) {
    const pathname = req.url.split("?")[0];
    if (pathname === "/downloads/config" && req.method === "GET") {
      send(res, 200, { enabled, target: "compute-alt", folder: "Movies" });
      return;
    }
    const match = /^\/downloads(?:\/([a-f0-9]{32}))?$/.exec(pathname);
    if (!match) return send(res, 404, { error: "Not found" });
    if (!enabled) return send(res, 503, { error: "Server downloads are not configured" });
    if (req.method !== (match[1] ? "GET" : "POST")) return send(res, 405, { error: "Method not allowed" });
    const auth = /^Bearer ([^\s]{1,1024})$/.exec(req.headers.authorization || "");
    if (!auth) return send(res, 401, { error: "Sign in to Stremio to save to compute-alt" });
    try {
      // Check the actual Stremio account, never a browser-supplied email. Cache
      // only a hash of its session key, briefly, for progress polling.
      const hash = createHash("sha256").update(auth[1]).digest("hex");
      if ((sessions.get(hash) || 0) < Date.now()) {
        const result = await request("https://api.strem.io/api/getUser", "POST",
          { "Content-Type": "application/json" }, JSON.stringify({ authKey: auth[1] }));
        if (result.status !== 200 || result.body.error || !allowed.includes((result.body.result && result.body.result.email || "").toLowerCase())) {
          return send(res, 403, { error: "This Stremio account cannot save to compute-alt" });
        }
        for (const [key, expiry] of sessions) if (expiry < Date.now()) sessions.delete(key);
        if (sessions.size >= 100) sessions.clear();
        sessions.set(hash, Date.now() + 60000);
      }
      let body;
      if (req.method === "POST") {
        body = req.body && typeof req.body === "object" ? JSON.stringify(req.body) : await new Promise((resolve, reject) => {
          let data = "";
          req.on("data", (chunk) => {
            data += chunk;
            if (data.length > 32768) reject(new Error("Download request too large"));
          });
          req.on("end", () => resolve(data));
          req.on("error", reject);
        });
        if (body.length > 32768) return send(res, 413, { error: "Download request too large" });
        try { JSON.parse(body); } catch (_) { return send(res, 400, { error: "Invalid JSON" }); }
      }
      const remote = new URL("/downloads" + (match[1] ? "/" + match[1] : ""), env.CINEMA_DOWNLOAD_API);
      if (remote.protocol !== "https:") throw new Error("Download API requires HTTPS");
      const result = await request(remote, req.method, {
        "Authorization": "Bearer " + env.CINEMA_DOWNLOAD_TOKEN,
        "Content-Type": "application/json",
      }, body);
      send(res, result.status, result.body);
    } catch (_) { send(res, 502, { error: "Cannot reach the download service. Try again shortly." }); }
  };
};
