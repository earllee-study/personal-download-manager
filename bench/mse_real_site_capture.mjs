// Capture a real MSE site through the PDM extension via CDP.
//
// Prerequisites:
//   * Edge/Chrome with the remote debugging port and the PDM extension loaded
//     (the DASH-IF reference player and Akamai BBB MPD are the default target).
//   * Native host registered (use the packaged multi-track host).
//   * The page should be paused; this script starts capture, clicks #btn-load,
//     plays the video, samples progress, then calls mseStop.
//
// Usage:
//   node bench/mse_real_site_capture.mjs <cdp> <page-substring> [seconds] [out-json] [reset-after-seconds]
// `reset-after-seconds` > 0 clears the video src/MediaSource after that many
// seconds to exercise the stop path when a page resets mid-capture.
import fs from "node:fs";

const cdp = process.argv[2] || "http://127.0.0.1:9226";
const pageMatch = process.argv[3] || "dashif.org";
const seconds = Number(process.argv[4] || 15);
const outJson = process.argv[5] || "";
const resetAfter = Number(process.argv[6] || 0);
const extensionId = "hfiihggbmdmoofcadgmkingenjgcaend";
const popupUrl = `chrome-extension://${extensionId}/popup.html`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

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

async function waitForTargets() {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) {
    const targets = await listTargets();
    const page = targets.find(
      (item) => item.type === "page" && item.url.includes(pageMatch)
    );
    let popup = targets.find(
      (item) => item.type === "page" && item.url.includes("chrome-extension://")
    );
    if (page && popup) return { page, popup };
    if (page && !popup) {
      try {
        await fetch(`${cdp}/json/new?${encodeURIComponent(popupUrl)}`, {
          method: "PUT",
        });
      } catch (error) {
        /* retry */
      }
      await sleep(500);
      const next = await listTargets();
      popup = next.find(
        (item) => item.type === "page" && item.url.includes("chrome-extension://")
      );
      if (popup) return { page, popup };
    }
    await sleep(500);
  }
  throw new Error("timed out waiting for page and extension popup");
}

async function pageState(pageClient) {
  return pageClient.evaluate(`(() => {
    const v = document.querySelector("video");
    return {
      title: document.title,
      url: location.href,
      paused: v ? v.paused : null,
      readyState: v ? v.readyState : null,
      currentTime: v ? v.currentTime : null,
      duration: v ? v.duration : null,
      src: v ? (v.currentSrc || v.src || "").slice(0, 120) : null,
      body: document.body.innerText.slice(0, 200)
    };
  })()`);
}

async function main() {
  const { page, popup } = await waitForTargets();
  const popupClient = await connect(popup.webSocketDebuggerUrl);
  const pageClient = await connect(page.webSocketDebuggerUrl);
  await popupClient.send("Runtime.enable");
  await pageClient.send("Runtime.enable");
  await pageClient.send("Page.enable");

  const tabPattern = new URL(page.url).origin + "/*";
  const activated = await popupClient.evaluate(`(async () => {
    const tabs = await chrome.tabs.query({ url: ${JSON.stringify(tabPattern)} });
    if (!tabs.length) return { ok: false, error: "no tab" };
    await chrome.tabs.update(tabs[0].id, { active: true });
    await new Promise((resolve) => setTimeout(resolve, 300));
    return { ok: true, tab: tabs[0].id, url: tabs[0].url };
  })()`);
  console.log("activate:", JSON.stringify(activated));
  if (!activated.ok) throw new Error("cannot activate page tab");

  const started = await popupClient.evaluate(
    `(async () => await chrome.runtime.sendMessage({ type: "mseStart" }))()`
  );
  console.log("start:", JSON.stringify(started));
  if (!started.ok) throw new Error("mseStart failed");

  try {
    await pageClient.send("Page.bringToFront");
  } catch (error) {
    /* minimized window may still play */
  }

  const loaded = await pageClient.evaluate(`(async () => {
    const load = document.getElementById("btn-load");
    if (load) load.click();
    await new Promise((resolve) => setTimeout(resolve, 5000));
    const v = document.querySelector("video");
    if (v) {
      v.muted = true;
      try { await v.play(); } catch (error) { /* reported by state */ }
    }
    return {
      clickedLoad: Boolean(load),
      paused: v ? v.paused : null,
      readyState: v ? v.readyState : null,
      currentTime: v ? v.currentTime : null,
      duration: v ? v.duration : null,
      src: v ? (v.currentSrc || v.src || "").slice(0, 120) : null
    };
  })()`);
  console.log("loaded:", JSON.stringify(loaded));

  const samples = [];
  const startedAt = Date.now();
  const end = startedAt + seconds * 1000;
  let lastTime = -1;
  let resetResult = null;
  while (Date.now() < end) {
    const state = await pageState(pageClient);
    samples.push({
      at: new Date().toISOString(),
      paused: state.paused,
      readyState: state.readyState,
      currentTime: state.currentTime,
      duration: state.duration,
    });
    console.log(
      `progress: t=${state.currentTime} paused=${state.paused} ready=${state.readyState}`
    );
    if (typeof state.currentTime === "number") lastTime = state.currentTime;
    if (resetAfter > 0 && Date.now() - startedAt >= resetAfter * 1000) {
      resetResult = await pageClient.evaluate(`(() => {
        const v = document.querySelector("video");
        if (!v) return { ok: false, error: "no video" };
        try {
          v.pause();
          v.removeAttribute("src");
          v.load();
        } catch (error) {
          return { ok: false, error: String(error) };
        }
        return { ok: true };
      })()`);
      console.log("reset:", JSON.stringify(resetResult));
      await sleep(1500);
      break;
    }
    await sleep(2000);
  }

  if (lastTime <= 0.5) {
    const play = await pageClient.evaluate(`(async () => {
      const v = document.querySelector("video");
      try { await v.play(); } catch (error) { return String(error); }
      return "play() called";
    })()`);
    console.log("late play:", play);
    await sleep(3000);
  }

  const stopStarted = Date.now();
  const stopPromise = popupClient.evaluate(
    `(async () => await chrome.runtime.sendMessage({ type: "mseStop" }))()`
  );
  const stopped = await Promise.race([
    stopPromise,
    sleep(30000).then(() => ({ ok: false, error: "CDP mseStop timeout" })),
  ]);
  const stopSeconds = (Date.now() - stopStarted) / 1000;
  console.log("stop:", JSON.stringify(stopped, null, 2));
  console.log("stop seconds:", stopSeconds.toFixed(2));

  const finalState = await pageState(pageClient);
  const payload = {
    timestamp: new Date().toISOString(),
    page: page.url,
    loaded,
    finalState,
    samples,
    resetAfter,
    resetResult,
    stopSeconds,
    reply: stopped,
  };
  if (outJson) {
    fs.writeFileSync(outJson, JSON.stringify(payload, null, 2) + "\n", "utf-8");
    console.log("saved:", outJson);
  }
  await sleep(500);
  popupClient.close();
  pageClient.close();
  return stopped;
}

main()
  .then((reply) => {
    if (!reply || !reply.ok) process.exit(1);
  })
  .catch((error) => {
    console.error("ERROR:", error.stack || error);
    process.exit(2);
  });
