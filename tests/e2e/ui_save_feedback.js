// Browser check for the ZLAN / Meter / Parameter tables (needs Node + Playwright + Chromium).
//   1. start the app in an empty folder:   (cd /tmp/x && ln -s <repo>/static static && ln -s <repo>/templates templates && python <repo>/app.py)
//   2. run:   node tests/e2e/ui_save_feedback.js <label> <screenshot.png>
// Checks: typed-but-unsaved values survive the automatic table refresh, "Saved" appears next to the
// Save button, invalid values are refused with a message, the meters table behaves the same.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const label = process.argv[2];
let failed = 0;
const check = (c, m) => { console.log((c ? 'ok  : ' : 'FAIL: ') + m); if (!c) failed = 1; };
(async () => {
  const browser = await chromium.launch({ ...(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {}), args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  await ctx.route(/fonts\.(googleapis|gstatic)\.com/, r => r.abort());
  const page = await ctx.newPage();
  const B = 'http://localhost:8000';
  await page.goto(B + '/');
  await page.request.post(B + '/api/auth/setup', { data: { username: 'admin', password: 'e2e-pass' } });
  for (const [i, ip] of ['10.9.9.1', '10.9.9.2', '10.9.9.3'].entries()) {
    await page.request.post(B + '/api/zlans', { data: { zlan_ip: ip, zlan_label: 'Z' + i, default_polling_interval: 15, port: 15999 } });
    await page.request.post(B + `/api/zlans/${ip}/meters`, { data: { meter_codes: ['M' + i + '01', 'M' + i + '02'] } });
    await page.request.put(B + `/api/zlans/${ip}/publish-interval`, { data: { default_publish_interval: 180 } });
  }
  await page.goto(B + '/');
  await page.waitForSelector('#app', { state: 'visible' });
  await page.click('button.tab[data-tab="zlans"]');
  await page.waitForSelector('#zlan-pub-10\\.9\\.9\\.1');
  const pub = '#zlan-pub-10\\.9\\.9\\.1', pub2 = '#zlan-pub-10\\.9\\.9\\.2';

  // 1) type, then let the page refresh itself the way it does on every connectivity event / timer
  await page.fill(pub, '77');
  await page.fill(pub2, '55');                       // a second, different row, also unsaved
  await page.evaluate(async () => { await loadZlans(); await loadMeters(); });
  await page.waitForTimeout(6000);                   // + the 5 s meters timer and any websocket events
  check(await page.inputValue(pub) === '77', `${label}: typed value survives the automatic refresh (is "${await page.inputValue(pub)}")`);

  // 2) save row 1 only -> "Saved" next to ITS button, row 2's unsaved typing is not lost
  await page.click(`tr:has(${pub}) >> nth=0 >> button >> nth=1`);
  await page.waitForTimeout(700);
  const flag = await page.locator(`${pub} ~ .save-flag`).allTextContents();
  check(flag.length === 1 && /Saved/.test(flag[0]), `${label}: "Saved" appears next to the Save button (${JSON.stringify(flag)})`);
  await page.evaluate(async () => { await loadZlans(); });
  const flag2 = await page.locator(`${pub} ~ .save-flag`).allTextContents();
  check(flag2.length === 1, `${label}: the Saved note also survives a refresh`);
  const z = await (await page.request.get(B + '/api/zlans')).json();
  check(z.find(x => x.zlan_ip === '10.9.9.1').default_publish_interval === 77, `${label}: 77 really stored in the database`);
  check(await page.inputValue(pub2) === '55', `${label}: other row's unsaved value kept (is "${await page.inputValue(pub2)}")`);

  // 3) invalid value: must say so, and keep what was typed
  const int1 = '#zlan-int-10\\.9\\.9\\.1';
  await page.fill(int1, '0');
  await page.click(`tr:has(${int1}) >> nth=0 >> button >> nth=0`);
  await page.waitForTimeout(700);
  const bad = await page.locator(`${int1} ~ .save-flag`).allTextContents();
  check(bad.length === 1 && /Not saved|✗|least/.test(bad[0]), `${label}: invalid value is refused with a message (${JSON.stringify(bad)})`);
  const z2 = await (await page.request.get(B + '/api/zlans')).json();
  check(z2.find(x => x.zlan_ip === '10.9.9.1').default_polling_interval === 15, `${label}: invalid value not stored`);

  // 4) meters table behaves the same
  const mp = '#meter-pub-M001';
  await page.fill(mp, '45');
  await page.evaluate(async () => { await loadMeters(); });
  check(await page.inputValue(mp) === '45', `${label}: meter field keeps typing across refresh`);
  await page.click(`tr:has(${mp}) >> nth=0 >> button >> nth=1`);
  await page.waitForTimeout(700);
  const mflag = await page.locator(`${mp} ~ .save-flag`).allTextContents();
  check(mflag.length === 1 && /Saved/.test(mflag[0]), `${label}: meter Save shows "Saved" (${JSON.stringify(mflag)})`);
  await page.screenshot({ path: process.argv[3] });
  await browser.close();
  process.exit(failed);
})().catch(e => { console.log('FAIL (exception):', e.message.split('\n')[0]); process.exit(1); });
