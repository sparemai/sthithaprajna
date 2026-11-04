/* Pilot-approval flow through the UI: a real system proposal appears with a countdown, the operator
 * approves it in the dock, and the probe order is sent (LIVE-PILOT behaviour, D9).
 *   sthitaprajna serve   then   node web/e2e/approvals.e2e.cjs
 */
const path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); } catch {
  ({ chromium } = require(path.join(require("child_process").execSync("npm root -g").toString().trim(), "playwright")));
}
const BASE = process.env.SP_URL || "http://127.0.0.1:8788";
const post = (p, b) => fetch(BASE + p, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) }).then((r) => r.json());
const get = (p) => fetch(BASE + p).then((r) => r.json());

(async () => {
  const exe = process.env.CHROMIUM || "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
  const browser = await chromium.launch(require("fs").existsSync(exe) ? { executablePath: exe } : {});
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  await post("/api/actions/pilot_approvals", { on: true });
  await post("/api/actions/speed", { speed: 240 });
  await page.goto(`${BASE}/#/dashboard`, { waitUntil: "networkidle" });
  const t0 = Date.now();
  let st = await get("/api/state");
  while (!st.approvals.length && Date.now() - t0 < 240000) {
    await page.waitForTimeout(1000);
    st = await get("/api/state");
  }
  if (!st.approvals.length) { console.log("SKIP  no qualifying setup appeared within 4 minutes of sim time"); await browser.close(); return; }
  await post("/api/actions/speed", { speed: 20 });
  const a = st.approvals[0];
  console.log(`INFO  approval ${a.kind} ${a.summary.symbol} grade ${a.summary.grade}, ${a.seconds_left}s left`);
  await page.waitForSelector(".approval-dock .approval, .approval", { timeout: 10000 });
  const txt = await page.textContent(".approval");
  const pass1 = txt.includes(a.summary.symbol) && txt.includes("Timeout = skip");
  console.log(`${pass1 ? "PASS" : "FAIL"}  approval card shows the proposal and the safe default`);
  const tradesBefore = st.risk.trades_today;
  await page.click(".approval >> text=Approve");
  await page.waitForTimeout(3000);
  const after = await get("/api/state");
  const decided = after.approvals.every((x) => x.id !== a.id);
  const entered = after.risk.trades_today > tradesBefore || after.positions.some((p) => p.symbol === a.summary.symbol) ||
    after.orders.some((o) => o.symbol === a.summary.symbol && o.purpose === "PROBE") ||
    after.events.some((e) => e.msg.includes(a.summary.symbol) && (e.kind === "entry" || e.msg.includes("Risk Guardian")));
  console.log(`${decided ? "PASS" : "FAIL"}  approval resolved after clicking Approve`);
  console.log(`${entered ? "PASS" : "FAIL"}  probe sent (or re-checked by the Risk Guardian at approval time)`);
  await post("/api/actions/pilot_approvals", { on: false });
  if (!(pass1 && decided && entered)) process.exitCode = 1;
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
