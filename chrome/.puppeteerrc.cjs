// Puppeteer downloads Chrome for Testing into chrome/.cache/puppeteer (gitignored).
// Branded Google Chrome 137+ ignores --load-extension, so tests never use /usr/bin/google-chrome.
const { join } = require("path");
module.exports = {
  cacheDirectory: join(__dirname, ".cache", "puppeteer"),
  chrome: { skipDownload: false },
  "chrome-headless-shell": { skipDownload: true },
  firefox: { skipDownload: true },
};
