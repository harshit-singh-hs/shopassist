// Vercel build step: point the static UI at the API (set SHOPASSIST_API in the Vercel project settings,
// e.g. https://<hf-user>-shopassist.hf.space). Fails the build if it's missing, rather than shipping a UI
// that calls itself and gets 404s.
import { writeFileSync } from "node:fs";

const api = (process.env.SHOPASSIST_API || "").trim().replace(/\/$/, "");
if (!/^https?:\/\//.test(api)) {
  console.error("Set SHOPASSIST_API to the backend URL, e.g. https://<hf-user>-shopassist.hf.space");
  process.exit(1);
}
writeFileSync("web/config.js", `window.SHOPASSIST_API = ${JSON.stringify(api)};\n`);
console.log(`UI will call the API at ${api}`);
