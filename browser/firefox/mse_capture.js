// Experimental MSE capture hook (MAIN world).
//
// Multi-track by design:
//   * every SourceBuffer gets its own track id and byte counter;
//   * appendBuffer bytes are copied (no MediaRecorder / no re-encoding);
//   * MIME is used when available, otherwise the init bytes are sniffed for
//     fMP4 `hdlr` (vide/soun) or WebM `TrackType` (1/2);
//   * EME/DRM signals abort immediately;
//   * the host enforces configurable per-track/session limits and a free-disk
//     guard; the hook itself does not hard-cap chunks.
//
// The hook is injected when the user starts capture from the popup, and it
// forwards chunks to mse_bridge.js (isolated world) via window.postMessage.

(() => {
  if (window.__pdmMseHooked) return;
  window.__pdmMseHooked = true;

  const DRM_RE = /enc[va]|widevine|playready|fairplay|clearkey|drm/i;
  // Firefox content scripts cannot read page-principal ArrayBuffers that
  // arrive through window.postMessage (the cross-compartment wrapper throws
  // "Permission denied to access property constructor"). Gecko therefore gets
  // base64 chunks encoded here in the MAIN world; Chromium keeps the zero-copy
  // transfer path.
  const GECKO = /firefox/i.test(navigator.userAgent || "");

  let active = true;
  let trackCounter = 0;
  let globalTotal = 0;
  let mimeByBuffer = new WeakMap();
  let trackByBuffer = new WeakMap();

  const post = (event, payload = {}, transfer = []) => {
    try {
      window.postMessage({ __pdmMse: true, event, ...payload }, "*", transfer);
    } catch (error) {
      /* page may have been torn down */
    }
  };

  const abortForDrm = () => {
    if (!active) return;
    active = false;
    post("drm");
  };

  const kindFromMime = (mime) => {
    const text = String(mime || "").toLowerCase();
    if (text.startsWith("audio/")) return "audio";
    if (text.startsWith("video/")) return "video";
    return "";
  };

  const toBase64 = (bytes) => {
    let binary = "";
    const step = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += step) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + step));
    }
    return btoa(binary);
  };

  // Best-effort init-segment sniffing for buffers created before the hook was
  // injected (their MIME is not observable through the MSE API).
  const sniffKind = (bytes) => {
    try {
      const data = bytes;
      const limit = Math.min(data.length, 64 * 1024);
      for (let i = 0; i + 12 < limit; i += 1) {
        if (
          data[i] === 0x68 &&
          data[i + 1] === 0x64 &&
          data[i + 2] === 0x6c &&
          data[i + 3] === 0x72 // "hdlr"
        ) {
          const handler = String.fromCharCode(
            data[i + 8],
            data[i + 9],
            data[i + 10],
            data[i + 11]
          );
          if (handler === "vide") return "video";
          if (handler === "soun") return "audio";
        }
      }
      for (let i = 0; i + 1 < limit; i += 1) {
        if (data[i] === 0x83) {
          // WebM TrackType: 1 = video, 2 = audio.
          if (data[i + 1] === 0x01) return "video";
          if (data[i + 1] === 0x02) return "audio";
        }
      }
    } catch (error) {
      /* ignore malformed data */
    }
    return "";
  };

  const fallbackMime = (kind) =>
    kind === "audio" ? "audio/mp4" : "video/mp4";

  const trackFor = (buffer, bytes) => {
    let info = null;
    try {
      info = trackByBuffer.get(buffer) || null;
    } catch (error) {
      info = null;
    }
    if (!info) {
      let mime = "";
      try {
        mime = mimeByBuffer.get(buffer) || "";
      } catch (error) {
        mime = "";
      }
      const kind = kindFromMime(mime) || sniffKind(bytes) || "";
      info = { id: `track-${++trackCounter}`, mime, kind, total: 0 };
      try {
        trackByBuffer.set(buffer, info);
      } catch (error) {
        /* WeakMap should always accept an object */
      }
      post("track", { track: info.id, mime: info.mime, kind: info.kind });
      return info;
    }
    if (!info.kind) {
      const kind = kindFromMime(info.mime) || sniffKind(bytes);
      if (kind) {
        info.kind = kind;
        post("track", { track: info.id, mime: info.mime, kind: info.kind });
      }
    }
    return info;
  };

  const originalRequest = navigator.requestMediaKeySystemAccess;
  if (typeof originalRequest === "function") {
    navigator.requestMediaKeySystemAccess = function (...args) {
      abortForDrm();
      return originalRequest.apply(this, args);
    };
  }

  if (typeof MediaSource !== "undefined" && MediaSource.prototype) {
    const originalAdd = MediaSource.prototype.addSourceBuffer;
    MediaSource.prototype.addSourceBuffer = function (mime, ...rest) {
      const text = String(mime || "");
      if (DRM_RE.test(text)) abortForDrm();
      const buffer = originalAdd.call(this, mime, ...rest);
      try {
        mimeByBuffer.set(buffer, text || "video/mp4");
      } catch (error) {
        /* WeakMap should always accept an object */
      }
      return buffer;
    };
  }

  if (typeof SourceBuffer !== "undefined" && SourceBuffer.prototype) {
    const originalChangeType = SourceBuffer.prototype.changeType;
    if (typeof originalChangeType === "function") {
      SourceBuffer.prototype.changeType = function (mime, ...rest) {
        const text = String(mime || "");
        if (DRM_RE.test(text)) abortForDrm();
        try {
          mimeByBuffer.set(this, text);
        } catch (error) {
          /* ignore */
        }
        try {
          const info = trackByBuffer.get(this);
          if (info) {
            info.mime = text;
            const kind = kindFromMime(text);
            if (kind) info.kind = kind;
            post("track", { track: info.id, mime: info.mime, kind: info.kind });
          }
        } catch (error) {
          /* ignore */
        }
        return originalChangeType.call(this, mime, ...rest);
      };
    }

    const originalAppend = SourceBuffer.prototype.appendBuffer;
    SourceBuffer.prototype.appendBuffer = function (data, ...rest) {
      if (active) {
        let bytes = null;
        try {
          if (data instanceof ArrayBuffer) {
            bytes = new Uint8Array(data.slice(0));
          } else if (ArrayBuffer.isView(data)) {
            bytes = new Uint8Array(
              data.buffer.slice(
                data.byteOffset,
                data.byteOffset + data.byteLength
              )
            );
          }
        } catch (error) {
          bytes = null;
        }
        if (bytes) {
          const info = trackFor(this, bytes);
          const mime = info.mime || fallbackMime(info.kind);
          if (DRM_RE.test(mime)) {
            abortForDrm();
          } else {
            info.total += bytes.length;
            globalTotal += bytes.length;
            if (GECKO) {
              post("chunk", {
                track: info.id,
                mime,
                kind: info.kind,
                data: toBase64(bytes),
                encoding: "base64",
              });
            } else {
              post(
                "chunk",
                {
                  track: info.id,
                  mime,
                  kind: info.kind,
                  data: bytes.buffer,
                },
                [bytes.buffer]
              );
            }
          }
        }
      }
      return originalAppend.call(this, data, ...rest);
    };
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data) return;
    if (event.data.__pdmMseStop) active = false;
    if (event.data.__pdmMseStart) {
      active = true;
      trackCounter = 0;
      globalTotal = 0;
      mimeByBuffer = new WeakMap();
      trackByBuffer = new WeakMap();
      post("started");
    }
  });
})();
