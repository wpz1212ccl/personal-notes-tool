/* v33「独立挑词界面」纯函数模块：kind 预判 / 区间归一 / 重叠渲染计算 / 计数与筛选。
 * 不碰 DOM / 网络 / localStorage —— DOM 接线在 app.js（#pick 路由），测试见
 * tests/test_pick_plan.py（Node 直接 require 本模块）。
 *
 * 契约来自 panel-v2 视觉稿（docs/research/v33-picking-ui/panel-v2.html，交互即契约）：
 * - 每句的挑词 = picks 数组 [{start, end, kind}]，start/end 为词下标（含端点）；
 * - kind 预判：拖选 1 词 = word、2–5 词 = phrase、6+ 词 = sentence（气泡里可改）；
 * - 重叠嵌套渲染：覆盖某词的**最短**挑词铺底色（内层），**最长**的挑词画下弧线
 *   （外层）——沿用 v31 以来「内层底色 + 外层弧线」的既有重叠语言；
 * - 计数（spec §D20）：已挑 = 词条数（picks 总数）、待挑 = 零挑词的句子、共 = 句数。
 *
 * 双环境导出：index.html 在 app.js 之前 defer 加载（window.PickPlan）；
 * tests/test_pick_plan.py 在 Node 里 require。
 */
(function (root) {
  "use strict";

  const PICK_KIND_LABEL = Object.freeze({
    word: "词",
    phrase: "短语",
    sentence: "短句",
  });
  // 2–5 词 = 短语，6+ = 短句（panel-v2 mouseup 的预判档位）
  const PHRASE_MAX = 5;

  function predictKind(wordCount) {
    const count = Number(wordCount);
    if (!Number.isFinite(count) || count <= 1) return "word";
    if (count <= PHRASE_MAX) return "phrase";
    return "sentence";
  }

  // 拖选两端 → 有序区间（含端点）。从右往左拖（b < a）同样成立。
  function normalizeRange(a, b) {
    const x = Number(a);
    const y = Number(b);
    const lo = Number.isFinite(x) && Number.isFinite(y) ? Math.min(x, y) : 0;
    const hi = Number.isFinite(x) && Number.isFinite(y) ? Math.max(x, y) : 0;
    return { start: lo, end: hi };
  }

  // 所选文本 = 词序拼接（词 span 里带标点也算一个词，与 panel-v2 一致）
  function textOfRange(words, start, end) {
    return (words || [])
      .slice(start, end + 1)
      .map((word) => String(word == null ? "" : word))
      .join(" ");
  }

  // 覆盖词 index 的挑词，按「长度 → 起点 → kind」升序：最短 = 内层，最长 = 外层。
  // 同长时给确定序（先出现的在内层），渲染不因排序抖动。
  function coveringPicks(picks, index) {
    return (picks || [])
      .filter((pick) => {
        if (!pick) return false;
        const start = Number(pick.start);
        const end = Number(pick.end);
        return (
          Number.isFinite(start) &&
          Number.isFinite(end) &&
          index >= start &&
          index <= end
        );
      })
      .sort(
        (one, two) =>
          one.end - one.start - (two.end - two.start) ||
          one.start - two.start ||
          String(one.kind).localeCompare(String(two.kind))
      );
  }

  // 每个词的渲染档：bg = 最短覆盖挑词的 kind（内层底色），line = 最长覆盖挑词的
  // kind（外层弧线；与内层是同一个挑词时不画）。返回长度 wordCount 的数组。
  function computePickStyles(picks, wordCount) {
    const styles = [];
    for (let index = 0; index < wordCount; index += 1) {
      const covering = coveringPicks(picks, index);
      if (!covering.length) {
        styles.push({ bg: null, line: null });
        continue;
      }
      const inner = covering[0];
      const outer = covering[covering.length - 1];
      styles.push({
        bg: inner.kind,
        line: outer !== inner ? outer.kind : null,
      });
    }
    return styles;
  }

  // 计数（spec §D20）：已挑 = 词条数（全部 picks），待挑 = 零挑词且没有服务端
  // 已挑标记（T4 票 #210：picked 持久，重开页面后零 picks 的已挑句不计入待挑；
  // 本 session 的词条数前端拿不到，已挑词条数仍按 picks 算），共 = 句数。
  function pickSummary(sentences) {
    let picked = 0;
    let pending = 0;
    for (const sentence of sentences || []) {
      const count = (sentence && sentence.picks ? sentence.picks : []).length;
      picked += count;
      if (!count && !(sentence && sentence.picked)) pending += 1;
    }
    return { picked, pending, total: (sentences || []).length };
  }

  // 来源统计（spec §B2c，v33 T5 票 #211）：统计条按来源展示句数（cc/ocr/asr）。
  // 未知来源忽略（老中枢可能带别值）；零计数由调用方自行略去。
  function sourceSummary(sentences) {
    const counts = { cc: 0, ocr: 0, asr: 0 };
    for (const sentence of sentences || []) {
      const source = sentence && sentence.source;
      if (source && counts[source] !== undefined) counts[source] += 1;
    }
    return counts;
  }

  // v34 T6（票 #219 / spec A8）：词条在原句里的**字符区间** → **词下标区间**（含端点）。
  // 已保存态重建用：`findTargetSpan(context, target)` 给字符区间，本函数把区间覆盖到的
  // 非空白词段（`\S+`，标点算在词里，与挑词时的词切分一致）折成 start/end。
  // 空句 / 非法端点 / 纯空白 / 越界 → null（调用方跳过该词条，不瞎猜）。
  function charSpanToWordRange(text, start, end) {
    const source = String(text == null ? "" : text);
    const from = Number(start);
    const to = Number(end);
    if (!source || !Number.isFinite(from) || !Number.isFinite(to)) return null;
    const lo = Math.max(0, Math.min(from, to));
    const hi = Math.max(from, to);
    if (lo >= source.length) return null;
    let first = -1;
    let last = -1;
    let index = 0;
    const pattern = /\S+/g;
    let match;
    while ((match = pattern.exec(source)) !== null) {
      const wordStart = match.index;
      const wordEnd = match.index + match[0].length;
      if (wordStart < hi && wordEnd > lo) {
        if (first < 0) first = index;
        last = index;
      }
      index += 1;
    }
    if (first < 0) return null;
    return { start: first, end: last };
  }

  // 筛选 tabs（spec §D：全部 / 只看可疑 / 只看未挑）；未知档位回全部。
  function filterSentences(sentences, filter) {
    const list = sentences || [];
    if (filter === "suspicious") return list.filter((s) => s && s.suspicious);
    if (filter === "unpicked")
      return list.filter(
        (s) => !((s && s.picks ? s.picks : []).length || (s && s.picked))
      );
    return list.slice();
  }

  const PickPlan = {
    predictKind,
    normalizeRange,
    textOfRange,
    coveringPicks,
    computePickStyles,
    charSpanToWordRange,
    pickSummary,
    sourceSummary,
    filterSentences,
  };
  Object.defineProperty(PickPlan, "PICK_KIND_LABEL", {
    value: PICK_KIND_LABEL,
    enumerable: true,
  });

  if (typeof module !== "undefined" && module.exports) module.exports = PickPlan;
  if (root) root.PickPlan = PickPlan;
})(typeof window !== "undefined" ? window : null);
