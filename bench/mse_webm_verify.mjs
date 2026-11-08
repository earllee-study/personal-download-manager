// End-to-end MSE WebM (VP9/Opus) capture verification through CDP.
//
// Prerequisites:
//   * Edge/Chrome with a remote debugging port and the PDM extension loaded.
//   * `python browser/serve_demo.py --port 8000`
//   * Native host registered (packaged or current source).
//
// Usage:
//   node bench/mse_webm_verify.mjs <cdp> [page-url] [out-json]

const cdp = process.argv[2] || "http://127.0.0.1:9224";
const pageUrl =
  process.argv[3] || "http://127.0.0.1:8000/browser/mse-webm-test.html";
const outJson = process.argv[4] || "";
const extensionId = "hfiihggbmdmoofcadgmkingenjgcaend";
const popupUrl = `chrome-extension://${extensionId}/popup.html`;
const tabPattern = new URL(pageUrl).origin + "/*";

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
      const detail =
        result.exceptionDetails.text +
        " " +
        JSON.stringify(result.exceptionDetails.exception?.description || "");
      throw new Error(detail);
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
    let targets = [];
    try {
      targets = await listTargets();
    } catch (error) {
      targets = [];
    }
    const page = targets.find(
      (item) => item.type === "page" && item.url.includes("mse-webm-test.html")
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
        /* retry below */
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
  throw new Error(
    "timed out waiting for mse-webm-test.html and the extension popup"
  );
}

async function pageState(client) {
  return client.evaluate(`(() => {
    const v = document.querySelector("video");
    const log = document.getElementById("log");
    return {
      title: document.title,
      url: location.href,
      paused: v ? v.paused : null,
      readyState: v ? v.readyState : null,
      currentTime: v ? v.currentTime : null,
      duration: v ? v.duration : null,
      log: log ? log.textContent.slice(-1000) : "",
    };
  })()`);
}

async function main() {
  const { page, popup } = await waitForTargets();
  const popupClient = await connect(popup.webSocketDebuggerUrl);
  const pageClient = await connect(page.webSocketDebuggerUrl);
  await popupClient.send("Runtime.enable");
  await pageClient.send("Runtime.enable");

  const activated = await popupClient.evaluate(`(async () => {
    const tabs = await chrome.tabs.query({ url: ${JSON.stringify(tabPattern)} });
    if (!tabs.length) return { ok: false, error: "no test tab" };
    await chrome.tabs.update(tabs[0].id, { active: true });
    await new Promise((resolve) => setTimeout(resolve, 400));
    return { ok: true, tab: tabs[0].id, url: tabs[0].url };
  })()`);
  console.log("activate:", JSON.stringify(activated));
  if (!activated.ok) throw new Error("cannot activate test tab");

  const started = await popupClient.evaluate(
    `(async () => await chrome.runtime.sendMessage({ type: "mseStart" }))()`
  );
  console.log("start:", JSON.stringify(started));
  if (!started.ok) throw new Error("mseStart failed");

  const played = await pageClient.evaluate(`(async () => {
    await playWebm();
    return true;
  })()`);
  console.log("play:", JSON.stringify(played));

  // Wait until the media clock advances (or the fixture ends).
  const deadline = Date.now() + 15000;
  let state = await pageState(pageClient);
  while (Date.now() < deadline) {
    state = await pageState(pageClient);
    if (
      typeof state.currentTime === "number" &&
      state.currentTime > 1.0
    ) {
      break;
    }
    await sleep(500);
  }
  console.log("state:", JSON.stringify(state));
  await sleep(500);

  const stopStarted = Date.now();
  const stopped = await popupClient.evaluate(
    `(async () => await chrome.runtime.sendMessage({ type: "mseStop" }))()`
  );
  const stopSeconds = (Date.now() - stopStarted) / 1000;
  console.log("stop:", JSON.stringify(stopped, null, 2));
  console.log("stop seconds:", stopSeconds.toFixed(2));

  const payload = {
    timestamp: new Date().toISOString(),
    browser: "Edge/Chromium (CDP)",
    page: page.url,
    played,
    state,
    stopSeconds,
    reply: stopped,
  };
  if (outJson) {
    const fs = await import("node:fs");
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
