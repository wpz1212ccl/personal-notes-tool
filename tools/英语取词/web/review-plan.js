/* v31「听看复习」高亮队列：**纯函数**模块，不碰 DOM / 网络 / localStorage。
 *
 * 单位 = 一条记录（record），不是渲染段：同一句里 `want ⊂ want to ⊂ want to go to`
 * 会切成多段，但复习时每个词条只播一次。排序规则：
 *   1) 段卡顺序 / 文章段落顺序原样保留；
 *   2) 同一句里互相重叠的记录成一个「簇」，簇内按 kind 优先级 词 → 短语 → 句，
 *      同档按出现位置、长度、target 排（用户 2026-11-04 确认：三档都要播）；
 *   3) 不重叠的簇仍按阅读顺序（先出现的先播）；
 *   4) 句内找不到落点的记录排在已定位记录之后，按同一条优先级；
 *   5) 同一 target + 同一 span 的重复记录只留第一条（records 通常是最新在前）。
 *
 * 双环境导出：index.html 在 app.js 之前 defer 加载（window.ReviewPlan）；
 * docs/research/v31-highlight-review/plan_check.js 在 Node 里 require。
 */
(function (root) {
  "use strict";

  const KIND_PRIORITY = Object.freeze({ word: 0, phrase: 1, sentence: 2 });
  const UNKNOWN_PRIORITY = 3;

  function kindPriority(kind) {
    const key = String(kind == null ? "" : kind).toLowerCase();
    return Object.prototype.hasOwnProperty.call(KIND_PRIORITY, key)
      ? KIND_PRIORITY[key]
      : UNKNOWN_PRIORITY;
  }

  function normalizedSpan(span) {
    if (!span) return null;
    const start = Number(span.start);
    const end = Number(span.end);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
    return { start, end };
  }

  function entryKey(entry) {
    const target = String((entry.record && entry.record.target) || "")
      .trim()
      .toLowerCase();
    const span = entry.span;
    return `${target}\u0000${span ? `${span.start}:${span.end}` : "none"}`;
  }

  function dedupe(entries) {
    const seen = new Set();
    const out = [];
    for (const entry of entries) {
      const key = entryKey(entry);
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(entry);
    }
    return out;
  }

  function compareEntries(one, two) {
    const rank = kindPriority(one.record && one.record.kind) - kindPriority(two.record && two.record.kind);
    if (rank) return rank;
    const startOne = one.span ? one.span.start : Number.POSITIVE_INFINITY;
    const startTwo = two.span ? two.span.start : Number.POSITIVE_INFINITY;
    if (startOne !== startTwo) return startOne - startTwo;
    const widthOne = one.span ? one.span.end - one.span.start : 0;
    const widthTwo = two.span ? two.span.end - two.span.start : 0;
    if (widthOne !== widthTwo) return widthOne - widthTwo;
    const targetOne = String((one.record && one.record.target) || "");
    const targetTwo = String((two.record && two.record.target) || "");
    return targetOne.localeCompare(targetTwo);
  }

  // 同一句内的排序：重叠记录成簇（区间有交集，传递合并），簇内走 kind 优先级；
  // 簇与簇之间按起点。未定位记录最后。
  function orderEntries(entries) {
    const located = [];
    const unlocated = [];
    for (const entry of entries) {
      const span = normalizedSpan(entry.span);
      if (span) located.push({ ...entry, span });
      else unlocated.push({ ...entry, span: null });
    }
    located.sort((one, two) => one.span.start - two.span.start || one.span.end - two.span.end);

    const clusters = [];
    for (const entry of located) {
      const last = clusters[clusters.length - 1];
      if (last && entry.span.start < last.end) {
        last.items.push(entry);
        if (entry.span.end > last.end) last.end = entry.span.end;
      } else {
        clusters.push({ start: entry.span.start, end: entry.span.end, items: [entry] });
      }
    }

    const ordered = [];
    for (const cluster of clusters) {
      cluster.items.sort(compareEntries);
      ordered.push(...cluster.items);
    }
    unlocated.sort(compareEntries);
    ordered.push(...unlocated);
    return ordered;
  }

  function finalize(entries) {
    return orderEntries(dedupe(entries)).map((entry) => {
      const location = String(entry.context || "");
      const span = entry.span;
      const visible = span ? location.slice(span.start, span.end) : "";
      const target = String((entry.record && entry.record.target) || "").trim();
      return {
        ...entry,
        // 没落点的记录没有可见切片可播，只能退回 target 原始拼写（它排在最后，见头注 4）。
        playText: visible.trim() || target,
      };
    });
  }

  function buildEntries(records, context, locate, cardKey, paragraphIndex) {
    const entries = [];
    for (const record of Array.isArray(records) ? records : []) {
      const target = String((record && record.target) || "").trim();
      if (!target) continue;
      const span = context && typeof locate === "function" ? normalizedSpan(locate(context, record)) : null;
      entries.push({
        record,
        context: String(context || ""),
        span,
        // 归属标识：驱动器不读（每条自带 record / context / span），
        // 但 plan_check.js 靠它断言「跨卡 / 跨段不交错」——排序语义唯一可直接观察的证据。
        cardKey: cardKey == null ? null : String(cardKey),
        paragraphIndex: Number.isFinite(Number(paragraphIndex)) ? Number(paragraphIndex) : null,
      });
    }
    return entries;
  }

  // 会话 / 星图：cards 已按阅读顺序排好（段内最早收录时间），每张卡一段原句。
  function buildSessionPlan(cards, locate) {
    const plan = [];
    for (const card of Array.isArray(cards) ? cards : []) {
      if (!card) continue;
      const entries = buildEntries(card.records, card.context, locate, card.key, null);
      plan.push(...finalize(entries));
    }
    return plan;
  }

  // 文章：paragraphs 原样顺序；未定位（start < 0）的记录没有可锚定的 mark，不进队列。
  // 名字不叫 buildArticlePlan —— 那是 v30 `ArticleRead` 的**连播**计划器，同一页面里两个
  // 同名不同义（一个排高亮词条，一个排自然段分片）只会让读代码的人挑错一个。
  function buildArticleHighlightPlan(article) {
    const plan = [];
    const paragraphs = article && Array.isArray(article.paragraphs) ? article.paragraphs : [];
    paragraphs.forEach((paragraph, fallbackIndex) => {
      if (!paragraph) return;
      const context = String(paragraph.text == null ? "" : paragraph.text);
      const index = Number.isFinite(Number(paragraph.index)) ? Number(paragraph.index) : fallbackIndex;
      const entries = [];
      for (const record of Array.isArray(paragraph.records) ? paragraph.records : []) {
        const target = String((record && record.target) || "").trim();
        if (!target) continue;
        const span = normalizedSpan({
          start: record.start_in_paragraph,
          end: record.end_in_paragraph,
        });
        if (!span) continue;
        entries.push({
          record,
          context,
          span,
          cardKey: `p${index}`,
          paragraphIndex: index,
        });
      }
      plan.push(...finalize(entries));
    });
    return plan;
  }

  // 只导出两个入口：优先级表与排序核心是模块内部的事，不外泄。
  const ReviewPlan = {
    buildSessionPlan,
    buildArticleHighlightPlan,
  };

  if (typeof module !== "undefined" && module.exports) module.exports = ReviewPlan;
  if (root) root.ReviewPlan = ReviewPlan;
})(typeof window !== "undefined" ? window : null);
