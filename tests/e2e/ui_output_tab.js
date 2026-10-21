// Browser check of the Output tab. Needs: the app on :8000 (fresh folder), a fake MQTT broker with login on
// $AUTH_PORT (slow CONNACK, user epicmqtt / s3cret), another without login on $ANON_PORT, and a CLOSED port $DEAD_PORT.
//   node tests/e2e/ui_output_tab.js <screenshot-dir>
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const OUT = process.argv[2] || '.';
const AUTH = process.env.AUTH_PORT, ANON = process.env.ANON_PORT, DEAD = process.env.DEAD_PORT;
let failed = 0;
const check = (c, m) => { console.log((c ? 'ok  : ' : 'FAIL: ') + m); if (!c) failed = 1; };
(async () => {
  const browser = await chromium.launch({ ...(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {}), args: ['--no-sandbox'] });
  const ctx = await browser.newContext({ viewport: { width: 1300, height: 1000 } });
  await ctx.route(/fonts\.(googleapis|gstatic)\.com/, r => r.abort());
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  const B = 'http://localhost:8000';
  const settings = async () => (await page.request.get(B + '/api/settings')).json();
  const vis = sel => page.locator(sel).evaluate(e => !e.hidden && getComputedStyle(e).display !== 'none');

  await page.goto(B + '/');
  await page.request.post(B + '/api/auth/setup', { data: { username: 'admin', password: 'e2e-pass' } });
  await page.goto(B + '/');
  await page.waitForSelector('#app', { state: 'visible' });
  await page.click('button.tab[data-tab="output"]');
  await page.selectOption('#conn-type', 'mqtt');
  if (!(await page.isChecked('#out-enabled'))) await page.click('label.switch-row:has(#out-enabled)');   // styled toggle: click the visible switch

  // 1) required fields: nothing saved, clear messages, focus on the first problem
  await page.fill('#mqtt-broker', ''); await page.fill('#mqtt-port', ''); await page.fill('#mqtt-topic-tmpl', '');
  await page.fill('#mqtt-conn-topic-tmpl', '');
  await page.click('#btn-save-output');
  check(await page.textContent('#err-mqtt_broker') === 'Required.', 'broker: "Required." shown under the box');
  check(await page.textContent('#err-mqtt_port') === 'Required.' && await page.textContent('#err-mqtt_topic_template') === 'Required.', 'port and topic template also flagged');
  check((await page.textContent('#output-status')).includes('nothing was saved'), 'banner says nothing was saved');
  check((await settings()).server_enabled !== 'true', 'invalid form was NOT stored');
  await page.screenshot({ path: OUT + '/output_required.png' });

  // 2) bad formats
  await page.fill('#mqtt-broker', 'mqtt://103.224.93.124'); await page.fill('#mqtt-port', '99999');
  await page.fill('#mqtt-topic-tmpl', 'ksb/meter{meter_c'); await page.fill('#mqtt-conn-topic-tmpl', 'ksb/connectivity/{entity_id}');
  await page.click('#btn-save-output');
  check((await page.textContent('#err-mqtt_broker')).includes('no mqtt://'), 'broker with mqtt:// explained');
  check((await page.textContent('#err-mqtt_port')).includes('1 to 65535'), 'port 99999 explained');
  check((await page.textContent('#err-mqtt_topic_template')).includes('braces'), 'broken topic template explained');
  await page.fill('#mqtt-broker', '127.0.0.1');
  check(await page.textContent('#err-mqtt_broker') === '', 'error disappears as soon as the box is edited');

  // 3) wrong password on a slow broker: loader + "Checking connection" are visible, settings still saved, failure explained
  await page.fill('#mqtt-port', AUTH); await page.fill('#mqtt-topic-tmpl', 'ksb/meter{meter_code}/{label}');
  await page.fill('#mqtt-username', 'epicmqtt'); await page.fill('#mqtt-password', 'wrong');
  await page.fill('#mqtt-resp-topic', 'ksb/server/response/idms'); await page.fill('#mqtt-resp-timeout', '15');
  const click = page.click('#btn-save-output');
  await page.waitForFunction(() => /Checking connection/.test(document.getElementById('busy-text').textContent) && !document.getElementById('busy-overlay').hidden, null, { timeout: 8000 });
  check(true, 'full-page loader shown with "' + await page.textContent('#busy-text') + '"');
  await page.waitForSelector('#top-progress.on', { timeout: 3000 });   // appears after 200 ms so fast requests never flash
  check(true, 'top progress bar is running');
  check(await page.locator('#btn-save-output').isDisabled(), 'Save button disabled while working (no double submit)');
  check((await page.textContent('#output-status')).includes('Checking connection'), 'status panel shows "Checking connection..."');
  await page.screenshot({ path: OUT + '/output_checking.png' });
  await click;
  await page.waitForFunction(() => document.getElementById('busy-overlay').hidden);
  const st1 = await page.textContent('#output-status');
  check(st1.includes('Saved, but the connection check failed') && st1.includes('rejected'), 'result: saved + login rejected -> "' + st1.replace(/\s+/g, ' ').slice(0, 150) + '..."');
  check((await settings()).mqtt_password === 'wrong', 'settings were saved even though the login is wrong');
  check(await page.locator('#btn-save-output').isEnabled(), 'buttons usable again');
  await page.screenshot({ path: OUT + '/output_login_failed.png' });

  // 4) correct password
  await page.fill('#mqtt-password', 's3cret');
  await page.click('#btn-save-output');
  await page.waitForFunction(() => /Saved and connected/.test(document.getElementById('output-status').textContent), null, { timeout: 12000 });
  const st2 = await page.textContent('#output-status');
  check(st2.includes('Logged in as epicmqtt') && st2.includes('Allowed to listen on ksb/server/response/idms') && st2.includes('Settings saved'), 'result: saved and connected, with every check listed');
  await page.screenshot({ path: OUT + '/output_connected.png' });
  await page.waitForFunction(() => /BROKER CONNECTED/i.test(document.body.innerText), null, { timeout: 12000 }).then(
    () => check(true, 'top-right chip turned to BROKER CONNECTED by itself (live client picked up the new password)'),
    () => check(false, 'chip did not turn to BROKER CONNECTED'));

  // 5) Test connection (no save) against a closed port
  await page.fill('#mqtt-port', DEAD);
  await page.click('#btn-test-output');
  await page.waitForFunction(() => /refused/.test(document.getElementById('output-status').textContent), null, { timeout: 12000 });
  check(true, 'closed port reported as refused');
  check((await settings()).mqtt_port === AUTH, 'Test connection does not save');

  // 6) show password toggle
  await page.check('#mqtt-show-pw'); check(await page.getAttribute('#mqtt-password', 'type') === 'text', 'show-password toggle works');
  await page.uncheck('#mqtt-show-pw');

  // 7) sign out really signs out (the cookie is HttpOnly)
  await page.click('#btn-logout');
  await page.waitForSelector('#login-screen', { state: 'visible' });
  check((await (await page.request.get(B + '/api/auth/status')).json()).logged_in === false, 'Sign out really ends the session');
  check(errors.length === 0, 'no JavaScript errors' + (errors.length ? ': ' + errors.join(' | ') : ''));
  await browser.close();
  process.exit(failed);
})().catch(e => { console.log('FAIL (exception):', e.message.split('\n')[0]); process.exit(1); });
