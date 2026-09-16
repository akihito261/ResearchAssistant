# Bundled PDF.js

Research Assistant ships PDF.js locally so the reader works without a CDN or
network connection.

- Version: 5.7.284
- Upstream: https://github.com/mozilla/pdf.js/releases/tag/v5.7.284
- Distribution archive: `pdfjs-5.7.284-legacy-dist.zip`
- Archive SHA-256: `B1EDDED128A7E50E7818BFE16564EB4012DD3F13F2847F9F94100C96567AFBCC`
- License: Apache License 2.0; see `LICENSE` in this directory.

`web/research_assistant.css` and its reference in `web/viewer.html` are the
project-specific customization. The remaining runtime files come from the
official legacy generic distribution. The legacy build is used because Qt
6.11.1 embeds Chromium 140, which does not yet provide all JavaScript APIs used
by PDF.js's modern build.

Source maps and the upstream demo PDF are intentionally omitted. Runtime
modules, fonts, CMaps, ICC profiles, WebAssembly decoders, locale files, and
their licenses remain bundled.
