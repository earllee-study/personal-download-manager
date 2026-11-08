// PDM native-messaging bridge + media sniffer.
//
// The extension forwards URLs the user asked for (context menu, popup, media
// list, optional takeover). It also observes requests to detect playable
// media (HLS playlists, plain video files) so the popup can offer them.
// Cookies are read for that exact URL only and go to the local native host.

const HOST_NAME = "com.pdm.downloader.host";
const MEDIA_KEY = "mediaByTab";
const MAX_PER_TAB = 40;
// Firefox has storage.session from 115; older builds (and any browser without
// it) fall back to local storage so the media list keeps working.
const sessionArea = chrome.storage.session || chrome.storage.local;

let takeover = false;
chrome.storage.local.get({ takeover: false }).then((value) => {
  takeover = Boolean(value.takeover);
});
chrome.storage.onChanged.addListener((changes) => {
  if (changes.takeover) takeover = Boolean(changes.takeover.newValue);
});

// ---------------------------------------------------------------------
// native messaging
// ---------------------------------------------------------------------
function sendToHost(message) {
  return new Promise((resolve) => {
    let port;
    let settled = false;
    const finish = (reply) => {
      if (settled) return;
      settled = true;
      resolve(reply);
      try {
        port?.disconnect();
      } catch (error) {
        /* already gone */
      }
    };
    try {
      port = chrome.runtime.connectNative(HOST_NAME);
    } catch (error) {
      resolve({ ok: false, error: String(error) });
      return;
    }
    port.onMessage.addListener(finish);
    port.onDisconnect.addListener(() => {
      const error = chrome.runtime.lastError;
      finish({ ok: false, error: error ? error.message : "native host closed" });
    });
    try {
      port.postMessage(message);
    } catch (error) {
      finish({ ok: false, error: String(error) });
    }
  });
}

async function cookieHeader(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    return cookies.map((cookie) => `${cookie.name}=${cookie.value}`).join("; ");
  } catch (error) {
    return "";
  }
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: "basic",
      iconUrl: "icon128.png",
      title,
      message: String(message || "").slice(0, 180),
    });
  } catch (error) {
    /* notifications are best-effort */
  }
}

async function sendDownload(url, filename, referer, kind) {
  if (!/^https?:/i.test(url || "")) {
    notify("PDM", "只接受 http/https 链接");
    return { ok: false, error: "unsupported scheme" };
  }
  const reply = await sendToHost({
    action: "download",
    url,
    filename: filename || undefined,
    referer: referer || undefined,
    cookie: (await cookieHeader(url)) || undefined,
    user_agent: navigator.userAgent,
    kind: kind || undefined,
  });
  if (reply.ok) {
    const label = kind === "hls" ? "已发送（HLS）" : "已发送";
    notify("PDM", `${label}：${url.slice(0, 120)}`);
  } else {
    notify("PDM", `失败：${reply.error || "未知错误"}`);
  }
  return reply;
}

// ---------------------------------------------------------------------
// experimental MSE/blob capture (multi-track, unencrypted fMP4/WebM)
// ---------------------------------------------------------------------
let msePort = null;
const msePending = [];
let mseState = null;

function isFirefox() {
  return /firefox/i.test(navigator.userAgent || "");
}

function firefoxMajorVersion() {
  const match = /Firefox\/(\d+)/.exec(navigator.userAgent || "");
  return match ? Number(match[1]) : 0;
}

function closeMsePort(error) {
  const port = msePort;
  msePort = null;
  while (msePending.length) {
    const resolve = msePending.shift();
    resolve({ ok: false, error: error || "native host closed" });
  }
  if (port) {
    try {
      port.disconnect();
    } catch (ignored) {
      /* already disconnected */
    }
  }
}

function ensureMsePort() {
  if (msePort) return msePort;
  msePort = chrome.runtime.connectNative(HOST_NAME);
  msePort.onMessage.addListener((message) => {
    const resolve = msePending.shift();
    if (resolve) resolve(message);
  });
  msePort.onDisconnect.addListener(() => {
    const lastError = chrome.runtime.lastError;
    closeMsePort(lastError ? lastError.message : "native host closed");
  });
  return msePort;
}

function mseRequest(message, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve(value);
    };
    timer = setTimeout(() => {
      if (settled) return;
      // Closing the port rejects every queued request together; reusing a
      // late reply for the next message would be worse than losing the port.
      closeMsePort("native host timed out");
      finish({ ok: false, error: "native host timed out" });
    }, timeoutMs);
    let port;
    try {
      port = ensureMsePort();
    } catch (error) {
      finish({ ok: false, error: String(error) });
      return;
    }
    msePending.push(finish);
    try {
      port.postMessage(message);
    } catch (error) {
      const index = msePending.indexOf(finish);
      if (index >= 0) msePending.splice(index, 1);
      finish({ ok: false, error: String(error) });
    }
  });
}

function resetMseState() {
  mseState = null;
}

async function mseStartCapture(message) {
  if (mseState && mseState.active) {
    return { ok: false, error: "MSE capture is already running" };
  }
  // MAIN-world injection needs Firefox 128+. Older Gecko builds would either
  // reject `world: "MAIN"` or silently inject into the isolated world, which
  // cannot patch the page's MediaSource prototypes.
  if (isFirefox() && firefoxMajorVersion() > 0 && firefoxMajorVersion() < 128) {
    return { ok: false, error: "MSE 捕获需要 Firefox 128 或更高版本" };
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabId = tab && tab.id;
  if (!Number.isFinite(tabId) || tabId < 0) {
    return { ok: false, error: "no active tab" };
  }
  const scripting =
    chrome.scripting || (typeof browser !== "undefined" ? browser.scripting : null);
  if (!scripting) return { ok: false, error: "page scripting is unavailable" };

  mseState = {
    active: true,
    tabId,
    filename: String(message?.filename || "").slice(0, 200),
    // Page metadata becomes the GUI queue entry's URL/title.
    sourceUrl: String(tab.url || "").slice(0, 8192),
    title: String(tab.title || "").slice(0, 512),
    tracks: new Map(),
    bytes: 0,
    chain: Promise.resolve(),
  };
  try {
    await scripting.executeScript({
      target: { tabId },
      files: ["mse_bridge.js"],
    });
    await scripting.executeScript({
      target: { tabId },
      files: ["mse_capture.js"],
      world: "MAIN",
    });
    await scripting.executeScript({
      target: { tabId },
      func: () => window.postMessage({ __pdmMseStart: true }, "*"),
    });
  } catch (error) {
    resetMseState();
    return { ok: false, error: `cannot inject MSE hook: ${error}` };
  }
  notify("PDM", "MSE 捕获已开始；请在页面播放视频，然后点“停止并保存”");
  return { ok: true, hint: "start capture before playback" };
}

function mseAccountBytes() {
  if (!mseState) return 0;
  let total = 0;
  for (const track of mseState.tracks.values()) total += track.bytes || 0;
  return total;
}

function timeoutAfter(ms, value) {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

function handleMseEvent(message, sender) {
  const tabId = sender && sender.tab && sender.tab.id;
  if (!mseState || !mseState.active || tabId !== mseState.tabId) return;
  const event = message.event;
  if (event === "started" || event === "track") return;
  if (event === "drm") {
    notify("PDM", "检测到 EME/DRM；MSE 捕获已停止（不尝试解密）");
    mseAbort("DRM/EME");
    return;
  }
  if (event === "limit") {
    const which = message.limit === "track" ? "单轨" : "总捕获";
    notify("PDM", `MSE 捕获达到${which}上限，已停止并保存当前数据`);
    mseStopCapture();
    return;
  }
  if (event === "skip") {
    notify("PDM", "已跳过一个无法识别的 MSE 轨道");
    return;
  }
  if (event !== "chunk") return;

  const trackId = String(message.track || "track-1");
  mseState.chain = mseState.chain.then(async () => {
    if (!mseState || !mseState.active) return;
    let track = mseState.tracks.get(trackId);
    if (!track) {
      track = { hostSession: null, seq: 0, bytes: 0, mime: "", kind: "" };
      mseState.tracks.set(trackId, track);
    }
    try {
      if (!track.hostSession) {
        const mime = message.mime || track.mime || "video/mp4";
        const kind =
          message.kind ||
          track.kind ||
          (/^audio\//i.test(mime)
            ? "audio"
            : /^video\//i.test(mime)
              ? "video"
              : "unknown");
        if (/enc[va]|widevine|playready|fairplay|clearkey|drm/i.test(mime)) {
          throw new Error("DRM/EME MSE capture is not supported");
        }
        const started = await mseRequest({
          action: "mse_start",
          filename: mseState.filename,
          mime,
          kind,
          track: trackId,
          source_url: mseState.sourceUrl,
          title: mseState.title,
        });
        if (!started.ok) throw new Error(started.error || "mse_start failed");
        track.hostSession = started.session;
        track.mime = mime;
        track.kind = kind;
      }
      const reply = await mseRequest({
        action: "mse_chunk",
        session: track.hostSession,
        seq: track.seq,
        data: message.data,
      });
      if (!reply.ok) {
        if (/exceeds|low disk space/i.test(String(reply.error || ""))) {
          notify("PDM", `MSE 捕获停止：${reply.error}`);
          setTimeout(() => {
            mseStopCapture();
          }, 0);
          return;
        }
        throw new Error(reply.error || "mse_chunk failed");
      }
      track.seq += 1;
      track.bytes = Number(reply.bytes || track.bytes);
      mseState.bytes = mseAccountBytes();
    } catch (error) {
      notify("PDM", `MSE 捕获失败：${error}`);
      setTimeout(() => {
        mseAbort(String(error));
      }, 0);
    }
  });
}

async function mseAbort(reason) {
  const state = mseState;
  if (!state) return;
  state.active = false;
  const chainDone = await Promise.race([
    state.chain.then(() => true, () => true),
    timeoutAfter(5000, false),
  ]);
  if (!chainDone) {
    closeMsePort("capture chain timeout");
    resetMseState();
    return;
  }
  await mseRequest({ action: "mse_abort_all" }, 10000);
  closeMsePort(reason);
  resetMseState();
}

async function mseStopCapture() {
  const state = mseState;
  if (!state || !state.active) {
    return { ok: false, error: "MSE capture is not running" };
  }
  state.active = false;
  const stopHook = (async () => {
    try {
      const scripting =
        chrome.scripting ||
        (typeof browser !== "undefined" ? browser.scripting : null);
      await scripting.executeScript({
        target: { tabId: state.tabId },
        func: () => window.postMessage({ __pdmMseStop: true }, "*"),
      });
    } catch (ignored) {
      /* tab may have navigated away or been reloaded */
    }
  })();
  await Promise.race([stopHook, timeoutAfter(5000, "stop-hook-timeout")]);
  const chainDone = await Promise.race([
    state.chain.then(() => true, () => true),
    timeoutAfter(10000, false),
  ]);
  if (!chainDone) {
    closeMsePort("capture chain timeout");
    resetMseState();
    return {
      ok: false,
      error: "MSE 停止超时（捕获链未完成）；请检查 capture/mse/*.part",
    };
  }
  if (!state.tracks || state.tracks.size === 0) {
    closeMsePort("no MSE data");
    resetMseState();
    return { ok: false, error: "没有捕获到 MSE 数据（请先开始捕获再播放）" };
  }
  // 150 s covers the host's bounded ffmpeg merge; if the host or page is
  // gone, the request resolves with an error instead of hanging the popup.
  const reply = await mseRequest({ action: "mse_end_all" }, 150000);
  closeMsePort("MSE capture finished");
  resetMseState();
  if (reply.ok) {
    const files = (reply.files || []).map((item) => item.path);
    const summary = reply.merged?.path
      ? `已合并：${reply.merged.path}`
      : `已保存 ${files.length} 个轨道：${files.join(", ")}`;
    notify("PDM", `MSE ${summary}`);
  } else {
    notify("PDM", `MSE 保存失败：${reply.error || "未知错误"}`);
  }
  return reply;
}

// ---------------------------------------------------------------------
// media detection
// ---------------------------------------------------------------------
let mediaState = null; // { "tabId": [{url, kind, mime, time}] }
let writeChain = Promise.resolve();

async function loadMedia() {
  if (mediaState !== null) return;
  const data = await sessionArea.get({ [MEDIA_KEY]: {} });
  mediaState = data[MEDIA_KEY] || {};
}

function persistMedia() {
  const snapshot = mediaState;
  const write = () => sessionArea.set({ [MEDIA_KEY]: snapshot });
  writeChain = writeChain.then(write, write);
  return writeChain;
}

function kindFromUrl(url) {
  const text = String(url || "");
  // Playlist URLs are sometimes wrapped in query parameters or lack a suffix.
  if (/\.m3u8(?:[?#]|$)/i.test(text)) return "hls";
  if (/\.mpd(?:[?#]|$)/i.test(text)) return "dash";
  const path = text.split(/[?#]/)[0].toLowerCase();
  if (/\.(mp4|webm|mkv|mov|flv)$/.test(path)) return "file";
  return null; // .m4s/.ts segments would flood the list; playlists cover them
}

// Injected into the active page (and its frames); must be self-contained.
function pageMediaScan() {
  const found = new Map(); // url -> Set(source)
  let mse = false;
  let framePrefix = "";
  try {
    if (window.top !== window) framePrefix = "iframe/";
  } catch (error) {
    framePrefix = "iframe/";
  }
  const add = (value, source) => {
    if (!value) return;
    const text = String(value);
    if (/^blob:/i.test(text)) {
      mse = true;
      return;
    }
    try {
      const url = new URL(text, document.baseURI);
      if (url.protocol === "http:" || url.protocol === "https:") {
        if (!found.has(url.href)) found.set(url.href, new Set());
        found.get(url.href).add(source);
      }
    } catch (error) {
      /* ignore malformed URLs */
    }
  };
  const patterns = [
    /(?:https?:)?\/\/[^\s"'<>]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>]*)?/gi,
    /["']([^"']+\.(?:m3u8|mpd)(?:\?[^"']*)?)["']/gi,
  ];
  const visit = (root, prefix) => {
    for (const element of root.querySelectorAll(
      "video, audio, source, a[href], link[href]"
    )) {
      add(element.currentSrc, prefix + "dom");
      add(element.src, prefix + "dom");
      add(element.href, prefix + "dom");
    }
    // Playlist URLs often live in inline scripts or JSON blobs, not DOM links.
    for (const script of root.querySelectorAll("script")) {
      const text = script.textContent || "";
      for (const pattern of patterns) {
        pattern.lastIndex = 0;
        let match;
        while ((match = pattern.exec(text))) {
          add(match[1] || match[0], prefix + "script");
        }
      }
    }
    // Players increasingly render inside open shadow roots.
    for (const element of root.querySelectorAll("*")) {
      if (element.shadowRoot) visit(element.shadowRoot, prefix + "shadow/");
    }
  };
  visit(document, framePrefix);
  return {
    urls: Array.from(found.entries())
      .slice(0, 80)
      .map(([url, sources]) => ({ url, sources: Array.from(sources) })),
    mse,
  };
}

function kindFromMime(mime) {
  const type = String(mime || "").split(";")[0].trim().toLowerCase();
  if (!type) return null;
  if (
    type.includes("mpegurl") ||
    type === "application/x-mpegurl" ||
    type === "audio/mpegurl" ||
    type === "application/vnd.apple.mpegurl"
  ) {
    return "hls";
  }
  if (type.includes("dash+xml")) return "dash";
  if (type.startsWith("video/") && type !== "video/mp2t") return "file";
  if (type === "application/mp4") return "file";
  return null;
}

function updateBadge(tabId, count) {
  try {
    chrome.action.setBadgeText({ tabId, text: count > 0 ? String(count) : "" });
    if (count > 0) {
      chrome.action.setBadgeBackgroundColor({ tabId, color: "#2563eb" });
    }
  } catch (error) {
    /* tab already gone */
  }
}

async function addMedia(tabId, url, kind, mime, options = {}) {
  if (tabId < 0 || !kind) return;
  await loadMedia();
  const key = String(tabId);
  const list = mediaState[key] || [];
  const source = String(options.source || "network");
  const label = String(options.label || "");
  const existing = list.find((item) => item.url === url);
  if (existing) {
    if (mime && !existing.mime) existing.mime = mime;
    if (label && !existing.label) existing.label = label;
    if (!Array.isArray(existing.sources)) existing.sources = [];
    if (!existing.sources.includes(source)) existing.sources.push(source);
    await persistMedia();
    return;
  }
  list.unshift({
    url,
    kind,
    mime: mime || "",
    time: Date.now(),
    sources: [source],
    label,
  });
  mediaState[key] = list.slice(0, MAX_PER_TAB);
  updateBadge(tabId, mediaState[key].length);
  await persistMedia();
}

async function clearTab(tabId) {
  await loadMedia();
  const key = String(tabId);
  for (const expanded of expandedMasters) {
    if (expanded.startsWith(`${tabId}|`)) expandedMasters.delete(expanded);
  }
  if (!mediaState[key]) return;
  delete mediaState[key];
  updateBadge(tabId, 0);
  await persistMedia();
}

// HLS master playlists list variants; expose them as selectable candidates.
const expandedMasters = new Set();

function attributeValue(line, name) {
  const marker = `${name}=`;
  const index = line.indexOf(marker);
  if (index < 0) return "";
  const rest = line.slice(index + marker.length);
  if (rest.startsWith('"')) {
    const end = rest.indexOf('"', 1);
    return end > 0 ? rest.slice(1, end) : "";
  }
  const end = rest.indexOf(",");
  return end < 0 ? rest : rest.slice(0, end);
}

function parseMasterPlaylist(text, baseUrl) {
  const lines = String(text || "")
    .split(/\r?\n/)
    .map((line) => line.trim());
  if (!lines.length || !lines[0].startsWith("#EXTM3U")) return [];
  const variants = [];
  let pending = null;
  for (const line of lines) {
    if (line.startsWith("#EXT-X-STREAM-INF:")) {
      pending = line;
      continue;
    }
    if (!line || line.startsWith("#") || !pending) continue;
    let uri;
    try {
      uri = new URL(line, baseUrl).href;
    } catch (error) {
      pending = null;
      continue;
    }
    const bandwidth = attributeValue(pending, "BANDWIDTH");
    const resolution = attributeValue(pending, "RESOLUTION");
    const parts = [];
    if (resolution) parts.push(resolution);
    if (bandwidth && Number(bandwidth) > 0) {
      parts.push(`${Math.round(Number(bandwidth) / 1000)} kbps`);
    }
    variants.push({ url: uri, label: parts.join(" · ") || "变体" });
    pending = null;
  }
  return variants;
}

async function expandHlsMaster(tabId, url, source) {
  const key = `${tabId}|${url}`;
  if (expandedMasters.has(key)) return;
  expandedMasters.add(key);
  try {
    const response = await fetch(url, {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) return;
    const length = Number(response.headers.get("content-length") || 0);
    if (length > 1024 * 1024) return;
    const text = await response.text();
    if (text.length > 1024 * 1024) return;
    const variants = parseMasterPlaylist(text, url);
    if (!variants.length) return;
    await addMedia(tabId, url, "hls", null, {
      source,
      label: "自动（最佳码率）",
    });
    for (const variant of variants) {
      await addMedia(tabId, variant.url, "hls", null, {
        source,
        label: variant.label,
      });
    }
  } catch (error) {
    /* master fetch is best-effort (CORS/offline/expired URL) */
  }
}

const REQUEST_FILTER = {
  urls: ["http://*/*", "https://*/*"],
  types: ["media", "xmlhttprequest", "other", "object", "main_frame"],
};

chrome.webRequest.onBeforeRequest.addListener((details) => {
  const kind = kindFromUrl(details.url);
  if (!kind) return;
  addMedia(details.tabId, details.url, kind, null, { source: "network" });
  if (kind === "hls") expandHlsMaster(details.tabId, details.url, "network");
}, REQUEST_FILTER);

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    const header = (details.responseHeaders || []).find(
      (item) => item.name.toLowerCase() === "content-type"
    );
    const kind = kindFromMime(header && header.value);
    if (!kind) return;
    addMedia(details.tabId, details.url, kind, header.value, {
      source: "network",
    });
    if (kind === "hls") expandHlsMaster(details.tabId, details.url, "network");
  },
  REQUEST_FILTER,
  ["responseHeaders"]
);

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  if (changeInfo.status === "loading" && changeInfo.url) clearTab(tabId);
});
chrome.tabs.onRemoved.addListener((tabId) => clearTab(tabId));

// ---------------------------------------------------------------------
// menus, takeover, messages
// ---------------------------------------------------------------------
chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "pdm-link",
    title: "用 PDM 下载此链接",
    contexts: ["link"],
  });
  chrome.contextMenus.create({
    id: "pdm-page",
    title: "用 PDM 下载当前页面",
    contexts: ["page", "video", "audio"],
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === "pdm-link" && info.linkUrl) {
    sendDownload(info.linkUrl, undefined, tab?.url);
  } else if (info.menuItemId === "pdm-page" && tab?.url) {
    sendDownload(tab.url, undefined, undefined);
  }
});

function takeOver(item, filename) {
  if (!takeover) return;
  const url = item.url || item.finalUrl || "";
  if (!/^https?:/i.test(url)) return;
  chrome.downloads.cancel(item.id, () => {
    sendDownload(url, filename || item.filename, undefined, kindFromUrl(url));
  });
}

if (chrome.downloads.onDeterminingFilename) {
  // Chromium: deciding the filename lets us cancel before any bytes land.
  chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
    takeOver(item, null);
    suggest();
  });
} else if (chrome.downloads.onCreated) {
  // Firefox has no onDeterminingFilename; cancel as soon as it appears.
  chrome.downloads.onCreated.addListener((item) => takeOver(item, null));
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "mseStart") {
    mseStartCapture(message).then(sendResponse);
    return true;
  }
  if (message?.type === "mseStop") {
    mseStopCapture().then(sendResponse);
    return true;
  }
  if (message?.type === "mseEvent") {
    handleMseEvent(message, _sender);
    sendResponse({ ok: true });
    return true;
  }
  if (message?.type === "mseStatus") {
    sendResponse({
      ok: true,
      active: Boolean(mseState && mseState.active),
      bytes: mseState ? mseState.bytes : 0,
      tracks: mseState && mseState.tracks ? mseState.tracks.size : 0,
    });
    return true;
  }
  if (message?.type === "ping") {
    sendToHost({ action: "ping" }).then(sendResponse);
    return true;
  }
  if (message?.type === "download") {
    sendDownload(message.url, message.filename, message.referer).then(sendResponse);
    return true;
  }
  if (message?.type === "getMedia") {
    loadMedia().then(() =>
      sendResponse({
        ok: true,
        media: (mediaState && mediaState[String(message.tabId)]) || [],
      })
    );
    return true;
  }
  if (message?.type === "downloadMedia") {
    sendDownload(message.url, message.filename, message.referer, message.kind)
      .then(sendResponse);
    return true;
  }
  if (message?.type === "clearMedia") {
    clearTab(message.tabId).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (message?.type === "scanPage") {
    const tabId = Number(message.tabId);
    const scripting =
      chrome.scripting ||
      (typeof browser !== "undefined" ? browser.scripting : null);
    if (!Number.isFinite(tabId) || tabId < 0 || !scripting) {
      sendResponse({ ok: false, error: "page scripting is unavailable" });
      return true;
    }
    scripting
      .executeScript({
        target: { tabId, allFrames: true },
        func: pageMediaScan,
      })
      .then(async (results) => {
        let found = 0;
        let mse = false;
        for (const item of results || []) {
          const result = item && item.result;
          const urls = Array.isArray(result)
            ? result
            : (result && Array.isArray(result.urls) && result.urls) || [];
          if (result && result.mse) mse = true;
          for (const entry of urls) {
            const url =
              typeof entry === "string" ? entry : entry && entry.url;
            if (!url) continue;
            const sources =
              entry && Array.isArray(entry.sources) && entry.sources.length
                ? entry.sources
                : ["dom"];
            const kind = kindFromUrl(url);
            if (!kind) continue;
            for (const source of sources) {
              await addMedia(tabId, url, kind, null, { source });
            }
            if (kind === "hls") {
              await expandHlsMaster(tabId, url, sources[0]);
            }
            found += 1;
          }
        }
        await loadMedia();
        sendResponse({
          ok: true,
          found,
          mse,
          media: (mediaState && mediaState[String(tabId)]) || [],
        });
      })
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
});
