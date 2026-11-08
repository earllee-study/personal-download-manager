"""Small runtime translation layer for the GUI (Simplified Chinese / English).

The UI source strings are Chinese.  ``tr()`` maps an exact source string to
English when the active language is English; unknown strings fall back to the
Chinese source, so adding a label without a translation never breaks the UI.

Static widget text (labels, buttons, menus, placeholders, headers, tooltips,
combo items) is translated once when a top-level widget is shown by
``install_translation_filter()``.  Text assembled at runtime must call
``tr()`` explicitly (or use a ``{}`` template in the catalog).

Language codes:
  * ``system`` -- Chinese if the OS locale starts with ``zh``, otherwise English
  * ``zh``     -- Simplified Chinese
  * ``en``     -- English
"""

from __future__ import annotations

import re

from PySide6.QtCore import QEvent, QLocale, QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialogButtonBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QTabWidget,
    QTableWidget,
    QTextEdit,
    QWidget,
)

LANGUAGES = ("system", "zh", "en")
_LANGUAGE_LABELS = {
    "system": "跟随系统",
    "zh": "简体中文",
    "en": "English",
}

_WEEKDAY_EN = {
    "周一": "Mon",
    "周二": "Tue",
    "周三": "Wed",
    "周四": "Thu",
    "周五": "Fri",
    "周六": "Sat",
    "周日": "Sun",
}

# Chinese source -> English.  Keep keys byte-for-byte identical to the UI
# strings (including spaces and newlines); ``tests/test_i18n.py`` checks that
# the common screens have no untranslated labels in English mode.
_EN: dict[str, str] = {
    # ---- main window -------------------------------------------------
    "Personal Download Manager — 下载器": "Personal Download Manager — Downloader",
    "显示": "View",
    "全部": "All",
    "进行中": "Active",
    "等待中": "Queued",
    "已完成": "Completed",
    "失败": "Failed",
    "队列": "Queue",
    "搜索文件名或 URL…": "Search filename or URL…",
    "共 0 个任务": "0 tasks",
    "添加下载": "Add download",
    "批量添加": "Batch add",
    "继续": "Resume",
    "暂停": "Pause",
    "删除": "Delete",
    "打开文件": "Open file",
    "打开目录": "Open folder",
    "定时开始…": "Schedule…",
    "设置": "Settings",
    "清除已完成": "Clear completed",
    "所有下载完成后关机": "Shut down when all downloads finish",
    "队列空闲且所有任务都已结束（至少一个完成）后，倒计时结束即关机；倒计时期间可取消。此开关只对本次运行有效。": (
        "Shut down when the queue is idle and every task has ended (at least one "
        "completed); you can cancel during the countdown. This switch applies to "
        "the current session only."
    ),
    "完成后关机：{} 秒": "Shutdown in {} s",
    "完成后关机：已启用": "Shutdown when done: armed",
    "下载完成后关机": "Shut down after downloads",
    "取消关机": "Cancel shutdown",
    "下载队列已结束，将在 {} 秒后关闭计算机。": (
        "The download queue has finished. The computer will shut down in {} seconds."
    ),
    "关机命令执行失败": "Shutdown failed",
    "无法关闭计算机：\n{}": "The computer could not be shut down:\n{}",
    # ---- MSE captures -------------------------------------------------
    "已捕获": "Captured",
    "MSE 捕获": "MSE capture",
    "已导入 {} 个捕获": "Imported {} captures",
    "MSE 捕获（{} 个轨道）": "MSE capture ({} tracks)",
    "已用 ffmpeg 合并视频与音频": "Video and audio merged with ffmpeg",
    "未合并：{}": "Not merged: {}",
    "来源：MSE 捕获": "Source: MSE capture",
    "页面标题：{}": "Page title: {}",
    "捕获轨道：{} 个": "Captured tracks: {}",
    "附加文件：{}": "Extra file: {}",
    "归档中": "Archiving",
    "正在归档到下载目录…": "Archiving into the download folder…",
    "归档失败：{}": "Archive failed: {}",
    "原始捕获：{}": "Original capture: {}",
    "MSE 归档失败": "MSE archive failed",
    "{}：{}": "{}: {}",
    "捕获文件不存在": "The capture file is missing",
    "清理捕获源轨…": "Clean up capture source tracks…",
    "清理捕获源轨": "Clean up capture source tracks",
    "清理 {} 个捕获的 {} 个源轨（约 {}）？归档/主文件不会被删除。": (
        "Delete {} source tracks from {} captures (about {})? Archived/primary "
        "files are kept."
    ),
    "已清理 {} 个文件，释放 {}。": "Removed {} files, freed {}.",
    "源轨已清理：{} 个（{}）": "Source tracks cleaned: {} ({})",
    "复制 URL": "Copy URL",
    "属性": "Properties",
    "退出": "Quit",
    "检查更新…": "Check for updates…",
    "关于": "About",
    "文件": "File",
    "下载": "Downloads",
    "全部暂停": "Pause all",
    "全部继续": "Resume all",
    "帮助": "Help",
    "主工具栏": "Main toolbar",
    "就绪": "Ready",
    "显示主窗口": "Show main window",
    "删除任务": "Delete task",
    "同时删除已下载的文件和分片": "Also delete the downloaded file and parts",
    "从列表中移除所有已完成任务？": "Remove all completed tasks from the list?",
    "同时删除文件": "Also delete files",
    "文件名：": "File name:",
    "保存到：": "Save to:",
    "状态：": "Status:",
    "进度：": "Progress:",
    "大小：": "Size:",
    "已下载：": "Downloaded:",
    "引擎重试：": "Engine retries: ",
    "自动重试：": "Auto retries: ",
    "添加时间：": "Added: ",
    "计划开始：": "Scheduled start: ",
    "重复：": "Repeat: ",
    "下次自动重试：": "Next auto retry: ",
    "错误：": "Error: ",
    "下载属性": "Download properties",
    "移动到队列": "Move to queue",
    "检查更新": "Check for updates",
    "尚未配置更新清单 URL。\n\n请在“设置 → 更新清单”中填写发布方提供的 HTTPS 地址。": (
        "No update manifest URL is configured.\n\n"
        "Set one in Settings → Update manifest to the HTTPS URL provided by the publisher."
    ),
    "关于 Personal Download Manager": "About Personal Download Manager",
    "\n\n多连接、动态分段、断点续传的 HTTP(S) 下载器。\n使用 deepseek-v4.1-flash-1017 开发的测试项目；下载引擎与测试均为本项目实现。\n\n请只下载你有权下载的内容。": (
        "\n\nA multi-connection, dynamically segmented, resumable HTTP(S) downloader.\n"
        "A test project developed with deepseek-v4.1-flash-1017; the download "
        "engine and tests are implemented in this project.\n\n"
        "Only download content you are allowed to download."
    ),
    "下载完成": "Download complete",
    "浏览器下载": "Browser download",
    "全部队列": "All queues",
    "剪贴板链接": "Clipboard link",
    "程序仍在后台运行，双击托盘图标可恢复。": (
        "Still running in the background; double-click the tray icon to restore."
    ),
    "：": ":",
    " 字节": " bytes",
    " · 暂停 ": " · paused ",
    " · 等待 ": " · queued ",
    "下载中 ": "downloading ",
    " · 失败 ": " · failed ",
    " · 完成 ": " · completed ",
    " · 计划 ": " · scheduled ",
    " · 重试 ": " · retrying ",
    " 个任务": " tasks",
    "共 ": "Total ",
    "已添加 ": "Added ",
    " 个任务？": " tasks?",
    "删除 ": "Delete ",
    "发现下载链接：\n": "Download link found:\n",
    "\n\n是否添加？": "\n\nAdd it?",
    "下载中 {} · 等待 {} · 暂停 {} · 完成 {} · 失败 {}": (
        "Downloading {} · queued {} · paused {} · completed {} · failed {}"
    ),
    " · 计划 {}": " · scheduled {}",
    " · 重试 {}": " · retrying {}",
    "共 {} 个任务": "{} tasks in total",
    "删除 {} 个任务？": "Delete {} task(s)?",
    "已添加 {} 个任务": "Added {} task(s)",
    "文件名：{}": "File name: {}",
    "URL：{}": "URL: {}",
    "保存到：{}": "Save to: {}",
    "状态：{}": "Status: {}",
    "进度：{}": "Progress: {}",
    "大小：{} 字节": "Size: {} bytes",
    "已下载：{} 字节": "Downloaded: {} bytes",
    "引擎重试：{}": "Engine retries: {}",
    "自动重试：{}": "Auto retries: {}",
    "添加时间：{}": "Added: {}",
    "计划开始：{}": "Scheduled start: {}",
    "重复：{}": "Repeat: {}",
    "下次自动重试：{}": "Next auto retry: {}",
    "错误：{}": "Error: {}",
    "Personal Download Manager {}\n\n多连接、动态分段、断点续传的 HTTP(S) 下载器。\n使用 deepseek-v4.1-flash-1017 开发的测试项目；下载引擎与测试均为本项目实现。\n\n请只下载你有权下载的内容。": (
        "Personal Download Manager {}\n\nA multi-connection, dynamically segmented, resumable "
        "HTTP(S) downloader.\nA test project developed with deepseek-v4.1-flash-1017; the "
        "download engine and tests are implemented in this project.\n\n"
        "Only download content you are allowed to download."
    ),
    "发现下载链接：\n{}\n\n是否添加？": "Download link found:\n{}\n\nAdd it?",

    # ---- table model -------------------------------------------------
    "等待中": "Queued",
    "下载中": "Downloading",
    "已暂停": "Paused",
    "已取消": "Cancelled",
    "已计划": "Scheduled",
    "等待重试": "Waiting to retry",
    "确定": "OK",
    "是": "Yes",
    "否": "No",
    "保存": "Save",
    "文件名": "File name",
    "状态": "Status",
    "进度": "Progress",
    "大小": "Size",
    "速度": "Speed",
    "剩余": "Remaining",
    "连接": "Connections",
    "保存位置": "Save location",
    "（已自动重试 {} 次）": " (auto-retried {} times)",

    # ---- recurrence editor / schedule --------------------------------
    "不重复": "Do not repeat",
    "每天": "Daily",
    "每周": "Weekly",
    "每隔 N 天": "Every N days",
    " 天": " days",
    "每隔 N 天在所选时间重复": "Repeat every N days at the chosen time",
    "重复": "Repeat",
    "每周重复至少选择一个星期。": "Select at least one weekday for a weekly repeat.",
    "定时开始": "Schedule start",
    "取消计划": "Cancel schedule",
    "选择重复后，任务每次完成（或重试耗尽）都会自动排到下一个匹配的时间；“取消计划”会同时关闭重复。": (
        "With a repeat rule, each completed task (or exhausted retry budget) is "
        "re-scheduled to the next matching time. “Cancel schedule” also removes "
        "the repeat rule."
    ),
    "计划开始时间必须晚于当前时间。": "The scheduled start must be later than now.",
    "输入有误": "Invalid input",
    "周一": "Monday",
    "周二": "Tuesday",
    "周三": "Wednesday",
    "周四": "Thursday",
    "周五": "Friday",
    "周六": "Saturday",
    "周日": "Sunday",
    "一": "M",
    "二": "T",
    "三": "W",
    "四": "T",
    "五": "F",
    "六": "S",
    "日": "S",

    # ---- add download ------------------------------------------------
    "浏览…": "Browse…",
    "留空则使用服务器/URL 文件名": "Leave empty to use the server/URL filename",
    "作为流媒体播放列表下载（HLS/DASH）": "Download as a streaming playlist (HLS/DASH)",
    "解析 m3u8 或 mpd，并发下载分片并按序拼接；支持未加密 VOD 与标准 AES-128（密钥可获取），SAMPLE-AES/DRM 拒绝": (
        "Parse m3u8 or mpd, download segments concurrently and concatenate them; "
        "plain VOD and standard AES-128 with a retrievable key; SAMPLE-AES/DRM "
        "is refused"
    ),
    "最佳码率": "Best bitrate",
    "最差码率": "Worst bitrate",
    "视频轨": "Video track",
    "音频轨": "Audio track",
    "视频+音频（合并）": "Video + audio (merge)",
    "合并需要设置 ffmpeg 路径（设置 → ffmpeg 路径）；没有 ffmpeg 时会保留两条轨道。": (
        "Merging needs an ffmpeg path (Settings → FFmpeg path); without "
        "ffmpeg the two tracks are kept separately."
    ),
    "ffmpeg 路径": "FFmpeg path",
    "ffmpeg.exe 或包含 ffmpeg 的目录（留空则从 PATH 查找）": (
        "ffmpeg.exe or a directory containing ffmpeg (empty = search PATH)"
    ),
    "用于把 DASH 视频轨和音频轨合并为可播放 MP4；留空则从 PATH 查找。": (
        "Used to merge DASH video and audio tracks into a playable MP4; "
        "empty means search PATH."
    ),
    " 连接": " connections",
    "不限，例如 5M": "Unlimited, e.g. 5M",
    "自动（按扩展名）": "Auto (by extension)",
    "不分类": "No category",
    "立即开始下载": "Start downloading now",
    "下载地址": "URL",
    "保存目录": "Save folder",
    "分类目录": "Category",
    "连接数": "Connections",
    "限速": "Speed limit",
    "显示高级选项": "Show advanced options",
    "高级选项": "Advanced options",
    "用户名:密码": "user:password",
    "http://127.0.0.1:8080 或 socks5://…": "http://127.0.0.1:8080 or socks5://…",
    "留空使用全局设置": "Leave empty to use the global setting",
    "可选，64 位十六进制": "Optional, 64 hex characters",
    "每行一个请求头，例如：\nReferer: https://example.com/": (
        "One request header per line, e.g.:\nReferer: https://example.com/"
    ),
    "可选，每行一个镜像 URL（内容/ETag 必须与主地址一致）": (
        "Optional, one mirror URL per line (content/ETag must match the primary)"
    ),
    "启用动态分段": "Enable dynamic segmentation",
    "自适应连接数（AIMD，实验性）": "Adaptive connections (AIMD, experimental)",
    "从少量连接开始，健康时逐步增加，遇到 429/503 时减半；上限为连接数": (
        "Start with a few connections, increase while healthy, halve on 429/503; "
        "capped by the connection count"
    ),
    "尝试 HTTP/2（实验性）": "Try HTTP/2 (experimental)",
    "尝试 HTTP/3（QUIC，实验性）": "Try HTTP/3 (QUIC, experimental)",
    "需要 aioquic；与 HTTP/2 互斥，且不支持代理": (
        "Needs aioquic; mutually exclusive with HTTP/2 and cannot use a proxy"
    ),
    "校验 TLS 证书": "Verify TLS certificate",
    "FTP 主动模式（PORT/EPRT）": "FTP active mode (PORT/EPRT)",
    "仅 ftp:// 与 ftps://：由服务器连接回本机的数据端口；默认是被动模式": (
        "ftp:// and ftps:// only: the server connects back to a local data port; "
        "passive mode is the default"
    ),
    "可选，SFTP 私钥文件（如 id_ed25519）": "Optional SFTP private key (e.g. id_ed25519)",
    "私钥口令（可选）": "Key passphrase (optional)",
    "首次连接时自动接受 SFTP 主机密钥": "Accept SFTP host keys on first connection",
    "不安全：会接受任意主机密钥。默认拒绝未知主机；已知主机请用 --sftp-known-hosts 或系统 known_hosts。": (
        "Insecure: accepts any host key. Unknown hosts are rejected by default; "
        "use --sftp-known-hosts or the system known_hosts for known hosts."
    ),
    "认证": "Authentication",
    "代理": "Proxy",
    "额外请求头": "Extra headers",
    "镜像 URL": "Mirror URLs",
    "SFTP 私钥": "SFTP private key",
    "私钥口令": "Key passphrase",
    "重试次数": "Retries",
    "选择下载目录": "Choose download folder",
    "分类目录已在设置中关闭": "Categories are disabled in Settings",
    "保存到下载目录根目录": "Saved to the download folder root",
    "下载地址格式不正确。": "The URL is invalid.",
    "只支持 http://、https://、ftp://、ftps:// 或 sftp:// 地址。": (
        "Only http://, https://, ftp://, ftps:// or sftp:// URLs are supported."
    ),
    "无法创建保存目录：": "Cannot create the save folder: ",
    "文件已存在": "File already exists",
    "限速格式不正确，例如 5M、512K。": "Invalid speed limit, e.g. 5M or 512K.",
    "请求头格式不正确：": "Invalid header: ",
    "认证格式应为 用户名:密码。": "Authentication must be user:password.",
    "SHA-256 应为 64 位十六进制字符串。": "SHA-256 must be 64 hex characters.",
    "子目录：": "Subfolder: ",

    # ---- batch add ---------------------------------------------------
    "批量添加（通配 URL）": "Batch add (wildcard URLs)",
    "每行一个地址或模式，例如：\nhttps://cdn.example.com/img[01-10].jpg\nhttps://cdn.example.com/{a,b}/file.bin\n支持 [1-10]、[001-010]、[1-10:2]、[a-z]、{a,b,c}；# 开头为注释": (
        "One URL or pattern per line, e.g.:\n"
        "https://cdn.example.com/img[01-10].jpg\n"
        "https://cdn.example.com/{a,b}/file.bin\n"
        "Supports [1-10], [001-010], [1-10:2], [a-z], {a,b,c}; lines starting with # are comments"
    ),
    "URL / 模式": "URL / pattern",
    "选择保存目录": "Choose save folder",
    "模式有误：": "Invalid pattern: ",
    "尚未输入任何 URL。": "No URL has been entered.",
    "将添加 ": "Will add ",
    " 个任务（上限 ": " tasks (limit ",
    "，例如：": ", e.g.: ",
    "；其中 ": "; ",
    " 个不是受支持的地址，将被忽略": " unsupported addresses will be ignored",
    "批量添加": "Batch add",
    "没有识别到有效的地址（http/https/ftp/ftps/sftp）。": (
        "No valid URL was found (http/https/ftp/ftps/sftp)."
    ),

    # ---- settings ----------------------------------------------------
    "管理队列…": "Manage queues…",
    "按分类保存到子目录": "Save into category subfolders",
    "分类目录…": "Categories…",
    "https://…/latest.json（留空则不检查更新）": "https://…/latest.json (empty = no update checks)",
    "仅在你点击“帮助 → 检查更新”时访问；安装包必须通过 SHA-256 校验，运行安装器前还会再次确认。": (
        "Only accessed when you click Help → Check for updates; the installer must "
        "pass SHA-256 verification and you are asked again before it runs."
    ),
    "不限，例如 10M（所有任务合计）": "Unlimited, e.g. 10M (all tasks combined)",
    "带宽时段…": "Bandwidth windows…",
    "不限": "Unlimited",
    "同一主机上所有任务合计的连接数上限": "Connection cap shared by all tasks on one host",
    "代理分组…": "Proxy groups…",
    "编辑主机规则…": "Edit host rules…",
    "失败任务自动重试（仅瞬时错误）": "Auto-retry failed tasks (transient errors only)",
    " 次": " times",
    " 秒": " seconds",
    "首次重试等待时间，之后每次翻倍（上限 1 小时）": (
        "Delay before the first retry, doubling afterwards (max 1 hour)"
    ),
    "监视剪贴板中的下载链接": "Watch the clipboard for download links",
    "最小化到系统托盘": "Minimize to the system tray",
    "关闭窗口时最小化到托盘": "Minimize to tray when the window is closed",
    "浅色": "Light",
    "深色": "Dark",
    "跟随系统": "Follow system",
    "默认下载目录": "Default download folder",
    "默认分类": "Default category",
    "默认连接数": "Default connections",
    "同时下载任务数": "Concurrent tasks",
    "默认限速": "Default speed limit",
    "默认代理": "Default proxy",
    "全局限速": "Global speed limit",
    "更新清单": "Update manifest",
    "带宽时段": "Bandwidth windows",
    "每主机连接上限": "Per-host connection cap",
    "代理分组": "Proxy groups",
    "主机规则": "Host rules",
    "最多自动重试": "Max auto retries",
    "首次重试间隔": "First retry delay",
    "关机倒计时": "Shutdown countdown",
    "MSE 捕获自动保存到下载目录": "Save MSE captures into the download folder",
    "归档成功后删除已合并的源轨": "Delete merged source tracks after archiving",
    "仅在合并文件成功归档后删除 capture/mse 中的原始视频/音频轨；未合并、归档失败或关闭归档时保留。": (
        "Only delete the original video/audio tracks in capture/mse after the "
        "merged file has been archived; keep them when there is no merge, "
        "archiving fails, or archiving is disabled."
    ),
    "捕获完成后在后台把合并文件（没有合并时为主轨）移动到下载目录/分类；源轨保留在捕获目录。": (
        "Move the merged file (or the primary track when there is no merge) into "
        "the download folder/category in the background; source tracks stay in "
        "the capture folder."
    ),
    "所有任务完成，且没有等待、暂停、计划或重试任务时，倒计时结束即关机。在“下载”菜单中开启或取消；该开关只对本次运行有效。": (
        "Once every task has finished and nothing is queued, paused, scheduled or "
        "waiting to retry, the computer shuts down at the end of the countdown. "
        "Enable or cancel it in the Download menu; the switch applies to the "
        "current session only."
    ),
    "主题": "Theme",
    "语言": "Language",
    "语言更改将在重启后生效。": "The language change takes effect after restart.",
    "选择默认下载目录": "Choose default download folder",
    "编辑主机规则…（": "Edit host rules… (",
    "代理分组…（": "Proxy groups… (",
    "分类目录…（": "Categories… (",
    "管理队列…（": "Manage queues… (",
    "带宽时段…（": "Bandwidth windows… (",
    " 条）": " rules)",
    " 个）": " )",
    "无法创建目录：": "Cannot create folder: ",
    "限速格式不正确，例如 5M。": "Invalid speed limit, e.g. 5M.",
    "主机规则引用了不存在的代理组：": "Host rules reference unknown proxy groups: ",
    "子目录：{}": "Subfolder: {}",
    "{} 已存在。\n是否覆盖？": "{} already exists.\nOverwrite?",
    "请求头格式不正确：{}": "Invalid header: {}",
    "无法创建保存目录：{}": "Cannot create the save folder: {}",
    "模式有误：{}": "Invalid pattern: {}",
    "将添加 {} 个任务（上限 {}）。": "Will add {} tasks (limit {}).",
    "，例如：{}": ", e.g.: {}",
    "；其中 {} 个不是受支持的地址，将被忽略": "; {} unsupported addresses will be ignored",
    "编辑主机规则…（{} 条）": "Edit host rules… ({} rules)",
    "代理分组…（{} 个）": "Proxy groups… ({})",
    "分类目录…（{} 个）": "Categories… ({})",
    "管理队列…（{} 个）": "Manage queues… ({})",
    "带宽时段…（{} 条）": "Bandwidth windows… ({})",
    "无法创建目录：{}": "Cannot create folder: {}",
    "第 {} 行缺少名称。": "Row {} has no name.",
    "第 {} 行缺少代理地址。": "Row {} has no proxy address.",
    "名称重复：{}": "Duplicate name: {}",
    "第 {} 行缺少主机模式。": "Row {} has no host pattern.",
    "第 {} 行的代理组“{}”不存在，请先在“代理分组…”里添加。": (
        "Row {} references unknown proxy group “{}”; add it in Proxy groups… first."
    ),
    "第 {} 行同时填写了代理和代理组，将使用直接填写的代理。": (
        "Row {} sets both a proxy and a proxy group; the explicit proxy will be used."
    ),
    "第 {} 行连接数必须是整数。": "Row {} connections must be an integer.",
    "第 {} 行认证格式应为“用户:密码”。": "Row {} authentication must be user:password.",
    "第 {} 行无效：{}": "Row {} is invalid: {}",
    "第 {} 行缺少队列名。": "Row {} has no queue name.",
    "队列名重复：{}": "Duplicate queue name: {}",
    "第 {} 行的并发数必须是整数。": "Row {} concurrency must be an integer.",
    "第 {} 行时间格式应为 HH:MM。": "Row {} time must be HH:MM.",
    "第 {} 行开始和结束不能相同。": "Row {} start and end must differ.",
    "第 {} 行限速格式不正确，例如 1M。": "Row {} has an invalid speed limit, e.g. 1M.",

    # ---- advanced editors (static labels) ----------------------------
    "名称": "Name",
    "子目录（相对下载目录）": "Subfolder (relative to the download folder)",
    "扩展名（逗号分隔）": "Extensions (comma separated)",
    "添加": "Add",
    "删除选中": "Delete selected",
    "扩展名匹配时保存到“下载目录/子目录”。同一扩展名只归入第一个匹配的分类；留空的行会被忽略。": (
        "Matching extensions are saved under download folder/subfolder. An extension "
        "belongs to the first matching category; empty rows are ignored."
    ),
    "代理地址": "Proxy address",
    "代理分组只保存名称和代理地址；主机规则里填写“代理组”即可复用。修改一个分组会同时影响引用它的所有主机规则。": (
        "Proxy groups store a name and a proxy URL; host rules reuse them via the "
        "proxy-group field. Editing a group affects every rule that references it."
    ),
    "主机模式": "Host pattern",
    "代理组": "Proxy group",
    "认证（用户:密码）": "Authentication (user:password)",
    "认证类型": "Auth type",
    "站点策略": "Site policies",
    "模式支持 * 通配（example.com、*.cdn.example.com），首个匹配生效。连接数/限速/代理留空表示沿用全局；UA/Cookie/认证留空表示沿用任务自身设置。认证格式为“用户:密码”。代理组填写“代理分组…”里的名称；同时填了代理和代理组时，直接代理优先。": (
        "Patterns support * wildcards (example.com, *.cdn.example.com); the first "
        "match wins. Empty connections/speed/proxy inherit the global settings; "
        "empty UA/Cookie/auth inherit the task values. Authentication is "
        "user:password. Proxy group names come from Proxy groups…; if both proxy "
        "and proxy group are set, the explicit proxy wins."
    ),
    "主机策略": "Host policies",
    "同时任务数": "Concurrent tasks",
    "队列名": "Queue name",
    "下载队列": "Download queues",
    "“主队列”的并发数在设置里用“同时下载任务数”调整；这里添加额外队列。任务可在右键菜单里移动到其他队列。": (
        "The main queue's concurrency is set by Concurrent tasks in Settings; add "
        "extra queues here. Tasks can be moved via the context menu."
    ),
    "“主队列”由设置里的并发数控制，不能在这里添加。": (
        "The main queue is controlled by the concurrency setting and cannot be added here."
    ),
    "开始 (HH:MM)": "Start (HH:MM)",
    "结束 (HH:MM)": "End (HH:MM)",
    "限速（留空=不限）": "Limit (empty = unlimited)",
    "时段每天生效；开始晚于结束表示跨午夜（例如 22:00–06:00）。重叠时取最小限速；时段外使用“全局限速”。修改在 30 秒内生效。": (
        "Windows apply every day; a start later than the end crosses midnight "
        "(e.g. 22:00–06:00). Overlaps use the smallest limit; outside all windows "
        "the global limit applies. Changes take effect within 30 seconds."
    ),
    "并发数必须在 1–10 之间。": "Concurrency must be between 1 and 10.",

    # ---- update dialog ----------------------------------------------
    "下载并安装": "Download and install",
    "关闭": "Close",
    "当前版本 ": "Current version ",
    "正在检查更新…（当前版本 ": "Checking for updates… (current version ",
    "当前已是最新版本（": "You are up to date (",
    "发现新版本 ": "New version ",
    "（当前 ": " (current ",
    "发布时间：": "Published: ",
    "安装包大小：": "Installer size: ",
    "检查更新失败：": "Update check failed: ",
    "取消": "Cancel",
    "正在下载更新…": "Downloading update…",
    "正在下载更新… ": "Downloading update… ",
    "更新包已下载并通过 SHA-256 校验。": "The update was downloaded and passed SHA-256 verification.",
    "安装更新": "Install update",
    "更新包已保存到：\n": "The update package was saved to:\n",
    "\n\n现在运行安装程序吗？": "\n\nRun the installer now?",
    "无法启动安装程序": "Cannot start the installer",
    "安装包已保存：": "Installer saved: ",
    "安装程序已启动。": "The installer has been started.",
    "下载已取消。": "Download cancelled.",
    "下载失败：": "Download failed: ",
    "正在取消下载…": "Cancelling download…",
    "当前版本 {}。": "Current version {}.",
    "正在检查更新…（当前版本 {}）": "Checking for updates… (current version {})",
    "当前已是最新版本（{}）。": "You are up to date ({}).",
    "发现新版本 {}（当前 {}）。": "New version {} (current {}).",
    "发布时间：{}": "Published: {}",
    "安装包大小：{}": "Installer size: {}",
    "检查更新失败：{}": "Update check failed: {}",
    "正在下载更新… {} / {}": "Downloading update… {} / {}",
    "正在下载更新… {}": "Downloading update… {}",
    "更新包已保存到：\n{}\n\n现在运行安装程序吗？": (
        "The update package was saved to:\n{}\n\nRun the installer now?"
    ),
    "安装包已保存：{}": "Installer saved: {}",
    "下载失败：{}": "Download failed: {}",
    "简体中文": "Chinese (Simplified)",
    "）：": "): ",
    "（": " (",
}

_current = "zh"
_filter: "_LanguageFilter | None" = None


def resolve_language(code: str | None) -> str:
    value = str(code or "system").strip().lower()
    if value in ("zh", "en"):
        return value
    return "zh" if QLocale.system().name().lower().startswith("zh") else "en"


def set_language(code: str | None) -> str:
    """Set the active language and return the resolved code."""
    global _current
    _current = resolve_language(code)
    return _current


def current_language() -> str:
    return _current


def language_label(code: str) -> str:
    return _LANGUAGE_LABELS.get(code, code)


def tr(text: str) -> str:
    """Translate one source string; unknown strings fall back to Chinese."""
    if _current == "en":
        return _EN.get(text, text)
    return text


def translate_repeat(text: str) -> str:
    """English form of ``schedule.RepeatRule.describe()`` output."""
    if _current != "en" or not text:
        return text
    match = re.fullmatch(r"每隔 (\d+) 天 (\d{2}:\d{2})", text)
    if match:
        days, moment = match.groups()
        return f"Every {days} days at {moment}"
    match = re.fullmatch(r"每天 (\d{2}:\d{2})", text)
    if match:
        return f"Daily at {match.group(1)}"
    match = re.fullmatch(r"每周 (.+) (\d{2}:\d{2})", text)
    if match:
        days, moment = match.groups()
        english = ", ".join(_WEEKDAY_EN.get(day, day) for day in days.split("、"))
        return f"Weekly {english} at {moment}"
    return text


def _localize_buttons(root: QWidget) -> None:
    for obj in [root, *root.findChildren(QObject)]:
        if isinstance(obj, QDialogButtonBox):
            _translate_button_box(obj)
        elif isinstance(obj, QMessageBox):
            _translate_message_box(obj)


def translate_widget_tree(root: QWidget) -> None:
    """Translate static text (English) and localise standard dialog buttons."""
    _localize_buttons(root)
    if _current != "en":
        return
    objects = [root]
    objects.extend(root.findChildren(QObject))
    for obj in objects:
        try:
            if isinstance(obj, QAction):
                obj.setText(tr(obj.text()))
                obj.setToolTip(tr(obj.toolTip()))
                obj.setStatusTip(tr(obj.statusTip()))
                obj.setIconText(tr(obj.iconText()))
            if isinstance(obj, QMenu):
                obj.setTitle(tr(obj.title()))
            if isinstance(obj, QWidget):
                obj.setWindowTitle(tr(obj.windowTitle()))
                obj.setToolTip(tr(obj.toolTip()))
            if isinstance(obj, QLabel):
                obj.setText(tr(obj.text()))
            if isinstance(obj, QGroupBox):
                obj.setTitle(tr(obj.title()))
            if isinstance(obj, QAbstractButton):
                obj.setText(tr(obj.text()))
            if isinstance(obj, QLineEdit):
                obj.setPlaceholderText(tr(obj.placeholderText()))
            if isinstance(obj, (QPlainTextEdit, QTextEdit)):
                obj.setPlaceholderText(tr(obj.placeholderText()))
            if isinstance(obj, QTabWidget):
                for index in range(obj.count()):
                    obj.setTabText(index, tr(obj.tabText(index)))
            if isinstance(obj, QTableWidget):
                for column in range(obj.columnCount()):
                    item = obj.horizontalHeaderItem(column)
                    if item is not None:
                        item.setText(tr(item.text()))
                for row in range(obj.rowCount()):
                    item = obj.verticalHeaderItem(row)
                    if item is not None:
                        item.setText(tr(item.text()))
            if isinstance(obj, QDialogButtonBox):
                _translate_button_box(obj)
            if isinstance(obj, QMessageBox):
                _translate_message_box(obj)
        except (RuntimeError, AttributeError):
            # A widget may be deleted while the tree is being walked.
            continue


_STANDARD_BUTTONS = {
    QDialogButtonBox.StandardButton.Ok: "确定",
    QDialogButtonBox.StandardButton.Cancel: "取消",
    QDialogButtonBox.StandardButton.Yes: "是",
    QDialogButtonBox.StandardButton.No: "否",
    QDialogButtonBox.StandardButton.Close: "关闭",
    QDialogButtonBox.StandardButton.Save: "保存",
}


def _translate_button_box(box: QDialogButtonBox) -> None:
    for standard, source in _STANDARD_BUTTONS.items():
        button = box.button(standard)
        if button is not None:
            button.setText(tr(source))


def _translate_message_box(box: QMessageBox) -> None:
    for button in box.buttons():
        standard = box.standardButton(button)
        source = _STANDARD_BUTTONS.get(standard)
        if source:
            button.setText(tr(source))


class _LanguageFilter(QObject):
    """Translate a top-level widget once it is shown.

    Dialogs are constructed on demand; translating at Show covers every screen
    without touching each constructor.  The call is deferred so late layout
    changes have already populated their text.
    """

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.Show
            and isinstance(obj, QWidget)
            and obj.isWindow()
        ):
            translate_widget_tree(obj)
        return False


def install_translation_filter(app) -> None:
    global _filter
    if _filter is None:
        _filter = _LanguageFilter(app)
        app.installEventFilter(_filter)


__all__ = [
    "LANGUAGES",
    "current_language",
    "install_translation_filter",
    "language_label",
    "resolve_language",
    "set_language",
    "tr",
    "translate_repeat",
    "translate_widget_tree",
]
