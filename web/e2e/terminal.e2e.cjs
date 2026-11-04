/* Interactive end-to-end test of the Sthitaprajna terminal against a running server.
 *
 *   sthitaprajna serve --speed 120        # in one terminal (SIM mode)
 *   node web/e2e/terminal.e2e.cjs         # in another (needs playwright + a Chromium)
 *
 * Exits non-zero on the first failed assertion. Uses only operator actions the spec allows (D6).
 */
const path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); } catch {
  ({ chromium } = require(path.join(require("child_process").execSync("npm root -g").toString().trim(), "playwright")));
}
const BASE = process.env.SP_URL || "http://127.0.0.1:8788";
const results = [];
function ok(cond, name) {
  results.push({ name, pass: !!cond });
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  if (!cond) { process.exitCode = 1; }
}
async function api(p, body) {
  const r = await fetch(BASE + p, body ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) } : {});
  return r.json();
}

(async () => {
  const exe = process.env.CHROMIUM || "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";
  const browser = await chromium.launch(require("fs").existsSync(exe) ? { executablePath: exe } : {});
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));

  await api("/api/actions/speed", { speed: 120 });
  await page.goto(`${BASE}/#/dashboard`, { waitUntil: "networkidle" });
  await page.waitForSelector(".hero-number");
  ok((await page.textContent(".hero-number")).includes("₹"), "dashboard hero shows equity in rupees");
  const unbounded = (await api("/api/state")).risk_mode === "unbounded";
  console.log(`INFO  risk mode ${unbounded ? "unbounded (D-031)" : "bounded"}`);
  if (unbounded) {
    ok(await page.isVisible("text=Free capital") && !(await page.$(".gear-opt")), "unbounded: capital gauge, no gear selector");
    ok(!(await page.$("text=Pre-market state check")) && !(await page.$(".stepper")), "unbounded: no state check and no staging stepper");
  } else {
    ok(await page.isVisible(".gear-opt.on"), "gear selector shows the active gear");
  }
  const controls = await page.$$eval("button, a, [role=button]", (els) => els.map((x) => x.innerText.trim()));
  ok(!controls.some((l) => /^(buy|sell)\b/i.test(l)), "no manual Buy/Sell control anywhere on the dashboard (D6)");

  // pre-market state check: a 'No' lowers the gear for today (never raises)
  const before = (await api("/api/state")).gear.effective;
  if (unbounded) {
    ok(true, "state check does not exist in unbounded mode (skipped)");
  } else if (await page.$("text=Pre-market state check")) {
    await page.click(".card:has-text('Pre-market state check') .seg >> nth=1 >> text=No");
    await page.click("text=Submit check");
    await page.waitForTimeout(800);
    const after = (await api("/api/state")).gear.effective;
    const order = ["ATTACK", "PRESS", "GUARD", "PRESERVE", "HALTED"];
    ok(order.indexOf(after) === Math.min(4, order.indexOf(before) + 1), `state check 'No' lowers gear one step (${before} -> ${after})`);
  } else {
    ok(true, "state check already answered this session (skipped)");
  }

  // command palette runs a read-only command
  await page.keyboard.press("Control+k");
  await page.fill(".palette-input", "/status");
  await page.keyboard.press("Enter");
  await page.waitForSelector(".palette-reply");
  ok((await page.textContent(".palette-reply")).includes(unbounded ? "In trades" : "Gear"), "command palette /status replies with equity");
  await page.keyboard.press("Escape");

  // scanner: the whole universe ranked, filters work, a row opens the chart
  await page.click("nav >> text=Scanner");
  await page.waitForSelector("table.scanner-t tbody tr", { timeout: 20000 });
  ok((await page.$$("table.scanner-t tbody tr")).length > 5, "scanner ranks the universe");
  await page.click(".seg >> text=Options-eligible");
  await page.waitForTimeout(1200);
  ok((await page.$$eval("table.scanner-t tbody tr", (rs) => rs.every((r) => r.innerText.includes("lot")))), "options-eligible filter shows only stocks with options");
  const sym = (await page.textContent("table.scanner-t tbody tr >> nth=0 >> b")).trim();
  await page.click("table.scanner-t tbody tr >> nth=0");
  await page.waitForFunction((s) => document.querySelector("h1")?.innerText === s, sym, { timeout: 10000 });
  ok(true, `clicking ${sym} opens its chart in the terminal`);

  // option chain switches underlying; lot comes from rules
  await page.click("nav >> text=Option Chain");
  await page.waitForSelector("table.chain");
  await page.click(".seg >> text=SENSEX");
  await page.waitForFunction(() => document.body.innerText.includes("Lot 20"));
  ok(true, "option chain switches to SENSEX with lot 20");
  ok(await page.isVisible(".spot-line"), "chain shows the spot line at ATM");

  // failure injection flips the arbitration table
  await page.click("nav >> text=System");
  await page.click("text=Kill primary feed");
  await page.waitForFunction(() => [...document.querySelectorAll("tr")].some((r) => r.innerText.startsWith("▶ Dhan down")), null, { timeout: 15000 });
  ok(true, "killing the primary feed moves arbitration to PRIMARY_DOWN");
  const st = await api("/api/state");
  ok(st.risk.halts.some((h) => h.includes("PRIMARY_DOWN")), "entries halted while the primary feed is down");
  await page.click("text=Clear all faults");
  await page.waitForFunction(() => [...document.querySelectorAll("tr")].some((r) => r.innerText.startsWith("▶ Both healthy")), null, { timeout: 20000 });
  ok(true, "clearing faults restores BOTH_AGREE");

  // kill switch: one step flatten + halt, resume only after review
  await page.click(".btn-kill");
  await page.click(".btn-kill");  // confirm
  await page.waitForTimeout(1500);
  const killed = await api("/api/state");
  ok(killed.risk.kill_switch === true, "KILL engages the kill switch");
  await page.waitForTimeout(2500);
  ok((await api("/api/state")).positions.length === 0, "KILL flattens every position");
  await page.click("nav >> text=The Monk");
  const resume = unbounded ? "button:text-is('Resume')" : "text=Resume after review";
  await page.waitForSelector(resume);
  ok(await page.isDisabled(resume), "resume is disabled until the review box is ticked");
  await page.check(unbounded ? "text=I have reviewed what happened" : "text=I have reviewed what happened (required)");
  await page.click(resume);
  await page.waitForTimeout(800);
  ok((await api("/api/state")).risk.kill_switch === false, "resume after review releases the kill switch");

  // research: Monte Carlo renders a fan chart and a histogram
  await page.click("nav >> text=Research");
  await page.waitForSelector("svg[aria-label='Monte Carlo equity percentile bands']", { timeout: 20000 });
  ok(true, "Monte Carlo fan chart renders");
  ok(await page.isVisible("svg[aria-label='Distribution of terminal equity']"), "terminal-equity histogram renders");
  await page.waitForSelector("text=D12.1 reproduced by this engine");
  await page.waitForFunction(() => document.querySelectorAll("svg[aria-label='matches spec']").length === 9, null, { timeout: 30000 });
  ok(true, "all 9 D12.1 rows match the master prompt");

  // positions page actions exist only as risk-reducing controls
  await page.click("nav >> text=Positions");
  await page.waitForSelector("h1:has-text('Positions')");
  const labels = await page.$$eval("button", (bs) => bs.map((b) => b.innerText.trim()));
  ok(!labels.some((l) => /^(buy|sell|add|increase|widen)/i.test(l)), "positions page offers no open/add/widen controls");

  // theme toggle
  await page.click("button[aria-label='Toggle theme']");
  ok((await page.getAttribute("html", "data-theme")) === "dark", "theme toggles to dark");
  await page.click("button[aria-label='Toggle theme']");

  ok(errors.length === 0, `no console/page errors (${errors.slice(0, 3).join(" | ")})`);
  await browser.close();
  const failed = results.filter((r) => !r.pass).length;
  console.log(`\n${results.length - failed}/${results.length} E2E checks passed`);
})().catch((e) => { console.error(e); process.exit(1); });
