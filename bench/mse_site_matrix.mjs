// Run a reproducible matrix of public, non-DRM MSE sites/cases through the
// PDM extension and the native host.
//
// Prerequisites:
//   * Edge/Chrome with --remote-debugging-port and the PDM extension loaded.
//   * Native host registered; PDM_DOWNLOADER_CAPTURE_DIR / PDM_DOWNLOADER_FFMPEG
//     set in the browser's environment.
//   * For the local WebM control: `python browser/serve_demo.py --port 8000`.
//
// Usage:
//   node bench/mse_site_matrix.mjs <cdp> [out-dir]
//
// Each case opens its own tab, starts capture before playback, waits for the
// media clock to advance, stops and writes <out-dir>/<id>.json plus summary.json.

const cdp = process.argv[2] || "http://127.0.0.1:9335";
const outDir = process.argv[3] || "build/site-matrix/run";
const fs = await import("node:fs");
const path = await import("node:path");
fs.mkdirSync(outDir, { recursive: true });

const extensionId = "hfiihggbmdmoofcadgmkingenjgcaend";
const popupUrl = `chrome-extension://${extensionId}/popup.html`;
const dashif = (asset) =>
  "https://reference.dashif.org/dash.js/latest/samples/dash-if-reference-player/index.html?url=" +
  asset;
const fmp4Hls = (asset) =>
  "https://hls-js.netlify.app/demo/?src=" +
  encodeURIComponent(asset) +
  "&demoConfig=" +
  Buffer.from(JSON.stringify({ enableStreaming: false, pad: 0 })).toString("base64");

const cases = [
  {
    id: "dashif-akamai-bbb",
    kind: "dashif",
    match: "dashif.org",
    url: dashif("https://dash.akamaized.net/akamai/bbb_30fps/bbb_30fps.mpd"),
    seconds: 8,
  },
  {
    id: "dashif-shaka-angel-one",
    kind: "dashif",
    match: "dashif.org",
    url: dashif("https://storage.googleapis.com/shaka-demo-assets/angel-one/dash.mpd"),
    seconds: 8,
  },
  {
    id: "dashif-unified-tears",
    kind: "dashif",
    match: "dashif.org",
    url: dashif(
      "https://demo.unified-streaming.com/k8s/features/stable/video/tears-of-steel/tears-of-steel.ism/.mpd"
    ),
    seconds: 8,
  },
  {
    id: "hlsjs-shaka-angel-fmp4",
    kind: "hlsjs",
    match: "hlsjs.video-dev.org",
    url: fmp4Hls("https://storage.googleapis.com/shaka-demo-assets/angel-one-hls/hls.m3u8"),
    seconds: 8,
  },
  {
    id: "local-webm",
    kind: "webm",
    match: "mse-webm-test.html",
    url: "http://127.0.0.1:8000/browser/mse-webm-test.html",
    seconds: 5,
  },
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    sleep(ms).then(() => {
      throw new Error(`${label} timed out after ${ms}ms`);
    }),
  ]);
}

class Client {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (!message.id || !this.pending.has(message.id)) return;
      const { resolve, reject } = this.pending.get(message.id);
      this.pending.delete(message.id);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result);
    };
  }
  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  async evaluate(expression, awaitPromise = true) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(
        result.exceptionDetails.text +
          " " +
          JSON.stringify(result.exceptionDetails.exception?.description || "")
      );
    }
    return result.result.value;
  }
  close() {
    this.ws.close();
  }
}

async function connect(url) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(url);
    ws.onopen = () => resolve(new Client(ws));
    ws.onerror = () => reject(new Error(`cannot connect ${url}`));
  });
}

async function listTargets() {
  return (await fetch(`${cdp}/json/list`)).json();
}

async function ensurePopup() {
  let targets = await listTargets();
  let popup = targets.find(
    (item) => item.type === "page" && item.url.includes("chrome-extension://")
  );
  if (!popup) {
    await fetch(`${cdp}/json/new?${encodeURIComponent(popupUrl)}`, {
      method: "PUT",
    });
    await sleep(500);
    targets = await listTargets();
    popup = targets.find(
      (item) => item.type === "page" && item.url.includes("chrome-extension://")
    );
  }
  if (!popup) throw new Error("extension popup target is unavailable");
  return connect(popup.webSocketDebuggerUrl);
}

async function openPage(url) {
  const response = await withTimeout(fetch(`${cdp}/json/new?${encodeURIComponent(url)}`, {
    method: "PUT",
  }), 15000, "open tab");
  if (!response.ok) throw new Error(`cannot open tab: HTTP ${response.status}`);
  const target = await response.json();
  for (let i = 0; i < 60; i += 1) {
    const targets = await listTargets();
    const page = targets.find((item) => item.id === target.id);
    if (page) return connect(page.webSocketDebuggerUrl);
    await sleep(250);
  }
  throw new Error("opened tab did not expose a CDP target");
}

async function waitForPageReady(client, match, timeout = 30000) {
  const deadline = Date.now() + timeout;
  let last = null;
  while (Date.now() < deadline) {
    try {
      last = await client.evaluate(
        "({url: location.href, ready: document.readyState})"
      );
      if (last && last.ready === "complete" && last.url.includes(match)) {
        return last;
      }
    } catch (error) {
      last = { error: String(error) };
    }
    await sleep(500);
  }
  throw new Error(`page did not become ready: ${JSON.stringify(last)}`);
}

async function pageState(client) {
  return withTimeout(client.evaluate(`(() => {
    const v = document.querySelector("video");
    const log = document.getElementById("log");
    return {
      title: document.title,
      url: location.href,
      paused: v ? v.paused : null,
      readyState: v ? v.readyState : null,
      currentTime: v ? v.currentTime : null,
      duration: v ? v.duration : null,
      buffers: v && v.buffered ? v.buffered.length : 0,
      log: log ? log.textContent.slice(-800) : "",
      status: (document.getElementById("statusOut") || {}).textContent || "",
      error: (document.getElementById("errorOut") || {}).textContent || "",
    };
  })()`), 10000, "page state");
}

async function activateTab(popup, match) {
  let result = null;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    result = await withTimeout(popup.evaluate(`(async () => {
      const tabs = await chrome.tabs.query({});
      const matched = tabs.filter((tab) => (tab.url || "").includes(${JSON.stringify(match)}));
      if (!matched.length) return { ok: false, error: "no matching tab", tabs: tabs.map((t) => t.url) };
      const tab = matched[matched.length - 1];
      await chrome.tabs.update(tab.id, { active: true });
      await new Promise((resolve) => setTimeout(resolve, 400));
      return { ok: true, id: tab.id, url: tab.url };
    })()`), 15000, "activate tab");
    if (result.ok) break;
    await sleep(1000);
  }
  if (!result.ok) throw new Error(JSON.stringify(result));
  return result;
}

async function startCapture(popup) {
  const reply = await withTimeout(
    popup.evaluate(
      `(async () => await chrome.runtime.sendMessage({ type: "mseStart" }))()`
    ),
    20000,
    "mseStart"
  );
  if (!reply || !reply.ok) throw new Error(`mseStart: ${JSON.stringify(reply)}`);
  return reply;
}

async function trigger(kind, page) {
  if (kind === "dashif") {
    return withTimeout(page.evaluate(`(async () => {
      for (let i = 0; i < 40; i += 1) {
        if (document.getElementById("btn-load")) break;
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
      const load = document.getElementById("btn-load");
      if (load) load.click();
      await new Promise((resolve) => setTimeout(resolve, 6000));
      let v = document.querySelector("video");
      if (v && v.readyState < 1 && load) load.click();
      await new Promise((resolve) => setTimeout(resolve, 3000));
      v = document.querySelector("video");
      if (v) { v.muted = true; v.play().catch(() => {}); }
      return { clickedLoad: Boolean(load), paused: v ? v.paused : null,
               readyState: v ? v.readyState : null, currentTime: v ? v.currentTime : null };
    })()`), 45000, "dashif trigger");
  }
  if (kind === "hlsjs") {
    return withTimeout(page.evaluate(`(async () => {
      const cb = document.getElementById("enableStreaming");
      if (cb && !cb.checked) cb.click();
      await new Promise((resolve) => setTimeout(resolve, 1500));
      const v = document.querySelector("video");
      if (v) { v.muted = true; v.play().catch(() => {}); }
      return { toggled: Boolean(cb), paused: v ? v.paused : null,
               readyState: v ? v.readyState : null, currentTime: v ? v.currentTime : null };
    })()`), 30000, "hlsjs trigger");
  }
  if (kind === "webm") {
    return withTimeout(page.evaluate(`(async () => {
      const done = await Promise.race([
        playWebm().then(() => "done", (error) => String(error)),
        new Promise((resolve) => setTimeout(() => resolve("timeout"), 20000))
      ]);
      return { playWebm: done };
    })()`), 30000, "webm trigger");
  }
  throw new Error(`unknown case kind ${kind}`);
}

async function stopCapture(popup) {
  return popup.evaluate(
    `(async () => await chrome.runtime.sendMessage({ type: "mseStop" }))()`
  );
}

async function runCase(caseSpec, popup) {
  const record = { id: caseSpec.id, url: caseSpec.url, kind: caseSpec.kind, ok: false };
  let page = null;
  try {
    page = await openPage(caseSpec.url);
    await page.send("Runtime.enable");
    await page.send("Page.enable");
    await sleep(1000);
    record.pageReady = await waitForPageReady(page, caseSpec.match);
    record.activated = await activateTab(popup, caseSpec.match);
    record.start = await startCapture(popup);
    record.trigger = await trigger(caseSpec.kind, page);

    const deadline = Date.now() + 25000;
    let state = await pageState(page);
    let nudges = 0;
    while (Date.now() < deadline) {
      state = await pageState(page);
      if (typeof state.currentTime === "number" && state.currentTime > 1.0) break;
      if (nudges < 3 && Date.now() > deadline - 20000) {
        nudges += 1;
        try {
          await page.evaluate(`(() => {
            const load = document.getElementById("btn-load");
            if (load) load.click();
            const v = document.querySelector("video");
            if (v) { v.muted = true; v.play().catch(() => {}); }
          })()`);
        } catch (error) {
          /* ignore */
        }
      }
      await sleep(1000);
    }
    record.state = state;
    if (caseSpec.seconds) await sleep(caseSpec.seconds * 200);
    const started = Date.now();
    const reply = await Promise.race([
      stopCapture(popup),
      sleep(160000).then(() => ({ ok: false, error: "CDP mseStop timeout" })),
    ]);
    record.stopSeconds = (Date.now() - started) / 1000;
    record.reply = reply;
    record.ok = Boolean(reply && reply.ok);
    record.trackCount = reply?.track_count ?? 0;
    record.merged = reply?.merged
      ? { path: reply.merged.path, size: reply.merged.size, sha256: reply.merged.sha256 }
      : null;
    record.files = (reply?.files || []).map((f) => ({
      path: f.path,
      mime: f.mime,
      kind: f.kind,
      size: f.size,
      sha256: f.sha256,
    }));
    record.mergeError = reply?.merge_error ?? null;
  } catch (error) {
    record.error = error.stack || String(error);
  } finally {
    try {
      if (page) await page.send("Page.close");
    } catch (error) {
      /* tab may already be gone */
    }
    try {
      page?.close();
    } catch (error) {
      /* ignore */
    }
    await sleep(800);
  }
  fs.writeFileSync(
    path.join(outDir, `${caseSpec.id}.json`),
    JSON.stringify(record, null, 2) + "\n",
    "utf-8"
  );
  return record;
}

async function main() {
  const only = (process.env.PDM_MATRIX_ONLY || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const selected = only.length
    ? cases.filter((item) => only.includes(item.id))
    : cases;
  if (!selected.length) throw new Error(`no cases match ${only.join(",")}`);
  const popup = await ensurePopup();
  const results = [];
  for (const caseSpec of selected) {
    console.log(`\n=== ${caseSpec.id} ===`);
    const record = await runCase(caseSpec, popup);
    console.log(
      `${caseSpec.id}: ok=${record.ok} tracks=${record.trackCount} ` +
        `merged=${record.merged ? "yes" : "no"} stop=${record.stopSeconds ?? "-"}s` +
        (record.error ? ` error=${record.error}` : "")
    );
    results.push(record);
  }
  try {
    popup.close();
  } catch (error) {
    /* ignore */
  }
  const summary = {
    timestamp: new Date().toISOString(),
    cdp,
    cases: results,
  };
  fs.writeFileSync(
    path.join(outDir, "summary.json"),
    JSON.stringify(summary, null, 2) + "\n",
    "utf-8"
  );
  const failed = results.filter((item) => !item.ok);
  console.log(`\nmatrix: ${results.length - failed.length}/${results.length} passed`);
  if (failed.length) process.exit(1);
}

main().catch((error) => {
  console.error("ERROR:", error.stack || error);
  process.exit(2);
});
