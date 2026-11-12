// 取词台浏览器上下文上报（内容脚本）。
//
// 传输链路照 docs/research/v3-extension-transport.md（R0 spike）：
//   cs.js --chrome.runtime.sendMessage--> MV3 service worker --fetch--> 127.0.0.1 hub
// 内容脚本本身不 fetch（公网页面上会被 Local Network Access 永久 pending）。
// 上报体字段与 docs/specs/v3-context-and-ocr.md §2.2 一致：
//   href/title/hasVideo/currentTime/duration/paused/focused/at
// v36（#240）追加 extVersion（运行中扩展的 manifest 版本，版本握手用）。
// 2026-11-11（区域校正）追加 videoRect：视频**内容区**在屏幕上的物理像素矩形
//   `{x,y,w,h}`（已扣掉 letterbox 黑边、乘过 devicePixelRatio）。hub 用它把用户
//   拖过的字幕带存成相对比例，页面滚动/影院模式/挪窗后重新投影，绝对坐标不再失效。
// v36（票 #241）：发送判据是「**标签页可见**」，`focused` 只作为字段上报（归属决策
// 要用它，但它不再是发送开关）；隐藏标签页另由 service worker 的 chrome.alarms
// 兜底叫醒（见下面的 onMessage）。
(() => {
  "use strict";

  const HEARTBEAT_MS = 2000; // 心跳周期（R0 结论：2s 足够且能保活 SW）
  const RECONNECT_MS = 1000; // 长连接断开后的重连退避
  const RETRY_DELAYS_MS = [1000, 2000, 4000]; // 上报失败的有界退避（票 #71）：最多 3 次
  const HEARTBEAT_PING = "ctx-heartbeat"; // SW 的 alarms 兜底叫醒消息（票 #241）

  // 只有「扩展被重载/禁用」才彻底停摆：那时 chrome.runtime 已失效，重试多少遍都是白搭。
  // 其余 runtime 错误是瞬时故障（典型是 SW 冷启动/重启期间的 "Receiving end does not
  // exist"）——退避重试几次就恢复，绝不 stop()：stop() 之后 stopped 永不复位，那个标签页
  // 就永久静音了（票 #71 的原始事故）。
  let stopped = false;
  let heartbeatTimer = null;
  let portRetryTimer = null;
  let retryTimer = null; // 在途的一次退避重试
  let retryIndex = 0; // 退避表游标：0..RETRY_DELAYS_MS.length
  let port = null;

  // 选“最像在播的那个”video：优先未暂停且已就绪的，其次面积最大的。
  // （B站/小红书一个页面可能挂多路 <video>。）
  function pickVideo() {
    const all = document.querySelectorAll("video");
    if (!all.length) return null;
    let best = null;
    let bestScore = -1;
    for (const v of all) {
      const playing = !v.paused && !v.ended && v.readyState > 0;
      const r = v.getBoundingClientRect();
      const score = (playing ? 1e9 : 0) + r.width * r.height;
      if (score > bestScore) {
        bestScore = score;
        best = v;
      }
    }
    return best;
  }

  function finiteOrNull(value) {
    return Number.isFinite(value) ? value : null;
  }

  // 视频**内容区**的屏幕物理像素矩形（区域校正用）。
  // getBoundingClientRect 是视口 CSS 坐标；screenX/screenY 是窗口外框的屏幕 CSS
  // 坐标。边框/标题栏用 outer−inner 差近似：左右边框对称，竖直差值算在顶部
  // （Windows 标题栏在上）。最后乘 devicePixelRatio 换成 GDI 抓屏用的物理像素。
  function videoScreenRect(v) {
    if (!v) return null;
    const r = v.getBoundingClientRect();
    if (!(r.width > 0 && r.height > 0)) return null;
    let cx = r.left;
    let cy = r.top;
    let cw = r.width;
    let ch = r.height;
    const vw = v.videoWidth || 0;
    const vh = v.videoHeight || 0;
    if (vw > 0 && vh > 0) {
      // 扣掉 letterbox：只取真正有画面的那块。
      const scale = Math.min(r.width / vw, r.height / vh);
      cw = vw * scale;
      ch = vh * scale;
      cx = r.left + (r.width - cw) / 2;
      cy = r.top + (r.height - ch) / 2;
    }
    const dpr = window.devicePixelRatio || 1;
    const borderX = Math.max(0, (window.outerWidth - window.innerWidth) / 2);
    const borderY = Math.max(0, window.outerHeight - window.innerHeight - borderX);
    return {
      x: Math.round((window.screenX + borderX + cx) * dpr),
      y: Math.round((window.screenY + borderY + cy) * dpr),
      w: Math.max(1, Math.round(cw * dpr)),
      h: Math.max(1, Math.round(ch * dpr)),
    };
  }

  // v36（#240）版本握手：上报**运行中**扩展的 manifest 版本。hub 拿它与仓库里
  // extension/manifest.json 的版本比对 —— 改过扩展但没手动重载时两边不一致，
  // 这是「浏览器侧跑的是不是仓库里那版代码」唯一的自证手段。
  // 扩展上下文失效时读不到：回空串（hub 记「未上报」），绝不让心跳抛错。
  function extensionVersion() {
    try {
      return chrome.runtime.getManifest().version || "";
    } catch (e) {
      return "";
    }
  }

  // §2.2 上报体；title 原样返回（清洗归 hub 的 clean_title）。
  function snapshot(focused) {
    const v = pickVideo();
    return {
      href: location.href,
      title: document.title,
      hasVideo: !!v,
      currentTime: v ? finiteOrNull(v.currentTime) : null,
      duration: v ? finiteOrNull(v.duration) : null,
      paused: v ? v.paused : null,
      focused: focused,
      at: Date.now(),
      extVersion: extensionVersion(),
      videoRect: videoScreenRect(v),
    };
  }

  // 扩展上下文是否还活着（重载后 chrome.runtime.id 会变 undefined）。
  function runtimeAlive() {
    try {
      return !!(chrome.runtime && chrome.runtime.id);
    } catch (e) {
      return false;
    }
  }

  function stop() {
    stopped = true;
    if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
    if (portRetryTimer !== null) clearTimeout(portRetryTimer);
    if (retryTimer !== null) clearTimeout(retryTimer);
    heartbeatTimer = null;
    portRetryTimer = null;
    retryTimer = null;
    port = null;
  }

  // runtime 错误的分类（票 #71）：只认一种救不回来的情况。
  //   "fatal" —— 扩展被重载/卸载，chrome.runtime 整体失效 → 停摆（重试没有意义）。
  //   "retry" —— 其余一律当瞬时故障（MV3 的 SW 冷启动/重启期间会报
  //              "Receiving end does not exist"，那是正常现象）→ 退避重试，绝不 stop()。
  function classifyRuntimeError(message) {
    if (/context invalidated/i.test(message || "")) return "fatal";
    return "retry";
  }

  // 有界退避：1s / 2s / 4s 各一次（最多 3 次）；用尽即收手，回到只靠心跳的 2s 节奏
  // —— 这不是永久静音：发通一次就把游标归零，下次再坏又是完整的一条退避阶梯。
  function retryLater(payload) {
    if (retryTimer !== null) return; // 已有一条在途，不叠加
    if (retryIndex >= RETRY_DELAYS_MS.length) return; // 这一轮退避已用尽
    const delay = RETRY_DELAYS_MS[retryIndex];
    retryIndex += 1;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      send(payload);
    }, delay);
  }

  // 一处判定：fatal 才停摆；可恢复错误退避重试。
  function handleRuntimeError(message, payload) {
    if (classifyRuntimeError(message) === "fatal") {
      stop();
      return;
    }
    retryLater(payload);
  }

  function send(payload) {
    if (stopped) return;
    if (!runtimeAlive()) {
      stop();
      return;
    }
    try {
      chrome.runtime.sendMessage(payload, () => {
        // 读一下 lastError，避免控制台出现 “Unchecked runtime.lastError”。
        const err = chrome.runtime.lastError;
        if (!err) {
          retryIndex = 0; // 发通了：退避阶梯复位（失败连着失败时不许复位，否则退不起来）
          return;
        }
        handleRuntimeError(err.message, payload);
      });
    } catch (e) {
      // 扩展被重载后这里会同步抛 “Extension context invalidated.”。
      handleRuntimeError(e && e.message, payload);
    }
  }

  // 长连接只为保活 SW（R0：SW 被杀后 port 会自动重连，无需状态机）。
  function connectPort() {
    if (stopped) return;
    if (!runtimeAlive()) {
      stop();
      return;
    }
    try {
      port = chrome.runtime.connect({ name: "ctx-keepalive" });
      port.onDisconnect.addListener(() => {
        void chrome.runtime.lastError; // 读掉，避免控制台报错
        port = null;
        if (stopped) return;
        if (!runtimeAlive()) {
          stop();
          return;
        }
        portRetryTimer = setTimeout(connectPort, RECONNECT_MS);
      });
    } catch (e) {
      // 同步抛错同样先分类：扩展被重载（context invalidated）才停摆，
      // 其余按「连接没建上」处理 —— 稍后重连一次，不整页静音。
      if (classifyRuntimeError(e && e.message) === "fatal") {
        stop();
        return;
      }
      portRetryTimer = setTimeout(connectPort, RECONNECT_MS);
    }
  }

  connectPort();

  // 心跳（票 #241 / spec 决策 1）：判据是**标签页可见**，不是窗口聚焦。
  // 「这个页面还在、还在播什么」与「用户此刻正看着它」是两件事 —— 后者只作为
  // `focused` 字段上报（hub 的归属决策要用），不再是发送的开关：
  // 以前按 `document.hasFocus()` 判，焦点一离开浏览器就一条都不发，中枢于是
  // 完全看不见副屏/后台还在播的视频（P0 断流根因）。
  function heartbeat() {
    if (stopped) return;
    if (document.visibilityState !== "visible") return; // 隐藏：2s 定时器会被节流，交给 alarms 兜底
    send(snapshot(document.hasFocus()));
  }

  heartbeatTimer = setInterval(heartbeat, HEARTBEAT_MS);

  // 聚焦到来时立刻补一条，不等下一个 2s 周期。
  window.addEventListener("focus", () => {
    if (!stopped) send(snapshot(true));
  }, true);

  // 切后台 / 窗口失焦：补一条 focused:false，让 hub 及时知道“人不在这个页面了”。
  window.addEventListener("blur", () => {
    if (!stopped) send(snapshot(false));
  }, true);

  // 切回这个标签页：立刻补一条（隐藏期间 2s 定时器被节流，回来这一拍不该等）。
  document.addEventListener("visibilitychange", () => {
    if (stopped) return;
    if (document.visibilityState === "visible") send(snapshot(document.hasFocus()));
  });

  // 后台兜底（票 #241 / spec 决策 1）：没有焦点门之后，隐藏标签页的 `setInterval`
  // 仍会被浏览器节流到约 1 次/分钟（长于中枢的场景新鲜度窗口）。SW 的
  // `chrome.alarms`（周期 ≥30s）每拍叫醒一次，这里收到就**照报不误**——不看
  // visibilityState，否则「后台但仍在播」那种情形还是瞎的。
  chrome.runtime.onMessage.addListener((msg) => {
    if (stopped) return;
    if (!msg || msg.type !== HEARTBEAT_PING) return;
    send(snapshot(document.hasFocus()));
  });

  // 首帧先报一条（后台标签页打开时 focused=false，也照报一次）。
  send(snapshot(document.hasFocus()));
})();
