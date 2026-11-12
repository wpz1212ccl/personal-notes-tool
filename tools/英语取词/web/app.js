/* 取词台 v2 前端（票 #30 / W1 §8.1；票 #31 / W2 §8.2 复习模式）。
 *
 * 纪律：
 * - 零外部资源、零构建：只用系统字体与本文件；
 * - **反模式 XSS**：一切用户内容（目标词/释义/原句/标题/标签）只经
 *   `textContent` 或 `document.createElement` 写入，本文件没有一处 innerHTML；
 * - 键盘全流程：/ j k Enter r t Esc ?；复习中 Space 揭示、1–4 打分
 *   （见 index.html 的帮助浮层）。
 */
"use strict";

const KINDS = [
  ["word", "词"],
  ["phrase", "短语"],
  ["sentence", "句"],
];
const KIND_LABEL = { word: "词", phrase: "短语", sentence: "句" };
// v3 §4：会话 7 类（抽屉下拉的中文名；key 与后端 sessions.CATEGORIES 一致）
const CATEGORIES = [
  ["video", "视频"],
  ["article", "文章"],
  ["note", "笔记"],
  ["podcast", "播客"],
  ["docs", "文档"],
  ["chat", "对话"],
  ["other", "其他"],
];
const CATEGORY_LABEL = Object.fromEntries(CATEGORIES);
// v6 §Implementation：站点短名由中枢 `/api/sessions` 的 `site` 字段带回，
// 前端不再自带站点映射表（一份映射只有一个维护点）。
const YOUTUBE_HOSTS = ["youtube.com", "youtu.be"];
const THEME_KEY = "workbench-theme";
// v6 §Implementation·一期：侧栏折叠态（站点/账号两层）。侧栏每次全量重建，
// DOM 上的 class 会被冲掉，只能存 state + localStorage。
const SIDEBAR_FOLD_KEY = "workbench-sidebar-fold";
const SEARCH_DEBOUNCE_MS = 200;
const LIST_LIMIT = 500; // 单用户量级，一次拉全（服务端上限 500）
const REVIEW_LIMIT = 500; // §8.2 队列也一次拉全，due_at 升序
const TOAST_MS = 2600;
const SETTINGS_POLL_MS = 5000; // §8.4：保存后 ≤5s 轮询到外壳重探的新状态
// v34 T5（票 #218 / spec §C23）：顶栏收句状态条的轮询间隔；页面不可见时暂停。
const CAPTURE_POLL_MS = 10000;
// §8.4 的状态徽标：hotkey-status.json 的 state → （中文，样式类）
const HOTKEY_BADGES = {
  ok: ["可用", "is-ok"],
  taken: ["被占用", "is-bad"],
  dup: ["与本工具内重复", "is-warn"],
  bad: ["语法错误", "is-bad"],
};
const HOTKEY_MODIFIERS = ["Control", "Alt", "Shift", "Meta", "ContextMenu"];
// 浏览器 event.key → AHK 基础键名（§9.1 的写法）
const HOTKEY_NAMED_KEYS = {
  Escape: "Escape",
  Enter: "Enter",
  " ": "Space",
  Tab: "Tab",
  Backspace: "Backspace",
  Delete: "Delete",
  Insert: "Insert",
  Home: "Home",
  End: "End",
  PageUp: "PgUp",
  PageDown: "PgDn",
  ArrowUp: "Up",
  ArrowDown: "Down",
  ArrowLeft: "Left",
  ArrowRight: "Right",
};
// 小键盘运算键：event.code → AHK 基础键名（与 settings.py 的 _NAMED_BASES 同名）
const NUMPAD_CODE = {
  NumpadAdd: "NumpadAdd",
  NumpadSubtract: "NumpadSub",
  NumpadMultiply: "NumpadMult",
  NumpadDivide: "NumpadDiv",
  NumpadDecimal: "NumpadDot",
};
// 框内键能单独用的标点（与 settings.py 的 _BOX_PUNCT 一致；`;` `=` 进不了 ini）。
// 标点只在不带修饰键时收：AHK 的 `,.` 这类写法没有歧义，带修饰键则不然。
const PUNCT_BASE = ",-./'`[]\\";
// AHK 基础键名 → 给人看的写法（键位提示 / 帮助浮层用）
const KEY_DISPLAY = {
  Escape: "Esc",
  Enter: "Enter",
  Space: "空格",
  Tab: "Tab",
  Backspace: "Backspace",
  Delete: "Del",
  Insert: "Ins",
  Home: "Home",
  End: "End",
  PgUp: "PgUp",
  PgDn: "PgDn",
  Up: "↑",
  Down: "↓",
  Left: "←",
  Right: "→",
  NumpadAdd: "小键盘+",
  NumpadSub: "小键盘-",
  NumpadMult: "小键盘*",
  NumpadDiv: "小键盘/",
  NumpadDot: "小键盘.",
};
// 全局热键在「空态/帮助」里露脸的动作（key → 中文名）
const HINT_ACTIONS = [
  ["hotkey", "呼出速记框"],
  ["hotkey_capture", "直接截图取词"],
  ["hotkey_review", "打开取词台"],
];
const SCOPE_LABEL = { global: "全局", box: "框内" };
// §8.2 打分键：按键 → 档位 → 中文标签（档位名与 §8.3 / hub 一致）
const GRADES = [
  ["1", "again", "忘记"],
  ["2", "hard", "困难"],
  ["3", "good", "记得"],
  ["4", "easy", "简单"],
];
const GRADE_BY_KEY = { 1: "again", 2: "hard", 3: "good", 4: "easy" };

const state = {
  route: "list",
  query: "",
  filter: "all", // all / due / today / session
  sessionId: null,
  tag: null,
  records: [],
  total: 0,
  sessions: [],
  // v22 P1b（票 #166 / spec §8.1）：侧栏「文章」分组 + 文章页。文章是**独立列表**，
  // 不参与会话筛选 / 搜索；`articleId` 同时用于篇卡选中态。
  articles: [],
  articleTotal: 0,
  articleId: null,
  article: null,
  articleLoading: false,
  articleError: "",
  // v22 #171（用户验收反馈）：文章页单卡视图的当前段（0 基）。换文章 / 刷新 / 重分段后归 0；
  // 翻段只改这一个数（深链仍是 #/article/<id>，不把段号写进 hash）。
  articleIndex: 0,
  // v22 P3（票 #168 / spec §8.5）：「只播有词段」开关（默认关 = 整篇全文）；
  // 启动时由 readArticleWordedOnly() 从 localStorage 恢复（键 workbench-article-worded-only）。
  articleWordedOnly: false,
  summary: null,
  // v28：打开会话时的精确空释义数（GET /api/dict/pending）。不在会话筛选里、或
  // 后端旧版本没有这个端点时保持 null —— 提示条只在有可信计数时才出现。
  // 记录带上 sessionId：切换会话后旧计数不许冒充新会话的。
  sessionRefill: null,
  // 文章页同一口径（scope=article）：payload 的段落 + 待复核区不含 passage_index
  // 失效的记录，而补释义动作会连它们一起补；N 以服务端 COUNT 为准。
  articleRefill: null,
  selectedId: null,
  view: null, // 列表 / 星图；启动默认星图（readView() || "orbit"），切换即记住。见 currentView()
  // v14 T3（票 #124）：外壳偏好。immersive=星图全幅；sidebar=覆盖式侧栏展开；
  // header=会话头动作层展开。启动时被 readShell() 覆盖，缺键回落同一组默认值。
  shell: { immersive: true, sidebar: false, header: false },
  // v19 T2（票 #153）：右缘会话坞开合（hidden / peek / pinned）。运行时态，
  // 不入 localStorage —— 坞是临时工具条，刷新回隐藏（「默认不占地」诉求）。
  dock: "hidden",
  startAtFirst: false, // v14 T2：一次性标志 —— 下次星图渲染完直接聚焦最早一段（顺序通读）
  drawer: null,
  confirmDelete: false,
  confirmSession: null, // 侧栏里待确认删除的会话 id（null = 没有）
  // v6 §Implementation·一期：侧栏分组折叠态，只存「显式覆盖」——
  // 缺省 = 站点组展开、账号组收起（见 foldCollapsed）；tags 为票 #87 的标签区折叠。
  fold: { site: {}, account: {}, tags: {} },
  loading: true,
  error: "",
  theme: "light",
  hotkeyHints: null, // 全局热键的当前键位（GET /api/settings 一次，给空态/帮助用）
  wheelNav: true, // v15 T2（票 #127）：阅读页纸面滚轮换段；GET /api/settings 的 wheel_nav，缺省开
  tts: null, // v11 §6（票 #112）：GET /api/settings 的 tts 块（开关/可用性/主路径/缓存）
  review: null, // §8.2 本轮复习状态（进入 #/review 时重建）
  reviewSession: null, // `#/review?session=ID` 的会话过滤
  settings: null, // §8.4 设置页（GET /api/settings 的原样响应 + 派生字段）
  settingsForm: null, // 词典开关 / 空闲分钟 / 主题的待保存值
  settingsDraft: {}, // 键位：改过但还没保存的那些
  settingsStamp: "", // 上次渲染用的 探测戳 + 时效状态，变了才重绘
  settingsError: "",
  settingsLoading: false,
  settingsSaving: false,
  settingsTimer: 0,
  record: null, // 正在录键位的行 {key, scope, error}
  // v34 T6（票 #219 / spec A1-A4 / ADR-0014）：待选句池 —— 一级路由 #/backlog，
  // `#/pick/<id>` 是一等别名（同一页面，不改写 hash）。视图按会话分段、按需加载。
  poolId: null, // 路由聚焦的会话 id（#/backlog?session=N 或 #/pick/N）
  pool: null, // { loading, error, sessions, bySession, activeId, filter, saving, seq }
  // 挑词暂存（纯前端 UI 中间态）：sessionId -> sentenceId -> [{start,end,kind,text,key}]。
  // 落 localStorage（键 workbench-pick-drafts）；挑 = 起草，保存 = 落库（ADR-0014）。
  poolDrafts: {},
  // 服务端待挑数缓存（会话 id → 待挑数）：顶栏「待选句」计数与池子组头的同一份真值。
  backlogCounts: {},
  // 会话 id → 最近一次「挑词侧」计数写入的序号。loadAll 拿旧列表结果落地前先比对：
  // 请求发出后又有更新时，不许用旧值回压新值（计数一致性的竞态兜底）。
  pendingStamps: {},
  // v34 T5（票 #218 / spec §C21–C24）：收句全局状态（GET /api/capture/status 的原样
  // 回包）。pill 只是它的一个渲染面；`captureEventId` 是事件环的增量游标，
  // `captureLoaded` 保证首拉只对齐游标、不补弹历史事件。
  capture: null,
  captureTimer: 0,
  captureEventId: 0,
  captureLoaded: false,
};

let pendingCountSeq = 0; // 单调序号：每次从挑词页 / count 端点写入 pending 都 +1

const dom = {
  main: document.getElementById("main"),
  sidebar: document.getElementById("sidebar"),
  search: document.getElementById("search"),
  filters: document.getElementById("filters"),
  stats: document.getElementById("topbar-stats"),
  capturePill: document.getElementById("capture-pill"),
  reviewBtn: document.getElementById("review-btn"),
  backlogBtn: document.getElementById("backlog-btn"),
  backlogCount: document.getElementById("backlog-count"),
  themeBtn: document.getElementById("theme-btn"),
  settingsBtn: document.getElementById("settings-btn"),
  drawer: document.getElementById("drawer"),
  backdrop: document.getElementById("drawer-backdrop"),
  help: document.getElementById("help"),
  helpClose: document.getElementById("help-close"),
  helpNote: document.getElementById("help-hotkeys"),
  brand: document.querySelector(".brand"),
  toasts: document.getElementById("toast-host"),
  // v14 T3：immersive shell 的把手与遮罩（静态节点，不随主区重绘）
  handle: document.getElementById("sidebar-handle"),
  scrim: document.getElementById("shell-scrim"),
};

/* --- 小工具 ----------------------------------------------------------------- */

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function button(className, text, onClick) {
  const node = el("button", className, text);
  node.type = "button";
  if (onClick) node.addEventListener("click", onClick);
  return node;
}

function debounce(fn, ms) {
  let timer = 0;
  return (...args) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(() => fn(...args), ms);
  };
}

function parseTs(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/.exec(
    String(value || "")
  );
  if (!match) return null;
  return new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
    Number(match[6] || 0)
  );
}

function pad2(value) {
  return String(value).padStart(2, "0");
}

function nowStamp() {
  const date = new Date();
  return (
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ` +
    `${pad2(date.getHours())}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`
  );
}

function sameDay(a, b) {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

function formatTime(value) {
  const date = parseTs(value);
  if (!date) return String(value || "");
  const now = new Date();
  const hhmm = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  if (sameDay(date, now)) return `今天 ${hhmm}`;
  if (sameDay(date, new Date(now.getTime() - 86400000))) return `昨天 ${hhmm}`;
  const day = `${date.getMonth() + 1}-${pad2(date.getDate())}`;
  return date.getFullYear() === now.getFullYear()
    ? day
    : `${date.getFullYear()}-${day}`;
}

function formatFull(value) {
  const date = parseTs(value);
  if (!date) return String(value || "—");
  return (
    `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ` +
    `${pad2(date.getHours())}:${pad2(date.getMinutes())}`
  );
}

// v22 P1b（票 #166 / spec §8.1）：篇卡标题的 ShortTitle 口径 —— 与外壳
// `lib/Logic.ahk::ShortTitle` 同一条规则：单行化（空白折叠 + 去首尾）→ 超 28 字截断加「…」。
// 「去浏览器尾巴」（`_哔哩哔哩_bilibili` 之类）已在建库 `clean_title` 时做过，篇卡这里不再重复。
// 列表/抽屉里的会话标题仍显示完整标题（CSS 省略），只有「文章」篇卡按这个口径收短。
function shortTitle(title) {
  const text = String(title || "")
    .replace(/\s+/g, " ")
    .trim();
  return text.length > 28 ? `${text.slice(0, 28)}…` : text;
}

function isDue(record) {
  return Boolean(record.due_at) && String(record.due_at) <= nowStamp();
}

function tagList(record) {
  return String(record.tags || "")
    .split(/\s+/)
    .filter(Boolean);
}

/* §5.1：高亮词定位（纯函数）。顺序 = ① 整词（词边界）→ ② 整词同词干 → ③ 裸子串兜底。 */

// 极简词干（两边都做）：ies→y，去尾部 e/s/es/ed/ing/d（保留 ≥2 字符）
function stemKey(word) {
  const text = String(word || "").toLowerCase().trim();
  if (!text) return "";
  if (text.length > 4 && text.endsWith("ies")) return `${text.slice(0, -3)}y`;
  for (const suffix of ["ing", "ed", "es", "s", "d", "e"]) {
    if (text.length - suffix.length >= 2 && text.endsWith(suffix)) {
      return text.slice(0, text.length - suffix.length);
    }
  }
  return text;
}

// 正则元字符转义（目标词当字面量用，别让它变成模式）
function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// v4.1（#54 P2-2）：① 整词（词边界）大小写不敏感匹配，取**首次出现**。
// 短语（含空格）也走这条：`\b` 只锚两端，中间的空格是字面量。
// `he` 不再命中 `The`、`us` 不再命中 `use`、`art` 不再命中 `start`。
// 性能：整页重绘时同一个目标词会被反复匹配（列表 / 星图 / 抽屉各一遍），
// 编译好的正则按词缓存，省掉每次 new RegExp 的重新编译。匹配语义与原来一致。
const WORD_PATTERN_CACHE = new Map();
function wholeWordHit(text, word) {
  let pattern = WORD_PATTERN_CACHE.get(word);
  if (!pattern) {
    pattern = new RegExp(`\\b${escapeRegExp(word)}\\b`, "i");
    if (WORD_PATTERN_CACHE.size >= 2000) WORD_PATTERN_CACHE.clear();
    WORD_PATTERN_CACHE.set(word, pattern);
  }
  const match = pattern.exec(text);
  if (!match) return null;
  return { start: match.index, end: match.index + match[0].length };
}

// 性能：同一条词条的位置在一次会话里会被重算很多遍（列表 / 星图 / 阅读页）。
// 词条对象在数据没变时是稳定的，按对象缓存「原句 → span」，内容一改（PATCH 会换新对象）
// 自然失效。span 只被读，不会被改。
const TARGET_SPAN_CACHE = new WeakMap();
function targetSpanForRecord(context, record) {
  const cached = TARGET_SPAN_CACHE.get(record);
  if (cached && cached.context === context) return cached.span;
  const span = findTargetSpan(context, record.target);
  TARGET_SPAN_CACHE.set(record, { context, span });
  return span;
}

// 原句里目标词的位置：[start, end)；找不到返回 null
function findTargetSpan(context, target) {
  const text = String(context || "");
  const word = String(target || "").trim();
  if (!text || !word) return null;
  const whole = wholeWordHit(text, word);          // ① 整词（词边界）
  if (whole) return whole;
  const stem = stemKey(word);                      // ② 整词同词干
  if (stem) {
    const tokens = /[A-Za-z]+(?:['’][A-Za-z]+)?/g;
    let match;
    while ((match = tokens.exec(text)) !== null) {
      if (stemKey(match[0]) === stem) {
        return { start: match.index, end: match.index + match[0].length };
      }
    }
  }
  const hit = text.toLowerCase().indexOf(word.toLowerCase()); // ③ 裸子串兜底（现行为）
  if (hit !== -1) return { start: hit, end: hit + word.length };
  return null;
}

// W8（票 #183）：改收整条记录 —— 旧数据释义有值但 meaning_source 为空（v21 之前落库
// 的记录没有来源位），显示中性「早期记录」，不再出现「有释义却标尚无释义」的自相矛盾。
function meaningSourceLabel(record) {
  const source = record && record.meaning_source;
  if (source === "user") return "手打";
  if (source === "recheck") return "重查"; // v29：取词台手动重查后写入的来源
  if (source === "machine") return "机翻"; // v22 修复（#172）：机翻兜底的来源口径
  if (String(source || "").startsWith("dict:")) return "词典填入";
  if (record && record.meaning) return "早期记录";
  return "尚无释义";
}

// v22 修复（#172）：机翻兜底的释义在取词台/hover 明确标「机翻」—— 不把机翻伪装成
// 词典义。返回 null（非机翻）或一个沿用 .badge 风格的小标；调用点自行决定挂在哪里。
function meaningMachineBadge(record) {
  if (!record || record.meaning_source !== "machine") return null;
  const badge = el("span", "badge is-machine", "机翻");
  badge.title = "机器翻译：会自动填入，仅供参考";
  return badge;
}

// v34 T1（票 #214 / spec D30）：多词目标空释义不再显示「短语（见原句）」——
// 那会把「还没补」伪装成「去看原句」。统一改「暂无释义」，补释义入口仍由
// 调用点的 `!record.meaning` 决定（文案改了，入口不丢）。
// kind 归一复用 `orbitKindOf`（与星图同一口径）；单词词条保持各调用点原有 fallback 文案。
function meaningLabel(record, fallback) {
  if (record && record.meaning) return record.meaning;
  const kind = orbitKindOf(record);
  if (kind !== "word") return "暂无释义";
  return fallback;
}

function sessionTitle(key) {
  if (key === "none") return "（未归属）";
  const session = state.sessions.find((item) => String(item.id) === String(key));
  return session ? session.title : `会话 #${key}`;
}

function sessionById(id) {
  if (id === null || id === undefined) return null;
  return state.sessions.find((item) => String(item.id) === String(id)) || null;
}

// 会话分类：非 7 类或缺失一律归 `other`（§4）
function sessionCategory(session) {
  const key = session && session.category ? String(session.category) : "other";
  return CATEGORY_LABEL[key] ? key : "other";
}

function hostOf(url) {
  const text = String(url || "").trim();
  if (!text) return "";
  try {
    const parsed = new URL(text);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "";
    return (parsed.hostname || "").toLowerCase();
  } catch (error) {
    return "";
  }
}

// 会话待挑数：backlogCounts 是池子 / count 端点写入的单一真值（loadAll 会用列表
// 接口的 subtitle_pending 补水，且不会回压请求期间的新值）；端点不可用（null）时
// 才回退列表接口字段。返回 NaN = 两个来源都没有（顶栏计数按 0 处理）。
function sessionPendingCount(session) {
  if (!session) return NaN;
  const cached = state.backlogCounts[session.id];
  if (cached !== undefined && cached !== null && Number.isFinite(Number(cached))) {
    return Math.max(0, Number(cached));
  }
  const server = Number(session.subtitle_pending);
  return Number.isFinite(server) ? Math.max(0, server) : NaN;
}

// 池子 / count 端点拿到最新 pending：写缓存 + 回写会话字段 + 记序号（竞态以它为准）。
function applyBacklogPending(sessionId, value) {
  const count = Math.max(0, Number(value) || 0);
  state.backlogCounts[sessionId] = count;
  state.pendingStamps[sessionId] = ++pendingCountSeq;
  const session = sessionById(sessionId);
  if (session) session.subtitle_pending = count;
}

// 列表接口落地：把 subtitle_pending 补给 backlogCounts，但跳过「请求发出后又被
// 挑词侧改写」的会话（stamp 比请求起点新），避免旧响应把新计数压回去。
function syncPendingFromSessions(sessions, seqAtStart) {
  for (const session of sessions || []) {
    const server = Number(session.subtitle_pending);
    if (!Number.isFinite(server)) continue;
    if ((Number(state.pendingStamps[session.id]) || 0) > seqAtStart) continue;
    state.backlogCounts[session.id] = Math.max(0, server);
  }
}

// v6 §Implementation：站点短名来自会话接口的 `site` 字段（不再前端自己算）
function withParam(url, key, value) {
  try {
    const parsed = new URL(url);
    parsed.searchParams.set(key, value); // 加/替换同名参数
    return parsed.toString();
  } catch (error) {
    return url;
  }
}

// §2.6：跳转链接合成。有秒数时 B站 → t=<int>、YouTube → t=<int>s；其他站点原样。
function jumpUrl(url, seconds) {
  const text = String(url || "");
  if (!text) return "";
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds)))
    return text;
  const host = hostOf(text);
  if (!host) return text;
  const total = Math.max(0, Math.floor(Number(seconds)));
  if (host === "bilibili.com" || host.endsWith(".bilibili.com"))
    return withParam(text, "t", String(total));
  if (YOUTUBE_HOSTS.some((item) => host === item || host.endsWith("." + item)))
    return withParam(text, "t", `${total}s`);
  return text;
}

// `mm:ss` / `h:mm:ss` 文本 → 秒；解析不出返回 null（调用方回退到会话 URL）
function positionSeconds(value) {
  const match = /^(?:(\d+):)?([0-5]?\d):([0-5]\d)$/.exec(String(value || "").trim());
  if (!match) return null;
  return Number(match[1] || 0) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}

// 展示层时间：分钟补零（后端 `time_text` 是不补零的 `2:36`，不能直接拿来展示）
function formatClock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  return hours
    ? `${hours}:${pad2(minutes)}:${pad2(secs)}`
    : `${pad2(minutes)}:${pad2(secs)}`;
}

// 「打开原视频 ↗」/「▶ 回到B站 · 02:36」：新标签打开，键盘可 Tab + Enter
// v6 §Implementation：`site` 由调用方从会话接口的 `site` 字段传进来（缺省回「原视频」）
function videoLink(url, seconds, site) {
  const label = String(site || "") || "原视频";
  const timed =
    seconds !== null && seconds !== undefined && Number.isFinite(Number(seconds));
  const text = timed
    ? `▶ 回到 ${label} · ${formatClock(Number(seconds))}`
    : "打开原视频 ↗";
  const href = jumpUrl(url, timed ? Number(seconds) : null);
  // W17（票 #189）：赋 href 前按 hostOf 统一校验（只放行 http/https）——后端已挡脏
  // 数据，这里是纵深：万一存量数据 / 越权写入了 `javascript:` 之类的协议，宁可把
  // 链接降级成纯文本（保留文案与原 URL 作 title），也不给可点击的坏 href 留入口。
  const safe = hostOf(href);
  const node = el(safe ? "a" : "span", "video-link", text);
  if (safe) {
    node.href = href;
    node.target = "_blank";
    node.rel = "noopener";
    node.title = href;
    node.setAttribute(
      "aria-label",
      timed
        ? `在新标签打开原视频（${label} ${formatClock(Number(seconds))}）`
        : "在新标签打开原视频"
    );
  } else {
    node.title = String(url || "");
  }
  return node;
}

// AHK 键位串（`^!1`）→ 给人看的写法（`Ctrl+Alt+1`）；空串返回空。
function keyDisplay(spec) {
  const text = String(spec || "").trim();
  if (!text) return "";
  const mods = { "^": "Ctrl", "!": "Alt", "+": "Shift", "#": "Win" };
  const parts = [];
  let index = 0;
  while (index < text.length && mods[text[index]]) {
    parts.push(mods[text[index]]);
    index += 1;
  }
  const base = text.slice(index);
  const baseLabel = KEY_DISPLAY[base] || (/^[a-z]$/.test(base) ? base.toUpperCase() : base);
  parts.push(baseLabel);
  return parts.join("+");
}

// 空态 / 帮助浮层里指的「当前」全局键位（拿不到就退回默认）
function hintText(key, fallback) {
  const spec = state.hotkeyHints ? state.hotkeyHints[key] : null;
  return keyDisplay(spec || fallback);
}

async function loadHotkeyHints() {
  try {
    const payload = await api("/api/settings");
    state.hotkeyHints = payload.hotkeys || {};
    // v15 T2（票 #127）：滚轮换段开关；GET 缺键 / 中枢没就绪都按「开着」兜底
    state.wheelNav = Number(payload.wheel_nav) !== 0;
    applyTtsState(payload.tts);
  } catch (error) {
    state.hotkeyHints = {}; // 中枢没就绪：键位提示留空，不打扰
    state.wheelNav = true; // 读不到开关按「开着」兜底（与 wheel_nav 缺省值一致）
    applyTtsState(null); // 读不到开关按「开着」兜底（warm / 静音预热照常可用）
  }
  // v14 T2：只补热键相关的两处，不再整页重绘。这里原来调 renderAll()，而它跟 loadAll()
  // 是并行的两个请求 —— 谁后到谁重绘，晚到的那次会把刚建好的星图拆掉重建，
  // 「深链 read=first 首屏聚焦最早一段」就这么被吃掉了。有数据时列表/星图与热键无关。
  renderHelpNote();
  if (!state.loading && state.route === "list" && !state.records.length) renderMain();
  // v11 §6（票 #112）：warm 与静音预热都等 tts 状态就位 —— 关掉后一次都不发
  startTtsActivity();
}

// v11 §6（票 #112）：全局唯一一份「在线发音状态」，只来自 GET /api/settings 的 tts 块
function applyTtsState(block) {
  state.tts = block && typeof block === "object" ? block : null;
}

/* --- API -------------------------------------------------------------------- */

async function api(path, options) {
  const response = await fetch(path, options);
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    payload = null;
  }
  if (!response.ok || !payload || payload.ok === false) {
    const message = (payload && payload.error) || `HTTP ${response.status}`;
    const failure = new Error(message);
    failure.payload = payload; // 400 的 errors 字段：设置页拿它报「哪一项不合法」
    throw failure;
  }
  return payload;
}

function jsonRequest(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/* --- 播放链路事件上报（2026-11-06）------------------------------------------
 * hub.log 只记服务端动作；缓冲卡住、音频报错、请求超时被 AbortController 砍掉、
 * 连播停在第几段，以前一个字都不落盘。这里把关键事件批量送到
 * POST /api/playback/log（写 logs/playback.log；服务端 playback_log.py 还会按
 * 白名单再过滤一次，双保险）。
 * 纪律：只上报元数据（事件名 / 耗时 / 状态码 / 错误码 / 段片号 / 后端 / 字节数），
 * 绝不带文本、URL、标题；批量攒着发、失败静默，绝不改变播放流程。
 */
const PLAYBACK_LOG_URL = "/api/playback/log";
const PLAYBACK_LOG_FLUSH_MS = 2000;
const PLAYBACK_LOG_BATCH = 20;
const PLAYBACK_LOG_MAX = 100;
const playbackLogSid = Math.random().toString(36).slice(2, 10);
let playbackLogQueue = [];
let playbackLogSeq = 0;
let playbackLogTimer = 0;

function logPlaybackEvent(event, fields) {
  try {
    const item = Object.assign(
      { event, sid: playbackLogSid, seq: ++playbackLogSeq },
      fields || {}
    );
    playbackLogQueue.push(item);
    if (playbackLogQueue.length > PLAYBACK_LOG_MAX) playbackLogQueue.shift();
    if (playbackLogQueue.length >= PLAYBACK_LOG_BATCH) flushPlaybackLog();
    else if (!playbackLogTimer)
      playbackLogTimer = window.setTimeout(flushPlaybackLog, PLAYBACK_LOG_FLUSH_MS);
  } catch (error) {
    // 日志绝不影响播放
  }
}

function flushPlaybackLog() {
  if (playbackLogTimer) {
    window.clearTimeout(playbackLogTimer);
    playbackLogTimer = 0;
  }
  if (!playbackLogQueue.length) return;
  const events = playbackLogQueue;
  playbackLogQueue = [];
  try {
    // keepalive：页面被关 / 切路由时这一批也能发出去 —— 正是「中断」现场最关键的一批
    fetch(PLAYBACK_LOG_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events }),
      keepalive: true,
    }).catch(() => {});
  } catch (error) {
    // 上报失败静默：日志链路不许把播放打断
  }
}

window.addEventListener("pagehide", flushPlaybackLog);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") flushPlaybackLog();
});

function recordsUrl() {
  const params = new URLSearchParams();
  params.set("limit", String(LIST_LIMIT));
  params.set("sort", "-captured_at");
  const query = state.query.trim();
  if (query) params.set("query", query);
  if (state.tag) params.set("tag", state.tag);
  if (state.filter === "due") params.set("state", "due");
  else if (state.filter === "today") params.set("state", "today");
  else if (state.filter === "session" && state.sessionId !== null)
    params.set("session", String(state.sessionId));
  return `/api/records?${params.toString()}`;
}

// 票 #72：单调请求序号。列表 / 抽屉 / 顶栏计数各有一条自己的线（互不取消），
// 只有「最后一次发出」的那条响应能落地；早发出、后到的旧响应静默丢弃 ——
// 不覆盖新视图，也不弹错。与复习队列的 state.review 守卫同一个道理。
let loadAllSeq = 0;
let drawerSeq = 0;
let summarySeq = 0;

async function loadAll({ skeleton = false, background = false, changedIds = [], structural = false } = {}) {
  const seq = ++loadAllSeq;
  const pendingSeqAtStart = pendingCountSeq; // v33：旧列表响应不得回压期间的挑词计数
  const sessionId =
    state.filter === "session" && state.sessionId !== null ? state.sessionId : null;
  // v31 #196：后台重拉保留旧快照；收尾时只把「变了的 / 显式带来的」卡片行就地换掉。
  const previousRecords = background ? state.records : null;
  if (skeleton) {
    state.loading = true;
    renderMain();
  }
  try {
    const [records, sessions, summary, articles, pending] = await Promise.all([
      api(recordsUrl()),
      api("/api/sessions"),
      api("/api/summary"),
      // 旧 hub 没有这个端点时文章分组留空，不影响列表主路径（回退演练）
      api("/api/articles?limit=50&offset=0").catch(() => ({
        articles: [],
        total: 0,
      })),
      // v28：打开会话时的提示条要精确 N（已载入的 500 条不能当全量）。端点不存在就
      // 当没有提示，不耽误列表主路径。
      sessionId === null
        ? Promise.resolve(null)
        : api(`/api/dict/pending?scope=session&id=${sessionId}`).catch(() => null),
    ]);
    if (seq !== loadAllSeq) return; // 旧响应不覆盖新视图
    state.records = records.records || [];
    state.total = Number(records.total || 0);
    state.sessions = sessions.sessions || [];
    // v33 修复轮：列表接口的 subtitle_pending 是「按会话待挑数」的列表版，落地时
    // 补给徽标缓存（跳过期间被挑词侧更新过的会话，见 syncPendingFromSessions）。
    syncPendingFromSessions(state.sessions, pendingSeqAtStart);
    state.articles = articles.articles || [];
    state.articleTotal = Number(articles.total || 0);
    state.summary = summary;
    state.sessionRefill =
      sessionId !== null && pending && Number.isFinite(Number(pending.empty))
        ? { sessionId, empty: Number(pending.empty) }
        : null;
    state.error = "";
    // §3：列表加载 / 选中会话 → 当前列表靠前的 target 交后台预热（去抖 500ms、最多 40）
    prewarmTargets(
      state.records.slice(0, PREWARM_MAX_TARGETS).map((record) => record.target)
    );
  } catch (error) {
    if (seq !== loadAllSeq) return; // 旧响应的错误也不落地（静默）
    // 后台重拉失败不改当前视图的错误态：用户手上还有一份可用数据。
    if (!background) state.error = error && error.message ? error.message : String(error);
  } finally {
    if (seq === loadAllSeq) {
      state.loading = false;
      if (background) renderBackgroundUpdates(previousRecords, changedIds, structural);
      else renderAll();
    }
  }
}

/* --- v22 P1b（票 #166 / spec §8.2）：文章页一次拉全篇 -------------------------- */

// 一篇 ≤60 段 × 数百字 ≈ 20–40KB（spec §8.6），一次拉完整页渲染；与列表/抽屉一样
// 用单调序号守卫：路由切走 / 部分失败时旧响应静默丢弃。
let articleSeq = 0;
async function loadArticle(id, { skeleton = false, keepPosition = false } = {}) {
  const seq = ++articleSeq;
  const prevIndex = state.articleIndex;
  const sameArticle = state.article !== null && String(state.articleId) === String(id);
  const inPlaceRefresh = keepPosition && state.route === "article" && sameArticle;
  state.articleLoading = true;
  state.articleError = "";
  if (!inPlaceRefresh) {
    state.article = null;
    state.articleRefill = null;
  }
  if (skeleton) renderMain();
  try {
    const [payload, pending] = await Promise.all([
      api(`/api/articles/${id}`),
      // v28：文章级 N 也用服务端精确计数（payload 不含 passage_index 失效的记录）。
      // 旧 hub / 请求失败就退回 payload 里能数到的条数（文案降级「至少 N 条」）。
      api(`/api/dict/pending?scope=article&id=${Number(id)}`).catch(() => null),
    ]);
    if (seq !== articleSeq) return; // 旧响应不覆盖新视图
    state.article = payload;
    state.articleId = Number(payload.id);
    state.articleRefill =
      pending && Number.isFinite(Number(pending.empty))
        ? { articleId: Number(payload.id), empty: Number(pending.empty) }
        : null;
    const lastIndex = Math.max((payload.paragraphs || []).length - 1, 0);
    if (inPlaceRefresh) {
      // v31 #196：同篇因变更刷新才保位；越界夹到末段（用户故事 8）。
      state.articleIndex = Math.min(Math.max(prevIndex, 0), lastIndex);
    } else {
      state.articleIndex = 0; // v22 #171：新文章（深链 / 刷新 / 重分段）从第 1 段开始
    }
  } catch (error) {
    if (seq !== articleSeq) return;
    if (inPlaceRefresh) {
      // 原位刷新失败：当前画面还完好，别把 error-box 盖到已读内容上。
      state.articleError = "";
      state.articleLoading = false;
      toast(`刷新失败：${error && error.message ? error.message : error}`);
      return;
    }
    state.articleError = error && error.message ? error.message : String(error);
  }
  state.articleLoading = false;
  if (seq !== articleSeq) return;
  if (inPlaceRefresh) {
    refreshArticleSurface();
    return;
  }
  renderAll();
}

/* --- v23 P1（spec §4.1 / §4.5）：补释义 --------------------------------------
 *
 * 历史空释义不会自愈（研究文档 §4：spool 行落盘时为空就永久为空），所以取词台给
 * 三个入口：阅读页 header（会话级）、文章页 header（文章级）、单条词旁「补」。
 * 三处同一套接口：POST /api/dict/refill 起任务 → GET /api/dict/refill/{job_id}
 * 每 500ms 轮询（按钮带「补齐中 x/y」进度）→ 完成 toast 计数，失败逐条进
 * 「词 · 原因 · 建议」小面板。轮询对网络抖动连续重试 3 次才放弃；404 判任务失效。
 * 放弃后 job_id 留 sessionStorage，叠层里提供「重新接驳」（W2/W10，票 #183）。
 * 纪律：原生 <button>（键盘可达）、用户内容只走 textContent、只用 DOM 节点拼装页面；
 * 只补空释义，前端不提供覆盖开关（spec §10 决策 1）。
 */
const REFILL_POLL_MS = 500;
// spec §4.1：limit 默认 200（后端同样夹住）；超过时前端提示分批。
const REFILL_LIMIT = 200;
// W2（票 #183）：轮询容错口径 —— 网络抖动连续重试 3 次才放弃；404（hub 重启 /
// 任务过期 10 分钟）不重试，任务真没了。放弃时 job_id 留 sessionStorage 可接驳。
const REFILL_POLL_RETRIES = 3;
const REFILL_JOB_KEY = "workbench-refill-job";
// spec §4.5：reason → [文案, 建议]。v24 D1（票 #182/#183）契约固定七档：
// not found / timeout / offline / network / parse / http / word-mismatch
// （fragment/empty 是更早的内部口径）。未知 reason 仍给可读兜底（后端口径可能先于前端演进）。
const REFILL_REASONS = {
  "not found": ["词典未收录", "可直接手填；短语可换更短的搭配再补"],
  "timeout": ["查询超时", "检查网络后重试；或稍后再补（已试过的词会缓存）"],
  "offline": ["离线模式（仅本地词典）", "在「设置 → 词典」打开在线查询后重试"],
  "network": ["网络失败", "检查网络或代理后重试（这次没到达词典源）"],
  "parse": ["词典源返回了无法解析的内容", "稍后重试；持续出现说明源侧异常"],
  "http": ["词典源返回了错误状态", "稍后重试；持续出现说明源侧异常"],
  "word-mismatch": ["词典返回了别的词，已拒收", "重试；持续出现说明源侧异常（不填错义）"],
  "fragment": ["看起来是没打完的词", "补全目标词后重试"],
  "empty": ["词典无释义内容", "可手填"],
};

let refillJob = null; // 进行中的任务 { jobId, body, btn, label, timer }；同一时刻只允许一个
let refillStarting = false; // POST 已发出、job_id 还没回来：挡住第二次点击（服务端仍会 409 兜底）
let refillReportNode = null; // 失败清单叠层（与详情抽屉互不占用状态）
let refillReportReturnFocus = null;

function refillReason(reason) {
  return (
    REFILL_REASONS[reason] || [
      String(reason || "未知原因"),
      "稍后重试；持续失败请查看中枢日志",
    ]
  );
}

/* --- v29：单条「重查」 ------------------------------------------------------
 * 释义看起来是别的词 / 明显不对时，用户可以从 popover、阅读卡、详情抽屉发起一次
 * **强制重查**：hub 用 fresh=True 绕过 dict_cache（错误旧义可能就钉在缓存里），
 * 先回新旧释义预览，用户确认后才覆盖这一条。确认前绝不写库；预览后若别处改过
 * 释义，apply 会 409，不覆盖更新的内容。
 */
let recheckStarting = false; // 一次只发一个重查请求
let recheckNode = null; // 预览叠层（独立于补释义失败清单）
let recheckReturnFocus = null;

function recheckInlineButton(record) {
  const node = button("refill-inline recheck-inline", "重查", (event) => {
    event.preventDefault();
    event.stopPropagation();
    startRecheck(record, node);
  });
  node.title = "重新查这一条的释义（先预览新旧释义，确认后才替换）";
  node.setAttribute("aria-label", `重查 ${record.target} 的释义`);
  return node;
}

function startRecheck(record, btn) {
  if (recheckStarting) {
    toast("正在重查，请稍候");
    return;
  }
  recheckStarting = true;
  const label = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "重查中…";
    btn.setAttribute("aria-busy", "true");
  }
  api("/api/dict/recheck", jsonRequest("POST", { id: record.id }))
    .then((payload) => {
      recheckStarting = false;
      restoreRefillButton(btn, label);
      openRecheckDialog(payload);
    })
    .catch((error) => {
      recheckStarting = false;
      restoreRefillButton(btn, label);
      // api() 对 `ok:false` 也走 catch；失败原因在 error.payload.reason 里，
      // 不能只报 HTTP 200（会丢掉「返回了别的词」这类关键提示）。
      const failure = error && error.payload;
      if (failure && failure.reason) {
        const [reasonLabel, advice] = refillReason(failure.reason);
        toast(`重查失败：${reasonLabel}；${advice}`);
        return;
      }
      const message = error && error.message ? error.message : String(error);
      if (message === "not found") {
        // 旧中枢没有 /api/dict/recheck 路由 → 404 `not found`。磁盘上的新前端
        // 配着内存里的旧中枢时，必须说清是版本问题，不能报成词典失败。
        toast("重查接口不可用：中枢可能还是旧版本，请重启英语取词后再试");
        return;
      }
      if (message === "record not found") {
        toast("词条不存在，可能已被删除");
        return;
      }
      if (message === "recheck expired") {
        toast("重查结果已过期，请重新重查");
        return;
      }
      toast(`重查失败：${message}`);
    });
}

function recheckReportOpen() {
  return recheckNode !== null;
}

function closeRecheckDialog() {
  if (!recheckNode) return;
  recheckNode.remove();
  recheckNode = null;
  const back = recheckReturnFocus;
  recheckReturnFocus = null;
  if (back && back.isConnected) back.focus();
}

function recheckSide(label, data, kind) {
  const side = el("section", `recheck-side is-${kind}`);
  const head = el("div", "recheck-label", label);
  if (kind === "new")
    head.append(el("span", "badge", data.machine ? "机翻" : "词典"));
  side.append(head);
  if (data.phonetic) side.append(el("div", "recheck-phonetic", data.phonetic));
  side.append(el("div", "recheck-text", data.meaning || "（空）"));
  return side;
}

// 预览层：只展示，不写库。确认按钮才走 apply。
function openRecheckDialog(preview) {
  closeRecheckDialog();
  recheckReturnFocus =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const overlay = el("div", "refill-report-backdrop");
  const panel = el("div", "refill-report recheck-report");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-labelledby", "recheck-title");
  panel.tabIndex = -1;

  const head = el("div", "refill-report-head");
  const title = el(
    "h3",
    "refill-report-title",
    `重查释义：${preview.target}`
  );
  title.id = "recheck-title";
  head.append(title);
  head.append(
    button("btn btn-subtle refill-report-close", "关闭", () =>
      closeRecheckDialog()
    )
  );
  panel.append(head);

  const diff = el("div", "recheck-diff");
  diff.append(recheckSide("现有释义", preview.old, "old"));
  diff.append(recheckSide("重查结果", preview.new, "new"));
  panel.append(diff);

  panel.append(
    el(
      "p",
      "refill-report-note",
      "只有点「用新释义替换」才会写库；保留原释义不会改动任何内容。"
    )
  );
  if (preview.same) {
    panel.append(
      el(
        "p",
        "refill-report-note",
        "重查结果与现有释义相同；如果仍觉得不对，可能是目标词或原句需要修改。"
      )
    );
  }

  const actions = el("div", "refill-report-actions");
  const primary = button("btn btn-primary", "用新释义替换", () =>
    applyRecheck(preview, primary)
  );
  actions.append(primary);
  actions.append(button("btn", "保留原释义", () => closeRecheckDialog()));
  panel.append(actions);

  overlay.append(panel);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeRecheckDialog();
  });
  document.body.append(overlay);
  recheckNode = overlay;
  primary.focus();
}

function applyRecheck(preview, primary) {
  primary.disabled = true;
  primary.textContent = "替换中…";
  api(
    "/api/dict/recheck/apply",
    jsonRequest("POST", { id: preview.record_id, token: preview.token })
  )
    .then((payload) => {
      closeRecheckDialog();
      const record = payload && payload.record;
      if (record) {
        state.records = state.records.map((item) =>
          item.id === record.id ? record : item
        );
        if (state.drawer && state.drawer.id === record.id) {
          state.drawer = record;
          renderDrawer();
        }
      }
      toast("已更新释义");
      refreshAfterRefill(record ? [record.id] : []);
    })
    .catch((error) => {
      primary.disabled = false;
      primary.textContent = "用新释义替换";
      const message = error && error.message ? error.message : String(error);
      if (message === "record changed") {
        closeRecheckDialog();
        toast("释义已被修改，请重新重查");
        refreshAfterRefill();
        return;
      }
      toast(`替换失败：${message}`);
    });
}

function refillEmptyCount(records) {
  return (records || []).reduce(
    (sum, record) => sum + (record && !record.meaning ? 1 : 0),
    0
  );
}

// 单条词旁的「补」：只补这一条。stopPropagation —— 不许触发父卡点击 / 播放 / 开抽屉。
function refillInlineButton(record) {
  const node = button("refill-inline", "补", (event) => {
    event.preventDefault();
    event.stopPropagation();
    startRefill({ scope: "records", ids: [record.id] }, node);
  });
  node.title = "补查这一条的释义（只补空释义，不覆盖已有内容）";
  node.setAttribute("aria-label", `补 ${record.target} 的释义`);
  return node;
}

// header 入口：阅读页 / 文章页共用。N 由调用方算好（当前阅读上下文 / 整篇文章）。
// W3（票 #183）：atLeast = N 只是下界 —— 按钮文案如实降级「至少 N 条」，承诺不超出动作。
function refillHeaderButton(count, body, title, { atLeast = false } = {}) {
  const label = atLeast ? `补释义（至少 ${count} 条）` : `补释义（${count} 条）`;
  const node = button("btn btn-subtle refill-btn", label, (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (count > REFILL_LIMIT)
      toast(
        `${atLeast ? "至少" : "共"} ${count} 条，本次最多补 ${REFILL_LIMIT} 条；完成后可再点一次`
      );
    startRefill(body, node);
  });
  node.title = title;
  node.setAttribute(
    "aria-label",
    atLeast ? `补释义：至少 ${count} 条空释义` : `补释义：${count} 条空释义`
  );
  return node;
}

// v28：当前会话的后端精确空释义数；没有可信计数（旧 hub / 请求失败 / 不是这个会话）
// 返回 null。顶部提示条与阅读页 header 共用它，两处 N 不会再打架。
function sessionRefillCount(sessionId) {
  const entry = state.sessionRefill;
  if (!entry || String(entry.sessionId) !== String(sessionId)) return null;
  const count = Number(entry.empty);
  return Number.isFinite(count) ? count : null;
}

function articleRefillCount(articleId) {
  const entry = state.articleRefill;
  if (!entry || String(entry.articleId) !== String(articleId)) return null;
  const count = Number(entry.empty);
  return Number.isFinite(count) ? count : null;
}

// 阅读页的「当前上下文」= 当前这段所属会话（会话筛选用会话级接口；无会话时用可见的
// 空释义 id 走 records 级）。入口只在本段至少有一条空释义时出现（spec §4.1 入口 1），
// 但 N 是**会话级**计数 —— 用户点一次就把这个会话的空释义一起补掉。
// 星图 item 是渲染时的快照，刷新后可能已不在 state.records 里，所以当前段自己的记录
// 再并一次，保证 N 不会因一次刷新变成 0。
function readingRefillPlan(item) {
  const itemRecords = item && item.records ? item.records : [];
  if (!itemRecords.some((record) => !record.meaning)) return null;
  const primary = itemRecords[0];
  const sessionId = primary ? primary.session_id : null;
  const hasSession = sessionId !== null && sessionId !== undefined;
  const pool = hasSession
    ? state.records.filter(
        (record) =>
          record.session_id !== null &&
          record.session_id !== undefined &&
          String(record.session_id) === String(sessionId)
      )
    : state.records.slice();
  const seen = new Set(pool.map((record) => String(record.id)));
  const records = pool.slice();
  for (const record of itemRecords) {
    if (!seen.has(String(record.id))) records.push(record);
  }
  const empty = records.filter((record) => !record.meaning);
  if (!empty.length) return null;
  if (hasSession) {
    // v28：打开会话时已拿到后端精确计数，就与顶部提示条用同一个 N；拿不到（浏览全集 /
    // 旧 hub）才退回当前筛选子集的空释义数，文案仍降级「至少 N 条」。
    const exact = sessionRefillCount(sessionId);
    return {
      count: exact !== null ? exact : empty.length,
      atLeast: exact === null,
      body: { scope: "session", id: sessionId },
    };
  }
  // 无会话：按显式 ids 补，动作范围 = 数出来的这几条，N 是精确值。
  return {
    count: empty.length,
    body: { scope: "records", ids: empty.map((record) => record.id) },
  };
}

// 文章页：优先用服务端 COUNT（范围与 scope=article 动作完全一致）；旧 hub 拿不到时
// 退回可见段落 + 待复核区的空释义数，只当下界（文案降级「至少 N 条」）。
function articleRefillPlan(article) {
  if (!article) return null;
  const exact = articleRefillCount(article.id);
  let count = exact;
  if (count === null) {
    const records = [];
    for (const paragraph of article.paragraphs || []) {
      for (const record of paragraph.records || []) records.push(record);
    }
    for (const record of article.needs_review || []) records.push(record);
    count = refillEmptyCount(records);
  }
  if (!count) return null;
  return {
    count,
    atLeast: exact === null,
    body: { scope: "article", id: article.id },
    subject: "本文",
    title: "补查本文所有段落里仍为空的释义（只补空释义，不覆盖已有内容）",
  };
}

// v28：打开会话（filter=session）时的提示条计划。N 用后端精确 COUNT；没有可信计数
// （旧 hub / 请求失败）就不提示 —— 宁可不弹，也不拿 500 条上限的近似值骗人。
function sessionRefillPlan() {
  if (state.filter !== "session" || state.sessionId === null) return null;
  const count = sessionRefillCount(state.sessionId);
  if (count === null || count <= 0) return null;
  return {
    count,
    atLeast: false,
    body: { scope: "session", id: state.sessionId },
    subject: "本会话",
    title: "补查本会话里仍为空的释义（只补空释义，不覆盖已有内容）",
  };
}

// 打开会话 / 文章时的「缺释义」提示条：说明 + 批量补词按钮。按钮沿用 header 入口的
// 同一套进度 / 失败清单（startRefill → 轮询 → 完成刷新），不另起一套补释义链路。
function refillNotice(plan) {
  const notice = el("div", "refill-notice");
  const text = el(
    "span",
    "refill-notice-text",
    `${plan.subject}还有 ${plan.atLeast ? "至少 " : ""}${plan.count} 条词条没有释义`
  );
  text.setAttribute("role", "status"); // 提示出现时读屏播报一次；按钮不在 live region 里
  notice.append(text);
  // 单任务有 200 条防呆上限：超过时把「本次最多」写进提示，别让 N 像是能一次补完。
  notice.append(
    el(
      "span",
      "refill-notice-hint",
      plan.count > REFILL_LIMIT
        ? `只补空释义；本次最多补 ${REFILL_LIMIT} 条，完成后可再点一次`
        : "只补空释义，不覆盖已有内容"
    )
  );
  notice.append(
    refillHeaderButton(plan.count, plan.body, plan.title, { atLeast: plan.atLeast })
  );
  return notice;
}

function startRefill(body, btn) {
  if (refillJob || refillStarting) {
    toast("补释义任务已在运行，请稍候");
    return;
  }
  refillStarting = true;
  const label = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "补齐中…";
    btn.setAttribute("aria-busy", "true");
  }
  api("/api/dict/refill", jsonRequest("POST", { ...body, limit: REFILL_LIMIT }))
    .then((payload) => {
      refillStarting = false;
      clearStoredRefillJob(); // 新任务开工：之前留着的接驳 job_id 不再有意义
      refillJob = { jobId: payload.job_id, body, btn, label, timer: 0, retries: 0 };
      toast(`补释义开始：共 ${payload.total} 条`);
      scheduleRefillPoll();
    })
    .catch((error) => {
      refillStarting = false;
      restoreRefillButton(btn, label);
      const message = error && error.message ? error.message : String(error);
      if (message === "refill running") toast("补释义任务已在运行，请稍候");
      else toast(`补释义失败：${message}`);
    });
}

function scheduleRefillPoll() {
  if (!refillJob) return;
  window.clearTimeout(refillJob.timer);
  refillJob.timer = window.setTimeout(pollRefillJob, REFILL_POLL_MS);
}

async function pollRefillJob() {
  const job = refillJob;
  if (!job) return;
  job.timer = 0;
  try {
    const payload = await api(`/api/dict/refill/${encodeURIComponent(job.jobId)}`);
    if (refillJob !== job) return; // 已收尾 / 已被替换：旧回包不落地
    job.retries = 0; // 这轮通了：抖动计数清零
    if (!payload || payload.state !== "done") {
      updateRefillProgress(payload); // W10：按钮给「补齐中 x/y」
      scheduleRefillPoll();
      return;
    }
    finishRefill(payload);
  } catch (error) {
    if (refillJob !== job) return;
    const message = error && error.message ? error.message : String(error);
    // 404 = hub 重启 / 任务过期（10 分钟）：任务真没了，重试与接驳都没有意义
    if (message === "not found") {
      endRefill();
      clearStoredRefillJob();
      toast("任务已失效，请重试");
      return;
    }
    // 其余（网络抖动 / 中枢暂不可达）按 W2 重试：连续 3 次才放弃，别一次抖动就弃任务
    job.retries += 1;
    if (job.retries <= REFILL_POLL_RETRIES) {
      scheduleRefillPoll();
      return;
    }
    abandonRefill(job, message);
  }
}

// W10：轮询间隙把进度写到入口按钮上（回包字段 done/total）。按钮可能已被重绘摘走
//（isConnected 为假就不写）；「重新接驳」时按钮引用为空，进度只活在 toast / 叠层里。
function updateRefillProgress(payload) {
  const job = refillJob;
  if (!job || !job.btn || !job.btn.isConnected) return;
  const total = Number(payload && payload.total) || 0;
  const done = Number(payload && payload.done) || 0;
  job.btn.textContent = `补齐中 ${done}/${total}`;
}

// W2：连续重试用尽 → 停止轮询，但 hub 的任务可能还在跑。job_id 存进 sessionStorage，
// 弹「重新接驳」叠层：接驳 = 继续跟踪；放弃 = 不再跟踪（已补上的释义不受影响）。
function abandonRefill(job, message) {
  endRefill();
  storeRefillJob(job.jobId);
  toast(`补释义轮询中断：${message}`);
  openRefillReattach(job.jobId, message);
}

// W2：接驳只需要 job_id（轮询就是 GET /api/dict/refill/<job_id>）。原按钮引用多半已
// 随重绘失效，进度不再有落点，完成/再次中断照旧走 finishRefill / abandonRefill。
function resumeRefillJob(jobId) {
  if (refillJob || refillStarting) {
    toast("补释义任务已在运行，请稍候");
    return;
  }
  refillJob = { jobId: String(jobId), body: null, btn: null, label: "", timer: 0, retries: 0 };
  toast("已重新接驳补释义任务");
  scheduleRefillPoll();
}

function storeRefillJob(jobId) {
  try {
    window.sessionStorage.setItem(REFILL_JOB_KEY, String(jobId));
  } catch (error) {
    /* 隐私模式等场景存不了：本次会话内的接驳入口仍在（叠层按钮） */
  }
}

function readStoredRefillJob() {
  try {
    return window.sessionStorage.getItem(REFILL_JOB_KEY) || "";
  } catch (error) {
    return "";
  }
}

function clearStoredRefillJob() {
  try {
    window.sessionStorage.removeItem(REFILL_JOB_KEY);
  } catch (error) {
    /* 存不了也就无从清：接驳入口随本会话消失 */
  }
}

function finishRefill(payload) {
  const failed = Array.isArray(payload.failed) ? payload.failed : [];
  clearStoredRefillJob(); // 任务收尾：之前留的接驳 job_id 不再有意义
  endRefill();
  toast(`补释义：成功 ${Number(payload.filled) || 0}，仍缺 ${failed.length}`);
  if (failed.length) openRefillReport(failed, payload);
  refreshAfterRefill();
}

function endRefill() {
  const job = refillJob;
  refillJob = null;
  refillStarting = false;
  if (!job) return;
  window.clearTimeout(job.timer);
  job.timer = 0;
  restoreRefillButton(job.btn, job.label);
}

function restoreRefillButton(btn, label) {
  if (!btn || !btn.isConnected) return; // 页面已重绘：旧按钮脱离文档，不用管
  btn.disabled = false;
  btn.removeAttribute("aria-busy");
  if (label) btn.textContent = label;
}

// 完成后的刷新：文章页保位重拉本文（只重画当前卡）；其它入口后台重拉列表 / 侧栏，
// 当前视图与抽屉保持不动。`changedIds` 让保存路径把「刚改过的那条」显式交给行刷新。
async function refreshAfterRefill(changedIds = []) {
  if (state.route === "article" && state.articleId) {
    await loadArticle(state.articleId, { keepPosition: true });
    return;
  }
  await loadAll({ background: true, changedIds });
}

// v31 #196：编辑保存成功后的刷新 —— 文章页保位重拉本文（只重画当前卡），
// 其它路由后台重拉列表 / 侧栏 / 待补数；都不整页重建，调用方无需 await。
// 改原句 / 挪会话会改变卡片归并键：调用方传 structural，后台收尾重画列表内容。
function refreshAfterChange(changedIds = [], { structural = false } = {}) {
  if (state.route === "article" && state.articleId) {
    loadArticle(state.articleId, { keepPosition: true });
  }
  loadAll({ background: true, changedIds, structural });
}

function refillReportOpen() {
  return refillReportNode !== null;
}

function closeRefillReport() {
  if (!refillReportNode) return;
  refillReportNode.remove();
  refillReportNode = null;
  const back = refillReportReturnFocus;
  refillReportReturnFocus = null;
  if (back && back.isConnected) back.focus();
}

// 失败清单：独立叠层（不复用详情抽屉 —— 抽屉的 state.drawer 语义是「正在看某条词」）。
// 空列表不打开（全成功只 toast）；Esc / 关闭按钮 / 点遮罩都能收。
function openRefillReport(failed, payload) {
  closeRefillReport();
  refillReportReturnFocus =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const overlay = el("div", "refill-report-backdrop");
  const panel = el("div", "refill-report");
  panel.setAttribute("role", "dialog");
  // W15（票 #189）：不声明 aria-modal —— 两处叠层都没有焦点陷阱（Tab 能走到页面别处），
  // 声明模态与行为不符反而误导读屏；降级为非模态 dialog 语义（改动小者），Esc 关层照旧。
  panel.setAttribute("aria-labelledby", "refill-report-title");
  panel.tabIndex = -1;

  const head = el("div", "refill-report-head");
  const title = el("h3", "refill-report-title", `补释义：${failed.length} 条仍缺`);
  title.id = "refill-report-title";
  head.append(title);
  head.append(
    button("btn btn-subtle refill-report-close", "关闭", () => closeRefillReport())
  );
  panel.append(head);

  const filled = Number(payload && payload.filled) || 0;
  const skipped = Number(payload && payload.skipped) || 0;
  panel.append(
    el(
      "p",
      "refill-report-summary",
      `成功 ${filled} 条 · 跳过 ${skipped} 条 · 仍缺 ${failed.length} 条`
    )
  );
  panel.append(
    el(
      "p",
      "refill-report-note",
      "「跳过」= 期间已手填或已有释义（补释义不覆盖任何非空释义）。"
    )
  );

  const list = el("ul", "refill-report-list");
  for (const item of failed) {
    const record = item && typeof item === "object" ? item : {};
    const row = el("li", "refill-report-row");
    const word =
      record.target !== undefined && record.target !== null && record.target !== ""
        ? String(record.target)
        : `#${record.record_id}`;
    row.append(el("span", "refill-report-word", word));
    const [reasonLabel, advice] = refillReason(record.reason);
    row.append(el("span", "refill-report-reason", reasonLabel));
    row.append(el("span", "refill-report-advice", advice));
    list.append(row);
  }
  panel.append(list);

  overlay.append(panel);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeRefillReport();
  });
  document.body.append(overlay);
  refillReportNode = overlay;
  panel.focus();
}

// W2（票 #183）：轮询放弃后的「重新接驳」叠层 —— 与失败清单共用同一个叠层槽位
//（refillReportNode：同一时刻只有一个补释义叠层，Esc / 点遮罩都能收）。
// 页面刷新后靠 sessionStorage 里留的 job_id 再弹一次（见启动块的 readStoredRefillJob）。
function openRefillReattach(jobId, reason) {
  closeRefillReport();
  refillReportReturnFocus =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const overlay = el("div", "refill-report-backdrop");
  const panel = el("div", "refill-report");
  panel.setAttribute("role", "dialog");
  // W15（票 #189）：同上 —— 无焦点陷阱就不声明模态，降级为非模态 dialog。
  panel.setAttribute("aria-labelledby", "refill-reattach-title");
  panel.tabIndex = -1;

  const head = el("div", "refill-report-head");
  const title = el("h3", "refill-report-title", "补释义轮询已中断");
  title.id = "refill-reattach-title";
  head.append(title);
  head.append(
    button("btn btn-subtle refill-report-close", "关闭", () => closeRefillReport())
  );
  panel.append(head);
  panel.append(
    el("p", "refill-report-summary", `连续重试仍失败（${reason}）。中枢的任务可能还在后台跑。`)
  );
  panel.append(
    el(
      "p",
      "refill-report-note",
      "「重新接驳」继续跟踪这个任务；「放弃」不再跟踪（已补上的释义不会丢）。"
    )
  );
  const actions = el("div", "refill-report-actions");
  actions.append(
    button("btn btn-primary", "重新接驳", () => {
      closeRefillReport();
      resumeRefillJob(jobId);
    })
  );
  actions.append(
    button("btn", "放弃", () => {
      clearStoredRefillJob();
      closeRefillReport();
    })
  );
  panel.append(actions);

  overlay.append(panel);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeRefillReport();
  });
  document.body.append(overlay);
  refillReportNode = overlay;
  panel.focus();
}

/* --- 渲染：顶栏 / 侧栏 -------------------------------------------------------- */

function renderChrome() {
  renderStats();
  renderBacklogNav();
  renderFilters();
  renderSidebar();
  renderHelpNote();
  syncListHeaderCount();
}

// v34 T6（票 #219 / spec A1）：顶栏「待选句」一级入口的计数 —— /api/sessions 的
// subtitle_pending 汇总（服务端真值）。池子进页 / 保存 / 撤销后都会重画。
function backlogTotalPending() {
  let total = 0;
  for (const session of state.sessions) {
    const count = sessionPendingCount(session);
    if (Number.isFinite(count)) total += count;
  }
  return total;
}

function renderBacklogNav() {
  if (!dom.backlogBtn || !dom.backlogCount) return;
  const total = backlogTotalPending();
  dom.backlogCount.textContent = String(total);
  dom.backlogCount.hidden = total <= 0;
  dom.backlogBtn.title = `打开待选句（${total} 句待挑）`;
  dom.backlogBtn.setAttribute("aria-label", `打开待选句（${total} 句待挑）`);
  dom.backlogBtn.classList.toggle("is-on", state.route === "backlog");
}

// 后台重拉后列表顶部的总条数也要跟着变（header 不整块重建，只改这一行）。
function syncListHeaderCount() {
  const sub = dom.main.querySelector(".list-sub");
  if (!sub) return;
  sub.textContent = state.loading ? "加载中…" : `${state.total} 条`;
}

function renderAll() {
  renderChrome();
  renderMain();
}

// v31 #196：后台重拉后的收尾 —— 只刷周边（顶栏 / 筛选 / 侧栏 / 待补提示）与
// 受影响的卡片行；当前视图和抽屉都保持原样，不整棵重建主区。
function renderBackgroundUpdates(previousRecords, changedIds, structural) {
  renderChrome();
  syncSessionRefillNotice();
  const changed = refreshChangedRows(previousRecords, changedIds, structural);
  // 星图阅读卡不在这条 .row 路径上：把新记录合回场景并重画当前卡。
  if (orbit && typeof orbit.updateRecords === "function") orbit.updateRecords(changed);
}

// 在 wrap 里换掉由渲染器生成的一块节点；没有旧节点时插到首个内容块之前。
function replaceOrInsertBlock(wrap, selector, fresh, anchorSelector) {
  const existing = wrap.querySelector(selector);
  if (existing) {
    existing.replaceWith(fresh);
    return;
  }
  const anchor = wrap.querySelector(anchorSelector);
  wrap.insertBefore(fresh, anchor || null);
}

// 卡片的归并键（与 contextCards 同一口径）：同会话 + 同原句才算同一张卡。
function cardKeyOf(record) {
  const context = String(record.context || "").trim();
  return `${record.session_id == null ? "none" : record.session_id}\u0001${
    context || `id:${record.id}`
  }`;
}

// 把后台重拉后变了的卡片行就地换掉（按 record id 找到所在旧行）。
// `changedIds` 是保存路径显式带来的（乐观合并过的记录 diff 看不出来）；再并上
// 快照对比兜住批量补释义这类调用方拿不到 id 的变更。
// 只有释义 / 音标 / 徽标这类卡片内变化才走「换一行」；合并 / 拆分卡、过滤进出
// 会让行与卡对不上 —— 这时重画列表内容（header / 侧栏 / 抽屉都不动）。
// 星图 / 复习页没有 .row / .groups：数据已更新，下次进入自然取到新值。
function refreshChangedRows(previousRecords, changedIds = [], structuralHint = false) {
  if (state.route !== "list") return new Set();
  const before = previousRecords
    ? new Map(previousRecords.map((record) => [String(record.id), record]))
    : new Map();
  const changed = new Set(changedIds.map(String));
  const afterIds = new Set(state.records.map((record) => String(record.id)));
  let structural = Boolean(structuralHint);
  for (const record of state.records) {
    const old = before.get(String(record.id));
    if (!old) {
      structural = true;
      continue;
    }
    if (cardKeyOf(old) !== cardKeyOf(record)) structural = true;
    if (
      old.meaning !== record.meaning ||
      old.context !== record.context ||
      old.target !== record.target ||
      old.phonetic !== record.phonetic ||
      old.kind !== record.kind ||
      old.tags !== record.tags ||
      old.position !== record.position ||
      old.session_id !== record.session_id ||
      old.doc_id !== record.doc_id ||
      old.passage_index !== record.passage_index
    ) {
      changed.add(String(record.id));
    }
  }
  for (const id of before.keys()) if (!afterIds.has(id)) structural = true;
  if (structural) {
    const groups = dom.main.querySelector(".groups");
    if (!groups) return changed;
    const scroll = window.scrollY;
    groups.replaceWith(state.records.length ? groupedList() : emptyView());
    window.scrollTo(0, scroll);
    return changed;
  }
  if (!changed.size) return changed;
  const freshRows = new Map();
  for (const group of groupRecords()) {
    for (const card of contextCards(group.records)) {
      const row = cardRow(card);
      for (const record of card.records) freshRows.set(String(record.id), row);
    }
  }
  const stale = new Set();
  for (const id of changed) {
    const node = dom.main.querySelector(`.row[data-record-ids~="${id}"]`);
    if (node) stale.add(node);
  }
  const placed = new Set();
  for (const node of stale) {
    const [firstId] = String(node.dataset.recordIds || "").split(" ");
    const fresh = freshRows.get(firstId);
    if (!fresh || placed.has(fresh)) continue;
    placed.add(fresh);
    node.replaceWith(fresh);
  }
  return changed;
}

// 列表路由顶部「本会话还有 N 条词条没有释义」提示条跟着精确计数走：原地换 / 收。
function syncSessionRefillNotice() {
  if (state.route !== "list") return;
  const wrap = dom.main.querySelector(".main-wrap.is-list");
  if (!wrap) return;
  const plan = !state.loading && !state.error ? sessionRefillPlan() : null;
  if (!plan) {
    const existing = wrap.querySelector(".refill-notice");
    if (existing) existing.remove();
    return;
  }
  replaceOrInsertBlock(
    wrap,
    ".refill-notice",
    refillNotice(plan),
    ".groups, .orbit, .skeleton, .empty, .error-box"
  );
}

// 帮助浮层里的全局热键提示：显示**当前**键位，而不是写死的 Ctrl+Alt+1
// （写死会让人以为改不了 —— 用户报障「快捷键框死了」）。
function renderHelpNote() {
  if (!dom.helpNote) return;
  const rows = HINT_ACTIONS.map(([key, label]) => {
    const spec = state.hotkeyHints ? state.hotkeyHints[key] : "";
    return spec ? `${keyDisplay(spec)} ${label}` : "";
  }).filter(Boolean);
  dom.helpNote.textContent = rows.length
    ? `取词热键（系统全局，由外壳注册；设置页里可改）：${rows.join(" · ")}。`
    : "取词热键在「设置」页里改（系统全局，由外壳注册）。";
}

// 性能：顶栏计数只在 summary / 错误态变化时重画（每次 loadAll 都会调 renderAll）。
let statsRenderSignature = null;
function renderStats() {
  const summary = state.summary;
  const signature = summary
    ? `${summary.records}|${summary.due_now}|${summary.today}|${state.error}`
    : `none|${state.error}`;
  if (signature === statsRenderSignature) return;
  statsRenderSignature = signature;
  if (!summary) {
    dom.stats.textContent = state.error ? "中枢未就绪" : "";
  } else {
    dom.stats.textContent = `共 ${summary.records} 词 · ${summary.due_now} 待复习 · 今日 ${summary.today}`;
  }
  const due = summary ? Number(summary.due_now || 0) : 0;
  clear(dom.reviewBtn);
  dom.reviewBtn.append(document.createTextNode("复习"));
  if (due > 0) dom.reviewBtn.append(el("span", "badge is-due", String(due)));
  dom.reviewBtn.append(el("kbd", null, "r"));
}

// v34 T5（票 #218 / spec §C19–C23）：顶栏收句常驻状态条。
// 相位词表是 spec 的 11 档正本；键位一律走 hintText 取真实配置值（不写死）。
const CAPTURE_PHASE_LABEL = Object.freeze({
  unarmed: "收句未开启",
  no_heartbeat: "心跳中断（页面不在前台）",
  cc_reading: "查轨中…",
  cc_ok: "CC 直读",
  cc_missing: "无英文 CC · 转屏幕采样",
  cc_error: "CC 故障 · 转屏幕采样",
  sampling: "采集中",
  paused: "已暂停",
  dormant: "需要拖一次字幕带",
  redrag_needed: "区域失效，请重拖",
  failed: "OCR 引擎不可用",
});

// 相位主文案：动态部分（总开关关 / 未按开始 / 两种拖框）取真值键位。
function capturePhaseText(status) {
  const phase = String((status && status.phase) || "unarmed");
  if (phase === "unarmed") {
    return status && status.enabled === false
      ? "收句已关（到设置页打开）"
      : `收句未开启（按 ${hintText("hotkey_capture_arm", "^!7")} 开始）`;
  }
  if (phase === "dormant") {
    return `需要拖一次字幕带（按 ${hintText("hotkey_redrag", "^!6")} 拖框）`;
  }
  if (phase === "redrag_needed") {
    return `区域失效，请重拖（${hintText("hotkey_redrag", "^!6")}）`;
  }
  if (phase === "paused") {
    return status && status.session ? "已暂停（回来自动续）" : "等待播放";
  }
  return CAPTURE_PHASE_LABEL[phase] || phase;
}

// 「多久前」：服务端时间戳统一是本地 `YYYY-MM-DD HH:MM:SS`，替换成 T 再解析。
function captureAgeText(stamp) {
  const text = String(stamp || "").trim();
  if (!text) return "";
  const at = Date.parse(text.replace(" ", "T"));
  if (!Number.isFinite(at)) return "";
  const seconds = Math.max(0, Math.round((Date.now() - at) / 1000));
  if (seconds < 60) return `${seconds}s 前`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  return `${Math.round(minutes / 60)} 小时前`;
}

// 会话名压短（B站尾巴不占位置；长标题截断，完整名放 title 属性）。
function captureSessionName(title) {
  const text = String(title || "")
    .replace(/_哔哩哔哩_bilibili$/, "")
    .trim();
  return text.length > 16 ? `${text.slice(0, 15)}…` : text;
}

// 副文案：正在采的会话名 + 本段句数 + 最近落库多久前。
function captureDetailText(status) {
  if (!status) return "";
  const parts = [];
  const session = status.session;
  if (session && session.title) parts.push(captureSessionName(session.title));
  const phase = String(status.phase || "");
  const inSegment = [
    "sampling",
    "dormant",
    "paused",
    "redrag_needed",
    "failed",
    "cc_ok",
    "cc_missing",
    "cc_error",
  ].includes(
    phase
  );
  if (session && inSegment) {
    parts.push(`本段 ${Number(status.added_in_segment) || 0} 句`);
  }
  const age = captureAgeText(status.last_added_at);
  if (age) parts.push(`最近落库 ${age}`);
  return parts.join(" · ");
}

function capturePhaseTone(phase, enabled) {
  if (phase === "sampling") return "live";
  if (phase === "cc_ok") return "ok";
  if (phase === "failed") return "bad";
  if (phase === "dormant" || phase === "redrag_needed" || phase === "cc_missing" ||
      phase === "cc_error") {
    return "warn";
  }
  return "idle";
}

// 关键事件（spec §C24）：**瞬时提示只走一个通道** —— fallback / redrag 由外壳
// 托盘气泡负责（用户看视频时取词台通常不在前台）；web 只消费事件游标，避免
// 取词台打开时同一事件双弹。开始 / 停止由 T3 的 RPC 回话 toast 负责，
// 保存成功沿用池子保存的 toast。
function captureEventToasts(events) {
  if (!Array.isArray(events)) return;
  for (const event of events) {
    const id = Number(event && event.id);
    if (!Number.isFinite(id) || id <= state.captureEventId) continue;
    state.captureEventId = id;
  }
  state.captureLoaded = true;
}

function renderCapturePill(status) {
  const node = dom.capturePill;
  if (!node) return;
  if (!status || typeof status !== "object") {
    node.hidden = true;
    return;
  }
  const phase = String(status.phase || "unarmed");
  const main = capturePhaseText(status);
  const detail = captureDetailText(status);
  const text = detail ? `${main} · ${detail}` : main;
  node.textContent = text;
  node.title = text;
  node.className = `capture-pill is-${capturePhaseTone(phase, status.enabled)}`;
  node.hidden = false;
  captureEventToasts(status.events);
}

async function pollCaptureStatus() {
  try {
    const status = await api("/api/capture/status");
    state.capture = status;
    renderCapturePill(status);
  } catch (error) {
    // 中枢不在位 / 老中枢没有这个端点：pill 收起，不打扰主界面
    state.capture = null;
    renderCapturePill(null);
  }
}

function startCapturePoll() {
  stopCapturePoll();
  if (document.visibilityState === "hidden") return;
  state.captureTimer = window.setInterval(pollCaptureStatus, CAPTURE_POLL_MS);
}

function stopCapturePoll() {
  if (state.captureTimer) {
    window.clearInterval(state.captureTimer);
    state.captureTimer = 0;
  }
}

// 页面不可见时暂停轮询（不烧请求）；恢复可见立刻拉一次再继续（spec §C23）。
function onCaptureVisibilityChange() {
  if (document.visibilityState === "hidden") {
    stopCapturePoll();
    return;
  }
  pollCaptureStatus();
  startCapturePoll();
}

// 启动入口：挂可见性监听 + 立刻拉一次 + 开 10s 轮询。
function startCaptureWatch() {
  document.addEventListener("visibilitychange", onCaptureVisibilityChange);
  startCapturePoll();
  pollCaptureStatus();
}

// v14 #121：侧栏默认收起后，「本会话」缺会话时的提示要指向真实的入口（左上 ☰ 把手）。
// 只在列表路由且侧栏没开时改口；复习 / 设置页侧栏是普通目录，仍说「左栏」。
function sessionPickHint() {
  return state.route === "list" && !state.shell.sidebar
    ? "先点左上 ☰ 选一个会话"
    : "先在左栏选一个会话";
}

// 性能：筛选条只在筛选 / 选中会话 / 路由变化时重画。
let filtersRenderSignature = null;
function renderFilters() {
  const sessionTitleText =
    state.sessionId === null ? "" : sessionTitle(String(state.sessionId));
  // v34 T6（票 #219 / spec A2）：筛选条右侧的「挑词（N 句待选）→」入口已撤除 ——
  // 待选句改走顶栏一级入口；这里只保留筛选 chip 与「本会话」提示。
  const signature = [
    state.filter,
    state.sessionId,
    sessionTitleText,
    state.route,
  ].join("|");
  if (signature === filtersRenderSignature) return;
  filtersRenderSignature = signature;
  for (const chip of dom.filters.querySelectorAll(".chip")) {
    const filter = chip.dataset.filter;
    chip.classList.toggle("is-on", state.filter === filter);
    if (filter === "session") {
      chip.disabled = state.sessionId === null;
      chip.title =
        state.sessionId === null ? sessionPickHint() : sessionTitle(String(state.sessionId));
    }
  }
  dom.reviewBtn.classList.toggle("is-on", state.route === "review");
  if (dom.settingsBtn) dom.settingsBtn.classList.toggle("is-on", state.route === "settings");
}

/* --- v6 §Implementation·一期：侧栏出处分组树 --------------------------------- */

// 分组键 = (site, account_id || account)（§Implementation·一期的数据与接口）；
// `site` 为空归「其他」组。站点与账号层的顺序都按会话列表的原始次序（最近在前）。
function provenanceGroups(sessions) {
  const sites = new Map();
  for (const session of sessions) {
    const site = String(session.site || "");
    const account = String(session.account || "");
    const accountKey = String(session.account_id || account);
    let group = sites.get(site);
    if (!group) {
      group = { site, label: site || "其他", total: 0, accounts: new Map() };
      sites.set(site, group);
    }
    let bucket = group.accounts.get(accountKey);
    if (!bucket) {
      bucket = { key: accountKey, label: account || "（未分组）", total: 0, sessions: [] };
      group.accounts.set(accountKey, bucket);
    }
    bucket.sessions.push(session);
    bucket.total += 1;
    group.total += 1;
  }
  return Array.from(sites.values(), (group) => ({
    site: group.site,
    label: group.label,
    total: group.total,
    accounts: Array.from(group.accounts.values()),
  }));
}

function foldKey(site, accountKey) {
  return `${site}\u0001${accountKey}`;
}

// 缺省：站点组展开、账号组收起（用户故事 8）。只有显式覆盖过的组才查得到值。
function foldCollapsed(kind, key) {
  const flag = state.fold[kind][key];
  return flag === undefined ? kind === "account" : !!flag;
}

// 这一帧到底该收起吗。**用户点过组头就一律听用户的**（state.fold 里那个键就是他的表态），
// 没点过才按「缺省 / 选中链展开（用户故事 9）」来画。
// 原来写的是 `!holdsSelected && foldCollapsed(...)`：含当前选中会话的那个组永远强制展开，
// 于是只剩这一个展开面板时，点组头写进了 state 却渲染不出来 —— 看起来像被锁死（用户报障）。
function foldStateFor(kind, key, holdsSelected) {
  const explicit = state.fold[kind][key];
  if (explicit !== undefined) return Boolean(explicit);
  return holdsSelected ? false : foldCollapsed(kind, key);
}

// 用户故事 9：选中哪条链，哪条链就展开。折叠态是「显式才有值」的，所以这里把
// 链上的键删掉、回到「缺省 + 选中」，别的组上手动的折叠选择不受影响。
function revealChain() {
  if (state.sessionId === null) return;
  for (const group of provenanceGroups(state.sessions)) {
    if (!group.accounts.some((bucket) => bucket.sessions.some(isSelectedSession))) continue;
    delete state.fold.site[group.site];
    for (const bucket of group.accounts) {
      if (bucket.sessions.some(isSelectedSession))
        delete state.fold.account[foldKey(group.site, bucket.key)];
    }
  }
  saveFold();
}

function saveFold() {
  try {
    window.localStorage.setItem(SIDEBAR_FOLD_KEY, JSON.stringify(state.fold));
  } catch (error) {
    /* 隐私模式等场景下 localStorage 不可用：折叠仍在本次会话生效 */
  }
}

function loadFold() {
  let saved = null;
  try {
    saved = JSON.parse(window.localStorage.getItem(SIDEBAR_FOLD_KEY) || "null");
  } catch (error) {
    saved = null;
  }
  state.fold = {
    site: saved && saved.site ? saved.site : {},
    account: saved && saved.account ? saved.account : {},
    tags: saved && saved.tags ? saved.tags : {},
  };
}

function isSelectedSession(session) {
  return state.sessionId !== null && String(session.id) === String(state.sessionId);
}

function groupHead(kind, label, total, dataset) {
  const head = button("side-group-head", "");
  head.dataset.fold = kind;
  for (const [key, value] of Object.entries(dataset)) head.dataset[key] = value;
  head.append(el("span", "side-caret"));
  head.append(el("span", "side-group-name", label));
  head.append(el("span", "side-count", String(total)));
  return head;
}

// 点组头只折叠，不做组级筛选（Out of Scope）。
// 收/展取的是**屏幕上这一帧的样子**（nextCollapsed 由调用方从 DOM 读），
// 否则「缺省收起但被选中链撑开」的账号组第一次点会算错方向、看起来点了没反应。
function toggleFold(kind, key, nextCollapsed) {
  state.fold[kind][key] = Boolean(nextCollapsed);
  saveFold();
  renderSidebar();
}

// 一对「全部展开 / 全部收起」：把当前所有组写成显式态（用户故事 10）。
function setAllFold(collapsed) {
  for (const group of provenanceGroups(state.sessions)) {
    state.fold.site[group.site] = collapsed;
    for (const bucket of group.accounts) {
      state.fold.account[foldKey(group.site, bucket.key)] = collapsed;
    }
  }
  saveFold();
  renderSidebar();
}

function siteGroupNode(group) {
  // 当前选中会话所在的那条链缺省展开（用户故事 9）：不写 state，只影响本次渲染；
  // 用户手动点过组头就以他为准（见 foldStateFor）。
  const holdsSelected = group.accounts.some((bucket) =>
    bucket.sessions.some(isSelectedSession)
  );
  const collapsed = foldStateFor("site", group.site, holdsSelected);
  const box = el("div", "side-group");
  box.dataset.group = "site";
  box.dataset.site = group.site;
  if (collapsed) box.classList.add("is-collapsed");
  const head = groupHead("site", group.label, group.total, { site: group.site });
  head.setAttribute("aria-expanded", collapsed ? "false" : "true");
  // 收起时也要看得出「选中会话在这个组里」，别让选中悄悄藏进折叠里
  if (holdsSelected) head.classList.add("is-holding");
  box.append(head);

  const body = el("div", "side-group-body");
  const single = group.accounts.length === 1; // 只有一个账号组时省略账号层（用户故事 4）
  for (const bucket of group.accounts) {
    if (single) {
      for (const session of bucket.sessions) body.append(sessionItem(session));
    } else {
      body.append(accountGroupNode(group.site, bucket));
    }
  }
  box.append(body);
  return box;
}

function accountGroupNode(site, bucket) {
  const holdsSelected = bucket.sessions.some(isSelectedSession);
  const collapsed = foldStateFor("account", foldKey(site, bucket.key), holdsSelected);
  const box = el("div", "side-group");
  box.dataset.group = "account";
  box.dataset.site = site;
  box.dataset.account = bucket.key;
  if (collapsed) box.classList.add("is-collapsed");
  const head = groupHead("account", bucket.label, bucket.total, {
    site,
    account: bucket.key,
  });
  head.setAttribute("aria-expanded", collapsed ? "false" : "true");
  if (holdsSelected) head.classList.add("is-holding");
  box.append(head);

  const body = el("div", "side-group-body");
  for (const session of bucket.sessions) body.append(sessionItem(session));
  box.append(body);
  return box;
}

function sessionItem(session) {
  const category = sessionCategory(session);
  const row = el("div", "side-item-row");
  row.classList.add(`cat-${category}`); // 左色条颜色（§4）
  if (String(session.id) === String(state.sessionId)) row.classList.add("is-on");

  const item = button("side-item", "", () => selectSession(session.id));
  item.append(el("span", "cat-dot")); // 会话行前的分类色点
  const body = el("span", "side-item-body");
  const head = el("span", "side-item-head");
  head.append(el("span", "side-item-title", session.title));
  const site = session.site || ""; // v6：站点徽章读接口字段，非空才显示
  if (site) {
    const badge = el("span", "side-site", site);
    badge.title = `站点：${site}`;
    head.append(badge);
  }
  body.append(head);
  const meta = el("span", "side-item-meta");
  // v34 T6（票 #219 / spec A2）：会话行的徽标与长摘要一并撤除 —— 待选句有自己的一级
  // 入口（顶栏），摘要回到「N 词」，不再让 94 个会话都渲染零计数的噪音。
  meta.append(el("span", null, `${session.records} 词`));
  meta.append(
    el("span", null, formatTime(session.last_captured_at || session.last_active_at))
  );
  body.append(meta);
  item.append(body);
  item.setAttribute(
    "aria-label",
    `${CATEGORY_LABEL[category]}会话「${session.title}」`
  );
  row.append(item);

  const confirming = String(state.confirmSession) === String(session.id);
  if (confirming) {
    row.classList.add("is-confirming");
    row.append(
      button("side-del is-armed", `删除 ${session.records} 词`, () =>
        deleteSession(session.id, session.title, session.records)
      )
    );
    row.append(
      button("side-del is-cancel", "取消", () => {
        state.confirmSession = null;
        renderSidebar();
      })
    );
  } else {
    const del = button("side-del", "×", () => {
      state.confirmSession = session.id;
      renderSidebar();
    });
    del.title = `删除会话「${session.title}」及其全部词条`;
    del.setAttribute("aria-label", `删除会话 ${session.title}`);
    row.append(del);
  }
  return row;
}

// 性能：侧栏是整页最大的一块 DOM（56 个会话 → 700+ 节点）。它的可见结果只由
// 「会话目录 / 折叠态 / 选中态 / 标签 / 路由」决定；列表筛选、搜索、分页时这些都不变，
// 以前每次 loadAll 都整块拆了重建（还有一次全量样式/布局）。输入签名没变就跳过，画面不变。
let sidebarRenderSignature = null;
function sidebarSignature() {
  const parts = [
    state.route,
    state.filter,
    state.sessionId,
    state.tag,
    state.confirmSession,
    // 会话行里的相对时间（今天 / 昨天）跨天要跟着变
    new Date().toDateString(),
  ];
  for (const session of state.sessions) {
    parts.push(
      session.id,
      session.records,
      session.site,
      session.account,
      session.account_id,
      session.title,
      session.last_captured_at,
      session.last_active_at,
      sessionCategory(session)
    );
  }
  // v22 P1b：文章分组的可见结果只由文章目录 / 选中文章决定（不参与会话筛选）
  parts.push("A", state.articleId, state.articles.length, state.articleTotal);
  for (const article of state.articles) {
    parts.push(
      article.id,
      article.title,
      article.segments,
      article.records,
      article.needs_review,
      article.last_captured_at,
      article.source_app
    );
  }
  const tags = state.summary ? state.summary.tags || [] : [];
  parts.push("T", tags.length);
  for (const item of tags) parts.push(item.tag, item.count);
  parts.push("F", JSON.stringify(state.fold));
  return parts.join("\u0001");
}

// v22 P1b（票 #166 / spec §8.1）：侧栏「文章」分组。篇卡 = ShortTitle 口径标题
// （接口已按 clean_title 归一）/ `N 段 · M 词` / 来源 / 最近录入；倒序由接口给。
// 独立列表：不参与会话筛选、搜索、标签（spec §8.1），所以只读 state.articles。
function articleSideItem(article) {
  const row = el("div", "side-item-row article-row");
  if (String(article.id) === String(state.articleId)) row.classList.add("is-on");
  const item = button("side-item", "", () => {
    window.location.hash = `#/article/${article.id}`;
  });
  const body = el("span", "side-item-body");
  const head = el("span", "side-item-head");
  const titleNode = el("span", "side-item-title", shortTitle(article.title));
  titleNode.title = article.title; // 截断后悬停仍能看到全名
  head.append(titleNode);
  if (article.source_app) {
    const badge = el("span", "side-site", article.source_app);
    badge.title = `来源：${article.source_app}`;
    head.append(badge);
  }
  body.append(head);
  const meta = el("span", "side-item-meta");
  meta.append(el("span", null, `${article.segments} 段 · ${article.records} 词`));
  if (Number(article.needs_review) > 0) {
    meta.append(el("span", "side-needs-review", `待复核 ${article.needs_review}`));
  }
  meta.append(el("span", null, formatTime(article.last_captured_at)));
  body.append(meta);
  item.append(body);
  item.setAttribute(
    "aria-label",
    `文章「${article.title}」，${article.segments} 段 ${article.records} 词`
  );
  row.append(item);
  return row;
}

function articlesSection() {
  const box = el("div", "side-section");
  const head = el("div", "side-head");
  head.append(el("span", "side-title", "文章"));
  if (state.articles.length) {
    head.append(el("span", "side-count", String(state.articleTotal || state.articles.length)));
  }
  box.append(head);
  if (!state.articles.length) {
    box.append(el("div", "side-empty", "还没有文章 —— 长文取词后会出现在这里"));
    return box;
  }
  const group = el("div", "side-group article-group");
  const body = el("div", "side-group-body");
  for (const article of state.articles) body.append(articleSideItem(article));
  group.append(body);
  box.append(group);
  return box;
}

function renderSidebar() {
  const signature = sidebarSignature();
  if (signature === sidebarRenderSignature) {
    syncShell(); // 侧栏可交互状态仍要与当前 shell 对齐
    return;
  }
  sidebarRenderSignature = signature;
  clear(dom.sidebar);

  // v22 P1b（spec §8.1）：文章分组在会话分组**上方**；独立列表，不参与会话筛选。
  dom.sidebar.append(articlesSection());

  const sessionsBox = el("div", "side-section");
  const sessionsHead = el("div", "side-head");
  sessionsHead.append(el("span", "side-title", "会话"));
  if (state.sessions.length) {
    sessionsHead.append(el("span", "side-count", String(state.sessions.length)));
    const tools = el("span", "side-tools");
    const expandAll = button("side-tool", "全部展开");
    expandAll.dataset.foldAll = "expand";
    const collapseAll = button("side-tool", "全部收起");
    collapseAll.dataset.foldAll = "collapse";
    tools.append(expandAll, collapseAll);
    sessionsHead.append(tools);
  }
  // v14 T3：覆盖式侧栏里的收起入口（键盘 [ / Esc 同样可收）。
  // 只有列表路由的侧栏才是覆盖层；复习 / 设置页的侧栏是普通目录，这个按钮在那儿是死控件。
  if (state.route === "list") {
    const close = button("side-close", "«", () => setSidebarOpen(false));
    close.title = "收起侧栏（[）";
    close.setAttribute("aria-label", "收起侧栏");
    sessionsHead.append(close);
  }
  sessionsBox.append(sessionsHead);
  if (!state.sessions.length) {
    sessionsBox.append(el("div", "side-empty", "还没有会话 —— 捕获第一个词就会出现"));
  }
  // v6 §Implementation·一期：会话区从平铺改成「站点组 → 账号组 → 会话行」三层树。
  // 只依赖 state.sessions（独立接口，不受搜索/筛选/标签过滤削减）—— 左栏永远是完整目录。
  for (const group of provenanceGroups(state.sessions)) {
    sessionsBox.append(siteGroupNode(group));
  }
  dom.sidebar.append(sessionsBox);

  const tagsBox = el("div", "side-section");
  const tags = state.summary ? state.summary.tags || [] : [];
  if (!tags.length) {
    const tagsHead = el("div", "side-head");
    tagsHead.append(el("span", "side-title", "标签"));
    tagsBox.append(tagsHead);
    tagsBox.append(el("div", "side-empty", "还没有标签"));
  } else {
    // 票 #87：标签全量渲染（接口不再截前 12），并复用会话树同款折叠组头 ——
    // 折叠态同样存 state.fold.tags、走同一份 CSS 两态与 localStorage 持久化。
    const collapsed = foldCollapsed("tags", "tags");
    const group = el("div", "side-group");
    group.dataset.group = "tags";
    if (collapsed) group.classList.add("is-collapsed");
    const head = groupHead("tags", "标签", tags.length, {});
    head.setAttribute("aria-expanded", collapsed ? "false" : "true");
    group.append(head);
    const body = el("div", "side-group-body");
    const cloud = el("div", "tag-cloud");
    for (const item of tags) {
      const value = String(item.tag || "").replace(/^#/, "");
      const chip = button("chip", `${item.tag} ${item.count}`, () => selectTag(value));
      if (state.tag === value) chip.classList.add("is-on");
      cloud.append(chip);
    }
    body.append(cloud);
    group.append(body);
    tagsBox.append(group);
  }
  dom.sidebar.append(tagsBox);
  // v14 T3：侧栏重建后把把手徽标 / inert / 遮罩重新对齐（会话数可能刚变）
  syncShell();
}

async function deleteSession(id, title, records) {
  state.confirmSession = null;
  try {
    await api(`/api/sessions/${id}`, { method: "DELETE" });
    if (String(state.sessionId) === String(id)) {
      state.sessionId = null;
      if (state.filter === "session") state.filter = "all";
    }
    toast(`已删除会话「${title}」（连带 ${records} 个词条）`);
    await loadAll();
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    // 旧版中枢没有这个路由：刷新出来的新前端会打到 404，提示重启而不是甩英文
    toast(
      message === "not found"
        ? "删除会话要新版中枢：请用托盘菜单「重启中枢」后再试"
        : `删除会话失败：${message}`
    );
  }
}

/* --- 渲染：主区（列表）------------------------------------------------------- */

function groupRecords() {
  const groups = [];
  const index = new Map();
  for (const record of state.records) {
    const key = record.session_id == null ? "none" : String(record.session_id);
    if (!index.has(key)) {
      const group = {
        key,
        title: sessionTitle(key),
        session: key === "none" ? null : sessionById(key),
        records: [],
      };
      index.set(key, group);
      groups.push(group);
    }
    index.get(key).records.push(record);
  }
  return groups;
}

// v7 §5.1：会话里 `context` 一模一样的词条并成一张卡 —— 一次截图记下一整段、在同段里
// 连记多个词时，那段只画一遍、每个词各自高亮（用户报障：一段记 5 个词 = 5 张卡、同段重复 5 遍）。
// `context` 为空的词条各占一张卡（没有原句可共，并起来只会把不相干的词堆一块）。
function contextCards(records) {
  const cards = [];
  const index = new Map();
  for (const record of records) {
    const context = String(record.context || "").trim();
    const key = context ? `ctx:${context}` : `id:${record.id}`;
    if (!index.has(key)) {
      const card = { key, context, records: [] };
      index.set(key, card);
      cards.push(card);
    }
    index.get(key).records.push(record);
  }
  return cards;
}

// j/k 走的是**卡**不是词条：一张卡只算一格，否则连按 j 会停在原地（同一张卡里的第二条）
// 性能：j/k / Enter / 搜索回车都会要这份顺序，一次数据只算一遍（数据换了才重算）。
let visibleOrderCache = { records: null, order: [] };
function visibleOrder() {
  if (visibleOrderCache.records === state.records) return visibleOrderCache.order;
  const order = groupRecords().flatMap((group) =>
    contextCards(group.records).map((card) => card.records[0])
  );
  visibleOrderCache = { records: state.records, order };
  return order;
}

function listTitle() {
  let title = "全部词条";
  if (state.filter === "due") title = "待复习";
  else if (state.filter === "today") title = "今天";
  else if (state.filter === "session" && state.sessionId !== null)
    title = `会话：${sessionTitle(String(state.sessionId))}`;
  return state.tag ? `${title} · #${state.tag}` : title;
}

function clearFilters() {
  state.query = "";
  dom.search.value = "";
  state.filter = "all";
  state.sessionId = null;
  state.tag = null;
  state.selectedId = null;
  loadAll({ skeleton: true });
}

// --- v19 T2（票 #153）：会话头三组内容抽成共用构建件 ------------------------
// standard 列表页的胶囊（listHeader）与沉浸态星图的右缘会话坞（orbitDock）各取
// 所需，避免双份维护。`.view-switch-btn` 等 class 原样保留（v8 验收口径）。
function readThroughButton() {
  // v14 T2：顺序通读入口
  const sequence = button("btn btn-subtle", "⏮ 顺序通读", startReadThrough);
  // v14 #121：>500 条时前端只载入最新 500 条，「从最早一段开始」只能到已加载子集
  sequence.title =
    "按收录时间从早到晚逐段读（不改复习到期日）" +
    (state.total > LIST_LIMIT ? ` · 仅载入最新 ${LIST_LIMIT} 条` : "");
  return sequence;
}

// v31：听看复习入口（当前筛选 / 当前会话）。只播高亮词条，不播整段原文。
function listenReviewButton() {
  const entry = button("btn btn-subtle review-entry-btn", "🎧 听看", () =>
    startSessionHighlightReview()
  );
  entry.title =
    "只播高亮词条（同句重叠按 词 → 短语 → 句；按最早收录顺序），每条配释义弹窗与发音";
  entry.setAttribute("aria-label", "听看复习当前范围内已高亮的词条（按最早收录顺序）");
  return entry;
}

function viewSwitchGroup() {
  // v8 星图：一屏看完本会话。切过一次就记住（setView 写 localStorage）。
  const toggle = el("div", "view-switch");
  toggle.setAttribute("role", "group");
  toggle.setAttribute("aria-label", "显示方式");
  for (const [key, label] of [
    ["orbit", "◈ 星图"],
    ["list", "☰ 列表"],
  ]) {
    const choice = button(
      `view-switch-btn${currentView() === key ? " is-on" : ""}`,
      label,
      () => setView(key)
    );
    choice.setAttribute("aria-pressed", currentView() === key ? "true" : "false");
    toggle.append(choice);
  }
  return toggle;
}

function sessionActionButtons() {
  // 动作层（复习本会话 / 标签 / 清除会话 / 清除筛选）。v14 T3 收进浮层；
  // 「清除会话」是破坏性动作，坞里默认折叠、点击展开（hover 弹出易误触）。
  const items = [];
  if (state.sessionId !== null) {
    // §8.2：从列表的会话一键进入「只复习这条视频的词」
    const session = state.sessions.find(
      (item) => String(item.id) === String(state.sessionId)
    );
    items.push(
      button(
        "btn btn-primary",
        `复习本会话${session ? `（${session.records} 词）` : ""}`,
        () => {
          window.location.hash = `#/review?session=${state.sessionId}`;
        }
      )
    );
  }
  if (state.tag) {
    items.push(button("chip is-on", `标签 #${state.tag} ×`, () => selectTag(null)));
  }
  if (state.sessionId !== null) {
    items.push(button("chip", "清除会话 ×", () => selectSession(null)));
  }
  if (state.filter !== "all" || state.tag || state.query.trim() || state.sessionId !== null) {
    items.push(button("btn btn-subtle", "清除筛选", clearFilters));
  }
  return items;
}

function listHeader() {
  const header = el("div", "list-header");
  header.id = "list-header";
  if (!state.shell.header) header.classList.add("is-collapsed");
  header.append(el("h2", "list-title", listTitle()));
  header.append(
    el("span", "list-sub", state.loading ? "加载中…" : `${state.total} 条`)
  );

  const right = el("div", "list-header-right");
  if (!state.loading && !state.error && state.records.length) {
    right.append(readThroughButton(), listenReviewButton(), viewSwitchGroup());
  }
  const actions = el("div", "list-header-actions");
  actions.append(...sessionActionButtons());
  right.append(actions);

  const more = button(
    "btn btn-subtle list-header-toggle",
    state.shell.header ? "▴" : "▾",
    () => setHeaderOpen(!state.shell.header)
  );
  more.title = `${state.shell.header ? "收起会话动作" : "展开会话动作"}（h）`;
  more.setAttribute(
    "aria-label",
    state.shell.header ? "收起会话动作" : "展开会话动作"
  );
  more.setAttribute("aria-expanded", state.shell.header ? "true" : "false");
  more.setAttribute("aria-controls", "list-header");

  header.append(right, more);
  return header;
}

// v19 T2（票 #153）：沉浸态星图的右缘会话坞。收起态 = 贴右缘的小把手（不占地）；
// hover 展开（peek）/ 移出收回，点击把手钉住（pinned）。开合态见 setDock / state.dock。
// 事件直接绑在新建元素上：renderMain 全量重建时旧元素连同监听一起丢弃，不会叠加。
function orbitDock() {
  const dock = el("div", "orbit-dock");
  dock.id = "orbit-dock";

  const handle = el("button", "dock-handle");
  handle.type = "button";
  handle.setAttribute("aria-expanded", state.dock !== "hidden" ? "true" : "false");
  handle.setAttribute("aria-controls", "orbit-dock-panel");
  handle.title = "会话坞：悬停展开 · 点击钉住";
  handle.append(
    el("span", "dock-handle-label", "会话"),
    el("span", "dock-handle-count", String(state.sessions.length || 0))
  );

  const panel = el("div", "dock-panel");
  panel.id = "orbit-dock-panel";
  const head = el("div", "dock-head");
  head.append(
    el("h2", "dock-title", listTitle()),
    el("span", "dock-sub", state.loading ? "加载中…" : `${state.total} 条`)
  );
  panel.append(head);

  if (!state.loading && !state.error && state.records.length) {
    const viewRow = el("div", "dock-view");
    viewRow.append(readThroughButton(), listenReviewButton(), viewSwitchGroup());
    panel.append(viewRow);
  }

  // 动作区：「动作 ⌄」分节头点击开合；grid 0fr⇄1fr 折叠动画（同 .orbit-playbar 惯例）
  const actionsHead = button(
    "dock-actions-head",
    "",
    () => setHeaderOpen(!state.shell.header)
  );
  actionsHead.setAttribute("aria-expanded", state.shell.header ? "true" : "false");
  actionsHead.setAttribute("aria-controls", "orbit-dock-actions");
  actionsHead.title = `${state.shell.header ? "收起会话动作" : "展开会话动作"}（h）`;
  actionsHead.append(
    el("span", "dock-actions-label", "动作"),
    el("span", "dock-chevron", "⌄")
  );
  const actionsBody = el("div", "dock-actions-body");
  actionsBody.id = "orbit-dock-actions";
  if (state.shell.header) actionsBody.classList.add("is-open");
  const actionsClip = el("div", "dock-actions-clip");
  const actionsInner = el("div", "dock-actions-inner");
  actionsInner.append(...sessionActionButtons());
  actionsClip.append(actionsInner);
  actionsBody.append(actionsClip);

  panel.append(actionsHead, actionsBody);

  const zone = bindHoverZone(
    handle,
    panel,
    () => setDock("peek"),
    () => {
      if (state.dock !== "pinned") setDock("hidden"); // 钉住不因移出而收回
    }
  );
  handle.addEventListener("click", () => {
    zone.disarm(); // 点击 = 明确指令，悬停计时器作废（防点击后被 re-peek 弹开）
    setDock(state.dock === "pinned" ? "hidden" : "pinned");
  });

  dock.append(handle, panel);
  return dock;
}

// 会话坞开合：只写 dataset 与把手 aria，不重绘主区（同 setSidebarOpen 的纪律 ——
// 星图舞台尺寸恒定，星子不重排）。钉住是比悬停预览更强的状态，不许被降级。
function setDock(mode) {
  if (state.dock === "pinned" && mode === "peek") return;
  state.dock = mode;
  document.body.dataset.dock = mode;
  const handle = document.querySelector(".orbit-dock .dock-handle");
  if (handle) {
    handle.setAttribute("aria-expanded", mode !== "hidden" ? "true" : "false");
  }
}

function ctxMark(record, label, segment) {
  // §5.1：目标词高亮为可聚焦按钮；v11 §1.1：悬停/聚焦显义、单击只播放
  const mark = el("button", "ctx-mark", label);
  mark.type = "button";
  mark.setAttribute("aria-expanded", "false");
  mark.setAttribute("aria-label", `「${record.target}」：悬停看释义，点击播放`);
  mark.__records = [record]; // 悬停 / 聚焦时从这里取释义（不给每个词挂一套监听）
  // v27 §3.1：段 span 与覆盖词条的 span 一起挂在元素上，点击 / 预取经同一 helper 算文本
  mark.__segment = segment ? { start: segment.start, end: segment.end } : null;
  mark.__spans = segment ? segment.coveringSpans : [];
  mark.addEventListener("click", (event) => {
    event.stopPropagation(); // 不触发卡片抽屉，也不触发全局「点别处关闭」
    if (hasSelection()) return; // v15 C3：拖选松手落在高亮词上不播词
    // v11 §1.1：单击只负责播放音频，不再切换 popover —— 开关归悬停 / 聚焦。
    // 触屏、脚本、辅助技术点进来时 popover 可能没开：先显义再播放；已开就保持不动。
    if (popoverAnchor !== mark) openPopover(mark, [record]);
    pronounce(playbackTextForSegment(mark), mark);
  });
  return mark;
}

// v7 §5.1：两个目标词叠在同一段字上（`scandal` 落在 `fraud scandal` 里面这种）——
// 换一种底色标出来；点击一次把重叠的几条释义**全**列出来（否则被压住的那条没入口）。
// v27 §3.1：碎片段点击播覆盖词条里 span 最短者的完整 target；popover 逐条 ▶ 补精确入口。
function overlapMark(records, label, segment) {
  const mark = el("button", "ctx-mark is-overlap", label);
  mark.type = "button";
  mark.setAttribute("aria-expanded", "false");
  const names = records.map((record) => `「${record.target}」`).join("、");
  mark.setAttribute(
    "aria-label",
    `重叠高亮 ${names}：悬停看释义，点击播放最短覆盖词，弹层可逐条播放`
  );
  mark.title = `这里高亮重叠：${records
    .map((record) => record.target)
    .join(" / ")} · 点击播最短覆盖词，悬停任一条可单独播放`;
  mark.__records = records; // 悬停时列出全部释义
  mark.__segment = segment ? { start: segment.start, end: segment.end } : null;
  mark.__spans = segment ? segment.coveringSpans : [];
  mark.addEventListener("click", (event) => {
    event.stopPropagation();
    if (hasSelection()) return; // v15 C3：拖选松手落在高亮词上不播词
    if (popoverAnchor !== mark) openPopover(mark, records);
    pronounce(playbackTextForSegment(mark), mark); // 段=整条词条播段文本；碎片播最短覆盖词条
  });
  return mark;
}

// v7 §5.1：一张卡 = 一段原句 + 落在这段上的全部目标词。
// `located` = 句内找得到落点的那些（带 [start,end)）；`header` = 头部要列的全部词，
// 按「段里出现的先后」排，句内找不到的排最后（它们只能靠头部那枚词当入口）。
function cardLayout(card) {
  if (card.layout) return card.layout; // 性能：同一张卡（如星图阅读页）只算一遍
  const context = String(card.context || "").trim();
  const located = [];
  const unlocated = [];
  for (const record of card.records) {
    const span = context ? targetSpanForRecord(context, record) : null;
    if (span && span.end > span.start) located.push({ record, span });
    else unlocated.push(record);
  }
  located.sort((a, b) => a.span.start - b.span.start);
  card.layout = {
    context,
    located,
    header: [...located.map((item) => item.record), ...unlocated],
  };
  return card.layout;
}

// 原句按「高亮端点」切成小段：每段要么是纯文本、要么被 1 条盖住（普通高亮）、
// 要么被 ≥2 条盖住（重叠高亮）。端点取所有 span 的两头，所以每段要么整段在某个
// span 里、要么整段在外 —— 不会把一条高亮切成半截。首尾端点外的字原样保留。
// v27 §3.1：additive 带上 `start` / `end` / `coveringSpans` —— 播放文本要判断
//「段 span 是否等于某条覆盖词条的 span」；老消费方只读 text / records，不受影响。
function sentenceSegments(context, located) {
  const cuts = [...new Set(located.flatMap((item) => [item.span.start, item.span.end]))]
    .sort((a, b) => a - b);
  const segments = [];
  for (let index = 0; index + 1 < cuts.length; index += 1) {
    const start = cuts[index];
    const end = cuts[index + 1];
    const covering = located.filter((item) => item.span.start <= start && end <= item.span.end);
    segments.push({
      text: context.slice(start, end),
      records: covering.map((item) => item.record),
      start,
      end,
      coveringSpans: covering.map((item) => ({ record: item.record, span: item.span })),
    });
  }
  if (!segments.length) return segments;
  const head = context.slice(0, cuts[0]);
  if (head)
    segments.unshift({ text: head, records: [], start: 0, end: cuts[0], coveringSpans: [] });
  const tail = context.slice(cuts[cuts.length - 1]);
  if (tail)
    segments.push({
      text: tail,
      records: [],
      start: cuts[cuts.length - 1],
      end: context.length,
      coveringSpans: [],
    });
  return segments;
}

// v27 §3.1：点击 / 预取的播放文本。段 span 正好等于某条覆盖词条的 span → 播段文本
//（所见即所播：want 段播 want、词干匹配的 running 段播 running）；段只是碎片 → 播覆盖
// 词条中 span 最短者的完整 target（平手取 target 文本更短者）。缺 __spans（卡片头部
// 那枚裸词等）回退段文本。
// `start` = 播放文本在段/原句坐标里的起点：段 = 词条 span 时就是段起点；碎片段是所选
// 词条 span 的起点。粉笔进度必须从 `start` 起笔，不能从碎片段起点起笔（否则长 target
// 会把绿笔划到后面的字上 —— 2026-11-01 用户反馈）。
function playbackForSegment(mark) {
  const label = String(mark.textContent == null ? "" : mark.textContent);
  const spans = Array.isArray(mark.__spans) ? mark.__spans : [];
  const segment = mark.__segment;
  if (!spans.length || !segment) return { text: label, start: null };
  let best = null;
  let bestTarget = "";
  for (const item of spans) {
    const span = item && item.span;
    if (!span || !(span.start <= segment.start && segment.end <= span.end)) continue;
    if (span.start === segment.start && span.end === segment.end) {
      return { text: label, start: segment.start };
    }
    const target = String((item.record && item.record.target) || "").trim();
    if (!target) continue;
    if (!best) {
      best = item;
      bestTarget = target;
      continue;
    }
    const width = span.end - span.start;
    const bestWidth = best.span.end - best.span.start;
    if (width < bestWidth || (width === bestWidth && target.length < bestTarget.length)) {
      best = item;
      bestTarget = target;
    }
  }
  if (!best) return { text: label, start: null };
  return { text: bestTarget, start: best.span.start };
}

function playbackTextForSegment(mark) {
  return playbackForSegment(mark).text;
}

// §5.1（v6/v7）：主体 = 原句，命中处替换为高亮词；一段只画一遍。
// v11 §1：原句自己也是一个发音入口 —— click / Enter / Space 播整段，且不冒泡开抽屉。
function sentenceNode(context, located) {
  const sentence = el("p", "row-sentence");
  sentence.tabIndex = 0;
  sentence.setAttribute("role", "button");
  sentence.setAttribute("aria-label", "播放整段原句发音");
  sentence.title = "点原句听发音 · 点头部看详情";
  const activate = (event) => {
    event.preventDefault();
    event.stopPropagation(); // 卡片 row.click / 全局 Enter 都不接管
    if (hasSelection()) return; // v15 C3：拖选松手后的 click 不播整段
    // v15 C1：点原句 = 播放 / 暂停 / 继续（同 owner 同文本）；整段仍按句切分（#114）
    togglePlayback(sentence, context);
  };
  // v16 T4（C4 / §5.9 D5）：鼠标单击**延迟**起播 —— 双击的第一击不再先播整句。
  // 键盘 Enter / Space 仍走 activate()，保持即时（键盘没有双击语义）。
  sentence.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (event.detail >= 2) {
      cancelSentenceClickPlay(); // 双击 / 三击：取消第一击排下的起播
      return;
    }
    if (hasSelection()) return;
    // detail === 0 是程序化 click（辅助技术 / 脚本）：没有双击语义，不必延迟
    // 正在播这一句时点它 = 暂停 / 继续：也不延迟，否则「点一下没反应」
    if (event.detail <= 0 || isActiveSentencePlayback(sentence, context)) {
      togglePlayback(sentence, context);
      return;
    }
    scheduleSentenceClickPlay(sentence, context);
  });
  sentence.addEventListener("keydown", (event) => {
    // v16 T4（C4 #1）：Shift+Enter 是「朗读选中 / 重播快照」，归 document 级入口；这里放行
    if (event.shiftKey && event.key === "Enter") return;
    const isSpace = event.key === " " || event.key === "Spacebar";
    // v31：复习中 Space 归复习条（暂停/继续），不能被元素级处理器抢成整句播放。
    if (isSpace && highlightReview) {
      event.preventDefault();
      event.stopPropagation();
      reviewTogglePause();
      return;
    }
    // Space 归原句自己（列表/阅读页都不做「Space 揭示背面」）；其余键不拦
    if (event.key === "Enter" || isSpace) activate(event);
  });
  // v16 T4（C4 #2）：双击选词 → 只出浮标，不播（播放由用户点浮标决定）。
  // 走与拖选 / 兜底同一处归属判定；双击落在词上时选区就在这句里。
  sentence.addEventListener("dblclick", (event) => {
    event.stopPropagation();
    cancelSentenceClickPlay();
    // 双击的选区就在这句里；万一选区被判成跨句（少见），C5 提示也已由 showSelectionForHit 发出
    showSelectionForHit(resolveSelectionTarget());
  });
  // v16 T4（C4 #3）：右键菜单（键盘 ContextMenu 键也走 contextmenu 事件）
  sentence.addEventListener("contextmenu", (event) => openSentenceMenu(event, sentence, context));
  // v13 §5（P1-2）：按下 / 键盘聚焦就先取「这次点击会播的首片」，点击时尽量直接命中缓存。
  // 落在高亮词上的按下归词自己（它播的是那个词，不是整段）：否则会白取一次原句首片。
  const intent = (event) => {
    if (markFromEvent(event)) return;
    prefetchIntent(context);
  };
  sentence.addEventListener("pointerdown", intent, { passive: true });
  sentence.addEventListener("focusin", intent);
  // v15 C3：拖选守卫 —— 记下按下位置，位移 > 6px 视为拖选（document 级统一判定，
  // 拖选期间关掉悬停显义；松手后不清选区，只在选区末端浮出「▶ 朗读选中」）
  sentence.addEventListener("pointerdown", (event) => beginSentenceDrag(sentence, event));
  const segments = sentenceSegments(context, located);
  if (!segments.length) {
    sentence.append(document.createTextNode(context)); // 一条都落不上：原句照常显示
    return sentence;
  }
  for (const segment of segments) {
    if (!segment.records.length) sentence.append(document.createTextNode(segment.text));
    else if (segment.records.length === 1)
      sentence.append(ctxMark(segment.records[0], segment.text, segment));
    else sentence.append(overlapMark(segment.records, segment.text, segment));
  }
  return sentence;
}

// 卡片头部那枚词：句内找得到 → 纯词标签（入口在句内高亮词）；找不到 / 无原句 → 词本身
// 即入口。每条词条在一张卡里只出现一次，且恰好有一个「点击看释义」入口（§5.1 回退链）。
function targetLabel(record, located) {
  if (located) {
    const node = el("span", "row-target", record.target);
    if (KIND_LABEL[record.kind]) node.classList.add(`kind-${record.kind}`);
    return node;
  }
  return ctxMark(record, record.target);
}

function cardRow(card) {
  const layout = cardLayout(card);
  const primary = card.records[0]; // records 按 -captured_at 排：头部时刻 / 抽屉锚点取最新那条
  const row = el("div", "row");
  const kind = KIND_LABEL[primary.kind] ? primary.kind : null;
  if (kind) row.classList.add(`kind-${kind}`); // §5：行首 2px 色条（取最新那条的类型）
  row.dataset.id = String(primary.id);
  // v31 #196：静默重拉后按词条 id 找回「这条记录所在的那一行」，只替换受影响的行。
  row.dataset.recordIds = card.records.map((record) => String(record.id)).join(" ");
  row.tabIndex = 0;
  row.setAttribute("role", "button");
  // 颜色不是唯一信号：aria-label / title 里带上「词/短语/句」（§5）
  row.setAttribute(
    "aria-label",
    card.records.length > 1
      ? `这一段的 ${card.records.length} 个词条的详情`
      : kind
        ? `${KIND_LABEL[kind]}「${primary.target}」的详情`
        : `${primary.target} 的详情`
  );
  if (card.records.length > 1) row.title = `这一段里记了 ${card.records.length} 个词条`;
  else if (kind) row.title = `词条类型：${KIND_LABEL[kind]}`;
  if (primary.id === state.selectedId) row.classList.add("is-active");

  // §5.1（v6）：时间线锚点 —— 本段的全部目标词 + 收录时刻 + 徽标（徽标靠右）
  const top = el("div", "row-top");
  const targets = el("span", "row-targets");
  const locatedIds = new Set(layout.located.map((item) => item.record.id));
  for (const record of layout.header)
    targets.append(targetLabel(record, locatedIds.has(record.id)));
  top.append(targets);
  top.append(el("span", "row-time", formatTime(primary.captured_at)));

  const badges = el("span", "row-badges");
  // v7：并了多条时不能把 hits 相加 —— 一段里记 15 个新词会显示成 `×15`，读起来像
  // 「这段被记了 15 次」。取「重复最多的那个词」的次数（单条卡与 v6 的语义一致），
  // 到底是哪个词重复交给 title。
  const maxHits = card.records.reduce((max, record) => Math.max(max, Number(record.hits) || 0), 0);
  if (maxHits > 1) {
    const badge = el("span", "badge", `×${maxHits}`);
    badge.title = card.records
      .filter((record) => Number(record.hits) > 1)
      .map((record) => `${record.target} ×${record.hits}`)
      .join("、");
    badges.append(badge);
  }
  if (card.records.some(isDue)) badges.append(el("span", "badge is-due", "待复习"));
  for (const tag of [...new Set(card.records.flatMap(tagList))])
    badges.append(el("span", "badge is-tag", tag));
  if (badges.childNodes.length) top.append(badges);
  row.append(top);

  if (layout.context) row.append(sentenceNode(layout.context, layout.located));

  row.addEventListener("click", () => openDrawer(primary.id));
  row.addEventListener("keydown", (event) => {
    // role="button" 的约定：Enter（全局键位）与 Space 都要能激活
    // 高亮词按钮自己的 Space/Enter 归它自己（keydown 会冒泡到这里）
    if (event.target !== row) return;
    if (event.key === " " || event.key === "Spacebar") {
      event.preventDefault();
      openDrawer(primary.id);
    }
  });
  row.addEventListener("focus", () => {
    state.selectedId = primary.id;
    highlightRow();
  });
  return row;
}

function highlightRow() {
  for (const node of dom.main.querySelectorAll(".row")) {
    node.classList.toggle("is-active", node.dataset.id === String(state.selectedId));
  }
}

/* --- §5.2 释义 popover（单例，挂在 body；锚定高亮词下方，越界翻上）------------ */

let popover = null; // 复用的 DOM 节点
let popoverAnchor = null; // 当前锚定的高亮词按钮（null = 未打开）

// v11 §1.1（票 #111）：悬停显义控制器 —— 列表 / 阅读页**一个**事件委托 + **一对**计时器。
// 不给每个高亮词各挂 pointerenter/pointerleave：一段十几条词条，那样会挂出十几套监听。
const HOVER_OPEN_MS = 150; // 指针停在词上的意图延迟（§1.1）
const HOVER_CLOSE_MS = 200; // 移开后的宽限：够把指针从词挪进 popover
let hoverOpenTimer = 0;
let hoverCloseTimer = 0;
let hoverMark = null; // 指针当前停着的那枚高亮词（null = 已经移开）
let popoverHovered = false; // 指针是否正停在 popover 上（进入 = 取消收起）

function isPopoverOpen() {
  return popoverAnchor !== null;
}

function ensurePopover() {
  if (!popover) {
    popover = el("div", "ctx-popover");
    popover.setAttribute("role", "dialog");
    popover.addEventListener("click", (event) => event.stopPropagation());
    bindPopoverHover(popover);
    bindPopoverKeys(popover); // W7（票 #189）：Esc / Shift+Tab 与「焦点离开就收」
    document.body.append(popover);
  }
  return popover;
}

function closePopover() {
  clearHoverTimers(); // 计时器跟着 popover 一起收：旧计时器不许把已经离开的 popover 又打开
  popoverHovered = false;
  if (popoverAnchor) popoverAnchor.setAttribute("aria-expanded", "false");
  popoverAnchor = null;
  if (popover) {
    popover.classList.remove("is-open", "is-above", "is-review", "is-closing");
    clear(popover);
  }
}

// 触屏 / 无 hover 设备：不启用悬停计时（点一下高亮词 = 显义 + 播放，点别处收起）
function isHoverNone() {
  return window.matchMedia("(hover: none)").matches;
}

// 路由切换、重渲染、closePopover()、滚动 / resize、Esc 都走这里：
// 计时器一律清掉，免得旧计时器把已经离开的 popover 又打开。
function clearHoverTimers() {
  if (hoverOpenTimer) {
    window.clearTimeout(hoverOpenTimer);
    hoverOpenTimer = 0;
  }
  if (hoverCloseTimer) {
    window.clearTimeout(hoverCloseTimer);
    hoverCloseTimer = 0;
  }
  hoverMark = null;
}

function cancelHoverClose() {
  if (!hoverCloseTimer) return;
  window.clearTimeout(hoverCloseTimer);
  hoverCloseTimer = 0;
}

function scheduleHoverOpen(mark) {
  cancelHoverClose(); // A → B：直接换内容，不经过「先收起再打开」的闪动
  if (hoverOpenTimer) window.clearTimeout(hoverOpenTimer);
  hoverMark = mark;
  hoverOpenTimer = window.setTimeout(() => {
    hoverOpenTimer = 0;
    if (hoverMark !== mark || !mark.isConnected) return; // 已经移开 / 行重建掉了
    // §3：悬停 150ms 即预取这次点击会播的文字（高）；移开不取消已发出的请求
    // v27 §3.3：预取键 = 播放键 —— 与点击走同一 helper（碎片段取整条 target）
    prefetch(playbackTextForSegment(mark));
    if (popoverAnchor === mark) return; // 已经为它开着，不重建
    openPopover(mark, mark.__records || []);
  }, HOVER_OPEN_MS);
}

function scheduleHoverClose() {
  if (hoverOpenTimer) {
    window.clearTimeout(hoverOpenTimer);
    hoverOpenTimer = 0;
  }
  hoverMark = null;
  if (hoverCloseTimer) return; // 已经在宽限里：等它到点
  hoverCloseTimer = window.setTimeout(() => {
    hoverCloseTimer = 0;
    if (popoverHovered) return; // 指针已经走到 popover 里
    closePopover();
  }, HOVER_CLOSE_MS);
}

function markFromEvent(event) {
  if (!(event.target instanceof Element)) return null;
  return event.target.closest(".ctx-mark");
}

// 列表 / 阅读页容器上的事件委托：只命中 `.ctx-mark`。原句正文、卡片头部非词区域、
// 复习卡、星图浮卡都不是 `.ctx-mark`，天然不参与。
dom.main.addEventListener("pointerover", (event) => {
  if (isHoverNone()) return;
  if (isDragSelecting()) return; // v15 C3：拖选中不弹悬停显义（会盖住选区终点、吃掉 mouseup）
  const mark = markFromEvent(event);
  if (!mark) return;
  if (event.relatedTarget instanceof Node && mark.contains(event.relatedTarget)) return;
  scheduleHoverOpen(mark);
});

dom.main.addEventListener("pointerout", (event) => {
  if (isHoverNone()) return;
  const mark = markFromEvent(event);
  if (!mark) return;
  if (event.relatedTarget instanceof Node && mark.contains(event.relatedTarget)) return;
  scheduleHoverClose();
});

// v13 §5（P1-2）：按下高亮词就先取这次点击会播的那段文字（= `label`）。
// v27 §3.3：文本改经 `playbackTextForSegment`，并整段交给 `prefetch` —— 预取键必须
// 等于点击的播放键（`pronounce` 单词模式也是一次整条请求，不再只取首片）。
// 去重靠 `prefetch` 自己的三重判断：悬停已经取过 / 正在途中的不会再发一次。
function primeMarkIntent(event) {
  const mark = markFromEvent(event);
  if (mark) prefetch(playbackTextForSegment(mark));
}
dom.main.addEventListener("pointerdown", primeMarkIntent, { passive: true });

// popover 自己也是一块「悬停区」：指针移进来取消收起，移出去再按 200ms 宽限收起。
// 用显式标记而不是 `:hover`：popover 换词时内容会重建，浏览器缓存的 hover 链可能是旧的。
function bindPopoverHover(node) {
  node.addEventListener("pointerover", () => {
    popoverHovered = true;
    cancelHoverClose();
  });
  node.addEventListener("pointerout", (event) => {
    if (node.contains(event.relatedTarget)) return;
    popoverHovered = false;
    scheduleHoverClose();
  });
}

// W7（票 #189）：popover 里的「详情 / 补」键盘可达。popover 挂在 body 上，不在自然
// Tab 链里（原注释自认「不保证 Tab 得进去」），所以进出都显式带：
//   词上 Tab → 进第一个动作按钮；Shift+Tab 在第一个按钮上 → 回锚词（popover 保持开）；
//   最后一个按钮 Tab → 放行去页面下一处（focusout 收 popover）；Esc → 收起并归位锚词。
function popoverActions() {
  if (!popover) return [];
  return Array.from(popover.querySelectorAll("button")).filter(
    (node) => !node.disabled
  );
}

function bindPopoverKeys(node) {
  node.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault(); // 全局 Esc 先看 defaultPrevented：这里只收 popover 这一层
      // 先把焦点送回锚词再收：此刻 popover 还开着，锚词的 focusin 命中「已为它开着」
      // 分支不会重建；收完焦点正好落在词上（而不是跌回 body）。
      const anchor = popoverAnchor;
      if (anchor) anchor.focus({ preventScroll: true });
      closePopover();
      return;
    }
    if (event.key !== "Tab") return;
    const actions = popoverActions();
    if (!actions.length) return;
    if (!event.shiftKey) return; // 按钮之间的 Tab 交给自然顺序（最后一个放行去页面）
    if (document.activeElement !== actions[0]) return; // 其余 Shift+Tab 同样自然回退
    event.preventDefault(); // 第一个按钮 Shift+Tab = 回锚词
    if (popoverAnchor) popoverAnchor.focus({ preventScroll: true });
  });
  // 焦点离开 popover（Tab 去了页面别处）→ 收起；回锚词上时保留（词的 focusin 接得住）。
  node.addEventListener("focusout", (event) => {
    // 收起过程自己会再发一次 focusout：closePopover 摘 DOM 时，Chrome 对其中持有
    // 焦点的动作按钮同步跑 focus fixup（rel=null），此刻外层已把 popoverAnchor 置
    // null —— 不拦就会重入 closePopover，内层 clear 抢走外层 removeChild 正在摘的
    // 节点，抛 NotFoundError 把 openDrawer 拦腰打断（用户真机验收：点 popover 的
    // 「详情」抽屉打不开，orbit / 列表两处同崩）。已不在打开态的 focusout 一律
    // 视作收起的副产品，交给进行中的那次收起。
    if (!isPopoverOpen()) return;
    if (event.relatedTarget instanceof Node) {
      if (node.contains(event.relatedTarget)) return;
      if (popoverAnchor && popoverAnchor.contains(event.relatedTarget)) return;
    }
    closePopover();
  });
}

// 键盘：focus 立即显义、失焦立即收起（不等 150 / 200ms）。Enter / Space 由按钮原生
// click 激活，沿用 #109 的 pronounce；popover 挂在 body 上不在 Tab 链里，Tab 的进出
// 由上面的 W7 委托显式带（词上 Tab 进 popover，Esc / Shift+Tab 回词上）。
dom.main.addEventListener("focusin", (event) => {
  const mark = markFromEvent(event);
  if (!mark) return;
  prefetch(playbackTextForSegment(mark)); // v13 §5 / v27 §3.3：聚焦预取 = 点击播放文本（整条）
  clearHoverTimers();
  if (popoverAnchor === mark) return;
  openPopover(mark, mark.__records || []);
});

dom.main.addEventListener("focusout", (event) => {
  const mark = markFromEvent(event);
  if (!mark) return;
  // 焦点移进 popover（点里面的「详情」）时不收：否则按钮会在 click 之前被摘掉
  if (popover && event.relatedTarget instanceof Node && popover.contains(event.relatedTarget)) return;
  closePopover(); // 内部会清计时器
});

// W7（票 #189）：焦点在锚词上时 Tab 进 popover 的第一个动作按钮（详情 / 补）——
// popover 挂在 body 上不在自然 Tab 链里，不拦这次 Tab 焦点就直接落到列表下一处去了。
dom.main.addEventListener("keydown", (event) => {
  if (event.key !== "Tab" || event.shiftKey) return;
  const mark = markFromEvent(event);
  if (!mark || popoverAnchor !== mark) return;
  const actions = popoverActions();
  if (!actions.length) return;
  event.preventDefault();
  actions[0].focus({ preventScroll: true });
});

function positionPopover(anchor, node) {
  node.style.top = "0px";
  node.style.left = "0px";
  const rect = anchor.getBoundingClientRect();
  const size = node.getBoundingClientRect();
  let top = rect.bottom + 6;
  const above = top + size.height > window.innerHeight - 4;
  if (above) top = rect.top - 6 - size.height;
  node.classList.toggle("is-above", above);
  if (top < 4) top = 4;
  const left = Math.max(
    6,
    Math.min(rect.left + rect.width / 2 - size.width / 2, window.innerWidth - size.width - 6)
  );
  node.style.top = `${Math.round(top)}px`;
  node.style.left = `${Math.round(left)}px`;
}

// v7 §5.2：popover 收一个**词条数组** —— 普通高亮传一条，重叠高亮传盖住这一段的那几条。
// v31：`options.review` = 听看复习的自动弹窗（只影响入场动画的 class，内容/交互不变）。
function openPopover(anchor, records, options = {}) {
  closePopover(); // 单例
  const node = ensurePopover();
  const review = Boolean(options && options.review);
  const list = Array.isArray(records) ? records : [records];
  for (const record of list) {
    const item = el("div", "ctx-pop-item");
    // 多条重叠时先点名，否则读不出下面两行释义各是谁的
    if (list.length > 1) item.append(el("div", "ctx-pop-target", record.target));
    if (record.phonetic) item.append(el("div", "ctx-pop-phonetic", record.phonetic));
    const meaning = el("div", "ctx-pop-meaning", meaningLabel(record, "（暂无释义）"));
    if (!record.meaning) meaning.classList.add("is-empty");
    item.append(meaning);
    const machineBadge = meaningMachineBadge(record); // #172：机翻小标
    if (machineBadge) item.append(machineBadge);
    // v23 P1：空释义后给「补」；和「详情」同一行（.ctx-pop-actions）
    const actions = el("div", "ctx-pop-actions");
    // v27 §3.2：重叠段里每条词条一个「播整条」入口 —— 被压住的最长条也能精确播放
    const play = button("ctx-pop-play", "▶", (event) => {
      event.stopPropagation(); // 点 ▶ 不冒泡（悬停 popover 不因此收起）
      pronounce(record.target, anchor);
    });
    play.title = `播放「${record.target}」`;
    play.setAttribute("aria-label", `播放「${record.target}」`);
    actions.append(play);
    if (!record.meaning) actions.append(refillInlineButton(record));
    else actions.append(recheckInlineButton(record));
    // 一张卡里可能并了十来条，卡片点击只开得了最新那条的抽屉 —— 每条给一个自己的入口
    actions.append(button("ctx-pop-more", "详情", () => openDrawer(record.id)));
    item.append(actions);
    node.append(item);
  }
  // v31：先落 is-review，再重新挂 is-open —— 入场动画每次都从头播（同一条 DOM 复用）。
  node.classList.toggle("is-review", review);
  node.classList.remove("is-open");
  void node.offsetWidth;
  node.classList.add("is-open");
  popoverAnchor = anchor;
  anchor.setAttribute("aria-expanded", "true");
  positionPopover(anchor, node);
}

function groupedList() {
  // v6：不再套 .card —— 词条自己就是白卡，套壳会变成白卡叠白卡
  const box = el("div", "groups");
  for (const group of groupRecords()) {
    const head = el("div", "group-head");
    // 分组标题前的来源色点：主区也能一眼看出会话类型（侧栏同一套 7 类色）
    if (group.session) {
      head.classList.add(`cat-${sessionCategory(group.session)}`);
      head.append(el("span", "cat-dot"));
    }
    head.append(el("span", "group-title", group.title));
    head.append(el("span", "group-count", `${group.records.length} 词`));
    // §2.6：会话分组标题旁的「打开原视频」，只在会话有 url 时出现
    if (group.session && group.session.url)
      head.append(videoLink(group.session.url, null, group.session.site));
    box.append(head);
    const rows = el("div", "rows");
    for (const card of contextCards(group.records)) rows.append(cardRow(card));
    box.append(rows);
  }
  return box;
}

/* --- v8 星图：本会话的 3D 星盘 -------------------------------------------------
 *
 * 起因：本会话几十条词条在列表里只能一条条往下滚，看得到字、看不到全貌。
 * 做法：把每张「段卡」（contextCards 的单位，一段原句 + 落在上面的词）铺在一张
 *       缓慢自转的星盘上；JS 每帧做一次透视投影，只写 transform / opacity /
 *       z-index，画布画星尘与轨道环。点卡片 → 飞到正中展开成「阅读页」
 *       （完整原句 + 释义 + 动作），Esc / 点空白回星图。
 *
 * 大段原句：星图上只留两行摘要（末尾渐隐）+「长句 · N 字」标记；阅读页里按字号
 *          阶梯自适应，仍放不下才在页内滚动（页本身不滚，面板高度不变）。
 *
 * 纪律照旧：零外部资源、无 innerHTML、文字全走 DOM（可选中、可读屏）。
 */

const ORBIT_LIMIT = 80; // 星图最多铺 80 段（再多 DOM 与观感都撑不住，请在列表里看）
const VIEW_KEY = "workbench-view";
// v14 T3（票 #124）：沉浸外壳偏好。首次无存储 → 星图全幅 + 侧栏/会话头收纳。
const SHELL_KEY = "workbench-shell";
const SHELL_DEFAULTS = { immersive: true, sidebar: false, header: false };
const ORBIT_GOLDEN = 2.399963229728653; // 黄金角：卡片在盘面上疏密均匀
const ORBIT_FLATTEN = 0.74; // 盘面纵向压扁（星系形，也把舞台的宽高比用满）
const ORBIT_TILT = 0.58; // 初始俯角（rad）；上下拖动可从近乎平视压到俯视
const ORBIT_FOCAL = 2100; // 焦距 = 机位；两者比值决定透视强度
const ORBIT_SPIN = 0.032; // 自转角速度（rad/s，约三分半一圈）
// W16（票 #189）：layoutPositions 的 O(n²) 松弛轮次。首跑 / 稳态全轮次定版；
// 拖拽窗口（ResizeObserver 连续回调）降到降档轮次保帧率，收敛后补一次全轮次。
const ORBIT_ROUNDS = 160;
const ORBIT_ROUNDS_DRAG = 16;
const ORBIT_RESIZE_SETTLE_MS = 240; // 两次 RO 回调间隔小于它 = 还在拖拽中
const ORBIT_HINTS =
  "编号=收录先后（越小越早）· 拖动旋转 · 上下拖改俯角 · 滚轮缩放 · 点卡片聚焦 · ←/→ 换一张 · s 从最早一段 · 双击复位";

// v15 T2（票 #127）：阅读页纸面滚轮换段 —— 手势锁与动量抑制（ADR 0007）。
// 同手势 = 滚动事件间隔 < 180ms；像素模式（deltaMode 0）累计 |deltaY| ≥ 40、
// 行模式（deltaMode 1）累计 ≥ 3 行才判换段意图；触发后 180ms 内忽略后续滚轮。
// 「一次手势最多换一段」是硬要求，下面四个阈值只是手感参数（真机可调）。
const WHEEL_GESTURE_GAP_MS = 180;
const WHEEL_LOCK_MS = 180;
const WHEEL_STEP_PX = 40;
const WHEEL_STEP_LINES = 3;

// v15 T2（票 #127）：换段的内容层动画 —— 只动 opacity / transform，纸面尺寸与位置不动。
const READ_FADE_OUT_MS = 80; // 旧内容淡出（滚轮换段先收再换）
const READ_FADE_IN_MS = 140; // 新内容淡入 + 上移 8px 落位

// 画布用的颜色：跟 CSS 主题走（深色是暖炭星野，浅色是纸色天光）
const ORBIT_PALETTE = {
  light: {
    dust: "rgba(122, 96, 60, 0.34)",
    ember: "rgba(180, 66, 45, 0.3)",
    ring: "rgba(122, 96, 60, 0.12)",
    ringHot: "rgba(180, 66, 45, 0.16)",
    link: "rgba(150, 112, 66, 0.3)",
    linkHot: "rgba(180, 66, 45, 0.6)",
  },
  dark: {
    dust: "rgba(236, 230, 218, 0.4)",
    ember: "rgba(217, 112, 90, 0.55)",
    ring: "rgba(236, 230, 218, 0.07)",
    ringHot: "rgba(217, 112, 90, 0.14)",
    link: "rgba(236, 230, 218, 0.18)",
    linkHot: "rgba(217, 112, 90, 0.8)",
  },
};

// 确定性伪随机（不用 Math.random：同一份数据每次进来星盘都长一样）
function orbitRand(seed) {
  const value = Math.sin(seed * 12.9898) * 43758.5453;
  return value - Math.floor(value);
}

function orbitClamp(value, min, max) {
  return value < min ? min : value > max ? max : value;
}

function readView() {
  try {
    const saved = window.localStorage.getItem(VIEW_KEY);
    return saved === "list" || saved === "orbit" ? saved : null;
  } catch (error) {
    return null; // 隐私模式：本会话内仍然可切
  }
}

/* v14 T3（票 #124）：沉浸外壳状态。与 workbench-view 并列，逐字段布尔归一；
   坏值 / 缺键 / localStorage 不可用一律回落 SHELL_DEFAULTS（照 readView 的纪律）。 */
function readShell() {
  let saved = null;
  try {
    saved = JSON.parse(window.localStorage.getItem(SHELL_KEY) || "null");
  } catch (error) {
    saved = null;
  }
  const source = saved && typeof saved === "object" ? saved : {};
  return {
    immersive:
      typeof source.immersive === "boolean"
        ? source.immersive
        : SHELL_DEFAULTS.immersive,
    sidebar:
      typeof source.sidebar === "boolean" ? source.sidebar : SHELL_DEFAULTS.sidebar,
    header:
      typeof source.header === "boolean" ? source.header : SHELL_DEFAULTS.header,
  };
}

function saveShell() {
  try {
    window.localStorage.setItem(SHELL_KEY, JSON.stringify(state.shell));
  } catch (error) {
    /* 隐私模式等场景下 localStorage 不可用：偏好仍在本次会话生效 */
  }
}

// v14 #121：首次进入沉浸星图时提示一次操作入口。只记 sessionStorage —— 关掉标签页
// 后会再提示一次，不新增 localStorage 键（偏好键仍只有 workbench-view / workbench-shell）。
const SHELL_HINT_KEY = "workbench-shell-hint";
function hintImmersiveOnce() {
  try {
    if (window.sessionStorage.getItem(SHELL_HINT_KEY)) return;
    window.sessionStorage.setItem(SHELL_HINT_KEY, "1");
  } catch (error) {
    return; // 隐私模式存不住：干脆不提示，免得每次渲染都弹
  }
  toast("沉浸星图：悬停左缘展开侧栏 · 右缘「会话」坞 · f 退出");
}

// 按路由 / 星图态 / 偏好写 body dataset。只有「列表路由 + 真的画出星图 + 用户要沉浸」
// 才挂 immersive；复习 / 设置 / 空态 / 骨架 / 错误一律 standard（否则全幅空壳）。
function applyShell(orbitMode = document.body.dataset.view === "orbit") {
  const immersive =
    state.route === "list" && orbitMode === true && state.shell.immersive === true;
  document.body.dataset.shell = immersive ? "immersive" : "standard";
  document.body.dataset.sidebar =
    state.route === "list" && state.shell.sidebar === true ? "open" : "hidden";
  if (immersive) hintImmersiveOnce(); // v14 #121：首次真的落进沉浸态才提示（含渲染后的再进入）
  syncShell();
}

// DOM 与状态对齐：把手 aria-expanded / 会话数徽标；侧栏 inert + aria-hidden；遮罩 class。
// 只在列表路由把侧栏当覆盖层；复习 / 设置页的侧栏仍是普通目录，不能 inert。
function syncShell() {
  const routeList = state.route === "list";
  const sidebarOpen = routeList && state.shell.sidebar === true;
  // v19 T1（票 #152）：peek = 悬停预览，侧栏视觉可见可交互，inert 必须解除；
  // aria-expanded 仍只反映钉住态（预览不改变持久状态，读屏走 [ 键语义）。
  const peeking = document.body.dataset.sidebar === "peek";
  if (dom.handle) {
    dom.handle.setAttribute("aria-expanded", sidebarOpen ? "true" : "false");
    // v14 #121：把手的可访问名跟着开合走（☰ 本身读不出动作）
    dom.handle.setAttribute("aria-label", sidebarOpen ? "收起侧栏" : "展开侧栏");
    const count = dom.handle.querySelector(".shell-count");
    if (count) count.textContent = String(state.sessions.length || 0);
  }
  const hidden = routeList && !sidebarOpen && !peeking;
  dom.sidebar.inert = hidden;
  if (hidden) dom.sidebar.setAttribute("aria-hidden", "true");
  else dom.sidebar.removeAttribute("aria-hidden");
  if (dom.scrim) dom.scrim.classList.toggle("is-open", sidebarOpen);
}

// 侧栏开合：只改状态与 dataset / class，不重绘主区 —— 舞台宽度恒定，星子不重排。
function setSidebarOpen(open) {
  state.shell.sidebar = Boolean(open);
  saveShell();
  applyShell();
  if (state.shell.sidebar) {
    const row = dom.sidebar.querySelector(".side-item-row.is-on");
    if (row) row.scrollIntoView({ block: "nearest" });
    const close = dom.sidebar.querySelector(".side-close");
    if (close) close.focus({ preventScroll: true });
    else dom.sidebar.focus({ preventScroll: true });
  } else if (dom.handle) {
    dom.handle.focus({ preventScroll: true });
  }
}

// --- v19 T1（票 #152）：悬停预览（peek）与 hover 热区 -----------------------
// peek 只写 body[data-sidebar]，不落 localStorage、不抢焦点 —— 「刷新后侧栏常开」
// 是钉住（setSidebarOpen）的事。applyShell 每次都会把 dataset 归位 open|hidden，
// 所以路由切换 / 重渲染天然清掉残留的 peek。
function setSidebarPeek(open) {
  if (open) {
    if (state.route !== "list" || state.shell.sidebar) return;
    document.body.dataset.sidebar = "peek";
  } else {
    document.body.dataset.sidebar =
      state.route === "list" && state.shell.sidebar ? "open" : "hidden";
  }
  syncShell();
}

// hover 热区公共件（左栏与右缘会话坞共用）。只用 pointerenter / pointerleave ——
// pointerover 的源码计数被 tests/test_shell_source.py 钉死，不许新增。
// 打开前停 120ms（路过不闪），收回留 260ms 宽限（容住面板滑出盖过把手时
// 两侧 enter/leave 的先后差，口径对齐 ctx-mark popover 的宽限模式）。
const PEEK_OPEN_MS = 120;
const PEEK_CLOSE_MS = 260;
function bindHoverZone(trigger, panel, onOpen, onClose) {
  if (!trigger || !panel) return { disarm() {} };
  if (window.matchMedia("(hover: none)").matches) return { disarm() {} }; // 触屏：开合全靠点击
  let inside = 0; // 位掩码：1 = 指针在 trigger 上，2 = 在面板上
  let timer = 0;
  const arm = (delay, action) => {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(() => {
      timer = 0;
      action();
    }, delay);
  };
  // 点击 = 用户给出了明确的开/收指令，悬停计时器必须作废——否则点击收回后
  // 120ms 内 pending 的 onOpen 又把面板弹开（验收 v19-verify-dock B5 抓到）。
  const disarm = () => {
    if (timer) {
      window.clearTimeout(timer);
      timer = 0;
    }
  };
  trigger.addEventListener("pointerenter", () => {
    inside |= 1;
    disarm();
    arm(PEEK_OPEN_MS, onOpen);
  });
  trigger.addEventListener("pointerleave", () => {
    inside &= ~1;
    arm(PEEK_CLOSE_MS, onClose);
  });
  panel.addEventListener("pointerenter", () => {
    inside |= 2;
    disarm(); // 指针在面板上绝不收回
  });
  panel.addEventListener("pointerleave", () => {
    inside &= ~2;
    arm(PEEK_CLOSE_MS, onClose);
  });
  return { disarm };
}

// 会话动作区开合。沉浸态作用于右缘会话坞的「动作 ⌄」分节头（v19 T2，票 #153）；
// standard 列表页仍指向旧胶囊的动作层（那里开关 display:none、动作层常显，
// 调用无视觉副作用）。state.shell.header 键与 h 键 / Esc 链口径沿用 v14。
function setHeaderOpen(open) {
  state.shell.header = Boolean(open);
  saveShell();
  const dock = dom.main.querySelector(".orbit-dock");
  if (dock) {
    const body = dock.querySelector(".dock-actions-body");
    if (body) body.classList.toggle("is-open", state.shell.header);
    const headBtn = dock.querySelector(".dock-actions-head");
    if (headBtn) {
      const label = state.shell.header ? "收起会话动作" : "展开会话动作";
      headBtn.title = `${label}（h）`;
      headBtn.setAttribute("aria-label", label);
      headBtn.setAttribute("aria-expanded", state.shell.header ? "true" : "false");
    }
    return;
  }
  const header = dom.main.querySelector(".list-header");
  if (!header) return;
  header.classList.toggle("is-collapsed", !state.shell.header);
  const toggle = header.querySelector(".list-header-toggle");
  if (!toggle) return;
  const label = state.shell.header ? "收起会话动作" : "展开会话动作";
  toggle.textContent = state.shell.header ? "▴" : "▾";
  toggle.title = `${label}（h）`;
  toggle.setAttribute("aria-label", label);
  toggle.setAttribute("aria-expanded", state.shell.header ? "true" : "false");
}

// f：切换星图全幅。列表视图下先切到星图（setView 会重绘并走 applyShell）；
// 已是星图时只写 dataset，舞台新尺寸由既有 ResizeObserver 接住。
function toggleImmersive() {
  state.shell.immersive = !state.shell.immersive;
  saveShell();
  invalidateSelectionRead(); // v16 C1：进入 / 离开全屏阅读态 = 上下文变化（视图可能重建）
  if (currentView() !== "orbit") {
    setView("orbit");
    return;
  }
  applyShell();
  // v19 T2（票 #153）：胶囊 ⇄ 会话坞互换。不 renderMain —— 星图不重建
  //（v14 的性能决策），只把头部元素换掉，星子布局不受影响。
  const header = dom.main.querySelector(".list-header, .orbit-dock");
  if (header) {
    if (state.dock !== "hidden") setDock("hidden");
    header.replaceWith(
      document.body.dataset.shell === "immersive" ? orbitDock() : listHeader()
    );
  }
}

// 当前视图：启动时 state.view 已定为「上次记住的视图，缺省星图」，正常路径直接返回它。
// 兜底分支只在 state.view 尚未初始化时用（本会话 → 星图，其它 → 列表）。
function currentView() {
  if (state.view) return state.view;
  return state.filter === "session" && state.sessionId !== null ? "orbit" : "list";
}

function setView(view) {
  if (currentView() === view) return;
  state.view = view;
  try {
    window.localStorage.setItem(VIEW_KEY, view);
  } catch (error) {
    /* 存不住就只在本次会话生效 */
  }
  renderMain();
}

// 星图上的单位 = 列表里的一张段卡（同段原句只画一次，见 contextCards）
// v14 T2：段卡的先后 = 段内**最早**一条的收录时间；同秒按 id 升序（先入库的先出）。
// 序号用**绝对序号**（80 段封顶时本图从第 start 段开始，不装成 1），顺序通读据此定位。
function orbitDeck() {
  const cards = [];
  for (const group of groupRecords()) {
    for (const card of contextCards(group.records)) {
      let first = null;
      let firstId = Infinity;
      let firstAt = "";
      for (const record of card.records) {
        const at = parseTs(record.captured_at);
        if (!at) continue; // 解析失败不算「最早」：这种段排到最后（Infinity），不再冒充第 1 段
        const time = at.getTime();
        const id = Number(record.id) || 0;
        if (first === null || time < first || (time === first && id < firstId)) {
          first = time;
          firstId = id;
          firstAt = record.captured_at;
        }
      }
      cards.push({
        key: card.key,
        context: card.context,
        records: card.records,
        group,
        at: first === null ? Number.POSITIVE_INFINITY : first,
        firstId: firstId === Infinity ? 0 : firstId,
        firstAt,
      });
    }
  }
  // 阅读顺序 = 收录先后：用户从上往下读文章，先读到的段落在前，
  // 所以 ←/→ 走的就是「文章的上一段 / 下一段」，不是倒着放的最新优先。
  cards.sort((one, two) => one.at - two.at || one.firstId - two.firstId);
  // 80 段封顶保留**最近**的 80 段（用户确认）：序号仍是绝对序号，本图从第 start 段开始
  const shown = cards.slice(-ORBIT_LIMIT);
  const start = cards.length - shown.length + 1;
  shown.forEach((item, index) => {
    item.order = start + index;
    item.total = cards.length;
  });
  return { cards: shown, total: cards.length, start };
}

// v31：听看复习的段卡顺序与星图同源（段内最早收录时间），但不做 80 段封顶 ——
// 复习要覆盖当前筛选里的全部段卡；星图那条路径仍保留自己的截断与绝对序号。
// key 带上 group.key：同一段原句跨会话出现时（极罕见）不会在计划里被误认为同一张卡。
function reviewSessionCards() {
  const cards = [];
  for (const group of groupRecords()) {
    for (const card of contextCards(group.records)) {
      let first = null;
      let firstId = Infinity;
      for (const record of card.records) {
        const at = parseTs(record.captured_at);
        if (!at) continue;
        const time = at.getTime();
        const id = Number(record.id) || 0;
        if (first === null || time < first || (time === first && id < firstId)) {
          first = time;
          firstId = id;
        }
      }
      cards.push({
        key: `${group.key}::${card.key}`,
        context: card.context,
        records: card.records,
        group,
        at: first === null ? Number.POSITIVE_INFINITY : first,
        firstId: firstId === Infinity ? 0 : firstId,
      });
    }
  }
  cards.sort((one, two) => one.at - two.at || one.firstId - two.firstId);
  return cards;
}

function orbitKindOf(record) {
  return KIND_LABEL[record && record.kind] ? record.kind : "word";
}

// 星图上的原句：只标色、不挂 popover（整张卡是一个按钮，里面不能再套按钮）
function orbitSentence(context, located) {
  const sentence = el("p", "orbit-sentence");
  const segments = sentenceSegments(context, located);
  if (!segments.length) {
    sentence.append(document.createTextNode(context));
    return sentence;
  }
  for (const segment of segments) {
    if (!segment.records.length) sentence.append(document.createTextNode(segment.text));
    else
      sentence.append(
        el(
          "mark",
          segment.records.length > 1 ? "orbit-mark is-overlap" : "orbit-mark",
          segment.text
        )
      );
  }
  return sentence;
}

// 星图上的「星子」：只有词本身（+N 表示这段还记了几条）。原句放悬停浮卡里 ——
// 35 段原句同时铺开只会糊成一片字，看全貌靠词，读内容靠悬停 / 点开。
function orbitBrief(item, layout) {
  const primary = item.records[0];
  const kind = orbitKindOf(primary);
  const brief = el("div", "orbit-brief");
  brief.append(el("span", `orbit-kind kind-${kind}`, KIND_LABEL[kind]));
  brief.append(el("span", `orbit-target kind-${kind}`, primary.target));
  if (layout.header.length > 1)
    brief.append(el("span", "orbit-target-more", `+${layout.header.length - 1}`));
  return brief;
}

// 悬停浮卡：原句 + 这一段里其它词 + 时刻。不做交互（点卡片进阅读页）。
function orbitHover(item, layout) {
  const primary = item.records[0];
  const kind = orbitKindOf(primary);
  const pop = el("div", "orbit-pop");
  const popHead = el("div", "orbit-pop-head");
  popHead.append(el("span", `orbit-kind kind-${kind}`, KIND_LABEL[kind]));
  pop.append(popHead);
  if (layout.context) pop.append(orbitSentence(layout.context, layout.located));
  else if (primary.meaning) pop.append(el("div", "orbit-pop-meaning", primary.meaning));
  else pop.append(el("div", "orbit-pop-meaning is-empty", "（无原句）"));

  const extra = layout.header.slice(1, 5);
  if (extra.length || primary.meaning) {
    const meta = el("div", "orbit-pop-meta");
    if (extra.length)
      meta.append(el("span", "orbit-pop-more", extra.map((record) => record.target).join(" · ")));
    if (layout.header.length > 5) meta.append(el("span", "orbit-pop-more", `+${layout.header.length - 5}`));
    pop.append(meta);
  }
  const foot = el("div", "orbit-pop-foot");
  foot.append(el("span", "orbit-order-label", `第 ${item.order}/${item.total} 段`));
  // v14 T2：显示段内**最早**的时刻（排序用的就是它）；后来补记的那条另起一句「最近 …」
  foot.append(el("span", "orbit-time", formatTime(item.firstAt)));
  if (item.firstAt !== primary.captured_at)
    foot.append(el("span", "orbit-time is-recent", `最近 ${formatTime(primary.captured_at)}`));
  if (item.records.some(isDue)) foot.append(el("span", "orbit-flag is-due", "待复习"));
  if (layout.context && layout.context.length > 160)
    foot.append(el("span", "orbit-long", `长句 · ${layout.context.length} 字`));
  foot.append(el("span", "orbit-pop-hint", "点击展开"));
  pop.append(foot);
  return pop;
}

// v15 C2（票 #126）：阅读页播放条 —— 只在整段 / 选区朗读中或暂停中出现（单词发音不显示）。
// 只依赖播放状态，不参与 v12 进度高亮；循环开关是内存态（不写 localStorage / config）。
// v16.x（2026-10-24 用户反馈）：显隐从 display 硬切改成**高度过渡**（播放开始面板平滑
// 长出这一行、结束平滑收回）。外层 wrap 只管折叠（grid 0fr ⇄ 1fr），内层 clip 把 bar
// 的外距 / 内距 / 分隔线一起裁掉 —— 折叠才不会残留一圈边距；bar 本体的 is-on 语义不变
// （renderPlaybar 一行未改），wrap 用 CSS :has 跟随，不新增 JS 状态。
function orbitPlaybar() {
  const wrap = el("div", "orbit-playbar-wrap");
  const clip = el("div", "orbit-playbar-clip");
  const bar = el("div", "orbit-playbar");
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "播放条");
  bar.append(
    button("btn btn-subtle orbit-playbar-toggle", "⏸ 暂停", () => {
      if (pronouncePaused) resumePlayback();
      else pausePlayback();
    }),
    button("btn btn-subtle orbit-playbar-stop", "⏹ 停止", () => stopPronounce()),
    button("btn btn-subtle orbit-playbar-replay", "↻ 重播本段", replayPlayback),
    // 连播（自动挡）：开着时每段自然播完 → 过渡音 + 停顿 → 自动接下一段，到末段即停
    button("btn btn-subtle orbit-playbar-auto", "▶▶ 连播", toggleAutoRead),
    button("btn btn-subtle orbit-playbar-loop", "🔁 循环", togglePlaybackLoop)
  );
  bar.querySelector(".orbit-playbar-loop").setAttribute("aria-pressed", "false");
  bar.querySelector(".orbit-playbar-auto").setAttribute("aria-pressed", "false");
  clip.append(bar);
  wrap.append(clip);
  return wrap;
}

// 无原句卡（2026-10-25 用户报障：Alt+1 只录词的卡没法发音）的播放入口：词头（词 + 音标行）
// 就是本卡的「原句」。走 startSentencePlayback（单词 → pronounce mode:"long"）而不是裸
// pronounce：与整段同一套播放会话，播放条 / 暂停 / 重播 / 连播全部现成复用（裸 word 模式
// 没有播放条，endPlayback 的连播驱动也接不上）。
function makePlayableHead(head, target) {
  head.classList.add("is-playable");
  head.tabIndex = 0;
  head.setAttribute("role", "button");
  head.setAttribute("aria-label", `播放 ${target} 的发音`);
  head.title = "点词头听发音 · Enter / Space 同效";
  const play = (event) => {
    event.preventDefault();
    event.stopPropagation(); // 别触发全局「点别处收浮标 / 关 popover」（同原句 click 的口径）
    startSentencePlayback(head, target);
  };
  head.addEventListener("click", play);
  head.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") play(event);
  });
}

// 本卡阅读页的播放入口节点（renderPlaybar 点亮谁、连播从哪起播，都以它为准）：
// 有原句 = .row-sentence；无原句词卡 = 标了 is-playable 的词头。
function orbitReadPlayEntry(read) {
  if (!read) return null;
  return (
    read.querySelector(".row-sentence") || read.querySelector(".orbit-read-defhead.is-playable")
  );
}

// 阅读页：完整原句（高亮词仍可点开释义）+ 这一段的全部释义 + 动作。
// v10：字号固定、写不下就让**正文**滚动，不再为了「一屏放下」把字缩到 11px
// （用户报障：一段记得多的时候字太小，看不清）。顶部那排「词 + 音标」与下面的
// 释义是同一批词，白占三四行 —— 音标并进释义首行，顶部只留一行元信息。
function orbitRead(item, api) {
  const layout = cardLayout(item);
  const primary = item.records[0];
  const kind = orbitKindOf(primary);
  const read = el("div", "orbit-read");

  const meta = el("div", "orbit-read-meta");
  meta.append(el("span", `orbit-kind kind-${kind}`, KIND_LABEL[kind]));
  // v14 T2：第 i/N 段（绝对序号）+「最初记录」+ 段内最早时刻。
  // 之前这里显示的是段内最新一条，与星图的排序口径对不上（点开第 1 段却是后来的日期）。
  meta.append(el("span", "orbit-read-order", `第 ${item.order}/${item.total} 段`));
  meta.append(el("span", "orbit-read-count", `本段 ${item.records.length} 词`));
  meta.append(el("span", "orbit-flag", "最初记录"));
  meta.append(el("span", "orbit-time", formatFull(item.firstAt)));
  if (item.records.some(isDue)) meta.append(el("span", "orbit-flag is-due", "待复习"));
  for (const tag of [...new Set(item.records.flatMap(tagList))])
    meta.append(el("span", "orbit-flag", `#${tag}`));
  read.append(meta);

  // v23 P1（spec §4.1 入口 1）：本会话里仍为空的释义一键补查 —— 入口在 header
  // （紧跟元信息行），不塞进脚注导航里。本段一条空释义都没有时整块不出现。
  const refillPlan = readingRefillPlan(item);
  if (refillPlan) {
    const headActions = el("div", "orbit-read-head-actions");
    headActions.append(
      refillHeaderButton(
        refillPlan.count,
        refillPlan.body,
        "补查本会话里仍为空的释义（只补空释义，不覆盖已有内容）",
        { atLeast: refillPlan.atLeast }
      )
    );
    read.append(headActions);
  }

  const body = el("div", "orbit-read-body");
  if (layout.context) body.append(sentenceNode(layout.context, layout.located));
  // 有记录就画释义块：整段都没有释义（会话 114 形态）时也必须看到每一行与「补」。
  if (layout.header.length) {
    // 一段里可以记十几条：释义两列排开；顺序跟句子里出现的先后一致（同 layout.header）。
    // 没有原句的卡没有「上面的句子」，那就不画分隔线、不留空档（is-first）。
    const defs = el("div", layout.context ? "orbit-read-meanings" : "orbit-read-meanings is-first");
    let playHead = null; // 无原句卡（Alt+1 只录词）的第一个词头 = 本卡播放入口（句卡的入口是 .row-sentence）
    let playTarget = "";
    for (const record of layout.header) {
      const row = el("div", record.meaning ? "orbit-read-def" : "orbit-read-def is-empty");
      const head = el("div", "orbit-read-defhead");
      head.append(el("span", `orbit-read-defterm kind-${orbitKindOf(record)}`, record.target));
      if (record.phonetic) head.append(el("span", "orbit-read-phonetic", record.phonetic));
      if (!layout.context && !playHead && record.target) {
        playHead = head;
        playTarget = record.target;
      }
      row.append(head);
      const deftext = el("div", "orbit-read-deftext", meaningLabel(record, "（暂无释义）"));
      // v23 P1：空释义行尾给「补」；v29：非空释义行尾给「重查」（只动这一条）。
      // 两者放进同一行，长释义在窄纸上折行时不挤压按钮。
      const line = el("div", "orbit-read-defline");
      line.append(
        deftext,
        record.meaning ? recheckInlineButton(record) : refillInlineButton(record)
      );
      row.append(line);
      const machineBadge = meaningMachineBadge(record); // #172：机翻小标
      if (machineBadge) row.append(machineBadge);
      defs.append(row);
    }
    if (playHead) makePlayableHead(playHead, playTarget);
    body.append(defs);
  }
  read.append(body);

  read.append(orbitPlaybar()); // v15 C2：播放条在动作脚注上方（只在播放 / 暂停中出现）
  const foot = el("div", "orbit-read-foot");
  foot.append(button("btn btn-primary", "打开详情", () => openDrawer(primary.id)));
  const listen = button("btn btn-subtle review-card-entry", "🎧 听看本段", () =>
    startOrbitCardHighlightReview()
  );
  listen.title =
    "只播本段高亮词条（词 → 短语 → 句；按最早收录顺序），每条配释义弹窗与发音";
  foot.append(listen);
  foot.append(button("btn btn-subtle", "← 上一段", () => api.step(-1)));
  foot.append(button("btn btn-subtle", "下一段 →", () => api.step(1)));
  foot.append(button("btn btn-subtle", "回星图 Esc", () => api.unfocus()));
  // 正文滚得动时才出现：滚动条被全局藏掉了，总得有个说得出「还有下文」的地方。
  // v15 T2（票 #127，A5）：文案两态 —— 可滚且没到底 → 先滚正文；不可滚 → 滚轮换段。
  foot.append(
    el(
      "span",
      "orbit-read-hint",
      state.wheelNav ? "↓ 滚轮翻正文 · 到底再滚 = 下一段" : "↓ 滚轮上下翻"
    )
  );
  if (state.wheelNav) foot.append(el("span", "orbit-read-wheel", "滚轮 = 上一段 / 下一段"));
  read.append(foot);
  return read;
}

// 正文滚动状态：只切 class（底部渐隐 + 滚动提示），字号一律不动。
// 滚到底就把渐隐和「滚轮上下翻」一起收掉 —— 已经到底了还提示「下面还有」是假信号。
function orbitSyncReadScroll(plate) {
  const body = plate.querySelector(".orbit-read-body");
  if (!body) return;
  const sync = () => {
    plate.classList.toggle("is-scrollable", body.scrollHeight > body.clientHeight + 2);
    plate.classList.toggle(
      "is-at-end",
      body.scrollTop + body.clientHeight >= body.scrollHeight - 2
    );
  };
  if (!body.dataset.scrollBound) {
    body.dataset.scrollBound = "1";
    body.addEventListener("scroll", sync, { passive: true });
  }
  sync();
}

let orbit = null; // 当前挂着的星图实例；renderMain 开头必须销毁（画布 / rAF / 观察器）

function orbitDestroy() {
  if (!orbit) return;
  orbit.destroy();
  orbit = null;
}

function orbitView() {
  const stage = el("div", "orbit");
  stage.setAttribute("role", "group");
  stage.setAttribute("aria-label", "星图：本会话的全部词条，可拖动旋转，点卡片聚焦阅读");
  const deck = orbitDeck();
  // v14 T2：顺序通读的一次性旗子交给 createOrbit —— 真正消费它的是首次 measure()
  orbit = createOrbit(stage, deck, { startAtFirst: state.startAtFirst });
  state.startAtFirst = false;
  return stage;
}

// v14 T2：顺序通读 —— 按收录先后从最早一段开始逐段读，不改 SRS 到期日、不打分。
// 已在星图就直接从第 1 张开场；否则切到星图，让首次 measure() 消费 startAtFirst。
function startReadThrough() {
  if (orbit) {
    orbit.startSequence();
    return;
  }
  state.startAtFirst = true;
  if (currentView() !== "orbit") setView("orbit");
  else renderMain();
}

function createOrbit(stage, deck, options) {
  const canvas = el("canvas", "orbit-fx");
  const core = el("div", "orbit-core");
  const world = el("div", "orbit-world");
  const legend = el("div", "orbit-legend", ORBIT_HINTS);
  legend.setAttribute("aria-hidden", "true");
  stage.append(canvas, core, world, legend);

  const ctx = canvas.getContext("2d");
  const scene = {
    stage,
    canvas,
    ctx,
    world,
    legend,
    items: [],
    links: [],
    dust: [],
    dustSpan: 0,
    spanX: 480,
    spanY: 380,
    rect: null,
    observer: null,
    angle: -0.55,
    parX: 0,
    parY: 0,
    spinVel: 0,
    tilt: ORBIT_TILT,
    zoom: 1,
    zoomTarget: 1,
    hover: -1,
    hoverDim: 0,
    focusIndex: -1,
    dim: 0,
    suppressUntil: 0,
    w: 0,
    h: 0,
    dpr: 0,
    radius: 400,
    raf: 0,
    last: 0,
    time: 0,
    alive: true,
    live: false,
    // v14 T2：顺序通读 —— 首次 live 直接聚焦最早一段；sequence=true 时两端夹住不环绕
    startAtFirst: Boolean(options && options.startAtFirst),
    sequence: false,
    paused: false,
    palette: ORBIT_PALETTE[state.theme] || ORBIT_PALETTE.light,
    reduced: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    drag: null,
  };

  function orbitLegendText() {
    // v14 #121：前端一次只拉最新 500 条（LIST_LIMIT），>500 时图例也要说清口径，
    // 不能假装「最早」= 全量最早；80 段封顶与绝对序号的口径同时保留。
    const loaded = state.total > LIST_LIMIT ? ` · 仅载入最新 ${LIST_LIMIT} 条` : "";
    // 80 段封顶 + 绝对序号：说清「这张图从第几段开始」，否则「第 83 段」像丢了 82 段
    const capped =
      deck.total > ORBIT_LIMIT
        ? `${ORBIT_HINTS} · 只铺最近 ${ORBIT_LIMIT} 段（共 ${deck.total} 段，本图从第 ${deck.start} 段开始，切列表看全部）`
        : ORBIT_HINTS;
    return capped + loaded;
  }

  // ---- 停帧：两个来源（抽屉/帮助浮层、阅读页聚焦）合成一个 ---------------------
  // v15 T2（票 #127，A4）：阅读页聚焦期间压掉星图背景重绘。停帧有两个来源，
  // 必须合成一个 scene.paused —— 任何一个要停就停，全部放开才恢复；
  // 否则「关抽屉」会把阅读页的停帧一起解除（两边互相踩）。
  const pausedBy = { overlay: false, reading: false };
  let readingPauseRaf = 0;
  let wheelLeaveTimer = 0;
  let wheelLeaveAnim = null;
  let wheelLeaveRead = null;

  // 清掉「上一段淡出」的残留（计时器 / WAAPI 动画 / will-change）：反向滚轮能在锁内
  // 立刻再换一段，上一段的淡出必须收干净，别把 will-change 留在没动画的元素上。
  function clearWheelLeave() {
    if (wheelLeaveTimer) window.clearTimeout(wheelLeaveTimer);
    wheelLeaveTimer = 0;
    if (wheelLeaveAnim) wheelLeaveAnim.cancel();
    wheelLeaveAnim = null;
    if (wheelLeaveRead) wheelLeaveRead.style.willChange = "";
    wheelLeaveRead = null;
  }

  function applyPaused() {
    const paused = pausedBy.overlay || pausedBy.reading;
    scene.paused = paused;
    if (paused) {
      if (scene.raf) window.cancelAnimationFrame(scene.raf);
      scene.raf = 0;
    } else {
      scene.last = 0;
      schedule();
    }
  }

  // 等聚焦飞行收敛（fk → 1）再停帧：飞行途中停会让纸面「跳」到位（spec §A4 / 已知风险 5）。
  // reduced-motion 只是没有自转 / 浮动，纸面飞行缓动照旧 —— 同样等收敛，别在飞行中途停。
  function armReadingPause(entry) {
    if (readingPauseRaf) window.cancelAnimationFrame(readingPauseRaf);
    readingPauseRaf = 0;
    const started = performance.now();
    readingPauseRaf = window.requestAnimationFrame(function tick() {
      readingPauseRaf = 0;
      if (!scene.alive || scene.focusIndex !== entry.index) return;
      // 浮层中途压下就会停在飞行里：直接认账，等浮层放开时 setPaused 会续飞再停。
      if (pausedBy.overlay || entry.fk > 0.999 || performance.now() - started > 1200) {
        pausedBy.reading = true;
        applyPaused();
        return;
      }
      readingPauseRaf = window.requestAnimationFrame(tick);
    });
  }

  function setReadingPaused(value) {
    if (readingPauseRaf) window.cancelAnimationFrame(readingPauseRaf);
    readingPauseRaf = 0;
    pausedBy.reading = Boolean(value);
    applyPaused();
  }

  // ---- 换段的内容层动画（只动 opacity / transform，纸面尺寸与位置不动）--------
  function playReadIn(read) {
    if (!read || scene.reduced) return; // prefers-reduced-motion：瞬切
    read.style.willChange = "opacity, transform"; // 只在动画期间加，结束即移除
    const anim = read.animate(
      [
        { opacity: 0, transform: "translateY(8px)" },
        { opacity: 1, transform: "none" },
      ],
      { duration: READ_FADE_IN_MS, easing: "ease-out" }
    );
    const done = () => {
      read.style.willChange = "";
    };
    anim.addEventListener("finish", done);
    anim.addEventListener("cancel", done);
  }

  // 滚轮换段：旧内容先淡出 80ms 再换（DOM 一换就没有「旧」可淡了），新内容在
  // focusCard 里淡入。计时用 setTimeout 而不是 animationend：标签页在后台时
  // WAAPI 会暂停，等它就可能卡住不换段。
  function pageByWheel(delta) {
    const entry = scene.items[scene.focusIndex];
    const read = entry ? entry.read : null;
    clearWheelLeave();
    // 顺序通读两端夹住：step 只会 toast、不会换段 —— 别先淡出（内容会闪一下空白）
    const clamped =
      scene.sequence &&
      (delta > 0 ? scene.focusIndex >= scene.items.length - 1 : scene.focusIndex <= 0);
    if (!read || scene.reduced || clamped) {
      api.step(delta);
      return;
    }
    read.style.willChange = "opacity, transform";
    const anim = read.animate([{ opacity: 1 }, { opacity: 0 }], {
      duration: READ_FADE_OUT_MS,
      easing: "ease-in",
      fill: "forwards",
    });
    wheelLeaveAnim = anim;
    wheelLeaveRead = read;
    wheelLeaveTimer = window.setTimeout(() => {
      clearWheelLeave();
      if (!scene.alive || scene.focusIndex !== entry.index) return;
      api.step(delta); // 与按钮 / ←→ 同一条换段入口（普通浏览环绕、顺序通读两端夹住）
    }, READ_FADE_OUT_MS);
  }

  // ---- 聚焦 / 回星图 ---------------------------------------------------------
  function focusCard(index) {
    if (index < 0 || index >= scene.items.length) return;
    const entry = scene.items[index];
    if (scene.focusIndex === index && entry.read) return;
    // v15 T2（票 #127，A4）：换段要飞要重排 —— 先放开背景重绘，飞行收敛后再停（armReadingPause）
    setReadingPaused(false);
    scene.hover = -1; // v14：清掉聚焦瞬间残留的星座高亮
    stopPronounce(); // v11 §1：换一段就停掉上一段的朗读（按钮 / j·k / ←·→ 都走这里）
    invalidateSelectionRead(); // v16 C1：换段 = 上下文变化，旧快照（含浮标）一起失效
    closePopover(); // v11 §1.1：换一段 = 旧阅读页收回去，悬停 popover 一起收
    // 旧的阅读页要整个收回去：只摘 is-focus 会把上一张读过的大纸留在场景里
    // （既挡视线又白白参与布局/绘制，点「打开详情」还可能点到它头上）。
    for (const other of scene.items) {
      if (other === entry) continue;
      other.node.inert = true; // v14：背景星子退出 Tab / 命中测试
      other.node.tabIndex = -1;
      if (other.node.classList.contains("is-focus") || other.plate.classList.contains("is-reading")) {
        other.node.classList.remove("is-focus");
        other.plate.classList.remove("is-reading", "is-scrollable", "is-at-end");
      }
    }
    scene.focusIndex = index;
    state.selectedId = entry.primary.id;
    if (!entry.read) {
      entry.read = orbitRead(entry.item, api);
      entry.plate.append(entry.read);
    }
    entry.node.classList.add("is-focus");
    // 立刻去虚：远处的星子是带景深的，阅读页不能跟着糊（用户报障）
    entry.blur = 0;
    entry.node.style.filter = "";
    entry.plate.classList.add("is-reading");
    playReadIn(entry.read); // v15 T2（票 #127，A4）：新内容淡入 + translateY(8px→0)，纸面不动
    // 性能：滚动条状态要读一次布局，但不必在点击事件里同步做 —— 挪到下一帧判断，
    // 画面只差不到一帧，点击事件立刻返回（输入响应不再被 30ms+ 的排版卡住）。
    requestAnimationFrame(() => orbitSyncReadScroll(entry.plate));
    stage.classList.add("is-focus");
    // v13 §2：换卡先清掉上一张卡**待发**的预取（已发出的请求保留，回来仍入缓存）
    clearPrefetchQueue();
    // v13 §1：只预取点击真正会播的前 1–2 片（不再把整段原句发去合成），再排队卡内
    // 最多 8 个 target；切片与点击同源（同一个 splitForSpeech + 预取里的 trim）。
    const contextChunks = splitForSpeech(entry.item.context).slice(0, PREFETCH_CONTEXT_CHUNKS);
    for (const chunk of contextChunks) prefetch(chunk);
    for (const record of entry.item.records.slice(0, 8)) prefetch(record.target);
    // v14：换到一张曾是背景的卡时，先摘掉 old 焦点留下的 inert，否则 focus() 静默失败
    entry.node.inert = false;
    entry.node.tabIndex = 0;
    // 性能：聚焦会触发一次样式 / 布局检查（~15ms），同样挪到同一帧的 rAF 里（绘制前完成）。
    requestAnimationFrame(() => entry.node.focus({ preventScroll: true }));
    schedule();
    armReadingPause(entry); // 等这张纸飞到位再停背景重绘（不取消飞行动画与背景退让缓动）
  }

  // v31 #196：数据变更后重画当前阅读页（不换卡、不动镜头）。正文滚动位置保留。
  function refreshReadCard(entry) {
    if (!entry || !entry.read) return;
    const old = entry.read;
    const oldBody = old.querySelector(".orbit-read-body");
    const scrollTop = oldBody ? oldBody.scrollTop : 0;
    const fresh = orbitRead(entry.item, api);
    old.replaceWith(fresh);
    entry.read = fresh;
    const body = fresh.querySelector(".orbit-read-body");
    if (body) body.scrollTop = scrollTop;
    orbitSyncReadScroll(entry.plate);
  }

  function unfocusCard() {
    if (scene.focusIndex < 0) return;
    setReadingPaused(false); // v15 T2：回星图恢复背景重绘
    stopPronounce(); // v11 §1：回星图 = 退出阅读页，当前音频一起停
    invalidateSelectionRead(); // v16 C1：退出阅读页 = 上下文变化，旧快照（含浮标）一起失效
    clearPrefetchQueue(); // v13 §2：退出阅读页同样清掉**待发**预取（已发出的保留）
    closePopover(); // v11 §1.1：退出阅读页，悬停 popover 一起收
    scene.focusIndex = -1;
    scene.sequence = false; // v14 T2：回星图即退出顺序通读，普通浏览恢复环绕
    for (const entry of scene.items) {
      entry.node.inert = false; // v14：回星图，全部星子恢复可聚焦 / 可命中
      entry.node.tabIndex = 0;
      entry.node.classList.remove("is-focus");
      entry.plate.classList.remove("is-reading", "is-scrollable", "is-at-end");
    }
    stage.classList.remove("is-focus");
    schedule();
  }

  const api = {
    isFocused() {
      return scene.focusIndex >= 0;
    },
    focus(index) {
      focusCard(index);
    },
    // v14 T2：顺序通读开场 —— 从本图最早一段读起，并进入「两端夹住」模式
    startSequence() {
      scene.sequence = true;
      focusCard(0);
    },
    step(delta) {
      const total = scene.items.length;
      if (!total) return;
      let next = scene.focusIndex;
      if (scene.sequence) {
        // 顺序通读不环绕：走到头就停在原处，给一句提示（普通浏览仍走下面的模运算）
        const target = next < 0 ? (delta > 0 ? 0 : total - 1) : next + delta;
        if (target >= total) {
          toast("已到最新一段");
          return;
        }
        if (target < 0) {
          toast("已到最早一段");
          return;
        }
        focusCard(target);
        return;
      }
      next = next < 0 ? (delta > 0 ? 0 : total - 1) : (next + delta + total) % total;
      focusCard(next);
    },
    // 连播驱动器（2026-10-24）只读窥视：focusIndex 藏在闭包里，外部要知道
    // 「播到哪一段、一共几段」才能判断还有没有下一段。item 与 focusCard 用的是同一份。
    current() {
      const entry = scene.items[scene.focusIndex];
      if (!entry) return null;
      return { index: scene.focusIndex, total: scene.items.length, item: entry.item };
    },
    // 只读：第 index 段的卡片数据（连播预取下一段原句 / 目标词用；越界返回 null）
    itemAt(index) {
      const entry = scene.items[index];
      return entry ? entry.item : null;
    },
    // v31 #196：后台重拉后把新 record 合回场景；当前阅读页就地重画，
    // 不换段、不收回星图、也不动镜头。
    updateRecords(ids) {
      const wanted = new Set(Array.from(ids || [], (id) => String(id)));
      if (!wanted.size) return;
      let focused = false;
      for (const entry of scene.items) {
        let touched = false;
        entry.item.records = entry.item.records.map((record) => {
          if (!wanted.has(String(record.id))) return record;
          const fresh = state.records.find((item) => String(item.id) === String(record.id));
          if (!fresh) return record;
          touched = true;
          return fresh;
        });
        if (!touched) continue;
        entry.item.layout = null; // layout 缓存里还是旧 record 对象，清掉重算
        if (scene.focusIndex >= 0 && scene.items[scene.focusIndex] === entry) focused = true;
      }
      if (focused) refreshReadCard(scene.items[scene.focusIndex]);
    },
    unfocus() {
      unfocusCard();
    },
    refreshTheme() {
      scene.palette = ORBIT_PALETTE[state.theme] || ORBIT_PALETTE.light; // 深浅切换：画布跟着换色
      // 阅读页停帧时 schedule() 是空操作：直接补一帧，否则纸面背后的星图还是旧配色。
      if (scene.paused) draw();
      else schedule();
    },
    // 抽屉 / 帮助浮层压在星图上时停帧：底层一直在动，弹层滑入就会卡。
    // v15 T2：与阅读页聚焦的停帧合成（pausedBy），关浮层不许把阅读页的停帧一起解除。
    setPaused(value) {
      pausedBy.overlay = Boolean(value);
      if (!pausedBy.overlay && pausedBy.reading && scene.focusIndex >= 0) {
        // 浮层开合时可能停在飞行中途：先放开跑完飞行，收敛后再停
        setReadingPaused(false);
        armReadingPause(scene.items[scene.focusIndex]);
        return;
      }
      applyPaused();
    },
    destroy() {
      scene.alive = false;
      if (scene.raf) window.cancelAnimationFrame(scene.raf);
      scene.raf = 0;
      if (readingPauseRaf) window.cancelAnimationFrame(readingPauseRaf);
      readingPauseRaf = 0;
      clearWheelLeave();
      // W16（票 #189）：measure 的 rAF 合并与收敛补量也要跟着销毁走
      if (measureRaf) window.cancelAnimationFrame(measureRaf);
      measureRaf = 0;
      if (measureSettleTimer) window.clearTimeout(measureSettleTimer);
      measureSettleTimer = 0;
      if (scene.observer) scene.observer.disconnect();
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };

  // 切到别的标签页就停帧：星图是常驻动画，别在后台空烧 CPU
  function onVisibility() {
    if (document.hidden) {
      if (scene.raf) window.cancelAnimationFrame(scene.raf);
      scene.raf = 0;
      scene.last = 0;
    } else {
      schedule();
    }
  }
  document.addEventListener("visibilitychange", onVisibility);

  // ---- 卡片 -----------------------------------------------------------------
  for (let index = 0; index < deck.cards.length; index += 1) {
    const item = deck.cards[index];
    const layout = cardLayout(item);
    const primary = item.records[0];
    const kind = orbitKindOf(primary);
    const node = el("div", `orbit-card kind-${kind}`);
    node.dataset.index = String(index);
    node.tabIndex = 0;
    node.setAttribute("role", "button");
    node.setAttribute(
      "aria-label",
      `${KIND_LABEL[kind]}「${primary.target}」第 ${item.order}/${item.total} 段：回车聚焦阅读`
    );
    node.style.setProperty("--i", String(index));
    const plate = el("div", "orbit-plate");
    // v14 T2：序号角标走 CSS 伪元素（零 DOM 节点、不进 measure 测宽）；首卡朱砂描边
    plate.dataset.order = String(item.order);
    plate.dataset.first = item.order === deck.start ? "1" : "0";
    if (item.order === deck.start) node.classList.add("is-first");
    plate.append(orbitBrief(item, layout));
    node.append(plate);

    const entry = {
      index,
      item,
      primary,
      node,
      plate,
      read: null,
      hoverPop: null,
      baseX: 0,
      baseY: 0,
      baseZ: 0,
      w: 0,
      h: 0,
      hover: 0,
      fk: 0,
      x: 0,
      y: 0,
      tx: 0,
      ty: 0,
      s: 0.001,
      ts: 1,
      o: 0,
      to: 1,
      ry: 0,
      rx: 0,
      ry2: 0,
      rx2: 0,
      depth: 2000,
      blur: -1,
      z: 0,
      transform: "",
      opacityValue: "",
      opacityOnPlate: false,
    };
    scene.items.push(entry);

    node.addEventListener("pointerenter", () => {
      if (scene.focusIndex >= 0) return; // v14：阅读时不建浮卡、不改 hover
      scene.hover = index;
      // 浮卡按需构建：只有真悬停过的卡片才多这一块 DOM
      if (!entry.hoverPop) {
        entry.hoverPop = orbitHover(item, layout);
        node.append(entry.hoverPop);
      }
      node.classList.toggle("is-above", entry.y > scene.h * 0.16);
      node.classList.toggle("is-right", entry.x > scene.w * 0.24);
      node.classList.toggle("is-left", entry.x < -scene.w * 0.24);
      schedule();
    });
    node.addEventListener("pointerleave", () => {
      if (scene.hover === index) scene.hover = -1;
      schedule();
    });
    node.addEventListener("click", (event) => {
      // v14 #121：聚焦态不拦事件 —— 不 stopPropagation，让它冒泡到 stage.click。
      // 点背景卡 = 点空白 = 回星图；点聚焦卡本身由 stage.click 的 is-focus 守卫放过。
      if (scene.focusIndex >= 0) return;
      event.stopPropagation();
      if (performance.now() < scene.suppressUntil) return; // 刚拖过：这次算旋转，不算点击
      // 纸里的按钮（上一段 / 下一段 / 打开详情）自己干活：点它们的事件会冒泡到卡片上，
      // 不挡住就会在换段之后又被这张卡抢回来（用户点了「下一段」纹丝不动的根因）
      if (event.target.closest(".orbit-read")) return;
      focusCard(index);
    });
    node.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
      // 阅读页里的按钮与高亮词自己处理 Enter/Space —— 卡片只认「焦点就在卡上」
      // （否则按「下一段」会变成打开抽屉；与列表里 cardRow 的判定一致）
      if (event.target !== node) return;
      event.preventDefault();
      event.stopPropagation(); // 别让全局 Enter 再开一次抽屉
      if (scene.focusIndex === index) openDrawer(primary.id);
      else focusCard(index);
    });
    node.addEventListener("focus", () => {
      state.selectedId = primary.id;
    });
    world.append(node);
  }

  // ---- 几何：松弛排版 + 透视投影 ---------------------------------------------
  // 卡片在屏幕空间里两两推开（免得上 35 段原文糊成一片），再换算回盘面坐标；
  // 位置随整盘缓慢自转，深度差交给透视缩放与透明度。
  // W16（票 #189）：松弛轮次可降 —— 拖拽窗口里用 ORBIT_ROUNDS_DRAG 保帧率，
  // 收敛后的补量跑 ORBIT_ROUNDS 全轮次，布局质量不受影响（冷却曲线按轮次数归一）。
  function layoutPositions(rounds = ORBIT_ROUNDS) {
    const total = scene.items.length;
    const cosT = Math.cos(scene.tilt);
    const ax = Math.max(120, scene.w / 2 - 26);
    const ay = Math.max(90, scene.h / 2 - 34);
    const boxes = scene.items.map((entry) => ({
      hw: (entry.w || 140) / 2 + 8,
      hh: (entry.h || 34) / 2 + 8,
    }));
    // 黄金角初值（椭圆内均匀），再松弛
    const points = scene.items.map((entry, index) => {
      const t = (index + 0.5) / Math.max(1, total);
      const r = Math.sqrt(0.06 + 0.94 * t);
      const a = index * ORBIT_GOLDEN;
      return { x: Math.cos(a) * r * ax, y: Math.sin(a) * r * ay };
    });
    for (let round = 0; round < rounds; round += 1) {
      const cool = 0.62 * (1 - round / rounds) + 0.1;
      for (let i = 0; i < points.length; i += 1) {
        for (let j = i + 1; j < points.length; j += 1) {
          const dx = points[j].x - points[i].x;
          const dy = points[j].y - points[i].y;
          const ox = boxes[i].hw + boxes[j].hw - Math.abs(dx);
          const oy = boxes[i].hh + boxes[j].hh - Math.abs(dy);
          if (ox <= 0 || oy <= 0) continue;
          if (ox < oy) {
            const push = (ox / 2 + 0.5) * (dx >= 0 ? 1 : -1) * cool;
            points[i].x -= push;
            points[j].x += push;
          } else {
            const push = (oy / 2 + 0.5) * (dy >= 0 ? 1 : -1) * cool;
            points[i].y -= push;
            points[j].y += push;
          }
        }
      }
      for (const point of points) {
        const norm = Math.hypot(point.x / ax, point.y / ay);
        if (norm > 0.97) {
          point.x = (point.x / norm) * 0.97;
          point.y = (point.y / norm) * 0.97;
        }
      }
    }
    points.forEach((point, index) => {
      const entry = scene.items[index];
      entry.baseX = point.x;
      entry.baseY = point.y / cosT; // 投影时还会乘回 cosT，屏幕上正好落在松弛好的位置
      entry.baseZ = (orbitRand(entry.index + 7) - 0.5) * 150; // 前后错层：有近有远才有纵深
    });
    scene.spanX = ax;
    scene.spanY = ay / cosT;
    // 星座线：每颗星连最近的两位邻居（算一次，随盘一起转）
    const flat = Math.cos(ORBIT_TILT);
    const pairs = new Set();
    for (let i = 0; i < scene.items.length; i += 1) {
      const a = scene.items[i];
      const near = [];
      for (let j = 0; j < scene.items.length; j += 1) {
        if (j === i) continue;
        const b = scene.items[j];
        const dx = b.baseX - a.baseX;
        const dy = (b.baseY - a.baseY) * flat;
        near.push([dx * dx + dy * dy, j]);
      }
      near.sort((one, two) => one[0] - two[0]);
      for (const [distance, j] of near.slice(0, 2)) {
        if (distance > 300 * 300) continue;
        pairs.add(i < j ? `${i}-${j}` : `${j}-${i}`);
      }
    }
    scene.links = [...pairs].map((key) => key.split("-").map(Number));
    if (scene.dustSpan !== ax) {
      scene.dustSpan = ax;
      scene.dust = Array.from({ length: 140 }, (_, index) => ({
        r: 0.1 + orbitRand(index + 1) * 1.0,
        a: orbitRand(index + 31) * Math.PI * 2,
        z: (orbitRand(index + 71) - 0.5) * 240,
        size: 0.5 + orbitRand(index + 101) * 1.6,
        alpha: 0.16 + orbitRand(index + 131) * 0.7,
        speed: 0.55 + orbitRand(index + 151) * 0.5,
      }));
    }
  }

  // 把盘面坐标 (x0,y0,z0) 转到屏幕；返回屏幕坐标 / 深度 / 缩放
  function projectPoint(x0, y0, z0) {
    const ca = Math.cos(scene.angle + scene.parX * 0.05);
    const sa = Math.sin(scene.angle + scene.parX * 0.05);
    const dx = x0 * ca - y0 * sa;
    const dy = x0 * sa + y0 * ca;
    const tilt = scene.tilt + scene.parY * 0.03;
    const cosT = Math.cos(tilt);
    const sinT = Math.sin(tilt);
    const sy = dy * cosT - z0 * sinT;
    const depth = ORBIT_FOCAL / scene.zoom + dy * sinT + z0 * cosT;
    const s = ORBIT_FOCAL / Math.max(1, depth);
    return { x: scene.w / 2 + dx * s, y: scene.h / 2 + sy * s, depth, s };
  }

  // 卡片目标位：盘面位置 → 屏幕 + 深度；再叠加悬停抬起与聚焦飞行
  function project(entry) {
    const point = projectPoint(entry.baseX, entry.baseY, entry.baseZ);
    const bob = scene.reduced ? 0 : Math.sin(entry.baseX * 0.012 + scene.angle * 2) * 3;
    // 卡片是绝对定位在舞台中心再平移的：这里要的是相对中心的偏移，不是舞台坐标
    let x = point.x - scene.w / 2;
    let y = point.y - scene.h / 2 + bob;
    let s = point.s * (1 + entry.baseZ / 520); // 错层：靠前的字大一点、实一点
    let o =
      orbitClamp(0.52 + 0.48 * ((point.s - 0.9) / 0.2), 0.42, 1) *
      (0.66 + 0.34 * (1 - Math.abs(entry.baseZ) / 75));
    let ry = orbitClamp(-(point.x - scene.w / 2) / 95, -7, 7);
    let rx = orbitClamp((point.y - scene.h / 2) / 130, -5, 5);

    if (scene.hover === entry.index && scene.focusIndex < 0) {
      s *= 1 + entry.hover * 0.06;
      ry *= 1 - entry.hover;
      rx *= 1 - entry.hover;
    } else if (scene.hover >= 0 && scene.focusIndex < 0) {
      o *= 1 - 0.32 * scene.hoverDim; // 悬停时其它星子退后，浮卡才读得清
    }
    if (entry.fk > 0.0005) {
      // 聚焦：飞到舞台正中（偏移 0），缩放到 1:1 —— 阅读页里的字必须是原尺寸
      x += (0 - x) * entry.fk;
      y += (0 - y) * entry.fk;
      s += (1 - s) * entry.fk;
      o += (1 - o) * entry.fk;
      ry *= 1 - entry.fk;
      rx *= 1 - entry.fk;
    }
    if (scene.dim > 0.0005) {
      const dim = scene.dim * (1 - entry.fk);
      o *= 1 - 0.84 * dim;
      s *= 1 - 0.08 * dim;
    }
    return { x, y, s, o, ry, rx, depth: point.depth };
  }

  // W16（票 #189）：relaxRounds 只对「重排」生效 —— 首跑（还没 sized）必须全轮次
  // 定版，拖拽窗口传 ORBIT_ROUNDS_DRAG，稳态 / 收敛补量都是全轮次。
  function measure(relaxRounds = ORBIT_ROUNDS) {
    const w = stage.clientWidth;
    const h = stage.clientHeight;
    if (!w || !h) return;
    scene.w = w;
    scene.h = h;
    scene.rect = stage.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (scene.dpr !== dpr || canvas.width !== Math.round(w * dpr)) {
      scene.dpr = dpr;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    stage.style.setProperty("--orbit-h", `${h}px`);
    // 先量一遍每条词卡的天然尺寸（松弛排版要靠它），之后把宽度钉住
    const firstLayout = !scene.sized;
    if (!scene.sized) {
      scene.sized = true;
      for (const entry of scene.items) {
        entry.w = Math.max(56, entry.plate.offsetWidth);
        entry.h = Math.max(26, entry.plate.offsetHeight);
      }
      for (const entry of scene.items) entry.plate.style.setProperty("--w", `${entry.w + 4}px`);
    }
    layoutPositions(firstLayout ? ORBIT_ROUNDS : relaxRounds);
    // 首次就位：直接落在目标位置，别从画面中心飞出来（进场交给 CSS 的错峰动画）
    for (const entry of scene.items) {
      const spot = project(entry);
      entry.x = entry.tx = spot.x;
      entry.y = entry.ty = spot.y;
      entry.s = entry.ts = spot.s;
      entry.o = entry.to = spot.o;
      entry.ry = entry.ry2 = spot.ry;
      entry.rx = entry.rx2 = spot.rx;
      entry.depth = spot.depth;
    }
    if (!scene.live) {
      scene.live = true;
      stage.classList.add("is-live");
      legend.textContent = orbitLegendText();
      // v14 T2：顺序通读开场。必须在这里消费 startAtFirst —— createOrbit 返回时 stage
      // 还没挂进 DOM，那时 clientWidth=0，measure() 会早退，卡片也还没就位。
      if (scene.startAtFirst) {
        scene.startAtFirst = false;
        scene.sequence = true; // 从顺序通读进来 = 两端夹住，走到头不绕回第 1 段
        focusCard(0);
      }
    }
    // v15 T2：阅读页停帧时窗口若变了，先放开跑一帧重新就位，收敛后再停
    // （否则纸面停在旧位置，直到用户回星图才被下一帧拉回来）。
    if (pausedBy.reading && scene.focusIndex >= 0) {
      setReadingPaused(false);
      armReadingPause(scene.items[scene.focusIndex]);
    }
    draw();
    schedule();
  }

  // 两两相斥（屏幕空间，盒式）：只推目标点，最多两轮，够解开自转造成的贴合
  function separate() {
    const items = scene.items;
    for (let round = 0; round < 2; round += 1) {
      for (let i = 0; i < items.length; i += 1) {
        const a = items[i];
        for (let j = i + 1; j < items.length; j += 1) {
          const b = items[j];
          const hw = (a.w * a.ts + b.w * b.ts) / 2 + 7;
          const hh = (a.h * a.ts + b.h * b.ts) / 2 + 8;
          const dx = b.tx - a.tx;
          const dy = b.ty - a.ty;
          const ox = hw - Math.abs(dx);
          const oy = hh - Math.abs(dy);
          if (ox <= 0 || oy <= 0) continue;
          if (ox < oy) {
            const push = (ox / 2) * (dx >= 0 ? 1 : -1);
            a.tx -= push;
            b.tx += push;
          } else {
            const push = (oy / 2) * (dy >= 0 ? 1 : -1);
            a.ty -= push;
            b.ty += push;
          }
        }
      }
    }
    // 别被推到面板外：贴着边也要露出整张词卡
    for (const entry of items) {
      const halfW = (entry.w * entry.ts) / 2 + 10;
      const halfH = (entry.h * entry.ts) / 2 + 12;
      const maxX = Math.max(0, scene.w / 2 - halfW);
      const maxY = Math.max(0, scene.h / 2 - halfH);
      entry.tx = orbitClamp(entry.tx, -maxX, maxX);
      entry.ty = orbitClamp(entry.ty, -maxY, maxY);
    }
  }

  // ---- 每帧 -----------------------------------------------------------------
  function frame(now) {
    if (!scene.alive) return;
    scene.raf = 0;
    const dt = scene.last ? Math.min(0.05, (now - scene.last) / 1000) : 0.016;
    scene.last = now;
    scene.time += dt;

    if (!scene.drag) {
      // 悬浮在某张卡上 / 正在阅读时停转：不然要点的字一直在跑
      if (!scene.reduced && scene.hover < 0 && scene.focusIndex < 0) {
        scene.angle += ORBIT_SPIN * dt;
      }
      if (Math.abs(scene.spinVel) > 0.0008) {
        scene.angle += scene.spinVel * dt;
        scene.spinVel *= Math.pow(0.015, dt); // 松手后的惯性
      }
    }
    scene.zoom += (scene.zoomTarget - scene.zoom) * Math.min(1, dt * 9);
    scene.hoverDim += ((scene.hover >= 0 ? 1 : 0) - scene.hoverDim) * Math.min(1, dt * 8);
    const dimTarget = scene.focusIndex >= 0 ? 1 : 0;
    scene.dim += (dimTarget - scene.dim) * Math.min(1, dt * 7);

    const ease = 1 - Math.exp(-dt * 10);
    for (const entry of scene.items) {
      entry.hover += ((scene.hover === entry.index ? 1 : 0) - entry.hover) * Math.min(1, dt * 14);
      entry.fk += ((scene.focusIndex === entry.index ? 1 : 0) - entry.fk) * Math.min(1, dt * 9);
      const target = project(entry);
      entry.tx = target.x;
      entry.ty = target.y;
      entry.ts = target.s;
      entry.to = target.o;
      entry.ry2 = target.ry;
      entry.rx2 = target.rx;
      entry.depth = target.depth;
    }
    // 星子之间互斥：自转时两张卡贴到一起就轻轻让开（只影响目标点，缓动去追）
    if (scene.focusIndex < 0) separate();
    for (const entry of scene.items) {
      entry.x += (entry.tx - entry.x) * ease;
      entry.y += (entry.ty - entry.y) * ease;
      entry.s += (entry.ts - entry.s) * ease;
      entry.o += (entry.to - entry.o) * ease;
      entry.ry += (entry.ry2 - entry.ry) * ease;
      entry.rx += (entry.rx2 - entry.rx) * ease;

      const node = entry.node;
      // 性能：同一串 style 不重复写。自转停下（悬停 / 阅读 / 松手）后位置很快收敛，
      // 以前每帧仍然给 80 张卡重写一遍同样的 transform / opacity，白白触发样式重算。
      // 写进去的字符串与原来逐字符相同，画面不变。
      const transform =
        `translate(-50%, -50%) translate3d(${entry.x.toFixed(2)}px, ${entry.y.toFixed(2)}px, 0) ` +
        `scale(${entry.s.toFixed(4)}) rotateY(${entry.ry.toFixed(2)}deg) rotateX(${entry.rx.toFixed(2)}deg)`;
      if (transform !== entry.transform) {
        entry.transform = transform;
        node.style.transform = transform;
      }
      // 透明度：默认压整张卡 —— 没有浮卡可见时与「压纸面」没有任何可见差异
      //（只在合成层边界有 ≤1 级舍入），但样式重算只落在卡本身，不会牵动卡里的每个字。
      // 只有**当前悬停的那张卡**带着可见浮卡，才把透明度压到纸面上，保证浮卡实心。
      const opacityOnPlate = scene.hover === entry.index;
      if (entry.opacityOnPlate !== opacityOnPlate) {
        entry.opacityOnPlate = opacityOnPlate;
        const keep = entry.opacityValue || entry.o.toFixed(3);
        if (opacityOnPlate) {
          node.style.opacity = "";
          entry.plate.style.opacity = keep;
        } else {
          entry.plate.style.opacity = "";
          node.style.opacity = keep;
        }
      }
      const opacity = entry.o.toFixed(3);
      if (opacity !== entry.opacityValue) {
        entry.opacityValue = opacity;
        if (opacityOnPlate) entry.plate.style.opacity = opacity;
        else node.style.opacity = opacity;
      }
      // 景深：越远的星子越虚（0.5px 一档，只在换档时写 style，不然滤镜每帧重算）。
      // 聚焦 / 悬停中的那张必须绝对清晰——它就是给人读的（浮卡和阅读页都是它的子节点）。
      const sharp = entry.fk > 0.02 || scene.hover === entry.index;
      const blur = sharp
        ? 0
        : Math.round(orbitClamp((entry.depth - 2160) / 150, 0, 1.2) * 2) / 2;
      if (blur !== entry.blur) {
        entry.blur = blur;
        node.style.filter = blur ? `blur(${blur}px)` : "";
      }
      const z = Math.round(2000 - entry.depth + entry.fk * 1500 + entry.hover * 2600);
      if (z !== entry.z) {
        entry.z = z;
        node.style.zIndex = String(z);
      }
    }

    draw();
    if (needsFrames()) schedule();
  }

  function needsFrames() {
    if (!scene.alive || scene.paused) return false;
    if (!scene.reduced) return true; // 自转一直在
    if (Math.abs(scene.spinVel) > 0.001) return true;
    if (Math.abs(scene.zoomTarget - scene.zoom) > 0.001) return true;
    if (scene.dim > 0.002) return true;
    for (const entry of scene.items) {
      if (Math.abs(entry.fk - (scene.focusIndex === entry.index ? 1 : 0)) > 0.002) return true;
      if (entry.hover > 0.002) return true;
    }
    return false;
  }

  function schedule() {
    if (!scene.alive || scene.paused || scene.raf) return;
    scene.raf = window.requestAnimationFrame(frame);
  }

  function draw() {
    const { w, h } = scene;
    if (!w || !h) return;
    const pal = scene.palette;
    ctx.setTransform(scene.dpr, 0, 0, scene.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const cosT = Math.cos(scene.tilt + scene.parY * 0.03);
    const sinT = Math.sin(scene.tilt + scene.parY * 0.03);
    const camZ = ORBIT_FOCAL / scene.zoom;
    const cx = w / 2;
    const cy = h / 2;
    const ca = Math.cos(scene.angle + scene.parX * 0.05);
    const sa = Math.sin(scene.angle + scene.parX * 0.05);
    const projectWorld = (dx, dy, dz) => {
      const sy = dy * cosT - dz * sinT;
      const depth = camZ + dy * sinT + dz * cosT;
      const s = ORBIT_FOCAL / Math.max(1, depth);
      return { x: cx + dx * s, y: cy + sy * s, depth, s };
    };

    // 星线：最近邻连成星座。位置按盘面算，线随盘一起转；悬停时它那几条点亮。
    ctx.lineWidth = 1;
    for (const [i, j] of scene.links) {
      const a = scene.items[i];
      const b = scene.items[j];
      if (!a || !b) continue;
      const pa = projectPoint(a.baseX, a.baseY, 0);
      const pb = projectPoint(b.baseX, b.baseY, 0);
      const hot = scene.hover === i || scene.hover === j;
      ctx.globalAlpha = scene.focusIndex >= 0 ? 0.25 : hot ? 1 : 0.55;
      ctx.strokeStyle = hot ? pal.linkHot : pal.link;
      ctx.beginPath();
      ctx.moveTo(pa.x, pa.y);
      ctx.lineTo(pb.x, pb.y);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;

    // 轨道环：三圈很淡的椭圆，给出「星盘」的空间感
    ctx.lineWidth = 1;
    for (const [ring, frac] of [0.34, 0.67, 1].entries()) {
      ctx.beginPath();
      for (let step = 0; step <= 96; step += 1) {
        const a = (step / 96) * Math.PI * 2;
        const x0 = Math.cos(a) * scene.spanX * frac;
        const y0 = Math.sin(a) * scene.spanY * frac;
        const point = projectWorld(x0 * ca - y0 * sa, x0 * sa + y0 * ca, 0);
        if (step === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      }
      ctx.strokeStyle = ring === 2 ? pal.ringHot : pal.ring;
      ctx.stroke();
    }
    // 光核外那一小圈：整张图的眼睛


    // 星尘：跟着盘面差速转，深度决定大小与亮度
    for (const particle of scene.dust) {
      const a = particle.a + scene.angle * particle.speed;
      const point = projectWorld(
        Math.cos(a) * scene.spanX * particle.r,
        Math.sin(a) * scene.spanY * particle.r,
        particle.z
      );
      const size = particle.size * point.s;
      // 星尘也会闪：同一颗按自己的相位明暗
      const twinkle = 0.72 + 0.28 * Math.sin(scene.time * 1.4 + particle.a * 5);
      ctx.globalAlpha =
        scene.focusIndex >= 0
          ? particle.alpha * (1 - scene.dim) * 0.6 * twinkle
          : particle.alpha * orbitClamp(0.35 + point.s - 0.6, 0.12, 1) * twinkle;
      ctx.fillStyle = particle.speed > 1 ? pal.ember : pal.dust;
      ctx.fillRect(point.x, point.y, Math.max(0.7, size), Math.max(0.7, size));
    }
    ctx.globalAlpha = 1;
  }

  // ---- 交互 -----------------------------------------------------------------
  function onPointerDown(event) {
    if (scene.focusIndex >= 0) return; // v14：阅读时不拖拽（阅读页内选字/按钮本来就走上面一层）
    if (event.button !== undefined && event.button !== 0) return;
    if (event.target.closest(".orbit-read")) return; // 阅读页里要能选字 / 点按钮
    scene.drag = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      moved: 0,
      at: performance.now(),
      captured: false,
    };
    stage.classList.add("is-dragging");
  }

  function onPointerMove(event) {
    // 鼠标在面板里的位置 → 极轻的视差（整盘跟着偏一点，画面就活了）
    const rect = scene.rect;
    if (rect) {
      scene.parX = ((event.clientX - rect.left) / Math.max(1, rect.width)) * 2 - 1;
      scene.parY = ((event.clientY - rect.top) / Math.max(1, rect.height)) * 2 - 1;
    }
    if (!scene.drag || scene.drag.id !== event.pointerId) {
      schedule();
      return;
    }
    const dx = event.clientX - scene.drag.x;
    const dy = event.clientY - scene.drag.y;
    scene.drag.x = event.clientX;
    scene.drag.y = event.clientY;
    scene.drag.moved += Math.abs(dx) + Math.abs(dy);
    // 真开始拖了才抓指针：一按下就抓会把 click 抢到舞台身上（点卡片就失效）
    if (!scene.drag.captured && scene.drag.moved > 4) {
      scene.drag.captured = true;
      stage.setPointerCapture?.(event.pointerId);
    }
    scene.angle += dx * 0.006;
    scene.tilt = orbitClamp(scene.tilt + dy * 0.004, 0.14, 1.42);
    const now = performance.now();
    const dt = Math.max(0.008, (now - scene.drag.at) / 1000);
    scene.drag.at = now;
    scene.spinVel = orbitClamp((dx * 0.006) / dt, -5, 5);
    schedule();
  }

  function onPointerUp(event) {
    if (!scene.drag || scene.drag.id !== event.pointerId) return;
    if (scene.drag.moved > 8) scene.suppressUntil = performance.now() + 160; // 拖动不是点击
    if (scene.drag.captured) stage.releasePointerCapture?.(event.pointerId);
    scene.drag = null;
    stage.classList.remove("is-dragging");
    schedule();
  }

  // ---- 滚轮：未聚焦 = 缩放；阅读页纸面 = 边界续翻（v15 T2，票 #127）-------------
  // 手势状态（每次滚动手势一份）：方向、累计量、这次手势是否已经换过段。
  let wheelAt = 0;
  let wheelDir = 0;
  let wheelAccum = 0;
  let wheelFired = false;
  let wheelLockUntil = 0;

  function onWheel(event) {
    if (scene.focusIndex >= 0) {
      // A1：只有阅读页纸面响应换段（含原句与释义区）；聚焦态的舞台空白什么都不做
      const plate = event.target.closest(".orbit-plate.is-reading");
      if (!plate) return; // 不缩放、不换段
      if (!state.wheelNav) return; // A7：开关关掉 = 回到「只滚正文、不换段」
      const dir = event.deltaY > 0 ? 1 : event.deltaY < 0 ? -1 : 0;
      if (!dir) return; // 横向滚轮（deltaX）不参与换段
      const now = performance.now();
      // A3：事件间隔 < 180ms 视为同一手势；方向一变立刻重置累计与锁（反向能马上换）
      if (dir !== wheelDir || now - wheelAt > WHEEL_GESTURE_GAP_MS) {
        wheelDir = dir;
        wheelAccum = 0;
        wheelFired = false;
        wheelLockUntil = 0;
      }
      wheelAt = now;
      // A2：正文能滚且还没到该方向的边界 → 原生滚动（纯滚正文）。
      // 例外：指针停在纸面的脚注 / 元信息条带上（在 plate 内、不在 body 内）时，
      // 原生滚动只认事件目标的可滚祖先 —— 那不是 .orbit-read-body，滚轮会白滚。
      // 这里手动把它补到正文上（行模式按 ~16px/行折算），段不换。
      const body = plate.querySelector(".orbit-read-body");
      const canScroll = Boolean(body) && body.scrollHeight > body.clientHeight + 2;
      const atBoundary =
        !canScroll ||
        (dir > 0
          ? body.scrollTop + body.clientHeight >= body.scrollHeight - 2
          : body.scrollTop <= 0);
      if (!atBoundary) {
        if (body && !body.contains(event.target)) {
          event.preventDefault();
          body.scrollTop += event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
        }
        return;
      }
      // 到边界（或正文根本不可滚）：拦下原生滚动，按累计量判「换段意图」
      event.preventDefault();
      if (wheelFired || now < wheelLockUntil) return; // 一次手势最多换一段
      // 累计口径与阈值同单位：像素模式累计 |deltaY|（阈值 40），行模式累计行数（阈值 3）
      wheelAccum += Math.abs(event.deltaY);
      const need = event.deltaMode === 1 ? WHEEL_STEP_LINES : WHEEL_STEP_PX;
      if (wheelAccum < need) return;
      wheelFired = true;
      wheelLockUntil = now + WHEEL_LOCK_MS;
      pageByWheel(dir); // 与按钮 / ←→ 同一条 api.step 入口（不新增 orbit.step 字面量）
      return;
    }
    // 未聚焦：保持现有缩放分支原样
    event.preventDefault();
    scene.zoomTarget = orbitClamp(
      scene.zoomTarget * (event.deltaY > 0 ? 0.92 : 1.08),
      0.72,
      2.3
    );
    schedule();
  }

  stage.addEventListener("pointerdown", onPointerDown);
  stage.addEventListener("pointermove", onPointerMove);
  stage.addEventListener("pointerup", onPointerUp);
  stage.addEventListener("pointercancel", onPointerUp);
  stage.addEventListener("wheel", onWheel, { passive: false });
  stage.addEventListener("click", (event) => {
    // v14 #121：阅读页内按钮 / 选字、点聚焦卡本身都不算「点空白」
    if (event.target.closest(".orbit-read")) return;
    const card = event.target.closest(".orbit-card");
    if (card && card.classList.contains("is-focus")) return;
    if (performance.now() < scene.suppressUntil) return;
    // 点空白、或聚焦态点背景卡（背景卡已 pointer-events:none，这层是键盘/合成点击的兜底）
    // = 回星图；未聚焦时 unfocusCard() 本身是空操作
    unfocusCard();
  });
  stage.addEventListener("dblclick", (event) => {
    if (scene.focusIndex >= 0) return; // v14：阅读时不复位视角
    if (event.target.closest(".orbit-card")) return;
    scene.angle = -0.55;
    scene.tilt = ORBIT_TILT;
    scene.zoomTarget = 1;
    scene.spinVel = 0;
    schedule();
  });
  stage.addEventListener("pointerleave", () => {
    scene.hover = -1;
    scene.parX = 0;
    scene.parY = 0;
    schedule();
  });
  // W16（票 #189）：拖拽窗口时 ResizeObserver 每帧回调，原样直接 measure 会在回调里
  // 跑 160 轮 O(n²) 松弛，掉帧明显。两步：① 回调经 rAF 合并，一帧最多量一次；
  // ② SETTLE 窗口内的连续回调视为拖拽中，降轮次排版；停止变化后补一次全轮次，
  // 把拖拽期的低精度布局拉回正常质量（首跑不走这里，仍走下面的全轮次 measure）。
  let measureRaf = 0;
  let measureSettleTimer = 0;
  let measureSettleAt = 0;

  function scheduleMeasure() {
    if (measureRaf) return;
    measureRaf = window.requestAnimationFrame(() => {
      measureRaf = 0;
      const now = performance.now();
      const dragging = now < measureSettleAt;
      measureSettleAt = now + ORBIT_RESIZE_SETTLE_MS;
      measure(dragging ? ORBIT_ROUNDS_DRAG : ORBIT_ROUNDS);
      if (measureSettleTimer) window.clearTimeout(measureSettleTimer);
      measureSettleTimer = window.setTimeout(() => {
        measureSettleTimer = 0;
        measureSettleAt = 0;
        measure();
      }, ORBIT_RESIZE_SETTLE_MS + 20);
    });
  }

  scene.observer = new ResizeObserver(scheduleMeasure);
  scene.observer.observe(stage);

  measure();
  schedule();
  return api;
}

function skeletonView() {
  const box = el("div", "card skeleton");
  for (let index = 0; index < 3; index += 1) {
    const row = el("div", "skeleton-row");
    row.append(el("span", "skeleton-bar w1"));
    row.append(el("span", "skeleton-bar w2"));
    row.append(el("span", "skeleton-bar w3"));
    box.append(row);
  }
  return box;
}

function isFiltered() {
  return Boolean(
    state.query.trim() || state.tag || state.sessionId !== null || state.filter !== "all"
  );
}

function emptyView() {
  const box = el("div", "card empty");
  if (isFiltered()) {
    box.append(el("div", "empty-title", "没有匹配的词条"));
    // 空列表最气人的是「不知道被哪个条件筛没了」：把当前生效的条件写出来
    const conditions = [];
    if (state.query.trim()) conditions.push(`搜索「${state.query.trim()}」`);
    if (state.sessionId !== null) conditions.push(`会话「${sessionTitle(String(state.sessionId))}」`);
    if (state.tag) conditions.push(`标签 #${state.tag}`);
    if (state.filter === "due") conditions.push("待复习");
    if (state.filter === "today") conditions.push("今天");
    if (conditions.length) box.append(el("p", "hint", `当前条件：${conditions.join(" · ")}`));
    box.append(button("btn", "清除筛选", clearFilters));
  } else {
    box.append(el("div", "empty-title", "还没有词条"));
    box.append(
      el(
        "p",
        "hint",
        `按 ${hintText("hotkey", "^!1")} 呼出速记框，或 ${hintText("hotkey_capture", "^!2")} 直接截图取词（键位可在设置页改）`
      )
    );
  }
  return box;
}

function errorView() {
  const box = el("div", "error-box");
  box.append(el("div", null, `连不上数据中枢：${state.error}`));
  box.append(el("div", "hint", "确认 hub 在跑（hub.py --selftest 看状态），然后重试。"));
  box.append(button("btn", "重试", () => loadAll({ skeleton: true })));
  return box;
}

function placeholderCard(title, count, note) {
  const box = el("div", "card placeholder-card");
  if (count !== null) box.append(el("div", "placeholder-count", count));
  box.append(el("div", "empty-title", title));
  box.append(el("p", "hint", note));
  box.append(button("btn btn-primary", "回到列表", () => {
    window.location.hash = "#/list";
  }));
  return box;
}

function renderMain() {
  closePopover(); // 行重建 / 翻页 / 路由切换：锚点会失效，先收掉
  // W18（票 #189）：主区重建不再无条件掐朗读。「边听边改」时播放的落点在抽屉里
  //（owner 不在 main），主区怎么重建都拆不到它 —— 让这轮发音自然播完，保存 /
  // 补释义完成的 loadAll 从此不断音（抽屉自己的重建 renderDrawer 本来就不停播，
  // 两边口径自此一致）。owner 在主区（列表卡 / 星图 / 文章页 / 复习卡 / 选区朗读）
  // 时照旧停：那些节点马上被拆，视觉态挂不住，连播驱动器也跟着页面走；owner 为空
  // 而长播还在收尾的边缘态也照旧停，不留下没 UI 的声音。切路由 / 关抽屉各有自己的
  // stopPronounce，不走这里。
  if (!pronounceOwner || dom.main.contains(pronounceOwner)) stopPronounce();
  invalidateSelectionRead();
  orbitDestroy(); // 星图的画布与 rAF 必须跟着视图一起收掉
  // 窄窗布局用：复习 / 设置页隐藏侧栏，把屏幕留给当前任务（列表页仍保留目录）
  document.body.dataset.route = state.route;
  const orbitMode =
    state.route === "list" &&
    !state.loading &&
    !state.error &&
    state.records.length > 0 &&
    currentView() === "orbit";
  document.body.dataset.view = orbitMode ? "orbit" : "list";
  // v14 T3：外壳状态必须在主区重建前落定 —— 沉浸态的全幅高度链要从第一次绘制就生效
  applyShell(orbitMode);
  // v14 T2：startAtFirst 只对「下一次真正落进星图」有效；没进星图就作废，
  // 免得深链/按钮设的旗子留给后面的某次渲染（加载中的骨架不算，那时还分不出视图）
  if (!orbitMode && !state.loading) state.startAtFirst = false;
  clear(dom.main);
  // v6 §5.1：列表页收成阅读栏（卡片流），复习页 / 设置页仍用全宽
  const wrap = el(
    "div",
    state.route === "list"
      ? `main-wrap is-list${orbitMode ? " is-orbit" : ""}`
      : state.route === "settings"
        ? "main-wrap is-settings"
        : state.route === "article"
          ? "main-wrap is-article"
          : state.route === "backlog"
            ? "main-wrap is-pool"
            : "main-wrap is-review"
  );

  if (state.route === "review") {
    renderReview(wrap);
    dom.main.append(wrap);
    return;
  }

  if (state.route === "settings") {
    renderSettings(wrap);
    dom.main.append(wrap);
    return;
  }

  // v22 P1b（spec §8.2）：文章页 = 标题/统计/动作 + 待复核区 + 段落卡列表
  if (state.route === "article") {
    renderArticle(wrap);
    dom.main.append(wrap);
    return;
  }

  // v34 T6（票 #219）：待选句池（#/backlog，`#/pick/<id>` 为同一页面的别名）
  if (state.route === "backlog") {
    renderPool(wrap);
    dom.main.append(wrap);
    return;
  }

  // v19 T2（票 #153）：沉浸态星图用右缘会话坞替代左上胶囊；standard 列表页
  // （含非沉浸星图）保持原胶囊。applyShell 已在上方把 dataset.shell 落定。
  // 主区重建 = 上下文变化，坞一律收回（悬停预览 / 钉住都不跨渲染存活）。
  if (state.dock !== "hidden") setDock("hidden");
  wrap.append(
    document.body.dataset.shell === "immersive" ? orbitDock() : listHeader()
  );
  // v28：打开会话时的缺释义提示（精确 N 来自后端）。主区顶部一进页面就可见，
  // 不要求用户先点开某一段才碰到「补释义」。
  const sessionPlan = !state.loading && !state.error ? sessionRefillPlan() : null;
  if (sessionPlan) wrap.append(refillNotice(sessionPlan));
  if (state.loading) wrap.append(skeletonView());
  else if (state.error) wrap.append(errorView());
  else if (!state.records.length) wrap.append(emptyView());
  else if (orbitMode) wrap.append(orbitView());
  else wrap.append(groupedList());
  dom.main.append(wrap);
}

/* --- v22 P1b（票 #166 / spec §8.2）：文章页与段落卡 --------------------------- */

const PARA_TYPE_LABEL = { body: "正文", heading: "标题", list: "列表", quote: "引用" };

function articleActions() {
  // P3（票 #168 / spec §8.5）：「▶ 播放」按段连播；P2b（票 #167 / spec §8.2）：
  // 「重新分段」「删除本文」接上窄写 HTTP（P2a 契约）——不再是 disabled 占位。
  const box = el("div", "article-actions");
  const articleId = state.article ? state.article.id : state.articleId;
  const play = button("btn btn-subtle article-play-btn", "▶ 播放", () => {
    startArticleRead(0);
  });
  play.title = "从第 1 段开始连播（「只播有词段」关 = 整篇全文；播到末段停止）";
  play.setAttribute("aria-label", "从第 1 段开始连播本文");
  box.append(play);
  const listen = button("btn btn-subtle article-listen-btn", "🎧 听看高亮", () =>
    startArticleHighlightReview()
  );
  listen.title =
    "只播本文的高亮词条（词 → 短语 → 句；按最早收录顺序），每条配释义弹窗与发音";
  listen.setAttribute("aria-label", "听看复习本文的高亮词条（按最早收录顺序）");
  box.append(listen);
  box.append(articleWordedToggle());

  // 重新分段 → POST /api/articles/{id}/resegment（P2a）：成功重拉详情 + 刷新侧栏；
  // 409 是业务态（旧文章没有原始分段输入）—— 用中文说清楚，不伪造、不破坏页面。
  const reseg = button("btn btn-subtle", "重新分段", () =>
    resegmentArticle(articleId, reseg)
  );
  reseg.dataset.articleAction = "resegment";
  reseg.title =
    "用创建时保存的原始分段输入重跑分段（旧 v21 文章没有原始输入 → 会提示请重新录入）";
  box.append(reseg);

  // 删除本文 → 沿用取词台既有 is-armed 两段点击（spec §12 / app.js 会话删除同款）：
  // 第一下按钮变红进入「确认删除」、旁边出现「取消」；第二下才真的发 DELETE。
  // `disarm()` 是**唯一**退出确认态的入口：取消、删除失败都走它 —— 失败后闭包 `armed`
  // 必须复位，否则下一次单击会直接发 DELETE、绕过二次确认（review 实质缺陷）。
  const del = button("btn btn-subtle btn-danger", "删除本文", null);
  del.dataset.articleAction = "delete";
  del.title = "删除本文（词条保留，二次确认后生效）";
  let armed = false;
  const disarm = () => {
    armed = false;
    del.textContent = "删除本文";
    del.classList.remove("is-armed");
    del.disabled = false;
    reseg.disabled = false;
    cancel.hidden = true;
    cancel.remove();
  };
  const cancel = button("btn btn-subtle", "取消", () => disarm());
  cancel.dataset.articleAction = "delete-cancel";
  cancel.hidden = true;
  del.addEventListener("click", () => {
    if (del.disabled) return;
    if (!armed) {
      armed = true;
      del.textContent = "确认删除";
      del.classList.add("is-armed");
      reseg.disabled = true; // 确认期间别让两件事并行
      cancel.hidden = false;
      box.append(cancel);
      return;
    }
    deleteArticle(articleId, del, reseg, disarm);
  });
  box.append(del);
  return box;
}

// v22 P2b（spec §8.2）：重新分段。成功 = toast 计数 + 重拉详情 + 重拉侧栏文章分组；
// 失败（尤其 409 no/bad segmentation source）只提示，不动当前页面（旧文章降级如实显示）。
async function resegmentArticle(id, btn) {
  if (!id || (btn && btn.disabled)) return;
  const label = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "重新分段中…";
  }
  try {
    const payload = await api(`/api/articles/${id}/resegment`, { method: "POST" });
    // spec §14 第 7 步的验收文案：N 段 · 重映射 X 条 · 待复核 Y 条
    toast(
      `${payload.segments} 段 · 重映射 ${payload.remapped} 条 · 待复核 ${payload.needs_review} 条`
    );
    await loadArticle(id);
    await loadAll(); // 侧栏「文章」分组跟着刷新（段数/词数变了）
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    if (message === "no segmentation source") {
      toast("该文没有原始分段输入，无法重新分段（请重新录入）");
    } else if (message === "bad segmentation source") {
      toast("原始分段输入损坏，无法重新分段；词条未受影响");
    } else if (message === "not found") {
      toast("文章不存在（可能已被删除）");
    } else {
      toast(`重新分段失败：${message}`);
    }
  } finally {
    // 成功路径页面已重绘（旧按钮脱离文档），只复原仍挂在页面上的按钮
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.textContent = label;
    }
  }
}

// v22 P2b（spec §8.2 §12）：删除文章（词条保留）。DELETE ?keep_records=1 → 回到列表 +
// 侧栏刷新 + toast；确认按钮此时已由 is-armed 变为「确认删除」。
// `onFailure` = 调用方（articleActions）的 `disarm()`：删除失败必须**退出确认态**再让
// 用户操作，否则闭包里的 armed 还留着 → 下一次单击直接又发 DELETE（绕过 §12 二次确认）。
async function deleteArticle(id, btn, resegBtn, onFailure) {
  if (!id) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "删除中…";
  }
  if (resegBtn) resegBtn.disabled = true;
  try {
    await api(`/api/articles/${id}?keep_records=1`, { method: "DELETE" });
    toast("文章已删除，词条保留");
    // 路由回列表：hashchange 会 loadAll；这里再显式拉一次，保证侧栏/列表即时刷新。
    window.location.hash = "#/list";
    await loadAll({ skeleton: true });
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    if (message === "not found") toast("文章不存在（可能已被删除）");
    else toast(`删除失败：${message}`);
    // 唯一收口：复位 armed + 文案/class/取消按钮（review 实质缺陷的修法）
    if (onFailure) onFailure();
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.classList.remove("is-armed");
    }
    if (resegBtn && resegBtn.isConnected) resegBtn.disabled = false;
  }
}

function articleTotalWords(article) {
  return (
    article.paragraphs.reduce(
      (sum, paragraph) => sum + paragraph.records.length,
      0
    ) + article.needs_review.length
  );
}

function articleMeta(article) {
  const meta = el("div", "article-meta");
  meta.append(
    el(
      "span",
      null,
      `${article.stats.segments} 段 · ${articleTotalWords(article)} 词 · 共 ${article.stats.chars} 字`
    )
  );
  if (article.stats.segments) {
    meta.append(el("span", null, `平均 ${article.stats.avg_chars} 字/段`));
  }
  if (article.source_app) meta.append(el("span", null, `来源 ${article.source_app}`));
  meta.append(
    el("span", "article-format", article.format === "manual" ? "粘贴" : "OCR")
  );
  if (article.stats.suspicious.length) {
    const warn = el("span", "article-warn", `异常 ${article.stats.suspicious.length} 段`);
    warn.title = "空段 / 超长段（接口 stats.suspicious 的明细）";
    meta.append(warn);
  }
  return meta;
}

// spec §8.2：待复核区 —— 重分段后原句没在新段落里定位到的词条，单独成区（不丢词）。
// v22 #171：单卡视图里这段区域在纸面上方，默认收起成一条「待复核 N 条」，展开才占地方；
// 0 条时 renderArticle 根本不渲染（见上）。
function articleNeedsReview(records) {
  const box = el("details", "needs-review");
  const head = el("summary", "needs-review-head");
  head.append(el("span", "needs-review-title", `待复核 ${records.length} 条`));
  head.append(
    el(
      "span",
      "needs-review-note",
      "重分段后原句未在新段落中定位到；词条保留，不参与段落卡排序"
    )
  );
  box.append(head);
  const body = el("div", "needs-review-body");
  for (const record of records) {
    const row = el("div", "needs-review-row");
    const main = el("div", "needs-review-main");
    main.append(el("div", "needs-review-target", record.target));
    const meaning = el(
      "span",
      "needs-review-meaning",
      meaningLabel(record, "（暂无释义）")
    );
    if (!record.meaning) meaning.classList.add("is-empty");
    main.append(meaning);
    // v23 P1：待复核区同样给单条「补」（行尾的「详情」保持不动）
    if (!record.meaning) main.append(refillInlineButton(record));
    const machineBadge = meaningMachineBadge(record); // #172：机翻小标（与释义同一行）
    if (machineBadge) main.append(machineBadge);
    if (record.context) {
      main.append(el("div", "needs-review-context", record.context));
    }
    row.append(main);
    row.append(button("btn btn-subtle", "详情", () => openDrawer(record.id)));
    body.append(row);
  }
  box.append(body);
  return box;
}

// v26 §3.1：文章段与段卡共用同一套 mark 构造 —— 高亮观感 / 配色 / hover / 朗读态
// 全部交给 `.ctx-mark` 与 `--mark-bg` / `--mark-bg-overlap`（不再按 record.id 取色）。
function paragraphMark(record, text, segment) {
  return ctxMark(record, text, segment);
}

function paragraphOverlapMark(records, text, segment) {
  return overlapMark(records, text, segment);
}

// 段内高亮：后端给了每条的 start_in_paragraph/end_in_paragraph（前端零搜索），
// 复用 sentenceSegments 的端点切分；找不到偏移（-1）的词条不进卡面，只在卡头提示。
function articleParagraphBody(paragraph) {
  const body = el("div", "para-text");
  const located = [];
  for (const record of paragraph.records) {
    const start = Number(record.start_in_paragraph);
    const end = Number(record.end_in_paragraph);
    if (start >= 0 && end > start) located.push({ record, span: { start, end } });
  }
  located.sort(
    (left, right) => left.span.start - right.span.start || left.span.end - right.span.end
  );
  const segments = sentenceSegments(paragraph.text, located);
  if (!segments.length) {
    body.append(document.createTextNode(paragraph.text));
    return body;
  }
  // v27 §3.1：segment 一并传给 mark —— 文章段与列表卡同规则（碎片段播最短覆盖词条）
  for (const segment of segments) {
    if (!segment.records.length) body.append(document.createTextNode(segment.text));
    else if (segment.records.length === 1)
      body.append(paragraphMark(segment.records[0], segment.text, segment));
    else body.append(paragraphOverlapMark(segment.records, segment.text, segment));
  }
  // v22 P3（票 #168 / spec §8.5）：给每个记录 span 记它在段原文里的字符区间 —— 连播时
  // 「当前分片命中的记录 span 加 is-speaking」按区间求交即可（纯数据属性，不改文本、
  // 不触发重排）。偏移与 article-read.js 分片用的 [start,end) 同一坐标系。
  let cursor = 0;
  for (const node of Array.from(body.childNodes)) {
    const text = String(node.textContent == null ? "" : node.textContent);
    if (node.nodeType === 1 && node.classList && node.classList.contains("ctx-mark")) {
      node.dataset.paraStart = String(cursor);
      node.dataset.paraEnd = String(cursor + text.length);
    }
    cursor += text.length;
  }
  return body;
}

function paragraphTypeChip(type) {
  const key = PARA_TYPE_LABEL[type] ? type : "body";
  return el("span", `para-type para-type-${key}`, PARA_TYPE_LABEL[key]);
}

function articleParagraphCard(paragraph) {
  const card = el("section", "para-card");
  card.dataset.index = String(paragraph.index);
  const wordless = paragraph.records.length === 0;
  if (wordless) card.classList.add("is-wordless");
  if (paragraph.suspicious) card.classList.add("is-suspicious");

  const head = el("div", "para-head");
  head.append(el("span", "para-no", `第 ${paragraph.index + 1} 段`));
  head.append(paragraphTypeChip(paragraph.type));
  head.append(
    el("span", "para-words", wordless ? "未记词" : `本段 ${paragraph.records.length} 词`)
  );
  // v22 P3（票 #168 / spec §8.5 / D20）：点任意段落卡 = 从该段开始播。热区 = **卡头** +
  // 卡面**正文空白文本区**（修复轮补上，见下方 textBox 的 click 委托）+ 这枚明示的 ▶ 按钮
  // （键盘 / 自动化）。记录 span（点词 = 听单词）、展开按钮、拖选 / 复制不接管 —— 这不是
  // 「整卡拦点击」，而是按落点区分：落在可交互元素上就归它们，落在普通正文上就起播。
  const playSegment = button("para-play", "▶", () => startArticleRead(paragraph.index));
  playSegment.title = `从第 ${paragraph.index + 1} 段开始连播`;
  playSegment.setAttribute("aria-label", `从第 ${paragraph.index + 1} 段开始连播`);
  head.append(playSegment);
  if (!wordless) {
    const listenSegment = button("para-play para-listen", "🎧", () =>
      startArticleParagraphHighlightReview(paragraph.index)
    );
    listenSegment.title = `只播第 ${paragraph.index + 1} 段的高亮词条（词 → 短语 → 句；按最早收录顺序）`;
    listenSegment.setAttribute(
      "aria-label",
      `听看复习第 ${paragraph.index + 1} 段的高亮词条（按最早收录顺序）`
    );
    head.append(listenSegment);
  }
  head.title = "点卡头从本段开始连播";
  const missing = paragraph.records.filter(
    (record) => Number(record.start_in_paragraph) < 0
  );
  if (missing.length) {
    const node = el(
      "span",
      "para-miss",
      `未定位 ${missing.map((record) => record.target).join(" / ")}`
    );
    node.title = "这些词条在段内没定位到（原句可能被改过）；词条本身没有丢";
    head.append(node);
  }
  if (paragraph.suspicious) {
    head.append(
      el(
        "span",
        "para-warn",
        paragraph.chars === 0 ? "空段（数据异常）" : "段过长"
      )
    );
  }
  card.append(head);
  // 点击委托放在卡头上：按钮 / 链接 / 输入 / 展开开关与拖选都不接管（见上）。
  head.addEventListener("click", (event) => {
    if (
      event.target instanceof Element &&
      event.target.closest("button, a, input, label, select")
    ) {
      return;
    }
    if (hasSelection()) return; // 拖选松手落在卡头上：不抢用户的选区
    startArticleRead(paragraph.index);
  });

  const textBox = el("div", "para-body");
  textBox.append(articleParagraphBody(paragraph));
  if (paragraph.chars === 0) {
    textBox.append(el("div", "para-empty", "空段（数据异常）"));
  }
  // 修复轮（review Spec (b) / D20）：正文空白文本区也起播，热区因此覆盖卡面。
  // 记录 span（点词听单词 / 看释义）、展开按钮、输入控件与拖选 / 复制一律不接管。
  textBox.addEventListener("click", (event) => {
    if (
      event.target instanceof Element &&
      event.target.closest("button, a, input, label, select, .ctx-mark")
    ) {
      return;
    }
    if (hasSelection()) return; // 拖选松手 / 双击选词：不抢选区
    startArticleRead(paragraph.index);
  });
  card.append(textBox);

  // spec §8.2：无词段淡显；「展开 / 收起」保留（v22 #171：单卡视图默认展开 —— 卡内就是
  // 当前段全文，收起只是给超长无词段留一个手动收纳，不再默认省一屏）。
  if (wordless) {
    const toggle = button("para-toggle", "收起", () => {
      const collapsed = card.classList.toggle("is-collapsed");
      toggle.textContent = collapsed ? "展开" : "收起";
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    });
    toggle.setAttribute("aria-expanded", "true");
    toggle.title = "展开 / 收起本段原文";
    head.append(toggle);
  }
  return card;
}

function renderArticle(wrap) {
  // v22 P3：重渲染 = 上下文变化（换文章 / 重新加载 / 从别的路由回来）—— 上一轮连播的
  // DOM 锚点已经失效，状态一起收掉（静默：重渲染不是用户点了「停止」）。
  if (articleRead) resetArticleReadSilently();
  const article = state.article;
  const head = el("div", "article-head");
  const back = button("btn btn-back", "← 返回", () => {
    window.location.hash = "#/list";
  });
  back.title = "回到列表（Esc）";
  head.append(back);
  const titles = el("div", "article-titles");
  titles.append(el("h2", "list-title", article ? article.title : "文章"));
  if (article) titles.append(articleMeta(article));
  head.append(titles);
  if (article) head.append(articleActions());
  wrap.append(head);
  if (article) wrap.append(articlePlaybarNode());

  if (state.articleError) {
    const box = el("div", "error-box");
    box.append(el("div", null, `文章加载失败：${state.articleError}`));
    box.append(el("div", "hint", "文章可能已被删除，或数据中枢暂时不可用。"));
    box.append(
      button("btn", "重试", () =>
        loadArticle(state.articleId, { skeleton: true })
      )
    );
    box.append(
      button("btn btn-subtle", "回到列表", () => {
        window.location.hash = "#/list";
      })
    );
    wrap.append(box);
    return;
  }

  if (!article) {
    wrap.append(skeletonView()); // 首次加载（深链直接打开 / 前进后退都走这里）
    return;
  }

  if (article.needs_review.length) {
    wrap.append(articleNeedsReview(article.needs_review));
  }
  // v22 #171（用户验收反馈）：不再竖向铺全部段落卡 —— 一次一张纸面，滚轮 / 按钮 / 播放
  // 三条路都只换「当前段」。默认展开的待复核条在纸面上方，0 条时整块不渲染。
  wrap.append(articleOrbitView(article));
  renderArticlePlaybar();
}

// v31 #196：变更后的保位原位刷新 —— 只换 article-orbit 子树（进度 + 提示条 + 当前卡），
// header / 侧栏 / 列表都不动。当前卡仍交给 articleOrbitView 里既有的单卡渲染。
function refreshArticleSurface() {
  if (!state.article) return;
  const wrap = dom.main.querySelector(".main-wrap.is-article");
  if (!wrap) return;
  // 连播中：旧分片高亮挂在即将被换掉的卡上 —— 先静默收掉计划与音频，
  // 别让 driver 醒来后指向已脱离文档的旧 DOM（用户故事 9）。
  if (articleRead) {
    resetArticleReadSilently();
    stopPronounce();
  }
  invalidateSelectionRead();
  const body = articleBodyNode();
  const scroll = body ? body.scrollTop : 0;
  const orbit = wrap.querySelector(".article-orbit");
  const fresh = articleOrbitView(state.article);
  if (orbit) orbit.replaceWith(fresh);
  else wrap.append(fresh);
  const freshBody = articleBodyNode();
  if (freshBody) freshBody.scrollTop = scroll;
  syncArticleNeedsReview();
  renderArticlePlaybar();
}

// 保位刷新后待复核条也要与最新数据一致：0 条移除，有变化就原位替换。
function syncArticleNeedsReview() {
  const wrap = dom.main.querySelector(".main-wrap.is-article");
  if (!wrap || !state.article) return;
  const records = state.article.needs_review || [];
  if (!records.length) {
    const existing = wrap.querySelector(".needs-review");
    if (existing) existing.remove();
    return;
  }
  replaceOrInsertBlock(
    wrap,
    ".needs-review",
    articleNeedsReview(records),
    ".article-orbit"
  );
}

/* --- v22 #171（用户验收反馈）：文章页 = 星图阅读式单卡 -------------------------
 * 一次只渲染当前段一张纸面卡（卡面复用 articleParagraphBody：同款高亮 / 重叠 / 点词发音 /
 * hover 释义 / 类型 chip / 无词段淡显 / 异常标黄全都不动）。翻段只有一条入口
 * `articleStep()`：可见的上一段/下一段按钮、滚轮（先滚正文、到底/到顶再切）、P3 播放中的
 * 播放条按钮都落在这里。滚轮手势参数（WHEEL_*）与星图阅读页共用（ADR 0007），但**不复制**
 * canvas / 星盘 / 聚焦飞行 —— 文章页没有可旋转的场景，只有一张纸。
 */

const ARTICLE_CARD_FADE_MS = 140;

function articleParagraphCount() {
  return state.article && state.article.paragraphs ? state.article.paragraphs.length : 0;
}

// 可滚的正文 = 当前这张卡的 .para-body（每次换段都重建 DOM，所以每次重新取，不缓存节点）。
function articleBodyNode() {
  const list = document.querySelector(".article-paragraphs.is-single");
  return list ? list.querySelector(".para-card .para-body") : null;
}

function syncArticleProgress(root = document) {
  const total = articleParagraphCount();
  const label = root.querySelector(".article-progress-label");
  if (label) label.textContent = total ? `第 ${state.articleIndex + 1} / ${total} 段` : "";
  const prev = root.querySelector(".article-prev");
  if (prev) prev.disabled = state.articleIndex <= 0;
  const next = root.querySelector(".article-next");
  if (next) next.disabled = state.articleIndex >= total - 1;
}

// 换段的新纸淡入（与星图阅读页 focusCard 的 playReadIn 同一种「只动 opacity/transform」；
// prefers-reduced-motion 直接瞬切）。
function playArticleCardIn(node) {
  if (!node || typeof node.animate !== "function") return;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  node.animate(
    [
      { opacity: 0, transform: "translateY(6px)" },
      { opacity: 1, transform: "none" },
    ],
    { duration: ARTICLE_CARD_FADE_MS, easing: "ease-out" }
  );
}

// 换到第 index 段：只重建那一张卡 + 同步进度。越界在这里夹住（提示由各调用方给）。
// `list` / `root` 只在首次（视图还没进文档时）由 articleOrbitView 传进来；之后走 document。
function showArticleParagraph(index, { animate = true, list = null, root = null } = {}) {
  const total = articleParagraphCount();
  if (!total) return;
  const clamped = Math.min(Math.max(Number(index) || 0, 0), total - 1);
  const changed = clamped !== state.articleIndex;
  state.articleIndex = clamped;
  const target = list || document.querySelector(".article-paragraphs.is-single");
  if (!target) return;
  // 换段 = 上下文变化：旧 mark 随卡一起移除，悬停 popover 必须一起收（同星图 focusCard）。
  closePopover();
  clear(target);
  const paragraph = state.article.paragraphs[clamped];
  if (paragraph) target.append(articleParagraphCard(paragraph));
  syncArticleProgress(root || document);
  // 新的一段从头读（与星图阅读页换段同手感）：正文滚回顶部。
  const body = target.querySelector(".para-card .para-body");
  if (body) body.scrollTop = 0;
  if (animate && changed) playArticleCardIn(target.firstElementChild);
}

// 翻段唯一入口：连播中走 P3 的同一份全篇计划（articleReadStep，不新建 plan、不归零计数）；
// 没在播就只换可见卡。
function articleStep(delta) {
  if (articleRead) {
    articleReadStep(delta);
    return;
  }
  articleStepSegment(delta);
}

function articleStepSegment(delta) {
  const total = articleParagraphCount();
  if (!total) return;
  const next = state.articleIndex + delta;
  if (next < 0) {
    toast("已经是第一段");
    return;
  }
  if (next >= total) {
    toast("已经是最后一段");
    return;
  }
  showArticleParagraph(next);
}

function articleOrbitView(article) {
  const view = el("section", "article-orbit");
  const total = article.paragraphs.length;
  const progress = el("div", "article-progress");
  const prev = button("btn btn-subtle article-prev", "◀ 上一段", () => articleStep(-1));
  prev.title = "上一段（滚轮向上同效）";
  const next = button("btn btn-subtle article-next", "下一段 ▶", () => articleStep(1));
  next.title = "下一段（滚轮向下同效）";
  progress.append(prev, el("span", "article-progress-label", ""), next);
  if (state.wheelNav) {
    progress.append(
      el("span", "article-progress-hint", "滚轮 = 上一段 / 下一段（正文能滚时先滚正文）")
    );
  }
  view.append(progress);
  // v28：文章级提示条贴在当前段落卡上方 —— 打开文章 / 翻到任意一段都能看见。
  const refillPlan = articleRefillPlan(article);
  if (refillPlan) view.append(refillNotice(refillPlan));
  const list = el("div", "article-paragraphs is-single");
  view.append(list);
  if (total) {
    // 首次渲染时视图还没进文档（renderArticle 之后才 append）：把 list / 进度根节点
    // 直接传进去，别让 showArticleParagraph 靠 document 找自己。
    showArticleParagraph(state.articleIndex, { animate: false, list, root: view });
  } else {
    state.articleIndex = 0;
    view.append(el("div", "article-empty", "这篇文章没有可显示的段落。"));
  }
  attachArticleWheelNav(view);
  return view;
}

/* --- v22 P3（票 #168 / spec §8.5）：文章页按自然段连播 -------------------------
 * 单位 = 自然段：按 `article.paragraphs` 顺序，每段只读一次（根治「同段多词重复朗读」）；
 * 范围 = 该文章，不跨文章；播到末段停止。播放计划 / 分片由 `article-read.js` 的纯函数
 * 产出（Node 证据脚本 plan_check.js 直接测它）；真正出声复用现有 `pronounceLong`
 * （TTS 缓存 / 前瞻 / 暂停 / 503 冷却 + speechSynthesis 兜底全都不另起一套）。
 */

// 偏好键：沿用现有 `workbench-*` 前缀（theme / view / shell / sidebar-fold 同族）。
const ARTICLE_WORDED_ONLY_KEY = "workbench-article-worded-only";
// 连播状态：{ token, articleId, plan, pos, chunkIndex }。token 认领本轮会话 —— 重渲染 /
// 切换开关 / 用户点别的发音都会让旧 driver 在 await 回来时按 token 或序号自行退出。
let articleRead = null;
let articleReadToken = 0;

function readArticleWordedOnly() {
  try {
    return window.localStorage.getItem(ARTICLE_WORDED_ONLY_KEY) === "1";
  } catch (error) {
    return false; // 隐私模式等 localStorage 不可用：偏好只在本会话生效（同主题 / 折叠的纪律）
  }
}

function saveArticleWordedOnly() {
  try {
    window.localStorage.setItem(
      ARTICLE_WORDED_ONLY_KEY,
      state.articleWordedOnly ? "1" : "0"
    );
  } catch (error) {
    // 忽略：见上
  }
}

// 「只播有词段」toggle（默认关 = 整篇全文）；开关状态立即按新范围生效（播放中 = 从当前段起）。
function articleWordedToggle() {
  const box = el("label", "article-worded-toggle");
  const input = el("input", "article-worded-input");
  input.type = "checkbox";
  input.checked = state.articleWordedOnly;
  input.addEventListener("change", () => setArticleWordedOnly(input.checked));
  box.append(input);
  box.append(el("span", null, "只播有词段"));
  box.title = "默认关：整篇全文；开：只连播有记录的自然段（状态记在本地）";
  return box;
}

function setArticleWordedOnly(on) {
  state.articleWordedOnly = Boolean(on);
  saveArticleWordedOnly();
  const session = articleRead;
  if (!session) {
    renderArticlePlaybar(); // 没在播：只落偏好，播放条备注同步（隐藏时也无害）
    return;
  }
  // spec §8.5：开关后立即按新范围生效 —— 播放中切换 = 从**当前段**起按新集合继续。
  const current = session.plan[session.pos];
  startArticleRead(current ? current.index : 0);
}

function articleCard(index) {
  return document.querySelector(
    `.article-paragraphs .para-card[data-index="${Number(index)}"]`
  );
}

function articlePlaybarNode() {
  const bar = el("div", "article-playbar");
  bar.hidden = true;
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "连播控制");
  bar.append(el("span", "article-playbar-seg"));
  bar.append(el("span", "article-playbar-chunk"));
  const prev = button("btn btn-subtle article-play-prev", "◀ 上一段", () =>
    articleReadStep(-1)
  );
  prev.title = "上一段（连播中可用；滚轮向上同效）";
  const toggle = button("btn btn-subtle article-play-toggle", "⏸ 暂停", () =>
    articleReadTogglePause()
  );
  toggle.title = "暂停 / 继续（Esc 停止并退出播放条）";
  const next = button("btn btn-subtle article-play-next", "下一段 ▶", () =>
    articleReadStep(1)
  );
  next.title = "下一段（连播中可用；滚轮向下同效）";
  const stop = button("btn btn-subtle article-play-stop", "■ 停止", () =>
    articleReadStop()
  );
  stop.title = "停止连播（Esc 同效）";
  bar.append(prev, toggle, next, stop);
  bar.append(el("span", "article-playbar-note"));
  return bar;
}

function renderArticlePlaybar() {
  const bar = document.querySelector(".article-playbar");
  if (!bar) return;
  const session = articleRead;
  const on = Boolean(session);
  bar.hidden = !on;
  bar.classList.toggle("is-on", on);
  if (!on) return;
  const segment = session.plan[session.pos];
  const total = session.plan.length;
  const segNode = bar.querySelector(".article-playbar-seg");
  if (segNode) {
    segNode.textContent = segment
      ? `第 ${segment.index + 1} 段 · ${session.pos + 1}/${total}`
      : "";
  }
  const chunkNode = bar.querySelector(".article-playbar-chunk");
  if (chunkNode) {
    chunkNode.textContent =
      segment && segment.chunks.length
        ? `分片 ${Math.min((session.chunkIndex || 0) + 1, segment.chunks.length)}/${segment.chunks.length}`
        : "空段";
  }
  const toggle = bar.querySelector(".article-play-toggle");
  if (toggle) toggle.textContent = pronouncePaused ? "▶ 继续" : "⏸ 暂停";
  const note = bar.querySelector(".article-playbar-note");
  if (note) note.textContent = state.articleWordedOnly ? "只播有词段：开" : "只播有词段：关";
  const prev = bar.querySelector(".article-play-prev");
  if (prev) prev.disabled = session.pos <= 0;
  const next = bar.querySelector(".article-play-next");
  if (next) next.disabled = session.pos >= total - 1;
}

// 清掉上一分片点亮的记录 span；卡片高亮单独收（换段时才动）。
// 只清**自己点亮过的**那些（`articleReadMarks`）：连播中用户点某个词听发音时
// `pronounce()` 也会给那枚 mark 挂 is-speaking —— 别把用户这一轮的播放态顺手摘掉。
let articleReadMarks = new Set();

function articleReadClearChunkMarks() {
  for (const mark of articleReadMarks) {
    if (mark !== pronounceOwner) mark.classList.remove("is-speaking");
  }
  articleReadMarks.clear();
}

function articleReadClearCard() {
  for (const card of document.querySelectorAll(".article-paragraphs .para-card.is-playing")) {
    card.classList.remove("is-playing");
  }
}

// 当前分片点亮：记录 span 的段内区间与分片区间有交集 → is-speaking（沿用现有 class；
// 区间来自 articleParagraphBody 写的 data-para-start / data-para-end，与分片同坐标系）。
function articleReadShowChunk(segment, chunkIndex) {
  articleReadClearChunkMarks();
  const chunk = segment.chunks[chunkIndex];
  const card = articleCard(segment.index);
  if (!chunk || !card) return;
  for (const mark of card.querySelectorAll(".para-text .ctx-mark")) {
    const start = Number(mark.dataset.paraStart);
    const end = Number(mark.dataset.paraEnd);
    if (!(end > start)) continue;
    if (start < chunk.end && chunk.start < end) {
      mark.classList.add("is-speaking");
      articleReadMarks.add(mark);
    }
  }
}

function finishArticleRead(token) {
  if (!articleRead || articleRead.token !== token) return;
  resetArticleReadSilently("finish");
  // 收尾路径必须真正停音频：末段自然播完时它已经结束（无副作用），但「末段再点下一段 /
  // 滚轮向下」是在**本段还在响**的时候越界收尾 —— 只收 UI 会留下继续朗读的尾音。
  stopPronounce();
  toast("已播到最后一段，连播结束");
}

// 内部收口（**不碰音频**）：重渲染 / 被别的发音顶掉时只清连播状态与视觉。
// 用户点「■ 停止」走的是 articleReadStop()（会 stopPronounce），语义不同别混用。
// 2026-11-06：`reason` 只用于日志 —— finish / stop / abort（重渲染、切路由、被别的发音顶掉）
// 三种收口以前在服务端完全看不出来，播到一半「凭空消失」无从定位。
function resetArticleReadSilently(reason = "abort") {
  cancelArticleReadGap(); // v30 T2：呼吸定时器不能比连播活得久（停止 / 换段 / 重渲染都走这里）
  const session = articleRead;
  articleRead = null;
  articleReadClearChunkMarks();
  articleReadClearCard();
  renderArticlePlaybar();
  if (session) {
    const event =
      reason === "finish"
        ? "article_finish"
        : reason === "stop"
          ? "article_stop"
          : "article_abort";
    logPlaybackEvent(event, { pos: Number(session.pos) || 0, reason });
  }
}

function articleReadStop() {
  if (!articleRead) return;
  resetArticleReadSilently("stop"); // 先落 null：driver 醒来看到 null 直接退出，不会接着走下一段
  stopPronounce(); // 停止语义沿用现有原语（序号 +1 作废在途结果）
  toast("连播已停止");
}

function articleReadTogglePause() {
  if (!articleRead) return;
  // v30 T2：暂停落在段落呼吸期间 —— 冻结在段边界（不补足剩余 350ms），
  // 继续走同一条 resumePlayback（它会立刻唤醒 driver 的 playbackHold）；音频内暂停不变。
  if (pronouncePaused) resumePlayback(); // v15 C1：暂停 / 继续是现有原语，不动
  else if (!pauseArticleReadGap()) pausePlayback();
  renderArticlePlaybar();
}

// 上一段 / 下一段：在**同一份全篇计划**里移动 pos（滚轮同一条入口）。
// 不能重新 startArticleRead(段号)：那会重建 plan 并把 pos 归 0，点第 N 段起播后「上一段」
// 就回不到起播段之前，计数 / 分片也会从 1/M 重来。只有 pos 到边界才 disabled。
function articleReadStep(delta) {
  const session = articleRead;
  if (!session) return;
  const pos = session.pos + delta;
  if (pos < 0) {
    toast("已经是第一段");
    return;
  }
  if (pos >= session.plan.length) {
    finishArticleRead(session.token); // 末段越界：收尾且停掉还在响的这一段
    return;
  }
  beginArticleRead(session.plan, pos);
}

// 起播的共用入口：plan 已经是过滤后的全篇计划，pos 是计划下标。
function beginArticleRead(plan, pos) {
  articleReadToken += 1;
  const token = articleReadToken;
  articleRead = null; // 旧 driver 醒来按 token 退出
  resetArticleReadSilently();
  stopPronounce(); // 先停上一轮音频（暂停 / 在途结果一并作废）
  articleRead = {
    token,
    articleId: state.article.id,
    plan,
    pos,
    chunkIndex: 0,
  };
  renderArticlePlaybar();
  logPlaybackEvent("article_start", {
    total: plan.length,
    pos,
    para: Number(plan[pos] && plan[pos].index) || 0,
  });
  articleReadRun(token);
}

// 入口（「▶ 播放」传 0；点第 N 段卡 / 正文传该段号）：
// 永远按**整篇**构建计划（wordedOnly 过滤后），pos = 第一个 paragraph.index >= 起播段 ——
// 这样播放条计数 = 全篇段数，且「上一段」能一直回到第 1 段（spec §8.5 的直觉语义）。
function startArticleRead(startIndex) {
  if (state.route !== "article" || !state.article) return;
  const plan = ArticleRead.buildArticlePlan(state.article, {
    wordedOnly: state.articleWordedOnly,
  });
  if (!plan.length) {
    resetArticleReadSilently();
    toast(
      state.articleWordedOnly
        ? "没有带记录的自然段可播（关掉「只播有词段」= 整篇全文）"
        : "这篇没有可朗读的段落"
    );
    return;
  }
  const target = Number.isFinite(Number(startIndex)) ? Number(startIndex) : 0;
  const pos = plan.findIndex((segment) => segment.index >= target);
  if (pos < 0) {
    resetArticleReadSilently();
    toast(
      state.articleWordedOnly
        ? "这一段后面没有带记录的自然段可播（「只播有词段」开着）"
        : "这一段后面没有可朗读的段落"
    );
    return;
  }
  beginArticleRead(plan, pos);
}

/* --- v30 T2（票 #193 / spec #191）：段落呼吸 -------------------------------
 * 段与段之间固定一段 350ms 的呼吸：上一段最后一片 `ended`（= `await run` 落地）时起计时，
 * 与「下一段首片合成」并行 —— 只补足 350ms，不再叠加别的延时。
 * 只在自然段之间：第一段之前、最后一段之后都不加；句内片间不加（那是 pronounceLong 内部）。
 * 会话卡连播的 AUTO_READ_GAP_MS（1100ms）是另一套推进逻辑，两套常量各管各的，别混用。
 * 可取消：停止 / 切路由 / 换段 / 重渲染 / 点别的发音都要立刻收掉定时器与后续起播。
 * 暂停落在呼吸期间：冻结在段边界（pronouncePaused + playbackHold），继续时直接起下一段。
 */
const ARTICLE_PARAGRAPH_GAP_MS = 350;
let articleReadGapTimer = 0; // 呼吸定时器句柄（0 = 没有在计时）
let articleReadGapDone = null; // 这次呼吸的收尾函数（暂停 / 取消时提前解开）

// 呼吸：等满 ARTICLE_PARAGRAPH_GAP_MS；终止条件由 driver 醒来后自己复核
// （token / 序号），这里只负责「到点或被提前解开」。
function articleReadGapWait() {
  return new Promise((resolve) => {
    const done = () => {
      if (articleReadGapDone !== done) return;
      articleReadGapDone = null;
      if (articleReadGapTimer) {
        window.clearTimeout(articleReadGapTimer);
        articleReadGapTimer = 0;
      }
      resolve();
    };
    articleReadGapDone = done;
    articleReadGapTimer = window.setTimeout(done, ARTICLE_PARAGRAPH_GAP_MS);
  });
}

// 取消（停止 / 换段 / 切路由 / 重渲染）：立刻收掉定时器，别让「已经退出的人」被叫回来。
function cancelArticleReadGap() {
  if (articleReadGapTimer) {
    window.clearTimeout(articleReadGapTimer);
    articleReadGapTimer = 0;
  }
  if (articleReadGapDone) articleReadGapDone();
}

// 呼吸期间暂停：把计时冻结在段边界 —— 收掉定时器并解开等待（driver 随即落到 playbackHold），
// 置 pronouncePaused 让播放条显示「▶ 继续」；继续时 resumePlayback 会立刻唤醒 playbackHold。
function pauseArticleReadGap() {
  if (!articleReadGapDone) return false; // 不在呼吸窗口（计时已到 / 没有在连播）
  if (articleReadGapTimer) {
    window.clearTimeout(articleReadGapTimer);
    articleReadGapTimer = 0;
  }
  pronouncePaused = true;
  articleReadGapDone();
  return true;
}

/* --- v30 T3（票 #194 / spec #191）：跨段首片预取 -----------------------------------
 * 当前段第一片开始取/播后，把**下一段的第一片**排进低优先级「下一段」队列
 *（`primeLongBatch` → `longNextQueue`）。只预取一段、只取首片，不整段 / 整篇；
 * 与当前段剩余片共用 `LONG_LOOKAHEAD_CONCURRENCY = 2`，不新增连接、不改池上限。
 * 真正取片由 `pumpLongPrime` 补位，成功即经 `loadPronounceBlob` → `rememberPronounceBlob`
 * 写进前端内存缓存 —— 段尾 `pronounceLong` 清空前瞻表也不影响下一段首片命中、不重复合成。
 * 失败静默（泵的 `.catch` 吞掉）；播到该片时按用户优先级重试，仍失败走既有 SAPI 兜底。
 * 停止 / 切路由 / 换段：`pausePronounceAudio` 已清空待发队列，token 复核再挡一次，
 * 在途请求回来只进缓存、不播放。
 */
function articleReadPrimeNextFirst(session, token) {
  const live = articleRead;
  if (!live || live !== session || live.token !== token) return;
  const next = live.plan[live.pos + 1];
  if (!next || !next.chunks.length) return; // 末段 / 空段：没有可预取的首片
  primeLongBatch([next.chunks[0].text]);
}

function articleReadPrefetchNext(session, token) {
  // 排到微任务：先让本轮 `pronounceLong` 把当前段剩余片排进急队列（`longPrimeQueue`），
  // 「下一段」才真的排在它们后面 —— 保住 `longNextQueue` 的低优先级原义。
  Promise.resolve().then(() => articleReadPrimeNextFirst(session, token));
}

/* --- v32（2026-11-06）：文章连播的跨段滚动预热窗口 ----------------------------
 * 症状：整篇连播（14:32:23–14:37:28）里仍有 20 次 1.7–7.4s 的现场合成，且越往后
 * 越密。原因是原来每段只把「下一段首片」排进低优先级队列，段内剩余片要等**进入
 * 该段之后**才由 primeLongLookahead 排 —— 前瞻只领先一两片，而一片音频常只有
 * 2–4s，播放速度追得上合成速度，段尾就得干等。
 *
 * 做法：不改 LONG_LOOKAHEAD_CONCURRENCY(=2)、不动 EdgePool、不改任何既有前瞻原语，
 * 只按播放顺序把「当前片之后最多 12 片」（跨自然段落遍历整篇计划）排进
 * longNextQueue。每片 onChunk 都重排一次（微任务里，等急队列先排）：段末
 * pronounceLong 的 clearLongPrimeQueue() 清掉的队列会在下一片自动补回，已取回 /
 * 在途 / 已排队的片由 primeLongChunk 去重。合成流水线因此从起播就满负荷，
 * 领先播放进度约 40–50s，远大于最慢 7.4s 的合成；播放到那片时命中缓存即可出声。
 *
 * 与 articleReadPrimeNextFirst 不冲突：后者仍作「下一段首片」的兜底保留，
 * 窗口只包含当前片之后的片，首片若已在急队列/在途会被去重挡掉。
 */
const ARTICLE_LOOKAHEAD_CHUNKS = 12;

function articleReadPrimeWindow(session, token, report = false) {
  const live = articleRead;
  if (!live || live !== session || live.token !== token) return;
  const ahead = [];
  let pos = Number(live.pos) || 0;
  let from = (Number(live.chunkIndex) || 0) + 1; // 当前片已在播：从它的下一片开始
  while (pos < live.plan.length && ahead.length < ARTICLE_LOOKAHEAD_CHUNKS) {
    const chunks = (live.plan[pos] && live.plan[pos].chunks) || [];
    for (
      let i = from;
      i < chunks.length && ahead.length < ARTICLE_LOOKAHEAD_CHUNKS;
      i += 1
    ) {
      const text = chunks[i] && chunks[i].text;
      if (text) ahead.push(text);
    }
    pos += 1;
    from = 0;
  }
  for (const text of ahead) primeLongChunk(text, longNextQueue);
  if (ahead.length) pumpLongPrime();
  if (report) {
    // 只报元数据：窗口大小 / 段位 / 计划段数，绝不带文本。用于事后确认预热确实在跑。
    logPlaybackEvent("article_prime", {
      size: ahead.length,
      pos: Number(live.pos) || 0,
      total: live.plan.length,
    });
  }
}

// driver：一段播完再下一段；末段之后 finish（不环绕、不跨文章）。
// 每段的音频等待复用 pronounceLong 的返回值 —— 它自然播完 / 被停止都会让 await 落地，
// 这里只做「归属判定 + 推进」这两件文章页特有的事。
async function articleReadRun(token) {
  for (;;) {
    const session = articleRead;
    if (!session || session.token !== token) return;
    const segment = session.plan[session.pos];
    if (!segment) {
      finishArticleRead(token);
      return;
    }
    articleReadClearChunkMarks();
    articleReadClearCard();
    // v22 #171：单卡视图 —— 播到哪段就把那张纸换上来（旧列表视图这里只需滚动）。
    showArticleParagraph(segment.index);
    const card = articleCard(segment.index);
    if (card) {
      card.classList.add("is-playing");
      // 读到哪段滚到哪段（nearest：已经在视野里就不动，不打断用户）
      card.scrollIntoView({ block: "nearest" });
    }
    session.chunkIndex = 0;
    renderArticlePlaybar();
    if (!segment.chunks.length) {
      session.pos += 1; // 空段（数据异常）：跳过音频，但原文仍在计划里
      continue;
    }
    const chunks = segment.chunks.map((chunk) => chunk.text);
    const owner = card ? card.querySelector(".para-text") : null;
    const run = pronounceLong(chunks, owner, {
      mode: "long", // 沿用长段落播放条 / 暂停 / 循环互斥的既有语义
      text: segment.text,
      onChunk: (index) => {
        const live = articleRead;
        if (!live || live.token !== token) return;
        live.chunkIndex = index;
        articleReadShowChunk(segment, index);
        renderArticlePlaybar();
        // v30 T3（票 #194）：本段第一片开始取/播 → 预取下一段第一片（只一段、只首片）。
        if (index === 0) articleReadPrefetchNext(session, token);
        // v32：每片滚动一次跨段预热窗口。排到微任务，让本轮 primeLongLookahead
        // 先把「当前段剩余片」排进急队列，窗口片才不会抢在要播的片前面。
        Promise.resolve().then(() =>
          articleReadPrimeWindow(session, token, index === 0)
        );
      },
    });
    // pronounceLong 的同步段已经 ++pronounceSeq：记下本轮序号，await 回来看是否被顶掉
    // （用户点了单词发音 / Esc / 切路由 / 另一段起播都会变）。
    const seq = pronounceSeq;
    await run;
    const live = articleRead;
    if (!live || live.token !== token) return;
    if (seq !== pronounceSeq) {
      resetArticleReadSilently(); // 被别的发音顶掉：只收连播视觉，不抢用户这一轮
      return;
    }
    // 下一段不存在（末段）：直接 finish —— 末段之后不再等一个呼吸。
    if (session.pos + 1 >= session.plan.length) {
      finishArticleRead(token);
      return;
    }
    // v30 T4（#195 review）：段尾再确认一次下一段首片。预取成功/在途时是 no-op；
    // 预取失败时在这里补发，让合成与 350ms 呼吸并行（总间隔 = max(350ms, 剩余合成)）。
    articleReadPrimeNextFirst(session, token);
    // v32：上一段的 pronounceLong 收口时 clearLongPrimeQueue() 清掉了备用队列，
    // 这里在 350ms 呼吸期间把跨段窗口补回来，让合成与呼吸并行、流水线不空转。
    articleReadPrimeWindow(session, token);
    // v30 T2（票 #193）：段间 350ms 段落呼吸 —— 上一段最后一片已经 ended
    //（await run 落地那一刻），计时与「下一段首片合成」并行；末段之后不走这里。
    // 注意：pos 在呼吸期间仍停在**刚播完的**这一段 —— 边界上的「上一段 / 下一段」
    // 与暂停时播放条的段号都按这个边界语义走，真正步进放在唤醒之后。
    await articleReadGapWait();
    const afterGap = articleRead;
    if (!afterGap || afterGap.token !== token) return; // 停止 / 换段：不许起下一段
    if (seq !== pronounceSeq) {
      resetArticleReadSilently(); // 用户点了别的发音：让位，不抢回
      return;
    }
    // 呼吸期间被暂停：等待已提前解开，这里冻结到「继续」为止（不补足剩余 350ms）。
    if (!(await playbackHold(seq))) {
      const pausedAtGap = articleRead;
      if (!pausedAtGap || pausedAtGap.token !== token) return;
      if (seq !== pronounceSeq) resetArticleReadSilently();
      return;
    }
    session.pos += 1;
  }
}

// v22 #171：滚轮 = 先滚正文、到底/到顶再切段（与星图阅读页 onWheel 同一套 WHEEL_* 手势
// 参数：180ms 手势间隔 / 40px 或 3 行触发阈值 / 一次手势最多换一段）。设置页「滚轮换段」
// 关掉时文章页只滚正文（与星图阅读页同一条总开关）。连播中切段仍走 P3 的 articleReadStep。
function attachArticleWheelNav(node) {
  let wheelAt = 0;
  let wheelDir = 0;
  let wheelAccum = 0;
  let wheelFired = false;
  let wheelLockUntil = 0;
  node.addEventListener(
    "wheel",
    (event) => {
      if (!state.wheelNav) return;
      if (!articleParagraphCount()) return; // 0 段文章：不接管滚轮（页面照常滚）
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const dir = event.deltaY > 0 ? 1 : event.deltaY < 0 ? -1 : 0;
      if (!dir) return; // 横向滚轮不参与切段
      const now = Date.now();
      // 事件间隔 < 180ms 视为同一手势；方向一变立刻重置累计与锁（反向能马上切）
      if (dir !== wheelDir || now - wheelAt > WHEEL_GESTURE_GAP_MS) {
        wheelDir = dir;
        wheelAccum = 0;
        wheelFired = false;
        wheelLockUntil = 0;
      }
      wheelAt = now;
      // 正文能滚且还没到该方向的边界 → 原生滚动（纯滚正文，不换段）。
      // 例外：指针停在卡头 / 进度行这类不滚的位置时，原生滚动找不到可滚祖先 ——
      // 手动把它补到正文上（行模式按 ~16px/行折算），段不换。
      const body = articleBodyNode();
      const canScroll = Boolean(body) && body.scrollHeight > body.clientHeight + 2;
      const atBoundary =
        !canScroll ||
        (dir > 0
          ? body.scrollTop + body.clientHeight >= body.scrollHeight - 2
          : body.scrollTop <= 0);
      if (!atBoundary) {
        if (body && !body.contains(event.target)) {
          event.preventDefault();
          body.scrollTop += event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
        }
        return;
      }
      // 到边界（或正文根本不可滚）：拦下原生滚动，按累计量判「切段意图」
      event.preventDefault();
      if (wheelFired || now < wheelLockUntil) return; // 一次手势最多换一段
      wheelAccum += Math.abs(event.deltaY);
      const need = event.deltaMode === 1 ? WHEEL_STEP_LINES : WHEEL_STEP_PX;
      if (wheelAccum < need) return;
      wheelFired = true;
      wheelLockUntil = now + WHEEL_LOCK_MS;
      articleStep(dir);
    },
    { passive: false }
  );
}

// 自动化 / 手测钩子（票面要求）：只读计划 / 当前状态 / 现有播放原语状态，不引依赖、
// 不改产品行为 —— 只有 E2E 注入脚本会读它（见 docs/research/v22-audio-paragraph/
// e2e_article_read.py）；生产路径没有任何调用方。
window.__articleReadDebug = {
  plan: (options = {}) => ArticleRead.buildArticlePlan(state.article, options),
  state: () =>
    articleRead
      ? {
          articleId: articleRead.articleId,
          pos: articleRead.pos,
          index: articleRead.plan[articleRead.pos]
            ? articleRead.plan[articleRead.pos].index
            : null,
          chunks: articleRead.plan[articleRead.pos]
            ? articleRead.plan[articleRead.pos].chunks.length
            : 0,
          chunkIndex: articleRead.chunkIndex,
          paused: pronouncePaused,
          playing: playbackIsLong(),
        }
      : null,
  // 现有播放原语的状态（只读）：E2E 用 mode "" 证明「收尾路径确实调了 stopPronounce」。
  playback: () => ({
    mode: playbackMode,
    owner: Boolean(pronounceOwner),
    seq: pronounceSeq,
  }),
  start: (index = 0) => startArticleRead(index),
};

/* --- 复习模式（§8.2；队列由 /api/records 的 due 过滤 + due_at 排序表达）----- */

function reviewQueueUrl() {
  const params = new URLSearchParams();
  params.set("limit", String(REVIEW_LIMIT));
  params.set("sort", "due_at");
  params.set("state", "due");
  if (state.reviewSession !== null)
    params.set("session", String(state.reviewSession));
  return `/api/records?${params.toString()}`;
}

async function loadReviewQueue() {
  stopPronounce(); // v11 §1：重新加载/换会话 = 换一批卡，先停掉上一张的尾音
  invalidateSelectionRead(); // v16 C1：换一批卡 = 上下文变化（旧原句节点随之脱离文档）
  const review = {
    session: state.reviewSession,
    queue: [],
    index: 0,
    revealed: false,
    done: false,
    busy: false,
    grades: { again: 0, hard: 0, good: 0, easy: 0 },
    loading: true,
    error: "",
  };
  state.review = review;
  renderMain();
  try {
    const payload = await api(reviewQueueUrl());
    if (state.route !== "review" || state.review !== review) return; // 中途退出/换会话：旧响应不覆盖新队列
    review.queue = payload.records || [];
  } catch (error) {
    if (state.route !== "review" || state.review !== review) return;
    review.error = error && error.message ? error.message : String(error);
  }
  review.loading = false;
  renderMain();
}

function renderReview(wrap) {
  const review = state.review;
  const total = review ? review.queue.length : 0;

  const head = el("div", "review-head");
  head.append(el("h2", "list-title", "复习"));
  if (review && total && !review.loading) {
    head.append(
      el("span", "list-sub", `${review.done ? total : review.index + 1} / ${total}`)
    );
  }
  if (state.reviewSession !== null) {
    const chip = button(
      "chip is-on",
      `本会话：${sessionTitle(String(state.reviewSession))} ×`,
      () => {
        window.location.hash = "#/review"; // 去掉会话过滤，复习全部
      }
    );
    chip.title = "只复习这条会话的词；点一下改回全部";
    head.append(chip);
  }
  const right = el("div", "list-header-right");
  right.append(
    button("btn btn-subtle", "退出 Esc", () => {
      window.location.hash = "#/list";
    })
  );
  head.append(right);
  wrap.append(head);

  const track = el("div", "review-progress-track");
  const fill = el("div", "review-progress-fill");
  fill.style.transform = `scaleX(${total && review ? Math.min(1, review.index / total) : 0})`;
  track.append(fill);
  track.setAttribute("role", "progressbar");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", String(Math.max(total, 1)));
  track.setAttribute(
    "aria-valuenow",
    String(review ? Math.min(review.index + (review.done ? 0 : 1), total) : 0)
  );
  wrap.append(track);

  const stage = el("div", "review-stage");
  if (!review || review.loading) stage.append(skeletonView());
  else if (review.error) stage.append(reviewErrorView());
  else if (!review.queue.length) stage.append(reviewEmptyView());
  else if (review.done) stage.append(reviewDoneView());
  else {
    stage.append(reviewCardView(review.queue[review.index]));
    stage.append(reviewActionsView());
  }
  wrap.append(stage);
}

function reviewCardView(record) {
  const revealed = Boolean(state.review && state.review.revealed);
  // §3：复习卡一出现就预取当前卡的 target（高）；换卡/重载只预取当前这张
  prefetch(record.target);
  const card = el("div", revealed ? "card review-card is-revealed" : "card review-card");

  // §8.2：卡片右上角显示所属会话名（复习时知道「这是哪条视频里的词」）
  const top = el("div", "review-card-top");
  const badge = el(
    "span",
    "badge",
    sessionTitle(record.session_id == null ? "none" : String(record.session_id))
  );
  badge.title = "所属会话";
  top.append(badge);
  card.append(top);

  const front = el("div", "review-front");
  const target = el("div", "review-target", record.target);
  if (KIND_LABEL[record.kind]) target.classList.add(`kind-${record.kind}`); // §5：正面目标词同色
  // v11 §1：正面大词也能播目标词，但**不揭示背面**；键盘 Enter / Space 都认（W11）
  target.tabIndex = 0;
  target.setAttribute("role", "button");
  target.setAttribute("aria-label", `播放「${record.target}」发音`);
  target.addEventListener("click", (event) => {
    event.stopPropagation(); // 不触发卡片的 revealCard
    pronounce(record.target, target);
  });
  target.addEventListener("keydown", (event) => {
    // W11（票 #189）：role="button" 的 ARIA 契约是 Space 也要激活 —— 之前只认 Enter，
    // 聚焦在大词上按 Space 会冒泡成 handleReviewKey 的「揭示背面」，当着用户面泄露答案。
    // preventDefault 让全局处理器开头那道 defaultPrevented 检查直接放行。
    if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
    event.preventDefault();
    event.stopPropagation();
    pronounce(record.target, target);
  });
  front.append(target);
  if (record.phonetic) front.append(el("div", "review-phonetic", record.phonetic));
  front.append(
    button("btn review-audio", "发音", (event) => {
      event.stopPropagation(); // 点发音不许顺手翻到背面
      pronounce(record.target, event.currentTarget); // #109：统一发音管线
    })
  );
  card.append(front);

  if (revealed) {
    const back = el("div", "review-back");
    const meaning = el("div", "review-meaning", meaningLabel(record, "（没有释义）"));
    if (!record.meaning) meaning.classList.add("is-empty");
    back.append(meaning);
    const machineBadge = meaningMachineBadge(record); // #172：机翻小标
    if (machineBadge) back.append(machineBadge);
    // v15 C1/C3：复习卡背面的原句也是「播放 / 暂停 / 继续」入口与选区朗读的落点
    // （与列表卡 / 阅读页共用 sentenceNode，拖选守卫一套）
    if (record.context) {
      const contextNode = sentenceNode(record.context, []);
      contextNode.classList.add("review-context");
      // v16 C12（票 #137）：复习卡背面没有可点开的「头部」，sentenceNode 那句
      // 「点头部看详情」在这里是错的（aria-label「播放整段原句发音」照旧，三处一致）。
      contextNode.title = "点原句听发音 · 拖选可朗读";
      back.append(contextNode);
    } else {
      back.append(el("div", "review-context", "（没有原句）"));
    }
    const source = [record.source_app, record.source_title, record.position]
      .filter(Boolean)
      .join(" · ");
    back.append(el("div", "review-source", source ? `出处：${source}` : "出处：—"));
    // §2.6：复习卡背面的「打开原视频」；能解析出 position 就带上跳转时间
    const session = sessionById(record.session_id);
    if (session && session.url) {
      const linkRow = el("div", "review-link-row");
      linkRow.append(videoLink(session.url, positionSeconds(record.position), session.site));
      back.append(linkRow);
    }
    card.append(back);
  } else {
    card.append(el("div", "review-hint", "Space 或点这张卡揭示背面"));
    card.addEventListener("click", revealCard);
  }
  return card;
}

function reviewActionsView() {
  const review = state.review;
  const box = el("div", "review-actions-box");
  const row = el("div", "review-actions");
  for (const [key, grade, label] of GRADES) {
    const node = button(`btn grade-btn grade-${grade}`, "", () => gradeCurrent(grade));
    node.disabled = !review.revealed || review.busy;
    node.title = `${grade}（${label}）`;
    node.append(el("kbd", null, key));
    node.append(el("span", "grade-label", label));
    row.append(node);
  }
  box.append(row);
  box.append(
    el(
      "div",
      "review-hint",
      review.revealed
        ? "按 1–4 打分，自动进入下一张"
        : "先按 Space 揭示背面，再用 1–4 打分"
    )
  );
  return box;
}

function reviewDoneView() {
  const review = state.review;
  const total = review.queue.length;
  const remembered = total - review.grades.again;
  const rate = total ? Math.round((remembered / total) * 100) : 0;
  const card = el("div", "card review-done");
  card.append(el("div", "review-done-count", `${total} / ${total}`));
  card.append(el("div", "empty-title", "本轮复习完成"));
  const breakdown = el("div", "review-breakdown");
  for (const [, grade, label] of GRADES) {
    breakdown.append(
      el("span", `badge grade-${grade}`, `${label} ${review.grades[grade]}`)
    );
  }
  card.append(breakdown);
  // 「正确率」= 非 again 的占比：§8.3 里只有 again 算遗忘（lapses+1、reps 归零）
  card.append(el("div", "review-rate", `正确率 ${rate}%（记住 ${remembered} / ${total}）`));
  if (review.grades.again) {
    card.append(
      el("div", "hint", `${review.grades.again} 张 again 的卡 10 分钟后回到队列`)
    );
  }
  const actions = el("div", "review-done-actions");
  actions.append(button("btn btn-primary", "再来一轮", () => loadReviewQueue()));
  actions.append(
    button("btn", "回到列表", () => {
      window.location.hash = "#/list";
    })
  );
  card.append(actions);
  return card;
}

function reviewEmptyView() {
  const card = el("div", "card empty");
  card.append(el("div", "empty-title", "现在没有到期的词条"));
  card.append(el("p", "hint", "队列 = due_at ≤ 现在，按到期时间升序。"));
  const actions = el("div", "review-done-actions");
  if (state.reviewSession !== null) {
    actions.append(
      button("btn", "复习全部（去掉会话过滤）", () => {
        window.location.hash = "#/review";
      })
    );
  }
  actions.append(
    button("btn btn-primary", "回到列表", () => {
      window.location.hash = "#/list";
    })
  );
  card.append(actions);
  return card;
}

function reviewErrorView() {
  const box = el("div", "error-box");
  box.append(el("div", null, `连不上数据中枢：${state.review.error}`));
  box.append(button("btn", "重试", () => loadReviewQueue()));
  box.append(
    button("btn btn-subtle", "回到列表", () => {
      window.location.hash = "#/list";
    })
  );
  return box;
}

function revealCard() {
  const review = state.review;
  if (!review || review.revealed || review.done) return;
  review.revealed = true;
  invalidateSelectionRead(); // v16 C1/C12：揭示会整块重渲染，旧快照的原句节点即将脱离文档
  renderMain();
}

async function gradeCurrent(grade) {
  const review = state.review;
  if (!review || review.done || review.busy || !review.queue.length) return;
  if (!review.revealed) {
    toast("先按 Space 揭示背面再打分");
    return;
  }
  const record = review.queue[review.index];
  review.busy = true;
  try {
    await api(`/api/records/${record.id}/review`, jsonRequest("POST", { grade }));
  } catch (error) {
    if (state.route === "review" && state.review === review) {
      review.busy = false;
      toast(`打分失败：${error && error.message ? error.message : error}`);
    }
    return;
  }
  if (state.route !== "review" || state.review !== review) return; // 打分途中退出了
  review.busy = false;
  review.grades[grade] += 1;
  review.index += 1;
  review.revealed = false;
  stopPronounce(); // v11 §1：进入下一张卡，上一张的朗读不跟着走
  invalidateSelectionRead(); // v16 C1/C12：下一张卡要重渲染，旧快照指向的节点即将脱离文档
  if (review.index >= review.queue.length) review.done = true;
  if (state.summary && Number(state.summary.due_now) > 0) {
    state.summary.due_now = Number(state.summary.due_now) - 1; // 这张卡已离开到期队列
  }
  renderStats();
  renderMain();
  if (review.done) refreshSummary(); // 本轮结束：与 /api/summary 对一次账
}

async function refreshSummary() {
  const seq = ++summarySeq;
  try {
    const summary = await api("/api/summary");
    if (seq !== summarySeq) return; // 票 #72：旧响应不覆盖新的顶栏计数
    state.summary = summary;
    renderStats();
  } catch (error) {
    /* 顶栏计数过期不致命；下次 loadAll 会纠正 */
  }
}


/* --- 设置页（§8.4；票 #32）-------------------------------------------------- */

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function hotkeyStatusItem(action) {
  const status = state.settings ? state.settings.status : null;
  if (!status) return null;
  return (status.items || []).find((item) => item.action === action) || null;
}

function statusStamp() {
  const status = state.settings ? state.settings.status : null;
  const date = status ? parseTs(status.probed_at) : null;
  if (!date) return "";
  const clock = `探测于 ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  // v34 T2（#215）：快照是「探测那一刻」的，带上年龄才看得出它有多旧（阈值在服务端算）
  const fresh = state.settings ? state.settings.hotkeyFreshness : null;
  const age = fresh && Number.isFinite(fresh.age_s) ? fresh.age_s : null;
  if (age === null) return clock;
  if (age < 60) return `${clock}（刚刚）`;
  if (age < 3600) return `${clock}（${Math.floor(age / 60)} 分钟前）`;
  return `${clock}（${Math.floor(age / 3600)} 小时前）`;
}

// v34 T2：重绘判据 = 探测戳 + 时效状态。时效会随时间/重探切换（旧键位 → 新结果），
// 只看 probed_at 的话切换要么不重绘、要么把正在录键位的行打断。
function hotkeyStamp() {
  const fresh = state.settings ? state.settings.hotkeyFreshness : null;
  return `${statusStamp()}|${fresh ? fresh.state : "none"}`;
}

function specBadge(action, spec) {
  if (!spec) return { label: "未设置", cls: "is-unknown", title: "这个动作没有绑键位" };
  const item = hotkeyStatusItem(action);
  if (!item) return { label: "未探测", cls: "is-unknown", title: "外壳还没探测过这个键位" };
  if (item.spec && item.spec !== spec) {
    // 状态是旧键位的（刚保存、外壳还没重探）：不能拿它冒充新键位的结论
    return {
      label: "未探测",
      cls: "is-unknown",
      title: `外壳报的还是旧键位 ${item.spec}，等它重探`,
    };
  }
  const [label, cls] = HOTKEY_BADGES[item.state] || ["未探测", "is-unknown"];
  if (item.state === "dup" && item.detail) {
    return { label: `与本工具内 ${item.detail} 重复`, cls, title: item.detail };
  }
  return { label, cls, title: item.detail || label };
}

function startRecord(key, scope) {
  state.record = { key, scope, error: "" };
  renderMain();
}

function ahkFromEvent(event) {
  let spec = "";
  if (event.ctrlKey) spec += "^"; // AHK v2：^=Ctrl !=Alt +=Shift #=Win
  if (event.altKey) spec += "!";
  if (event.shiftKey) spec += "+";
  if (event.metaKey) spec += "#";

  let base = null;
  if (/^Numpad[0-9]$/.test(event.code || "")) base = event.code;
  else if (NUMPAD_CODE[event.code]) base = NUMPAD_CODE[event.code];
  else if (/^F([1-9]|1[0-9]|2[0-4])$/.test(event.key)) base = event.key.toUpperCase();
  else if (HOTKEY_NAMED_KEYS[event.key]) base = HOTKEY_NAMED_KEYS[event.key];
  else if (/^[a-zA-Z]$/.test(event.key)) base = event.key.toLowerCase();
  else if (/^[0-9]$/.test(event.key)) base = event.key;
  // 单标点（, . / - 等）：只在不带修饰键时收 —— AHK 的 `,` 无歧义，`^,` 则有歧义。
  else if (
    !spec &&
    String(event.key).length === 1 &&
    PUNCT_BASE.includes(event.key)
  )
    base = event.key;
  if (!base) return null;
  return spec + base;
}

function handleRecordKey(event) {
  const record = state.record;
  if (event.key === "Escape") {
    event.preventDefault();
    state.record = null;
    renderMain();
    return;
  }
  if (HOTKEY_MODIFIERS.includes(event.key)) return; // 光按修饰键不算，继续等
  event.preventDefault();
  const spec = ahkFromEvent(event);
  if (!spec) {
    record.error =
      "这个键录不了（试试字母 / 数字 / F1–F24 / 小键盘 / Enter 这类；标点只能不带修饰单独用）";
    renderMain();
    return;
  }
  // 用户要求 8：无修饰的字母 / 数字 / 标点键当全局键非法
  if (record.scope === "global" && spec.length === 1 && !/[\^!+#]/.test(spec)) {
    record.error =
      "不带修饰的单个字符做全局键会抢走全系统的输入；加个 Ctrl / Alt / Shift / Win，或改用 F1–F24 这类功能键";
    renderMain();
    return;
  }
  state.settingsDraft[record.key] = spec;
  state.record = null;
  renderMain();
}

function hotkeyRow(action) {
  const draft = state.settingsDraft[action.key];
  const dirty = draft !== undefined && draft !== action.spec;
  const current = dirty ? draft : action.spec;
  const recording = Boolean(state.record && state.record.key === action.key);

  const row = el("div", recording ? "hotkey-row is-recording" : "hotkey-row");
  const name = el("div", "hotkey-name");
  name.append(el("span", "hotkey-label", action.label));
  name.append(el("span", "hotkey-scope", SCOPE_LABEL[action.scope] || action.scope));
  row.append(name);

  const keyCell = el("div", "hotkey-key");
  if (recording) {
    keyCell.append(el("span", "hotkey-spec is-recording", "按下新键位…（Esc 取消）"));
  } else {
    const spec = button(
      "hotkey-spec",
      current || "（未绑定）",
      () => startRecord(action.key, action.scope)
    );
    spec.title = action.default
      ? `点一下录新键位（默认 ${action.default}）`
      : "点一下录新键位（默认未绑定）";
    if (!current) spec.classList.add("is-empty");
    keyCell.append(spec);
  }
  row.append(keyCell);

  const badgeCell = el("div", "hotkey-badge");
  const info = dirty ? { label: "待保存", cls: "is-warn", title: "还没写进 config.ini" } : specBadge(action.key, current);
  const badge = el("span", `badge ${info.cls}`, info.label);
  if (info.title) badge.title = info.title;
  badgeCell.append(badge);
  if (dirty) {
    badgeCell.append(
      button("hotkey-revert", "撤销", () => {
        delete state.settingsDraft[action.key];
        renderMain();
      })
    );
  } else if (current !== action.default) {
    // 改乱了可以一键回默认（值来自 §9.1 的默认表，随 GET /api/settings 下发）
    badgeCell.append(
      button("hotkey-revert", "用默认", () => {
        state.settingsDraft[action.key] = action.default;
        renderMain();
      })
    );
  }
  row.append(badgeCell);

  if (recording && state.record.error) {
    row.append(el("div", "hotkey-error", state.record.error));
  }
  return row;
}

function settingsHeader() {
  const head = el("div", "page-head");
  const left = el("div", "page-head-left");
  // 设置页的「出口」：以前只有保存按钮，进来就出不去（用户报障）
  left.append(
    button("btn btn-back", "← 返回列表", () => {
      window.location.hash = "#/list";
    })
  );
  const titles = el("div", "page-titles");
  titles.append(el("h2", "page-title", "设置"));
  titles.append(
    el("span", "page-sub", statusStamp() || "热键状态：还没收到外壳的探测结果")
  );
  left.append(titles);
  head.append(left);

  const right = el("div", "list-header-right");
  const dirty = settingsDirty();
  if (dirty) right.append(el("span", "badge is-warn", "有未保存的改动"));
  const save = button(
    "btn btn-primary",
    state.settingsSaving ? "保存中…" : "保存设置",
    saveSettings
  );
  save.disabled = state.settingsSaving || !state.settings;
  right.append(save);
  head.append(right);
  return head;
}

// 有草稿（改过但没保存）→ 提示 + 离开前叫一声
function settingsDirty() {
  return Boolean(state.settingsDraft && Object.keys(state.settingsDraft).length);
}

function settingsCard(title, note) {
  const card = el("div", "card settings-card");
  const head = el("div", "settings-head");
  head.append(el("h3", "section-title", title));
  if (note) head.append(el("span", "hint", note));
  card.append(head);
  return card;
}

function hotkeyCard() {
  const card = settingsCard(
    "快捷键",
    "点键位那一格 → 按下新组合（Esc 取消）。修改只写 config.ini，保存后外壳 2 秒内自动生效，无需重启。"
  );
  card.append(
    el(
      "p",
      "hint hotkey-tip",
      `任意组合都能用：Alt+Q、Ctrl+Shift+F、F8 这类功能键、小键盘 Numpad1 都可以。` +
        "「全局」键要不带修饰的单个字母/数字会被拒绝（会抢走全系统输入），加个修饰键或改用功能键即可；" +
        "「框内」键只在速记框显示时生效，单标点（, . / -）也收。"
    )
  );
  // v34 T2（#215）：快照比配置旧时不假装绿 —— 明说「外壳报的还是旧键位，等它重探」。
  // 文案由中枢算好下发（键位取真值，不写死）；这里只负责显示。
  const freshness = state.settings.hotkeyFreshness;
  if (freshness && freshness.stale && freshness.message) {
    card.append(el("p", "hint hotkey-stale", freshness.message));
  }
  for (const action of state.settings.actions) card.append(hotkeyRow(action));
  const foot = el("div", "settings-foot");
  const stamp = statusStamp();
  foot.append(
    el(
      "span",
      "hint",
      stamp
        ? `${stamp}（每 5 秒自动刷新）`
        : "外壳还没写 hotkey-status.json —— 探测结果出来前一律显示「未探测」"
    )
  );
  card.append(foot);
  return card;
}

// v11 §7（票 #112）的状态行：可用（带缓存统计）/ edge-tts 未安装 / 已关闭
function ttsStatusText(tts) {
  const block = tts && typeof tts === "object" ? tts : {};
  if (block.enabled === false) return "已关闭，使用本机语音";
  if (block.available === false) return "edge-tts 未安装，暂用本机语音";
  const files = Number(block.cache_files) || 0;
  const bytes = Number(block.cache_bytes) || 0;
  return `可用 · 缓存 ${files} 条 / ${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

// 票 #116：把音色 id 换成下拉里的展示名（状态行用；拿不到就退回 id）
function ttsVoiceLabel(tts) {
  const id = tts && tts.voice;
  const voices = (state.settings && state.settings.tts_voices) || [];
  const voice = voices.find((item) => item.id === id);
  return voice ? voice.label : id || "—";
}

function optionsCard() {
  const form = state.settingsForm;
  const card = settingsCard("词典与会话");

  const onlineRow = el("div", "form-row");
  const onlineBox = el("label", "form-check");
  const online = el("input");
  online.type = "checkbox";
  online.checked = Number(form.dict_online) !== 0;
  online.addEventListener("change", () => {
    form.dict_online = online.checked ? 1 : 0;
  });
  onlineBox.append(online);
  onlineBox.append(el("span", null, "允许在线词典"));
  onlineRow.append(onlineBox);
  onlineRow.append(el("span", "hint", "关掉就只用本地词库，绝不发网络请求（dict_online）"));
  card.append(onlineRow);

  // v6 三期（票 #89）：账号解析联网开关；隐私口径与 README 一致
  const accountRow = el("div", "form-row");
  const accountBox = el("label", "form-check");
  const account = el("input");
  account.type = "checkbox";
  account.checked = Number(form.resolve_account_online) !== 0;
  account.addEventListener("change", () => {
    form.resolve_account_online = account.checked ? 1 : 0;
  });
  accountBox.append(account);
  accountBox.append(el("span", null, "联网解析视频账号"));
  accountRow.append(accountBox);
  accountRow.append(
    el(
      "span",
      "hint",
      "只把视频 ID 发给 bilibili，不涉及词/释义/原句（resolve_account_online）"
    )
  );
  card.append(accountRow);

  // v11 §7（票 #112）：在线神经语音总开关 + 隐私说明 + 当前状态行（照规格原文）
  const ttsRow = el("div", "form-row tts-row");
  ttsRow.append(
    switchBox("在线神经语音（Edge 朗读）", form.tts_online, (checked) => {
      form.tts_online = checked ? 1 : 0;
    })
  );
  ttsRow.append(
    el(
      "span",
      "hint",
      "只把要朗读的词/句发给微软；不发送释义、标题、出处、标签。" +
        "关掉后只用本机语音（优先英文 SAPI，最后浏览器本地系统语音；不会走在线音色）。"
    )
  );
  card.append(ttsRow);

  // 票 #116：音色下拉（17 个 en-US 神经语音）。保存后热生效，下一句就用新声音；
  // 缓存键含 voice，切音色不会串音。离线 SAPI 只有 Zira，不随这里变。
  const voiceRow = el("div", "form-row tts-voice-row");
  voiceRow.append(el("span", "form-label", "发音音色"));
  const voiceSelect = el("select", "field");
  for (const voice of state.settings.tts_voices || []) {
    const option = el(
      "option",
      null,
      `${voice.label}${voice.gender ? `（${voice.gender}）` : ""}`
    );
    option.value = voice.id;
    if (voice.id === form.tts_voice) option.selected = true;
    voiceSelect.append(option);
  }
  voiceSelect.addEventListener("change", () => {
    form.tts_voice = voiceSelect.value;
  });
  voiceRow.append(voiceSelect);
  voiceRow.append(
    el(
      "span",
      "hint",
      "Edge 在线神经语音；保存后立即生效（下一句就用新音色）。离线 SAPI 只有 Zira，不受影响。"
    )
  );
  card.append(voiceRow);

  const ttsStatusRow = el("div", "form-row tts-status");
  ttsStatusRow.append(el("span", "form-label", "发音状态"));
  ttsStatusRow.append(
    el("span", "browser-state tts-state", ttsStatusText(state.settings && state.settings.tts))
  );
  ttsStatusRow.append(
    el("span", "hint", `当前音色：${ttsVoiceLabel(state.settings && state.settings.tts)}；语速 / 缓存上限在 config.ini 里改`)
  );
  card.append(ttsStatusRow);

  const idleRow = el("div", "form-row");
  const idleBox = el("label", "form-check");
  idleBox.append(el("span", null, "会话空闲切分"));
  const idle = el("input", "field field-num");
  idle.type = "number";
  idle.min = "1";
  idle.max = "1440";
  idle.value = String(form.session_idle_min);
  idle.addEventListener("input", () => {
    form.session_idle_min = idle.value;
  });
  idleBox.append(idle);
  idleBox.append(el("span", null, "分钟"));
  idleRow.append(idleBox);
  idleRow.append(el("span", "hint", "停手超过这么久，下一个词算新会话（session_idle_min）"));
  card.append(idleRow);

  const themeRow = el("div", "form-row");
  themeRow.append(el("span", "form-label", "主题"));
  const seg = el("div", "seg");
  for (const [value, label] of [["dark", "深色"], ["light", "浅色"]]) {
    const chip = button(
      form.theme === value ? "chip is-on" : "chip",
      label,
      () => {
        form.theme = value;
        applyTheme(value);
        renderMain();
      }
    );
    seg.append(chip);
  }
  themeRow.append(seg);
  themeRow.append(el("span", "hint", "写进 config.ini 的 theme（取词台与外壳一致）"));
  card.append(themeRow);
  return card;
}

/* --- v5 §7：浏览器链接与停播卡片（只显示旗标与站点短名，**没有** URL/标题/原句/释义）-- */

function ageText(ms) {
  const value = Number(ms);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1000) return "刚刚";
  const seconds = Math.floor(value / 1000);
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  return `${Math.floor(minutes / 60)} 小时前`;
}

// SMTC 的 app token（`MSEdge` / `chrome.exe`…）→ 给人看的名（Edge / Chrome…）
function mediaAppLabel(app) {
  const text = String(app || "").trim();
  if (!text) return "";
  const low = text.toLowerCase();
  for (const [hint, label] of [
    ["edge", "Edge"],
    ["chrome", "Chrome"],
    ["firefox", "Firefox"],
  ]) {
    if (low.includes(hint)) return label;
  }
  return text.replace(/\.exe$/i, "");
}

// 扩展：已连接（站点 · 多久前）/ 未连接（站点短名，绝不显示 URL）
function browserExtText(ext) {
  if (!ext || !ext.connected) return "未连接";
  const parts = [];
  if (ext.site) parts.push(String(ext.site));
  const age = ageText(ext.age_ms);
  if (age) parts.push(age);
  return `已连接（${parts.join(" · ")}）`;
}

// 本机探针：可用（最近 N ms）/ 可用（上次没读到）/ 不可用 / 已关闭
function browserProbeText(probe) {
  if (!probe || !probe.enabled) return "已关闭";
  if (!probe.ready) return "不可用";
  if (probe.last_ok === true && Number(probe.last_ms) > 0)
    return `可用（最近 ${probe.last_ms} ms）`;
  if (probe.last_ok === false) return "可用（上次没读到）";
  return "可用";
}

// 媒体联动：可用 · Edge · 正在播放／已暂停／无媒体会话 / 不可用 / 已关闭
function browserMediaText(media) {
  if (!media || !media.enabled) return "已关闭";
  if (!media.available) return "不可用";
  const parts = ["可用"];
  const app = mediaAppLabel(media.app);
  if (app) parts.push(app);
  if (media.status === "playing") parts.push("正在播放");
  else if (media.status === "paused") parts.push("已暂停");
  else parts.push("无媒体会话");
  return parts.join(" · ");
}

// 最近一次取词：来源（扩展/本机探针/无）· 有链接/无链接 · 有位置/无位置 · 时间
function browserCaptureText(capture) {
  const source = capture ? capture.source : "";
  const label = source === "extension" ? "扩展" : source === "probe" ? "本机探针" : "无";
  const parts = [`来源 ${label}`];
  parts.push(capture && capture.has_url ? "有链接" : "无链接");
  parts.push(capture && capture.has_position ? "有位置" : "无位置");
  if (capture && capture.at) parts.push(formatFull(capture.at));
  return parts.join(" · ");
}

function browserRow(label, value, hint) {
  const row = el("div", "form-row");
  row.append(el("span", "form-label", label));
  row.append(el("span", "browser-state", value));
  if (hint) row.append(el("span", "hint", hint));
  return row;
}

// 与「允许在线词典」同款开关（form-check + checkbox）：只改待保存值，保存按钮统一 PUT
function switchBox(text, checked, onChange) {
  const box = el("label", "form-check");
  const input = el("input");
  input.type = "checkbox";
  input.checked = Number(checked) !== 0;
  input.addEventListener("change", () => onChange(input.checked));
  box.append(input);
  box.append(el("span", null, text));
  return box;
}

function browserCard() {
  const browser = state.settings.browser || {};
  const form = state.settingsForm;
  const card = settingsCard(
    "浏览器链接与停播",
    "视频链接自动识别的两个来源与停播/续播联动（v5 §2、§3）；开关保存后写回 config.ini，工具自动生效。"
  );

  const ext = browser.ext || {};
  card.append(
    browserRow(
      "扩展",
      browserExtText(ext),
      ext.connected
        ? "每标签页精度（URL + 播放进度）由扩展上报"
        : "装了能拿到「每标签页精度」（安装步骤见工具目录 extension/README.md），不装也能自动识别"
    )
  );

  const probeRow = el("div", "form-row");
  probeRow.append(el("span", "form-label", "本机探针"));
  probeRow.append(el("span", "browser-state", browserProbeText(browser.probe)));
  probeRow.append(
    switchBox("读浏览器地址栏", form.probe_url, (checked) => {
      form.probe_url = checked ? 1 : 0;
    })
  );
  probeRow.append(el("span", "hint", "呼出时读本次捕获窗口的地址栏拿链接，无需装扩展（probe_url）"));
  card.append(probeRow);

  const mediaRow = el("div", "form-row");
  mediaRow.append(el("span", "form-label", "媒体联动"));
  mediaRow.append(el("span", "browser-state", browserMediaText(browser.media)));
  mediaRow.append(
    switchBox("呼出暂停", form.media_pause, (checked) => {
      form.media_pause = checked ? 1 : 0;
    })
  );
  mediaRow.append(el("span", "hint", "呼出取词时暂停、收工后续播，走 Windows 媒体会话（media_pause）"));
  card.append(mediaRow);

  // v33 T5（票 #211）：「视频收句」单一总开关（config.ini 的 video_capture）。
  // 开 = 心跳驱动的自动调度（B站播放中 → 查 CC，无英文轨转屏幕采样）；
  // 关 = 完全静默（零查轨零采样）。
  const videoRow = el("div", "form-row");
  videoRow.append(el("span", "form-label", "视频收句"));
  videoRow.append(el("span", "browser-state", form.video_capture ? "开" : "关"));
  videoRow.append(
    switchBox("视频收句", form.video_capture, (checked) => {
      form.video_capture = checked ? 1 : 0;
    })
  );
  videoRow.append(
    el(
      "span",
      "hint",
      "看 B站视频时自动把英文原句收进待选句：有英文 CC 整轨直读，没有就字幕带屏幕采样（video_capture）。关 = 完全静默，零查轨零采样"
    )
  );
  card.append(videoRow);

  // v15 T2（票 #127，A5）：阅读页纸面滚轮换段的总开关（config.ini 的 wheel_nav）
  const wheelRow = el("div", "form-row");
  wheelRow.append(el("span", "form-label", "滚轮换段"));
  wheelRow.append(el("span", "browser-state", form.wheel_nav ? "开" : "关"));
  wheelRow.append(
    switchBox("滚轮换段", form.wheel_nav, (checked) => {
      form.wheel_nav = checked ? 1 : 0;
    })
  );
  wheelRow.append(
    el(
      "span",
      "hint",
      "阅读页纸面滚轮换上一段 / 下一段；正文能滚时先滚正文；其他区域（星图空白 / 列表 / 抽屉 / 侧栏）滚轮不触发（wheel_nav）"
    )
  );
  card.append(wheelRow);

  card.append(browserRow("最近一次取词", browserCaptureText(browser.last_capture), null));
  return card;
}

function downloadLink(text, href, filename) {
  const node = el("a", "btn", text);
  node.href = href;
  node.download = filename; // 同源 + download：浏览器直接存文件，前端不碰文件系统
  return node;
}

function dataCard() {
  const data = state.settings.data;
  const card = settingsCard("数据", "导出走 /api/export，前端不直接读文件");
  const kv = el("dl", "kv");
  kv.append(el("dt", null, "数据库"));
  kv.append(el("dd", null, data.db_path));
  kv.append(el("dt", null, "大小"));
  kv.append(
    el(
      "dd",
      null,
      `${formatBytes(data.db_bytes)} · 词条 ${data.records} · 会话 ${data.sessions}`
    )
  );
  kv.append(el("dt", null, "配置文件"));
  kv.append(el("dd", null, state.settings.config_path));
  card.append(kv);
  const actions = el("div", "settings-actions");
  actions.append(downloadLink("导出 JSON", "/api/export?format=json", "capture-export.json"));
  actions.append(downloadLink("导出 Markdown", "/api/export?format=md", "英语生词.md"));
  card.append(actions);
  card.append(el("p", "hint", "Markdown 是 v1 条目格式（front-matter + 每条一段），导回去照样能在 Obsidian 里用。"));
  return card;
}

function renderSettings(wrap) {
  wrap.append(settingsHeader());
  if (state.settingsError) {
    const box = el("div", "error-box");
    box.append(el("div", null, `连不上数据中枢：${state.settingsError}`));
    box.append(button("btn", "重试", () => loadSettings({ skeleton: true })));
    wrap.append(box);
    return;
  }
  if (!state.settings || state.settingsLoading) {
    wrap.append(skeletonView());
    return;
  }
  wrap.append(hotkeyCard());
  wrap.append(optionsCard());
  wrap.append(browserCard());
  wrap.append(dataCard());
}

async function loadSettings({ skeleton = false } = {}) {
  if (skeleton || !state.settings) {
    state.settingsLoading = true;
    renderMain();
  }
  try {
    const payload = await api("/api/settings");
    state.settings = payload;
    state.settings.actions = (payload.actions || []).map((action) => ({
      ...action,
      spec: (payload.hotkeys || {})[action.key] || "",
    }));
    state.settings.status = payload.hotkey_status || null;
    // v34 T2（#215）：快照时效（旧键位 / 旧快照 / 未探测）——页面据此给明确提示
    state.settings.hotkeyFreshness = payload.hotkey_freshness || null;
    state.settings.data = payload.data || {
      db_path: "—",
      db_bytes: 0,
      records: 0,
      sessions: 0,
    };
    state.settingsForm = {
      dict_online: payload.dict_online,
      session_idle_min: payload.session_idle_min,
      theme: payload.theme,
      probe_url: payload.probe_url,
      media_pause: payload.media_pause,
      resolve_account_online: payload.resolve_account_online,
      tts_online: payload.tts_online,
      tts_voice: payload.tts_voice,
      // v15 T2（票 #127）：滚轮换段开关（老 hub 不回显这个键时按默认「开」）
      wheel_nav: payload.wheel_nav === undefined ? 1 : payload.wheel_nav,
      // v33 T5（票 #211）：「视频收句」总开关（老 hub 不回显时按默认「开」）
      video_capture: payload.video_capture === undefined ? 1 : payload.video_capture,
    };
    applyTtsState(payload.tts);
    state.settingsDraft = {};
    state.record = null;
    state.settingsStamp = hotkeyStamp();
    state.settingsError = "";
  } catch (error) {
    state.settingsError = error && error.message ? error.message : String(error);
  }
  state.settingsLoading = false;
  renderMain();
}

async function refreshHotkeyStatus() {
  if (state.route !== "settings" || !state.settings) return;
  try {
    const payload = await api("/api/hotkeys");
    state.settings.status = payload.status || null;
    state.settings.hotkeyFreshness = payload.hotkey_freshness || null;
    const stamp = hotkeyStamp();
    if (stamp !== state.settingsStamp) {
      state.settingsStamp = stamp;
      renderMain(); // 探测结果换了（或时效状态变了）才重绘，避免打断正在录键位的行
    }
  } catch (error) {
    /* 轮询失败不打扰用户：下一次再来 */
  }
}

function startSettingsPoll() {
  stopSettingsPoll();
  state.settingsTimer = window.setInterval(refreshHotkeyStatus, SETTINGS_POLL_MS);
}

function stopSettingsPoll() {
  if (state.settingsTimer) {
    window.clearInterval(state.settingsTimer);
    state.settingsTimer = 0;
  }
}

// v11 §7（票 #112）：PUT 回包只带设置项（没有 tts 状态块）→ 重新 GET 一次，把状态行与
// 全局开关（warm 的闸门）对齐。回包里若真有 `tts`，直接用，不再多跑一趟。
async function refreshTtsStatus(saved) {
  const inline = saved && saved.tts;
  if (inline) {
    applyTtsState(inline);
    if (state.settings) state.settings.tts = inline;
    return inline;
  }
  try {
    const payload = await api("/api/settings");
    const block = payload.tts || null;
    applyTtsState(block);
    if (state.settings) {
      state.settings.tts = block;
      state.settings.tts_online = payload.tts_online;
      state.settings.tts_voice = payload.tts_voice;
      state.settings.tts_voices = payload.tts_voices || state.settings.tts_voices;
    }
    return block;
  } catch (error) {
    return null; // 刷新失败不挡「保存成功」；状态行保留旧值
  }
}

async function saveSettings() {
  if (state.settingsSaving || !state.settings) return;
  const form = state.settingsForm;
  const idle = Number(form.session_idle_min);
  if (!Number.isInteger(idle) || idle < 1 || idle > 1440) {
    toast("会话空闲分钟要填 1–1440 之间的整数");
    return;
  }
  const body = {
    hotkeys: { ...state.settings.hotkeys, ...state.settingsDraft },
    dict_online: Number(form.dict_online) !== 0 ? 1 : 0,
    session_idle_min: idle,
    theme: form.theme,
    probe_url: Number(form.probe_url) !== 0 ? 1 : 0,
    media_pause: Number(form.media_pause) !== 0 ? 1 : 0,
    resolve_account_online: Number(form.resolve_account_online) !== 0 ? 1 : 0,
    tts_online: Number(form.tts_online) !== 0 ? 1 : 0, // v11 §7（票 #112）
    tts_voice: form.tts_voice, // 票 #116：音色热更新（下一句即生效）
    wheel_nav: Number(form.wheel_nav) !== 0 ? 1 : 0, // v15 T2（票 #127）：滚轮换段开关
    video_capture: Number(form.video_capture) !== 0 ? 1 : 0, // v33 T5（票 #211）：视频收句总开关
  };
  state.settingsSaving = true;
  renderMain();
  try {
    const payload = await api("/api/settings", jsonRequest("PUT", body));
    const saved = payload.settings;
    state.settings = { ...state.settings, ...saved };
    state.settings.actions = state.settings.actions.map((action) => ({
      ...action,
      spec: saved.hotkeys[action.key] || "",
    }));
    state.settingsDraft = {};
    state.settingsForm = {
      dict_online: saved.dict_online,
      session_idle_min: saved.session_idle_min,
      theme: saved.theme,
      probe_url: saved.probe_url,
      media_pause: saved.media_pause,
      resolve_account_online: saved.resolve_account_online,
      tts_online: saved.tts_online,
      tts_voice: saved.tts_voice,
      wheel_nav: saved.wheel_nav,
    };
    // v15 T2：前端立刻按新开关走（下次进阅读页的脚注文案也一起换）
    state.wheelNav = Number(saved.wheel_nav) !== 0;
    // v11 §7：开关回显与状态行都换成写回后的值；关掉后 warm 立刻停（state.tts 已同步）
    await refreshTtsStatus(saved);
    applyTheme(saved.theme);
    state.settingsStamp = hotkeyStamp(); // 记下旧戳，重探后戳变了才会重绘
    toast("已保存，工具已自动生效（无需重启）");
    startSettingsPoll();
  } catch (error) {
    const errors = error && error.payload && error.payload.errors;
    const first = errors ? Object.values(errors)[0] : null;
    toast(`保存失败：${first || (error && error.message) || error}`);
  } finally {
    state.settingsSaving = false;
    renderMain();
  }
}

/* --- 交互：筛选 / 搜索 / 键盘 ------------------------------------------------- */

function selectSession(id) {
  if (id === null) {
    state.sessionId = null;
    state.filter = state.filter === "session" ? "all" : state.filter;
  } else if (String(state.sessionId) === String(id) && state.filter === "session") {
    state.sessionId = null;
    state.filter = "all";
  } else {
    state.sessionId = Number(id);
    state.filter = "session";
  }
  // 会话与标签是两条浏览轴：点会话就退出标签范围（否则两条一起 AND，多半空列表）
  if (state.sessionId !== null) state.tag = null;
  state.selectedId = null;
  state.confirmSession = null;
  revealChain(); // 选中链展开（清掉链上的显式折叠），见 foldStateFor
  if (ensureListRoute()) return; // 从设置页/复习页点会话：先回列表，再按新筛选拉数据
  loadAll({ skeleton: true });
}

function selectTag(tag) {
  const value = tag ? String(tag).replace(/^#/, "") : null;
  state.tag = state.tag === value ? null : value;
  // 点标签 = 切到「整库这个标签」：同会话范围互斥。
  // 原来它和会话 AND 在一起（#B站 × 小红书会话 = 0 条），看着像标签功能坏了（用户报障）。
  if (state.tag !== null) {
    state.sessionId = null;
    if (state.filter === "session") state.filter = "all";
  }
  state.selectedId = null;
  if (ensureListRoute()) return;
  loadAll({ skeleton: true });
}

// 不在列表页时，任何「选会话/选标签/改筛选/搜索」都先切回列表：
// 否则在设置页点侧栏只会高亮、主区纹丝不动（用户报障「会话被框死」）。
function ensureListRoute() {
  if (state.route === "list") return false;
  window.location.hash = "#/list"; // hashchange 会带着新状态重新拉数据
  return true;
}

function focusSearch() {
  dom.search.focus();
  dom.search.select();
}

function focusSelectedRow() {
  const row = dom.main.querySelector(`.row[data-id="${state.selectedId}"]`);
  if (!row) return;
  row.focus({ preventScroll: true });
  row.scrollIntoView({ block: "nearest" });
}

function moveSelection(delta) {
  // v4.1（#54 P2-4）：选中行移动 → 收掉挂着的释义 popover（否则它钉在旧行的高亮词上）
  closePopover();
  const order = visibleOrder();
  if (!order.length) return;
  const index = order.findIndex((record) => record.id === state.selectedId);
  let next;
  if (index < 0) next = delta >= 0 ? 0 : order.length - 1;
  else next = Math.max(0, Math.min(order.length - 1, index + delta));
  state.selectedId = order[next].id;
  highlightRow();
  focusSelectedRow();
}

function toggleHelp(open) {
  const next = open === undefined ? !dom.help.classList.contains("is-open") : open;
  dom.help.classList.toggle("is-open", next);
  dom.help.setAttribute("aria-hidden", next ? "false" : "true");
  if (orbit) orbit.setPaused(next); // 浮层盖住星图时停帧
}

function applyTheme(theme) {
  state.theme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = state.theme;
  dom.themeBtn.textContent = state.theme === "dark" ? "☀ 日间" : "☾ 夜间";
  if (orbit) orbit.refreshTheme(); // 星图的画布颜色跟着换，不用整页重绘
  try {
    window.localStorage.setItem(THEME_KEY, state.theme);
  } catch (error) {
    /* 隐私模式等场景下 localStorage 不可用：主题仍在本次会话生效 */
  }
}

function toggleTheme() {
  applyTheme(state.theme === "dark" ? "light" : "dark");
  // W6（票 #183）：设置页草稿的主题同步走 —— 顶栏按钮与 t 键共用这一个口，否则
  // 顶栏切完主题再点「保存设置」，form 里还是旧值，保存瞬间界面被弹回去。
  // renderMain 只是刷新设置页的 chip 选中态（其它路由没有草稿，不用重绘）。
  if (state.route === "settings" && state.settingsForm) {
    state.settingsForm.theme = state.theme;
    renderMain();
  }
}

function initTheme() {
  let saved = null;
  try {
    saved = window.localStorage.getItem(THEME_KEY);
  } catch (error) {
    saved = null;
  }
  applyTheme(saved === "dark" ? "dark" : "light"); // §8：首次无 localStorage 默认浅色
}

function isTyping(target) {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return (
    tag === "INPUT" ||
    tag === "TEXTAREA" ||
    tag === "SELECT" ||
    target.isContentEditable
  );
}

function handleEscape() {
  clearHoverTimers(); // v11 §1.1：Esc 连悬停计时器一起清（还等在 150ms 里的那次不再弹）
  cancelSentenceClickPlay(); // v16 T4：Esc 之后不许那次延迟起播再响起来
  if (closeSentenceMenu()) return; // v16 T4：右键菜单开着就先收菜单
  // v31：听看复习是最内层的连续任务 —— Esc 一次停掉整轮（音频 + 弹窗 + 控制条）。
  if (highlightReview) {
    stopHighlightReview();
    return;
  }
  // v16 C8（票 #137）：一次 Esc 只做一件事。v31 起听看复习比「播放中」更内层，
  // 已在上面先处理；其余优先级 = 播放中 → 浮标 → 其余层级。
  // v15 是「先收浮标并 return」，播放中得按两次 Esc 才停 —— 这里把播放提到最前。
  // v16 T5（C6）：合成中时 pronounceOwner / playbackMode 都已置位，「播放中」这一条就是
  // 合成中的停止分支（真的取消这一轮，而不是只收浮标让请求在后台跑完再冒出兜底语音）。
  if (playbackIsLong() || pronounceOwner) {
    stopPronounce(); // 停止语义不变（序号 +1 作废在途结果）；快照保留（C1）
    hideSelectionBubble();
    clearSelectionHighlight();
    return;
  }
  if (selectionBubbleOn()) {
    hideSelectionBubble(); // 只收浮标；快照保留，键盘入口还能再播（C2 / C8）
    clearSelectionHighlight(); // v16 C3：Esc 收浮标 → 自绘高亮一起清（快照保留，仍可重播）
    return;
  }
  stopPronounce(); // v11 §1：Esc 退出当前层时，当前发音一起停（不留尾音）
  if (dom.help.classList.contains("is-open")) {
    toggleHelp(false);
    return;
  }
  // v29：重查预览层在最上面，Esc 先收它；其次是 v23 的补释义失败清单。
  if (recheckReportOpen()) {
    closeRecheckDialog();
    return;
  }
  if (refillReportOpen()) {
    closeRefillReport();
    return;
  }
  if (isPopoverOpen()) {
    closePopover(); // §5.3：帮助 → popover → 抽屉 → …
    return;
  }
  if (state.drawer !== null) {
    closeDrawer();
    return;
  }
  // v14 T3：覆盖层先收 —— 侧栏 → 会话坞 → 动作区 → 再走既有的阅读页 / 路由链路
  if (state.route === "list" && state.shell.sidebar) {
    setSidebarOpen(false);
    return;
  }
  // v19 T2（票 #153）：会话坞开着（peek / pinned）先收坞，比动作区浅一层
  if (
    state.route === "list" &&
    document.body.dataset.dock &&
    document.body.dataset.dock !== "hidden"
  ) {
    setDock("hidden");
    return;
  }
  if (state.route === "list" && state.shell.header) {
    setHeaderOpen(false);
    return;
  }
  if (orbit && orbit.isFocused()) {
    orbit.unfocus(); // 星图：先退出聚焦阅读
    return;
  }
  if (state.route === "review") {
    window.location.hash = "#/list"; // §8.2：Esc 退出复习回列表
    return;
  }
  if (state.route === "settings") {
    // 设置页也有出口：Esc 回列表（没保存的键位草稿会丢掉）
    if (settingsDirty()) toast("已放弃未保存的键位改动");
    window.location.hash = "#/list";
    return;
  }
  if (state.route === "article") {
    window.location.hash = "#/list"; // v22 P1b：文章页出口（与「← 返回」同一动作）
    return;
  }
  if (state.route === "backlog") {
    leavePool(); // v34 T6：草稿 > 0 时先提示一次，再按一次才回列表
    return;
  }
  if (document.activeElement === dom.search && dom.search.value) {
    dom.search.value = "";
    state.query = "";
    loadAll({ skeleton: true });
  }
}

function isActivatable(target) {
  // Enter 是「开详情」，但不能抢走按钮 / 下拉 / 链接自己的 Enter 激活
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "BUTTON" || tag === "SELECT" || tag === "A";
}

function handleReviewKey(event, key) {
  if (key === " " || key === "Spacebar") {
    // v16 C11（票 #137，D6）：与 Enter 同款 —— 焦点在按钮 / 链接 / 下拉上时，Space 归控件
    // 自己（浮标聚焦时按 Space 是朗读，不是翻面）；只有「没聚焦控件」才当作揭示背面。
    if (isActivatable(event.target)) return;
    event.preventDefault(); // 不让 Space 滚页
    revealCard();
  } else if (GRADE_BY_KEY[key]) {
    event.preventDefault();
    gradeCurrent(GRADE_BY_KEY[key]);
  } else if (key === "t") {
    toggleTheme();
  } else if (key === "?") {
    toggleHelp(true);
  }
}

document.addEventListener("keydown", (event) => {
  if (event.defaultPrevented) return;
  const key = event.key;

  // §8.4 录制中：整个键盘都归录制器（Esc 取消），别让下面的列表快捷键插手
  if (state.record) {
    handleRecordKey(event);
    return;
  }

  if (key === "Escape") {
    handleEscape();
    return;
  }
  // 输入中 / 有抽屉 / 有帮助浮层：只保留 Esc
  if (isTyping(event.target) || state.drawer !== null) return;
  // v23 P1 / v29：补释义清单或重查预览开着时，全局快捷键（s / j / f …）不许穿透
  if (recheckReportOpen()) return;
  if (refillReportOpen()) return;
  if (dom.help.classList.contains("is-open")) {
    if (key === "?") toggleHelp(false);
    return;
  }
  // v16 T4（C11）：右键菜单里的按键归菜单项自己（原生 button 的 Tab / Enter / Space）——
  // 尤其复习页的 Space 揭示，不许把菜单项的激活抢走
  if (sentenceMenu && sentenceMenu.contains(event.target)) return;
  // v31：听看复习中的键盘 —— 空格暂停 / 继续，← → 上一条 / 下一条。
  // 只认裸键：Ctrl / Alt / Shift + 方向键留给浏览器（后退 / 扩选）；
  // 只有复习条自己的按钮放行原生激活；页面上的其它按钮（包括刚点的「听看」入口）
  // 不再让 Space 触发第二次起播，而是统一当暂停 / 继续。
  if (
    highlightReview &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey &&
    !event.shiftKey &&
    !(event.target instanceof Element && event.target.closest(".review-bar"))
  ) {
    if (key === " " || key === "Spacebar") {
      event.preventDefault();
      reviewTogglePause();
      return;
    }
    if (key === "ArrowLeft") {
      event.preventDefault();
      reviewGo(-1);
      return;
    }
    if (key === "ArrowRight") {
      event.preventDefault();
      reviewGo(1);
      return;
    }
  }
  // v16 T4（C4 #1）：原句聚焦时 Shift+Enter = 朗读选区 / 重播快照；两者都没有就不拦截。
  // 放在路由分支之前 —— 复习卡背面的原句也要能用（Enter 本身仍归 openDrawer / 复习）。
  if (
    event.shiftKey &&
    key === "Enter" &&
    !event.ctrlKey &&
    !event.metaKey &&
    !event.altKey
  ) {
    const node = sentenceOwnerFrom(event);
    if (node) {
      // 有得播才拦（preventDefault）；没得播也 return —— 但**不** preventDefault，
      // 浏览器默认行为照旧，同时别让下面的全局 Enter 把抽屉打开（那不是「不拦截」的本意）
      if (playSelectionFromKeyboard(node)) event.preventDefault();
      return;
    }
  }
  if (event.ctrlKey || event.metaKey || event.altKey) return;

  // §8.2 复习模式：Space 揭示、1–4 打分、t、?（Esc 见 handleEscape）
  if (state.route === "review") {
    handleReviewKey(event, key);
    return;
  }

  // §8.4 设置页：只留 t / ?（输入框里的按键由上面的 isTyping 挡掉）
  if (state.route === "settings") {
    if (key === "t") {
      toggleTheme(); // W6：草稿同步与 chip 刷新都在 toggleTheme 里
    } else if (key === "?") {
      toggleHelp(true);
    }
    return;
  }

  if (key === "/") {
    event.preventDefault();
    focusSearch();
  } else if (key === "s" && state.route === "list") {
    // v14 T2：s = 顺序通读（从最早一段开始）。阅读页里不抢：那里用 ←/→ 翻段
    if (orbit && orbit.isFocused()) return;
    event.preventDefault();
    startReadThrough();
  } else if (key === "[" && state.route === "list") {
    // v14 T3：侧栏开合（输入框内已由 isTyping 挡掉）
    event.preventDefault();
    setSidebarOpen(!state.shell.sidebar);
  } else if (key === "h" && state.route === "list") {
    // v14 T3：会话头动作层开合
    event.preventDefault();
    setHeaderOpen(!state.shell.header);
  } else if (key === "f" && state.route === "list") {
    // v14 T3：沉浸星图开关（列表视图下先切星图，见 toggleImmersive）
    event.preventDefault();
    toggleImmersive();
  } else if (key === "j" || key === "k") {
    // v8 星图：没有可上下移的行，j/k 与 ←/→ 一样是「换一段聚焦」
    if (orbit) {
      event.preventDefault();
      orbit.step(key === "j" ? 1 : -1);
      return;
    }
    event.preventDefault();
    moveSelection(key === "j" ? 1 : -1);
  } else if ((key === "ArrowLeft" || key === "ArrowRight") && !event.shiftKey) {
    // v16（#131 C4）：Shift 按住时 ←/→ 是浏览器自带的「扩选」—— 这里不拦也不
    // preventDefault，把扩选还给浏览器（阅读页的键盘扩选入口因此成立）。
    if (orbit) {
      event.preventDefault();
      orbit.step(key === "ArrowRight" ? 1 : -1);
    } else if (state.route === "article") {
      // W5（票 #183）：文章页 ←/→ 翻段，对齐星图「方向键换段」的手感
      //（连播中走连播的同一份步进，见 articleStep）。
      event.preventDefault();
      articleStep(key === "ArrowRight" ? 1 : -1);
    }
  } else if (key === "Enter" && state.route === "list") {
    // W4（票 #183）：Enter 只在列表路由开词条抽屉 —— 文章页按 Enter 不再打开
    // 上一路由残留的词条；也不能退而开「当前列表」的第一条（那是另一份筛选子集
    // 的数据，跟眼前这篇文章无关）。
    if (isActivatable(event.target)) return;
    if (state.selectedId === null) {
      const order = visibleOrder();
      if (order.length) state.selectedId = order[0].id;
    }
    if (state.selectedId !== null) {
      event.preventDefault();
      openDrawer(state.selectedId);
    }
  } else if (key === "r") {
    window.location.hash = "#/review";
  } else if (key === "t") {
    toggleTheme();
  } else if (key === "?") {
    toggleHelp(true);
  }
});

// §5.2：点击别处 / 滚动 / 改窗口大小 → 关掉 popover（高亮词自身点击已 stopPropagation）
document.addEventListener("click", (event) => {
  // v16 T4：点了原句以外的东西 = 那次「单击播整段」已经不是用户的意图
  if (!(event.target instanceof Element) || !event.target.closest(".row-sentence")) {
    cancelSentenceClickPlay();
  }
  // v16（票 #137，D8）：浮标收口已迁到 capture 阶段的 mousedown —— 点原句 / 点高亮词
  // 自己调了 stopPropagation，document click 根本收不到，只有 capture 一定先到。
  // 这里只留「点空白后把选区放回来」（复制仍可用）。
  restoreSelectionAfterBlankClick(event); // 点空白后把选区放回来（保留给复制）
  // v14 T3：遮罩收侧栏；沉浸态点会话坞外收动作区（点自己人不收）——
  // v19 T2：判定对象从左上胶囊换成右缘会话坞。仍要排除 .list-header 内的点击：
  // standard 胶囊里点拨片切回星图时，这次点击冒泡到 document 的瞬间外壳已经
  // 切成沉浸态（renderMain 同步完成），不排除的话刚建好的坞动作区被误收
  //（v14-verify-shell 节 11 抓到）。
  if (event.target === dom.scrim) setSidebarOpen(false);
  if (
    state.shell.header &&
    document.body.dataset.shell === "immersive" &&
    event.target.closest &&
    !event.target.closest(".orbit-dock") &&
    !event.target.closest(".list-header")
  ) {
    setHeaderOpen(false);
  }
  if (!isPopoverOpen()) return;
  if (popover && popover.contains(event.target)) return;
  closePopover();
});
window.addEventListener(
  "scroll",
  () => {
    closePopover();
    closeSentenceMenu(); // v16 T4：菜单是 fixed 坐标，滚动后位置失效
    hideSelectionBubble(); // v15 C3：滚动后浮标位置失效，收掉（选区本身不清）
  },
  { passive: true, capture: true }
);
window.addEventListener("resize", () => {
  closePopover();
  closeSentenceMenu(); // v16 T4：同上，resize 后 fixed 坐标失效
  cancelSentenceClickPlay(); // v16 T4：视口变了，那次延迟起播也不再代表用户的意图
  hideSelectionBubble(); // v16 C9：fixed 坐标失效，收浮标（快照保留）
});

dom.filters.addEventListener("click", (event) => {
  // v34 T6：筛选条右侧的「挑词」入口已撤除（待选句走顶栏一级入口）
  const chip = event.target.closest(".chip");
  if (!chip || chip.disabled) return;
  const filter = chip.dataset.filter;
  if (filter === "session" && state.sessionId === null) {
    toast(sessionPickHint());
    return;
  }
  if (filter === "session" || state.filter !== filter) state.selectedId = null;
  state.filter = filter;
  if (ensureListRoute()) return;
  loadAll({ skeleton: true });
});

// v6 §Implementation·一期：侧栏组头折叠。全侧栏**一个**委托（写法照顶栏筛选 chip）：
// 折叠态存 state.fold，侧栏每次全量重建都会从 state 复原。
dom.sidebar.addEventListener("click", (event) => {
  const all = event.target.closest("[data-fold-all]");
  if (all) {
    setAllFold(all.dataset.foldAll === "collapse");
    return;
  }
  const head = event.target.closest(".side-group-head");
  if (!head) return;
  const site = head.dataset.site || "";
  // 收/展方向按屏幕上的现状翻，不按缺省值猜
  const next = !(head.closest(".side-group").classList.contains("is-collapsed"));
  if (head.dataset.fold === "site") toggleFold("site", site, next);
  else if (head.dataset.fold === "account")
    toggleFold("account", foldKey(site, head.dataset.account || ""), next);
  else if (head.dataset.fold === "tags") toggleFold("tags", "tags", next);
});

// W13（票 #189）：删除确认态「点别处解除」。用 pointerdown（先于 click）：点到确认行
// 以外的任何动作都不会被「先弹确认」拦一道 —— 同一次点击里解除归解除、动作归动作。
// 确认行内的点（删除 / 取消 / 行本身）不解除，交给各自的 click。
document.addEventListener("pointerdown", (event) => {
  if (state.confirmSession === null) return;
  const target = event.target instanceof Element ? event.target : null;
  if (target && target.closest(".side-item-row.is-confirming")) return;
  state.confirmSession = null;
  renderSidebar();
});

dom.search.addEventListener(
  "input",
  debounce(() => {
    state.query = dom.search.value;
    state.selectedId = null;
    if (ensureListRoute()) return;
    loadAll({ skeleton: true });
  }, SEARCH_DEBOUNCE_MS)
);

dom.search.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    dom.search.blur();
    const order = visibleOrder();
    if (order.length) {
      state.selectedId = order[0].id;
      highlightRow();
      focusSelectedRow();
    }
  }
});

dom.reviewBtn.addEventListener("click", () => {
  window.location.hash = "#/review";
});

if (dom.brand) {
  dom.brand.addEventListener("click", () => {
    window.location.hash = "#/list";
  });
}
dom.themeBtn.addEventListener("click", toggleTheme);
dom.helpClose.addEventListener("click", () => toggleHelp(false));
dom.help.addEventListener("click", (event) => {
  if (event.target === dom.help) toggleHelp(false);
});
dom.backdrop.addEventListener("click", closeDrawer);
if (dom.handle) {
  // v19 T1（票 #152）：hover 把手 → peek 展开；移出把手∪侧栏 260ms 后收回。
  // 点击仍是钉住（setSidebarOpen 持久化），hover 预览与钉住并存不冲突。
  const sidebarZone = bindHoverZone(
    dom.handle,
    dom.sidebar,
    () => setSidebarPeek(true),
    () => setSidebarPeek(false)
  );
  dom.handle.addEventListener("click", () => {
    sidebarZone.disarm(); // 点击 = 明确指令，悬停计时器作废（防点击后被 re-peek 弹开）
    setSidebarOpen(!state.shell.sidebar);
  });
}

window.addEventListener("hashchange", () => {
  stopPronounce(); // §6：切路由即停（含在途请求作废）
  hidePickBubble(); // v33 T3：气泡是 body 级浮层，不随主区重建收起，切路由显式收
  invalidateSelectionRead(); // v16 C1：路由切换 = 上下文变化，快照不能跨页存活
  clearPrefetchQueue(); // §6：切路由清空**待发**预取队列（已发出的保留）
  const wasReview = state.route === "review";
  const wasSettings = state.route === "settings";
  const wasArticle = state.route === "article";
  const wasRoute = state.route;
  state.route = routeFromHash();
  // W9（票 #189）：真的换了页（不是同页参数抖动）就把焦点还给主区 —— 旧页的焦点
  // 落点马上随 renderMain 重建被拆掉，不接住就读屏 / 键盘用户每次都摔回 body。
  // main 自带 tabindex=-1；先于渲染聚焦也没关系，renderMain 只换 main 的孩子。
  if (state.route !== wasRoute) dom.main.focus({ preventScroll: true });
  if (state.route === "settings") {
    if (!wasSettings) loadSettings({ skeleton: true });
    startSettingsPoll(); // §8.4：保存后 ≤5s 轮询到外壳重探的状态
    return;
  }
  if (wasSettings) {
    // 离开设置页：停轮询、丢草稿（没保存的键位不跨页保留）
    stopSettingsPoll();
    state.settings = null;
    state.settingsForm = null;
    state.settingsDraft = {};
    state.record = null;
    state.confirmSession = null;
  }
  // v22 P1b（spec §8.2）：文章页 —— 换文章 / 首次进入才重拉；同一篇的路由抖动只重绘。
  if (state.route === "article") {
    if (
      !wasArticle ||
      !state.article ||
      Number(state.article.id) !== state.articleId
    ) {
      loadArticle(state.articleId, { skeleton: true });
    } else {
      renderAll();
    }
    return;
  }
  if (wasArticle) state.article = null; // 退出文章：整篇数据不跨路由保留
  if (state.route === "review") {
    // 换会话过滤（#/review?session=ID ↔ #/review）要重新拉队列；
    // loadReviewQueue 会先把 state.review 置成加载态并自己渲染
    if (!wasReview || !state.review || state.review.session !== state.reviewSession) {
      loadReviewQueue();
    } else {
      renderAll();
    }
    return;
  }
  if (wasReview) state.review = null; // 退出复习：本轮状态不跨路由保留
  // v34 T6（票 #219）：待选句池 —— 进入 / 换聚焦会话才重拉；同会话路由抖动只重绘。
  const wasPool = wasRoute === "backlog";
  if (state.route === "backlog") {
    poolLeaveArmed = false; // 每次进池子，Esc 的「先提示一次」重新计
    const focusId = state.poolId;
    if (
      !wasPool ||
      !state.pool ||
      (focusId !== null && String(state.pool.activeId) !== String(focusId))
    ) {
      loadPool(state.poolId, { skeleton: true });
    } else {
      renderAll();
    }
    return;
  }
  // 退出待选句：视图状态不跨路由保留，暂存草稿不动
  if (wasPool) state.pool = null;
  // 回到列表一律重拉：从设置页/复习页过来时筛选/搜索可能刚变过，
  // 光 renderAll 会拿旧数据画新筛选（用户报障的「点了没反应」就是这么来的）。
  loadAll({ skeleton: true });
});

/* --- 详情抽屉 ---------------------------------------------------------------- */

function routeFromHash() {
  // W4（票 #183）：路由切换清掉选中词条 —— 残留的 selectedId 会让文章页的 Enter 打开
  // 上一路由的词条抽屉。列表页自己的选中变化（搜索回车 / j-k / 点行）都会显式重设，
  // 不受影响。
  state.selectedId = null;
  const raw = String(window.location.hash || "").replace(/^#\/?/, "");
  const [path, search] = raw.split("?");
  const route = path.trim();
  // v22 P1b（票 #166 / spec §8.2）：`#/article/<doc_id>` 文章页（前进后退 / 直接打开都走这里）
  const article = /^article\/(\d+)$/.exec(route);
  if (article) {
    state.articleId = Number(article[1]);
    state.reviewSession = null;
    return "article";
  }
  state.articleId = null;
  // v34 T6（票 #219 / spec A1）：`#/backlog` 待选句池（可带 ?session=<id> 聚焦）
  if (route === "backlog") {
    const session = new URLSearchParams(search || "").get("session");
    state.poolId = session && /^\d+$/.test(session) ? Number(session) : null;
    state.reviewSession = null;
    return "backlog";
  }
  // `#/pick/<session_id>` 保留为一等别名，解析到同一页面；**不改写 hash**
  //（端到端脚本按 hash 里的 e2e= 分档，改写会把查询串冲掉）。
  const pick = /^pick\/(\d+)$/.exec(route);
  if (pick) {
    state.poolId = Number(pick[1]);
    state.reviewSession = null;
    return "backlog";
  }
  state.poolId = null;
  if (route === "review") {
    // §8.2：`#/review?session=ID` 只复习某条视频的词
    const session = new URLSearchParams(search || "").get("session");
    state.reviewSession = session && /^\d+$/.test(session) ? Number(session) : null;
    return "review";
  }
  // v8：`#/list?session=26&view=orbit` 直接落到某个会话的星图（书签 / 演示用）
  if (route === "list") {
    const params = new URLSearchParams(search || "");
    const session = params.get("session");
    const view = params.get("view");
    if (session && /^\d+$/.test(session)) {
      state.sessionId = Number(session);
      state.filter = "session";
    }
    if (view === "orbit" || view === "list") state.view = view;
    // v14 T2：`&read=first` = 顺序通读深链 —— 首屏直接聚焦最早一段
    if (params.get("read") === "first") state.startAtFirst = true;
  }
  state.reviewSession = null;
  if (route === "settings") return "settings";
  return "list";
}

function setDrawerOpen(open) {
  dom.drawer.classList.toggle("is-open", open);
  dom.backdrop.classList.toggle("is-open", open);
  dom.drawer.setAttribute("aria-hidden", open ? "false" : "true");
}

// W9（票 #189）：打开抽屉前的焦点落点（null = 没记住）。关抽屉时还给它，
// 读屏 / 键盘用户不再每次都摔回 body。换词条重开不覆盖 —— 归还的始终是
// 「最初打开前」的落点（口径与 refillReportReturnFocus 一致）。
let drawerReturnFocus = null;

function closeDrawer() {
  stopPronounce(); // §6：关了抽屉就别留尾音，也别留「正在播放」的状态
  invalidateSelectionRead(); // v16 C1：关抽屉 = 上下文变化（抽屉里的原句读不到了）
  state.drawer = null;
  state.confirmDelete = false;
  clear(dom.drawer);
  setDrawerOpen(false);
  // W9（票 #189）：焦点归还原落点；落点已随列表重绘被拆掉时退而求其次聚焦
  // 当前选中行（不在列表页 / 没有选中行时它自己是空操作），仍好过摔回 body。
  const back = drawerReturnFocus;
  drawerReturnFocus = null;
  if (back && back.isConnected) back.focus({ preventScroll: true });
  else focusSelectedRow();
  if (orbit) orbit.setPaused(false); // 抽屉收了，星图继续转
}

async function openDrawer(id, { force = false } = {}) {
  // W9（票 #189）：首次打开前先记住焦点落点 —— 点 popover 的「详情」时那个按钮马上
  // 会随 popover 一起被摘走，等 await 回来再记就只剩 body 了。换词条重开不覆盖。
  if (!state.drawer && document.activeElement instanceof HTMLElement)
    drawerReturnFocus = document.activeElement;
  // W1（票 #178）：换一条词条、而当前抽屉里有没保存的编辑 → 先问（继续编辑 = 留下），
  // 不再静默冲掉。重开同一条不算切换：renderDrawer 会把未保存编辑原样带回。
  if (!force && state.drawer && state.drawer.id !== id) {
    const edits = collectDrawerEdits();
    if (edits) {
      showDrawerSwitchConfirm(edits, id);
      return;
    }
  }
  // v4.1（#54 P2-3）：开抽屉先收 popover —— 否则它隐形挂着，把抽屉的第一次 Esc 吃掉
  closePopover();
  // 抽屉压在星图上滑入：先停帧，别让底层几十张卡每帧重排抢主线程（用户报障「开详情很卡」）
  if (orbit) orbit.setPaused(true);
  // #107 §6：换一条词条就先停掉上一条的发音（开同一个不打断，边听边看释义是允许的）
  if (state.drawer && state.drawer.id !== id) stopPronounce();
  const seq = ++drawerSeq; // 票 #72：高亮跟着最后一次点击，抽屉内容也要跟着
  state.selectedId = id;
  highlightRow();
  try {
    const payload = await api(`/api/records/${id}`);
    if (seq !== drawerSeq) return; // 旧响应不覆盖新抽屉（点了 A 再点 B，抽屉里必须是 B）
    state.drawer = payload.record;
    state.confirmDelete = false;
    renderDrawer();
    setDrawerOpen(true);
    dom.drawer.focus({ preventScroll: true });
  } catch (error) {
    if (orbit) orbit.setPaused(false); // 没打开：别把星图冻着
    if (seq !== drawerSeq) return; // 旧响应的错误也不落地（静默）
    toast(`打开详情失败：${error && error.message ? error.message : error}`);
  }
}

/* --- v11 §6/§7（票 #112）：在线开关、浏览器兜底与 warm 节流 ------------------- */

const TTS_WARM_THROTTLE_MS = 20 * 1000; // §4：活动事件最多每 20s warm 一次
const TTS_WARM_KEEPALIVE_MS = 10 * 1000; // v13 §5：页面活跃时每 10s 心跳保温一次
const TTS_WARM_CONNECTIONS = 2; // v13 §5（P1-1）：活跃时保 2 条热连接
const TTS_FALLBACK_MS = 60 * 1000; // §6/§10：后端不可用后 60s 内直接走浏览器兜底
// v16 T5（spec C6 / D7）：前端发音请求超时。后端最坏 ≈80s 才回 503（连接 10+10 + 轮 30 +
// SAPI 30），前端不能陪等；但 edge 现场合成实测会到 11.6s（2026-11-06 13:50 那轮）：
// 10s 会把「服务端已合成完、响应还没人接」的请求砍掉，接着触发 60s 浏览器兜底冷却，
// 连播就此中断。20s 覆盖实测最慢片；配合下面的跨段滚动预热，正常路径根本走不到这里。
const TTS_REQUEST_TIMEOUT_MS = 20 * 1000;
// 静音预热只发这一个固定短串：**绝不含任何词条文本**（§2/§6 的隐私口径）
const TTS_PREWARM_TEXT = "Pronunciation is ready.";

let ttsWarmAt = 0; // 上次 warm 的时刻（20s 节流）
let ttsActivityArmed = false; // 活动监听只装一次
let ttsPrewarmDone = false; // 静音预热每页只做一次
let browserVoicesPromise = null; // getVoices 为空的才等（最多 1s），结果缓存给后续兜底用
let pronounceFallbackUntil = 0; // 503 / 网络失败后的 60s 冷却
let pronounceFallbackWarned = false; // 「已用本机语音」只提示一次
let pronounceNoVoiceWarned = false; // 「没有可用的英文语音」只提示一次
// #114：长段落里某片退浏览器兜底时，要等它真正读完再播下一片（否则两路声音叠在一起）。
// stopPronounce / 新一轮播放会调这个收尾函数，让等待立即结束。
let fallbackWaitFinish = null;

// «在线发音是否开着»：只认 GET /api/settings 的 tts.enabled；读不到按开兜底。
// #113 的 prefetch / prewarm 也走这里，别各自再读一遍状态。
function ttsEnabled() {
  return !state.tts || state.tts.enabled !== false;
}

// §4：页面加载调一次；之后 pointermove / keydown / focus 节流到每 20s 一次。
// 关闭时（含还没读到状态但明确 enabled=false）一次都不发；页面不可见时不调。
function warmTts() {
  if (!ttsEnabled()) return Promise.resolve(false);
  if (document.hidden) return Promise.resolve(false);
  const now = Date.now();
  if (ttsWarmAt && now - ttsWarmAt < TTS_WARM_THROTTLE_MS) return Promise.resolve(false);
  ttsWarmAt = now;
  return api("/api/tts/warm", { method: "POST" }).catch(() => null); // 预热失败静默
}

// v13 §5（P1-1）：页面活跃时保 1–2 条热连接 —— 靠周期心跳，而不是无条件常驻。
// 心跳带 `{"connections": 2}` 让后端把两条都重建 / 保温；活动事件那次 warm 仍是
// 一条（不带 body）。可见 + 在线才发；隐藏 / 关闭在线时一个请求都不发。
function ttsWarmBeat() {
  if (!ttsEnabled()) return Promise.resolve(false);
  if (document.hidden) return Promise.resolve(false);
  const now = Date.now();
  // 刚 warm 过（页面加载 / 活动事件）就不必再心跳一次
  if (ttsWarmAt && now - ttsWarmAt < TTS_WARM_KEEPALIVE_MS) return Promise.resolve(false);
  ttsWarmAt = now;
  return api("/api/tts/warm", jsonRequest("POST", { connections: TTS_WARM_CONNECTIONS })).catch(
    () => null // 保温失败静默：下一次心跳 / 用户点击还会重建连接
  );
}

function startTtsActivity() {
  if (ttsActivityArmed) return;
  ttsActivityArmed = true;
  const touch = () => {
    warmTts(); // 节流在 warmTts 里；失败静默（预热只是加速，不影响发音）
  };
  document.addEventListener("pointermove", touch, { passive: true });
  document.addEventListener("keydown", touch, true);
  window.addEventListener("focus", touch);
  window.setInterval(ttsWarmBeat, TTS_WARM_KEEPALIVE_MS); // v13 §5：心跳保温 2 条
  warmTts(); // §4：页面加载（tts 状态就绪后）这一次
  silentTtsPrewarm(); // §2/§6：静音预热每页一次
}

// §2/§6：`volume=0` 的固定短串预热一次，把首枪的平台初始化提前做掉。
// 只发固定短串、不发用户文本；关掉在线时只会选本地 en-*（没有就跳过，不提示）。
function silentTtsPrewarm() {
  if (ttsPrewarmDone) return;
  ttsPrewarmDone = true;
  if (!("speechSynthesis" in window)) return;
  browserVoices().then((voices) => {
    const voice = pickBrowserVoice(voices, ttsEnabled());
    if (!voice) return; // 没有可用英文 voice：静默跳过
    try {
      const utterance = new SpeechSynthesisUtterance(TTS_PREWARM_TEXT);
      utterance.voice = voice;
      utterance.lang = voice.lang || "en-US";
      utterance.volume = 0;
      window.speechSynthesis.speak(utterance);
    } catch (error) {
      /* 静音预热失败不影响任何功能 */
    }
  });
}

// §6：等 voice 列表就绪 —— 有就立即返回；否则最多等 1s 的 voiceschanged；超时返回 []。
// **绝不无限等**（Chrome 可能一直不发事件，见 §2 的能力探测）。
function browserVoices() {
  if (!("speechSynthesis" in window)) return Promise.resolve([]);
  const synth = window.speechSynthesis;
  const ready = typeof synth.getVoices === "function" ? synth.getVoices() : [];
  if (ready && ready.length) return Promise.resolve(ready);
  if (browserVoicesPromise) return browserVoicesPromise;
  browserVoicesPromise = new Promise((resolve) => {
    let settled = false;
    const finish = (voices) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      if (typeof synth.removeEventListener === "function") {
        synth.removeEventListener("voiceschanged", onVoices);
      }
      resolve(Array.isArray(voices) ? voices : []);
    };
    const onVoices = () => {
      const voices = typeof synth.getVoices === "function" ? synth.getVoices() : [];
      if (voices && voices.length) finish(voices);
    };
    const timer = window.setTimeout(() => {
      finish(typeof synth.getVoices === "function" ? synth.getVoices() : []);
    }, 1000);
    if (typeof synth.addEventListener === "function") {
      synth.addEventListener("voiceschanged", onVoices);
    }
  });
  return browserVoicesPromise;
}

// §2/§6：开时优先 Online + Natural 的 en-US，再任意 Online en-*，最后本地 en-*；
// 关时**只允许本地 en-***（名字带 Online 的一律不选），保证一个包都不发。
function pickBrowserVoice(voices, onlineEnabled) {
  const list = Array.isArray(voices) ? voices : [];
  const english = list.filter((voice) => /^en[-_]/i.test(String(voice.lang || "")));
  const online = english.filter((voice) => /Online/i.test(String(voice.name || "")));
  const local = english.filter(
    (voice) =>
      !/Online/i.test(String(voice.name || "")) && voice.localService !== false
  );
  if (!onlineEnabled) return local.length ? local[0] : null;
  const natural = online.filter(
    (voice) =>
      /Natural/i.test(String(voice.name || "")) &&
      /^en-US/i.test(String(voice.lang || ""))
  );
  if (natural.length) return natural[0];
  if (online.length) return online[0];
  return local.length ? local[0] : null;
}

// §6/§10 兜底 2：浏览器本地语音。返回是否真的发声。
// 没有可用英文 voice → 轻提示一次「这台机器没有可用的英文语音」，不阻塞其它功能。
// v16 T5（C6 / 研究 §5.10 E4）：兜底路径也要标记 owner（`is-speaking` 与播放条语义一致），
// 并在 utterance 收尾（end / error，含按字数兜底的 guard 超时）时走「非循环收尾」原语 ——
// 否则 playbackMode / Chunks / Origin 会永久悬挂，下一次 playbackIsLong() 误判为真，状态就错乱。
// `owner` / `seq` 只在「由某一轮播放发起」时给：收口必须比对话号，否则迟到的事件会打断新一轮。
function speakFallback(text, { warn = false, owner = null, seq = null } = {}) {
  const phrase = String(text == null ? "" : text).trim();
  if (!phrase) return Promise.resolve(false);
  // 兜底也走不通（没有 speechSynthesis / 没有英文 voice / speak 抛错）时：把「合成中」收掉，
  // 选区快照标 failed —— 否则浮标会停在「◌ 合成中」或退回「▶ 朗读选中」，都不是真相。
  const giveUp = () => {
    if (seq !== null && seq !== pronounceSeq) return;
    markSelectionSynthesizing(false);
    if (owner && playbackMode === "selection") markSelectionReadFailed(owner, phrase);
  };
  if (!("speechSynthesis" in window)) {
    giveUp();
    if (warn) warnNoEnglishVoice();
    return Promise.resolve(false);
  }
  return browserVoices().then((voices) => {
    // 等 voice 列表的这段时间里可能已经被新的一轮播放 / stopPronounce 取代：别打断新的一轮
    if (seq !== null && seq !== pronounceSeq) return false;
    const voice = pickBrowserVoice(voices, ttsEnabled());
    if (!voice) {
      giveUp();
      if (warn) warnNoEnglishVoice();
      return false;
    }
    try {
      if (window.speechSynthesis.speaking) window.speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(phrase);
      utterance.voice = voice;
      utterance.lang = voice.lang || "en-US";
      utterance.volume = 1;
      // 收口幂等：`stopPronounce()` 里的 `speechSynthesis.cancel()` 也会触发 end/error，
      // 那时会话已被 pausePronounceAudio 重置（pronounceOwner=null、seq 已变），这里直接空转。
      // 用 clearPronounceState + playbackSessionReset（endPlayback 的非循环分支原语），
      // **不走 endPlayback**：循环开启时它会立刻 replay，而后端还坏着 → 兜底朗读无限重来。
      let guard = 0;
      const release = () => {
        if (guard) {
          window.clearTimeout(guard);
          guard = 0;
        }
        if (seq !== null && seq !== pronounceSeq) return;
        if (owner && pronounceOwner !== owner) return;
        clearPronounceState();
        playbackSessionReset();
      };
      utterance.addEventListener("end", release);
      utterance.addEventListener("error", release);
      // 同上 #114 的口径：有的浏览器不派发 end —— 按字数给一个宽松上限（120ms/字 ≈ 比正常
      // 朗读慢一倍），别让 is-speaking 永久挂着；正常浏览器 end 先到，guard 只是兜底。
      guard = window.setTimeout(release, Math.max(6000, phrase.length * 120));
      if (owner) markPronounceOwner(owner);
      window.speechSynthesis.speak(utterance);
      selectionSpeechStarted(); // 兜底真的开口了：浮标不能停在「合成中」
      if (warn) warnFallbackSpeech();
      return true;
    } catch (error) {
      // speak 抛错 = 这轮根本没出声：把刚点亮的播放态收掉，别留一个假的 is-speaking
      if (owner && pronounceOwner === owner) {
        clearPronounceState();
        playbackSessionReset();
      }
      giveUp(); // 浮标 → 「⚠ 重试」（快照保留）
      return false; // 只是发音，失败不阻塞别的功能
    }
  });
}

// #114 §2.1：长段落里某片退浏览器兜底时，等它读完再继续下一片（不叠音）。
// 与 speakFallback 同一个音色选择/profile；`end` / `error` / 停止 / 超时都会收尾。
function speakFallbackWait(text, { warn = false } = {}) {
  const phrase = String(text == null ? "" : text).trim();
  if (!phrase) return Promise.resolve(false);
  if (!("speechSynthesis" in window)) {
    if (warn) warnNoEnglishVoice();
    return Promise.resolve(false);
  }
  return browserVoices().then((voices) => {
    const voice = pickBrowserVoice(voices, ttsEnabled());
    if (!voice) {
      if (warn) warnNoEnglishVoice();
      return false;
    }
    return new Promise((resolve) => {
      let done = false;
      let guard = 0;
      const finish = (ok) => {
        if (done) return;
        done = true;
        if (guard) window.clearTimeout(guard);
        if (fallbackWaitFinish === finish) fallbackWaitFinish = null;
        resolve(ok);
      };
      const utterance = new SpeechSynthesisUtterance(phrase);
      utterance.voice = voice;
      utterance.lang = voice.lang || "en-US";
      utterance.volume = 1;
      utterance.addEventListener("end", () => finish(true));
      utterance.addEventListener("error", () => finish(false));
      fallbackWaitFinish = finish;
      // 兜底：有的浏览器不派发 end（或音频设备卡住）——按字数给一个上限，别让整段永远等。
      guard = window.setTimeout(() => finish(true), Math.max(4000, phrase.length * 60));
      try {
        if (window.speechSynthesis.speaking) window.speechSynthesis.cancel();
        window.speechSynthesis.speak(utterance);
        if (warn) warnFallbackSpeech();
      } catch (error) {
        finish(false);
      }
    });
  });
}

function warnNoEnglishVoice() {
  if (pronounceNoVoiceWarned) return;
  pronounceNoVoiceWarned = true;
  toast("这台机器没有可用的英文语音");
}

function warnFallbackSpeech() {
  if (pronounceFallbackWarned) return;
  pronounceFallbackWarned = true;
  toast("已用本机语音（在线发音不可用）");
}

/* --- 发音（v11 §6：单例播放器 + 内存 blob 缓存 + 请求去重） ------------------- */

// `pronounce(text, owner)` 是前端**唯一**的发音入口：抽屉先接上，后面四处
// （高亮词 / 原句 / 复习卡 / 阅读页）都复用它，不再各写一套 Audio。
const PRONOUNCE_CACHE_MAX = 80; // §6：内存缓存上限，约 80 条 / 20MB
const PRONOUNCE_CACHE_BYTES = 20 * 1024 * 1024;
// v16 C7：两张表的键都是 `voice|rate|text`（pronounceCacheKey）—— 换音色后同一文本必须
// 落成新键（缓存 miss → 新请求），旧音色的条目留在表里自然走 LRU 淘汰。
const pronounceCache = new Map(); // key -> { url, size }：Map 迭代顺序就是 LRU 顺序
const pronouncePending = new Map(); // key -> Promise：同键并发只发一次请求
let pronounceCacheBytes = 0;
let pronounceSeq = 0; // 递增序号：快速连点时迟到的旧结果不播放
let pronounceAudio = null; // 单例播放器：任意时刻最多一路音频（§9）
let pronounceOwner = null; // 当前挂 `is-speaking` 的元素
let pronounceWarned = false; // 失败只轻提示一次（§6/§10）

// v16 C7：缓存键含音色与语速 —— 换音色后重播同一段必须重新合成（S13）。
// 语速暂由后端 status 缺席而恒为空串，字段位先占住；键变化自然让旧条目走 LRU 淘汰。
// 键按**当前**设置算（不是快照里的 voice/rate）：用户中途改设置，重播即落到新键。
// `state.tts` 为 null（后端离线）时键退化为 `||text` —— 可接受：那时在线发音本来就不可用。
function pronounceCacheKey(text) {
  const params = selectionSpeechParams();
  return `${params.voice}|${params.rate}|${text}`;
}

function pronouncePlayer() {
  if (!pronounceAudio) {
    pronounceAudio = new Audio();
    pronounceAudio.preload = "auto";
    // 播完撤掉播放态。长段落由 pronounceLong 自己收尾（否则每片都会清一次）；
    // v15 C2：单词 / 单片整段的自然收尾也要走 endPlayback —— 循环才接得上。
    pronounceAudio.addEventListener("ended", () => {
      if (!pronounceLongQueue.length && !pronounceLongWait) endPlayback();
    });
    // v16 T5（C6）：真的开声 = 退出浮标的「合成中」态（play() 兑现时播放已经开始）
    pronounceAudio.addEventListener("playing", () => selectionSpeechStarted());
    // v16 T5（C6 / 研究 §5.10 E2）：坏 blob / 解码失败 / 缓冲卡死不许让 `is-speaking`、
    // 播放条与进度 rAF 永久挂着（原来只挂 `ended`，坏音频等于把页面钉在「正在播」）。
    // 只在**确有活动播放**时动作：换 src / 空闲时迟到的旧事件不该打断现在这一轮。
    // 长段落正在等某片 `ended`（pronounceLongWait 非空）时，`error` 交给 `playPronounceUrl`
    // 自己的分片收口（跳过坏片、继续下一片，末片走 endPlayback 全清）—— 那里不会悬挂，
    // 也不该因为一片坏 blob 把整段朗读判死（v15 既有行为）。`stalled` 没有分片级监听，
    // 卡住的那片永远等不到 `ended`，必须在这里收（不能只让循环干等）。
    const onAudioFault = () => {
      if (!pronounceOwner && !playbackIsLong()) return;
      const selectionFault = playbackMode === "selection";
      const owner = pronounceOwner;
      const text = playbackText;
      pronounceSeq += 1; // 作废在途结果，长段落循环下一次 playbackHold 就退出
      pausePronounceAudio(); // = clearPronounceState() + playbackSessionReset() + 队列收口
      if (selectionFault) markSelectionReadFailed(owner, text);
    };
    pronounceAudio.addEventListener("error", () => {
      logPlaybackEvent("audio_error", {
        code: Number(pronounceAudio.error && pronounceAudio.error.code) || 0,
      });
      if (pronounceLongWait) return; // 分片链路自己在收（见上）
      onAudioFault();
    });
    // `stalled` 在**缓冲**时也可能触发（刚换 src、元数据还没到时浏览器就是在等数据），
    // 那时没有可收的会话、也不该把一轮刚开始的播放判死 —— 只在「确实已经在出声」时动作。
    pronounceAudio.addEventListener("stalled", () => {
      if (!pronounceAudio || pronounceAudio.paused || !(pronounceAudio.currentTime > 0)) return;
      logPlaybackEvent("audio_stalled"); // 缓冲卡死：以前只改 UI，日志一个字都没有
      onAudioFault();
    });
  }
  return pronounceAudio;
}

function clearPronounceState() {
  speakProgressClear();
  if (pronounceOwner) {
    pronounceOwner.classList.remove("is-speaking");
    pronounceOwner.classList.remove("is-paused"); // v15 C1：暂停态跟播放态一起收
    const plate =
      typeof pronounceOwner.closest === "function" ? pronounceOwner.closest(".orbit-plate") : null;
    if (plate) plate.classList.remove("is-paused");
  }
  pronounceOwner = null;
}

function markPronounceOwner(owner) {
  if (pronounceOwner && pronounceOwner !== owner) clearPronounceState();
  pronounceOwner = owner || null;
  if (pronounceOwner) pronounceOwner.classList.add("is-speaking");
  renderPlaybar(); // v15 C2：播放条只依赖播放状态（单词发音不显示）
}

/* --- v15 C1/C2（票 #126）：暂停 / 续播 与阅读页播放条 ------------------------
 * 「暂停」和「停止」是两件事（GLOSSARY v15）：
 *   - 暂停 = 只 audio.pause()，保留队列 / 前瞻 / 片内 currentTime / 进度；进度 rAF 因
 *     currentTime 不动而自然冻结；等片期间暂停只置标记，fetch 回来不播。
 *   - 停止 = stopPronounce()（现有语义不变）：序号 +1 作废在途结果，pausePronounceAudio
 *     清队列 / 清前瞻 / 归零 / 清状态。
 * 暂停中的 for 循环在 playbackHold 里等续播；停止 / 换段会把等待一起解开（seq 已变，
 * 循环醒来直接 return）—— 两套语义就在这里分开，别在别处混用。
 */
let pronouncePaused = false; // 只表示「本次朗读暂停」，从不代表停止
let playbackMode = ""; // "" / "word" / "long" / "selection"
let playbackText = ""; // 这次朗读的原文：toggle 用它判定「同文本」
let playbackChunks = []; // 这次朗读的分片（重播本段 / 循环用）
let playbackOrigin = null; // 选区朗读在 owner.textContent 里的起始偏移（喂进度）
let playbackLoop = false; // 循环开关（只在内存里，不持久化）
let playbackResumeAudio = false; // 暂停那一刻音频正在响：续播直接 play()；等片期间暂停则为 false
let playbackWake = null; // 暂停时 playbackHold 的收尾函数

/* --- v31「听看复习」：只播高亮词条，释义弹窗与音频同时出场 ------------------
 * 队列来自 `web/review-plan.js`（纯函数）：段卡/段落顺序不变，同一句里的重叠高亮
 * 按 词 → 短语 → 句 播。播放复用 pronounceLong（缓存 / 前瞻 / 暂停 / 兜底全继承），
 * 每一条先把弹窗锚到 mark 上、再起播；音频结束停留 REVIEW_HOLD_MS 再下一条。
 * 用户在队列外点别的发音 / 停止 / 切路由：stopPronounce 或序号复核会让本轮静默退出。 */
const REVIEW_HOLD_MS = 900; // 音频播完后留给「看释义」的时间
const REVIEW_EXIT_MS = 100; // 弹窗淡出后再挪到下一条
const REVIEW_FOCUS_WAIT_MS = 620; // 星图换卡飞行收敛（fk 每帧逼近 1，约 0.5s）
const REVIEW_SCROLL_WAIT_MS = 320; // 滚动到目标词后等它落定再定位弹窗
let highlightReview = null; // { token, plan, pos, paused, step, source, label, holdTimer, wake }
let highlightReviewToken = 0;
let reviewAdvancing = false; // 驱动器自己换卡 / 停止时的再入护栏（同 autoReadAdvancing）

// --- 连播 / 自动挡（2026-10-24）：从当前段起自动顺序播到本图末段，到头即停 ---
// 只在本会话（本次进星图重建的 deck）内走，不环绕、不跨图；内存态不持久化（同 playbackLoop）。
// 开关与循环互斥（二选一，endPlayback 的循环分支优先）；暂停不清开关——暂停中 ended 不触发，
// 驱动器自然冻结，续播后接着走。
let autoReadActive = false; // 连播开关
let autoReadAdvancing = false; // 驱动器换段中的再入护栏：step(1) 内部的 stopPronounce 别把自己关掉
let autoReadSeq = -1; // 驱动器认领的播放会话序号：endPlayback 靠它判定「播完的是不是我发起的那轮」
let autoReadTimer = 0; // 过渡窗口（提示音 + 停顿）的定时器句柄；任何手动停止都要一并取消
// 段间过渡 = 提示音（约 0.2s）+ 剩下的静默停顿（约 0.9s），给回忆留一口气。
// 2026-10-24 用户真机反馈：原 0.6s 太赶、音量偏轻 —— 窗口加到 1.1s，音量 0.12→0.22。
const AUTO_READ_GAP_MS = 1100;

function playbackIsLong() {
  return playbackMode === "long" || playbackMode === "selection";
}

function playbackWakeUp() {
  const wake = playbackWake;
  playbackWake = null;
  if (wake) wake();
}

// 只清「本次朗读」的会话状态；音频 / 队列 / 进度由各自的销毁原语处理
function playbackSessionReset() {
  pronouncePaused = false;
  playbackWakeUp();
  playbackMode = "";
  playbackText = "";
  playbackChunks = [];
  playbackOrigin = null;
  playbackLoop = false;
  playbackResumeAudio = false;
  // v16 T5（C6）：会话一旦收掉，浮标就不能再停在「合成中」——停止 / 自然播完 / 音频异常
  // 全都会经过这里（或经过 pausePronounceAudio → 这里），不必在每个出口各写一遍。
  if (selectionSynthesizing) markSelectionSynthesizing(false);
  renderPlaybar();
}

// 暂停视觉：纸面 + 原句，只换配色（不加布局、不改字号）
function markPlaybackPaused(on) {
  if (!pronounceOwner) return;
  pronounceOwner.classList.toggle("is-speaking", !on);
  pronounceOwner.classList.toggle("is-paused", on);
  const plate =
    typeof pronounceOwner.closest === "function" ? pronounceOwner.closest(".orbit-plate") : null;
  if (plate) plate.classList.toggle("is-paused", on);
}

function pausePlayback() {
  if (!playbackIsLong() || pronouncePaused) return;
  pronouncePaused = true;
  playbackLoop = false; // C2：暂停即关循环（要循环得重新开）
  // 只暂停：不递增序号、不清队列 / 前瞻、不归零、不销毁播放状态
  playbackResumeAudio = Boolean(pronounceAudio && !pronounceAudio.paused);
  if (playbackResumeAudio) pronounceAudio.pause();
  markPlaybackPaused(true);
  renderPlaybar();
}

function resumePlayback() {
  if (!pronouncePaused) return;
  pronouncePaused = false;
  markPlaybackPaused(false);
  const audio = pronounceAudio;
  // 暂停在片内：从停处续播；暂停在等片时音频已停/已结束，交给 playbackHold 播下一片
  if (playbackResumeAudio && audio && audio.getAttribute("src")) {
    const started = audio.play();
    if (started && typeof started.catch === "function") {
      started.catch(() => {
        if (!pronouncePaused) warnPronounce("");
      });
    }
  }
  playbackResumeAudio = false;
  playbackWakeUp();
  renderPlaybar();
}

// 暂停中不取片不播；停止 / 换段（seq 变了，暂停标记被清）就返回 false 让循环退出
async function playbackHold(seq) {
  while (pronouncePaused && seq === pronounceSeq) {
    await new Promise((resolve) => {
      playbackWake = resolve;
    });
  }
  return seq === pronounceSeq;
}

function togglePlayback(owner, text) {
  const phrase = String(text == null ? "" : text);
  // v16 C10（票 #137，D8）：先看「活动会话是选区」—— 那条会话的 playbackText 是**选中片段**，
  // 而 phrase 是整句，靠文本相等永远判不上，会把「点原句想暂停」变成停掉选区、改播整段。
  // 选区会话同一 owner → 直接暂停 / 继续（与播放条一致），不比对文本。
  if (playbackMode === "selection" && pronounceOwner === owner) {
    if (pronouncePaused) resumePlayback();
    else pausePlayback();
    return;
  }
  if (playbackIsLong() && pronounceOwner === owner && playbackText === phrase) {
    if (pronouncePaused) resumePlayback();
    else pausePlayback();
    return;
  }
  startSentencePlayback(owner, phrase);
}

// v16 T4：这一句正在播整段吗（点它该是暂停 / 继续，不是重新开播）
function isActiveSentencePlayback(owner, text) {
  const phrase = String(text == null ? "" : text);
  return Boolean(playbackIsLong() && pronounceOwner === owner && playbackText === phrase);
}

// 点原句的起点：整段仍按句切分（#114）—— 多片顺序播，单片（≤200）一次请求
function startSentencePlayback(owner, text) {
  const phrase = String(text == null ? "" : text).trim();
  if (!phrase) return;
  const chunks = splitForSpeech(phrase);
  if (chunks.length > 1) pronounceLong(chunks, owner, { mode: "long", text: phrase });
  else pronounce(phrase, owner, { mode: "long" });
}

// ↻ 重播本段：从第 1 片重来；循环开关保持用户当前选择
function replayPlayback() {
  if (!playbackIsLong() || !pronounceOwner || !playbackChunks.length) return;
  const mode = playbackMode;
  const owner = pronounceOwner;
  const chunks = playbackChunks.slice();
  const text = playbackText;
  const origin = playbackOrigin;
  const keepLoop = playbackLoop;
  // 连播中重播：重播是同一会话的延续，播完还要继续往下走 —— 把认领续到新一轮
  const keepAuto = autoReadActive && autoReadSeq >= 0;
  const before = pronounceSeq;
  if (chunks.length > 1) pronounceLong(chunks, owner, { mode, text, origin, keepLoop });
  else pronounce(chunks[0], owner, { mode, origin, keepLoop });
  if (keepAuto && pronounceSeq > before) autoReadSeq = pronounceSeq;
}

function togglePlaybackLoop() {
  if (!playbackIsLong()) return;
  playbackLoop = !playbackLoop;
  if (playbackLoop && autoReadActive) autoReadActive = false; // 与连播二选一：开循环即关连播
  renderPlaybar();
}

/* --- 连播 / 自动挡（2026-10-24）------------------------------------------------
 * 在既有自然收口（endPlayback）上接一段续播编排：每段播完 → 过渡音 + 停顿 →
 * step(1) 切下一段 → startSentencePlayback 起播，全部复用现有链路（预取 /
 * 前瞻 / 暂停 / 进度 / 播放条原样继承）。不碰 pronounce / pronounceLong / 滚轮 / 后端。
 * 会话归属：驱动器起播时记 autoReadSeq；endPlayback 里序号对不上（中途点单词发音、
 * 选区朗读等把会话顶掉）只关旗标、不前进，避免抢用户的拍。
 */
function cancelAutoReadGap() {
  if (autoReadTimer) {
    window.clearTimeout(autoReadTimer);
    autoReadTimer = 0;
  }
}

// 过渡提示音：Web Audio 现场合成（约 0.2s 柔和下滑短音），不走 TTS —— 零延迟、
// 不碰发音缓存，也就不给「fallback 到本机音色」开任何新窗口。合成失败静默跳过：
// 过渡音是装饰，不能因为它打断连播。
let autoReadAudioCtx = null;
function playAutoReadChime() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    if (!autoReadAudioCtx) autoReadAudioCtx = new Ctx();
    const ctx = autoReadAudioCtx;
    if (ctx.state === "suspended" && ctx.resume) ctx.resume();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(880, now); // A5 → D5 下滑，一句「翻页」的手感
    osc.frequency.exponentialRampToValueAtTime(587.33, now + 0.18);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.22, now + 0.02); // 中低音量：听得见但不抢正文（真机校过）
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.2);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.22);
  } catch (error) {
    // 忽略：见上
  }
}

// 驱动器起播一段：起播 + 认领会话 + 预取下一段首片。
// startSentencePlayback 内部（pronounce / pronounceLong 开头）会同步 ++pronounceSeq，
// 返回时新会话序号已定 —— endPlayback 靠比对 autoReadSeq 判定归属。
function startAutoSegment(owner, text) {
  const before = pronounceSeq;
  startSentencePlayback(owner, text);
  if (pronounceSeq === before) {
    autoReadActive = false; // 空文本等没起成播：别让开关空挂着
    renderPlaybar();
    return;
  }
  autoReadSeq = pronounceSeq;
  prefetchAutoNext();
}

// 趁当前段还在播，把下一段的**全部分片**交给前瞻通道备好。
// 2026-10-26 修：原来只预取前 2 片、而且走通用 `prefetch()` 那唯一一条后台槽位 ——
// 翻页时 focusCard 排的 8 个目标词会把它们压到最后，段尾现取就要等 1–2s 合成。
// 现在整段排进前瞻通道（自己的预算 + 收口补位），段尾命中率接近 100%。
// 下一段是无原句词卡时备它的目标词（2026-10-25：连播要能读出词卡）。
function prefetchAutoNext() {
  const cur = orbit ? orbit.current() : null;
  if (!cur) return;
  const next = orbit.itemAt(cur.index + 1);
  if (!next) return;
  if (next.context) primeLongBatch(splitForSpeech(next.context));
  const primary = next.records[0];
  if (primary && primary.target) primeLongBatch([primary.target]);
}

// 当前阅读页这一段的连播起播素材：有原句 → 整段；无原句词卡 → 词头 + 目标词；
// 两者皆无（极端空卡）→ null，连播只能诚实收场。
function autoReadEntry() {
  const plate = document.querySelector(".orbit-plate.is-reading");
  const owner = orbitReadPlayEntry(plate ? plate.querySelector(".orbit-read") : null);
  if (!owner) return null;
  const cur = orbit ? orbit.current() : null;
  const item = cur ? cur.item : null;
  if (item && item.context) return { owner, text: item.context };
  const target = item && item.records[0] ? item.records[0].target : "";
  if (target) return { owner, text: target };
  return null;
}

// 推进到下一段。到头即停：deck 每次进星图都重建，current() 的 total 就是本图段数，
// 天然「只在本会话内走」；不环绕（与顺序通读的到头语义一致）。
function advanceAutoRead() {
  const cur = orbit ? orbit.current() : null;
  if (!cur || cur.index < 0 || cur.index >= cur.total - 1) {
    autoReadActive = false;
    renderPlaybar();
    toast("已播到最后一段，连播结束");
    return;
  }
  // 2026-10-26：翻页与起播解耦 —— 音频一结束就把页翻过去，让眼睛先落到下一段开头，
  // 衔接音 + 停顿仍按原节奏（AUTO_READ_GAP_MS）走完再起播。原来两者绑在同一个定时器里，
  // 翻页要等过渡窗口，视觉上就成了「翻页在等音频」。
  autoReadAdvancing = true;
  let entry = null;
  try {
    orbit.step(1); // 唯一换段入口；内部 focusCard → stopPronounce 由护栏豁免
    entry = autoReadEntry(); // 有原句播整段；无原句词卡读目标词（2026-10-25）
  } finally {
    autoReadAdvancing = false;
  }
  // 翻页本身经过 stopPronounce（seq 已递增）：重新认领，下面的比对才认得住这一轮。
  autoReadSeq = pronounceSeq;
  if (!entry) {
    autoReadActive = false; // 连词都没有的空卡：收在诚实的地方
    renderPlaybar();
    toast("这一段没有可朗读的内容，连播结束");
    return;
  }
  playAutoReadChime(); // 过渡音照旧紧接音频结尾（与翻页同帧，听感不变）
  autoReadTimer = window.setTimeout(() => {
    autoReadTimer = 0;
    if (!autoReadActive || !orbit) return;
    // 停顿期间用户又点了别的发音：让位，不抢拍
    if (pronounceSeq !== autoReadSeq) {
      autoReadActive = false;
      renderPlaybar();
      return;
    }
    // 这 1.1s 里阅读页可能被重渲染过（owner 已脱离文档）：起播前按当前 DOM 重新解析一次。
    const live = autoReadEntry();
    startAutoSegment(live ? live.owner : entry.owner, live ? live.text : entry.text);
  }, AUTO_READ_GAP_MS);
}

// 播放条「▶▶ 连播」开关。正在播时点它 = 本轮播完自动接下一段；没在播（理论上进不到
// 这分支，播放条只在播放中可见）则从当前阅读页这一段直接起播。
function toggleAutoRead() {
  cancelAutoReadGap();
  if (autoReadActive) {
    autoReadActive = false;
    renderPlaybar();
    return;
  }
  autoReadActive = true;
  playbackLoop = false; // 与循环二选一：开连播即关循环（endPlayback 的循环分支优先）
  if (playbackIsLong() && pronounceOwner) {
    autoReadSeq = pronounceSeq; // 认领当前这轮会话：它播完就接着往下走
    prefetchAutoNext(); // 下一段首片现在就去排队，别等段尾才现取
    renderPlaybar();
    return;
  }
  const entry = autoReadEntry(); // 有原句播整段；无原句词卡读目标词
  if (entry) {
    startAutoSegment(entry.owner, entry.text);
    return;
  }
  autoReadActive = false; // 不在阅读页 / 这一段没有任何可朗读内容：开不起来
  renderPlaybar();
}

// 播放自然收尾（整段播完 / 单片播完）：循环开就从头再播，否则清状态、收播放条
function endPlayback() {
  if (playbackIsLong() && playbackLoop && playbackChunks.length && pronounceOwner) {
    replayPlayback();
    return;
  }
  clearPronounceState();
  playbackSessionReset();
  // 连播驱动器：只有「这轮确实是驱动器发起 / 认领的会话」才往前走。
  // 会话被顶掉（中途点单词发音 / 选区朗读等，序号对不上）→ 只关旗标，不抢用户的拍。
  if (!autoReadActive) return;
  if (autoReadAdvancing || autoReadSeq < 0 || pronounceSeq !== autoReadSeq) {
    autoReadActive = false;
    renderPlaybar();
    return;
  }
  advanceAutoRead();
}

// 播放条是阅读页 DOM 里的常驻一行：只切 is-on + 文字，不重建节点。
// 每张读过的卡各有一条；只有「当前 owner 所在的那条」可见（其余 display:none）。
function renderPlaybar() {
  // v31：听看复习有自己的控制条；阅读页那条常规播放条（暂停 / 连播 / 循环）先收起来，
  // 免得两条一起出现、点错按钮。复习结束 / 停止后 highlightReview 清空，常规条自然恢复。
  const active = playbackIsLong() && Boolean(pronounceOwner) && !highlightReview;
  for (const bar of document.querySelectorAll(".orbit-playbar")) {
    const read = bar.closest(".orbit-read");
    // 播放入口：句卡 = 原句；无原句词卡 = is-playable 词头（2026-10-25）——
    // 词头在播时这条 bar 也要亮，否则词卡上没有暂停 / 停止 / 连播可用。
    const entry = orbitReadPlayEntry(read);
    const on = active && Boolean(entry) && entry === pronounceOwner;
    bar.classList.toggle("is-on", on);
    if (!on) continue;
    const toggle = bar.querySelector(".orbit-playbar-toggle");
    if (toggle) toggle.textContent = pronouncePaused ? "▶ 继续" : "⏸ 暂停";
    const loop = bar.querySelector(".orbit-playbar-loop");
    if (loop) {
      loop.classList.toggle("is-on", playbackLoop);
      loop.setAttribute("aria-pressed", playbackLoop ? "true" : "false");
    }
    const auto = bar.querySelector(".orbit-playbar-auto");
    if (auto) {
      auto.classList.toggle("is-on", autoReadActive);
      auto.setAttribute("aria-pressed", autoReadActive ? "true" : "false");
    }
  }
  // v16 T2（D5）：浮标文案也由播放会话推导，所以跟播放条在同一处收口 ——
  // markPronounceOwner / pausePlayback / resumePlayback / playbackSessionReset /
  // togglePlaybackLoop 都已经调 renderPlaybar()，浮标借此和播放条永远同步。
  // 注意 renderSelectionBubble() 只改内容、不点亮浮标（可见性归用户动作与既有出口）。
  renderSelectionBubble();
}

/* --- v31「听看复习」：只播高亮词条 + 释义弹窗同步出场 -----------------------
 * 入口：会话列表 / 星图（当前筛选）、星图阅读页（本段）、文章页（全文 / 单段）。
 * 队列由 `ReviewPlan` 生成；每一条按顺序：
 *   1) 备好目标所在的卡（列表原地 / 星图聚焦 / 文章换段）；
 *   2) 滚动到目标词，留出弹窗空间；
 *   3) 打开**既有**释义 popover（`.is-review` 入场动画），同一拍起播 TTS；
 *   4) 播完停留 REVIEW_HOLD_MS 让用户看释义，再淡出弹窗、进入下一条。
 * 播放复用 pronounceLong：缓存 / 前瞻 / 暂停 / 503 兜底 / 进度全继承。
 * 用户在队列外点别的发音（seq 变化）或按停止 / Esc / 切路由：本轮静默退出，不抢拍。
 */

function reviewPlanForSession() {
  return ReviewPlan.buildSessionPlan(reviewSessionCards(), targetSpanForRecord);
}

function startSessionHighlightReview() {
  if (state.route !== "list" || state.loading || state.error || !state.records.length) return;
  const plan = reviewPlanForSession();
  if (!plan.length) {
    toast("当前范围没有高亮词条可听看");
    return;
  }
  // 星图最多铺 80 段；计划里有段不在图上时，直接切列表（列表能滚到全部卡）。
  if (currentView() === "orbit") {
    const visible = new Set();
    if (orbit) {
      for (const card of orbitDeck().cards) {
        for (const record of card.records) visible.add(String(record.id));
      }
    }
    if (orbit && plan.every((item) => visible.has(String(item.record.id)))) {
      startHighlightReview(plan, { source: "orbit", label: "听看复习" });
      return;
    }
    setView("list");
    toast("听看复习：段数超过星图上限，已切到列表");
  }
  startHighlightReview(plan, { source: "list", label: "听看复习" });
}

function startOrbitCardHighlightReview() {
  const current = orbit ? orbit.current() : null;
  if (!current || !current.item) return;
  const item = current.item;
  const plan = ReviewPlan.buildSessionPlan(
    [{ key: item.key, context: item.context, records: item.records }],
    targetSpanForRecord
  );
  if (!plan.length) {
    toast("本段没有高亮词条可听看");
    return;
  }
  startHighlightReview(plan, { source: "orbit-card", label: "听看本段" });
}

function startArticleHighlightReview() {
  if (state.route !== "article" || !state.article) return;
  const plan = ReviewPlan.buildArticleHighlightPlan(state.article);
  if (!plan.length) {
    toast("这篇文章没有高亮词条可听看");
    return;
  }
  startHighlightReview(plan, { source: "article", label: "听看本文" });
}

function startArticleParagraphHighlightReview(index) {
  const paragraphs =
    state.route === "article" && state.article && Array.isArray(state.article.paragraphs)
      ? state.article.paragraphs
      : [];
  const paragraph = paragraphs.find((item) => Number(item.index) === Number(index));
  if (!paragraph) return;
  const plan = ReviewPlan.buildArticleHighlightPlan({ paragraphs: [paragraph] });
  if (!plan.length) {
    toast("本段没有高亮词条可听看");
    return;
  }
  startHighlightReview(plan, { source: "article", label: `听看第 ${Number(index) + 1} 段` });
}

function startHighlightReview(plan, options = {}) {
  if (!Array.isArray(plan) || !plan.length) {
    toast("没有可听看的高亮词条");
    return;
  }
  if (highlightReview) clearHighlightReview({ silent: true });
  // 复习是独占的一路音频：先收掉文章连播 / 星图连播 / 当前发音。
  if (articleRead) resetArticleReadSilently();
  cancelAutoReadGap();
  autoReadActive = false;
  autoReadSeq = -1;
  renderPlaybar();
  reviewAdvancing = true;
  try {
    stopPronounce();
  } finally {
    reviewAdvancing = false;
  }
  closePopover();
  highlightReviewToken += 1;
  highlightReview = {
    token: highlightReviewToken,
    plan: plan.slice(),
    pos: -1,
    step: 0,
    played: 0,
    paused: false,
    source: options.source || "list",
    label: options.label || "听看复习",
    holdTimer: 0,
    holdResolve: null,
    wake: null,
  };
  // v31：弹窗动画不等网络，冷缓存下音频会比弹窗晚到 —— 起复习就把手上要播的**可见文本**
  // 整条计划交既有预热通道。列表加载时那批预热喂的是词条原始拼写，键不同（大小写 / 词形
  // 随原句），命中不了这里。
  prewarmTargets(plan.map((item) => item.playText));
  renderReviewBar();
  reviewGo(1, { fromStart: true });
}

// 停止 / 切路由 / 被别的发音顶掉：收 UI 与等待，不在这里动音频（调用方决定）。
function clearHighlightReview({ silent = false } = {}) {
  const session = highlightReview;
  if (!session) return;
  highlightReview = null;
  session.step += 1; // 让在途的 async 步骤醒来后直接退出
  reviewWake(session);
  closePopover();
  renderReviewBar();
  if (!silent) toast("听看复习已停止");
}

function stopHighlightReview() {
  if (!highlightReview) return;
  clearHighlightReview({ silent: false });
  reviewAdvancing = true;
  try {
    stopPronounce();
  } finally {
    reviewAdvancing = false;
  }
}

function finishHighlightReview() {
  const session = highlightReview;
  if (!session) return;
  // 先置空再 stopPronounce：它会跳过 clearHighlightReview，最后一条弹窗才留得住。
  highlightReview = null;
  session.step += 1;
  reviewWake(session);
  stopPronounce();
  renderReviewBar();
  toast(`听看复习完成：${session.played} 条高亮`);
  // 最后一条的释义弹窗保留（用户还要看），点别处 / Esc 自然收起。
}

// 换一条 / 上一条 / 下一条：同一套入口。越界给一句提示，不环绕。
function reviewGo(delta, { fromStart = false } = {}) {
  const session = highlightReview;
  if (!session) return;
  const target = fromStart ? 0 : session.pos + delta;
  if (target < 0) {
    toast("已经是第一条高亮");
    return;
  }
  if (target >= session.plan.length) {
    finishHighlightReview();
    return;
  }
  reviewWake(session); // 唤醒上一条还在等的 hold / 换卡 / 滚动定时器
  session.pos = target;
  session.step += 1;
  session.paused = false; // 手动换条 = 明确的继续动作
  renderReviewBar();
  runHighlightReviewItem(session, session.step);
}

function reviewTogglePause() {
  const session = highlightReview;
  if (!session) return;
  session.paused = !session.paused;
  if (session.paused) {
    if (playbackIsLong() && !pronouncePaused) pausePlayback();
  } else {
    if (pronouncePaused) resumePlayback();
    reviewWake(session);
  }
  renderReviewBar();
}

// 等待原语：可被停止 / 暂停 / 下一条提前唤醒，避免 async 步骤挂在定时器上。
function reviewSleep(session, ms) {
  return new Promise((resolve) => {
    session.holdResolve = resolve;
    session.holdTimer = window.setTimeout(() => {
      session.holdTimer = 0;
      session.holdResolve = null;
      resolve();
    }, ms);
  });
}

function reviewWake(session) {
  if (!session) return;
  if (session.holdTimer) {
    window.clearTimeout(session.holdTimer);
    session.holdTimer = 0;
  }
  if (session.holdResolve) {
    const resolve = session.holdResolve;
    session.holdResolve = null;
    resolve();
  }
  if (session.wake) {
    const wake = session.wake;
    session.wake = null;
    wake();
  }
}

async function runHighlightReviewItem(session, step) {
  const token = session.token;
  const alive = () => highlightReview === session && session.token === token && session.step === step;
  const item = session.plan[session.pos];
  if (!item) {
    finishHighlightReview();
    return;
  }
  // 上一条的弹窗先淡出（首条通常没有）；淡出期间不改 DOM 内容，避免闪。
  if (isPopoverOpen()) await closeReviewPopover(alive);
  if (!alive()) return;
  if (!(await reviewPrepareView(session, item, alive))) {
    if (alive()) clearHighlightReview({ silent: true });
    return;
  }
  if (!alive()) return;
  await reviewAwaitResume(session, alive);
  if (!alive()) return;
  const anchor = reviewAnchorFor(session, item);
  if (!anchor) {
    // 数据刚变 / 未定位词条没有可锚定的 mark：跳过这一条，不让整轮卡死。
    if (session.pos + 1 >= session.plan.length) finishHighlightReview();
    else reviewGo(1);
    return;
  }
  if (!(await reviewRevealAnchor(session, anchor.mark, alive))) return;
  if (!alive()) return;
  await reviewAwaitResume(session, alive);
  if (!alive()) return;
  // 弹窗与音频同一拍：先开弹窗（CSS 入场动画），紧接着 pronounceLong 起播。
  openPopover(anchor.mark, [item.record], { review: true });
  const owner = anchor.owner || anchor.mark;
  const origin = Number.isFinite(anchor.origin) ? anchor.origin : null;
  const run = pronounceLong([item.playText], owner, {
    mode: "long",
    text: item.playText,
    origin,
  });
  const playSeq = pronounceSeq;
  session.played += 1;
  await run;
  if (!alive()) return;
  if (pronounceSeq !== playSeq) {
    // 队列外有人点了别的发音 / 按了停止：让位，只收复习 UI，不抢这一轮。
    clearHighlightReview({ silent: true });
    return;
  }
  await reviewHold(session, alive);
  if (!alive()) return;
  if (pronounceSeq !== playSeq) {
    clearHighlightReview({ silent: true }); // 停留期间用户点了别的发音：让位
    return;
  }
  if (session.pos + 1 >= session.plan.length) finishHighlightReview();
  else reviewGo(1);
}

// 暂停落在「换卡 / 滚动 / 弹窗淡出」阶段：等继续再开下一条，不抢跑。
async function reviewAwaitResume(session, alive) {
  while (alive() && session.paused) {
    await new Promise((resolve) => {
      session.wake = resolve;
    });
  }
  return alive();
}

// 播完后留给用户看释义的时间；暂停时冻结，继续后接着数。
async function reviewHold(session, alive) {
  let left = REVIEW_HOLD_MS;
  while (alive() && left > 0) {
    if (session.paused) {
      await new Promise((resolve) => {
        session.wake = resolve;
      });
      continue;
    }
    const slice = Math.min(left, 80);
    const started = performance.now();
    await reviewSleep(session, slice);
    left -= performance.now() - started;
  }
  return alive();
}

async function reviewPrepareView(session, item, alive) {
  if (session.source === "orbit" || session.source === "orbit-card") {
    if (!orbit) return false;
    if (session.source === "orbit") {
      const index = reviewOrbitIndexFor(item);
      if (index < 0) return false;
      const current = orbit.current();
      if (!current || current.index !== index) {
        // 聚焦换卡会经过 stopPronounce：用 reviewAdvancing 豁免本轮，别把自己停掉。
        reviewAdvancing = true;
        try {
          orbit.focus(index);
        } finally {
          reviewAdvancing = false;
        }
        await reviewSleep(session, REVIEW_FOCUS_WAIT_MS);
        return alive();
      }
    }
    return true;
  }
  if (session.source === "article") {
    if (state.route !== "article" || !state.article) return false;
    const index = Number(item.paragraphIndex);
    if (Number.isFinite(index) && index !== state.articleIndex) {
      // 文章页一次只渲染一张卡；换段后 mark 立刻可查。用瞬切让弹窗定位稳定，
      // 平滑感交给弹窗自己的入场动画。
      showArticleParagraph(index, { animate: false });
    }
    return alive();
  }
  return state.route === "list";
}

function reviewOrbitIndexFor(item) {
  if (!orbit) return -1;
  // 用 orbitDeck() 而不是 orbit.current()：星图可以还没聚焦任何卡（current() = null），
  // 但 deck 顺序就是 createOrbit 建卡时用的顺序，未聚焦时也能定位到目标卡。
  const cards = orbitDeck().cards;
  for (let index = 0; index < cards.length; index += 1) {
    const candidate = cards[index];
    if (!candidate || !Array.isArray(candidate.records)) continue;
    if (candidate.records.some((record) => String(record.id) === String(item.record.id))) {
      return index;
    }
  }
  return -1;
}

function reviewAnchorFor(session, item) {
  let nodes;
  if (session.source === "article") {
    const card = articleCard(item.paragraphIndex);
    nodes = card ? card.querySelectorAll(".para-text .ctx-mark") : [];
  } else if (session.source === "orbit" || session.source === "orbit-card") {
    const read = document.querySelector(".orbit-plate.is-reading .orbit-read");
    nodes = read ? read.querySelectorAll(".ctx-mark") : [];
  } else {
    nodes = dom.main.querySelectorAll(".row-sentence .ctx-mark, .row-targets .ctx-mark");
  }
  const matches = [];
  for (const mark of nodes) {
    const records = Array.isArray(mark.__records) ? mark.__records : [];
    if (
      records.some(
        (record) => record === item.record || String(record.id) === String(item.record.id)
      )
    ) {
      matches.push(mark);
    }
  }
  if (!matches.length) return null;
  let mark = matches[0];
  if (item.span) {
    const exact = matches.find(
      (node) =>
        node.__segment &&
        node.__segment.start === item.span.start &&
        node.__segment.end === item.span.end
    );
    if (exact) mark = exact;
  }
  const sentence = mark.closest(".row-sentence");
  if (sentence) {
    return { mark, owner: sentence, origin: item.span ? item.span.start : null };
  }
  const paragraph = mark.closest(".para-text");
  if (paragraph) {
    return { mark, owner: paragraph, origin: item.span ? item.span.start : null };
  }
  return { mark, owner: mark, origin: null };
}

function reviewRevealAnchor(session, mark, alive) {
  const rect = mark.getBoundingClientRect();
  const top = 70;
  const bottom = window.innerHeight - 96;
  if (rect.top >= top && rect.bottom <= bottom) return Promise.resolve(alive());
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  mark.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
  return reviewSleep(session, reduced ? 40 : REVIEW_SCROLL_WAIT_MS).then(() => alive());
}

function closeReviewPopover(alive) {
  return new Promise((resolve) => {
    if (!isPopoverOpen()) {
      resolve(true);
      return;
    }
    if (!popover || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      closePopover();
      resolve(Boolean(alive()));
      return;
    }
    popover.classList.add("is-closing");
    window.setTimeout(() => {
      if (!alive()) {
        resolve(false); // 已经换到下一步 / 停止：别关新一步刚打开的弹窗
        return;
      }
      popover.classList.remove("is-closing");
      closePopover();
      resolve(true);
    }, REVIEW_EXIT_MS);
  });
}

let reviewBar = null;

function ensureReviewBar() {
  if (reviewBar) return reviewBar;
  const bar = el("div", "review-bar");
  bar.setAttribute("role", "group");
  bar.setAttribute("aria-label", "听看复习控制");
  const title = el("div", "review-bar-title");
  title.append(el("span", "review-bar-label", "听看复习"));
  title.append(el("span", "review-bar-count", ""));
  const track = el("div", "review-bar-track");
  track.append(el("div", "review-bar-fill"));
  const prev = button("btn btn-subtle review-bar-btn review-bar-prev", "◀ 上一条", () =>
    reviewGo(-1)
  );
  prev.title = "上一条高亮（←）";
  const toggle = button("btn btn-subtle review-bar-btn review-bar-toggle", "⏸ 暂停", () =>
    reviewTogglePause()
  );
  toggle.title = "暂停 / 继续（空格）";
  const next = button("btn btn-subtle review-bar-btn review-bar-next", "下一条 ▶", () =>
    reviewGo(1)
  );
  next.title = "下一条高亮（→）";
  const stop = button("btn btn-subtle review-bar-btn review-bar-stop", "■ 停止", () =>
    stopHighlightReview()
  );
  stop.title = "停止听看复习（Esc）";
  bar.append(title, track, prev, toggle, next, stop);
  document.body.append(bar);
  reviewBar = bar;
  return bar;
}

function renderReviewBar() {
  const bar = ensureReviewBar();
  const session = highlightReview;
  const on = Boolean(session);
  bar.classList.toggle("is-on", on);
  // 视觉上用 opacity 收；同时从无障碍树 / Tab 链里摘掉（opacity 0 仍会被读屏看见）。
  bar.inert = !on;
  bar.setAttribute("aria-hidden", on ? "false" : "true");
  if (!on) return;
  const count = session.plan.length ? (session.pos + 1) / session.plan.length : 0;
  const label = bar.querySelector(".review-bar-label");
  if (label) label.textContent = session.label;
  const countNode = bar.querySelector(".review-bar-count");
  if (countNode) countNode.textContent = `${session.pos + 1}/${session.plan.length}`;
  const fill = bar.querySelector(".review-bar-fill");
  if (fill) fill.style.width = `${Math.max(0, Math.min(1, count)) * 100}%`;
  const toggle = bar.querySelector(".review-bar-toggle");
  if (toggle) toggle.textContent = session.paused ? "▶ 继续" : "⏸ 暂停";
  const prev = bar.querySelector(".review-bar-prev");
  if (prev) prev.disabled = session.pos <= 0;
  const next = bar.querySelector(".review-bar-next");
  if (next) next.disabled = session.pos >= session.plan.length - 1;
}

/* --- v15 C3（票 #126）：选区朗读 + 拖选守卫 ----------------------------------
 * 在阅读页 / 列表卡 / 复习卡的原句里拖选 → 松手时在选区末端浮出单例小按钮
 * 「▶ 朗读选中」（position: fixed）；点它才播，不自动播（想复制却出声最恼人）。
 * 起止偏移在**未装饰**的 DOM 上先算好：首次播放的 decorateSentence 会替换文本节点、
 * 销毁活动选区，但原句 textContent 逐字节不变（v12 不变量），偏移仍然有效。
 *
 * v16 T1（票 #132，spec C1 / D1 / D2）：选区快照改成**常驻单例**，与播放会话解耦 ——
 * 播放开始 / 暂停 / 自然结束 / 失败都不清快照；清空只发生在显式失效钩子
 * （invalidateSelectionRead）。重播只读快照，不再回头解析 getSelection()。
 */
const DRAG_SELECT_PX = 6; // 位移超过这个距离才算拖选（不是点击）
// v16 T4（票 #135，spec C4 / D6）：兜底入口的两个去抖常量，与拖选阈值放一起，真机可调。
const SELECTION_CHANGE_DEBOUNCE_MS = 100; // selectionchange 兜底：选区停下来多久才认
const SENTENCE_CLICK_DELAY_MS = 250; // 单击原句的起播延迟：给双击的判定留出窗口（§5.9 D5）
let dragProbe = null; // { owner, pointerId, x, y, dragging }
let selectionBubble = null; // 单例浮标
// v16 T1（票 #132）：选区快照单例 —— 字段见 createSelectionRead。
// 生命周期只跟「用户的新选择 / 上下文变化」走：播放开始 / 暂停 / 结束 / 失败都不清它（C1）。
let selectionRead = null;
let pendingSelectionRanges = null; // 点空白前存下的选区（点完恢复，复制仍可用）
// v16 T5（C6）：浮标的「第 0 态」标记 —— 已发起请求、尚未开声。
// 选显式开关（而不是推导 `!audio.paused`）是为了让「开声」由 `playing` 事件精确定义：
// 音频 currentTime 在首帧还是 0，推导会在开声瞬间仍判成「合成中」，浮标文案抖一下。
let selectionSynthesizing = false;
let selectionFallbackTimer = 0; // selectionchange 兜底的去抖计时器
// v16 T4（D6）：我们自己写选区（点空白后恢复）时记下它 —— 兜底不得为这次改动出浮标（会闪）
let programmaticRanges = null;
// v16 T4（C5）：跨句 / 跨容器提示的闩锁 —— 同一段操作只提示一次
let crossSelectionToastShown = false;
let sentenceMenu = null; // 原句右键菜单（body 单例）
let sentenceMenuOwner = null; // 菜单当前挂在哪句上（动作要用）
let sentenceMenuText = ""; // 菜单「朗读整段」要播的原文
let sentenceClickTimer = 0; // 单击原句的延迟起播
let sentenceClickAct = null; // { owner, text } —— 到点要播的那一段
// v16 T4：指针还按着吗 —— 拖选途中不许兜底把浮标浮出来（会挡住松手落点）
let pointerHeld = false;
let fallbackSuppressed = false; // 按着期间被跳过的兜底：松手后补跑一次
let pointerOnOwnUI = false; // 这一轮按下的落点是不是我们自己的浮标 / 菜单

function hasSelection() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return false;
  return Boolean(String(selection.toString()).trim());
}

function beginSentenceDrag(owner, event) {
  if (event.button !== 0) return;
  // v16（票 #137）：记下 pointerId —— pointermove / pointerup 只认同一根手指，
  // 多指同时按 / 先抬起的那根不会把别的指针的探针清掉或误判成拖选。
  dragProbe = {
    owner,
    pointerId: event.pointerId,
    x: event.clientX,
    y: event.clientY,
    dragging: false,
  };
}

function isDragSelecting() {
  return Boolean(dragProbe && dragProbe.dragging);
}

function selectionBubbleOn() {
  return Boolean(selectionBubble && selectionBubble.classList.contains("is-on"));
}

// 选区起点 / 终点在 owner.textContent 里的偏移（Range 跨文本节点累加）
function selectionOffsetAt(owner, container, offset) {
  const range = document.createRange();
  range.selectNodeContents(owner);
  range.setEnd(container, offset);
  return String(range.toString()).length;
}

// v16 T1（C1）：快照的合成参数 —— 音色取在线发音状态块（v11 §7 的唯一真源）。
// 语速现在只活在 config.ini（后端 status 不暴露 tts_rate），记空串表示「未知」，
// 等后端补上这个字段后这里自然就有值。
function selectionSpeechParams() {
  const tts = state.tts || null;
  return {
    voice: String((tts && tts.voice) || ""),
    rate: String((tts && tts.rate) || ""),
  };
}

// v16 T1（C1）：建快照。归一化文本给 TTS，切片原文 / 偏移给定位与高亮（T3），
// chunks 建好就存 —— 重播只读快照，不再二次解析选区（D2）。
function createSelectionRead(owner, raw, start, end) {
  const text = String(raw == null ? "" : raw).replace(/\s+/g, " ").trim();
  if (!owner || !text) return null;
  const params = selectionSpeechParams();
  return {
    owner,
    text, // 归一化文本：送 TTS，也是重播的唯一输入
    slice: String(owner.textContent || "").slice(start, end), // 原句切片原文：定位 / 高亮用（C3）
    start,
    end,
    chunks: splitForSpeech(text),
    voice: params.voice,
    rate: params.rate,
    at: Date.now(),
    played: false, // v16 T2（C2）：这一快照起播过没有 —— 播完才给「↻ 重播这段」
    // v16 T5（C6 / D7）：上一轮朗读失败过 → 浮标是「⚠ 重试」。快照本身照旧保留（C1），
    // 新选区换进来自然回到 false。
    failed: false,
  };
}

// 快照还能用吗：原句节点必须还挂在文档上（复习卡揭示 / 列表重渲染会整块换 DOM）
function selectionReadLive() {
  return Boolean(selectionRead && selectionRead.owner && selectionRead.owner.isConnected);
}

// v16 T1（C1）：快照失效的**唯一**出口 —— 上下文变化（换段 / 换卡 / 切路由 / 关抽屉 /
// 原句改存 / 进出沉浸态）走这里，视觉顺手收掉。播放的停止仍由各处的 stopPronounce 负责，
// 两者别混：停止是播放会话的事，这里只管「选了哪段」还成不成立。
function invalidateSelectionRead() {
  selectionRead = null;
  selectionSynthesizing = false; // v16 T5：快照没了就没有「第 0 态」可言
  hideSelectionBubble();
  clearSelectionHighlight(); // v16 C3：上下文失效 → 自绘高亮一起清（高亮不能指向即将脱离文档的节点）
  // v16（票 #137，D4）：程序化恢复选区的残留缓存跟着快照一起清 —— 否则 Esc /
  // stopPropagation 吞掉那次 click 后，很久以前的选区会在下一次普通点击时复活。
  pendingSelectionRanges = null;
}

function ensureSelectionBubble() {
  if (selectionBubble) return selectionBubble;
  const node = button("selection-read", "▶ 朗读选中", playSelection);
  node.type = "button";
  node.setAttribute("aria-label", "朗读选中的文字");
  node.setAttribute("aria-busy", "false");
  // 按下别把选区塌掉：文本在松手时已经快照过，但保留高亮更符合「复制仍可用」
  node.addEventListener("mousedown", (event) => event.preventDefault());
  selectionBubble = node;
  document.body.append(node);
  return node;
}

// v16（#131 C11）：浮标的播报角色 —— 页面级视觉隐藏的 live region 单例。
// 只在浮标真的浮出时更新一次 textContent（更新即播报），polite 不打断读屏。
// 启动时就建好（见文件末尾）：aria-live 节点「刚插入就写内容」多数读屏不播报。
let selectionLive = null;

function ensureSelectionLive() {
  if (selectionLive) return selectionLive;
  selectionLive = el("div", "selection-live");
  selectionLive.setAttribute("role", "status");
  selectionLive.setAttribute("aria-live", "polite");
  document.body.append(selectionLive);
  return selectionLive;
}

function announceSelectionBubble(text) {
  ensureSelectionLive().textContent = text;
}

// v16 T5（C6 / D7）：浮标的第 0 态「合成中」与失败态「⚠ 重试」。
// 两个态不在这里写文案 —— 统一并入 renderSelectionBubble() 的推导（唯一渲染点），
// 这里只保留状态开关本身。判定用显式开关而不是推导 `!pronounceAudio.paused`：音频开声的
// 第一个瞬间 currentTime 仍是 0，推导会把「已开声」误判成「合成中」，文案会抖一下；
// `playing` 事件才是「真的开声」的权威口径。
function markSelectionSynthesizing(on) {
  selectionSynthesizing = Boolean(on);
  renderSelectionBubble();
}

// v16 T5（C6）：真的开声 = 退出「合成中」。中间的合成期浮标必须留着做第 0 态反馈；
// 开声后浮标不收起 —— T2 的「播放中浮标常驻」由 renderSelectionBubble() 推导出「⏸ 暂停」。
// **失败态不收**：要让用户看得见、点得到「⚠ 重试」。
function selectionSpeechStarted() {
  if (!selectionSynthesizing) return; // 不是选区这一轮：别去动浮标
  const read = selectionRead;
  if (playbackMode !== "selection" || !read || pronounceOwner !== read.owner) return; // 别的一轮出声了
  markSelectionSynthesizing(false);
}

// 失败置位：seq 仍是当前轮、mode === "selection"、owner / 文本与快照一致时才落（别给新选区
// 打上上一段的失败）。快照本身**不清**（C1 / D7：失败只是让浮标变成重试态）。
// 调用方负责「这是不是选区这一轮」的判定（playbackMode / 失败分支都在选区上下文里）。
function markSelectionReadFailed(owner, text) {
  const read = selectionRead;
  if (!read || !read.owner) return;
  if (owner && read.owner !== owner) return;
  const phrase = String(text == null ? "" : text).trim();
  if (phrase && read.text !== phrase) return;
  read.failed = true;
  selectionSynthesizing = false;
  // 合成期失败（请求超时 / 503 / 无可用语音 / play() 被拒）：浮标本来就可见，推一遍就换成
  // 「⚠ 重试」。播放中途音频坏掉时浮标同样可见（T2 常驻），原地变重试态。
  renderSelectionBubble();
}

// 只做视觉收起（摘 is-on）；**不**碰快照 —— 收浮标后仍要能重播（v16 C1 / C2）
// v16 T5：收起时一并撤掉「合成中」标记，别让下一次浮出带着上一段的脉冲。
function hideSelectionBubble() {
  selectionSynthesizing = false;
  if (!selectionBubble) return false;
  const wasOn = selectionBubble.classList.contains("is-on");
  selectionBubble.classList.remove("is-on");
  selectionBubble.classList.remove("is-synthesizing");
  selectionBubble.setAttribute("aria-busy", "false");
  return wasOn;
}

// v16 T2（D5）：活动选区会话的判据 —— 模式是选区，且 owner / 文本都与快照对得上。
// 只读既有播放会话状态，不新增模块级 UI 状态。
function selectionReadActive() {
  const read = selectionRead;
  return Boolean(
    read &&
      playbackIsLong() &&
      playbackMode === "selection" &&
      pronounceOwner === read.owner &&
      playbackText === read.text
  );
}

// v16 T2（C2 / D5）+ T5（C6 / D7）：浮标全部态的**唯一**推导点 —— 输入只有
// 「播放会话 + 快照 + 合成中开关」三处真源。优先级（自上而下，先命中先返回）：
//   合成中 > 播放中 / 暂停中 > 失败 > 播完 > 空闲
// 顺序就是「点它会发生什么」的顺序：活动会话里点浮标 = 暂停 / 继续（playSelection 的分流
// 依据就是 active），所以文案必须跟着 active 走；合成中压过 active（owner 已置位但还没出声，
// 这时点 = 重新起播这一轮）。失败只在会话已停（非 active）时才显示 —— 那才是「点我重试」的语义。
// 这里**只改文案与状态类，绝不点亮浮标**：可见性归「新拖选 / 点击起播」与「既有出口」，
// 否则 Esc / 点空白 / 滚动收掉的浮标会在下一次渲染时自己复活（C2 可见性规则）。
function renderSelectionBubble() {
  const read = selectionRead;
  const node = selectionBubble;
  if (!read || !selectionReadLive()) {
    hideSelectionBubble(); // 快照没了 / 原句已脱离文档：浮标跟着收
    return;
  }
  if (!node) return; // 浮标还没建过（没浮出过）：无需更新，也不提前往 body 塞节点
  const active = selectionReadActive(); // 播放中 / 暂停中
  const failed = Boolean(read.failed);
  const synthesizing = selectionSynthesizing;
  const words = read.text.trim().split(/\s+/).length;
  let label = "▶ 朗读选中";
  let aria = `朗读选中的 ${words} 个词`;
  let state = "idle";
  if (synthesizing) {
    // 合成中（第 0 态）：已发起请求、尚未开声；点它把这一轮重新起播。
    // 必须压过 active：此时 owner 已置位但还没出声，文案不能说「暂停」。
    label = "◌ 合成中";
    aria = "正在合成选中的文字";
    state = "synthesizing";
  } else if (active && !pronouncePaused) {
    // 播放中：点它 = 暂停（保进度，不从头发）
    label = "⏸ 暂停";
    aria = "暂停正在朗读的选段";
    state = "playing";
  } else if (active) {
    // 暂停中：点它 = 从停处继续
    label = "▶ 继续";
    aria = "继续朗读选段";
    state = "paused";
  } else if (failed) {
    // 失败且会话已停：点它 = 用同一快照再试一轮（快照保留，C1 / D7）
    label = "⚠ 重试";
    aria = "重试朗读选中的文字";
    state = "failed";
  } else if (read.played) {
    // 播完（或被打断）但快照还在：点它 = 从头重播同一片段
    label = "↻ 重播这段";
    aria = `重播刚才选中的 ${words} 个词`;
    state = "done";
  }
  // 状态类只为样式 / 验收提供钩子；文案仍是唯一真源的投影（D5）
  node.classList.toggle("is-playing", state === "playing");
  node.classList.toggle("is-paused", state === "paused");
  node.classList.toggle("is-done", state === "done");
  node.classList.toggle("is-synthesizing", state === "synthesizing");
  node.classList.toggle("is-failed", state === "failed");
  node.setAttribute("aria-busy", state === "synthesizing" ? "true" : "false");
  if (node.textContent !== label) node.textContent = label;
  if (node.getAttribute("aria-label") !== aria) node.setAttribute("aria-label", aria);
}

// 浮标坐标：贴在 rect 的右下（贴边时翻到左上），并夹在视口内
function positionSelectionBubble(node, rect) {
  node.style.left = "0px";
  node.style.top = "0px";
  const size = node.getBoundingClientRect();
  let left = rect.right + 6;
  let top = rect.bottom + 6;
  if (left + size.width > window.innerWidth - 6) {
    left = Math.max(6, window.innerWidth - size.width - 6);
  }
  if (top + size.height > window.innerHeight - 6) top = Math.max(6, rect.top - 6 - size.height);
  node.style.left = `${Math.round(left)}px`;
  node.style.top = `${Math.round(top)}px`;
}

// 浮标贴哪个矩形：区间**末端**那一个 clientRect（拖选 / 键盘扩选都是「贴着选区结尾」）。
// 空区间取 boundingRect；拿不到矩形（owner 已脱离文档 / 区间塌了）退回原句矩形。
function selectionEndRect(range, owner) {
  if (range) {
    const rects = range.getClientRects();
    const last = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
    if (last && (last.width || last.height)) return last;
  }
  return owner ? owner.getBoundingClientRect() : null;
}

// v16 T2（C2 可见性）：**选区会话开始**时才点亮浮标 —— 这是「开始播放就收」的反转。
// 这里不做通用重渲染（renderSelectionBubble 只改内容）：被点空白 / Esc / 滚动收掉的浮标
// 不会因为播放状态变化自己复活。已可见就保持原坐标（拖选时算好的那份），不跳动；
// 此前不可见（键盘等入口起播）才按快照区间的末端兜底摆一次（C2：位置贴在区间末端），
// 由偏移重建活 Range —— 不依赖原生选区是否还在（起播的 decorateSentence 会把它塌掉）。
function revealSelectionBubble() {
  const read = selectionRead;
  if (!read || !selectionReadLive()) return;
  const node = ensureSelectionBubble();
  if (!node.classList.contains("is-on")) {
    node.classList.add("is-on"); // 先点亮再量尺寸，否则 display:none 时宽高为 0
    const range = rangeFromOffsets(read.owner, read.start, read.end);
    const rect = selectionEndRect(range, read.owner);
    if (rect) positionSelectionBubble(node, rect);
  }
  renderSelectionBubble();
}

// 拖选结束：不清选区，在选区末端浮出「▶ 朗读选中」；返回浮标是否出现。
// `useRange` 可选：兜底入口「选区只盖住一整句」时传句内 Range —— 这时**不动**原生选区
// （C3：原生选区继续服务复制），只用它建快照与定位。
function showSelectionBubble(owner, useRange = null) {
  const selection = window.getSelection();
  if (!selection || (!useRange && (selection.isCollapsed || !selection.rangeCount))) return false;
  const range = useRange || selection.getRangeAt(0);
  if (!owner.contains(range.startContainer) || !owner.contains(range.endContainer)) return false;
  const raw = String(range.toString());
  const start = selectionOffsetAt(owner, range.startContainer, range.startOffset);
  const end = selectionOffsetAt(owner, range.endContainer, range.endOffset);
  // v16 C1：新选区 = 旧快照必须让位；建不出来（空文本）就不动旧快照
  const read = createSelectionRead(owner, raw, start, end);
  if (!read) return false;
  // 兜底与松手快路径会为同一份选区各跑一次：内容完全一致就别换快照（免得无谓地重置 at）
  const same =
    Boolean(selectionRead) &&
    selectionRead.owner === owner &&
    selectionRead.start === start &&
    selectionRead.end === end &&
    selectionRead.text === read.text;
  if (!same) {
    clearSelectionHighlight(); // v16 C3：新拖选 = 旧快照让位，旧高亮一起清（新的一段播放时才涂）
    selectionRead = read;
    selectionSynthesizing = false; // v16 T5：新选区不属于上一轮合成
  }
  crossSelectionToastShown = false; // C5：成功出浮标 = 新一轮操作，提示闩锁复位
  const node = ensureSelectionBubble();
  const wasOn = node.classList.contains("is-on"); // 已浮出时（键盘改选区）按内容决定要不要再播报
  // v16 C4 #5：浮标已开着时键盘改选区 → 文案回到「▶ 朗读选中」（新快照还没播过）
  node.textContent = "▶ 朗读选中";
  node.classList.add("is-on");
  positionSelectionBubble(node, selectionEndRect(range, owner));
  // v16 C11：浮出时用 live region 播报一次「已选中 N 个词，可朗读」（textContent 更新即播报）。
  // 松手快路径与 selectionchange 兜底会为同一份选区各调一次 → 「已经浮着 + 快照没换」时不再写，
  // 避免同一段选区连播两遍。
  if (!same || !wasOn) {
    announceSelectionBubble(`已选中 ${read.text.trim().split(/\s+/).length} 个词，可朗读`);
  }
  renderSelectionBubble(); // 新快照 = 回到「▶ 朗读选中」（played 为 false）
  return true;
}

/* v16 T3（C3 / D3 / D4）：自绘「刚才读的是哪段」。
 * 权威坐标是原句 textContent 的字符偏移；需要视觉标记时**每次**由偏移重建一个活 Range
 * （不缓存 cloneRange()：装饰替换边界文本节点后它会直接变空，研究文档探针 P5）。
 * 画的是 CSS Custom Highlight 覆盖层，不碰 DOM、不动 textContent、不新增节点，
 * 也不依赖原生选区 —— 原生选区继续只管复制（C3 降级口径见 applySelectionHighlight）。 */
const SELECTION_HIGHLIGHT_NAME = "selection-read"; // 固定单名：清除 / 断言都只认它

function selectionHighlightSupported() {
  return typeof CSS !== "undefined" && "highlights" in CSS && typeof Highlight !== "undefined";
}

// 字符偏移 → 活 Range：TreeWalker 按文本节点长度累加（未装饰的原句与已装饰的 `.rd-w`
// 是同构的 —— 装饰只把文本节点换成等长 span，累加结果逐字节不变）。
// 越界 / owner 已脱离文档 → null（调用方据此「不画」，不抛错）。
function rangeFromOffsets(owner, start, end) {
  if (!owner || owner.isConnected !== true) return null;
  const from = Number(start);
  const to = Number(end);
  if (!Number.isFinite(from) || !Number.isFinite(to) || from < 0 || to <= from) return null;
  const walker = document.createTreeWalker(owner, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let offset = 0;
  let bounded = false;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const next = offset + String(node.nodeValue || "").length;
    if (!bounded && from <= next) {
      range.setStart(node, Math.max(0, from - offset));
      bounded = true;
    }
    if (bounded && to <= next) {
      range.setEnd(node, Math.max(0, to - offset));
      return range;
    }
    offset = next;
  }
  return null; // to 超过了 textContent 长度
}

// 涂高亮：区间文本与快照 slice 逐字节一致的依据 —— offsets 本来就来自同一 textContent 的
// 偏移，rangeFromOffsets 只是按同样的文本节点长度累加回去，所以无需运行时断言。
// 不支持 Custom Highlight API 时静默跳过：不报错、不阻塞播放、不清原生选区（更不用 ::selection 硬凑）。
function applySelectionHighlight(read) {
  if (!selectionHighlightSupported()) return;
  const range = read ? rangeFromOffsets(read.owner, read.start, read.end) : null;
  if (!range) return;
  CSS.highlights.set(SELECTION_HIGHLIGHT_NAME, new Highlight(range));
}

function clearSelectionHighlight() {
  if (!selectionHighlightSupported()) return;
  CSS.highlights.delete(SELECTION_HIGHLIGHT_NAME);
}

// 高亮是否已经涂上（「装饰后要不要重画」的判断用；不支持该 API 时恒 false）
function selectionHighlightOn() {
  return selectionHighlightSupported() && CSS.highlights.has(SELECTION_HIGHLIGHT_NAME);
}

// v16 T1（D2）：用**快照**重新发起一轮选区播放 —— 不解析原生选区，也不看播放会话状态。
// 播放会话的建立仍走既有原语（pronounce / pronounceLong，mode:"selection" + origin）。
// 返回是否真的起了播放：快照不在 / 原句节点已脱离文档 → false（顺手让快照失效）。
// v16 T5（C6 / D7）：这是「显式点一下」的唯一入口（浮标 / 键盘 / 右键菜单都汇到这里），所以
//   1) 起播前立刻进入「合成中」态 —— 从点击到出声之间不许有静默期；
//   2) 顺手清掉 60s 兜底冷却 —— 冷却防的是**自动**重试风暴，用户手点「⚠ 重试」是明确意图；
//      不清的话 S11（修好后重试能播）会被这 60s 挡死。
function playSelectionSnapshot() {
  const read = selectionRead;
  if (!read || !read.chunks || !read.chunks.length) return false;
  if (!selectionReadLive()) {
    invalidateSelectionRead(); // 复习卡揭示 / 列表重渲染后 DOM 已换：别播幽灵节点
    return false;
  }
  const retry = read.failed; // 上一轮失败过 = 这次点击是「⚠ 重试」
  read.failed = false; // 新一轮：先回到「合成中」，别把上一轮的失败态挂在按钮上
  // 冷却防的是**自动**重试风暴（预取 / 前瞻 / 60s 内的静默兜底），用户手点「⚠ 重试」是明确
  // 意图：不清的话 S11（后端修好后重试能播）会被这 60s 挡死。只在重试时清，普通起播不动冷却。
  if (retry) pronounceFallbackUntil = 0;
  if (read.chunks.length > 1) {
    pronounceLong(read.chunks.slice(), read.owner, {
      mode: "selection",
      text: read.text,
      origin: read.start,
    });
  } else {
    pronounce(read.chunks[0], read.owner, { mode: "selection", origin: read.start });
  }
  // v16 T5（C6）：起播原语内部会先 pausePronounceAudio()（= playbackSessionReset()，那里
  // 会撤掉合成中标记），所以「合成中」必须放在调用**之后**置位：仍在本轮点击的同步段里，
  // 视觉上没有静默期。放在 revealSelectionBubble() 之前，浮标第一帧就是「◌ 合成中」。
  markSelectionSynthesizing(true);
  read.played = true; // v16 T2（C2）：这一快照已经起播过 —— 非活动态才给「↻ 重播这段」
  revealSelectionBubble(); // v16 T2（C2）：播放选区开始 → 浮标保持/变为可见（反转 v15）
  // v16 C3：播放开始 = 用户感知的时刻，立刻涂上「刚才读的是这段」（等合成期间也看得见）
  applySelectionHighlight(read);
  return true;
}

// 浮标点击（v16 T2 / C2）：活动选区会话中 = 暂停 / 继续（复用既有原语，保进度）；
// 否则 = 用快照起一轮。播放中不再收浮标（v15 的「点了就收」在 T2 反转）。
function playSelection() {
  cancelSelectionFallback(); // v16 T4：已经点下去播了，排队的兜底不许再把浮标顶回来
  // 合成中不算「活动会话」：这时的 owner 已置位，但音频还没出声，走 pausePlayback() 只会
  // 记一个用户看不见的暂停标记（浮标按优先级仍显示「◌ 合成中」）—— 点下去等于没反应。
  // 合成中再点 = 重新起播这一轮（T5 的显式重试口径）。
  if (selectionReadActive() && !selectionSynthesizing) {
    if (pronouncePaused) resumePlayback();
    else pausePlayback();
    return; // 两个原语都会 renderPlaybar() → renderSelectionBubble()，浮标文案自动跟手
  }
  // v16 T5（C6）：不再「点下就收浮标」—— 合成期浮标要留着当第 0 态；真的开声后由
  // selectionSpeechStarted() 退出合成中（T2 让浮标常驻，不收），失败则变成「⚠ 重试」。
  const started = playSelectionSnapshot(); // 快照不因播放而销毁（C1）
  if (!started) {
    markSelectionSynthesizing(false);
    hideSelectionBubble();
  }
}

/* --- v16 T4（票 #135，spec C4 / C5 / D6）：冗余入口的共用解析 -------------------
 * 键盘 / 双击 / 右键 / selectionchange 兜底四个入口都先问 resolveSelectionTarget()，
 * 再统一汇到 showSelectionBubble()（唯一的「解析选区 → 建快照」处），不各写一套。
 */
function elementOf(node) {
  if (!node) return null;
  return node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
}

// 选区落在输入框 / 可编辑区里：那不是「朗读选中」的对象（搜索框 Ctrl+A 也不该弹提示）
function selectionInField(node) {
  if (!node) return false;
  if (node.isContentEditable) return true;
  return Boolean(node.closest && node.closest("input, textarea, select, [contenteditable]"));
}

// 选区归属的唯一判定：
//   { owner }                两端落在同一个 `.row-sentence` 内 → 可以建快照
//   { owner, clamp: true }  两端都在原句外，但选区只盖住这一句（容器内 Ctrl+A）→ 按整句算
//   { crossed: true }       选区碰到原句、但跨了两句 / 跨容器 → C5 提示
//   null                    空 / 塌陷 / 纯空白 / 与原句无关 → 不出浮标、不提示
function resolveSelectionTarget() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  if (!String(selection.toString()).trim()) return null;
  const range = selection.getRangeAt(0);
  const startEl = elementOf(range.startContainer);
  const endEl = elementOf(range.endContainer);
  if (selectionInField(startEl) || selectionInField(endEl)) return null;
  const startOwner = startEl && startEl.closest ? startEl.closest(".row-sentence") : null;
  const endOwner = endEl && endEl.closest ? endEl.closest(".row-sentence") : null;
  if (startOwner && startOwner === endOwner) return { owner: startOwner };
  if (startOwner || endOwner) return { crossed: true };
  // 两端都不在原句里：只有「选区盖住的正是唯一一句」才认（Ctrl+A 选到整张卡不算跨句）
  const common = elementOf(range.commonAncestorContainer);
  const inside =
    common && common.querySelectorAll
      ? Array.from(common.querySelectorAll(".row-sentence")).filter((node) => range.intersectsNode(node))
      : [];
  if (!inside.length) return null; // 与任何原句无关的选区（页面别处的文字）：不接管
  if (inside.length === 1) return { owner: inside[0], clamp: true };
  return { crossed: true };
}

// 四个入口的统一落点：按归属结果出浮标 / 提示。
// `clamp`（选区只盖住这一整句，两端却在句外）→ 用句内 Range 建快照，但**不改**原生选区。
function showSelectionForHit(hit) {
  if (!hit) return false;
  if (hit.crossed) {
    reportCrossSelection();
    return false;
  }
  const owner = hit.owner;
  if (!owner || !owner.isConnected) return false;
  if (hit.clamp) {
    const range = document.createRange();
    range.selectNodeContents(owner);
    return showSelectionBubble(owner, range);
  }
  return showSelectionBubble(owner);
}

// C5：跨句提示的闩锁 —— 同一段操作只提示一次；成功出浮标或下一轮按下才复位。
// 顺手收掉旧浮标：它的位置与文本都已经对不上新选区了（快照保留，仍可 Shift+Enter 重播）。
function reportCrossSelection() {
  hideSelectionBubble();
  if (crossSelectionToastShown) return;
  crossSelectionToastShown = true;
  toast("只能朗读同一句，请缩小选区");
}

// D6：我们自己写选区（点空白后恢复）时记下这份选区，兜底不得为它出浮标（否则闪一下）。
// 判据用「选区是否仍与程序化写入的那份逐字相同」而不是计时器：同一份选区引发的 1~N 条
// selectionchange 全部跳过；用户一改选区就不再相同，兜底立刻恢复接管。
function markProgrammaticSelection(ranges) {
  programmaticRanges = ranges || null;
  cancelSelectionFallback();
}

function selectionIsProgrammatic() {
  if (!programmaticRanges) return false;
  if (!selectionMatchesRanges(programmaticRanges)) {
    programmaticRanges = null; // 用户已经改了选区：不再算程序化
    return false;
  }
  return true;
}

function selectionMatchesRanges(ranges) {
  const selection = window.getSelection();
  if (!selection || selection.rangeCount !== ranges.length) return false;
  for (let index = 0; index < ranges.length; index += 1) {
    const live = selection.getRangeAt(index);
    const saved = ranges[index];
    if (
      live.startContainer !== saved.startContainer ||
      live.startOffset !== saved.startOffset ||
      live.endContainer !== saved.endContainer ||
      live.endOffset !== saved.endOffset
    ) {
      return false;
    }
  }
  return true;
}

// C4 #4：selectionchange 兜底 —— 去抖后按「归属」决定出浮标 / 提示 / 什么都不做
function scheduleSelectionFallback() {
  cancelSelectionFallback();
  selectionFallbackTimer = window.setTimeout(() => {
    selectionFallbackTimer = 0;
    runSelectionFallback();
  }, SELECTION_CHANGE_DEBOUNCE_MS);
}

function cancelSelectionFallback() {
  if (selectionFallbackTimer) {
    window.clearTimeout(selectionFallbackTimer);
    selectionFallbackTimer = 0;
  }
}

function runSelectionFallback() {
  // 指针按着（= 正在拖选 / 触屏长按选字）就不抢：快路径松手时负责，长按松手后补跑。
  // 用「指针是否按着」而不是「探针是否在拖」：pointerup 丢失时探针会卡在 dragging
  // （复位归 T6 / C9），只看探针会把兜底永久堵死。pointerHeld 的每条出口都有复位
  // （pointerup / pointercancel / pointermove 的 buttons=0 / 窗口失焦）。
  if (pointerHeld) {
    fallbackSuppressed = true; // 松手后补跑（见 pointerup / pointercancel / blur）
    return;
  }
  if (selectionIsProgrammatic()) return; // 程序化恢复的选区：不抢（无浮标闪烁）
  const hit = resolveSelectionTarget();
  if (!hit) return; // 空 / 纯空白：不出浮标不提示
  if (!hit.crossed) cancelSentenceClickPlay(); // 用户实际是在选字：那次「点原句播整段」已过期
  showSelectionForHit(hit);
}

// C4 #1：Shift+Enter 的「在原句里」判定 —— 事件目标或活动元素落在某个 `.row-sentence` 内
function sentenceOwnerFrom(event) {
  const target = elementOf(event.target);
  const fromTarget = target && target.closest ? target.closest(".row-sentence") : null;
  if (fromTarget) return fromTarget;
  const active = elementOf(document.activeElement);
  return active && active.closest ? active.closest(".row-sentence") : null;
}

// C4 #1：Shift+Enter 键盘入口 —— 有未消费选区 → 建快照并播；否则重播活快照；
// 两者都没有 → 返回 false（调用方不 preventDefault、不 stopPropagation）。
function playSelectionFromKeyboard(owner) {
  const hit = resolveSelectionTarget();
  // 「未消费的选区」= 两端就在这句里（clamp 只把选区扩到整句，同样算这句）
  if (hit && hit.owner === owner && !hit.crossed) {
    if (!showSelectionForHit(hit)) return false;
    playSelection(); // 起播这一快照（浮标三态由 T2/T5 推导，不再「点了就收」）
    return true;
  }
  if (selectionReadLive()) {
    playSelection();
    return true;
  }
  if (hit && hit.crossed) {
    reportCrossSelection(); // C5：跨句选区读不了，也没有旧快照可退 —— 给一句提示
    return true;
  }
  return false;
}

/* --- v16 T4（C4 #2）：单击原句延迟起播，修「双击第一击先播整句」（§5.9 D5） ------ */

function scheduleSentenceClickPlay(owner, text) {
  cancelSentenceClickPlay();
  sentenceClickAct = { owner, text };
  sentenceClickTimer = window.setTimeout(() => {
    const act = sentenceClickAct;
    sentenceClickTimer = 0;
    sentenceClickAct = null;
    if (!act || !act.owner || !act.owner.isConnected) return;
    // 到点前用户已经选字（慢双击的下一击超过了阈值）：那是选词动作，不是「点原句播整段」
    if (hasSelection()) return;
    togglePlayback(act.owner, act.text);
  }, SENTENCE_CLICK_DELAY_MS);
}

function cancelSentenceClickPlay() {
  if (sentenceClickTimer) {
    window.clearTimeout(sentenceClickTimer);
    sentenceClickTimer = 0;
  }
  sentenceClickAct = null;
}

/* --- v16 T4（C4 #3）：原句右键菜单（body 单例，纯键盘可用） -------------------- */

function ensureSentenceMenu() {
  if (sentenceMenu) return sentenceMenu;
  const menu = el("div", "sentence-menu");
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "原句朗读");
  // 按下别塌掉原生选区（「朗读选中」要用它建快照）；焦点由 openSentenceMenu 接管
  menu.addEventListener("mousedown", (event) => event.preventDefault());
  menu.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeSentenceMenu();
      return;
    }
    // role="menu" 的键盘约定：↑/↓ 在项之间移动（Tab 也照常能走）
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    event.stopPropagation();
    const items = Array.from(menu.querySelectorAll(".sentence-menu-item:not(:disabled)"));
    if (!items.length) return;
    const at = items.indexOf(document.activeElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = at < 0 ? 0 : (at + step + items.length) % items.length;
    items[next].focus({ preventScroll: true });
  });
  // C11：Tab 把焦点带出菜单 = 菜单该收了（不再抢回焦点，让 Tab 链路继续走）
  menu.addEventListener("focusout", () => {
    window.setTimeout(() => {
      if (sentenceMenu && sentenceMenu.classList.contains("is-on") && !sentenceMenu.contains(document.activeElement)) {
        closeSentenceMenu(false);
      }
    }, 0);
  });
  const readSelection = button("sentence-menu-item", "朗读选中", () => {
    closeSentenceMenu();
    menuPlaySelection();
  });
  readSelection.dataset.act = "selection";
  readSelection.setAttribute("role", "menuitem");
  const readWhole = button("sentence-menu-item", "朗读整段", () => {
    const owner = sentenceMenuOwner;
    const text = sentenceMenuText;
    closeSentenceMenu();
    if (!owner || !owner.isConnected) return;
    startSentencePlayback(owner, text);
  });
  readWhole.dataset.act = "sentence";
  readWhole.setAttribute("role", "menuitem");
  menu.append(readSelection, readWhole);
  sentenceMenu = menu;
  document.body.append(menu);
  return menu;
}

// 输入框 / 抽屉文本区内不接管右键（原句在抽屉里也走同一套判定）
function sentenceMenuAllowed(target) {
  if (isTyping(target)) return false;
  return !(target instanceof Element && target.closest(".drawer"));
}

function openSentenceMenu(event, owner, text) {
  if (!owner || !owner.isConnected) return;
  if (!sentenceMenuAllowed(event.target)) return; // 不 preventDefault：让浏览器菜单照常
  event.preventDefault(); // 菜单出现时浏览器菜单不弹
  event.stopPropagation();
  cancelSentenceClickPlay(); // 右键不是单击：那次延迟起播作废
  fallbackSuppressed = false; // 菜单是显式动作：右键带出的选区变化不必再补浮标
  closePopover();
  const menu = ensureSentenceMenu();
  sentenceMenuOwner = owner;
  sentenceMenuText = String(text == null ? "" : text);
  const canSelection = hasSelection() || selectionReadLive();
  for (const item of menu.querySelectorAll(".sentence-menu-item")) {
    item.disabled = item.dataset.act === "selection" ? !canSelection : !sentenceMenuText.trim();
  }
  menu.classList.add("is-on");
  menu.style.left = "0px";
  menu.style.top = "0px";
  const size = menu.getBoundingClientRect();
  const box = owner.getBoundingClientRect();
  const x = event.clientX || box.left;
  const y = event.clientY || box.bottom;
  let left = x;
  let top = y;
  if (left + size.width > window.innerWidth - 6) {
    left = Math.max(6, window.innerWidth - size.width - 6);
  }
  if (top + size.height > window.innerHeight - 6) top = Math.max(6, top - size.height - 6);
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
  const first = menu.querySelector(".sentence-menu-item:not(:disabled)");
  if (first) first.focus({ preventScroll: true }); // C11：Tab / ↑↓ / Enter 立刻可用
}

function closeSentenceMenu(refocus = true) {
  if (!sentenceMenu || !sentenceMenu.classList.contains("is-on")) return false;
  const menu = sentenceMenu;
  const owner = sentenceMenuOwner;
  menu.classList.remove("is-on");
  sentenceMenuOwner = null;
  sentenceMenuText = "";
  // C11：键盘开着菜单 → 收掉后焦点回原句，Tab 链路不落到 body
  if (
    refocus &&
    menu.contains(document.activeElement) &&
    owner &&
    owner.isConnected
  ) {
    owner.focus({ preventScroll: true });
  }
  return true;
}

// 「朗读选中」：有选区就先刷新快照，没有就重播活快照 —— 两种都走快照入口播
function menuPlaySelection() {
  const hit = resolveSelectionTarget();
  if (hit && hit.crossed) {
    reportCrossSelection(); // C5：跨句读不了，也**不**拿旧快照顶替（会读错文本）
    return;
  }
  if (hit) showSelectionForHit(hit);
  if (!selectionReadLive()) return;
  playSelection();
}

function saveSelectionRanges() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  const ranges = [];
  for (let index = 0; index < selection.rangeCount; index += 1) {
    ranges.push(selection.getRangeAt(index).cloneRange());
  }
  return ranges;
}

function restoreSelectionRanges(ranges) {
  const selection = window.getSelection();
  if (!selection || !ranges || !ranges.length) return;
  // v16 T4（D6）：这是程序化改动 —— 兜底入口不得为它出浮标（否则闪一下）
  markProgrammaticSelection(ranges);
  selection.removeAllRanges();
  for (const range of ranges) selection.addRange(range);
}

// 拖选起点在 sentenceNode 的 pointerdown 里记下；document 级只做一对指针监听。
// v16（票 #137，C9）：只认发起这次拖选的那根指针（pointerId）—— 多指 / 先抬起的手指
// 不会清掉别人的探针，也不会把一次点击误判成拖选。
document.addEventListener(
  "pointerdown",
  (event) => {
    pointerHeld = true;
    // 这一轮按下是不是落在我们自己的浮标 / 菜单上（松手后别把刚收掉的浮标又补回来）
    pointerOnOwnUI = Boolean(
      (selectionBubble && selectionBubble.contains(event.target)) ||
        (sentenceMenu && sentenceMenu.contains(event.target))
    );
    // v16 T4（C5）：新一轮按下 = 新一轮操作，跨句提示的闩锁复位
    crossSelectionToastShown = false;
    // v16 T4（D6）：程序化恢复的「豁免」只对这一轮有效，用户自己动手就归还给兜底
    programmaticRanges = null;
    // v16 T4（C4 #2）：新的按下 = 上一次单击的意图作废（拖选 / 双击都算新动作）
    cancelSentenceClickPlay();
  },
  true
);

document.addEventListener(
  "pointerup",
  (event) => {
    // v16（票 #137，C9）：抬起的是另一根手指时不收 —— 探针与兜底闩锁都还归仍按着的那根
    if (dragProbe && event.pointerId !== dragProbe.pointerId) return;
    const wasHeld = pointerHeld;
    const onOwnUI = pointerOnOwnUI;
    pointerHeld = false;
    pointerOnOwnUI = false;
    if (!wasHeld) return;
    // 触屏长按 / 拖出窗口：选区在按着的时候就定了，松手不会再有 selectionchange。
    // 这类「按着期间被压下去的兜底」要在松手后补跑一次 —— 但**不**包括：
    // 鼠标拖选（下面的快路径负责）、点浮标 / 菜单自己（那是收起的动作）。
    const probe = dragProbe; // 注意：下面的 pointerup 监听器会先清掉它，这里读到的是仍在拖的状态
    if (fallbackSuppressed && !onOwnUI && !(probe && probe.dragging)) {
      fallbackSuppressed = false;
      scheduleSelectionFallback();
    } else {
      fallbackSuppressed = false;
    }
  },
  true
);

// 指针被系统抢走 / 窗口失焦时，pointerup 永远不会来 —— 别让兜底被永久挡住。
// v16（票 #137，C9）：探针的三个复位点（pointercancel / blur / visibilitychange-hidden），
// 否则「正在拖选」永久为真，悬停显义永久冻结。pointerHeld（T4 / C5）与探针在这一套里一起复位。
// `lostpointercapture` 这里不监听：我们从未 setPointerCapture（加了会改 click 路由），
// 它不会被触发；这三条已覆盖「拖选中途被打断」的全部现实路径。
document.addEventListener(
  "pointercancel",
  (event) => {
    if (dragProbe && event.pointerId !== dragProbe.pointerId) return; // 不是发起拖选的那根
    dragProbe = null;
    pointerHeld = false;
    fallbackSuppressed = false;
    scheduleSelectionFallback();
  },
  true
);
window.addEventListener("blur", () => {
  dragProbe = null;
  pointerHeld = false;
  // 拖出窗口后失焦：选区可能已经定下来了，补跑一次被按下的兜底
  if (fallbackSuppressed) {
    fallbackSuppressed = false;
    scheduleSelectionFallback();
  }
});
document.addEventListener("pointermove", (event) => {
  // v16 T4：pointerup 丢了（拖出窗口）但指针其实已经松开 —— 用 buttons 判据补上复位，
  // 紧接着的选区变化仍能出浮标（C5 的最后一段）
  if (pointerHeld && event.buttons === 0) {
    pointerHeld = false;
    if (fallbackSuppressed) {
      fallbackSuppressed = false;
      scheduleSelectionFallback();
    }
  }
  if (!dragProbe || dragProbe.dragging) return;
  if (event.pointerId !== dragProbe.pointerId) return;
  if (Math.hypot(event.clientX - dragProbe.x, event.clientY - dragProbe.y) <= DRAG_SELECT_PX) {
    return;
  }
  dragProbe.dragging = true;
  hideSelectionBubble();
  closePopover(); // 拖选期间关掉并冻结悬停显义（mouseup 后恢复）
});

document.addEventListener("pointerup", (event) => {
  if (dragProbe && event.pointerId !== dragProbe.pointerId) return; // 不是发起拖选的那根
  const probe = dragProbe;
  dragProbe = null;
  if (!probe || !probe.dragging) return;
  // v16 T4：快路径与兜底共用同一处归属判定 —— 跨句 / 跨容器走 C5 提示，不再静默失败；
  // 归属为空（拖出去松手、选区塌陷）时退回起落点那句，保持 v15 的宽松口径
  const hit = resolveSelectionTarget();
  if (hit && hit.crossed) {
    reportCrossSelection();
    return;
  }
  if (hit) showSelectionForHit(hit);
  else showSelectionBubble(probe.owner);
});

// 浮标随选区消失一起收（Esc / 点空白 / capture mousedown 另三处），其余变化交给 C4 的兜底入口。
// v16 T5（C6）：合成中 / 失败时**不收** —— 起播的 decorateSentence 会替换文本节点、塌掉原生
// 选区（selectionchange 随之而来），若照旧收掉，「⚠ 重试」就永远看不见了。
// （探针 / pointerHeld 的复位见上面的 pointercancel + blur，以及页面隐藏处的 visibilitychange。）
document.addEventListener("selectionchange", () => {
  if (selectionBubbleOn() && !hasSelection()) {
    // v16 T2（C2）/ T5（C6）：首次起播时 decorateSentence 会替换文本节点、把原生选区塌掉 ——
    // 那不是用户收浮标。本轮选区会话仍在进行（T2），或正处于合成中 / 失败态（T5：失败态收起后
    // 「⚠ 重试」就再也看不见了）时不收；其余（选区真的被用户塌掉）沿用 v15 的「随手收」。
    if (!selectionReadActive() && !selectionSynthesizing && !(selectionRead && selectionRead.failed)) {
      hideSelectionBubble();
    }
  }
  scheduleSelectionFallback();
});

// 点空白时浏览器默认会塌掉选区 —— 点之前存一份，click 后（非交互目标）放回来，复制仍可用
// v16（票 #137，C9 / D8）：浮标收口也在这里 —— capture 阶段的 mousedown 一定能收到，
// 不受目标元素 `stopPropagation()` 影响（点原句 / 点高亮词就是这样绕过 document click 的）。
// 只摘 is-on + 清自绘高亮，快照保留（之后仍能重播）。
document.addEventListener(
  "mousedown",
  (event) => {
    // v16 T4（C4 #3）：点菜单外关菜单 —— 用 capture 阶段，句子 / 高亮词的
    // stopPropagation 挡不住这里；点在菜单自己身上不算。不抢焦点（用户正要点的目标
    // 会在这次按下的默认动作里拿到焦点）
    if (sentenceMenu && sentenceMenu.classList.contains("is-on") && !sentenceMenu.contains(event.target)) {
      closeSentenceMenu(false);
    }
    if (!selectionBubbleOn()) {
      // v16（票 #137，D4）：浮标没开时收到 mousedown = 上一次存下的选区已经过期
      // （那一次 click 被 stopPropagation 吞掉 / 走了 Esc），别让它在下一次普通点击时复活。
      pendingSelectionRanges = null;
      return;
    }
    if (selectionBubble.contains(event.target)) return;
    pendingSelectionRanges = saveSelectionRanges();
    hideSelectionBubble();
    clearSelectionHighlight(); // C3：点空白清高亮
  },
  true
);

function restoreSelectionAfterBlankClick(event) {
  const ranges = pendingSelectionRanges;
  pendingSelectionRanges = null;
  if (!ranges || !ranges.length) return;
  const target = event.target;
  const interactive =
    target instanceof Element &&
    target.closest("button, a, input, textarea, select, .row-sentence, .ctx-mark, .review-target");
  if (interactive) return; // 点了句子 / 控件：那是新动作，不把旧选区塞回来
  restoreSelectionRanges(ranges);
}

/* --- v12：朗读进度跟随（粉笔划线） ------------------------------------------ */

// 进度是**估算**的（一期不改后端契约）：读 `<audio>` 的 currentTime / duration，
// 扣掉实测的首尾静音，再把「已过时间比例」按词权重折成字符位置。
// 实测（Edge 24kHz/48kbps MP3，4–200 字符，Chromium 解码 + 10ms 峰值窗口）：开头静音
// 0.10–0.14s、结尾静音 0.33–0.35s，与文本长度基本无关；所以静音不算进进度，
// 也不会在尾静音里提前「画完」。完整数据见 docs/specs/v12-reading-progress.md §5.3。
const SPEAK_LEAD_S = 0.12;
const SPEAK_TAIL_S = 0.34;
const SPEAK_MIN_USABLE_S = 0.2;
// 词权重只决定「相对进度」：字母越多读得越久，词间有停顿，标点再拖一点。
// 只影响时间→字符的折算，不需要和真实总时长对齐，所以不追求绝对准确。
const SPEAK_WORD_BASE = 1.2;
const SPEAK_PAUSE_SOFT = 0.9; // , ; :
const SPEAK_PAUSE_HARD = 2.2; // . ! ? …
const SPEAK_CJK_CHAR = 2.2;
// 浏览器兜底（speechSynthesis）没有音频时长：按权重估一个总时长，让划线也能走完。
const SPEAK_CLOCK_S_PER_WEIGHT = 0.075;

let speakProgress = null; // 同一时刻只有一路音频（§9），所以进度只要一个

// 原句里每个「词 + 其后空白」套一层 span：带上偏移（data-cs / data-ce，相对原句上下文），
// 不改字、不改换行、不拦点击 —— 文本选择、复制、屏幕阅读器、`.ctx-mark` 语义都与 v11 一致。
// 第一次播放时才装饰（列表里几百段原句不播放就一个节点都不多）。
function decorateSentence(sentence) {
  if (!sentence) return null;
  if (sentence.__rdSpans) return sentence.__rdSpans;
  const spans = [];
  const walk = (parent, base) => {
    let offset = base;
    for (const node of Array.from(parent.childNodes)) {
      if (node.nodeType === 3) {
        const text = String(node.nodeValue || "");
        const parts = text.match(/\S+\s*|\s+/g) || [];
        let local = 0;
        if (parts.length) {
          const frag = document.createDocumentFragment();
          for (const part of parts) {
            const span = document.createElement("span");
            span.className = "rd-w";
            span.dataset.cs = String(offset + local);
            span.dataset.ce = String(offset + local + part.length);
            span.textContent = part; // 纯空白也包：否则 `.ctx-mark` 后面那个空格会断笔
            frag.append(span);
            spans.push(span);
            local += part.length;
          }
          parent.replaceChild(frag, node);
        }
        offset += text.length;
      } else if (node.nodeType === 1) {
        const inner = String(node.textContent || "");
        if (node.classList && node.classList.contains("ctx-mark")) {
          // 高亮词按钮里的字也逐词包起来；按钮本身（点击 / 悬停 / aria）一点不动
          node.dataset.rdOrigin = String(offset);
          walk(node, offset);
        }
        offset += inner.length;
      }
    }
  };
  walk(sentence, 0);
  sentence.__rdSpans = spans;
  return spans;
}

// 词权重表：tokens 带 [start,end) 与权重；marks = 累计权重；total = 总权重
function speakWeightTable(text) {
  const source = String(text == null ? "" : text);
  const tokens = [];
  const pattern = /\S+\s*|\s+/g;
  let match;
  while ((match = pattern.exec(source)) !== null) {
    const piece = match[0];
    if (/^\s+$/.test(piece)) continue; // 纯空白不单独占时间（停顿算在前一个词后面）
    let weight = SPEAK_WORD_BASE;
    for (const char of piece.trim()) {
      if (/[A-Za-z0-9']/.test(char)) weight += 1;
      else if (/[\u3400-\u9fff]/.test(char)) weight += SPEAK_CJK_CHAR;
      else if (/[.!?…。！？]/.test(char)) weight += SPEAK_PAUSE_HARD;
      else if (/[,;:，、；：—–]/.test(char)) weight += SPEAK_PAUSE_SOFT;
    }
    tokens.push({ start: match.index, end: match.index + piece.length, weight });
  }
  const marks = [];
  let total = 0;
  for (const token of tokens) {
    total += token.weight;
    marks.push(total);
  }
  return { tokens, marks, total };
}

// 时间比例 u∈[0,1] → 本片内的字符位置（小数：词内按字符插值，划线才是连续的）
function speakCharAt(table, u) {
  const { tokens, marks, total } = table;
  if (!tokens.length || total <= 0) return 0;
  const target = Math.max(0, Math.min(1, u)) * total;
  let low = 0;
  let high = marks.length - 1;
  while (low < high) {
    const mid = (low + high) >> 1;
    if (marks[mid] < target) low = mid + 1;
    else high = mid;
  }
  const token = tokens[low];
  const before = low === 0 ? 0 : marks[low - 1];
  const frac = token.weight > 0 ? (target - before) / token.weight : 1;
  return token.start + Math.max(0, Math.min(1, frac)) * (token.end - token.start);
}

// 只写一个 CSS 变量 + 两个状态类：每帧最多动一个词，不重排、不重绘整段
function speakSetSpan(span, ratio) {
  const pct = Math.round(Math.max(0, Math.min(1, ratio)) * 1000) / 10;
  if (span.__rdP === pct) return; // 值没变就别碰样式（尾静音 / 暂停时每帧都写一遍没意义）
  span.__rdP = pct;
  span.style.setProperty("--rd-p", `${pct}%`);
  span.classList.toggle("is-spoken", pct >= 100);
  span.classList.toggle("is-current", pct > 0 && pct < 100);
}

// 把 charPos 之前的词一次性划完（换片 / 跳过失败片 / 收尾都走这里）
function speakFillThrough(progress, charPos) {
  const spans = progress.spans;
  while (progress.cursor < spans.length) {
    const span = spans[progress.cursor];
    if (Number(span.dataset.ce) > charPos) break;
    speakSetSpan(span, 1);
    progress.cursor += 1;
  }
}

function speakPaint(progress, charPos) {
  speakFillThrough(progress, charPos);
  const span = progress.spans[progress.cursor];
  if (!span) return;
  const cs = Number(span.dataset.cs);
  const ce = Number(span.dataset.ce);
  if (!(charPos > cs) || !(ce > cs)) return;
  speakSetSpan(span, (charPos - cs) / (ce - cs));
}

function speakProgressFrame() {
  const progress = speakProgress;
  if (!progress) return;
  progress.raf = 0;
  let ratio;
  if (progress.mode === "clock") {
    const seconds = progress.duration > 0 ? progress.duration : 1;
    ratio = (performance.now() - progress.startedAt) / 1000 / seconds;
  } else {
    const audio = pronouncePlayer();
    const duration = Number(audio.duration) || 0;
    if (audio.ended) {
      ratio = 1;
    } else if (duration > 0) {
      const usable = Math.max(SPEAK_MIN_USABLE_S, duration - SPEAK_LEAD_S - SPEAK_TAIL_S);
      ratio = (Number(audio.currentTime) - SPEAK_LEAD_S) / usable;
    } else {
      ratio = 0;
    }
  }
  ratio = Math.max(progress.u, Math.min(1, Math.max(0, ratio))); // 只前进，不回退
  progress.u = ratio;
  if (progress.table) speakPaint(progress, progress.origin + speakCharAt(progress.table, ratio));
  if (progress.mode === "clock" && ratio >= 1) {
    speakFillThrough(progress, Number.POSITIVE_INFINITY);
    // 兜底朗读没有结束事件：估的时长走完就收起来，别让句子一直挂着一条假进度
    const finished = progress;
    window.setTimeout(() => {
      if (speakProgress === finished) speakProgressClear();
    }, 900);
    return;
  }
  // 这一片已经划完（含尾静音）：停掉循环，等下一片的 speakProgressChunk 再起，
  // 换片间隙与尾静音里不空转。
  if (progress.mode === "audio" && ratio >= 1) return;
  progress.raf = window.requestAnimationFrame(speakProgressFrame);
}

function speakProgressClear() {
  const progress = speakProgress;
  if (!progress) return;
  speakProgress = null;
  if (progress.raf) window.cancelAnimationFrame(progress.raf);
  for (const span of progress.spans) {
    span.style.removeProperty("--rd-p"); // 回到 CSS 的默认 0%，不做内联残留
    span.classList.remove("is-spoken", "is-current");
    span.__rdP = 0;
  }
}

// 进度挂在谁身上：原句（列表 / 阅读页共用）、句内高亮词、复习卡正面大词，以及
// 卡片头部那枚「句内找不到落点」的裸词（`.ctx-mark` 挂在 `.row-top` 里，没有原句可挂）。
// 抽屉里的「发音」按钮（`.btn`）是界面文字，不参与划线。
// 2026-10-27（用户反馈「文章页播放时没有粉笔动画」）：文章页没有 `.row-sentence`，正文容器
// 是 `.para-text`。整段连播（owner = `.para-text`）与**卡里点记录词**（owner = `.ctx-mark`）
// 都挂回整段容器共用同一份逐词坐标 —— 整段装饰过之后 mark 子节点只剩 `.rd-w`，
// `decorateSentence(mark)` 会返回空数组（进度直接退出，点词就没划线了）；挂回整段既修这个，
// 也让「点词 → 整段」两种顺序共用同一份 span。
function speakHolder(owner) {
  if (!owner || typeof owner.closest !== "function") return null;
  const sentence = owner.closest(".row-sentence");
  if (sentence) return sentence;
  if (!owner.classList) return null;
  const paragraph = owner.closest(".para-text");
  if (owner.classList.contains("ctx-mark")) {
    if (paragraph) {
      // 段内偏移已有 `data-para-start`（articleParagraphBody 写的，与 `.rd-w` 的 cs/ce
      // 同一坐标系）→ 直接当 `rdOrigin` 用，speakProgressBegin 从这个词起笔。
      if (!Number.isFinite(Number(owner.dataset.rdOrigin))) {
        owner.dataset.rdOrigin = String(Number(owner.dataset.paraStart) || 0);
      }
      return paragraph;
    }
    return owner; // 句子卡外的裸词（`.row-top` 里那枚）仍只画自己
  }
  if (owner.classList.contains("review-target")) return owner;
  if (paragraph) return paragraph;
  return null;
}

// 开始一段进度：`chunks` 是这次要读的文本片（单段就一片；长段落是 splitForSpeech 的结果）。
// 原句自己 = 整段原文；点高亮词 / 读复习卡 = 只画那一个入口自己的字。
// v15 C3：`origin` 可选 —— 选区朗读从选区起点起笔（首次装饰前的偏移依然有效）。
function speakProgressBegin(owner, chunks, { mode = "audio", origin: startAt = null } = {}) {
  speakProgressClear();
  const holder = speakHolder(owner);
  const phrases = (Array.isArray(chunks) ? chunks : [chunks])
    .map((chunk) => String(chunk == null ? "" : chunk))
    .filter((chunk) => chunk);
  if (!holder || !phrases.length) return;
  const spans = decorateSentence(holder);
  if (!spans || !spans.length) return;
  // v16 C3：装饰替换了文本节点，活 Range 会跟着塌（研究文档探针 P5）—— 若自绘高亮正开着，
  // 每次装饰后都按偏移从快照重建一次；没开就不画（清掉的高亮不该被播放顺手复活）。
  // 重画是幂等的：快照的 owner 没被装饰时，重建出的还是同一个区间。
  if (selectionHighlightOn()) applySelectionHighlight(selectionRead);
  const whole = holder === owner;
  let origin = Number.isFinite(startAt) ? startAt : 0;
  if (!whole) {
    const entry = typeof owner.closest === "function" ? owner.closest(".ctx-mark") || owner : owner;
    // v27 §3.1：碎片段播的是覆盖词条的完整 target —— 起笔必须用该词条 span 的起点，
    // 不能用碎片段自己的 rdOrigin（否则长 target 会把绿笔顺延到后面的字上）。
    const playback = playbackForSegment(entry);
    const at = Number.isFinite(playback.start)
      ? playback.start
      : Number(entry && entry.dataset ? entry.dataset.rdOrigin : NaN);
    if (!Number.isFinite(at)) return; // 入口没有落在原句里（头部裸词之类）：不做进度
    origin = at;
  }
  const text = String(holder.textContent || "");
  const ranges = [];
  if (!whole) {
    ranges.push({ text: phrases[0], origin });
  } else {
    let cursor = origin; // 选区朗读：从选区在整句里的起点开始找
    for (const phrase of phrases) {
      const at = text.indexOf(phrase, cursor);
      const start = at >= 0 ? at : cursor; // 找不到（理论上不会）：按顺序硬接，进度不许乱跳
      ranges.push({ text: phrase, origin: start });
      cursor = start + phrase.length;
    }
  }
  const progress = {
    spans,
    ranges,
    table: null,
    origin: ranges[0] ? ranges[0].origin : origin,
    cursor: 0,
    u: 0,
    mode,
    duration: 0,
    startedAt: 0,
    raf: 0,
  };
  // 光标从「本次要读的第一个词」开始：点单词时，句子前面的词不该被划掉
  const first = spans.findIndex((span) => Number(span.dataset.ce) > progress.origin);
  progress.cursor = first < 0 ? spans.length : first;
  speakProgress = progress;
  progress.raf = window.requestAnimationFrame(speakProgressFrame);
  // 兜底时钟没有音频元素，begin 即起表；音频路径要等 play 前才显式起片
  if (mode === "clock") speakProgressChunk(0);
}

// 该播第 index 片了（长段落每片播之前调一次）
function speakProgressChunk(index) {
  const progress = speakProgress;
  if (!progress) return;
  const range = progress.ranges[index];
  if (!range) return;
  progress.origin = range.origin;
  progress.table = speakWeightTable(range.text);
  progress.u = 0;
  progress.startedAt = performance.now();
  progress.duration = progress.table.total * SPEAK_CLOCK_S_PER_WEIGHT;
  speakFillThrough(progress, range.origin); // 上一片若被跳过，这里把它补齐
  // 上一片划完后循环已停，这里重新起（换片间隙不空转）
  if (!progress.raf) progress.raf = window.requestAnimationFrame(speakProgressFrame);
}

// 这一片没拿到音频（退浏览器兜底 / 失败）：没有时间轴，就把这段直接补成划完
function speakProgressSkip(index) {
  const progress = speakProgress;
  if (!progress) return;
  const range = progress.ranges[index];
  if (!range) return;
  const end = range.origin + range.text.length;
  speakFillThrough(progress, end);
  progress.origin = end;
  progress.table = null;
  progress.u = 0;
}

// §6：播放前先停掉当前音频（pause + 回到开头），状态类一起收掉。
// v15 C1：这里是**停止**的销毁原语 —— 暂停标记、循环、播放条一起收（暂停不走这里）。
function pausePronounceAudio() {
  pronounceLongQueue.length = 0; // #114：停播就清掉整段队列，后续分片不再播
  pronounceLongPrefetch.clear(); // 前瞻请求作废（在途的回来只进缓存，不再播）
  clearLongPrimeQueue(); // 2026-10-26：待发的前瞻 / 下一段预取一并作废
  if (pronounceLongWait) pronounceLongWait(false); // 正在等 ended 的整段播放立刻停在这里
  if (fallbackWaitFinish) fallbackWaitFinish(false); // 浏览器兜底朗读也一起收尾（#114）
  if (pronounceAudio) {
    pronounceAudio.pause();
    try {
      pronounceAudio.currentTime = 0;
    } catch (error) {
      // 元数据还没到时赋值会抛；这时本来也没在播，忽略
    }
  }
  clearPronounceState();
  playbackSessionReset(); // 停止永远优先：暂停中换段 / 关抽屉 / Esc 都走回这里
}

// 停掉发音并**作废在途结果**：关抽屉 / 切路由 / 换词条都走这里
function stopPronounce() {
  // v31：听看复习的停止点也汇在这里 —— 用户在队列外点了别的发音 / 换段 / 切路由，
  // 本轮复习静默退出。驱动器自己换卡（orbit.focus）时用 reviewAdvancing 豁免。
  if (highlightReview && !reviewAdvancing) clearHighlightReview({ silent: true });
  // 连播取消点：停止键 / Esc / 换段 / 回星图 / 切路由 / 关抽屉全汇到这一处 ——
  // 过渡等待期的定时器一并取消（否则人已退出，「叮」一声又跳下一段）。
  // 驱动器自己 step(1) 也会经过这里（focusCard 内）：autoReadAdvancing 护栏豁免。
  cancelAutoReadGap();
  cancelArticleReadGap(); // v30 T2：文章连播的段间呼吸同样「停止即取消」
  if (autoReadActive && !autoReadAdvancing) {
    autoReadActive = false;
    renderPlaybar();
  }
  pronounceSeq += 1;
  pausePronounceAudio();
  if ("speechSynthesis" in window && window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
  }
}

// 失败只给一条轻提示：后端不可用是正常分支，控制台一律不刷（§6/§10）。
function warnPronounce(reason) {
  if (pronounceWarned) return;
  pronounceWarned = true;
  // 只报服务端给的原因码（disabled / offline / no_backend / timeout），不含文本
  toast(reason ? `发音暂不可用（${reason}）` : "发音暂不可用");
}

// v13 §2（票 #119）：可选 `priority` —— 只有后台预取带 "background"；用户点击
// （pronounce / pronounceLong 的当前片）保持旧 body（不带这个字段），旧行为不受影响。
const TTS_PRIORITY_BACKGROUND = "background";

function requestPronounceBlob(text, priority) {
  const payload = priority === TTS_PRIORITY_BACKGROUND ? { text, priority } : { text };
  // v16 T5（spec C6 / D7）：前端 20s 超时 = AbortController + 定时器，落定（成败都）清定时器。
  // abort 后 fetch 以 AbortError 拒绝（没有 status），正好落进调用方既有的「!status → 60s 冷却 +
  // 浏览器兜底」分支，**不新增第二条兜底链路**。
  // 注意：`pronouncePending` 让同文本的在途请求共享同一个 Promise，所以超时会同时让所有等待者
  // 失败 —— 这是可接受的：请求真挂了，所有等待者本来都该失败（而不是各自陪等到 80s）。
  const started = performance.now();
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), TTS_REQUEST_TIMEOUT_MS);
  return fetch("/api/tts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
    signal: controller.signal,
  })
    .then((response) => {
      if (!response.ok) {
        // 非 2xx：只读 JSON 的 error / reason（§4），提示文案只用它，不回显文本
        return response
          .json()
          .catch(() => null)
          .then((payload) => {
            const failure = new Error("tts_failed");
            failure.status = response.status; // 503 才进 60s 浏览器兜底冷却
            failure.reason = (payload && (payload.reason || payload.error)) || "";
            throw failure;
          });
      }
      // v25：把 X-TTS-Backend 一并带出 —— sapi 兜底音频不能进缓存
      //（rememberPronounceBlob），否则短暂网络抖动会让之后的播放一直机械音。
      return response.blob().then((blob) => ({
        blob,
        backend: response.headers.get("X-TTS-Backend") || "",
        cache: response.headers.get("X-TTS-Cache") || "",
      }));
    })
    .then((audio) => {
      const result = {
        url: URL.createObjectURL(audio.blob),
        size: audio.blob.size || 0,
        backend: audio.backend,
      };
      logPlaybackEvent("tts_ok", {
        ms: Math.round(performance.now() - started),
        size: result.size,
        backend: result.backend,
        cache: audio.cache,
      });
      return result;
    })
    .catch((error) => {
      // AbortError = 前端 20s 超时砍掉请求；服务端其实已经合成完（hub.log 里
      // 「tts backend=... ms=」有、访问行却没有），这条正是「卡顿 → 中断」的现场。
      const aborted = error && error.name === "AbortError";
      const status = Number(error && error.status) || 0;
      logPlaybackEvent(aborted ? "tts_timeout" : "tts_fail", {
        ms: Math.round(performance.now() - started),
        status,
        reason: aborted ? "abort" : status ? "http" : "network",
      });
      throw error;
    })
    .finally(() => window.clearTimeout(timeout));
}

// `key` 是已经建好的缓存键（含音色/语速，无法由 text 反推 —— 当前设置可能已变）。
function dropPronounceBlob(key) {
  const entry = pronounceCache.get(key);
  if (!entry) return;
  pronounceCache.delete(key);
  pronounceCacheBytes -= entry.size;
  URL.revokeObjectURL(entry.url); // 淘汰即回收，别让 blob 一直占着内存
}

// `key` 默认按 text 现算；只有 loadPronounceBlob 会显式传「发起请求时」的键 —— 在途期间
// 用户换了音色，回来的音频仍记在老音色键下，不会被错记成新音色的缓存（C7）。
function rememberPronounceBlob(
  text,
  url,
  size,
  key = pronounceCacheKey(text),
  backend = ""
) {
  // v25（2026-11-01 用户反馈「整段播放第 2 句变机械音」）：SAPI 兜底音频不进内存缓存 ——
  // 与服务端「SAPI 不落盘」同一口径。兜底音色与 Edge 音色不同，缓存它会让之后每次
  // 播放 / 重播一直机械音；不缓存，等冷却恢复后重取（通常几秒内）就回到正常音色。
  // 响应头缺失（旧桩 / 旧服务）时按可缓存处理，行为不变。
  if (backend === "sapi") return;
  // 同一个键换了新音频：旧 blob 一起收掉
  if (pronounceCache.has(key)) dropPronounceBlob(key);
  pronounceCache.set(key, { url, size });
  pronounceCacheBytes += size;
  while (
    pronounceCache.size > PRONOUNCE_CACHE_MAX ||
    (pronounceCacheBytes > PRONOUNCE_CACHE_BYTES && pronounceCache.size > 1)
  ) {
    dropPronounceBlob(pronounceCache.keys().next().value); // 迭代顺序 = LRU：首个就是最旧的键
  }
}

// 命中就拿 blob URL 直接把条目移到队尾（最近用过）；未命中返回 null。
// 注意：换 src 时**不能**当场 revoke 旧 URL —— 它可能还挂在缓存里给下一次重播用，
// 回收一律走 dropPronounceBlob（淘汰 / 同键替换）。
function takePronounceBlob(text) {
  const key = pronounceCacheKey(text);
  const entry = pronounceCache.get(key);
  if (!entry) return null;
  pronounceCache.delete(key);
  pronounceCache.set(key, entry);
  return entry.url;
}

// §6/§9：同一文本在途的请求共享同一个 Promise —— 连点/并发点同一词只合成一次
// `priority` 只在「这次真的发请求」时生效：已在途的请求保持先发起者的优先级
// （同一文本只发一次，不重复请求、不叠音）。
function loadPronounceBlob(text, priority) {
  const key = pronounceCacheKey(text); // 键按发起时的设置定：在途中改音色不会串键
  const running = pronouncePending.get(key);
  if (running) return running;
  const task = requestPronounceBlob(text, priority).then((audio) => {
    rememberPronounceBlob(text, audio.url, audio.size, key, audio.backend);
    // v25：解析值带上 backend —— 前瞻表据此把 sapi 结果让位（播到该片时重取）
    return { url: audio.url, backend: audio.backend };
  });
  pronouncePending.set(key, task);
  return task.then(
    (url) => {
      pronouncePending.delete(key);
      return url;
    },
    (error) => {
      pronouncePending.delete(key);
      throw error;
    }
  );
}

// v15 C1：`options` = { mode, origin, keepLoop }（可选；旧调用 `pronounce(text, owner)` 仍是单词发音）
//   mode="long" / "selection" 才在阅读页显示播放条；origin 只在选区朗读时给进度起点。
async function pronounce(text, owner, options = {}) {
  const phrase = String(text == null ? "" : text).trim();
  if (!phrase) return;
  const mode = options.mode || "word";
  const origin = Number.isFinite(options.origin) ? options.origin : null;
  const seq = ++pronounceSeq; // 这一轮点击的序号：迟到的旧结果不播
  pausePronounceAudio(); // 先停上一段（不递增序号，别把本轮作废）
  playbackMode = mode;
  playbackText = phrase;
  playbackChunks = [phrase];
  playbackOrigin = origin;
  if (options.keepLoop === true) playbackLoop = true; // 循环重播：别把开关一起清掉
  if ("speechSynthesis" in window && window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
  }
  if (Date.now() < pronounceFallbackUntil) {
    // §6/§10：后端刚判定不可用 —— 60s 内不再请求发音接口，直接浏览器兜底（不再提示）
    if (seq === pronounceSeq) {
      // v12：兜底朗读没有音频时长，按词权重估一条进度（begin 里起表，时间到就收）
      speakProgressBegin(owner, [phrase], { mode: "clock", origin });
      speakFallback(phrase, { warn: false, owner, seq });
    }
    return;
  }
  markPronounceOwner(owner || null);
  let url = takePronounceBlob(phrase); // 第二次点：命中内存缓存，不再发请求
  if (!url) {
    try {
      url = (await loadPronounceBlob(phrase)).url;
    } catch (error) {
      if (seq === pronounceSeq) {
        clearPronounceState();
        speakProgressBegin(owner, [phrase], { mode: "clock", origin });
        // v16 T5（C6 / D7）：选区朗读失败 → 浮标变「⚠ 重试」，快照保留（不清）。
        // 即使随后浏览器兜底真的念出来了，浮标仍是重试态：用户看到的「这条链路失败了」
        // 才是真相（S11 的口径），点一下就能再试在线发音。
        if (mode === "selection") markSelectionReadFailed(owner, phrase);
        const status = error && error.status;
        if (status === 503 || !status) {
          // §6/§10：503 / 网络失败 → 60s 内直接走浏览器兜底；同一页面只提示一次
          pronounceFallbackUntil = Date.now() + TTS_FALLBACK_MS;
          speakFallback(phrase, { warn: true, owner, seq });
        } else {
          // 400 / 413 等文本本身的问题：不进冷却，但也不静默失败（§10：413 用浏览器语音读）
          warnPronounce(error && error.reason ? error.reason : "");
          speakFallback(phrase, { warn: false, owner, seq });
        }
      }
      return; // 不抛未捕获异常：发音失败不该打断别的操作
    }
  }
  if (seq !== pronounceSeq) return; // 又点了别的 / 已被 stopPronounce 作废
  // v15 C1：取音频期间被暂停 → 等续播（回来仍从这一片起播）；停止 / 换段则直接退出
  if (!(await playbackHold(seq))) return;
  const audio = pronouncePlayer();
  if (audio.getAttribute("src") !== url) audio.src = url;
  try {
    audio.currentTime = 0; // 重复点同一段：从头上重播，不叠音
  } catch (error) {
    // 换了 src 还没元数据：本来就是从头播
  }
  speakProgressBegin(owner, [phrase], { origin }); // v12：音频真的开始播了才起进度
  speakProgressChunk(0);
  const started = audio.play();
  if (started && typeof started.catch === "function") {
    started.catch(() => {
      if (seq !== pronounceSeq) return;
      clearPronounceState(); // 播放被拒（自动播放策略 / 音频坏了）：收起状态、提示一次
      // v16 T5（C6）：play() 被拒也是失败的一种 —— 选区浮标要变成「⚠ 重试」
      if (mode === "selection") markSelectionReadFailed(owner, phrase);
      warnPronounce("");
    });
  }
}

/* --- v11 §2.1（票 #114）：长段落按句切分与逐句顺序播放 ------------------------ */

// v22 P3（票 #168 / spec §8.5）：分片算法搬到 `article-read.js` —— **唯一一份**纯函数，
// 浏览器（本页）与 Node 证据脚本（`docs/research/v22-audio-paragraph/plan_check.js`）
// 跑的是同一份代码。下面三个同名函数只做转发，旧调用点（整段朗读 / 选区 / 预取 / 文章页
// 连播）一个都不改，行为逐字不变。
const SPEECH_CHUNK_LIMIT = ArticleRead.SPEECH_CHUNK_LIMIT;

function splitForSpeech(text, limit = SPEECH_CHUNK_LIMIT) {
  return ArticleRead.splitForSpeech(text, limit);
}

function pushSpeechChunks(chunks, raw, limit) {
  ArticleRead.pushSpeechChunks(chunks, raw, limit);
}

function splitSpeechOverflow(sentence, limit) {
  return ArticleRead.splitSpeechOverflow(sentence, limit);
}

// 整段播放的队列：上一片播完再播下一片；句间不加额外静音（音频自带尾静音）。
let pronounceLongQueue = []; // 当前整段还没播的分片（停播 / 重播时清空）
let pronounceLongWait = null; // 正在等 `ended` 的收尾函数：停播时立刻解开这个 await
let pronounceLongPrefetch = new Map(); // 前瞻请求（text -> Promise；失败也留着，播到这片不重试）
// §6：发音取音频的**全局后台在途上限**（通用 prefetch 与长段落前瞻共用 v13 §2 的 1，
// 否则两套队列各限一份会让实际并发翻倍）。当前播放片自己发起的请求不计入这里。
let ttsFetchActive = 0;
// 2026-10-26：整段朗读的前瞻**独立预算**，不再借 `ttsFetchActive` 那唯一一条后台槽位。
// 翻页时 focusCard 会排 2 个原句片 + 8 个目标词，共用槽位就会把前瞻饿死十几秒 ——
// 每片播完现等合成，用户听感就是「断几秒再接上」。2 条 = 与 EdgePool 的 2 条连接对齐，
// 既能把下一片稳稳备好，也不会为一次朗读把连接池打满。
const LONG_LOOKAHEAD_CONCURRENCY = 2;
let longPrimeActive = 0; // 前瞻在途条数（按实际收口递减）
// 两级队列：当前段还没播的片（急）优先于下一段的整段（备）。分两级是因为
// `prefetchAutoNext()` 在 `pronounceLong` 的同步段之后才跑，若共用一个队列，
// 下一段会排到当前段的剩余片前面 —— 结果是要播的没取、取了的还用不上。
const longPrimeQueue = []; // 当前段的剩余片
const longNextQueue = []; // 下一段整段（段尾之前备好即可）

// 单片失败：该句退浏览器兜底（503 / 网络失败与 4xx 都退），**等它读完**，
// 后续句再继续；不整段重来、不叠音（#114 §2.1）。
async function pronounceChunkFailure(text, error) {
  const status = error && error.status;
  if (status === 503 || !status) {
    await speakFallbackWait(text, { warn: true }); // 同一页面只提示一次（warnFallbackSpeech 收口）
  } else {
    warnPronounce(error && error.reason ? error.reason : "");
    await speakFallbackWait(text, { warn: false });
  }
}

// 取一片音频：已缓存 / 已在途（前瞻或播放发起）就复用，否则现取。
// 失败结果留在 pronounceLongPrefetch 里 —— 播到这片时只看到一次失败，不重试。
function pronounceLongTask(chunk) {
  const running = pronounceLongPrefetch.get(chunk);
  if (running) return running;
  const cached = takePronounceBlob(chunk);
  // v25：内存缓存的 blob 一定是 Edge 音频（sapi 从 v25 起不进缓存）；表里统一解析
  // `{url, backend}` 形状 —— 与 loadPronounceBlob 的在途 Promise 一致。
  if (cached) return Promise.resolve({ url: cached, backend: "edge" });
  const task = loadPronounceBlob(chunk);
  pronounceLongPrefetch.set(chunk, task);
  return task;
}

// §2.1：把这一段的**剩余片**排进前瞻队列，播到哪补到哪（不阻塞首句）。
// 2026-10-26 重做：原实现只看后两片，且与通用预取共用 `ttsFetchActive` 那唯一一条槽位。
// 而翻页时 focusCard 会排入 2 个原句片 + 8 个目标词，槽位被它们连续占住十几秒，前瞻
// 一次都发不出去 → 每片播完都要现等 1–2s 合成，就是用户体感的「停顿-加载-续播」。
// 现在前瞻有自己的预算（LONG_LOOKAHEAD_CONCURRENCY），与后台预取互不挤占；仍走用户
// 优先级 —— 用后台优先级会让冷却期内的前瞻直接 503，把失败缓存进 pronounceLongPrefetch。
function primeLongLookahead(chunks, index) {
  // 只排「还没排过」的片：播放本身也在往队列里推同样文本，重复排会被下面的去重挡掉。
  for (let ahead = index + 1; ahead < chunks.length; ahead += 1) {
    primeLongChunk(chunks[ahead], longPrimeQueue);
  }
  pumpLongPrime();
}

// 整段备用（连播的下一段）：进低一级队列，不与当前段的剩余片抢位。
function primeLongBatch(chunks) {
  for (const chunk of chunks) primeLongChunk(chunk, longNextQueue);
  pumpLongPrime();
}

// 单片入前瞻队列（内存已命中就直接登记进 pronounceLongPrefetch，不占并发预算）
function primeLongChunk(chunk, queue) {
  if (!chunk || pronounceLongPrefetch.has(chunk)) return;
  const cached = takePronounceBlob(chunk);
  if (cached) {
    pronounceLongPrefetch.set(chunk, Promise.resolve({ url: cached, backend: "edge" }));
    return;
  }
  if (pronouncePending.has(pronounceCacheKey(chunk))) return; // 播放 / 预取已经在取
  if (longPrimeQueue.includes(chunk) || longNextQueue.includes(chunk)) return;
  queue.push(chunk);
}

// 补位泵：有空位就从**急**队列头取一片发出去，急了再取备用队列。
// `pronounce` / `pronounceLong` 的当前片不走这里（用户点击永远现发，不排队 —— #119 §2 口径）。
function pumpLongPrime() {
  while (longPrimeActive < LONG_LOOKAHEAD_CONCURRENCY) {
    const chunk = longPrimeQueue.length ? longPrimeQueue.shift() : longNextQueue.shift();
    if (!chunk) return;
    if (pronounceLongPrefetch.has(chunk)) continue;
    const cached = takePronounceBlob(chunk);
    if (cached) {
      pronounceLongPrefetch.set(chunk, Promise.resolve({ url: cached, backend: "edge" }));
      continue;
    }
    longPrimeActive += 1;
    const task = loadPronounceBlob(chunk); // 用户优先级：见 primeLongLookahead 的注释
    pronounceLongPrefetch.set(chunk, task);
    task
      .then((audio) => {
        // v25：SAPI 兜底结果不钉在前瞻表 —— 播到这片时重取，冷却恢复后就是正常音色。
        // 正在等的当前片不受影响：await 已拿到值，删的只是表项。
        if (audio.backend === "sapi") pronounceLongPrefetch.delete(chunk);
      })
      .catch(() => null) // 预取失败静默：播到这片时由播放路径退兜底
      .then(() => {
        longPrimeActive -= 1; // 按实际收口递减；停播也不提前清零
        pumpLongPrime();
      });
  }
}

// 停止时清空前瞻队列（在途请求回来只进缓存，不再续排）
function clearLongPrimeQueue() {
  longPrimeQueue.length = 0;
  longNextQueue.length = 0;
}

// 播一片并等它播完（`ended`）；停止 / 重播时由 pausePronounceAudio 立刻解开。
// `chunkIndex` = 这一片在整段里的序号（v12 进度用；-1 = 不是长段落，不起进度）。
function playPronounceUrl(url, seq, chunkIndex = -1) {
  const audio = pronouncePlayer();
  const beganAt = performance.now();
  const chunkFields = chunkIndex >= 0 ? { chunk: chunkIndex } : {};
  logPlaybackEvent("chunk_play", chunkFields);
  return new Promise((resolve) => {
    const finish = (ok) => {
      audio.removeEventListener("ended", onEnded);
      audio.removeEventListener("error", onError);
      if (pronounceLongWait === finish) pronounceLongWait = null;
      logPlaybackEvent(
        ok ? "chunk_ended" : "chunk_error",
        Object.assign({}, chunkFields, {
          ms: Math.round(performance.now() - beganAt),
          code: ok ? 0 : Number(audio.error && audio.error.code) || 0,
        })
      );
      resolve(ok);
    };
    const onEnded = () => finish(true);
    const onError = () => finish(false);
    audio.addEventListener("ended", onEnded);
    audio.addEventListener("error", onError);
    pronounceLongWait = finish; // 停播时由 pausePronounceAudio 解开
    if (audio.getAttribute("src") !== url) audio.src = url;
    try {
      audio.currentTime = 0; // 重复点同一段：从头上重播，不叠音
    } catch (error) {
      // 换了 src 还没元数据：本来就是从头播
    }
    if (chunkIndex >= 0) speakProgressChunk(chunkIndex); // v12：本片从 0 开始划线
    const started = audio.play();
    if (started && typeof started.catch === "function") {
      started.catch(() => {
        logPlaybackEvent(
          "play_rejected",
          Object.assign({}, chunkFields, {
            ms: Math.round(performance.now() - beganAt),
          })
        );
        if (seq !== pronounceSeq) {
          finish(false);
          return;
        }
        // v16 T5（C6）：选区这一轮播放被拒 = 失败，浮标变「⚠ 重试」（快照保留）。
        // 先取 owner / 文本再 clearPronounceState（它会清掉 pronounceOwner）。
        if (playbackMode === "selection") markSelectionReadFailed(pronounceOwner, playbackText);
        clearPronounceState(); // 播放被拒（自动播放策略 / 音频坏了）：收起状态、提示一次
        warnPronounce("");
        finish(false);
      });
    }
  });
}

// §2.1（票 #114）：点整段 → 逐句顺序播放。首片立即请求并播放（用户优先级），后续片用现有
// `loadPronounceBlob` 前瞻 2 片（v13 §2：后台并发 ≤1、后台优先级），不阻塞首句；任一片失败
// 退浏览器兜底、后续继续；停止（关抽屉 / 切路由 / 换段 / 点别的）即清空队列，整段只挂一个
// `is-speaking`。
// v15 C1：`options` = { mode, text, origin, keepLoop }。每片开始前与 fetch 收口后各过一次
// playbackHold —— 暂停中不取片不播，等片时暂停则 fetch 回来也不播（续播后从当前片继续）。
async function pronounceLong(chunks, owner, options = {}) {
  const mode = options.mode || "long";
  const text = options.text || chunks.join(" ");
  const origin = Number.isFinite(options.origin) ? options.origin : null;
  // v22 P3（票 #168）：文章页连播需要知道「现在要播第几片」来点亮记录 span / 更新播放条。
  // 只读回调，旧调用方不传 → null，行为与之前逐字一致（不改分片顺序 / 队列 / 前瞻）。
  const onChunk = typeof options.onChunk === "function" ? options.onChunk : null;
  const seq = ++pronounceSeq;
  pausePronounceAudio(); // 先停上一段（不递增序号，别把本轮作废）
  playbackMode = mode;
  playbackText = text;
  playbackChunks = chunks.slice();
  playbackOrigin = origin;
  if (options.keepLoop === true) playbackLoop = true; // 循环重播：别把开关一起清掉
  if ("speechSynthesis" in window && window.speechSynthesis.speaking) {
    window.speechSynthesis.cancel();
  }
  pronounceLongQueue = chunks.slice();
  markPronounceOwner(owner || null);
  speakProgressBegin(owner, chunks, { origin }); // v12：进度跨片连续，整段只画一条线
  // 首片同步发起请求（先于前瞻，保证首片先发）；命中内存缓存则直接用
  const firstTask = pronounceLongTask(chunks[0]);
  for (let index = 0; index < chunks.length; index += 1) {
    if (!(await playbackHold(seq))) return; // v15 C1：暂停中不取片不播；停止 / 换段直接退出
    if (seq !== pronounceSeq) return;
    const chunk = chunks[index];
    pronounceLongQueue.shift();
    // v22 P3：当前分片（取音频前就点亮/更新播放条 —— 等合成的那 1–2s 里也有「轮到这一片」的
    // 反馈；真正出声时高亮已经在位）。旧调用方没有回调，这一行是 no-op。
    if (onChunk) onChunk(index, chunk);
    // v13 §2：当前片先发（用户优先级、不进后台队列）—— 再让前瞻去占余下的后台槽位，
    // 这样后台并发 1 时前瞻也抢不到当前片前面，请求顺序仍是分片顺序。
    const currentTask = index === 0 ? firstTask : pronounceLongTask(chunk);
    primeLongLookahead(chunks, index); // 往后两片提前取（不挡首句）
    let url;
    try {
      url = (await currentTask).url;
    } catch (error) {
      if (seq !== pronounceSeq) return;
      speakProgressSkip(index); // 这一片没有音频：直接补成划完，别卡在半句
      // v16 T5（C6）：选区这一轮（任一 片）取不到音频 = 失败 —— 浮标变「⚠ 重试」，
      // 否则长选区会一直停在「合成中」。快照保留。
      if (mode === "selection") markSelectionReadFailed(owner, text);
      await pronounceChunkFailure(chunk, error); // 该句退兜底并读完，后续句继续
      if (seq !== pronounceSeq) return;
      continue;
    }
    if (!(await playbackHold(seq))) return; // 等片期间被暂停：fetch 回来也不播
    if (seq !== pronounceSeq) return;
    // v13 §2：当前片的请求刚收口，后台槽位可能刚空出来 —— 再补一次前瞻，
    // 让后两片赶在这一片播完前取回（否则当前片靠预取时前瞻会被挤到下一轮）。
    primeLongLookahead(chunks, index);
    const played = await playPronounceUrl(url, seq, index); // 上一片播完才播下一片（不叠音）
    if (!played && seq === pronounceSeq) {
      speakProgressSkip(index); // 音频中途坏了：补成划完
      // v16 T5（C6）：长选区里某片音频坏了也是「这条链路失败了」→ 浮标变「⚠ 重试」
      if (mode === "selection") markSelectionReadFailed(owner, text);
    }
  }
  if (seq === pronounceSeq) {
    // 整段自然播完：这一轮的前瞻表 / 待发队列已经没有用处（2026-10-26）。
    // 不清的话，命中缓存的片会被记号钉在表里（那些 promise 永不 reject），
    // 连播一路走下去就会攒住整篇的文本键 —— 纯内存泄漏，且不参与 LRU 淘汰。
    pronounceLongPrefetch.clear();
    clearLongPrimeQueue();
    endPlayback(); // 循环开就从头再播，否则清状态
  }
}

/* --- v11 §3（票 #113）：预取 / 预热 ---------------------------------------------- */

// `prefetch(text)` 是前端**唯一**的预取入口：和 `pronounce` 共享在途 Promise 与内存
// LRU，取回 blob 只进缓存、**绝不播放**。队列并发 1（v13 §2：后台让路给用户点击）；
// 已缓存 / 在途 / 已在队列的直接复用。
const PREFETCH_CONCURRENCY = 1; // v13 §2：后台并发上限（长段落前瞻也共用这个计数）
// v13 §1：阅读页只预取点击真正会播的**前 1–2 片**，不再把整段原句发去合成。
const PREFETCH_CONTEXT_CHUNKS = 2;
const prefetchQueue = []; // 待发的预取文本（去重；换卡 / 路由切换 / 页面隐藏时清空）

function prefetch(text) {
  if (!ttsEnabled()) return; // §3/§7：关掉在线 = 零请求
  // §6/§10：后端刚判定不可用（60s 冷却）时用户点击本来就退浏览器兜底，
  // 预取更不该再去撞发音接口（v13 §5：提前预取失败静默、不拖慢点击）。
  if (Date.now() < pronounceFallbackUntil) return;
  const phrase = String(text == null ? "" : text).trim();
  if (!phrase) return;
  // 已进内存缓存 / 同一文本正在请求（可能是点击发起的）：直接复用，不重复请求
  // v16 C7：键含音色/语速 —— 换了音色这里就是 miss，预取会为新音色重新取
  const key = pronounceCacheKey(phrase);
  if (pronounceCache.has(key) || pronouncePending.has(key)) return;
  if (prefetchQueue.includes(phrase)) return;
  prefetchQueue.push(phrase);
  runPrefetchQueue();
}

// v13 §5（P1-2）：按下 / 键盘聚焦时就提前取「即将点击的那段」的**首片**。
// 走唯一入口 `prefetch` —— 内存缓存 / 在途 Promise / 待发队列三重去重都在里面，
// 悬停、按下、聚焦、点击不会为同一段文本各发一次；失败静默。
function prefetchIntent(text) {
  const first = splitForSpeech(text)[0];
  if (first) prefetch(first);
}

function runPrefetchQueue() {
  while (ttsFetchActive < PREFETCH_CONCURRENCY && prefetchQueue.length) {
    const phrase = prefetchQueue.shift();
    ttsFetchActive += 1;
    loadPronounceBlob(phrase, TTS_PRIORITY_BACKGROUND)
      .catch(() => null) // 预取失败静默：用户点下去时还有 SAPI / 浏览器兜底
      .then(() => {
        ttsFetchActive -= 1;
        runPrefetchQueue();
      });
  }
}

// §6：路由切换 / 换卡 / 页面隐藏时清空**待发**队列；已经发出的请求保留（回来的音频照样进缓存）
function clearPrefetchQueue() {
  prefetchQueue.length = 0;
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) return;
  clearPrefetchQueue();
  // v16 C9（票 #137）：页面隐藏 = 拖选中途被打断，探针必须复位（T4 的 pointerHeld 同一套复位）
  dragProbe = null;
  pointerHeld = false;
});

// §3/§6：列表加载 / 选中会话后，把当前列表靠前的 30–40 个 target 交给后台预热。
// 去抖 500ms（连着切会话只发最后一批）；同一批不重复发；关闭在线零请求；失败静默。
const PREWARM_DEBOUNCE_MS = 500;
const PREWARM_MAX_TARGETS = 40;
let prewarmTimer = 0;
let prewarmBatch = [];
let prewarmSentKey = "";

function prewarmTargets(texts) {
  const unique = [
    ...new Set(
      (Array.isArray(texts) ? texts : [])
        .map((text) => String(text == null ? "" : text).trim())
        .filter(Boolean)
    ),
  ].slice(0, PREWARM_MAX_TARGETS);
  if (!unique.length) return;
  prewarmBatch = unique;
  if (prewarmTimer) window.clearTimeout(prewarmTimer);
  prewarmTimer = window.setTimeout(() => {
    prewarmTimer = 0;
    const batch = prewarmBatch;
    prewarmBatch = [];
    if (!batch.length || !ttsEnabled()) return; // 关闭在线：一个请求都不发
    const key = batch.join("\n");
    if (key === prewarmSentKey) return; // 同一批已经发过（缓存温度没变）
    prewarmSentKey = key;
    fetch("/api/tts/prewarm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ texts: batch }),
    }).catch(() => null); // 预热只是加速：失败不提示、不重试
  }, PREWARM_DEBOUNCE_MS);
}

function drawerSection(title) {
  const section = el("section", "drawer-section");
  section.append(el("h3", "section-title", title));
  return section;
}

// --- v15 T3（票 #128）：原句可编辑 + 段级保存 + 位置编辑 -----------------------

// 「段」= 同一会话里原句逐字相同的词条（与 `contextCards` 同一套规则）。`context` 为空的
// 词条永远不成段（`contextCards` 也是各占一张卡），所以空原句的保存范围就是本条。
function sameSegment(record, other, context) {
  if (!context) return false;
  const left = record.session_id == null ? "none" : String(record.session_id);
  const right = other.session_id == null ? "none" : String(other.session_id);
  return left === right && String(other.context || "").trim() === context;
}

// 本段词条（含本条；抽屉记录可能不在当前列表里，所以它自己一定补进来）。
// 列表只载入最新 500 条，这里算的是**前端可见**的条数；真正改了几条以段级保存
// 响应里的 `affected` 为准（后端的 WHERE 打的是整库）。
function segmentPeers(record) {
  const context = String(record.context || "").trim();
  if (!context) return [record];
  return [
    record,
    ...state.records.filter(
      (item) => item.id !== record.id && sameSegment(record, item, context)
    ),
  ];
}

// §B3 的定位试算要把换行 / 连续空白折叠掉（后端落库走 `one_line`，同一套口径）。
function contextProbe(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

// §B2：段级保存 = 后端一条事务（不做前端循环 PATCH —— 非原子、部分失败会把一张
// 归并卡裂成两张）。`expect_context` 带抽屉里的旧原句，过期由后端 400 挡下。
async function saveSegmentContext(record, value) {
  const oldContext = String(record.context || "").trim();
  // 保存前先记下本段 id：保存后 state.records 已是新原句，再按 id 反查就散段了。
  const peerIds = segmentPeers(record).map((item) => item.id);
  try {
    const payload = await api(
      `/api/records/${record.id}`,
      jsonRequest("PATCH", {
        context: value,
        scope: "segment",
        expect_context: record.context || "",
      })
    );
    const fresh = payload.record ? payload.record.context : value;
    state.records = state.records.map((item) => {
      if (payload.record && item.id === record.id) return payload.record;
      return sameSegment(record, item, oldContext)
        ? { ...item, context: fresh }
        : item;
    });
    state.drawer = payload.record;
    toast(`已保存原句（本段 ${payload.affected} 条）`);
    invalidateSelectionRead(); // v16 C1：段级改原句同样让旧快照失效（随后重渲染会换 DOM）
    renderDrawer();
    refreshAfterChange(peerIds, { structural: true });
  } catch (error) {
    toast(`保存失败：${error && error.message ? error.message : error}`);
  }
}

// §B1 / §B3：「原句」区 —— textarea + 保存（Ctrl+Enter）+「同时改本段全部词条」
// （默认勾选）+ 未命中时的内联确认条（自制轻量条，不用原生 confirm）。
function contextEditSection(record) {
  const section = drawerSection("原句");
  const area = el("textarea", "field field-area");
  area.value = record.context || "";
  drawerEditable("context", area, record.context || "");
  area.rows = 4;
  area.placeholder = "原句，或按 Ctrl+Enter 保存（清空 = 没有原句）";
  area.setAttribute("aria-label", "原句");
  section.append(area);

  const scopeRow = el("div", "field-row");
  const scopeBox = el("label", "form-check");
  const scopeInput = el("input");
  scopeInput.type = "checkbox";
  scopeInput.checked = true; // §B1：默认整段
  scopeInput.setAttribute("aria-label", "同时改本段全部词条");
  scopeBox.append(scopeInput);
  scopeBox.append(el("span", null, "同时改本段全部词条"));
  scopeRow.append(scopeBox);
  const peers = segmentPeers(record).length;
  scopeRow.append(
    el("span", "hint is-wrap", `本段 = 同一会话里原句逐字相同的 ${peers} 条`)
  );
  section.append(scopeRow);

  const guard = el("div", "context-guard");
  section.append(guard);

  const saveRow = el("div", "field-row");
  const save = button("btn btn-primary", "保存原句", () => saveContext());
  saveRow.append(save);
  saveRow.append(el("span", "hint", "Ctrl+Enter 也能保存"));
  section.append(saveRow);

  function saveContext() {
    const value = area.value;
    const probe = contextProbe(value);
    // 空原句允许保存（= 没有原句）；只有同段/本条的保存范围才拦未命中的词。
    const inSegment = scopeInput.checked && probe !== "";
    const targets = inSegment
      ? segmentPeers(record).map((item) => item.target)
      : [record.target];
    const missing = targets.filter((target) => !findTargetSpan(probe, target));
    if (probe && missing.length) {
      showGuard(missing, () => doSave(value, inSegment));
      return;
    }
    clear(guard);
    doSave(value, inSegment);
  }

  function showGuard(missing, onConfirm) {
    clear(guard);
    const box = el("div", "confirm-box");
    box.append(
      el(
        "span",
        "hint",
        `${missing.length} 个词已不在句中：${missing.join("、")}` +
          "（高亮与点词发音会失效）"
      )
    );
    box.append(
      button("btn btn-primary", "仍保存", () => {
        clear(guard);
        onConfirm();
      })
    );
    box.append(button("btn", "取消", () => clear(guard)));
    guard.append(box);
  }

  function doSave(value, inSegment) {
    if (inSegment) saveSegmentContext(record, value);
    else patchRecord(record.id, { context: value }, "已保存原句");
  }

  area.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      save.click();
    }
  });
  return section;
}

function kvBox(rows) {
  const box = el("dl", "kv");
  for (const [key, value] of rows) {
    box.append(el("dt", null, key));
    box.append(el("dd", null, value));
  }
  return box;
}

// §4/§2.6：会话类型（7 类中文下拉，PATCH /api/sessions/{id}）+ 原链接（PATCH，400 走 toast）
function sessionSection(record, session) {
  const category = sessionCategory(session);
  const section = drawerSection("会话");

  const head = el("div", "session-row");
  head.classList.add(`cat-${category}`);
  head.append(el("span", "cat-dot"));
  head.append(el("span", "session-name", session.title));
  head.append(el("span", "badge session-cat", CATEGORY_LABEL[category]));
  section.append(head);

  const catRow = el("div", "field-row");
  catRow.append(el("span", "hint", "类型"));
  const select = el("select", "field");
  for (const [value, label] of CATEGORIES) {
    const option = el("option", null, label);
    option.value = value;
    if (value === category) option.selected = true;
    select.append(option);
  }
  select.addEventListener("change", () => {
    if (select.value === category) return;
    patchSession(
      session.id,
      { category: select.value },
      `会话已改为「${CATEGORY_LABEL[select.value]}」`
    );
  });
  catRow.append(select);
  section.append(catRow);

  const urlInput = el("input", "field");
  urlInput.type = "url";
  urlInput.value = session.url || "";
  drawerEditable("url", urlInput, session.url || "");
  urlInput.placeholder = "https://…（原视频 / 页面链接）";
  urlInput.setAttribute("aria-label", "会话原链接");
  section.append(urlInput);

  const urlRow = el("div", "field-row");
  const saveUrl = button("btn", "保存链接", () => {
    const value = urlInput.value.trim();
    if (value === String(session.url || "")) return;
    patchSession(
      session.id,
      { url: value },
      value ? "已保存会话链接" : "已清掉会话链接"
    );
  });
  urlInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveUrl.click();
    }
  });
  urlRow.append(saveUrl);
  urlRow.append(el("span", "hint", "改成空 = 清掉链接"));
  section.append(urlRow);

  const linkRow = el("div", "review-link-row");
  if (session.url) {
    linkRow.append(videoLink(session.url, positionSeconds(record.position), session.site));
  } else {
    linkRow.append(el("span", "hint", "这条会话还没有链接"));
  }
  section.append(linkRow);
  return section;
}

// W1（票 #178 / spec F4）：抽屉里「显式保存」的输入字段（释义/原句/位置/标签/会话链接）。
// 任一次保存成功都会整块重绘抽屉，以前其他字段没保存的手打内容会被旧值静默冲掉 ——
// 现在重绘前先采集「DOM 当前值 ≠ 渲染时的服务器值」的字段（= 未保存编辑），
// 同一条词条的重绘原样带回；换词条的去留由 openDrawer 的确认条显式处理。
let drawerEditNodes = new Map(); // 本次渲染登记的可编辑字段（key → { node, saved }）
let drawerEditRecordId = null; // 这些字段属于哪条词条
let drawerKeepEdits = null; // 重绘时待带回的未保存编辑（key → 值）

// 渲染时登记一个可编辑字段；saved = 本轮渲染所依据的服务器值。
// 若这轮重绘带回了对同一条词条的未保存编辑，登记完立刻还原到输入框里。
function drawerEditable(key, node, saved) {
  drawerEditNodes.set(key, { node, saved: String(saved) });
  if (drawerKeepEdits && drawerKeepEdits[key] !== undefined) {
    node.value = String(drawerKeepEdits[key]);
  }
  return node;
}

function collectDrawerEdits() {
  const edits = {};
  for (const [key, entry] of drawerEditNodes) {
    if (!entry.node.isConnected) continue; // 抽屉已收起 / 节点已随重绘脱离文档
    if (String(entry.node.value) !== entry.saved) edits[key] = String(entry.node.value);
  }
  return Object.keys(edits).length ? edits : null;
}

// 换词条的确认条：prepend 在抽屉顶部，不动抽屉里的输入框（编辑内容还在节点里，
// 「继续编辑」只需要把这条摘掉）。确认样式沿用删除词条的 confirm-box。
const DRAWER_EDIT_LABELS = {
  meaning: "释义",
  context: "原句",
  position: "位置",
  tags: "标签",
  url: "会话链接",
};

function showDrawerSwitchConfirm(edits, nextId) {
  const existing = dom.drawer.querySelector(".drawer-switch-guard");
  if (existing) existing.remove();
  const names = Object.keys(edits)
    .map((key) => DRAWER_EDIT_LABELS[key] || key)
    .join("、");
  const box = el("div", "confirm-box drawer-switch-guard");
  box.append(
    el("span", "hint", `还有没保存的修改（${names}）：换一条词条就会丢掉`)
  );
  box.append(
    button("btn btn-primary", "放弃修改并打开", () =>
      openDrawer(nextId, { force: true })
    )
  );
  box.append(button("btn", "继续编辑", () => box.remove()));
  dom.drawer.prepend(box);
}

function renderDrawer() {
  const record = state.drawer;
  // 重绘 = 某次保存成功（或删除确认态切换）。先把旧 DOM 里没保存的编辑收起来；
  // 只在同一条词条的重绘里带回 —— 换词条时 drawerEditRecordId 对不上、自然丢弃，
  // 那条路已经在 openDrawer 里让用户明确选过了。
  drawerKeepEdits =
    record && drawerEditRecordId === record.id ? collectDrawerEdits() : null;
  drawerEditNodes = new Map();
  drawerEditRecordId = record ? record.id : null;
  clear(dom.drawer);
  if (!record) {
    drawerKeepEdits = null;
    return;
  }

  // 头部：目标词大字 + 音标 + 发音 + 关闭
  const head = el("div", "drawer-head");
  const titleBox = el("div", "drawer-title-box");
  const kindKey = KIND_LABEL[record.kind] ? record.kind : null;
  const title = el("div", "drawer-title", record.target);
  if (kindKey) title.classList.add(`kind-${kindKey}`); // §5：抽屉里 kind 色
  titleBox.append(title);
  const meta = el("div", "drawer-meta");
  if (record.phonetic) meta.append(el("span", "row-phonetic", record.phonetic));
  // 颜色不是唯一信号：类型文字徽章保留（§5）
  meta.append(
    el(
      "span",
      kindKey ? `badge kind-${kindKey}` : "badge",
      KIND_LABEL[record.kind] || record.kind
    )
  );
  if (Number(record.hits) > 1) meta.append(el("span", "badge", `命中 ×${record.hits}`));
  if (isDue(record)) meta.append(el("span", "badge is-due", "待复习"));
  for (const tag of tagList(record)) meta.append(el("span", "badge is-tag", tag));
  titleBox.append(meta);
  head.append(titleBox);
  const actions = el("div", "drawer-actions");
  actions.append(
    // #107 §6：走统一发音管线；按钮自己是 is-speaking 的宿主（只换配色，不改文字）
    button("btn", "发音", (event) => pronounce(record.target, event.currentTarget))
  );
  actions.append(button("btn btn-subtle", "关闭 Esc", closeDrawer));
  head.append(actions);
  dom.drawer.append(head);

  // 释义（可编辑，PATCH）
  const meaningSection = drawerSection("释义");
  const meaningArea = el("textarea", "field field-area");
  meaningArea.value = record.meaning || "";
  drawerEditable("meaning", meaningArea, record.meaning || "");
  meaningArea.rows = 3;
  meaningArea.placeholder = "手填释义，或按 Ctrl+Enter 保存";
  meaningSection.append(meaningArea);
  const meaningRow = el("div", "field-row");
  const saveMeaning = button("btn btn-primary", "保存释义", () =>
    patchRecord(record.id, { meaning: meaningArea.value }, "已保存释义")
  );
  meaningArea.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      saveMeaning.click();
    }
  });
  meaningRow.append(saveMeaning);
  if (record.meaning) {
    const recheck = button("btn", "重查", (event) =>
      startRecheck(record, event.currentTarget)
    );
    recheck.title = "重新查这一条的释义（先预览新旧释义，确认后才替换）";
    meaningRow.append(recheck);
  }
  meaningRow.append(
    el("span", "hint", `来源：${meaningSourceLabel(record)}`)
  );
  meaningSection.append(meaningRow);
  dom.drawer.append(meaningSection);

  // 原句（v15 T3 票 #128：可编辑 + 段级保存）
  dom.drawer.append(contextEditSection(record));

  // 出处
  const sourceSection = drawerSection("出处");
  sourceSection.append(
    kvBox([
      ["应用", record.source_app || "—"],
      ["标题", record.source_title || "—"],
      ["会话", sessionTitle(record.session_id == null ? "none" : String(record.session_id))],
    ])
  );
  // 位置（v15 T3 票 #128：可编辑，PATCH {position}）；空 = 清掉位置
  const positionRow = el("div", "field-row");
  positionRow.append(el("span", "hint", "位置"));
  const positionInput = el("input", "field");
  positionInput.type = "text";
  positionInput.value = record.position || "";
  drawerEditable("position", positionInput, record.position || "");
  positionInput.placeholder = "12:34 / 第 3 段";
  positionInput.setAttribute("aria-label", "原视频位置");
  const savePosition = button("btn", "保存位置", () => {
    const value = positionInput.value.trim();
    if (value === String(record.position || "")) return;
    patchRecord(record.id, { position: value }, "已保存位置");
  });
  positionInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      savePosition.click();
    }
  });
  positionRow.append(positionInput);
  positionRow.append(savePosition);
  sourceSection.append(positionRow);
  dom.drawer.append(sourceSection);

  // v3 §4/§2.6：会话类型（7 类下拉）+ 原链接（可改）+ 打开原视频
  const recordSession = sessionById(record.session_id);
  if (recordSession) dom.drawer.append(sessionSection(record, recordSession));

  // 收录时间
  const timeSection = drawerSection("时间");
  timeSection.append(
    kvBox([
      ["收录", formatFull(record.captured_at)],
      ["最近捕获", record.last_seen_at ? formatFull(record.last_seen_at) : "—"],
      ["命中次数", String(record.hits || 1)],
    ])
  );
  dom.drawer.append(timeSection);

  // 复习统计（§8.1；W2 的打分也会更新这里）
  const srsSection = drawerSection("复习统计");
  srsSection.append(
    kvBox([
      ["间隔", `${Number(record.interval_days || 0)} 天`],
      ["复习次数", String(record.reps || 0)],
      ["遗忘", String(record.lapses || 0)],
      ["熟练度", String(record.ease == null ? 2.5 : record.ease)],
      ["下次复习", record.due_at ? formatFull(record.due_at) : "—"],
      ["最近复习", record.last_review_at ? formatFull(record.last_review_at) : "—"],
    ])
  );
  dom.drawer.append(srsSection);

  // 类型（PATCH）
  const kindSection = drawerSection("类型");
  const seg = el("div", "seg");
  for (const [value, label] of KINDS) {
    const node = button(
      record.kind === value ? "chip is-on" : "chip",
      label,
      () => {
        if (record.kind !== value) {
          patchRecord(record.id, { kind: value }, `已改为「${label}」`);
        }
      }
    );
    seg.append(node);
  }
  kindSection.append(seg);
  dom.drawer.append(kindSection);

  // 标签（PATCH）
  const tagSection = drawerSection("标签");
  const tagInput = el("input", "field");
  tagInput.type = "text";
  tagInput.value = record.tags || "";
  drawerEditable("tags", tagInput, record.tags || "");
  tagInput.placeholder = "#cet4 #口语（空格分隔，自动补 #）";
  const saveTags = button("btn", "保存标签", () =>
    patchRecord(record.id, { tags: tagInput.value }, "已保存标签")
  );
  tagInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveTags.click();
    }
  });
  tagSection.append(tagInput);
  const tagRow = el("div", "field-row");
  tagRow.append(saveTags);
  tagRow.append(el("span", "hint", "点左栏标签云可按标签过滤"));
  tagSection.append(tagRow);
  dom.drawer.append(tagSection);

  // 挪会话（PATCH session_id）
  const moveSection = drawerSection("挪到别的会话");
  const select = el("select", "field");
  for (const session of state.sessions) {
    const option = el("option", null, `${session.title}（${session.records} 词）`);
    option.value = String(session.id);
    if (String(session.id) === String(record.session_id)) option.selected = true;
    select.append(option);
  }
  if (record.session_id == null) {
    const option = el("option", null, "（未归属）");
    option.value = "";
    option.selected = true;
    select.append(option);
  }
  select.addEventListener("change", () => {
    patchRecord(record.id, { session_id: Number(select.value) }, "已挪到别的会话");
  });
  moveSection.append(select);
  dom.drawer.append(moveSection);

  // 删除（带确认）
  const dangerSection = drawerSection("危险操作");
  if (!state.confirmDelete) {
    dangerSection.append(
      button("btn btn-danger", "删除这条词条", () => {
        state.confirmDelete = true;
        renderDrawer();
      })
    );
  } else {
    const box = el("div", "confirm-box");
    box.append(el("span", "hint", `删除「${record.target}」后不可恢复`));
    box.append(
      button("btn btn-danger", "确认删除", () => deleteRecord(record.id))
    );
    box.append(
      button("btn", "取消", () => {
        state.confirmDelete = false;
        renderDrawer();
      })
    );
    dangerSection.append(box);
  }
  dom.drawer.append(dangerSection);
}

// §4/§2.6：改会话（category / url）；400 由后端给出中文原因，走 toast 报错不静默
async function patchSession(id, fields, message) {
  try {
    const payload = await api(`/api/sessions/${id}`, jsonRequest("PATCH", fields));
    const index = state.sessions.findIndex((item) => String(item.id) === String(id));
    if (index >= 0) state.sessions[index] = { ...state.sessions[index], ...payload.session };
    if (message) toast(message);
    renderSidebar();
    renderDrawer();
    await loadAll();
  } catch (error) {
    toast(`保存失败：${error && error.message ? error.message : error}`);
  }
}

async function patchRecord(id, fields, message) {
  try {
    const payload = await api(`/api/records/${id}`, jsonRequest("PATCH", fields));
    state.drawer = payload.record;
    const index = state.records.findIndex((item) => item.id === id);
    if (index >= 0) state.records[index] = payload.record;
    // v16 C1：原句改了 → 旧快照的文本 / 偏移全部作废（要在重渲染之前清，别指向新 DOM）
    if (Object.prototype.hasOwnProperty.call(fields, "context")) invalidateSelectionRead();
    if (message) toast(message);
    renderDrawer();
    const structural = ["context", "session_id"].some((key) =>
      Object.prototype.hasOwnProperty.call(fields, key)
    );
    refreshAfterChange([id], { structural });
  } catch (error) {
    toast(`保存失败：${error && error.message ? error.message : error}`);
  }
}

async function deleteRecord(id) {
  const target = state.drawer ? state.drawer.target : "";
  try {
    await api(`/api/records/${id}`, { method: "DELETE" });
    closeDrawer();
    toast(`已删除「${target}」`);
    await loadAll();
  } catch (error) {
    toast(`删除失败：${error && error.message ? error.message : error}`);
  }
}

/* --- v34 T6（票 #219）：待选句池 + 两段式挑词 ---------------------------------
 * 路由 `#/backlog`（`#/pick/<id>` 是一等别名，不改写 hash）。页面 = 左侧会话分组
 * （只列待挑 > 0）+ 右侧按会话分段的句子流（组头吸顶、按需加载）+ 吸底暂存栏。
 * 交互契约（panel-v2 + ADR-0014）：句子按时间点排序、时间可跳回（t= 秒）；拖选任意
 * 连续范围（单击 = 选单词）→ kind 气泡（1 词 / 2–5 词 / 6+ 词三档预判、可改）→
 * **确认只进暂存**（浏览器 localStorage，零请求）→「保存」才逐条 POST 既有挑词
 * 端点落库；暂存 × 只移除草稿，已保存 × 走 DELETE 删词条。
 * 同句可重叠挑词（内层底色 + 外层弧线，渲染算力在 pick-plan.js）；重开池子用
 * 词条 + 原句做区间映射重建已保存 chip；可疑句看图改句（image_path 有值才显示）。
 * -------------------------------------------------------------------------- */

const PICK_FILTERS = [
  ["all", "全部"],
  ["suspicious", "只看可疑"],
  ["unpicked", "只看未挑"],
];
const PICK_SOURCE_LABEL = { cc: "CC", ocr: "OCR", asr: "ASR" };
// v34 T6（ADR-0014 §2）：挑词暂存只落浏览器（不建草稿表）；写入 try/catch 回落内存。
const PICK_DRAFT_KEY = "workbench-pick-drafts";

let poolSeq = 0;
let poolLeaveArmed = false; // v34 T6：草稿 > 0 时 Esc/返回先提示一次
let pickDrag = null; // 进行中的拖选 { sentence, box, start, end }
let pickBubbleState = null; // 气泡当前内容 { sentence, range, kind }

/* --- v34 T6（票 #219 / ADR-0014）：挑词暂存（纯前端） -------------------------
 * 挑 = 起草：只写浏览器里的 workbench-pick-drafts，零请求、零落库；
 * 保存 = 落库：逐条 POST 既有 pick 端点。刷新 / 切会话 / 关浏览器后暂存仍在
 * （清缓存丢失属预期）。形如 { "<sessionId>": { "<sentenceId>": [ {start,end,kind,text,key} ] } }。
 * -------------------------------------------------------------------------- */

function draftKeyOf(sentenceId, start, end, kind, text) {
  return `${Number(sentenceId)}:${Number(start)}:${Number(end)}:${String(kind)}:${String(text)}`;
}

function draftsOf(sessionId, sentenceId) {
  const bucket = state.poolDrafts[String(sessionId)];
  if (!bucket) return [];
  return bucket[String(sentenceId)] || [];
}

function draftItemsOf(sessionId) {
  const bucket = state.poolDrafts[String(sessionId)] || {};
  const items = [];
  for (const [sentenceId, list] of Object.entries(bucket)) {
    for (const item of list || []) items.push({ sentenceId, item });
  }
  return items;
}

function allDraftEntries() {
  const entries = [];
  for (const sessionId of Object.keys(state.poolDrafts)) {
    for (const entry of draftItemsOf(sessionId)) entries.push({ sessionId, ...entry });
  }
  return entries;
}

function draftTotals() {
  const entries = allDraftEntries();
  return {
    items: entries.length,
    sentences: new Set(entries.map((entry) => `${entry.sessionId}:${entry.sentenceId}`)).size,
  };
}

function loadPickDrafts() {
  let saved = null;
  try {
    saved = JSON.parse(window.localStorage.getItem(PICK_DRAFT_KEY) || "null");
  } catch (error) {
    saved = null;
  }
  const drafts = {};
  if (!saved || typeof saved !== "object") return drafts;
  for (const [sessionId, bucket] of Object.entries(saved)) {
    if (!/^\d+$/.test(sessionId) || !bucket || typeof bucket !== "object") continue;
    const clean = {};
    for (const [sentenceId, list] of Object.entries(bucket)) {
      if (!/^\d+$/.test(sentenceId) || !Array.isArray(list)) continue;
      const items = [];
      for (const raw of list) {
        if (!raw || typeof raw !== "object") continue;
        const start = Number(raw.start);
        const end = Number(raw.end);
        const kind = String(raw.kind || "");
        const text = String(raw.text || "");
        if (!Number.isFinite(start) || !Number.isFinite(end) || !text) continue;
        if (!PickPlan.PICK_KIND_LABEL[kind]) continue;
        items.push({ start, end, kind, text, key: draftKeyOf(sentenceId, start, end, kind, text) });
      }
      if (items.length) clean[sentenceId] = items;
    }
    if (Object.keys(clean).length) drafts[sessionId] = clean;
  }
  return drafts;
}

function savePickDrafts() {
  try {
    window.localStorage.setItem(PICK_DRAFT_KEY, JSON.stringify(state.poolDrafts));
  } catch (error) {
    /* 隐私模式等场景下 localStorage 不可用：暂存仍在本次会话生效（ADR-0014 §2） */
  }
}

function addDraftItem(sessionId, sentenceId, item) {
  const key = String(sessionId);
  if (!state.poolDrafts[key]) state.poolDrafts[key] = {};
  const bucket = state.poolDrafts[key];
  const sid = String(sentenceId);
  if (!bucket[sid]) bucket[sid] = [];
  bucket[sid].push(item);
  savePickDrafts();
}

function removeDraftItem(sessionId, sentenceId, item) {
  const list = draftsOf(sessionId, sentenceId);
  const index = list.indexOf(item);
  if (index >= 0) list.splice(index, 1);
  const bucket = state.poolDrafts[String(sessionId)];
  const sid = String(sentenceId);
  if (bucket && bucket[sid] && !bucket[sid].length) delete bucket[sid];
  if (bucket && !Object.keys(bucket).length) delete state.poolDrafts[String(sessionId)];
  savePickDrafts();
}

function clearDraftsForSentence(sessionId, sentenceId) {
  const bucket = state.poolDrafts[String(sessionId)];
  if (!bucket || !bucket[String(sentenceId)]) return;
  delete bucket[String(sentenceId)];
  if (!Object.keys(bucket).length) delete state.poolDrafts[String(sessionId)];
  savePickDrafts();
}

function cycleDraftKind(sessionId, sentenceId, item) {
  const order = ["word", "phrase", "sentence"];
  const index = order.indexOf(item.kind);
  item.kind = order[(index + 1) % order.length];
  item.key = draftKeyOf(sentenceId, item.start, item.end, item.kind, item.text);
  savePickDrafts();
  if (state.route === "backlog") renderMain();
}

function abandonDrafts() {
  const total = draftTotals().items;
  state.poolDrafts = {};
  savePickDrafts();
  if (state.route === "backlog") renderMain();
  toast(`已放弃 ${total} 个暂存挑词，词条库未动`);
}

// 恢复时句子 id 已不存在（句子被清理 / 改句作废）→ 丢弃该句的暂存并提示一次。
function prunePoolDrafts(sessionId, sentences) {
  const bucket = state.poolDrafts[String(sessionId)];
  if (!bucket) return;
  const known = new Set((sentences || []).map((sentence) => String(sentence.id)));
  let dropped = 0;
  for (const sentenceId of Object.keys(bucket)) {
    if (known.has(sentenceId)) continue;
    dropped += (bucket[sentenceId] || []).length;
    delete bucket[sentenceId];
  }
  if (!dropped) return;
  if (!Object.keys(bucket).length) delete state.poolDrafts[String(sessionId)];
  savePickDrafts();
  toast(`有 ${dropped} 条暂存挑词对应句子已不存在，已丢弃`);
}

/* --- v35 #227（spec §池页）：底图占用、已挑完分组与手动清理 -------------------
 * 数字全来自服务端：`GET /api/backlog/storage`（总占用 + 每会话一份，加读端点）
 * 与清理端点回执；前端不自己数文件。两个入口是**明确按钮**（不是单弹窗 radio），
 * 都用两段式内联确认 —— 与侧栏 `confirmSession` 同一做法，不引入新模态。
 * -------------------------------------------------------------------------- */

// 字节 → `x.x MB`（唯一换算处：组头 / 顶栏 / 80% 提示 / toast 共用）。
function fmtMB(bytes) {
  return `${(Math.max(0, Number(bytes) || 0) / 1024 / 1024).toFixed(1)} MB`;
}

function bandSizeText(info) {
  const images = Math.max(0, Number(info && info.images) || 0);
  const bytes = Math.max(0, Number(info && info.bytes) || 0);
  return `${images} 张 / ${fmtMB(bytes)}`;
}

// storage 快照按 session_id 建一次 Map（每组头 / 80% 提示都查它，避免每渲染 O(n²)）。
function bandStorageIndex(pool) {
  if (pool && pool.bandIndex instanceof Map) return pool.bandIndex;
  const index = new Map();
  const list =
    pool && pool.storage && Array.isArray(pool.storage.sessions)
      ? pool.storage.sessions
      : [];
  for (const item of list) index.set(String(item.session_id), item);
  if (pool) pool.bandIndex = index;
  return index;
}

// 该会话的底图占用（服务端 storage 快照里查；老中枢没有端点 → 全 0，界面照旧）。
function bandStorageOf(pool, sessionId) {
  const found = bandStorageIndex(pool).get(String(sessionId));
  if (!found) {
    return {
      images: 0,
      bytes: 0,
      picked_images: 0,
      unpicked_images: 0,
      capturing: false,
    };
  }
  return {
    images: Math.max(0, Number(found.images) || 0),
    bytes: Math.max(0, Number(found.bytes) || 0),
    picked_images: Math.max(0, Number(found.picked_images) || 0),
    unpicked_images: Math.max(0, Number(found.unpicked_images) || 0),
    capturing: Boolean(found.capturing),
  };
}

// 池页顶栏的总占用；#235 的 80% 黄字提示也挂在这里（同一份数字）。
function bandPoolSummary(pool) {
  const storage = (pool && pool.storage) || {};
  const images = Math.max(0, Number(storage.images) || 0);
  const bytes = Math.max(0, Number(storage.bytes) || 0);
  return `底图 ${bandSizeText({ images, bytes })}`;
}

// 「保留」偏好：只写浏览器 localStorage（不加 schema）。被保留的视频不再出现在
// 80% 黄字提醒里，但仍可随时手动清理（spec 用户故事 8/9）。
const POOL_BAND_KEEP_KEY = "pool-band-keep:";

function poolBandKeepKey(sessionId) {
  return `${POOL_BAND_KEEP_KEY}${Number(sessionId)}`;
}

function isPoolBandKept(sessionId) {
  try {
    return window.localStorage.getItem(poolBandKeepKey(sessionId)) === "1";
  } catch (error) {
    return false; // 隐私模式：偏好不落盘，不影响清理本身
  }
}

function setPoolBandKept(sessionId, kept) {
  try {
    const key = poolBandKeepKey(sessionId);
    if (kept) window.localStorage.setItem(key, "1");
    else window.localStorage.removeItem(key);
  } catch (error) {
    /* localStorage 不可用：偏好只在本次页面生命周期失效（ADR-0014 §2 同款兜底） */
  }
}

// 80% 黄字提示：数字与 `GET /api/backlog/storage` 完全同源（bytes / limit_bytes）。
// 「保留」在这里生效 —— 相关视频都保留 → 不再黄字提醒（占用数字照常显示）。
function bandWarning(pool) {
  const storage = (pool && pool.storage) || {};
  const limit = Math.max(0, Number(storage.limit_bytes) || 0);
  const bytes = Math.max(0, Number(storage.bytes) || 0);
  if (!(limit > 0) || bytes < limit * 0.8) return null;
  const percent = Math.min(100, Math.round((bytes / limit) * 100));
  const sessions = (pool.sessions || []).concat(pool.completed || []);
  const unkept = sessions.filter(
    (session) =>
      bandStorageOf(pool, session.id).images > 0 && !isPoolBandKept(session.id)
  );
  if (!unkept.length) {
    return {
      tone: "muted",
      text: `底图已占 ${percent}%（上限 ${fmtMB(limit)}），相关视频都已保留，不再提醒。`,
    };
  }
  return {
    tone: "warn",
    text: `已占上限的 ${percent}%（上限 ${fmtMB(limit)}）—— 建议清理已挑完的视频；不想再被提醒的视频点「保留」。`,
  };
}

// 收句态来自 storage 快照的 `capturing`（服务端调度器只读派生）—— 不依赖
// `pick.sources`（只有被展开的会话才有），否则未展开会话的确认面板黄字永远不出现。
function isSessionCapturing(pool, session) {
  return bandStorageOf(pool, session.id).capturing === true;
}

// 会话组头的右侧动作区：占用文字 + 两个明确按钮（按钮门控见 spec §池页）。
// `showSize=false` 用于「已挑完」分组（组头已经写了「已挑完 · 底图 N 张 / X MB」，别重复）。
function poolSectionActions(pool, session, { showSize = true } = {}) {
  const box = el("div", "pool-section-actions");
  const info = bandStorageOf(pool, session.id);
  const pending = Math.max(0, Number(session.subtitle_pending) || 0);
  const busy = Boolean(pool.bandBusy);
  if (info.images > 0) {
    if (showSize) {
      box.append(el("span", "pool-band-size", `底图 ${bandSizeText(info)}`));
    }
    const remove = button("btn btn-subtle pool-band-del", "删除底图", () => {
      if (busy) return;
      pool.bandConfirm = { sessionId: session.id, mode: "images" };
      renderMain();
    });
    remove.title = "删掉这个视频的底图（句子与词条都保留，之后不能再「看图改句」）";
    remove.disabled = busy;
    box.append(remove);
    // v35 #235：「保留」= 别再提醒我清理；仍可随时手动清理（spec 用户故事 8/9）。
    const kept = isPoolBandKept(session.id);
    const keep = button(
      `btn btn-subtle pool-band-keep${kept ? " is-on" : ""}`,
      kept ? "已保留" : "保留",
      () => {
        if (busy) return;
        setPoolBandKept(session.id, !kept);
        renderMain();
      }
    );
    keep.title = kept
      ? "已保留：底图接近上限时不再提醒这个视频（仍可随时手动清理）"
      : "不再提醒这个视频的底图清理（仍可随时手动清理）";
    keep.disabled = busy;
    box.append(keep);
  }
  // 危险按钮：只在 pending > 0 时出现（已挑完的视频没有待选句可清）。
  if (pending > 0) {
    const clear = button("btn btn-danger pool-band-clear", "清空本视频待选句", () => {
      if (busy) return;
      pool.bandConfirm = { sessionId: session.id, mode: "all" };
      renderMain();
    });
    clear.title = "清空这个视频的待选句与底图（词条库一条不动）";
    clear.disabled = busy;
    box.append(clear);
  }
  return box;
}

// 两段式内联确认面板：第一步点按钮 → 这里列出张数/MB/句数，第二步才真删。
function poolBandConfirm(pool, session, mode) {
  const info = bandStorageOf(pool, session.id);
  const pending = Math.max(0, Number(session.subtitle_pending) || 0);
  const box = el("div", "pool-band-confirm");
  box.setAttribute("role", "alertdialog");
  box.setAttribute("aria-label", mode === "all" ? "清空本视频待选句" : "删除底图");
  if (isSessionCapturing(pool, session)) {
    box.append(
      el("div", "pool-band-warn", "该视频还在收句，清理后新的字幕仍会产生底图")
    );
  }
  box.append(
    el(
      "div",
      "pool-band-confirm-title",
      mode === "all" ? "清空本视频待选句？" : "删除这个视频的底图？"
    )
  );
  const lines = [`该会话底图 ${bandSizeText(info)}`];
  if (mode === "all") {
    lines.push(
      `本次会删 ${pending} 条待选句 + ${info.images} 张底图（词条库不动）`
    );
  } else {
    lines.push(`${pending} 句待挑保留（仍可挑词，但不能再「看图改句」）`);
  }
  lines.push("不可恢复。");
  for (const line of lines) box.append(el("div", "pool-band-confirm-line", line));
  const row = el("div", "pool-band-confirm-actions");
  const cancel = button("btn btn-subtle", "取消", () => {
    pool.bandConfirm = null;
    renderMain();
  });
  const confirm = button(
    mode === "all" ? "btn btn-danger" : "btn btn-primary",
    mode === "all" ? `确认清空 ${pending} 句` : `确认删除 ${info.images} 张`,
    () => cleanupSessionBands(session.id, mode)
  );
  cancel.disabled = Boolean(pool.bandBusy);
  confirm.disabled = Boolean(pool.bandBusy);
  row.append(cancel, confirm);
  box.append(row);
  return box;
}

// 执行清理 → toast 回执（「已释放 21.6 MB（92 张）」；all 再加删除句数）→ 重拉池子。
async function cleanupSessionBands(sessionId, mode) {
  const pool = state.pool;
  if (!pool || pool.bandBusy) return;
  pool.bandBusy = true;
  pool.bandConfirm = null;
  renderMain();
  try {
    const payload = await api(
      `/api/sessions/${Number(sessionId)}/backlog/cleanup`,
      jsonRequest("POST", { mode: mode })
    );
    const freed = fmtMB(payload.freed_bytes);
    let message = `已释放 ${freed}（${Number(payload.deleted_images) || 0} 张）`;
    if (mode === "all") {
      message += ` · 已删除 ${Number(payload.deleted_rows) || 0} 条待选句（词条未动）`;
    }
    setPoolBandKept(sessionId, false); // 清理成功 = 这个视频不再需要「保留」标记
    toast(message);
    await loadPool(state.poolId);
  } catch (error) {
    const reason = error && error.message ? error.message : String(error);
    toast(`清理底图失败：${reason}`);
    if (state.pool === pool) {
      pool.bandBusy = false;
      renderMain();
    }
    return;
  }
}

/* --- 池子数据加载 ------------------------------------------------------------ */

async function loadPool(focusId, { skeleton = false } = {}) {
  const seq = ++poolSeq;
  state.pool = {
    loading: true,
    error: "",
    sessions: [],
    completed: [],
    storage: null,
    bySession: {},
    activeId: null,
    filter: "all",
    saving: false,
    bandBusy: false,
    bandConfirm: null,
    bandIndex: null,
    seq,
  };
  if (skeleton) renderMain();
  try {
    const [payload, storagePayload] = await Promise.all([
      api("/api/sessions"),
      // v35 #227：底图占用（总 + 每会话）；老中枢没这个端点 → 不显占用，界面照旧
      api("/api/backlog/storage").catch(() => null),
    ]);
    if (seq !== poolSeq || state.route !== "backlog") return; // 旧响应不覆盖新视图
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    // 顶栏 / 池子 / 侧栏摘要共用这一份服务端真值（spec A9）
    state.sessions = sessions;
    syncPendingFromSessions(sessions, pendingCountSeq);
    const withPending = sessions.filter((session) => Number(session.subtitle_pending) > 0);
    const active =
      withPending.find(
        (session) => focusId !== null && String(session.id) === String(focusId)
      ) ||
      withPending[0] ||
      null;
    state.pool.sessions = withPending;
    state.pool.completed = sessions.filter(
      (session) => !(Number(session.subtitle_pending) > 0)
    );
    state.pool.storage =
      storagePayload && typeof storagePayload === "object" ? storagePayload : null;
    state.pool.bandIndex = null; // 新快照 → 丢弃旧的 session_id 索引
    state.pool.activeId = active ? active.id : null;
    state.pool.loading = false;
    renderBacklogNav();
    renderSidebar();
    renderFilters();
    renderMain();
    if (active) await loadPoolSession(active.id);
  } catch (error) {
    if (seq !== poolSeq || state.route !== "backlog") return;
    state.pool.loading = false;
    state.pool.error = error && error.message ? error.message : String(error);
    renderMain();
  }
}

async function loadPoolSession(id, { reload = false } = {}) {
  const pool = state.pool;
  if (!pool) return null;
  const key = String(id);
  const previous = pool.bySession[key] || null;
  if (previous && (previous.loading || (previous.loaded && !reload))) return previous;
  const seq = pool.seq;
  pool.bySession[key] = {
    ...(previous || {}),
    sessionId: Number(id),
    session: sessionById(id),
    loading: !previous || !previous.loaded,
    loaded: previous ? Boolean(previous.loaded) : false,
    error: "",
    filter: pool.filter,
  };
  if (String(pool.activeId) === key) renderMain();
  try {
    const [payload, recordsPayload, status] = await Promise.all([
      api(`/api/sessions/${Number(id)}/backlog`),
      // 已挑态重建：该会话的词条（context + target → 字符区间 → 词下标）
      api(`/api/records?session=${Number(id)}&limit=500`).catch(() => null),
      // 来源状态（调度器 cc/ocr 现状）；老中枢 404 → null 不出徽标
      api(`/api/sessions/${Number(id)}/backlog/status`).catch(() => null),
    ]);
    if (state.pool !== pool || seq !== poolSeq) return null; // 旧响应不覆盖新视图
    const sentences = buildPoolSentences(Number(id), payload.sentences, recordsPayload, previous);
    prunePoolDrafts(key, sentences);
    const session = sessionById(id);
    const serverRecords = Number.isFinite(Number(payload.records))
      ? Number(payload.records)
      : null;
    pool.bySession[key] = {
      sessionId: Number(id),
      session,
      sentences,
      serverRecords,
      pending: Number(payload.pending) || 0,
      sources:
        status && status.ok
          ? {
              enabled: Boolean(status.enabled),
              cc: String(status.cc || "off"),
              ocr: String(status.ocr || "off"),
            }
          : null,
      loading: false,
      loaded: true,
      error: "",
      filter: pool.filter,
    };
    // 计数口径统一：会话行的「N 词」与池子统计都吃服务端 records（spec A9）
    if (session && serverRecords !== null) session.records = serverRecords;
    if (payload.pending !== undefined) applyBacklogPending(id, payload.pending);
    renderBacklogNav();
    renderSidebar();
    renderFilters();
    if (String(pool.activeId) === key) renderMain();
    return pool.bySession[key];
  } catch (error) {
    if (state.pool !== pool) return null;
    pool.bySession[key] = {
      ...(previous || {}),
      sessionId: Number(id),
      session: sessionById(id),
      loading: false,
      loaded: false,
      error: error && error.message ? error.message : String(error),
    };
    if (String(pool.activeId) === key) renderMain();
    toast(`打不开会话 #${id} 的待选句：${pool.bySession[key].error}`);
    return null;
  }
}

// 已保存态重建（spec A8）：词条与原句做文本区间映射，重开池子仍看得到挑过哪些词。
// 本次会话刚保存的 chip（有 record_id）优先保留精确词下标；records 快照稍后落地也不丢。
function buildPoolSentences(sessionId, rawSentences, recordsPayload, previous) {
  const sentences = (rawSentences || []).map((sentence) => ({
    ...sentence,
    sessionId: Number(sessionId),
    picks: [],
    picked: Boolean(sentence.picked_at), // 服务端已挑标记持久（重开页面一致）
  }));
  const byText = new Map();
  for (const sentence of sentences) {
    const text = String(sentence.text || "").trim();
    if (!byText.has(text)) byText.set(text, sentence);
  }
  const localById = new Map();
  for (const old of (previous && previous.sentences) || []) {
    for (const pick of old.picks || []) {
      if (pick && pick.recordId) localById.set(String(pick.recordId), { sentenceId: old.id, pick });
    }
  }
  const seen = new Set();
  const records =
    recordsPayload && Array.isArray(recordsPayload.records) ? recordsPayload.records : [];
  for (const record of records) {
    const sentence = byText.get(String(record.context || "").trim());
    if (!sentence) continue;
    const span = findTargetSpan(sentence.text, record.target);
    if (!span) continue;
    const range = PickPlan.charSpanToWordRange(sentence.text, span.start, span.end);
    if (!range) continue;
    const local = localById.get(String(record.id));
    const item = local
      ? { ...local.pick }
      : {
          start: range.start,
          end: range.end,
          kind: String(record.kind || "word"),
          text: String(record.target || ""),
          recordId: record.id,
          meaning: String(record.meaning || ""),
          meaningSource: String(record.meaning_source || ""),
          fill: record.meaning ? "filled" : "",
        };
    sentence.picks.push(item);
    seen.add(String(record.id));
  }
  for (const [recordId, entry] of localById) {
    if (seen.has(recordId)) continue;
    const sentence = sentences.find((item) => String(item.id) === String(entry.sentenceId));
    if (sentence) sentence.picks.push({ ...entry.pick });
  }
  for (const sentence of sentences) {
    sentence.picks.sort((one, two) => one.start - two.start || one.end - two.end);
  }
  return sentences;
}

// 保存 / 撤销后重拉会话清单：组头计数、只列待挑 > 0 的会话、顶栏总数一起刷新。
async function refreshPoolSessions() {
  if (!state.pool || state.route !== "backlog") return;
  try {
    const payload = await api("/api/sessions");
    if (!state.pool || state.route !== "backlog") return;
    const sessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    state.sessions = sessions;
    syncPendingFromSessions(sessions, pendingCountSeq);
    state.pool.sessions = sessions.filter((session) => Number(session.subtitle_pending) > 0);
    if (
      !state.pool.sessions.some(
        (session) => String(session.id) === String(state.pool.activeId)
      )
    ) {
      state.pool.activeId = state.pool.sessions.length ? state.pool.sessions[0].id : null;
    }
    renderBacklogNav();
    renderMain();
    if (state.pool.activeId !== null) loadPoolSession(state.pool.activeId);
  } catch (error) {
    /* 清单刷新失败不打断保存结果：下一轮进入页面会再拉 */
  }
}

function activatePoolSession(id) {
  const pool = state.pool;
  if (!pool || String(pool.activeId) === String(id)) return;
  pool.activeId = Number(id);
  renderMain();
  loadPoolSession(id);
}

function leavePool() {
  const total = draftTotals().items;
  if (total > 0 && !poolLeaveArmed) {
    poolLeaveArmed = true;
    toast(`还有 ${total} 个挑词在暂存栏（再按一次 Esc / 返回即离开，暂存会保留）`);
    return;
  }
  poolLeaveArmed = false;
  window.location.hash = "#/list";
}

function renderPool(wrap) {
  const pool = state.pool;
  if (!pool) return;
  if (pool.loading) {
    wrap.append(el("div", "pick-loading hint", "正在载入待选句…"));
    return;
  }
  if (pool.error) {
    const box = el("div", "card placeholder-card");
    box.append(el("div", "placeholder-count", "待选句"));
    box.append(el("div", "empty-title", "打不开待选句池"));
    box.append(el("p", "hint", `连不上数据中枢：${pool.error}`));
    box.append(
      button("btn btn-primary", "回到列表", () => {
        window.location.hash = "#/list";
      })
    );
    wrap.append(box);
    return;
  }
  hidePickBubble(); // 主区重建 = 上下文变化，开着的气泡不跨渲染存活
  wrap.append(poolHead(pool));
  if (!pool.sessions.length) {
    wrap.append(
      el("div", "pool-empty hint", "没有待挑的句子 —— 看视频时收进来的句子会出现在这里。")
    );
    return;
  }
  const body = el("div", "pool-body");
  body.append(poolRail(pool));
  body.append(poolFlow(pool));
  wrap.append(body);
  const bar = poolDraftbar(pool);
  if (bar) wrap.append(bar);
}

function poolHead(pool) {
  const head = el("div", "pool-head");
  const main = el("div", "pool-head-main");
  main.append(el("h1", "pool-title", "待选句"));
  let total = 0;
  for (const session of pool.sessions) {
    total += Math.max(0, Number(session.subtitle_pending) || 0);
  }
  // v35 #227：顶栏带底图总占用（服务端口径）；#235：到上限 80% 变黄字并提示清理。
  const warning = bandWarning(pool);
  const meta = el(
    "div",
    "pool-meta",
    `${pool.sessions.length} 个会话 · ${total} 句待挑 · ${bandPoolSummary(pool)}`
  );
  if (warning && warning.tone === "warn") meta.classList.add("is-band-warn");
  main.append(meta);
  if (warning) {
    main.append(
      el(
        "div",
        `pool-band-warn-line${warning.tone === "warn" ? " is-warn" : ""}`,
        warning.text
      )
    );
  }
  head.append(main);
  head.append(
    el(
      "div",
      "pool-sub",
      "在句子里拖选词 / 词组 / 短句 → 确认先进底部暂存栏，点「保存」才进词条库（可重叠）。"
    )
  );
  const back = button("btn pool-back", "← 返回列表", () => leavePool());
  back.title = "回列表（Esc 同款；有未保存挑词时先提示一次）";
  back.setAttribute("aria-label", "返回列表");
  head.append(back);
  return head;
}

function poolRail(pool) {
  const rail = el("aside", "pool-rail");
  rail.setAttribute("aria-label", "按会话分组");
  for (const session of pool.sessions) {
    if (!(Number(session.subtitle_pending) > 0)) continue;
    const active = String(pool.activeId) === String(session.id);
    const row = button(`pool-group${active ? " is-on" : ""}`, "");
    row.dataset.sessionId = String(session.id);
    row.title = session.title || "";
    row.append(el("span", "pool-group-title", session.title || `会话 #${session.id}`));
    row.append(
      el(
        "span",
        "pool-group-count",
        `待挑 ${Math.max(0, Number(session.subtitle_pending) || 0)}`
      )
    );
    row.addEventListener("click", () => activatePoolSession(session.id));
    rail.append(row);
  }
  return rail;
}

function poolFlow(pool) {
  const flow = el("section", "pool-flow");
  flow.setAttribute("aria-label", "句子流");
  for (const session of pool.sessions) {
    if (!(Number(session.subtitle_pending) > 0)) continue; // 只列待挑 > 0 的会话
    const key = String(session.id);
    const active = String(pool.activeId) === key;
    const section = el("div", `pool-section${active ? " is-active" : ""}`);
    section.dataset.sessionId = key;
    section.append(poolSectionHead(pool, session, active));
    if (!active) {
      flow.append(section);
      continue;
    }
    const pick = pool.bySession[key];
    if (!pick || (pick.loading && !pick.loaded)) {
      section.append(el("div", "pick-loading hint", "正在载入这一组的待选句…"));
      flow.append(section);
      continue;
    }
    if (pick.error) {
      section.append(el("div", "pick-empty hint", `这一组打不开：${pick.error}`));
      flow.append(section);
      continue;
    }
    pick.filter = pool.filter; // 三档筛选与顶部 tabs 同一份状态
    section.append(pickToolbar(pick));
    // v33 修复轮（spec §C11 第二档的落点）：字幕带区域失效 → 显著提示请用户重拖
    if (pick.sources && pick.sources.ocr === "redrag_needed") {
      section.append(el("div", "pick-redrag-banner", "字幕带区域失效，请按热键重拖"));
    }
    const list = el("div", "pick-list");
    const view = PickPlan.filterSentences(pick.sentences, pick.filter);
    if (!view.length) {
      list.append(
        el(
          "div",
          "pick-empty hint",
          pick.filter === "suspicious"
            ? "这一档没有可疑句。"
            : pick.filter === "unpicked"
              ? "都挑完了 —— 换「全部」看看已挑标记。"
              : "这个会话还没有待选句。"
        )
      );
    }
    for (const sentence of view) list.append(pickSentenceRow(pick, sentence));
    section.append(list);
    flow.append(section);
  }
  // v35 #227：已挑完但有底图的视频也有清理入口（否则挑完就从池页消失、入口无处可放）
  const completed = poolCompletedSections(pool);
  if (completed) flow.append(completed);
  return flow;
}

function poolSectionHead(pool, session, active) {
  // 组头 = 可点击的展开按钮 + 右侧「占用 + 两个按钮」+（可选）两段式确认面板。
  // 拆成 wrapper 是因为按钮里不能再嵌按钮（HTML 不允许），点「删除底图」不能当展开。
  const wrap = el("div", "pool-section-head-wrap");
  const head = button("pool-section-head", "");
  head.dataset.sessionId = String(session.id);
  head.setAttribute("aria-expanded", active ? "true" : "false");
  head.append(el("span", "pool-section-title", session.title || `会话 #${session.id}`));
  head.append(
    el(
      "span",
      "pool-section-count",
      `待挑 ${Math.max(0, Number(session.subtitle_pending) || 0)}`
    )
  );
  const pick = pool.bySession[String(session.id)];
  if (pick && Number.isFinite(pick.serverRecords)) {
    head.append(el("span", "pool-section-meta", `已挑 ${pick.serverRecords} 词条`));
  }
  head.append(el("span", "pool-section-caret", active ? "收起" : "展开"));
  head.addEventListener("click", () => activatePoolSession(session.id));
  wrap.append(head);
  wrap.append(poolSectionActions(pool, session));
  const confirm = pool.bandConfirm;
  if (confirm && String(confirm.sessionId) === String(session.id)) {
    wrap.append(poolBandConfirm(pool, session, confirm.mode));
  }
  return wrap;
}

// v35 #227：`pending == 0` 但仍有底图的会话 —— 只在有图时出现（spec §池页）。
function poolCompletedSections(pool) {
  const rows = [];
  for (const session of pool.completed || []) {
    if (Number(session.subtitle_pending) > 0) continue; // 还有待挑 = 不是已挑完
    const info = bandStorageOf(pool, session.id);
    if (!(info.images > 0)) continue;
    rows.push({ session, info });
  }
  if (!rows.length) return null;
  const box = el("section", "pool-completed");
  box.setAttribute("aria-label", "已挑完的视频");
  box.append(el("h2", "pool-completed-title", "已挑完的视频"));
  box.append(
    el(
      "div",
      "pool-completed-sub hint",
      "这些视频已经挑完，但底图还占着磁盘 —— 可以一键清理（句子与词条都保留）。"
    )
  );
  for (const { session, info } of rows) {
    const row = el("div", "pool-completed-row");
    row.dataset.sessionId = String(session.id);
    const head = el("div", "pool-completed-head");
    head.append(el("span", "pool-section-title", session.title || `会话 #${session.id}`));
    head.append(
      el("span", "pool-section-meta", `已挑完 · 底图 ${bandSizeText(info)}`)
    );
    row.append(head);
    row.append(poolSectionActions(pool, session, { showSize: false }));
    const confirm = pool.bandConfirm;
    if (confirm && String(confirm.sessionId) === String(session.id)) {
      row.append(poolBandConfirm(pool, session, confirm.mode));
    }
    box.append(row);
  }
  return box;
}

function poolDraftbar(pool) {
  const totals = draftTotals();
  if (!totals.items) return null;
  const bar = el("div", "pool-draftbar");
  bar.setAttribute("role", "status");
  bar.setAttribute("aria-live", "polite");
  bar.append(
    el("span", "pool-draft-count", `${totals.items} 个待保存 · 涉及 ${totals.sentences} 句`)
  );
  bar.append(el("span", "pool-draft-hint", "保存前不会进词条库"));
  const discard = button("btn btn-subtle", "放弃", () => abandonDrafts());
  const save = button(
    "btn btn-primary",
    pool.saving ? "保存中…" : `保存 ${totals.items} 个`,
    () => saveAllDrafts()
  );
  discard.disabled = Boolean(pool.saving);
  save.disabled = Boolean(pool.saving);
  bar.append(discard, save);
  return bar;
}

function pickToolbar(pick) {
  const bar = el("div", "pick-toolbar");
  bar.append(el("span", "pick-stat", pickStatText(pick)));
  bar.append(pickSourceBadges(pick)); // v33 修复轮（spec §B2c）：来源状态徽标
  const tabs = el("div", "pick-tabs");
  for (const [key, label] of PICK_FILTERS) {
    const tab = button(
      `pick-tab${pick.filter === key ? " is-on" : ""}`,
      label,
      () => {
        if (!state.pool || state.pool.filter === key) return;
        state.pool.filter = key;
        renderMain();
      }
    );
    tab.setAttribute("aria-pressed", pick.filter === key ? "true" : "false");
    tabs.append(tab);
  }
  bar.append(tabs);
  return bar;
}

function pickStatText(pick) {
  const summary = PickPlan.pickSummary(pick.sentences);
  // 计数口径统一（v34 T6 / spec A9）：已挑 = 服务端 records、待挑 = 服务端 pending；
  // 拿不到（老中枢）才回落本地 picks 计数（保存前本地 picks 为空，不会虚报）。
  const picked = Number.isFinite(pick.serverRecords) ? pick.serverRecords : summary.picked;
  const pending = Number.isFinite(pick.pending) ? pick.pending : summary.pending;
  const text = `已挑 ${picked} 词条 · 待挑 ${pending} 句 · 共 ${summary.total} 句`;
  // v33 T5（票 #211 / spec §B2c）：统计条按来源展示句数（来源徽标的补充口径）；
  // 有句子的来源才出现（CC 12 · OCR 5 这样排）。
  const sources = PickPlan.sourceSummary(pick.sentences);
  const sourceText = ["cc", "ocr", "asr"]
    .filter((key) => sources[key] > 0)
    .map((key) => `${PICK_SOURCE_LABEL[key]} ${sources[key]}`)
    .join(" · ");
  return sourceText ? `${text} · ${sourceText}` : text;
}

// v33 修复轮（spec §B2c / §C11）：来源状态徽标 —— 调度器该会话的 cc/ocr 现状。
// 老中枢没有 status 端点（sources=null）→ 整组不出现，界面照旧。
const PICK_SOURCE_STATE_LABEL = Object.freeze({
  cc: Object.freeze({
    ok: "CC 英文轨",
    no_english_track: "CC 无英文轨",
    error: "CC 故障",
    off: "CC 未在跑",
  }),
  ocr: Object.freeze({
    active: "OCR 采集中",
    idle: "OCR 暂停",
    dormant: "OCR 待拖字幕带",
    redrag_needed: "OCR 区域失效",
    failed: "OCR 不可用",
    off: "OCR 未在跑",
  }),
});

function pickSourceBadges(pick) {
  const box = el("div", "pick-sources");
  const sources = pick.sources;
  if (!sources) return box;
  for (const key of ["cc", "ocr"]) {
    const value = String(sources[key] || "");
    const label = (PICK_SOURCE_STATE_LABEL[key] || {})[value];
    if (!label) continue;
    box.append(el("span", `pick-source-badge is-${key} is-${value}`, label));
  }
  return box;
}

function pickWordsOf(sentence) {
  return String(sentence.text || "").split(/\s+/).filter(Boolean);
}

function pickSentenceRow(pick, sentence) {
  const row = el("div", "pick-sent");
  row.dataset.sessionId = String(pick.sessionId);
  row.dataset.sentenceId = String(sentence.id);
  const drafts = draftsOf(pick.sessionId, sentence.id);
  if (sentence.suspicious) row.classList.add("is-warn");
  // 已挑 = 已保存（暂存不算：待挑数要到保存才减，ADR-0014 §6）
  if ((sentence.picks || []).length || sentence.picked) row.classList.add("is-picked");

  row.append(pickTimeLink(pick.session, sentence.from_s));
  row.append(el("span", "pick-dot"));

  const body = el("div", "pick-body");
  const top = el("div", "pick-row-top");
  const srcLabel =
    PICK_SOURCE_LABEL[sentence.source] ||
    String(sentence.source || "").toUpperCase();
  if (srcLabel) {
    const src = el(
      "span",
      `pick-src${sentence.source === "cc" ? " is-cc" : ""}`,
      srcLabel
    );
    top.append(src);
  }
  if (sentence.suspicious) {
    top.append(
      el(
        "span",
        "pick-warn-tag",
        sentence.score !== null && sentence.score !== undefined
          ? `⚠ 可疑 · 置信度 ${sentence.score}`
          : "⚠ 可疑"
      )
    );
  }
  for (const item of sentence.picks || []) top.append(pickChip(pick, sentence, item));
  for (const item of drafts) top.append(draftChip(pick, sentence, item));
  if (!(sentence.picks || []).length && !drafts.length) {
    // T4：服务端已挑标记（重开页面后本 session 的词条列表拿不到）→ 标「已挑」
    top.append(
      el("span", sentence.picked ? "pick-picked-tag" : "pick-none-yet",
         sentence.picked ? "已挑" : "未挑")
    );
  }
  body.append(top);
  body.append(pickSentenceText(pick, sentence));
  // C3：只要有底图就开「看图 / 改句」——错句不一定被标可疑（#222）。
  // v35 #227：服务端已对账「文件还在不在」（image_ok）；缺图不显示入口，而不是点了报错。
  // D7：OCR 来源句缺底图不再静默隐藏 —— 明确提示一行（CC 句本来无图，不算错误）。
  if (sentence.image_path && sentence.image_ok !== false) {
    body.append(pickImgBox(pick, sentence));
    body.append(pickImgToggle(body));
  } else if (sentence.image_path && sentence.source && sentence.source !== "cc") {
    body.append(pickImgMissingNote(sentence));
  }
  row.append(body);
  return row;
}

// D7：OCR 来源句的底图缺失提示（旧目录存量图重启后由服务端兜底加载；真丢了提示用户）。
function pickImgMissingNote(sentence) {
  const imagePath = String(sentence.image_path || "");
  const note = el("div", "pick-imgbox-note", "底图缺失（旧目录文件，重启后自动兜底加载；若仍缺失可重新识别）");
  note.title = imagePath;
  return note;
}

// 时间列：`mm:ss` → 新标签打开原视频并跳到 t= 秒（spec §D：时间可跳回）
function pickTimeLink(session, seconds) {
  const href = jumpUrl(session && session.url, seconds);
  const safe = hostOf(href);
  const label = formatClock(seconds);
  const node = el(safe ? "a" : "span", "pick-t", label);
  if (safe) {
    node.href = href;
    node.target = "_blank";
    node.rel = "noopener";
    node.title = `回到原视频 ${label}`;
    node.setAttribute("aria-label", `在新标签打开原视频（跳到 ${label}）`);
  } else {
    node.title = String((session && session.url) || "");
  }
  return node;
}

function pickSentenceText(pick, sentence) {
  const words = pickWordsOf(sentence);
  const box = el("div", "pick-text");
  box.setAttribute("aria-label", "原句（拖选挑词）");
  // 已保存与暂存两态都铺底色（暂存也属于「已经挑了，只是还没落库」）
  const styles = PickPlan.computePickStyles(
    sentence.picks.concat(draftsOf(pick.sessionId, sentence.id)), // 已保存 + 暂存都铺底色
    words.length
  );
  words.forEach((word, index) => {
    const node = el("span", "pick-w", word);
    const style = styles[index] || {};
    if (style.bg) node.classList.add(`is-${style.bg}`);
    if (style.line) node.classList.add(`is-under-${style.line}`);
    node.dataset.wi = String(index);
    box.append(node);
    if (index < words.length - 1) box.append(document.createTextNode(" "));
  });
  bindPickDrag(box, sentence);
  return box;
}

// 拖选（v33 修复轮）：优先让浏览器原生文本选择生效（鼠标可拖过词间空格、双击选词），
// mouseup 时把原生选区吸附到整词；mousedown/mouseenter 的 pickDrag 只作为兜底 ——
// 单击（原生选区塌陷）与合成鼠标事件（E2E）没有原生选区，仍按起止词出气泡。
// 全局监听只绑一次（委托到 pickDrag），渲染重建不积压监听器。
function bindPickDrag(box, sentence) {
  bindPickGlobalHandlers();
  const words = Array.from(box.querySelectorAll(".pick-w"));
  // 按下落在词上才记起点；不 preventDefault —— 原生选择要能从词中间 / 词间空格开始。
  box.addEventListener("mousedown", (event) => {
    if (event.button !== 0) return;
    const target = event.target instanceof Element ? event.target.closest(".pick-w") : null;
    if (!target || !box.contains(target)) return;
    pickDrag = {
      sentence,
      box,
      start: Number(target.dataset.wi),
      end: Number(target.dataset.wi),
    };
    clearPickSelectionPaint(); // 新一轮按下 = 旧高亮作废（气泡仍留到 mouseup 重定位）
    paintPickDrag();
  });
  words.forEach((node, index) => {
    node.addEventListener("mouseenter", () => {
      if (!pickDrag || pickDrag.box !== box) return;
      pickDrag.end = index;
      paintPickDrag();
    });
  });
}

function pickSentenceOfBox(box) {
  if (!box || !state.pool) return null;
  const row = box.closest(".pick-sent");
  const sentenceId = row && row.dataset ? row.dataset.sentenceId : "";
  const sessionId = row && row.dataset ? row.dataset.sessionId : "";
  const pick = state.pool.bySession[String(sessionId)];
  return (
    ((pick && pick.sentences) || []).find(
      (sentence) => String(sentence.id) === String(sentenceId)
    ) || null
  );
}

// 原生选区 → {sentence, box, range}：
// - 空 / 塌陷 / 纯空白 → null（交给 pickDrag 兜底）；
// - 碰到不止一个 .pick-text → crossed（document 级判断，mouseup 落在第二句也能抓到）；
// - 只碰一句 → 遍历该句 .pick-w，用 range.intersectsNode 求 min/max（两端整词吸附）。
function pickNativeSelection() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || !selection.rangeCount) return null;
  const range = selection.getRangeAt(0);
  if (!String(range.toString()).trim()) return null;
  const boxes = Array.from(document.querySelectorAll(".pick-text")).filter((box) => {
    try {
      return range.intersectsNode(box);
    } catch (error) {
      return false;
    }
  });
  if (!boxes.length) return null;
  if (boxes.length > 1) return { crossed: true };
  const box = boxes[0];
  const sentence = pickSentenceOfBox(box);
  if (!sentence) return null;
  let start = -1;
  let end = -1;
  Array.from(box.querySelectorAll(".pick-w")).forEach((node, index) => {
    if (!range.intersectsNode(node)) return;
    if (start < 0) start = index;
    end = index;
  });
  if (start < 0) return null;
  return { sentence, box, range: { start, end } };
}

function clearNativeSelection() {
  const selection = window.getSelection();
  if (selection && !selection.isCollapsed) selection.removeAllRanges();
}

function bindPickGlobalHandlers() {
  if (bindPickGlobalHandlers.done) return;
  bindPickGlobalHandlers.done = true;
  document.addEventListener("mouseup", (event) => {
    if (state.route !== "backlog") {
      pickDrag = null;
      return;
    }
    const drag = pickDrag;
    pickDrag = null;
    const native = pickNativeSelection();
    if (native && native.crossed) {
      // 跨句：mouseup 可能落在第二句，必须 document 级兜底；只提示、不出气泡
      clearPickSelectionPaint();
      clearNativeSelection();
      toast("一次只能挑同一句里的内容");
      return;
    }
    if (native) {
      clearPickSelectionPaint();
      clearNativeSelection(); // 原生蓝选让位给 .is-sel（避免两套高亮打架）
      openPickBubble(native.range, native.sentence, event, native.box);
      return;
    }
    if (!drag) return;
    const range = PickPlan.normalizeRange(drag.start, drag.end);
    clearPickSelectionPaint();
    openPickBubble(range, drag.sentence, event, drag.box);
  });
  document.addEventListener("mousedown", (event) => {
    const bubble = document.querySelector(".pick-bubble");
    if (!bubble || !bubble.classList.contains("is-open")) return;
    if (bubble.contains(event.target)) return; // 点气泡内部（含输入框）不关也不新建选区
    // 点到词上 = 开始新拖选，气泡留着等 mouseup 重新定位（panel-v2 同款）
    const target = event.target instanceof Element ? event.target : null;
    if (target && target.closest(".pick-w")) return;
    hidePickBubble();
  });
}

function paintPickDrag() {
  if (!pickDrag) return;
  const range = PickPlan.normalizeRange(pickDrag.start, pickDrag.end);
  Array.from(pickDrag.box.querySelectorAll(".pick-w")).forEach((node, index) => {
    node.classList.toggle("is-sel", index >= range.start && index <= range.end);
  });
}

function clearPickSelectionPaint() {
  document.querySelectorAll(".pick-w.is-sel").forEach((node) => {
    node.classList.remove("is-sel");
  });
}

function ensurePickBubble() {
  let bubble = document.querySelector(".pick-bubble");
  if (bubble) return bubble;
  bubble = el("div", "pick-bubble");
  bubble.setAttribute("role", "dialog");
  bubble.setAttribute("aria-label", "挑词");
  bubble.append(el("div", "pick-bubble-text"));
  // v33 修复轮（spec §D19 纠错第三层）：目标词本身识别错时直接手打 —— 填了就用
  // 打的词生成词条，不填走拖选文本。
  const typed = document.createElement("input");
  typed.type = "text";
  typed.className = "pick-bubble-input";
  typed.placeholder = "识别错了？直接打";
  typed.setAttribute("aria-label", "手打目标词（覆盖拖选文本）");
  bubble.append(typed);
  const kinds = el("div", "pick-kinds");
  for (const [key, label] of Object.entries(PickPlan.PICK_KIND_LABEL)) {
    const node = el("span", "pick-kind", label);
    node.dataset.kind = key;
    node.addEventListener("click", () => {
      if (!pickBubbleState) return;
      pickBubbleState.kind = key; // 气泡可改：只改待确认状态，不落库
      bubble.querySelectorAll(".pick-kind").forEach((other) => {
        other.classList.toggle("is-on", other === node);
      });
    });
    kinds.append(node);
  }
  bubble.append(kinds);
  const btns = el("div", "pick-bubble-btns");
  btns.append(button("btn", "取消", hidePickBubble));
  // v34 T6：确认 = 起草，只进底部暂存栏（保存才落库，ADR-0014）
  btns.append(button("btn btn-primary", "✓ 挑它", stagePick));
  bubble.append(btns);
  document.body.append(bubble);
  return bubble;
}

function openPickBubble(range, sentence, event, box = null) {
  if (!state.pool) return;
  const bubble = ensurePickBubble();
  const words = pickWordsOf(sentence);
  // 整句拖选（多于一个词）自动判「短句」；单击单词仍按 1 词 → 词。
  const wholeSentence =
    words.length > 1 && range.start === 0 && range.end === words.length - 1;
  const kind = wholeSentence
    ? "sentence"
    : PickPlan.predictKind(range.end - range.start + 1);
  pickBubbleState = { sentence, range, kind };
  clearPickSelectionPaint();
  if (box) {
    Array.from(box.querySelectorAll(".pick-w")).forEach((node, index) => {
      node.classList.toggle("is-sel", index >= range.start && index <= range.end);
    });
  }
  bubble.querySelector(".pick-bubble-text").textContent = PickPlan.textOfRange(
    words,
    range.start,
    range.end
  );
  const typed = bubble.querySelector(".pick-bubble-input");
  if (typed) typed.value = ""; // 每次打开都清空：手打只覆盖当次确认
  bubble.querySelectorAll(".pick-kind").forEach((node) => {
    node.classList.toggle("is-on", node.dataset.kind === kind);
  });
  // 定位：指针右下 14px，贴视口边夹住（panel-v2 同款）
  const x = event && Number.isFinite(event.clientX) ? event.clientX : 100;
  const y = event && Number.isFinite(event.clientY) ? event.clientY : 100;
  bubble.style.left = `${Math.max(8, Math.min(x, window.innerWidth - 368))}px`;
  bubble.style.top = `${Math.max(8, Math.min(y + 14, window.innerHeight - 190))}px`;
  bubble.classList.add("is-open");
}

function hidePickBubble() {
  pickBubbleState = null;
  const bubble = document.querySelector(".pick-bubble");
  if (bubble) bubble.classList.remove("is-open");
  // v33 修复轮：气泡关闭（取消 / 确认 / 点空白 / 重建）时统一清掉拖选高亮，
  // 不留一串 .is-sel 在页面上。
  clearPickSelectionPaint();
}

function stagePick() {
  const active = pickBubbleState;
  const pool = state.pool;
  if (!active || !pool) return;
  const { sentence, range } = active;
  const kind = active.kind;
  const sessionId = Number(sentence.sessionId);
  // v33 修复轮（spec §D19）：气泡里手打了词就用打的（OCR 读错的词直接纠），不填走拖选文本
  const bubble = document.querySelector(".pick-bubble");
  const typed = bubble
    ? String((bubble.querySelector(".pick-bubble-input") || {}).value || "").trim()
    : "";
  const text = typed || PickPlan.textOfRange(pickWordsOf(sentence), range.start, range.end);
  hidePickBubble();
  if (!text || !Number.isFinite(sessionId)) return;
  const item = {
    start: range.start,
    end: range.end,
    kind,
    text,
    key: draftKeyOf(sentence.id, range.start, range.end, kind, text),
  };
  // 暂存按 (sentence_id, start, end, kind, text) 去重：同一轮误点两次不许变成两次 hits+1
  const list = draftsOf(sessionId, sentence.id);
  if (
    list.some(
      (draft) =>
        draft.start === item.start &&
        draft.end === item.end &&
        draft.kind === item.kind &&
        draft.text === item.text
    )
  ) {
    toast("这段已经挑过（在暂存栏里）");
    return;
  }
  addDraftItem(sessionId, sentence.id, item);
  if (state.route === "backlog") renderMain();
  toast("已放进暂存栏，点「保存」才进词条库");
}

// 保存 = 唯一的写入口（ADR-0014 §1/§7）：按会话、按句子时间顺序逐条 POST 既有挑词
// 端点；成功从暂存移除并转已保存 chip（记住 record_id），失败保留可重试并如实汇总。
async function saveAllDrafts() {
  const pool = state.pool;
  if (!pool || pool.saving) return;
  const sessionIds = Object.keys(state.poolDrafts);
  if (!sessionIds.length) return;
  pool.saving = true;
  renderMain();
  let saved = 0;
  let total = 0;
  const failed = [];
  for (const rawSessionId of sessionIds) {
    const sessionId = Number(rawSessionId);
    if (!Number.isFinite(sessionId)) continue;
    let pick = state.pool && state.pool.bySession[String(sessionId)];
    if (!pick || !pick.loaded) {
      pick = await loadPoolSession(sessionId, { reload: true });
    }
    const order = new Map();
    for (const sentence of (pick && pick.sentences) || []) {
      order.set(String(sentence.id), Number(sentence.from_s) || 0);
    }
    const entries = draftItemsOf(sessionId).sort((one, two) => {
      const a = order.has(String(one.sentenceId)) ? order.get(String(one.sentenceId)) : 0;
      const b = order.has(String(two.sentenceId)) ? order.get(String(two.sentenceId)) : 0;
      return a - b || one.item.start - two.item.start;
    });
    for (const entry of entries) {
      total += 1;
      try {
        const payload = await pickBacklogSentence(
          sessionId,
          entry.sentenceId,
          entry.item.text,
          entry.item.kind
        );
        const recordId = payload && payload.record_id ? Number(payload.record_id) : 0;
        const sentence = ((pick && pick.sentences) || []).find(
          (item) => String(item.id) === String(entry.sentenceId)
        );
        if (sentence) {
          sentence.picked = true;
          sentence.picks.push({
            start: entry.item.start,
            end: entry.item.end,
            kind: entry.item.kind,
            text: entry.item.text,
            recordId,
            meaning: String(payload.meaning || ""),
            fill: String(payload.fill || ""),
          });
          // v34 T1（spec D30）：worker 异步补释义，800ms 后按 record id 定向刷新
          refreshPickMeaning(sessionId, sentence.id, recordId);
        }
        removeDraftItem(sessionId, entry.sentenceId, entry.item);
        saved += 1;
      } catch (error) {
        failed.push(entry.item.text);
      }
    }
    // 保存后重拉 count 与该会话 backlog/records（计数与已挑态都以服务端为准）
    await refreshPickCounts(sessionId);
    await loadPoolSession(sessionId, { reload: true });
  }
  if (state.pool) state.pool.saving = false;
  const remaining = draftTotals().items;
  if (failed.length) {
    const names = failed.slice(0, 3).join("、");
    toast(
      `已保存 ${saved}/${total}，剩余 ${remaining} 个仍在暂存（失败：${names}${failed.length > 3 ? " 等" : ""}）`
    );
  } else {
    toast(`已保存 ${saved} 个挑词，已进该会话词条库`);
  }
  await refreshPoolSessions(); // 组头计数 / 顶栏总数 / 只剩待挑 > 0 的会话
  if (state.route === "backlog") renderMain();
}

// v33 修复轮（spec §E22）：「已挑 N 词条」以服务端为准 —— 挑/撤成功后重拉 count
// 端点（pending + records 双真值）。本地 picks 只做未确认前的即时显示。
async function refreshPickCounts(sessionId) {
  try {
    const payload = await api(`/api/sessions/${Number(sessionId)}/backlog/count`);
    const pick =
      state.pool && state.pool.bySession
        ? state.pool.bySession[String(sessionId)]
        : null;
    if (pick && Number.isFinite(payload.records)) {
      pick.serverRecords = Number(payload.records);
    }
    if (payload.pending !== undefined) {
      applyBacklogPending(sessionId, payload.pending);
      // 计数口径统一（spec A9）：池子统计与会话行摘要都吃服务端 records
      const session = sessionById(sessionId);
      if (session && Number.isFinite(payload.records)) {
        session.records = Number(payload.records);
      }
      if (pick) pick.pending = Math.max(0, Number(payload.pending) || 0);
      renderSidebar();
      renderFilters();
    }
    renderBacklogNav();
    if (state.route === "backlog") renderMain();
  } catch (error) {
    /* 计数刷新失败不回滚已成功的动作（下一轮操作会再拉） */
  }
}

// v34 T1（票 #214 / spec D30）：补释义 worker 是异步的（缓存毫秒级 / 在线机翻
// 预算可达 2.5s）。挑词回包只能保证「已入队」——按 800ms/2s/4s/8s 重试几次，
// 命中即停；只替换这一句的行，不整页重画，筛选与滚动位置都不动。
// 最后一拍仍无释义 → 从「补释义中」落到「暂无释义」（不能永远挂着 queued）。
const PICK_MEANING_RETRY_MS = [800, 2000, 4000, 8000];
function refreshPickMeaning(sessionId, sentenceId, recordId) {
  const rid = Number(recordId);
  if (!Number.isFinite(rid) || rid <= 0) return;
  const redraw = (pick, sentence) => {
    const row = document.querySelector(
      `.pick-sent[data-session-id="${Number(sessionId)}"][data-sentence-id="${Number(sentenceId)}"]`
    );
    if (row) row.replaceWith(pickSentenceRow(pick, sentence));
  };
  const attempt = (index) => {
    window.setTimeout(async () => {
      if (state.route !== "backlog" || !state.pool) return;
      const pick = state.pool.bySession[String(sessionId)];
      if (!pick) return;
      const sentence = (pick.sentences || []).find(
        (item) => String(item.id) === String(sentenceId)
      );
      const item =
        sentence && (sentence.picks || []).find((candidate) => Number(candidate.recordId) === rid);
      if (!item) return;
      let meaning = "";
      try {
        const payload = await api(`/api/records/${Number(recordId)}`);
        const record = payload && payload.record;
        if (record) {
          meaning = String(record.meaning || "");
          item.meaning = meaning;
          item.meaningSource = String(record.meaning_source || "");
        }
      } catch (error) {
        /* 后到释义刷新失败不打扰用户：还有下一拍，或下次进入页面再取 */
      }
      if (meaning) {
        item.fill = "filled";
        redraw(pick, sentence);
        return;
      }
      if (index + 1 < PICK_MEANING_RETRY_MS.length) {
        attempt(index + 1);
        return;
      }
      if (item.fill !== "failed") item.fill = ""; // 超时未补：如实显示「暂无释义」
      redraw(pick, sentence);
    }, PICK_MEANING_RETRY_MS[index]);
  };
  attempt(0);
}

// T4 契约：`POST /api/sessions/{sid}/backlog/pick`，body {sentence_id, text, kind}
function pickBacklogSentence(sessionId, sentenceId, text, kind) {
  return api(
    `/api/sessions/${Number(sessionId)}/backlog/pick`,
    jsonRequest("POST", { sentence_id: Number(sentenceId), text, kind })
  );
}

// v33 修复轮（spec §D19）：chip × 撤销的真回滚 —— 删该词条；该句无残留词条时
// 服务端把已挑标记清掉（回包 sentence_cleared=1，行拨回未挑态）。
function unpickBacklogPick(sessionId, recordId) {
  return api(
    `/api/sessions/${Number(sessionId)}/backlog/pick/${Number(recordId)}`,
    { method: "DELETE" }
  );
}

// v33 修复轮（spec §D19）：单句重识别 —— 对底图重跑 OCR，回 {ok, text, score, suspicious, sentence_key}
function reocrBacklogSentence(sessionId, sentenceId) {
  return api(
    `/api/sessions/${Number(sessionId)}/backlog/${Number(sentenceId)}/reocr`,
    jsonRequest("POST", {})
  );
}

// v34 T1（票 #214 / spec D30）：chip 的释义位 —— 释义优先；没有释义时显示补齐状态
// （补释义中 / 已补 / 暂无释义）；机翻来源挂「机翻」标（不把机翻伪装成词典义）。
function pickChipMeaning(item) {
  const box = el("span", "pick-chip-meaning");
  const meaning = String(item.meaning || "").trim();
  if (meaning) {
    box.append(el("span", "pick-chip-meaning-text", meaning));
    const badge = meaningMachineBadge({
      meaning,
      meaning_source: item.meaningSource,
    });
    if (badge) box.append(badge);
  }
  const fill = String(item.fill || "");
  if (fill === "queued") box.append(el("span", "pick-chip-fill", "补释义中"));
  else if (fill === "filled") box.append(el("span", "pick-chip-fill", "已补"));
  else if (!meaning) box.append(el("span", "pick-chip-fill", "暂无释义"));
  return box;
}

function pickChip(pick, sentence, item) {
  const chip = el("span", `pick-chip is-${item.kind}`);
  const dot = el("span", "pick-chip-dot");
  chip.append(dot);
  chip.append(el("span", "pick-chip-text", item.text));
  chip.append(el("span", "pick-chip-kind", PickPlan.PICK_KIND_LABEL[item.kind] || item.kind));
  chip.append(pickChipMeaning(item));
  const undo = el("i", "pick-chip-undo", "×");
  undo.title = "撤销这次挑词";
  undo.setAttribute("role", "button");
  undo.setAttribute("aria-label", `撤销挑词 ${item.text}`);
  undo.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (item.undoing) return; // 防连点：回滚请求在路上时不重复发
    item.undoing = true;
    try {
      // 已保存态 ×：走既有撤销端点（删词条，若该句已无词条则回未挑 —— ADR-0014 §4）
      if (item.recordId) {
        const payload = await unpickBacklogPick(pick.sessionId, item.recordId);
        if (payload && payload.sentence_cleared) sentence.picked = false; // 行回未挑
      }
      const index = sentence.picks.indexOf(item);
      if (index >= 0) sentence.picks.splice(index, 1);
      if (item.recordId) {
        await refreshPickCounts(pick.sessionId);
      } else if (state.route === "backlog") {
        renderMain();
      }
      if (state.route === "backlog") renderMain();
    } catch (error) {
      item.undoing = false;
      if (state.route === "backlog") renderMain();
      toast(`撤销没存上：${error && error.message ? error.message : error}`);
    }
  });
  chip.append(undo);
  return chip;
}

// 暂存态 chip（v34 T6 / ADR-0014 §4）：虚线描边 + 「未保存」；× 只从数组移除，
// 不发请求、不碰库；kind 标签点一下循环三档（保存前可改档位）。
function draftChip(pick, sentence, item) {
  const chip = el("span", `pick-chip is-draft is-${item.kind}`);
  chip.append(el("span", "pick-chip-dot"));
  chip.append(el("span", "pick-chip-text", item.text));
  const kind = el(
    "span",
    "pick-chip-kind",
    PickPlan.PICK_KIND_LABEL[item.kind] || item.kind
  );
  kind.title = "点一下换档位（词 / 短语 / 短句）";
  kind.setAttribute("role", "button");
  kind.setAttribute("tabindex", "0");
  kind.addEventListener("click", (event) => {
    event.stopPropagation();
    cycleDraftKind(pick.sessionId, sentence.id, item);
  });
  chip.append(kind);
  chip.append(el("span", "pick-chip-draft-tag", "未保存"));
  const meaning = el("span", "pick-chip-meaning");
  meaning.append(el("span", "pick-chip-fill", "保存后补释义"));
  chip.append(meaning);
  const undo = el("i", "pick-chip-undo", "×");
  undo.title = "从暂存栏移除（不写词条库）";
  undo.setAttribute("role", "button");
  undo.setAttribute("aria-label", `移除暂存挑词 ${item.text}`);
  undo.addEventListener("click", (event) => {
    event.stopPropagation();
    removeDraftItem(pick.sessionId, sentence.id, item);
    if (state.route === "backlog") renderMain();
  });
  chip.append(undo);
  return chip;
}

// 看图 / 改句（spec §D19 三层纠错的「看图改句」层）：可疑句 + 有底图才出现。
// 底图走 T4 只读端点（票 #210）：`GET /api/backlog/image?path=<文件名>` —— 端点
// 只 serve data/subtitle_bands 下的 .png（裸文件名沙箱，穿越/绝对路径一律 400），
// 所以这里从 T2 落盘的 image_path（绝对路径）只取**文件名**交给端点；加载失败
// 降级回显示路径的 code 节点（老中枢 / 文件被 TTL 清掉都不崩）。
function pickImgBox(pick, sentence) {
  const box = el("div", "pick-imgbox");
  box.append(el("div", "pick-imgbox-note", "字幕底图（对图人工改句，修正 OCR 错字）："));
  const imagePath = String(sentence.image_path || "");
  const fallback = el("code", "pick-imgbox-path", imagePath);
  fallback.title = imagePath;
  const name = imagePath.split(/[\\/]/).pop() || "";
  const img = document.createElement("img");
  img.className = "pick-imgbox-img";
  img.alt = "字幕底图";
  img.src = `/api/backlog/image?path=${encodeURIComponent(name)}`;
  // D7：加载失败时 OCR 来源句给明确提示（有「重识别」按钮可点），不再只默默显示路径。
  img.addEventListener("error", () =>
    img.replaceWith(sentence.source && sentence.source !== "cc"
      ? pickImgMissingNote(sentence)
      : fallback)
  );
  box.append(img);
  const input = document.createElement("input");
  input.type = "text";
  input.value = String(sentence.text || "");
  input.setAttribute("aria-label", "改后原句");
  box.append(input);
  const acts = el("div", "pick-acts");
  // v33 修复轮（spec §D19 纠错第二层）：单句重识别 —— 对底图重跑 OCR 回写文本
  acts.append(
    button("btn btn-subtle", "⟳ 重识别", async () => {
      try {
        const payload = await reocrBacklogSentence(pick.sessionId, sentence.id);
        sentence.text = String(payload.text || sentence.text);
        sentence.score = payload.score; // ⚠ 标的置信度随新结果
        sentence.suspicious = Boolean(payload.suspicious);
        sentence.picked = false; // 原句变了 = 服务端已清已挑标记，行回未挑
        sentence.picks = [];
        clearDraftsForSentence(pick.sessionId, sentence.id); // 旧暂存的词下标一并作废
        toast("已重识别");
        renderMain();
      } catch (error) {
        toast(`重识别没存上：${error && error.message ? error.message : error}`);
      }
    })
  );
  acts.append(
    button("btn btn-subtle", "✓ 保存改句", async () => {
      const text = String(input.value || "").trim();
      if (!text || text === sentence.text) return;
      try {
        await saveBacklogText(pick.sessionId, sentence.id, text);
        sentence.text = text;
        sentence.picks = []; // 原句变了：旧挑词的词下标全部作废，句子回未挑态
        clearDraftsForSentence(pick.sessionId, sentence.id);
        toast("已保存改句");
        renderMain();
      } catch (error) {
        toast(
          `改句没存上：${error && error.message ? error.message : error}（中枢可能还是旧版本）`
        );
      }
    })
  );
  box.append(acts);
  return box;
}

// T4 契约：`PATCH /api/sessions/{sid}/backlog/{sentence_id}`，body {text}
function saveBacklogText(sessionId, sentenceId, text) {
  return api(
    `/api/sessions/${Number(sessionId)}/backlog/${Number(sentenceId)}`,
    jsonRequest("PATCH", { text })
  );
}

function pickImgToggle(body) {
  const acts = el("div", "pick-acts");
  const toggle = el("a", "pick-toggle-img", "🖼 看图 / 改句");
  toggle.setAttribute("role", "button");
  toggle.title = "展开字幕底图与改句输入";
  toggle.addEventListener("click", () => {
    const box = body.querySelector(".pick-imgbox");
    if (box) box.classList.toggle("is-open");
  });
  acts.append(toggle);
  return acts;
}

/* --- toast ------------------------------------------------------------------- */

function toast(message) {
  const node = el("div", "toast", message);
  dom.toasts.append(node);
  window.requestAnimationFrame(() => node.classList.add("is-on"));
  window.setTimeout(() => {
    node.classList.remove("is-on");
    window.setTimeout(() => node.remove(), 200);
  }, TOAST_MS);
}

/* --- 启动 -------------------------------------------------------------------- */

initTheme();
ensureSelectionLive(); // v16 C11：live region 要先于任何播报挂上（插入后才写才算「变化」）
loadFold(); // 折叠态要先于首屏渲染复原（renderSidebar 每次全量重建）
state.poolDrafts = loadPickDrafts(); // v34 T6：暂存挑词要在首屏渲染前复原
state.shell = readShell(); // v14 T3：沉浸外壳偏好（必须在 routeFromHash 之前）
state.view = readView() || "orbit"; // v14 T3：首开默认星图；之后照旧记住
state.articleWordedOnly = readArticleWordedOnly(); // v22 P3：只播有词段的偏好
state.route = routeFromHash();
renderAll();
loadAll({ skeleton: true });
// v22 P1b：直接用 `#/article/<id>` 打开（深链 / 前进后退）时同时拉文章详情
if (state.route === "article") loadArticle(state.articleId, { skeleton: true });
loadHotkeyHints(); // 空态/帮助里的键位提示要显示当前值（一次 GET，本地）
// v34 T5（票 #218）：顶栏状态条开跑（10s；页面隐藏时停，恢复可见立刻拉一次）
startCaptureWatch();
if (state.route === "review") loadReviewQueue(); // 直接用 #/review 打开时也要拉队列
if (state.route === "backlog") loadPool(state.poolId, { skeleton: true }); // 直接用 #/backlog / #/pick/<id> 打开时也要拉待选句
if (state.route === "settings") {
  loadSettings({ skeleton: true });
  startSettingsPoll();
}
// W2（票 #183）：本标签页之前放弃的补释义任务还留着 job_id —— 给一次接驳入口
//（任务有 10 分钟 TTL，真过期会在接驳后立刻以「任务已失效」收场，不会卡住）。
const storedRefillJob = readStoredRefillJob();
if (storedRefillJob) openRefillReattach(storedRefillJob, "页面刷新中断了轮询");





