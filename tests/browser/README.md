# Console browser checks

`console.test.cjs` uses Node's built-in test runner and Playwright. The console itself
has no Node/build dependency. Playwright is needed here because neither Pytest's
HTTP client nor the standard library executes browser JavaScript or checks layout.

These tests start a temporary local static server and supply isolated HTTP fixtures.
They exercise the browser/API contract without touching the database or calling a
model. The regular Pytest integration tests separately use the real PostgreSQL API
and cover the underlying approval, resume, idempotency and authentication behavior.

With Node.js, Playwright and its Chromium browser already installed:

```bash
node --test tests/browser/console.test.cjs
```

If Playwright lives outside the repository, set `NODE_PATH` to the directory
containing its package. To use an installed Edge or Chrome instead of Playwright's
downloaded Chromium, set `BROWSER_CHANNEL` to `msedge` or `chrome`.

Example PowerShell setup with tools installed outside the repository:

```powershell
$uiTools = Join-Path $env:TEMP 'support-ops-browser-tools'
npm install --prefix $uiTools --no-save --package-lock=false playwright@1.62.1
$env:NODE_PATH = Join-Path $uiTools 'node_modules'
$env:BROWSER_CHANNEL = 'msedge'
node --test tests/browser/console.test.cjs
```

Coverage includes dashboard/search/status filters, empty and error states, API key
recovery, safe handling of untrusted text, trace JSON download, duplicate ticket and
approval submission, polling through completion, reviewer notes and personal keys,
approval conflicts, form correction, out-of-order trace responses, Escape/focus
behavior and widths of 1440, 768 and 375 pixels.
