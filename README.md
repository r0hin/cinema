# Cinema

Stremio makes it a little difficult to stream remotely, so I created this fly.io scaffold to run your own Stremio server. Make sure to use HTTPS!

Open the Fly app URL to use the bundled Stremio interface. It suppresses the casting-device update and expired Trakt authentication notifications in all languages; other notifications remain enabled. The streaming server is selected automatically.

Run `pnpm build` and `pnpm test` before deploying. Docker also builds and verifies the interface. The upstream shell and JavaScript are pinned by SHA-256 checksums in `webui/build.js`; review upstream changes before updating those checksums. Styles and artwork load from Stremio. The interface now uses the Fly app's origin, so you may need to sign in to Stremio again on first use.

![](2024-12-05-13-40-49.png)

The player includes **Save to compute-alt**, which downloads the current video
and selected/preferred English subtitle into `/home/ubuntu/Movies`. It uses your
existing Stremio sign-in; download access is restricted to the configured account.
Use `https://play.r0h.in/movie-title` in IINA's Open URL to stream the closest saved
or downloading movie. Add the release year to distinguish remakes. Unfinished
movies proxy the original Stremio stream; completed movies stream from disk.
See [remote/README.md](remote/README.md) for the architecture, API, and operations.
