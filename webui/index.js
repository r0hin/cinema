const fs = require("fs");
const path = require("path");
const https = require("https");

const assets = {
  "/": ["index.html", "text/html; charset=utf-8"],
  "/index.html": ["index.html", "text/html; charset=utf-8"],
  "/blob.js": ["blob.js", "application/javascript; charset=utf-8"],
};
const contents = {};
for (const [filename] of Object.values(assets)) {
  contents[filename] = fs.readFileSync(path.join(__dirname, "dist", filename));
}

module.exports = function serveWebUI(req, res) {
  if (req.method !== "GET" && req.method !== "HEAD") {
    res.writeHead(405, { Allow: "GET, HEAD" });
    res.end();
    return;
  }
  const pathname = req.url.split("?")[0];
  // Preserve relative artwork URLs without redirecting the app away from our
  // patched shell. Never forward credentials or cookies to the asset host.
  if (/^\/images\/[a-zA-Z0-9_./-]+\.(png|jpg|jpeg|gif|webp|ico|svg)$/.test(pathname) && !pathname.includes("..")) {
    const request = https.request("https://app.strem.io/shell-v4.4" + pathname, { method: req.method }, (upstream) => {
      const headers = {};
      for (const name of ["content-type", "content-length", "cache-control"]) {
        if (upstream.headers[name]) headers[name] = upstream.headers[name];
      }
      res.writeHead(upstream.statusCode, headers);
      upstream.pipe(res);
      upstream.on("error", () => res.destroy());
    });
    request.setTimeout(15000, () => request.destroy(new Error("Artwork request timed out")));
    request.on("error", () => {
      if (!res.headersSent) res.writeHead(502);
      res.end();
    });
    request.end();
    return;
  }
  const asset = Object.prototype.hasOwnProperty.call(assets, pathname) ? assets[pathname] : null;
  if (!asset) {
    res.writeHead(404);
    res.end();
    return;
  }
  const [filename, contentType] = asset;
  res.writeHead(200, {
    "Content-Type": contentType,
    "Content-Length": contents[filename].length,
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
  });
  res.end(req.method === "HEAD" ? undefined : contents[filename]);
};
