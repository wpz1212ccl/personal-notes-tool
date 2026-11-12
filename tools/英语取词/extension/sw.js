// MV3 service worker：把内容脚本的消息转发到本机 hub（规格 §2.2）。
//
// 链路与头部照 docs/research/v3-extension-transport.md（R0 spike）：
//   content script --sendMessage--> 本 SW --fetch--> http://127.0.0.1:47811
// 用 text/plain 简单请求（无 OPTIONS 预检）。内容脚本不直接 fetch——公网页面上
// 那样会被 Local Network Access 静默挂死。
//
// 只打 127.0.0.1，不联网。改 web_port 时同步改下面这一行（README 有说明）。
const HUB_ORIGIN = "http://127.0.0.1:47811";
// 票 #179：上报入口要验扩展共享令牌（hub 首启生成、存 config.ini 的 extension_token；
// 本 SW 在首次上报前 GET 回来。SW 进程被回收后缓存随它消失，下次消息重取即可）。
const CONTEXT_URL = HUB_ORIGIN + "/api/browser/context";
const TOKEN_URL = HUB_ORIGIN + "/api/extension/token";

// v36（票 #241 / spec 决策 1）：后台兜底心跳。
// 内容脚本的 2s 定时器在隐藏标签页里会被浏览器节流到约 1 次/分钟，而中枢的场景
// 新鲜度窗口是按「秒级心跳 + 低频兜底」设计的 —— 没有这条兜底，「后台但仍在播」
// 与「可见但被节流」两种情形都会瞎。alarms 的最小周期是 30s，正好是下限。
const HEARTBEAT_PING = "ctx-heartbeat"; // 与 cs.js 的同名常量对齐（两端认同一个消息名）
const ALARM_NAME = "ctx-heartbeat-tick";
const ALARM_MIN_PERIOD = 0.5; // 分钟 = 30s（chrome.alarms 的下限；够覆盖被节流的 setInterval）

let cachedToken = "";

function fetchToken() {
  return fetch(TOKEN_URL)
    .then((res) => {
      if (!res.ok) throw new Error("token http " + res.status);
      return res.json();
    })
    .then((data) => {
      cachedToken = typeof data.token === "string" ? data.token : "";
      return cachedToken;
    });
}

function getToken() {
  return cachedToken ? Promise.resolve(cachedToken) : fetchToken();
}

function report(payload) {
  getToken()
    .then((token) =>
      fetch(CONTEXT_URL, {
        method: "POST",
        headers: { "Content-Type": "text/plain", "X-Hub-Token": token },
        body: JSON.stringify(payload),
      }).then((res) => {
        // 401 = hub 换了令牌（config.ini 重新生成过）：丢缓存，下次心跳重取再发。
        if (res.status === 401) cachedToken = "";
        // 心跳回包可能带下行指令（如 player_fullscreen）；解析失败当没有指令。
        return res.json().catch(() => null);
      }).then((data) => applyCommand(data, payload.href))
    )
    .catch((err) => {
      // 失败只在 SW 控制台记一行；不许记 URL / 标题内容（§2.7 隐私纪律）。
      console.warn("[ctx] hub 未响应（已忽略，下次心跳再试）", err && err.name);
    });
}

function applyCommand(data, sentHref) {
  // 下行指令守卫：只认 player_fullscreen，且回包里的 href 必须与**这条心跳
  // 自己上报的 href** 逐字相等——hub 回给别的标签页的指令不能错套到这个窗口。
  if (!data || data.cmd !== "player_fullscreen") return;
  if (typeof data.href !== "string" || data.href !== sentHref) return;
  // 用 getLastFocused 而不是 tabs.query：SW 里没有「active 标签页」的判据时，
  // 它最接近「用户正在看的那个窗口」（cs.js 心跳刚到，说明那个窗口大概率在前台）。
  chrome.windows.getLastFocused((win) => {
    // 读 lastError 静默吞掉：窗口取不到时丢弃这条指令即可，不许 console。
    if (chrome.runtime.lastError) return;
    chrome.windows.update(win.id, { state: "fullscreen" });
  });
}

chrome.runtime.onMessage.addListener((msg) => {
  // 只认上下文上报（有非空 href 的对象）；其它消息忽略。
  if (!msg || typeof msg !== "object" || typeof msg.href !== "string" || !msg.href) {
    return;
  }
  report(msg);
});

// 保活端口的接收端（票 #71）：内容脚本会 connect 一条 name="ctx-keepalive" 的长连接
// 来拖住这个 service worker，可这边原来只有 onMessage —— 没有 onConnect 时连接随即以
// "Receiving end does not exist" 断开，保活是空转。这里接住并**保持**它：
// 端口上不走消息，所以什么都不做，也绝不主动挂断（SW 被回收时浏览器自己会断）。
const keepalivePorts = new Set();

function keepAlivePort(port) {
  if (port.name !== "ctx-keepalive") return;
  keepalivePorts.add(port);
  port.onDisconnect.addListener(() => {
    keepalivePorts.delete(port);
  });
}

chrome.runtime.onConnect.addListener(keepAlivePort);

// --- 后台兜底心跳（v36 / 票 #241）-------------------------------------------------
// 每个 alarms 周期把所有标签页的内容脚本叫一遍，收到消息的那一页立刻上报一条。
// 内容脚本那边**不看 visibilityState** —— 「后台但仍在播」正是要覆盖的情形。
function onAlarm(alarm) {
  if (!alarm || alarm.name !== ALARM_NAME) return;
  chrome.tabs.query({}, (tabs) => {
    if (chrome.runtime.lastError) return;
    for (const tab of Array.isArray(tabs) ? tabs : []) {
      if (typeof tab.id !== "number") continue;
      // 2026-11-10：**只叫「用户真在看 / 真有声」的标签**。
      // 原来叫所有标签（每个内容脚本回一条），开几十个标签时 hub 会在几十毫秒内
      // 收到几十条互相矛盾的 `video=no` 心跳（实测 40ms / 30 条）：场景判定被冲掉、
      // 日志刷到无法排障、调度器每拍白跑。兜底的目的只是「后台但仍在播的那一页」，
      // audible 正好覆盖它；active 覆盖正在看的那个窗口。
      if (!tab.active && !tab.audible) continue;
      // 回调只为读掉 lastError：没有内容脚本的页面（chrome://、扩展页）会报
      // "Could not establish connection"，静默吞掉即可（§2.7：不记页面内容）。
      chrome.tabs.sendMessage(tab.id, { type: HEARTBEAT_PING }, () => {
        void chrome.runtime.lastError;
      });
    }
  });
}

chrome.alarms.create(ALARM_NAME, { periodInMinutes: ALARM_MIN_PERIOD });
chrome.alarms.onAlarm.addListener(onAlarm);
