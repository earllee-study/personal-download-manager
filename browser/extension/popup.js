const status = document.getElementById("status");
const takeover = document.getElementById("takeover");
const mediaList = document.getElementById("mediaList");
const mediaEmpty = document.getElementById("mediaEmpty");
const mediaCount = document.getElementById("mediaCount");
const mseHint = document.getElementById("mseHint");
const mseStatus = document.getElementById("mseStatus");

let activeTabId = null;

function setStatus(text, color = "#475569") {
  status.textContent = text;
  status.style.color = color;
}

function ping() {
  setStatus("正在连接本地宿主…");
  chrome.runtime.sendMessage({ type: "ping" }, (reply) => {
    if (chrome.runtime.lastError) {
      setStatus("无法连接：扩展后台未响应", "#b91c1c");
      return;
    }
    if (reply?.ok) setStatus(`已连接（宿主 ${reply.version}）`, "#047857");
    else setStatus(`未连接：${reply?.error || "请安装原生宿主"}`, "#b91c1c");
  });
}

function kindLabel(kind) {
  if (kind === "hls") return "HLS 播放列表";
  if (kind === "dash") return "DASH 清单";
  return "视频文件";
}

function shortName(url) {
  try {
    const parsed = new URL(url);
    const parts = parsed.pathname.split("/").filter(Boolean);
    const name = parts.length ? parts[parts.length - 1] : parsed.host;
    return `${parsed.host} / ${name.slice(0, 48)}`;
  } catch (error) {
    return url.slice(0, 60);
  }
}

function renderMedia(media) {
  mediaList.textContent = "";
  const items = Array.isArray(media) ? media : [];
  mediaCount.textContent = items.length ? `（${items.length}）` : "";
  mediaEmpty.style.display = items.length ? "none" : "block";

  for (const item of items) {
    const row = document.createElement("li");
    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = item.label || shortName(item.url);
    name.title = item.url;
    const kind = document.createElement("span");
    kind.className = "kind";
    const sources = Array.isArray(item.sources) ? item.sources.join(" / ") : "";
    kind.textContent = sources
      ? `${kindLabel(item.kind)} · ${sources}`
      : kindLabel(item.kind);
    meta.appendChild(name);
    meta.appendChild(kind);

    const button = document.createElement("button");
    button.className = "small";
    button.textContent = "下载";
    button.addEventListener("click", () => {
      button.disabled = true;
      chrome.runtime.sendMessage(
        { type: "downloadMedia", url: item.url, kind: item.kind },
        (reply) => {
          if (chrome.runtime.lastError || !reply?.ok) {
            setStatus(`失败：${reply?.error || chrome.runtime.lastError?.message || "未知错误"}`, "#b91c1c");
            button.disabled = false;
          } else {
            setStatus(`已发送：${shortName(item.url)}`, "#047857");
          }
        }
      );
    });

    row.appendChild(meta);
    row.appendChild(button);
    mediaList.appendChild(row);
  }
}

function refreshMedia() {
  if (activeTabId === null) return;
  chrome.runtime.sendMessage({ type: "getMedia", tabId: activeTabId }, (reply) => {
    if (chrome.runtime.lastError) return;
    if (reply?.ok) renderMedia(reply.media);
  });
}

function scanPage(announce = true) {
  if (activeTabId === null) return;
  if (announce) setStatus("正在扫描页面中的媒体链接…");
  chrome.runtime.sendMessage(
    { type: "scanPage", tabId: activeTabId },
    (reply) => {
      if (chrome.runtime.lastError) {
        if (announce) {
          setStatus(
            `扫描失败：${chrome.runtime.lastError.message}`,
            "#b91c1c"
          );
        }
        return;
      }
      if (!reply?.ok) {
        if (announce) {
          setStatus(`扫描失败：${reply?.error || "未知错误"}`, "#b91c1c");
        }
        return;
      }
      renderMedia(reply.media || []);
      if (mseHint) mseHint.hidden = !reply.mse;
      if (announce) {
        setStatus(
          reply.mse
            ? "扫描完成；检测到 MSE/blob 流（暂不支持直接下载）"
            : `扫描完成：新增/已有 ${reply.media?.length || 0} 个媒体`,
          reply.mse ? "#b45309" : "#047857"
        );
      }
    }
  );
}

chrome.storage.local.get({ takeover: false }).then((value) => {
  takeover.checked = Boolean(value.takeover);
});
takeover.addEventListener("change", () => {
  chrome.storage.local.set({ takeover: takeover.checked });
});

document.getElementById("ping").addEventListener("click", ping);
document.getElementById("send").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return setStatus("当前标签页没有 URL", "#b91c1c");
  chrome.runtime.sendMessage(
    { type: "download", url: tab.url, referer: undefined },
    (reply) => {
      if (reply?.ok) setStatus("已发送到 PDM", "#047857");
      else setStatus(`失败：${reply?.error || "未知错误"}`, "#b91c1c");
    }
  );
});
document.getElementById("scan").addEventListener("click", () => scanPage(true));
document.getElementById("clear").addEventListener("click", () => {
  if (activeTabId === null) return;
  chrome.runtime.sendMessage({ type: "clearMedia", tabId: activeTabId }, () => {
    renderMedia([]);
  });
});

function setMseStatus(text, color = "#475569") {
  if (!mseStatus) return;
  mseStatus.textContent = text;
  mseStatus.style.color = color;
}

function sendRuntimeMessage(message, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      resolve({ ok: false, error: "宿主响应超时" });
    }, timeoutMs);
    try {
      chrome.runtime.sendMessage(message, (reply) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        const lastError = chrome.runtime.lastError;
        if (lastError) resolve({ ok: false, error: lastError.message });
        else resolve(reply || { ok: false, error: "宿主没有回复" });
      });
    } catch (error) {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve({ ok: false, error: String(error) });
    }
  });
}

document.getElementById("mseStart").addEventListener("click", () => {
  setMseStatus("正在开始 MSE 捕获…");
  sendRuntimeMessage({ type: "mseStart" }, 30000).then((reply) => {
    if (!reply?.ok) {
      setMseStatus(`失败：${reply?.error || "未知错误"}`, "#b91c1c");
      return;
    }
    setMseStatus("多轨捕获中；请播放视频，然后点“停止并保存”", "#047857");
  });
});

document.getElementById("mseStop").addEventListener("click", () => {
  setMseStatus("正在停止并保存…");
  sendRuntimeMessage({ type: "mseStop" }, 170000).then((reply) => {
    if (!reply?.ok) {
      setMseStatus(
        `失败：${reply?.error || "未捕获到数据"}`,
        "#b91c1c"
      );
      return;
    }
    const files = (reply.files || []).map((item) => item.path);
    const merged = reply.merged && reply.merged.path;
    if (merged) {
      setMseStatus(`已合并：${merged}`, "#047857");
    } else if (files.length) {
      const suffix = reply.merge_error ? `（未合并：${reply.merge_error}）` : "";
      setMseStatus(`已保存 ${files.length} 个轨道：${files.join("，")}${suffix}`, "#047857");
    } else {
      setMseStatus(`已保存：${reply.path || ""}`, "#047857");
    }
  });
});

sendRuntimeMessage({ type: "mseStatus" }, 5000).then((reply) => {
  if (!reply?.ok) return;
  if (reply.active) {
    setMseStatus(
      `捕获中（${reply.tracks || 0} 个轨道，${reply.bytes || 0} 字节）`,
      "#047857"
    );
  }
});

(async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  activeTabId = tab?.id ?? null;
  ping();
  refreshMedia();
  // DOM/script scan complements webRequest sniffing; quiet on restricted pages.
  scanPage(false);
  // The user may start playback while the popup is open.
  setInterval(refreshMedia, 1000);
})();
