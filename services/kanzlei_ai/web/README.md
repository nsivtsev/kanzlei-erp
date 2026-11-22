# Kanzlei AI web client

Use Node 22.13+ or a supported newer even-numbered release. Install with
`npm ci` and set `KANZLEI_AI_PROXY_TARGET=http://127.0.0.1:PORT` to a
disposable loopback API endpoint before running `npm run dev`, `npm test`,
`npm run build` or `npm run preview`. Vite serves local development at `/`
and proxies `/v1/`. The production bundle and local preview use `/chat/`,
with API requests under `/chat/v1/`. The `npm run test:browser` suite uses
Chrome with its own synthetic stub and Vite servers on ports 19991 and 19992.

The Farbschema control follows the browser preference until the user chooses
Hell or Dunkel. Only that preference is saved in local storage. Citations open
the cited page next to the conversation or in a full-screen viewer on phones.
Dokumente loads one authorized, filtered catalogue page on demand. It can show
an empty page with a continuation when no file on that ERP page matches the
processing filter. Visible history and open citations have a separate live
permission check on focus and while the conversation is visible.

Inter and Space Grotesk are bundled as variable WOFF2 files from Google Fonts
(Inter v20, Space Grotesk v22). Their SIL Open Font License notices ship in
`public/licenses/`. The browser loads the subsets it needs from the app origin.

The opt-in `real-milestone.browser.spec.ts` is an entry point for an already provisioned isolated synthetic fixture and authenticated ERP session. Set `KANZLEI_AI_REAL_MILESTONE=1`, `KANZLEI_AI_REAL_URL`, `KANZLEI_AI_REAL_CLIENT`, `KANZLEI_AI_REAL_QUESTION`, and `KANZLEI_AI_REAL_ANSWER` to exercise login, a question, and the cited page. It does not provision/upload a PDF or establish cross-client and unauthorized-user isolation; those remain required in the milestone harness. Never provide real document data. All routes must be enabled explicitly in the isolated service.
