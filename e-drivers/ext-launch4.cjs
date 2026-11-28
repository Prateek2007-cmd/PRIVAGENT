// Launch real Chrome for Testing + real built extension, keep alive (v4).
// Token read from final4/data/.local_token (same token the backend + API driver use).
// Env overrides: NAV_EXT_DIR (built extension dir), NAV_CHROME_PROFILE.
const { chromium } = require('/home/hatch/workspace/tools/pwshot/node_modules/playwright');
const fs = require('fs');

const BASE = 'http://127.0.0.1:8000';
const TOKEN = fs.readFileSync('/home/hatch/workspace/navigator-audit/final4/data/.local_token', 'utf8').trim();
const EXT = process.env.NAV_EXT_DIR || '/home/hatch/workspace/navigator-audit/final4/extension-dist';
const PROFILE = process.env.NAV_CHROME_PROFILE || '/tmp/final4-chrome-profile';
const CHROME = '/home/hatch/workspace/tools/cft/chrome-linux64/chrome';

async function api(method, path, body) {
  const res = await fetch(BASE + path, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* raw */ }
  return { status: res.status, json, text };
}

(async () => {
  fs.rmSync(PROFILE, { recursive: true, force: true });
  const proxy = process.env.https_proxy || process.env.HTTPS_PROXY || '';
  const context = await chromium.launchPersistentContext(PROFILE, {
    executablePath: CHROME,
    headless: true,
    args: [
      '--no-sandbox', '--disable-dev-shm-usage', '--ignore-certificate-errors',
      `--disable-extensions-except=${EXT}`,
      `--load-extension=${EXT}`,
    ],
    ...(proxy ? { proxy: { server: proxy, bypass: 'localhost,127.0.0.1' } } : {}),
  });
  let sw = null;
  for (let i = 0; i < 30 && !sw; i++) {
    sw = context.serviceWorkers().find((w) => w.url().includes('background.js'));
    if (!sw) await new Promise((r) => setTimeout(r, 500));
  }
  if (!sw) { console.error('FATAL: no extension service worker'); await context.close(); process.exit(1); }
  console.log('extension id:', new URL(sw.url()).hostname);
  await sw.evaluate((tok) => chrome.storage.local.set({
    privagentLocalToken: tok,
    privagentIsolatedProfile: true,
  }), TOKEN);
  console.log('token + isolated-profile flag seeded');
  let connected = false;
  for (let i = 0; i < 60; i++) {
    const s = await api('GET', '/api/isolation/status');
    if (s.json && s.json.extensionConnected) { connected = true; console.log('isolation:', JSON.stringify(s.json)); break; }
    await new Promise((r) => setTimeout(r, 1000));
  }
  if (!connected) { console.error('FATAL: extension never connected'); await context.close(); process.exit(1); }
  console.log('EXTENSION CONNECTED — keeping Chrome alive');
  await new Promise(() => {});
})();
