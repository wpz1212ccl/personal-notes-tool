/* v22 P3（票 #168 / spec §8.5）：文章页「按自然段连播」的**纯函数**模块。
 *
 * 只做三件事，不碰 DOM / 网络 / localStorage：
 *   1) 分片：splitForSpeech / splitSpeechOverflow（从 app.js §2.1 原样搬来，**唯一算法**，
 *      app.js 里的同名函数只做转发；证据脚本直接 require 这个文件，测的就是线上代码）；
 *   2) splitParagraph(text)：旧口径（200 字）—— 仍原样导出以兼容既有证据脚本，文章计划不再使用；
 *      splitArticleParagraph(text)：v30 自然停顿分片（T1 句末 → T2 分号/冒号/破折号 →
 *      T3 从句逗号，T4/T5 只兜底），带 [start,end) 偏移，播放时高亮能对上段落卡的记录 span；
 *   3) buildArticlePlan(article, {wordedOnly, startIndex})：连播播放计划 —— 每段一次、
 *      顺序 = paragraphs 顺序、范围 = 该文章（不跨文章），段内用 splitArticleParagraph。
 *
 * 双环境导出：index.html 里在 app.js 之前用 defer 加载（`window.ArticleRead`）；
 * `docs/research/v22-audio-paragraph/plan_check.js` 在 Node 里 require（CommonJS）。
 */
// 包在 IIFE 里：经典 <script> 的顶层 `const` 与 app.js 共享同一个全局词法环境，
// 裸写会与 app.js 的同名转发函数冲突（重复声明 = 整个脚本不执行）。导出统一走
// `window.ArticleRead`（浏览器）/ `module.exports`（Node 证据脚本）。
(function (root) {
  "use strict";

  /* --- 分片（v11 §2.1 / 票 #114；从 app.js 原样搬来，勿在这里改语义） ---------- */

  const SPEECH_CHUNK_LIMIT = 200; // 逐句请求，单片文本上限 200 字符
  // 句末：英文 . ! ? …、中文 。！？、换行
  const SPEECH_SENTENCE_END = /[.!?…。！？\n]/;
  // 中文 / 全角句末后面不写空格，也算句末（英文点要再看后面是不是空白，避开 1.5 / U.S.）
  const CJK_SENTENCE_END = /[。！？…]/;
  const SPEECH_SENTENCE_TAIL = /["'”’）)\]】」』]/; // 句末紧跟的收尾引号/括号归前一句
  // 单句超过 limit 时的再切点：空白 / 逗号 / 分号 / 冒号（中英都算）
  const SPEECH_SPLIT_POINT = /[\s,;:，、；：]/;

  // §2.1：点整段时按句切分（纯函数）—— 每片 ≤ limit，不丢字、不重字、顺序不变。
  // 短文本（≤ limit）原样返回一片：单句 / 词组仍只发一次请求。
  function splitForSpeech(text, limit = SPEECH_CHUNK_LIMIT) {
    const source = String(text == null ? "" : text).trim();
    if (!source) return [];
    const cap = Number(limit) > 0 ? Math.floor(Number(limit)) : SPEECH_CHUNK_LIMIT;
    if (source.length <= cap) return [source];
    const chunks = [];
    let start = 0;
    for (let i = 0; i < source.length; i += 1) {
      const mark = source[i];
      if (!SPEECH_SENTENCE_END.test(mark)) continue;
      let end = i + 1;
      // `?!` / `...` 与紧跟的收尾引号算同一句
      while (end < source.length && /[.!?…。！？]/.test(source[end])) end += 1;
      while (end < source.length && SPEECH_SENTENCE_TAIL.test(source[end])) end += 1;
      // 英文 . ! ? 后面必须是空白或结尾：`1.5` / `U.S.` 里的点不是句末
      if (mark !== "\n" && !CJK_SENTENCE_END.test(mark)) {
        if (end < source.length && !/\s/.test(source[end])) {
          i = end - 1;
          continue;
        }
      }
      pushSpeechChunks(chunks, source.slice(start, end), cap);
      start = end;
      i = end - 1;
    }
    pushSpeechChunks(chunks, source.slice(start), cap);
    return chunks;
  }

  function pushSpeechChunks(chunks, raw, limit) {
    const piece = String(raw == null ? "" : raw).trim();
    if (!piece) return;
    if (piece.length <= limit) {
      chunks.push(piece);
      return;
    }
    for (const part of splitSpeechOverflow(piece, limit)) chunks.push(part);
  }

  // 超长单句：先在空白 / 逗号处断开；实在没有切点才硬切（保证每片 ≤ limit）。
  // 切点字符归左片，右片只 trim 空白 —— 不丢字、不重字。
  function splitSpeechOverflow(sentence, limit) {
    const parts = [];
    let rest = sentence;
    while (rest.length > limit) {
      let cut = -1;
      for (let i = limit; i > 1; i -= 1) {
        if (SPEECH_SPLIT_POINT.test(rest[i - 1])) {
          cut = i;
          break;
        }
      }
      if (cut <= 0) cut = limit;
      const head = rest.slice(0, cut).trim();
      if (head) parts.push(head);
      rest = rest.slice(cut).trim();
    }
    if (rest) parts.push(rest);
    return parts;
  }

  /* --- 按段分片：带 [start,end) 偏移 ----------------------------------------- */

  // 一段原文 → [{ text, start, end }]。`text` 仍是 splitForSpeech 的产物（逐字与原文一致，
  // 只 trimmed 边界空白），start/end 是它在段落原文里的真实区间 —— 相邻分片之间只隔空白，
  // 所以「不重不丢」可被逐字断言（见 plan_check.js）。
  function splitParagraph(text, limit = SPEECH_CHUNK_LIMIT) {
    const source = String(text == null ? "" : text);
    const chunks = splitForSpeech(source, limit);
    const parts = [];
    let cursor = 0;
    for (const chunk of chunks) {
      // 分片按顺序取自原文，且只 trim 边界空白：从 cursor 往后第一次出现就是它的真实位置
      // （更早的相同串只可能落在上一片内或纯空白间隔里，两者都不可能）。
      const at = source.indexOf(chunk, cursor);
      const start = at >= 0 ? at : cursor;
      const end = start + chunk.length;
      parts.push({ text: chunk, start, end });
      cursor = end;
    }
    return parts;
  }

  /* --- v30 T1（票 #192 / spec #191）：文章自然停顿分片 ------------------------- */

  // 独立参数：首片 150 / 常规目标 200 / 硬上限 400 / 最短 50（旧 SPEECH_CHUNK_LIMIT 只服务会话路径）
  const ARTICLE_FIRST_LIMIT = 150;
  const ARTICLE_TARGET_LIMIT = 200;
  const ARTICLE_HARD_LIMIT = 400;
  const ARTICLE_MIN_TAIL = 50;

  const ARTICLE_SENTENCE_END = /[.!?…。！？]/;
  const ARTICLE_T2_MARK = /[;:—–]/;
  const ARTICLE_CLOSER = /["'”’»)\]}]/;
  const ARTICLE_ABBREV = new Set([
    "mr", "dr", "ms", "mrs", "st", "vs", "etc", "no", "jr", "sr", "inc", "ltd",
    "e.g", "i.e", "u.s", "p.m", "a.m",
  ]);
  const ARTICLE_CONNECTOR = new Set([
    "and", "but", "so", "yet", "because", "although", "though", "while", "whereas",
    "which", "who", "whom", "whose", "that", "when", "if", "however", "therefore", "then",
  ]);
  const ARTICLE_CLAUSE_STARTER = new Set([
    "i", "we", "he", "she", "they", "it", "you", "one", "there", "this", "that", "these",
    "those", "his", "her", "its", "their", "our", "my", "the", "a", "an", "then", "later",
    "however", "therefore", "meanwhile", "otherwise",
  ]);
  const ARTICLE_AUX = new Set([
    "is", "am", "are", "was", "were", "be", "been", "being", "do", "does", "did", "has",
    "have", "had", "can", "could", "will", "would", "shall", "should", "may", "might", "must",
  ]);
  const ARTICLE_IRREGULAR_VERB = new Set([
    "went", "came", "saw", "said", "told", "took", "made", "gave", "found", "ran", "ate",
    "bought", "brought", "kept", "left", "knew", "known", "grew", "held", "stood", "thought",
    "began", "became", "fell", "felt", "spent", "built", "sent", "met", "paid", "rose",
  ]);
  // 虚词：超硬上限兜底时，切点不能落在这些词之后 / 之前
  const ARTICLE_FUNCTION = new Set(
    (
      "a an the of in to on at by for with from into onto over under about after before between " +
      "among during without within through across against along around behind below beneath beside " +
      "beyond despite except inside outside since toward towards upon via per off out up down " +
      "is am are was were be been being do does did has have had can could will would shall should " +
      "may might must and but or nor so yet because although though while whereas if when that which " +
      "who whom whose as than unless until whether this these those some any no every each both " +
      "either neither all few many much more most other another such not"
    ).split(/\s+/)
  );
  const ARTICLE_UNIT = new Set([
    "km", "m", "cm", "mm", "kg", "g", "mg", "mi", "ft", "in", "yd", "h", "min", "s", "ms",
    "usd", "eur", "gb", "mb", "tb", "%", "°c", "°f",
  ]);

  function articleWords(text) {
    return String(text)
      .toLowerCase()
      .replace(/[^a-z0-9'’]+/g, " ")
      .split(/\s+/)
      .filter(Boolean);
  }

  function articleLastWord(text) {
    const list = articleWords(text);
    return list.length ? list[list.length - 1] : "";
  }

  function articleFirstWord(text) {
    const list = articleWords(text);
    return list.length ? list[0] : "";
  }

  // 段首标号 `(G)` / `1.` / `A)` 与后句之间不许断：返回标号后正文的起点（无标号 = 0）。
  function articleLabelEnd(source) {
    const match = /^\s*(?:\([A-Za-z0-9]{1,4}\)|[A-Za-z0-9]{1,4}[.)])\s+/.exec(source);
    return match ? match[0].length : 0;
  }

  function articleSkipSpace(source, at) {
    let index = at;
    while (index < source.length && /\s/.test(source[index])) index += 1;
    return index;
  }

  function articleTrimmedLength(source, start, end) {
    let left = start;
    let right = end;
    while (left < right && /\s/.test(source[left])) left += 1;
    while (right > left && /\s/.test(source[right - 1])) right -= 1;
    return right - left;
  }

  // 位置 pos 是否落在引号/括号/反引号代码内部（硬切兜底时避开）。
  function articleInsideMarks(source, pos) {
    let quote = 0;
    let paren = 0;
    let code = false;
    for (let i = 0; i < pos; i += 1) {
      const ch = source[i];
      if (ch === "`") { code = !code; continue; }
      if (code) continue;
      if (ch === "\"") { quote = quote ? 0 : 1; continue; }
      if (ch === "“") { quote += 1; continue; }
      if (ch === "”") { quote = Math.max(0, quote - 1); continue; }
      if (ch === "‘") { quote += 1; continue; }
      if (ch === "’") {
        if (quote) quote = Math.max(0, quote - 1);
        continue;
      }
      if (ch === "'") {
        const prev = source[i - 1] || "";
        const next = source[i + 1] || "";
        if (/[A-Za-z0-9]/.test(prev) && !/[A-Za-z]/.test(next)) continue;
        if (/[A-Za-z]/.test(prev) && /[A-Za-z]/.test(next)) continue;
        if (quote) quote = 0;
        else quote = 1;
        continue;
      }
      if (ch === "(" || ch === "[" || ch === "{") { paren += 1; continue; }
      if (ch === ")" || ch === "]" || ch === "}") { paren = Math.max(0, paren - 1); }
    }
    return quote > 0 || paren > 0 || code;
  }

  function articleLooksLikeClause(fragment) {
    const tokens = articleWords(fragment);
    if (tokens.length < 2) return false;
    if (ARTICLE_FUNCTION.has(tokens[tokens.length - 1])) return false;
    return tokens.some(
      (token) =>
        ARTICLE_AUX.has(token) ||
        ARTICLE_IRREGULAR_VERB.has(token) ||
        /^[a-z]{4,}ed$/.test(token) ||
        /^[a-z]{5,}ing$/.test(token)
    );
  }

  // 逗号是否是可断的从句边界（T3）：后接从句连词，或后接从句主语且逗号前是完整主谓。
  // 枚举（`A, B, and C` / `the book, the pen, and the ruler`）一律不断。
  function articleCommaCut(source, clauseStart, commaAt) {
    const after = articleSkipSpace(source, commaAt + 1);
    const word = (/^[A-Za-z]+/.exec(source.slice(after, after + 24)) || [""])[0].toLowerCase();
    if (!word) return false;
    const clause = source.slice(clauseStart, commaAt);
    const fragment = clause.slice(clause.lastIndexOf(",") + 1);
    if (ARTICLE_CONNECTOR.has(word)) {
      if ((word === "and" || word === "or" || word === "nor") && articleWords(fragment).length <= 4) {
        return false; // 串行逗号列表里的 `, and C`
      }
      return true;
    }
    if (!ARTICLE_CLAUSE_STARTER.has(word)) return false;
    if (!articleLooksLikeClause(clause)) return false;
    if (/^(?:the|a|an)$/.test(word)) {
      // `the book, the pen, and ...`：后一项自己又跟着逗号 → 仍是枚举
      const rest = source.slice(after);
      const stop = rest.search(/[.!?…。！？]/);
      const window = stop >= 0 ? rest.slice(0, stop) : rest;
      if (window.indexOf(",") >= 0 && window.indexOf(",") < 80) return false;
    }
    return true;
  }

  // 扫描一遍，收集允许切点 [{ pos, level }]（pos = 前一片的结束位置）。
  function articleCollectCuts(source) {
    const cuts = [];
    const labelEnd = articleLabelEnd(source);
    let quote = 0;
    let paren = 0;
    let code = false;
    let clauseStart = 0;
    const push = (pos, level) => {
      if (pos <= 0 || pos > source.length) return;
      if (pos <= labelEnd) return;
      cuts.push({ pos, level });
      clauseStart = pos;
    };
    for (let i = 0; i < source.length; i += 1) {
      const ch = source[i];
      if (ch === "`") { code = !code; continue; }
      if (code) continue;
      if (ch === "\"") {
        quote = quote ? 0 : 1;
        if (!quote && !paren && /^(\s|$)/.test(source[i + 1] || "")) push(i + 1, 2);
        continue;
      }
      if (ch === "“" || ch === "‘") { quote += 1; continue; }
      if (ch === "”") {
        quote = Math.max(0, quote - 1);
        if (!quote && !paren && /^(\s|$)/.test(source[i + 1] || "")) push(i + 1, 2);
        continue;
      }
      if (ch === "’") {
        // 右单引号只作闭合：quote=0 时是所有格/缩写（readers’ notes），不能开引号。
        if (!quote) continue;
        quote = Math.max(0, quote - 1);
        if (!quote && !paren && /^(\s|$)/.test(source[i + 1] || "")) push(i + 1, 2);
        continue;
      }
      if (ch === "'") {
        const prev = source[i - 1] || "";
        const next = source[i + 1] || "";
        if (/[A-Za-z0-9]/.test(prev) && !/[A-Za-z]/.test(next)) continue; // 所有格 / 尾缩写
        if (/[A-Za-z]/.test(prev) && /[A-Za-z]/.test(next)) continue; // don't
        if (quote) {
          quote = 0;
          if (!paren && /^(\s|$)/.test(next)) push(i + 1, 2);
        } else {
          quote = 1;
        }
        continue;
      }
      if (ch === "(" || ch === "[" || ch === "{") { paren += 1; continue; }
      if (ch === ")" || ch === "]" || ch === "}") {
        paren = Math.max(0, paren - 1);
        if (!quote && !paren && /^(\s|$)/.test(source[i + 1] || "")) push(i + 1, 2);
        continue;
      }
      if (ch === "\n") { push(i + 1, 1); continue; }
      if (quote > 0 || paren > 0) continue;
      if (ARTICLE_SENTENCE_END.test(ch)) {
        let end = i + 1;
        while (end < source.length && /[.!?…。！？]/.test(source[end])) end += 1;
        if (!/^(\s|$)/.test(source[end] || "")) continue;
        if (ch === ".") {
          const before = source.slice(Math.max(0, i - 24), i);
          const token = (/[A-Za-z][A-Za-z.]*$/.exec(before) || [""])[0];
          const bare = token.replace(/\.$/, "");
          if (/\d/.test(source[end] || "")) continue; // 1.5 的小数点；2019. 仍是句末
          if (ARTICLE_ABBREV.has(bare.toLowerCase())) continue;
          if (/\.[A-Za-z]/.test(bare)) continue; // U.S / e.g / i.e 这种内部带点的缩写
          if (/^[A-Za-z]$/.test(bare)) continue; // J. K. 这种单字母缩写
        }
        push(end, 1);
        i = end - 1;
        continue;
      }
      if (ARTICLE_T2_MARK.test(ch) && /^(\s|$)/.test(source[i + 1] || "")) {
        push(i + 1, 2);
        continue;
      }
      if (ch === "," && /^\s/.test(source[i + 1] || "")) {
        if (/\d/.test(source[i - 1] || "")) continue; // 200,000
        if (articleCommaCut(source, clauseStart, i)) push(i + 1, 3);
      }
    }
    return cuts;
  }

  // 超硬上限兜底：≤400 的最后一个空格；落在虚词/数字单位处就左移到词边界。
  function articleBadBoundary(source, start, cut) {
    const head = articleLastWord(source.slice(start, cut));
    const tail = articleFirstWord(source.slice(cut));
    if (ARTICLE_FUNCTION.has(head) || ARTICLE_FUNCTION.has(tail)) return true;
    if (/[\d$%.,]$/.test(source.slice(start, cut).trim()) && ARTICLE_UNIT.has(tail)) return true;
    if (ARTICLE_UNIT.has(head) && /^[\d$]/.test(tail)) return true;
    return false;
  }

  function articleHardCut(source, start, end) {
    const upper = Math.min(end - ARTICLE_MIN_TAIL, start + ARTICLE_HARD_LIMIT);
    if (upper <= start + ARTICLE_MIN_TAIL) return end;
    let cut = -1;
    for (let i = upper; i > start + ARTICLE_MIN_TAIL; i -= 1) {
      if (/\s/.test(source[i]) && !articleInsideMarks(source, i)) { cut = i; break; }
    }
    if (cut < 0) {
      for (let i = upper; i > start + ARTICLE_MIN_TAIL; i -= 1) {
        if (/\s/.test(source[i])) { cut = i; break; }
      }
    }
    if (cut < 0) return Math.min(upper, start + ARTICLE_HARD_LIMIT);
    let guard = 0;
    while (cut > start + ARTICLE_MIN_TAIL && guard < 16 && articleBadBoundary(source, start, cut)) {
      let prev = -1;
      for (let i = cut - 1; i > start + ARTICLE_MIN_TAIL; i -= 1) {
        if (/\s/.test(source[i])) { prev = i; break; }
      }
      if (prev < 0) break;
      cut = prev;
      guard += 1;
    }
    return cut;
  }

  function articlePickCut(source, start, end, cuts, isFirst) {
    const limit = Math.min(end, start + ARTICLE_HARD_LIMIT);
    const viable = cuts.filter((cut) => {
      if (cut.pos <= start || cut.pos > limit) return false;
      const head = articleTrimmedLength(source, start, cut.pos);
      const tail = end - articleSkipSpace(source, cut.pos);
      return head >= ARTICLE_MIN_TAIL && (tail === 0 || tail >= ARTICLE_MIN_TAIL);
    });
    if (!viable.length) {
      if (end - start <= ARTICLE_HARD_LIMIT) return end;
      return articleHardCut(source, start, end);
    }
    if (isFirst) {
      const early = viable.filter(
        (cut) => articleTrimmedLength(source, start, cut.pos) <= ARTICLE_FIRST_LIMIT
      );
      if (early.length) return early[0].pos;
      const sentence = viable.find((cut) => cut.level === 1);
      if (sentence) return sentence.pos;
      return viable[viable.length - 1].pos;
    }
    const target = viable.filter(
      (cut) => articleTrimmedLength(source, start, cut.pos) <= ARTICLE_TARGET_LIMIT
    );
    if (target.length) return target[target.length - 1].pos;
    return viable[viable.length - 1].pos;
  }

  // 一段原文 → [{ text, start, end }]。切点优先 T1 句末 → T2 分号/冒号/破折号/闭合 →
  // T3 从句逗号；T4/T5 只在超硬上限兜底。`text === source.slice(start,end)`（只 trim 边界空白）。
  function splitArticleParagraph(text) {
    const source = String(text == null ? "" : text);
    const parts = [];
    const cuts = articleCollectCuts(source);
    let cursor = 0;
    while (true) {
      let start = articleSkipSpace(source, cursor);
      if (start >= source.length) break;
      const end = source.length;
      let cut = articlePickCut(source, start, end, cuts, parts.length === 0);
      if (cut <= start) cut = end;
      let safeEnd = cut;
      while (safeEnd > start && /\s/.test(source[safeEnd - 1])) safeEnd -= 1;
      if (safeEnd > start) {
        parts.push({ text: source.slice(start, safeEnd), start, end: safeEnd });
        cursor = cut;
      } else {
        cursor = Math.max(cut, start + 1);
      }
    }
    return parts;
  }

  /* --- 播放计划 --------------------------------------------------------------- */

  // 连播计划：按 `article.paragraphs` 顺序（每段一次）；`startIndex` 是**段号**
  // （passage index，不是计划下标）；`wordedOnly` 只保留「有记录」的段。
  // 返回 [{ index, text, chunks: [{text,start,end}], records }]，空段 chunks=[]（播放时跳过）。
  // 范围天然 = 该 article 对象，不跨文章。
  function buildArticlePlan(article, options = {}) {
    const paragraphs = article && Array.isArray(article.paragraphs) ? article.paragraphs : [];
    const wordedOnly = options.wordedOnly === true;
    let startIndex = Number(options.startIndex);
    if (!Number.isFinite(startIndex) || startIndex < 0) startIndex = 0;
    const plan = [];
    for (const paragraph of paragraphs) {
      const index = Number(paragraph && paragraph.index);
      if (!Number.isFinite(index) || index < startIndex) continue;
      const records = Array.isArray(paragraph.records) ? paragraph.records : [];
      if (wordedOnly && !records.length) continue;
      const text = String(paragraph.text == null ? "" : paragraph.text);
      plan.push({
        index,
        text,
        chunks: splitArticleParagraph(text),
        records: records.slice(),
      });
    }
    return plan;
  }

  const ArticleRead = {
    SPEECH_CHUNK_LIMIT,
    splitForSpeech,
    pushSpeechChunks,
    splitSpeechOverflow,
    splitParagraph,
    ARTICLE_FIRST_LIMIT,
    ARTICLE_TARGET_LIMIT,
    ARTICLE_HARD_LIMIT,
    ARTICLE_MIN_TAIL,
    splitArticleParagraph,
    buildArticlePlan,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = ArticleRead;
  if (root) root.ArticleRead = ArticleRead;
})(typeof window !== "undefined" ? window : null);
