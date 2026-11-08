// Isolated-world bridge for the experimental MSE capture hook.
//
// The MAIN-world hook posts chunks (ArrayBuffer) on the page. This bridge
// base64-encodes them and forwards to the service worker. Each message keeps
// its track id so the service worker can maintain independent host sessions
// (video + audio, or several SourceBuffers). Native messages stay below the
// host's 4 MiB chunk limit.

(() => {
  if (window.__pdmMseBridge) return;
  window.__pdmMseBridge = true;

  const runtime =
    (typeof chrome !== "undefined" && chrome.runtime) ||
    (typeof browser !== "undefined" && browser.runtime);

  if (!runtime) return;

  const encode = (buffer) => {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const step = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += step) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + step));
    }
    return btoa(binary);
  };

  const forward = (message) => {
    try {
      runtime.sendMessage(message);
    } catch (error) {
      /* extension reloaded or service worker gone */
    }
  };

  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data || !event.data.__pdmMse) return;
    const kind = event.data.event;
    const track = event.data.track || "";
    const mime = event.data.mime || "";
    if (kind === "chunk" && event.data.data) {
      let data;
      if (typeof event.data.data === "string") {
        // Gecko: the MAIN world already encoded the bytes; content scripts
        // cannot touch page-principal ArrayBuffers.
        data = event.data.data;
      } else {
        try {
          data = encode(event.data.data);
        } catch (error) {
          return;
        }
      }
      forward({
        type: "mseEvent",
        event: "chunk",
        track,
        mime,
        kind: event.data.kind || "",
        data,
      });
      return;
    }
    if (
      kind === "drm" ||
      kind === "limit" ||
      kind === "skip" ||
      kind === "track" ||
      kind === "started"
    ) {
      forward({
        type: "mseEvent",
        event: kind,
        track,
        mime,
        kind: event.data.kind || "",
        limit: event.data.limit || "",
        total: event.data.total || 0,
      });
    }
  });

  runtime.onMessage.addListener((message) => {
    if (message && message.type === "mseStopHook") {
      window.postMessage({ __pdmMseStop: true }, "*");
    }
  });
})();
