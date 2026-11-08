// End-to-end MSE multi-track verification through the Chrome DevTools Protocol.
//
// Prerequisites:
//   * Edge/Chrome running with a remote debugging port and the PDM extension
//     loaded (Edge 156 accepts --load-extension; Chrome 156 may need the
//     extension loaded manually in the profile).
//   * `python browser/serve_demo.py --port 8000`
//   * Native host registered; for multi-track use a host built from current
//     source (the released dist host may be older).
//
// Usage:
//   node bench/mse_browser_verify.mjs http://127.0.0.1:9224

const cdp = process.argv[2] || "http://127.0.0.1:9222";
const pageUrl = process.argv[3] || "http://127.0.0.1:8000/browser/mse-test.html";
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
  let targets = [];
  while (Date.now() < deadline) {
    try {
      targets = await listTargets();
    } catch (error) {
      targets = [];
    }
    const page = targets.find(
      (item) => item.type === "page" && item.url.includes("mse-test.html")
    );
    const popup = targets.find(
      (item) => item.type === "page" && item.url.includes("chrome-extension://")
    );
    if (page && popup) return { page, popup };
    if (page && !popup) {
      // Open the popup page to start the extension service worker.
      try {
        await fetch(`${cdp}/json/new?${encodeURIComponent(popupUrl)}`, {
          method: "PUT",
        });
      } catch (error) {
        /* retried below */
      }
      await sleep(500);
      targets = await listTargets();
      const candidate = targets.find(
        (item) => item.type === "page" && item.url.includes("chrome-extension://")
      );
      if (candidate) return { page, popup: candidate };
    }
    await sleep(500);
  }
  throw new Error(
    "timed out waiting for the test page and extension popup; is the extension loaded?"
  );
}

async function main() {
  const { page, popup } = await waitForTargets();
  const popupClient = await connect(popup.webSocketDebuggerUrl);
  const pageClient = await connect(page.webSocketDebuggerUrl);
  await popupClient.send("Runtime.enable");
  await pageClient.send("Runtime.enable");

  const context = await popupClient.evaluate(
    `({ runtime: typeof chrome?.runtime, tabs: typeof chrome?.tabs, href: location.href })`
  );
  console.log("popup context:", JSON.stringify(context));
  if (context.runtime !== "object" || context.tabs !== "object") {
    throw new Error(
      `extension popup is not running with extension APIs (${JSON.stringify(context)})`
    );
  }

  const activate = await popupClient.evaluate(`(async () => {
    const tabs = await chrome.tabs.query({ url: ${JSON.stringify(tabPattern)} });
    if (!tabs.length) return { ok: false, error: "no test tab" };
    await chrome.tabs.update(tabs[0].id, { active: true });
    await new Promise((resolve) => setTimeout(resolve, 400));
    const active = await chrome.tabs.query({ active: true, currentWindow: true });
    return { ok: true, tab: tabs[0].id, active: active[0]?.url, count: tabs.length };
  })()`);
  console.log("activate:", JSON.stringify(activate));
  if (!activate.ok) throw new Error("cannot activate test tab");

  const started = await popupClient.evaluate(
    `(async () => await chrome.runtime.sendMessage({ type: "mseStart" }))()`
  );
  console.log("start:", JSON.stringify(started));
  if (!started.ok) throw new Error("mseStart failed");

  const played = await pageClient.evaluate(`(async () => {
    const log = document.getElementById("log");
    await playMulti();
    await new Promise((resolve) => setTimeout(resolve, 2000));
    return {
      log: log ? log.textContent : "",
      readyState: typeof video !== "undefined" ? video.readyState : -1,
      duration: typeof video !== "undefined" ? video.duration : -1,
      buffers: typeof buffers !== "undefined" ? buffers.length : -1
    };
  })()`);
  console.log("play:", JSON.stringify(played));

  const stopped = await popupClient.evaluate(
    `(async () => await chrome.runtime.sendMessage({ type: "mseStop" }))()`
  );
  console.log("stop:", JSON.stringify(stopped, null, 2));

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
