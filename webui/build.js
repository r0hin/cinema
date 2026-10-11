const https = require("https");
const fs = require("fs");
const path = require("path");
const { createHash } = require("crypto");

// Pin the shell we currently redirect to. An upstream change must be reviewed
// before it can replace the interface and its notification service.
const upstream = "https://app.strem.io/shell-v4.4/";
const assets = [
  ["", "index.html", "ece0332519944865c6522adbc5ea2fc6b7381466547c38155e305345a9ece7b4"],
  ["blob.js?ver=d81e11", "blob.js", "31c75f5396d8cb87830721587465cb64d46ef318f1ea1c9c36af8e7a6b63c533"],
];

function download(url) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, (response) => {
      if (response.statusCode !== 200) {
        response.resume();
        reject(new Error(`${url} returned HTTP ${response.statusCode}`));
        return;
      }
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve(Buffer.concat(chunks)));
      response.on("error", reject);
    });
    request.setTimeout(30000, () => request.destroy(new Error(`Timed out downloading ${url}`)));
    request.on("error", reject);
  });
}

async function build() {
  const downloaded = await Promise.all(assets.map(async ([url, name, expected]) => {
    const content = await download(upstream + url);
    const actual = createHash("sha256").update(content).digest("hex");
    if (actual !== expected) throw new Error(`Upstream ${name} changed; review the shell before updating its checksum`);
    return [name, content];
  }));

  const directory = path.join(__dirname, "dist");
  fs.mkdirSync(directory, { recursive: true });
  const policy = fs.readFileSync(path.join(__dirname, "suppress-notifications.js"), "utf8");
  for (const [name, content] of downloaded) {
    let output = content;
    if (name === "index.html") {
      const script = '<script src="blob.js?ver=d81e11"></script>';
      output = content.toString("utf8")
        .replace('href="blob.css?ver=d81e11"', `href="${upstream}blob.css?ver=d81e11"`)
        .replace(script, `${script}<script>${policy}</script>`);
      if (!output.includes(policy)) throw new Error("Could not install the notification policy");
    }
    fs.writeFileSync(path.join(directory, name), output);
  }
  console.log("Built the pinned Stremio interface with casting and Trakt notifications suppressed");
}

build().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
