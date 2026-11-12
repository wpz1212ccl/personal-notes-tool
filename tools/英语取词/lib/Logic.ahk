#Requires AutoHotkey v2.0

; ============================================================================
;  Logic.ahk —— 产品外壳的纯逻辑层（TDD 的预商定接缝）
;
;  本文件只有函数定义、零副作用：不建窗、不碰托盘、不拉进程、不改全局。
;  主脚本 英语取词.ahk 与测试 tests/test_logic.ahk 都 #Include 它。
;
;  逐条依据（v1）：docs/specs/capture-tool.md
;    §8.5 归一化 / §8.3 条目行序 / §7 猜词 / §6.2 出处 / §6.4 位置
;    §4.1 热键解析 / §11 守护状态转换 / §10.1 开机自启 / §10.2 日志滚动
;  v2 依据：docs/specs/v2-workbench.md
;    §6.3 速记框（环 / chip / 类型 / 键位表）/ §2 spool 与中枢 RPC / §4.1 词条字段
;    / §6.1 会话（chip 文案与离线退化）
;
;  v1 的 markdown 落盘函数（FrontmatterText / ReadmeText / EnsureVault /
;  AppendEntry / CaptureFilePath）在 #26 改成 spool 后已删：v2 起保存路径只写
;  `data/inbox.jsonl`，v1 markdown 的唯一消费者是 hub 的 `--import-vault`
;  （Python 侧，有测试钉住）。
; ============================================================================

; ---------------------------------------------------------------------------
;  归一化（spec §8.5）：单行化 + `::` → `：：`
;  `::` 一条来自 ADR 0001：obsidian-spaced-repetition 把含 `::` 的行整行当
;  单行卡（实测连 `> See also: http://x::8080` 都中招），所以正文除卡片行外
;  任何一行都不得出现 `::`。
; ---------------------------------------------------------------------------
NormalizeLine(s) {
    s := RegExReplace(s, "\s+", " ")          ; 连续空白（含换行）折叠成单空格
    s := StrReplace(s, "::", "：：")
    return Trim(s)                            ; Trim 只去空格制表符；此时首尾只剩单空格
}

; spec §8.3：标签空格分隔，自动给每个词元补 `#`
FormatTags(s) {
    s := NormalizeLine(s)
    if (s = "")
        return ""
    out := ""
    for t in StrSplit(s, " ") {
        if (t = "")
            continue
        if (SubStr(t, 1, 1) != "#")
            t := "#" . t
        out .= (out = "" ? "" : " ") . t
    }
    return out
}

; ---------------------------------------------------------------------------
;  猜词（spec §7）
; ---------------------------------------------------------------------------

; 读真词表 data/common_words.txt（一行一个小写高频词，前 2000 条）
; #102：值从恒 1 改成**词频序号**（1 = `the`，越小越常见）—— 近似档要按
; 「更短 → 更常见」排序（研究 §3-D5：同前缀不排序时 15.7% 要按 ↑↓ 换），
; 而 `Has()` 的语义不变（猜词 GuessTarget 只查存在性）。
LoadCommonWords(path) {
    m := Map()
    txt := ""
    try
        txt := FileRead(path, "UTF-8")
    catch
        return m
    for line in StrSplit(txt, ["`n", "`r"]) {
        w := Trim(line)
        if (w != "" && !m.Has(StrLower(w)))
            m[StrLower(w)] := m.Count + 1
    }
    return m
}

; v22 P4（票 #165 / spec §10.1）：新 token 定义 —— 按空白切，再「剥首尾 + 切内部杂符」：
;   · 首尾剥句读标点：`.,;:!?"'()[]{}<>«»…`（成对也去）；
;   · **内部**保留：`- ' , . $ % & / _ @ # + =`（其中 `,`/`.` 仅在两侧都是数字/字母时
;     保留，所以 `2,000`、`U.S.` 是整词；`word,` 的尾逗号已由首尾剥掉）；
;   · 词内**其它**杂符一律切开（不是只剥首尾）：镜头/字幕上 OCR 会把词间空隙读成 `»`
;     `(` `)` 这类符号（2026-10-19 用户现场：猜词把目标词填成 `For»example`、
;     `Excuse(me`、`tell)me`），整块保留等于把乱码写进词库；切开之后同一个句子里仍有
;     干净的词可选。规格 §7.1 的原意就是「剥掉非字母」，这里按原意收口；
;   · 只由标点组成的块（`---`、`?!`）不算独立 token：没有字母/数字 → 丢弃。
; 纯字母文本上新旧口径逐项相等（回归断言见 tests/test_logic.ahk 片 27）。
SplitTokens(row) {
    ; 快路径（绝大多数英文句/段落：「纯字母正文」）：行里没有数字 / 内部保留符号 /
    ; 连接符，也没有「字母,字母 / 字母.字母」这种内部句读时，新口径**逐项等价**于
    ; 旧的字母词元扫描（`[A-Za-z]+` 连续段），所以走最简单的那条 —— 按键延迟不退化
    ; 靠它（bench_completion V6 量的是这个场景）。
    if (!RegExMatch(row, "[\d$%&/_@#+='\-]|[A-Za-z][.,][A-Za-z]")) {
        out := [], pos := 1
        while (pos := RegExMatch(row, "[A-Za-z]+", &m, pos)) {
            out.Push(m[0])
            pos += StrLen(m[0])
        }
        return out
    }
    ; 慢路径（含数字/符号/连接符）：spec §10.1 新口径，一次扫描。
    ;   · token 主体 = 字母/数字 + 内部保留符号 `$%&/_@#+='-`；
    ;   · `.`/`,` 先宽松并进 token（`2,000`、`U.S.`），两侧不都是数字/字母的再切掉
    ;     （`_SplitTokenOnPunct` 后修，正常文本几乎不触发）；
    ;   · 开头 lookahead 要求后面必须出现字母/数字 —— 纯标点块（`---`、`?!`、`»`）
    ;     不会成为 token；匹配后再剥边界撇号（`'hello` → hello）。
    pat := "(?=[$%&/_@#+='\-]*[A-Za-z0-9])"
         . "[A-Za-z0-9$%&/_@#+='\-]+"
         . "(?:[.,][A-Za-z0-9$%&/_@#+='\-]+)*"
    out := [], pos := 1
    while (pos := RegExMatch(row, pat, &m, pos)) {
        pos += StrLen(m[0])
        tok := Trim(m[0], "'")
        if (tok = "")
            continue
        if (InStr(tok, ".") || InStr(tok, ",")) {
            for piece in _SplitTokenOnPunct(tok)
                out.Push(piece)
        } else
            out.Push(tok)
    }
    return out
}

; 慢路径后修：把「两侧不都是数字/字母」的 `.`/`,` 当切点（spec §10.1 步骤 3）。
; 只有含内部 `,`/`.` 的 token 才走这里（`2,000`/`U.S` 不受影响，正常文本几乎不触发）；
; 用一次正则标注切点，避免逐字符 RegExMatch 的调用开销。纯标点碎片丢弃。
_SplitTokenOnPunct(tok) {
    sep := Chr(1)
    t := RegExReplace(tok, "(?<![A-Za-z0-9])[.,]|[.,](?![A-Za-z0-9])", sep)
    if (!InStr(t, sep))
        return [tok]
    out := []
    for p in StrSplit(t, sep)
        if (p != "" && RegExMatch(p, "[A-Za-z0-9]"))
            out.Push(p)
    return out
}

; spec §7.2–7.4：跳过常见词/纯数字/长度<3 的词元，返回第一个剩下的（保留原始
; 大小写）；全被跳过则回退「最长词元」。
; 例（spec §7）：`I was reluctant to understand.` → `reluctant`。
; v17 W5（票 #142 / 决策 D1）：`suspects`（ParseSuspects 的产物，可省）里登记的疑似
; 粘连 token 不做预填 —— 目标词不该是一个坏串；跳过之后**选不出目标词**（没有
; 非疑似、非常见、≥3 字母的词元了）就回退现状（整段重来，不跳），绝不因为标记
; 而让目标词落空或落得更差。
GuessTarget(row, common, suspects := 0) {
    if (IsObject(suspects) && suspects.Length > 0) {
        found := _GuessTargetSkip(row, common, suspects)
        if (found != "")
            return found
    }
    best := "", bestLen := 0
    for t in SplitTokens(row) {
        if (StrLen(t) > bestLen)
            best := t, bestLen := StrLen(t)
        if (StrLen(t) < 3 || RegExMatch(t, "^\d+$"))
            continue
        if (common && common.Has(StrLower(t)))
            continue
        return t
    }
    return best
}

; GuessTarget 的「跳过疑似 token」趟：只找直接候选（非常见、≥3 字母），找
; 不到回空串（由调用方决定回退现状）。best 兜底不走这趟 —— 兜出来的常见词
; 不是用户要的目标词，宁可回退。
_GuessTargetSkip(row, common, suspects) {
    for t in SplitTokens(row) {
        if (IsSuspectToken(t, suspects))
            continue
        if (StrLen(t) < 3 || RegExMatch(t, "^\d+$"))
            continue
        if (common && common.Has(StrLower(t)))
            continue
        return t
    }
    return ""
}

; spec §4.3：原句里出现**独立的 `O` 词元**才提示（en-US 引擎把数字 0 读成
; 字母 O 的唯一系统性错误）。只提示、不自动改写（会误伤真词 O）。
HasLoneO(row) {
    for t in SplitTokens(row)
        if (t == "O")
            return true
    return false
}

; ---------------------------------------------------------------------------
;  出处（spec §6.2）
; ---------------------------------------------------------------------------

; 应用名友好化：内置小映射表，未命中的用去掉 .exe 的进程名
FriendlyApp(proc) {
    m := Map("chrome.exe", "Chrome", "msedge.exe", "Edge", "firefox.exe", "Firefox"
           , "Code.exe", "VS Code", "explorer.exe", "文件资源管理器"
           , "WindowsTerminal.exe", "Windows Terminal", "powershell.exe", "PowerShell"
           , "AutoHotkey64.exe", "AutoHotkey")
    return m.Has(proc) ? m[proc] : RegExReplace(proc, "\.exe$", "")
}

; `- 出处自动:` 的值：`<应用名> · "<窗口标题>"`（spec §6.2 的例子）
ProvenanceAuto(proc, title) {
    return FriendlyApp(proc) . ' · "' . title . '"'
}

; ---------------------------------------------------------------------------
;  位置 / 时间戳（spec §6.4）
; ---------------------------------------------------------------------------

; 秒 → `mm:ss` / `h:mm:ss`
FormatTimestamp(seconds) {
    s := Integer(seconds)
    hh := s // 3600
    mm := Mod(s, 3600) // 60
    ss := Mod(s, 60)
    return (hh > 0) ? Format("{:d}:{:02d}:{:02d}", hh, mm, ss)
                    : Format("{:d}:{:02d}", mm, ss)
}

; 「粘贴」按钮：剪贴板命中 `[?&]t=(\d+)s?`（YouTube 右键「复制当前时间的视频
; 网址」的形态）就换算成时间戳，否则把文本裁剪成单行直接填（spec §6.4）
PositionFromClipboard(text) {
    if (RegExMatch(text, "[?&]t=(\d+)s?", &m))
        return FormatTimestamp(m[1])
    return NormalizeLine(text)
}

; ---------------------------------------------------------------------------
;  遮罩拖框的两个端点坐标（spec §4.2）
;
;  两件事都是纯整数换算，所以放这一层（外壳只负责把消息里的值递进来）：
;
;  · `MouseLParamPoint(lp)`：`WM_LBUTTONDOWN` / `WM_LBUTTONUP` 的 `lParam` 是
;    `MAKELONG(x, y)` 的**客户区**坐标 —— 两个 16 位**有符号**字。多显示器时原点
;    可为负（第二屏摆主屏左边 → x < 0），所以必须按有符号解码：取低 16 位，
;    大于 0x7FFF 就减 0x10000（y 取次低 16 位，同理）。
;    2026-10-15 修复取词区域错位的根因：按下那一刻的坐标**只有消息里有**，
;    轮询 `GetKeyState` + `MouseGetPos` 会把检测晚 0~15ms 才补采一次坐标，
;    手一快整块框选就朝拖动方向缩掉十几到几十像素（实测 0/16/23ms）。
;
;  · `DragRect(x1, y1, x2, y2)`：§4.2 的归一化 —— `x=min(x1,x2)`、`w=abs(x1-x2)`。
;    不夹屏幕边界：越界的矩形交给内核侧 `pad_rect` 夹（贴边不补黑边）。
; ---------------------------------------------------------------------------
MouseLParamPoint(lp) {
    x := lp & 0xFFFF
    y := (lp >> 16) & 0xFFFF
    return { x: (x > 0x7FFF ? x - 0x10000 : x)
           , y: (y > 0x7FFF ? y - 0x10000 : y) }
}

DragRect(x1, y1, x2, y2) {
    return { x: Min(x1, x2), y: Min(y1, y2), w: Abs(x2 - x1), h: Abs(y2 - y1) }
}

; ---------------------------------------------------------------------------
;  v3 §7：速记框位置记忆（`box_pos`/`box_x`/`box_y` 三键的解析 + 可见性校正）
;
;  单位一律是**逻辑像素**（96dpi；与 config 里存的一致）—— 物理像素的换算
;  （`P()`/`D`）在外壳里做，这一层只管几何，好让 harness 拿假显示器矩形来钉。
; ---------------------------------------------------------------------------

; 三个 config 字符串 → `{ mode, x, y }`。只有 `mode=custom` 且 x/y 都是数字才算数：
; 键缺失 / 写坏 / mode 不认识 → 回落 `auto`（不报错、不静默停在非法坐标上）。
BoxPosConfig(mode, x, y) {
    if (Trim(mode) != "custom")
        return { mode: "auto", x: "", y: "" }
    if (!IsNumber(Trim(x)) || !IsNumber(Trim(y)))
        return { mode: "auto", x: "", y: "" }
    return { mode: "custom", x: Trim(x) + 0, y: Trim(y) + 0 }
}

; 可见性校正（§7）：`screens` = 各显示器的矩形（虚拟屏幕坐标系，逻辑像素），
; `w`/`h` = 框的尺寸（逻辑像素）。返回本次该用的落点：
;   · `auto`    —— 坐标不在**任何**显示器里（那块屏被拔了/布局变了）→ 交给调用方
;                  走现状（鼠标右下 + 越界翻转 + 夹进屏幕）；
;   · `custom`  —— 坐标在屏上 → 夹进虚拟屏幕包围盒（框的右/下边也留在屏内）。
BoxPosResolve(mode, x, y, screens, w, h) {
    cfg := BoxPosConfig(mode, x, y)
    if (cfg.mode != "custom" || screens.Length = 0)
        return { mode: "auto", x: "", y: "" }
    onScreen := false
    for s in screens {
        if (cfg.x >= s.x && cfg.x < s.x + s.w && cfg.y >= s.y && cfg.y < s.y + s.h) {
            onScreen := true
            break
        }
    }
    if (!onScreen)
        return { mode: "auto", x: "", y: "" }
    left := screens[1].x, top := screens[1].y
    right := screens[1].x + screens[1].w, bottom := screens[1].y + screens[1].h
    for s in screens {
        left := Min(left, s.x), top := Min(top, s.y)
        right := Max(right, s.x + s.w), bottom := Max(bottom, s.y + s.h)
    }
    return { mode: "custom"
           , x: Max(left, Min(cfg.x, right - w))
           , y: Max(top, Min(cfg.y, bottom - h)) }
}

; ---------------------------------------------------------------------------
;  多显示器最小修（票 #184 / A6；spec §3.2「外壳体验」组。spec §2 非目标：不做
;  完整多显示器重设计 —— 这里只把「虚拟屏坐标的换算/夹取」从外壳的系统调用里
;  剥出来）。单位一律**物理像素**：调用方喂 GetSystemMetrics(SM_XVIRTUALSCREEN=76
;  / SM_YVIRTUALSCREEN=77 / SM_CXVIRTUALSCREEN=78 / SM_CYVIRTUALSCREEN=79) 与
;  MonitorGet 的原样输出，本层不做 P() 换算（与上面 BoxPosResolve 的逻辑像素层
;  相区分）。主屏在左上的常规单屏里虚拟屏矩形就是 {0,0,W,H}，行为与旧主屏夹取
;  完全一致；左/上副屏的**负坐标**边界用例钉在 harness 片 38。
; ---------------------------------------------------------------------------

; 点 (px,py) 落在哪块显示器：`monitors` = MonitorGet 出的矩形数组（{x,y,w,h}）。
; 含点的那块；点在任何屏之外（屏缝 / 理论上 MouseGetPos 给不出的屏外值）回落
; `fallback`（调用方给虚拟屏矩形）—— 永远返回一个矩形，调用方不必再判空。
MonitorRectOf(monitors, px, py, fallback) {
    for m in monitors {
        if (px >= m.x && px < m.x + m.w && py >= m.y && py < m.y + m.h)
            return m
    }
    return fallback
}

; 把 w×h 的矩形**夹进** mon（四边各留 edge）。矩形比显示器还宽/高时钉在左/上
; edge 处 —— 与旧主屏夹取同形（Max(边+edge, Min(x, 对边-w-edge))）。
ClampRectInMonitor(x, y, w, h, mon, edge) {
    return { x: Max(mon.x + edge, Min(x, mon.x + mon.w - w - edge))
           , y: Max(mon.y + edge, Min(y, mon.y + mon.h - h - edge)) }
}

; 速记框落点（研究 §2.1 的口径搬到「鼠标所在屏」）：鼠标右下 offset 起步；x/y
; 越 mon 的右/下 edge 线就翻转到鼠标左/上方；最后整体夹进 mon（edge 内缩）。
; 主屏单屏 + 同参数 = 旧 PlaceBox 的逐字节等价（harness 片 38 钉）。
PlaceRectNearMouse(mx, my, w, h, mon, edge, offset) {
    x := mx + offset, y := my + offset
    if (x + w > mon.x + mon.w - edge)
        x := mx - w - offset
    if (y + h > mon.y + mon.h - edge)
        y := my - h - offset
    return ClampRectInMonitor(x, y, w, h, mon, edge)
}

; ---------------------------------------------------------------------------
;  热键解析 v2（v2 §9.2）；C5 起取代 v1 的 ParseHotkey。
;  config 的 AHK 语法串 → { ok, mods, vk, key, pass }；解析不了 → { ok:false, reason }。
;  修饰符 `^`=Ctrl、`!`=Alt、`+`=Shift、`#`=Win 写在基础键之前；基础键：
;    · A–Z / 0–9；
;    · 命名键 F1–F24 / Escape / Enter / Space / Tab / Home / End / PgUp / PgDn /
;      方向键（Left/Right/Up/Down）/ Numpad0–9（AHK 别名写法一并收：Esc、Return、
;      PageUp、PageDown）；
;    · allowSingleChar = true（框内键）时额外收**任意单字符**（如 `,`），VK 走
;      VkKeyScanW —— 全局键不收单标点，免得与正常文字输入打架。
;  `~`（放行前缀，票 #48 / B2）同样是基础键之前的写法：AHK 会把这次组合**原样**送
;  给系统（不吞触发键的 down）。Alt 系热键不加 `~` 时浏览器会收到一次「裸 Alt」→
;  Chromium 聚焦应用菜单「...」，之后的空格就废了（机制见
;  docs/research/v3-b1-mechanism.md）。`pass` 就是这个前缀的标记；注册串一律**原样
;  透传**（`Hotkey()` 认 `~`，不要重新拼串）。`~` 不参与 mods/vk —— 因此
;  `~!1` 与 `!1` 解析出同一个物理组合，冲突检测天然按同一组合判（§9.2）。
;  **绝不静默回落**：非法返回 reason，调用方拿它去点名（§9.2 冲突检测③；
;  静默回落就是「静默失效」，v1 §4.1 禁止）。`~` 单独、`~~!1`、`!1~`（前缀写在
;  基础键之后）都是非法并给 reason。
; ---------------------------------------------------------------------------
ParseHotkeyV2(spec, allowSingleChar := false) {
    s := Trim(spec)
    if (s = "")
        return { ok: false, reason: "键位为空" }
    mods := 0, pass := false, i := 1
    while (i <= StrLen(s)) {
        c := SubStr(s, i, 1)
        if (c = "^")
            mods |= 0x0002                  ; MOD_CONTROL
        else if (c = "!")
            mods |= 0x0001                  ; MOD_ALT
        else if (c = "+")
            mods |= 0x0004                  ; MOD_SHIFT
        else if (c = "#")
            mods |= 0x0008                  ; MOD_WIN
        else if (c = "~") {
            if (pass)                       ; `~~!1`：放行前缀写了两遍
                return { ok: false, reason: "放行前缀 ~ 只能写一次（如 ~!1）" }
            pass := true
        } else
            break                           ; 基础键从这里开始（`F12` / `A` / `1`）
        i++
    }
    key := SubStr(s, i)
    if (key = "")
        return { ok: false
               , reason: (pass && mods = 0) ? "只有放行前缀 ~，缺少基础键" : "只有修饰符，缺少基础键" }
    ; `~` 写在基础键之后（`!1~`）：那是把 `~` 当成了基础键的一部分，AHK 不认这种串
    if (InStr(key, "~"))
        return { ok: false, reason: "放行前缀 ~ 必须写在基础键之前（如 ~!1）" }
    if (StrLen(key) = 1) {
        if (RegExMatch(key, "^[A-Za-z0-9]$"))
            return { ok: true, mods: mods, vk: Ord(StrUpper(key)), key: StrUpper(key), pass: pass }
        if (allowSingleChar) {
            vk := VkOfChar(key)
            if (vk)
                return { ok: true, mods: mods, vk: vk, key: StrUpper(key), pass: pass }
            return { ok: false, reason: "单字符「" . key . "」没有可用的虚拟键码" }
        }
        return { ok: false
               , reason: "不认识的基础键「" . key . "」（全局键限 A–Z / 0–9 / 功能键 / 命名键）" }
    }
    ; F1–F24：VK 恒为 0x70+（F1=0x70），与修饰符无关（Windows 的定值）
    if (RegExMatch(key, "^[Ff](\d{1,2})$", &m)) {
        n := m[1] + 0
        if (n >= 1 && n <= 24)
            return { ok: true, mods: mods, vk: 0x70 + n - 1, key: "F" . n, pass: pass }
        return { ok: false, reason: "功能键越界「" . key . "」（限 F1–F24）" }
    }
    vk := NamedVk(key)
    if (vk)
        return { ok: true, mods: mods, vk: vk, key: CanonKey(key), pass: pass }
    return { ok: false, reason: "不认识的基础键「" . key . "」" }
}

; 命名键 → 虚拟键码（Windows 定值）。大小写不敏感；收 AHK 的常见别名写法。
; 集合与 settings.py 的 _NAMED_BASES **逐一对齐**：两边认得的基础键必须一致，
; 否则设置页能录、外壳却判「语法错误」（反之亦然），用户会以为功能坏了。
NamedVk(name) {
    static m := Map(
        "ESCAPE", 0x1B, "ESC", 0x1B, "ENTER", 0x0D, "RETURN", 0x0D
      , "SPACE", 0x20, "TAB", 0x09, "HOME", 0x24, "END", 0x23
      , "PGUP", 0x21, "PAGEUP", 0x21, "PGDN", 0x22, "PAGEDOWN", 0x22
      , "LEFT", 0x25, "UP", 0x26, "RIGHT", 0x27, "DOWN", 0x28
      , "BACKSPACE", 0x08, "DEL", 0x2E, "DELETE", 0x2E, "INSERT", 0x2D, "INS", 0x2D
      , "NUMPAD0", 0x60, "NUMPAD1", 0x61, "NUMPAD2", 0x62, "NUMPAD3", 0x63
      , "NUMPAD4", 0x64, "NUMPAD5", 0x65, "NUMPAD6", 0x66, "NUMPAD7", 0x67
      , "NUMPAD8", 0x68, "NUMPAD9", 0x69
      , "NUMPADADD", 0x6B, "NUMPADSUB", 0x6D, "NUMPADMULT", 0x6A
      , "NUMPADDIV", 0x6F, "NUMPADDOT", 0x6E)
    k := StrUpper(Trim(name))
    return m.Has(k) ? m[k] : 0
}

; 命名键的规范写法（状态文件与键位表都读它，免得 `Esc`/`Escape` 两种写法并存）。
CanonKey(name) {
    static canon := Map(
        "ESC", "Escape", "ESCAPE", "Escape",
        "ENTER", "Enter", "RETURN", "Enter",
        "SPACE", "Space", "TAB", "Tab",
        "HOME", "Home", "END", "End",
        "PGUP", "PgUp", "PAGEUP", "PgUp",
        "PGDN", "PgDn", "PAGEDOWN", "PgDn",
        "LEFT", "Left", "UP", "Up", "RIGHT", "Right", "DOWN", "Down",
        "BACKSPACE", "Backspace",
        "DEL", "Delete", "DELETE", "Delete",
        "INSERT", "Insert", "INS", "Insert",
        "NUMPADADD", "NumpadAdd", "NUMPADSUB", "NumpadSub"
      , "NUMPADMULT", "NumpadMult", "NUMPADDIV", "NumpadDiv"
      , "NUMPADDOT", "NumpadDot")
    k := StrUpper(Trim(name))
    if (canon.Has(k))
        return canon[k]
    if (RegExMatch(k, "^NUMPAD([0-9])$", &m))
        return "Numpad" . m[1]
    return name
}

; 单个可打印字符 → 虚拟键码（VkKeyScanW；参数是 WCHAR，传字符码而非字符串）。
; 0 = 解析不出（保留为语法错误）。
VkOfChar(c) {
    r := DllCall("VkKeyScanW", "UShort", Ord(c), "Short")
    if (r = -1)                            ; 0xFFFF：无映射
        return 0
    return r & 0xFF
}

; ParseHotkeyV2 的结果 → **规范化**键位串（修饰符按 AHK 习惯序 `^!+#` + CanonKey 基础键）。
; 用途：拼「同一组合的 up 形态」（`<key> Up`，2026-10-23 取消键抬起）。必须是规范名，
; 否则 `Esc Up` 与 `Escape Up` 会被 AHK 当成两个不同热键；`~` 放行前缀**不**带进来
; （放行是 down 侧的事，抬起侧要的就是「消费掉」）。解析失败 → 空串。
KeySpecOf(r) {
    if (!r.ok)
        return ""
    s := ""
    if (r.mods & 0x0002)                   ; MOD_CONTROL
        s .= "^"
    if (r.mods & 0x0001)                   ; MOD_ALT
        s .= "!"
    if (r.mods & 0x0004)                   ; MOD_SHIFT
        s .= "+"
    if (r.mods & 0x0008)                   ; MOD_WIN
        s .= "#"
    return s . r.key
}

; ---------------------------------------------------------------------------
;  目标词空判定（spec §8.5.3：为空 → 拒绝保存）
; ---------------------------------------------------------------------------
IsTargetBlank(s) => NormalizeLine(s) = ""

; ---------------------------------------------------------------------------
;  v2 速记框（docs/specs/v2-workbench.md §6.3）
; ---------------------------------------------------------------------------

; ---------------------------------------------------------------------------
;  config.ini 读取（spec §9）
;  不用 AHK 内建 IniRead：本机实测（中文 Windows、ANSI 代码页 936）IniRead 读不了
;  UTF-8 的 ini —— 段名「英语取词」的 UTF-8 字节被按 GBK 解出来，整段匹配不上，
;  于是**静默回落默认值**（#11 的探针就带着这个潜在 bug，只因默认值与 config.ini
;  一致没暴露）。内核侧用 Python 读 UTF-8（kernel.py 的 read_config），所以文件
;  必须保持 UTF-8；这里改成自己按 UTF-8 读，段名/键名两端才对得上。
; ---------------------------------------------------------------------------
ReadIniValue(path, section, key, default) {
    txt := ""
    try
        txt := FileRead(path, "UTF-8")
    catch
        return default
    if (SubStr(txt, 1, 1) = Chr(0xFEFF))          ; 容忍 UTF-8 BOM
        txt := SubStr(txt, 2)
    inSection := false
    for line in StrSplit(txt, ["`n", "`r"]) {
        l := Trim(line)
        if (l = "" || SubStr(l, 1, 1) = ";")      ; 空行与 ; 注释
            continue
        if (SubStr(l, 1, 1) = "[") {              ; 段头：换了段就切换归属
            inSection := (SubStr(l, 2, StrLen(l) - 2) = section)
            continue
        }
        if (!inSection)
            continue
        eq := InStr(l, "=")
        if (eq < 2)
            continue
        if (Trim(SubStr(l, 1, eq - 1)) = key)
            return Trim(SubStr(l, eq + 1))
    }
    return default
}

; ---------------------------------------------------------------------------
;  守护状态转换（spec §11：连接失败或请求超时即判内核已死 → 自动重启一次 +
;  toast + 托盘图标变灰；决策 D8：每次判死都重启一次（不是全程只一次），
;  重启后仍握手失败就回到 300ms×10s 重试节奏，不追加重启防风暴）
;
;  phase ∈ "alive" | "retrying"；event ∈ "death" | "ready" | "handshake-timeout"
;  返回 { phase, restart, icon, toast }：
;    icon  ∈ "normal" | "gray"（gray = 内核死，spec §11 的状态信号；暂停热键
;            不置灰 —— 决策 D9，两个信号不混用）
;    toast ∈ "" | "restarted" | "recovered"（主脚本把码映射成中文文案）
;
;  票 #29：**内核与中枢共用这一份**（每服务一条链 = 各自一个 phase 变量，
;  转换规则完全一样）；中枢不复制第二份状态机，只在下面加一层薄的 HubDeathAction。
; ---------------------------------------------------------------------------
WatchdogStep(phase, event) {
    if (event = "ready")
        return { phase: "alive", restart: false, icon: "normal", toast: "recovered" }
    if (event = "death") {
        ; 存活期判死要告知用户；重试期再死只记日志不弹 toast（否则内核起不来
        ; 时会每轮刷屏）
        toast := (phase = "alive") ? "restarted" : ""
        return { phase: "retrying", restart: true, icon: "gray", toast: toast }
    }
    if (event = "handshake-timeout")
        return { phase: "retrying", restart: false, icon: "gray", toast: "" }
    return { phase: phase, restart: false
           , icon: (phase = "alive" ? "normal" : "gray"), toast: "" }
}

; ---------------------------------------------------------------------------
;  中枢链的判死动作（票 #29）：在 WatchdogStep 之上收一层 —— 中枢的「判死」除了
;  socket 断开，还可能来自探测（连上了但一直不回话），而失败可能每个探测周期都
;  发生一次。所以**只有存活期判死才重启一次**；重试期再死一律不重启、不弹 toast
;  （防重启风暴）——「重试期再死不刷 toast」这条 AC 就落在这里。
;  phase ∈ "alive" | "retrying"；返回同 WatchdogStep 的 { phase, restart, icon, toast }。
; ---------------------------------------------------------------------------
HubDeathAction(phase) {
    step := WatchdogStep(phase, "death")
    if (phase != "alive")
        return { phase: step.phase, restart: false, icon: step.icon, toast: "" }
    return step
}

; ---------------------------------------------------------------------------
;  判死 → 待重启 → 下一拍重启（票 #69）。判死入口（Socket.ahk 的 OnMessage 回调，
;  第一行就是 `Critical`）**只许置位**：真正重启要跑 `taskkill` + `netstat`
;  （RunWait）与一个 Sleep 等端口循环，在 Critical 里跑完之前热键与托盘一律不响应。
;  所以回调只调这里 → 置「欠一次重启」位 + 派一枚一次性拍子；重启在那个拍子里跑。
;
;  event ∈ "death"（判死：socket 断开 / 探测超时）| "done"（拍子里的重启跑完）
;  返回 { pending, dispatch }：
;    pending  = 重启位（true = 还欠一次重启，回调只写它）
;    dispatch = 这次判死要不要派那枚一次性拍子
;  与 WatchdogStep 同款：内核与中枢各一条链（各自一个 pending 变量）、共用这一份。
; ---------------------------------------------------------------------------
RestartPhase(pending, event) {
    if (event = "death")
        ; 已经在位（上次的拍子还没跑 / 重启还没跑完）：重复判死**不叠加**
        ; ——否则一个崩溃的内核会被反复排重启（每个断连事件一枚拍子）
        return { pending: true, dispatch: pending ? false : true }
    if (event = "done")
        return { pending: false, dispatch: false }
    return { pending: pending, dispatch: false }
}

; ---------------------------------------------------------------------------
;  中枢存活探测的调度（票 #29：与 kernel 的 Tick/Watch 同一套手法 —— 定时 ping +
;  在途超时判死）。纯函数，harness 钉住。
;    pending  = 在途 ping 的发出时刻（0 = 没有在途）
;    lastSent = 上次发 ping 的时刻（0 = 从未发过）
;    now / interval / timeout = 时刻与阈值（ms）
;  返回 "dead"（在途 ping 超时 → 判死）/ "wait"（已有在途或还没到下一拍）/
;       "ping"（该发一枚）。
;  注意优先级：在途超时先于一切 —— 一个挂了、既不回话也不断开的连接，只有靠
;  「发了就等、等到超时」才判得出来（socket 的 onDisconnect 不会来）。
; ---------------------------------------------------------------------------
HubProbeStep(pending, lastSent, now, interval, timeout) {
    if (pending && now - pending > timeout)
        return "dead"
    if (pending)
        return "wait"
    if (lastSent && now - lastSent < interval)
        return "wait"
    return "ping"
}

; ---------------------------------------------------------------------------
;  内核就绪后的存活探测调度（票 #185 / A9；与 HubProbeStep 同一套手法：定时 ping +
;  在途超时判死，纯函数，harness 片 39 钉）。与 hub 版的差别就一处：inflight
;  （capture_ocr 或长文请求在途）时**不发也不判死** —— 内核忙起来 pong 会迟到
;  （引擎调用占着事件循环，研究报告 K4），那时判死是 Watch / LfLongWatch 的墙钟
;  管的事，探测不越权；在途结束后 pong 若仍无踪影，下一拍照样判得出来。
;  返回 "dead"（在途 ping 超时且空闲 → 判死）/ "wait"（在途 ping 没到点 / 该等
;  下一拍 / 有请求在途）/ "ping"（空闲且到了下一拍，该发一枚）。
; ---------------------------------------------------------------------------
KernelProbeStep(pending, lastSent, now, interval, timeout, inflight) {
    if (pending && now - pending > timeout && !inflight)
        return "dead"
    if (pending)
        return "wait"
    if (inflight)
        return "wait"
    if (lastSent && now - lastSent < interval)
        return "wait"
    return "ping"
}

; ---------------------------------------------------------------------------
;  toast 叠层的落点（票 #185 / A14；纯函数，harness 片 43 钉）。
;  heights = 自底向上每枚活动 toast 的物理高（第 1 枚最底）；gap = 相邻两枚的缝；
;  baseY = 最底一枚的**底边** y（虚拟屏右下起算，避开任务栏的那个点）。
;  返回同序的每枚**顶边** y：第 i 枚的底边 = baseY − 它下面那些枚的高与缝之和。
;  调用方在入队与出队后都用同一份 heights 重算 —— 自灭那枚的空位由上面的枚
;  下移补上（回收）。
; ---------------------------------------------------------------------------
ToastStackY(heights, gap, baseY) {
    ys := []
    bottom := baseY
    for ht in heights {
        ys.Push(bottom - ht)
        bottom -= ht + gap
    }
    return ys
}

; ---------------------------------------------------------------------------
;  开机自启单一入口（spec §10.1）
;  Startup 里只放一个指向**外壳**的快捷方式：目标 = AutoHotkey64.exe 全路径、
;  参数 = 带引号的脚本全路径、工作目录 = 工具目录。内核由外壳拉起，不占第二个
;  入口（§10.1 的单所有者，避免两个快捷方式各自启动的竞态）。
;  这三件套是纯函数；实际创建 .lnk 的 I/O（WScript.Shell COM）在主脚本，
;  且幂等：已存在就不重建。
; ---------------------------------------------------------------------------

StartupLinkPath(startupDir) {
    return startupDir . "\英语取词.lnk"
}

StartupShortcut(ahkExe, scriptPath) {
    SplitPath(scriptPath, , &toolDir)
    return { target: ahkExe, args: '"' . scriptPath . '"', workDir: toolDir }
}

; 自启设置（config `autostart`）：缺键/空 = 开（老用户升级后行为不变）；
; "0" / "off" / "false" = 关。开关本身在托盘菜单，真实状态由 Startup 里那个
; .lnk 的存在表达，config 只记住用户的意图（否则下次启动会把删掉的快捷方式
; 又建回来，用户的选择被静默推翻）。
AutostartEnabled(value) {
    v := Trim("" . value)
    return !(v = "0" || v = "off" || v = "false")
}

; ---------------------------------------------------------------------------
;  v2 速记框（docs/specs/v2-workbench.md §6.3）
;
;  形态：宽 400 lp、折叠态高 ≤170 lp（v1 是 480×~400）；三行常显（目标词 /
;  音标+释义 / 原句）+ 按钮行，`F2` 才展开元数据（出处/位置/标签）；去逐字段
;  标签、改灰色占位提示（EM_SETCUEBANNER）；默认深色。
;
;  本节的函数全是纯函数：GUI 事件只做「读控件 → 调这里 → 填回去」，环、chip、
;  类型推断、键位表文案一律不写死在事件回调里（票 #24 的硬要求，harness 钉住）。
; ---------------------------------------------------------------------------

; 焦点环（§6.3）：折叠 = 目标词 → 释义 → 原句 → 类型 chip → 会话 chip；
; 展开时把 出处 → 位置 → 标签 插在 原句 与 类型 chip 之间
; （§6.3 原文：目标词 → 释义 → 原句 →（展开时）出处 → 位置 → 标签 → 类型 chip →
;   会话 chip → 回到目标词）。
; 返回稳定字段名，GUI 用同一个名字索引控件（C.%name%）。
FieldRing(expanded) {
    if (expanded)
        return ["target", "meaning", "context", "prov", "pos", "tags", "kind", "session"]
    return ["target", "meaning", "context", "kind", "session"]
}

; cur 在环里的下标（1 起）；不在环里（含空串）→ 0
RingIndex(cur, ring) {
    for idx, f in ring {
        if (f = cur)
            return idx
    }
    return 0
}

; `Tab` / `↓` 的方向；走到末项回绕到环首。cur 不在环里 → 环首（§6.3 的四个方向都走环）。
NextField(cur, expanded) {
    ring := FieldRing(expanded)
    return ring[Mod(RingIndex(cur, ring), ring.Length) + 1]
}

; `Shift+Tab` / `↑` 的方向；走到首项回绕到环末。cur 不在环里 → 环末。
PrevField(cur, expanded) {
    ring := FieldRing(expanded)
    i := RingIndex(cur, ring)
    i := (i = 0) ? 1 : i
    return ring[Mod(i - 2 + ring.Length, ring.Length) + 1]
}

; ---------------------------------------------------------------------------
;  句内补全（v4-capture-and-recall §3.1 / §3.2 / §3.3，票 #50 A1；v5 §4.3 归一扩展）
;  来源 = **原句框当前值**；目标 = 目标词框里已打的前缀。全是纯函数：
;  外壳只做「读控件 → 调这里 → 填回去」。
; ---------------------------------------------------------------------------

; v5-capture-automation §4.3：网页/OCR 复制来的**排版标点**（弯引号 `’‘`、长破折号
; `–—`、减号 `−`、连字符 `‐`、上标撇 `′´`）会让 `don’t` 这类词补全失效 —— 分词与
; 比较前把原句与输入都归一成键盘上打得出的 ASCII 形态。只换标点、不动字母大小写。
NormalizePunct(s) {
    s := RegExReplace(s, "[’‘‚‛′´]", "'")
    ; v17 W1（票 #139）：连字符类补齐 U+2011 NON-BREAKING HYPHEN、U+2012 FIGURE DASH、
    ; U+2015 HORIZONTAL BAR —— 内核行级重建会产出 U+2011（kernel.py _LINE_HYPHENS 同集合），
    ; 不归一的话 `well‑known` 只能补出半截 `well`。
    s := RegExReplace(s, "[–—−‐‑‒―]", "-")
    return s
}

; v22 P4 词形归一（票 #165 / spec §10.1）：Fold(w) = 小写化 + 去 `'`/`-` **仅当两侧都是
; 字母**（数字/符号旁原样保留，避免 `2-000` 与 `2,000` 误判相等）。纯字母词形与旧口径
; 逐项一致：`dont` ≡ `don't`、`wellknown` ≡ `well-known`、`stateoftheart` ≡ `state-of-the-art`；
; `COVID-19` 的连字符保留，但输入 `COVID-1` 同样保留，所以前缀补全照旧命中。
Fold(w) {
    s := StrLower(w)
    ; 快速路径：绝大多数词元没有连接符，省掉一条正则（bench_completion 的按键延迟靠它）
    if (InStr(s, "'") || InStr(s, "-"))
        s := RegExReplace(s, "(?<=[A-Za-z])['-](?=[A-Za-z])", "")
    return s
}

; v22 P4 review 修复轮：**前缀比较**专用的 Fold —— typed 末尾的 `'`/`-` 只有在
; **前一个字符是字母**时才裁（`don'` → don、`well-` → well，保住 W1 的半截词补全）；
; 数字/符号后不裁：`2-` → `2-`，按 spec §10.2「前缀匹配按 Fold 口径」不能命中 `2,000`
; （否则输入 `2-` 会错误补出 `2,000`）。三个前缀比较点共用这一个口径。
FoldPrefix(w) {
    s := RTrim(w, "'-")
    if (s != "" && RegExMatch(SubStr(s, StrLen(s), 1), "[A-Za-z]"))
        return Fold(s)
    return Fold(w)
}

; ---------------------------------------------------------------------------
;  #102（v9 研究 §3-D5 / §5 P2-1）：Tab 补全的**两档**候选 + 排序
;
;  v4 的 `Completions` 只会「回声原句」：真词被 OCR 读坏 / 粘进邻居词元时补不到。
;  §3-D5 用真机取词库 393 个纯字母目标词离线回放：10.4% 的真词根本不在原句；
;  15.7% 要按 ↑↓ 换才轮到；2% 首个候选是 `normsand` / `fitbit` 这类词表查不到的
;  粘连串。所以候选改成顺序稳定的两档（第三档「句外常用词兜底」已被用户现场否决，见下）：
;    ① 原句里以输入为前缀的整词（现状，**句序不动**）；
;    ② 前缀一个都没命中时，原句里与输入编辑距离 ≤2 的**词表可查**词元
;       （§3-D5 实测：可救回 15/47 = 32% 的「真词不在原句」）。
;
;  ［2026-10-19 实测改判：① 档不排序、也不按 OOV 降权］本票 393 词离线回放的
;  实际数字：句序首中 300/355 = 84.5%；改成「更短 → 更常见」排序 281/355 = 78.9%；
;  按 OOV 降权 280/355 = 78.7%；两者都做 276/355 = 77.5%。原因是仓库这两份词表
;  （common 2000 ∪ words_10k）里 271/393 = 69% 的真词根本查不到，降权/排序会把
;  用户真正要补的那个词压到后面。票面 AC 是「首中率不低于 84.0%」，所以 ① 档
;  保持现状；排序只用于 ② 档（距离 → 更短 → 更常见 → 句序）。
;  OOV 校验落在 ② 档：它只把**能查到的**词元吸进来，不会把 `normsand` 这类
;  粘连串变成新候选。① 档的 OOV 保持原样（不修 ① 就无法同时守住 84%）。
;
;  ［2026-10-19 用户现场：第三档「句外常用词兜底」已删除］用户实测打 `av` 被补成
;  `available`，而原句里根本没有 available —— 「Tab 补出了不在句子中的词」，与
;  「句内补全」的语义和它的提示文案直接矛盾。兜底档整块删掉，`common` 现在只用于
;  ② 档排序；句内没有候选时，提示行照旧说「句内没有这个词（照打即可）」。
;
;  词表是**注入**的（known = 校验集，common = 排序参考）：本文件仍为零 I/O 纯函数。
;  只传 `(sentence, typed)` 的旧签名完全退回 v4 语义（无吸附、句序），
;  旧调用点与旧测试原样可用。外壳侧传的是 G_Common ∪ words_10k 与 G_Common。
; ---------------------------------------------------------------------------

; 编辑距离（只关心 ≤2）：返回 0/1/2，>2 返回 -1。
; 阈值取 2 的依据（§3-D5 的实测例子）：`physiotnerapist`→`physiotherapist` 只差
; 1 个字符，而 `incentiveto`→`incentive` 要删掉粘进来的 `to`（距离 2）—— 阈值 1
; 会漏掉后者，而「词粘连」正是 OCR 结构性的坏法；阈值 3 又太宽（`result`/`resort`
; 距离 2、`cat`/`cut` 距离 1，短词会互相吸附）。
EditDistanceAtMost2(a, b) {
    if (a = b)
        return 0
    la := StrLen(a), lb := StrLen(b)
    if (Abs(la - lb) > 2)
        return -1
    prev := []
    Loop (lb + 1)
        prev.Push(A_Index - 1)
    Loop la {
        i := A_Index
        cur := [i]
        Loop lb {
            j := A_Index
            cost := (SubStr(a, i, 1) = SubStr(b, j, 1)) ? 0 : 1
            cur.Push(Min(cur[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        }
        prev := cur
    }
    return (prev[lb + 1] <= 2) ? prev[lb + 1] : -1
}

; 候选记录的稳定排序：key 越小越靠前。key 里都带「句中位置」消歧，所以同分稳定。
SortByKey(items) {
    out := []
    while (items.Length > 0) {
        best := 1
        Loop items.Length {
            if (items[A_Index].key < items[best].key)
                best := A_Index
        }
        out.Push(items[best])
        items.RemoveAt(best)
    }
    return out
}

; 常见词表里的名次（越小越常见）；不在表里的排在所有常见词之后。
CommonRank(low, common) {
    if (IsObject(common) && common.Has(low))
        return common[low]
    return 999999
}

; v17 W2（票 #140 / 决策 D3）近似档虚词阈值：common 名次 ≤ 此值的高频虚词
; （the/for/was 这类）不进近似候选。真机回放里 fuzzy 对「补出目标」贡献为 0，
; 却贡献了 19/24 条整词改写，而虚词首吸（thre→the、port→for）全在这个名次区间。
FUZZY_STOPWORD_RANK := 100

; v17 W2 整词守卫谓词：句内存在任一**完整 token** 与 ft（Fold 后的输入）相等 → true。
; `CompletionTiers` 的近似档与 `CompletionMissHint` 的「整词已在句内」判定共用，
; 两侧永远同判。精确相等是 Fold 相等的子集，所以只比 Fold；调用方保证输入已归一
; （NormalizePunct + NormalizeLine）、无空白、ft = Fold(t)。
FoldEqualsAnyToken(toks, ft) {
    for w in toks {
        if (Fold(w) = ft)
            return true
    }
    return false
}

; 三档候选 + 来源。返回 { words: [...], sources: [...] }，两数组一一对应：
;   sources ∈ { sentence, fuzzy, common }（提示行据此标「句内 / 近似 / 常用词」）。
CompletionTiers(sentence, typed, known := 0, common := 0) {
    t := NormalizeLine(NormalizePunct(typed))
    ; v22 P4（票 #165 / spec §10.2）输入闸门：空 → 无候选；单字符**仅当**含
    ; `[0-9$%&/\\_-]` 时允许（`2`、`$`、`5` 能补出 `2,000` / `$2,000` / `50%`，纯字母
    ; 单字仍无候选）；≥2 照旧。含空白（用户在打短语）仍无候选 → 交给 chain。
    if (t = "" || RegExMatch(t, "\s"))
        return { words: [], sources: [] }
    if (StrLen(t) = 1 && !RegExMatch(t, "[0-9$%&/\\_-]"))
        return { words: [], sources: [] }
    ft := Fold(t)
    ; v22 P4：typed 末尾的连接符是最常见的中途态（弯引号输入的 `don’` 归一成 `don'`、
    ; 半截的 `well-`）。新 Fold 只在两侧都是字母时去连接符，`don'` 会折成 `don'`；
    ; **前缀比较**改走 FoldPrefix：只在连接符前面是字母时裁（`don'`→don、`well-`→well），
    ; 数字/符号后不裁 —— `2-` 不会被裁成 `2` 去误配 `2,000`（review 修复轮）。
    fp := FoldPrefix(t)
    if (fp = "")
        return { words: [], sources: [] }
    s := NormalizeLine(NormalizePunct(sentence))
    toks := SplitTokens(s)

    ; 候选入选（v17 W1 统一口径）：Fold(cand) 以 Fold(前缀) 开头，且原形态更长、
    ; 原形态不等于前缀（忽略大小写）；输出**原句形态**，去重键用 Fold（首次出现优先，
    ; `well-known` 与 `wellknown` 不重复出现）；RTrim 兜底保证候选不带尾随 `'`/`-`。
    ; 旧签名（不传词表）：v4 的单档句序回声，比较口径同为 Fold。
    if (!IsObject(known) && !IsObject(common)) {
        words := [], seen := Map()
        for w in toks {
            fw := Fold(w)
            if (StrLen(w) <= StrLen(t) || StrLower(w) = StrLower(t))
                continue
            if (SubStr(fw, 1, StrLen(fp)) != fp || seen.Has(fw))
                continue
            seen[fw] := true
            words.Push(RTrim(w, "'-"))
        }
        srcs := []
        Loop words.Length
            srcs.Push("sentence")
        return { words: words, sources: srcs }
    }

    ; ① 前缀直命中：**保持句序**（见文件头 2026-10-19 的实测改判）。不在这里做
    ; OOV 降权/排序 —— 仓库词表只覆盖 31% 的真词，任何重排都会把真词压后。
    direct := [], seen := Map()
    for w in toks {
        fw := Fold(w)
        if (StrLen(w) <= StrLen(t) || StrLower(w) = StrLower(t))
            continue
        if (SubStr(fw, 1, StrLen(fp)) != fp || seen.Has(fw))
            continue
        seen[fw] := true
        direct.Push({ word: RTrim(w, "'-") })
    }

    ; ② 近似吸附：只在「前缀一个都没命中」时启用（有直命中时用户要的就是原句那个
    ; 词，再吸附只会把候选搅浑）。候选必须是**词表可查**的词 —— 否则把
    ; `physiotnerapist` 这种坏词元吸进来，等于换一种方式填垃圾（票面 AC 的第 2 条）。
    ; 排序：距离 → 更短 → 更常见（common 名次）→ 句中更靠前。
    ; 另加 ≥4 字母门槛：2–3 字母时距离 2 足以把 `cat`/`cut` 这类无关短词吸进来
    ; （噪声大于收益），而粘连串与手误都是长输入，门槛拦不到它们。
    ; v17 W2（票 #140 / 决策 D3）再加两条守卫 —— 只收窄入选集合，
    ; 来源标签、档内排序方向与提示字面量一律不动：
    ;   · 整词守卫：输入（Fold 后、无空白）与句内任一完整 token 相等 → 整档不启用。
    ;     打完 hose/bugs/this 再按 Tab 不再被换成 have/but/his；direct 档照旧
    ;     （`read → reading` 保留，守卫谓词与 MissHint ① 共用）。
    ;   · 质量门（全部满足才入选）：候选长度 ≥ max(4, 输入长度-1)；候选与输入
    ;     首字母相同（输入侧已是 Fold 形态，天然忽略大小写）；候选不是高频虚词
    ;     （common 名次 > FUZZY_STOPWORD_RANK）；编辑距离 2 只允许「候选比输入长」，
    ;     等长/更短只允许距离 1（#102 的粘连型吸附整类撤回 —— 真机 fuzzy 补出
    ;     目标为 0，宁缺勿改）。
    fuzzy := []
    if (direct.Length = 0 && IsObject(known) && StrLen(ft) >= 4
     && !FoldEqualsAnyToken(toks, ft)) {
        idx := 0
        fseen := Map()
        lt := StrLower(t)
        minLen := Max(4, StrLen(ft) - 1)          ; W2 质量门：候选长度下限
        for w in toks {
            idx += 1
            lw := StrLower(w)
            if (StrLen(w) < 3 || lw = lt || fseen.Has(lw) || !known.Has(lw))
                continue
            if (StrLen(lw) < minLen)
                continue                          ; 太短（bugs↛but、thre↛the）
            if (SubStr(lw, 1, 1) != SubStr(ft, 1, 1))
                continue                          ; 首字母不同（port↛for/sort）
            if (CommonRank(lw, common) <= FUZZY_STOPWORD_RANK)
                continue                          ; 高频虚词
            ; 距离按 Fold 口径比（W1）：句内词元过词表门后不含连接符，Fold ≡ 小写；
            ; 输入带 `'`/`-` 时归一口径才不会把同一词算成距离 2。
            d := EditDistanceAtMost2(ft, lw)
            if (d < 0 || (d = 2 && StrLen(lw) <= StrLen(ft)))
                continue                          ; 距离 2 只允许候选更长（hose↛have）
            fseen[lw] := true
            key := d * 1000000000000000 + StrLen(lw) * 1000000000000 + CommonRank(lw, common) * 1000000 + idx
            fuzzy.Push({ word: RTrim(w, "'-"), key: key })
        }
        fuzzy := SortByKey(fuzzy)
    }

    ; ［2026-10-19 用户现场后撤掉第三档「常用词兜底」］原设计里 ①② 都空时会从 2000 常见词表
    ; 补出**句外词**（打 `av` → `available`，而原句里根本没有 available）。用户实测反馈：
    ; 「tab 的词会出现改词并不在句子中的情况」—— 这违背了「句内补全」这个语义，且用户无法
    ; 分辨补出来的词来自原句还是猜测。**兜底档已删**：`common` 现在只用于 ② 档的排序，
    ; 不再产生候选；句内没有就明确告诉用户「句内没有这个词（照打即可）」。
    words := [], srcs := []
    for rec in direct {
        words.Push(rec.word)
        srcs.Push("sentence")
    }
    for rec in fuzzy {
        words.Push(rec.word)
        srcs.Push("fuzzy")
    }
    return { words: words, sources: srcs }
}

; §3.1 句内补全候选（零 I/O）。返回句中原始形态的候选数组。
;   - 输入闸门（v22 P4 / spec §10.2）：空 → 无候选；单字符仅当含 `[0-9$%&/\\_-]`
;     （`2`、`$`、`5`）；≥2 照旧；含空白（用户在打短语）→ 无候选；
;   - 词元 = SplitTokens 语义（v22 P4 / spec §10.1：首尾剥句读标点、内部保留
;     `- ' , . $ % & / _ @ # + =`，`2,000` / `$2,000` / `50%` 都是整词；候选输出前
;     还有 RTrim 兜底，不带尾随 `'`/`-`）；
;   - 候选：Fold(cand) 以 FoldPrefix(前缀) 开头（末尾连接符仅字母后裁，其余同 Fold：
;     忽略 `'`/`-` 与大小写，数字/符号旁保留），且原形态长度 > 前缀、原形态不等于前缀；
;     忽略 Fold 去重，保留句中**首次出现**的形态（句首大写词补出 `Reinforce` 属预期）；
;   - 前缀等于句中某个完整单词（含 Fold 相等且长度相同的情形）时，该词自己不再作为
;     候选 —— 「用户已打出整词就不再补全」。
; v5 §4.3：原句与输入都先过 NormalizePunct 再分词/比较（其余语义一律不变）。
; #102：known / common 是带默认值的可选参数 —— 旧签名 `Completions(s, t)`
; 仍是 v4 语义（单档句序回声，比较口径同为 Fold）；外壳传词表时走 CompletionTiers。
Completions(sentence, typed, known := 0, common := 0) {
    return CompletionTiers(sentence, typed, known, common).words
}

; ---------------------------------------------------------------------------
;  顺句接词（Tab 连打）：把原句里的相邻词一个个接上来（v4 §3.5）
;
;  v4 §3 的补全只服务「半截词收尾」：一个词补齐，候选就没了（§3.2 原文是「再按 Tab 正常
;  进下一字段」）。要抄一整段短语 / 短句时，这就等于每个词都得手打 —— 于是加一条**顺句**：
;  原句框里那段话当词库，`Tab` 一次接上紧挨着的下一个词。
;
;  状态标记只有**框末尾的一个空格**，不新增全局变量：
;    · 接受一个候选 / 接上一个词之后，框文本 = `<词…> `（末尾留一个空格）；
;    · 末尾有空格 → 下一次 Tab 拿「已接上的那串词」回原句定位，接它后面那个词；
;    · 末尾没空格（用户手打，或把空格删了）→ 只做「短语中间半截词收尾」，不整串往后推。
;  为什么用空格当标记而不是记一个游标：控件文本是唯一事实来源 —— 用户删词、改词、从句子
;  中间起头都不会失步；游标要额外管失效，状态越多越容易漂。
;
;  定位每次都重算：把框里的词元（`SplitTokens`，与猜词同一套剥标点规则）当成一段连续词序，
;  在原句词序里找出现位置，取紧随其后的那个词。两条分支对「重复出现」的口径不同（#202）：
;    · ① 整串已接完、要接下一个词 —— 仍取**首次**出现（用户顺读时往前看更可预期；
;      #204 决策 B 已拍板保留这条语义，代价由「第 N 处 / 共 M 处」的可见提示承担 ——
;      判据见 ChainOccurrenceText）；
;    · ② 末词是半截词、要就地补完 —— 遍历**所有**出现位置：锚点常是 `a`/`the`/`of`
;      这类高频虚词，首现几乎必然不是眼前那处；只取首现会静默 no-op，甚至接错词。
;
;  为什么单 token 绝不接词：`Tab` 同时是字段环键（v4 §3.2 回归红线）。一个孤零零的词
;  （尤其是 OCR 猜词预填的那一个）按 Tab 十有八九是想去释义框，不是想接词；这两种意图在
;  「一个整词 + 无尾空格」上无法区分，所以一律按老语义走环。
; ---------------------------------------------------------------------------

; 顺句接词的**下一步**：0（不接）或 `{ word, replaceLast, occ, total }`。
;   replaceLast = true  → 框末尾是半截词，把它补成原句里紧挨着的那个词；
;   replaceLast = false → 这个词已经接完，追加原句里的下一个词。
;   occ / total（#205，仅 ① 追加档带）= 这一接用的是锚点的第几处 / 锚点共出现几处；
;   `total > 1` 就是「重复锚点」—— 选词仍是首现（见下），但调用方要把歧义说出来。
; v17 W1（票 #139）：锚点「精确优先、Fold 回落」—— 先按现状做大小写不敏感的精确
; 匹配；精确无解时允许「框里连续 k 个词」与「原句里连续 m 个词」在 Fold 后拼接相等
; （`well known` ≡ `well-known`、`dont` ≡ `don't`），起点/终点都落在原句 token 边界。
ChainStep(sentence, typed) {
    stoks := SplitTokens(NormalizePunct(sentence))
    ttoks := SplitTokens(NormalizePunct(typed))
    if (ttoks.Length = 0 || stoks.Length = 0)
        return 0
    n := ttoks.Length

    ; ① 末尾有空白 = 上一个词已接完（或用户自己打了空格，意图一样）→ 整串当锚
    ; #205（上游 #204 决策 B）：仍取首现，只是把「第几处 / 共几处」一起带出去 ——
    ; 唯一带得出这个数的地方就是 FindWordRuns 的全部起点（单值版拿不到总数）。
    if (RegExMatch(typed, "\s$")) {
        runs := FindWordRuns(stoks, ttoks, n)
        if (runs.Length > 0) {
            p := runs[1]
            return (p + n > stoks.Length) ? 0
                 : { word: stoks[p + n], replaceLast: false, occ: 1, total: runs.Length }
        }
        frs := FindFoldRuns(stoks, ttoks, n)    ; 精确无解 → Fold 回落
        if (frs.Length = 0 || frs[1].p + frs[1].span > stoks.Length)
            return 0                            ; 原句走完了 → 不接（Tab 走环）
        return { word: stoks[frs[1].p + frs[1].span], replaceLast: false
               , occ: 1, total: frs.Length }
    }

    ; ② 末尾是半截词：前 n-1 个词当锚，把半截词补成紧挨着的那个词。
    ; 不改大小写形态 —— 候选词用原句里的形态（与 v4 §3.1 的补全口径一致）。
    ; #202：锚点要遍历**全部**出现位置（`a` 在长句里出现 4 次是常态），按 Fold 去重后
    ; 恰好 1 个词才收尾；≥2 个不同词 → 歧义不猜（no-op 可读可重打，猜错就是静默改词）。
    k := n - 1
    if (k < 1)
        return 0
    ; 半截词前缀比较：走 FoldPrefix（`I don’` → `don'` → don；`well-` → well；
    ; `2-` 前面是数字 → 不裁，不会误配 `2,000`）
    lc := FoldPrefix(ttoks[n])
    cand := "", seen := Map()
    for r in ChainTailRuns(stoks, ttoks, k) {
        if (r.p + r.span > stoks.Length)
            continue                            ; 锚点就是句尾，后面没词可接
        w := stoks[r.p + r.span], lw := Fold(w)
        if (StrLen(lc) >= StrLen(lw) || SubStr(lw, 1, StrLen(lc)) != lc)
            continue                            ; 既不是前缀也不是整词 → 这个位置不算
        if (seen.Has(lw))
            continue                            ; 同词多处（大小写/连字符差异）只算一个
        seen[lw] := true
        if (cand != "")
            return 0                            ; 不同词 ≥ 2 个 → 歧义，不猜
        cand := w
    }
    return (cand = "") ? 0 : { word: cand, replaceLast: true }
}

; #202：② 末词半截的锚点集合 —— 锚点**全部**出现位置，统一成 `{ p, span }`。
; 冠词/介词当锚点时（`a` / `the` / `of`）首现几乎必然不是用户眼前那一处：旧实现只
; 取首个，于是要么静默 no-op，要么在 ① 里静默接错词。这里只负责「找齐」；
; 精确档优先、一个都没有才回落 Fold 档 —— 放宽的是位置，不是匹配强度。
ChainTailRuns(stoks, ttoks, k) {
    runs := []
    for p in FindWordRuns(stoks, ttoks, k)
        runs.Push({ p: p, span: k })
    return (runs.Length = 0) ? FindFoldRuns(stoks, ttoks, k) : runs
}

; v17 W1：Fold 回落锚点的**全部起点** —— 在 stoks 里找一段**连续词**，其 Fold 拼接恰
; 等于 ttoks 前 k 个词的 Fold 拼接（连接符/大小写差异忽略）。返回 `[{ p, span }]`
; （1 起下标 + 跨越的原句词数），空数组 = 没有。span 按每个起点各自收敛（最短），
; 不跨起点挑。① 与 ② 都用「全部起点」：② 要遍历所有位置判歧义（#202），① 要数
; 「共几处」（#205）—— 选词仍取首个，判据只写在这一处。
FindFoldRuns(stoks, ttoks, k) {
    out := []
    if (k < 1 || k > ttoks.Length)
        return out
    want := ""
    Loop k
        want .= Fold(ttoks[A_Index])
    Loop stoks.Length {
        p := A_Index
        got := ""
        Loop (stoks.Length - p + 1) {
            got .= Fold(stoks[p + A_Index - 1])
            if (StrLen(got) > StrLen(want))
                break
            if (got = want) {
                out.Push({ p: p, span: A_Index })
                break
            }
        }
    }
    return out
}

; 在 stoks 里找 ttoks 前 k 个词的**全部**连续出现（忽略大小写）；返回起始下标数组
; （1 起，升序），空数组 = 没有。匹配判据只有这一份（`StrLower` 比较、越界即空）。
; ① 取 `[1]` 当「首现」、`Length` 当「共几处」（#205）；② 遍历全部位置判歧义（#202）。
FindWordRuns(stoks, ttoks, k) {
    out := []
    if (k < 1 || k > stoks.Length || k > ttoks.Length)
        return out
    Loop (stoks.Length - k + 1) {
        p := A_Index
        hit := true
        Loop k {
            if (StrLower(stoks[p + A_Index - 1]) != StrLower(ttoks[A_Index])) {
                hit := false
                break
            }
        }
        if (hit)
            out.Push(p)
    }
    return out
}

; 顺句接词后应该落进目标词框的文本；接不上 → ""（调用方照旧走字段环）。
; 结果一律以**一个尾随空格**收尾 —— 它就是上面说的顺句状态标记。
ChainText(sentence, typed) {
    step := ChainStep(sentence, typed)
    if (!step)
        return ""
    ; v22 P4（spec §10.3）：写回也走新 tokenizer —— `2,000` 不会被拆成 `2 000`，
    ; 框里那串词的形态与锚点判定同口径。
    words := SplitTokens(NormalizeLine(NormalizePunct(typed)))
    if (step.replaceLast && words.Length > 0)
        words[words.Length] := step.word
    else
        words.Push(step.word)
    out := ""
    for w in words
        out .= (out = "" ? "" : " ") . w
    return out . " "
}

; 顺句提示（占用提示行的「补全候选」档）：按 Tab 之前就看得见会接上哪个词。
ChainHintText(sentence, typed) {
    step := ChainStep(sentence, typed)
    if (!step)
        return ""
    return (step.replaceLast ? "Tab 补全 → " : "Tab 接词 → ") . step.word . " · 顺句"
}

; ---------------------------------------------------------------------------
;  v17 W4（票 #141 / 决策 D2）：`Ctrl+Tab` 接词 —— 强制顺句
;
;  与 ChainStep 的关系：锚点规则一致（精确优先、Fold 回落、末词半截 replaceLast；
;  重复短语在 ① 取首次出现、在 ② 遍历全部位置并拒绝歧义 —— #202），放开的是
;  **单 token、无尾随空格**也能当锚点 —— 现有顺句
;  （Tab）要求「末尾有空白」或「≥2 词且末词半截」才接，因为 `Tab` 同时是字段环键，
;  「一个整词」上接词/走环两种意图无法区分；`Ctrl+Tab` 是专用键，没有这个歧义，
;  所以「去尾随空白后的词序列」一律当锚点（多词整词锚点 = `I was` → `reluctant`）。
;  定位失败返回 0 —— 调用方（外壳 CompCtrlTab）静默 no-op：不改值、不移动焦点、
;  不报错。`Tab`/`Shift+Tab` 的字段环职责不因此改变（v4 §3.2 红线）。
; ---------------------------------------------------------------------------

; `Ctrl+Tab` 接词的**下一步**：0（不接）或 `{ word, replaceLast, occ, total }`
; （语义同 ChainStep，occ / total 同样只由 ① 追加档带 —— 提示行的次数标签是同一条）。
ChainNext(sentence, typed) {
    stoks := SplitTokens(NormalizePunct(sentence))
    ttoks := SplitTokens(NormalizePunct(typed))
    if (ttoks.Length = 0 || stoks.Length = 0)
        return 0
    n := ttoks.Length

    ; ① 整串锚点（去尾随空白后的词序列；先精确、后 Fold 回落）→ 接紧挨着的下一个词；
    ;    原句走完 → no-op。#205：与 ChainStep ① 同源带 occ / total（提示行的次数标签共用）。
    runs := FindWordRuns(stoks, ttoks, n)
    if (runs.Length > 0) {
        p := runs[1]
        return (p + n > stoks.Length) ? 0
             : { word: stoks[p + n], replaceLast: false, occ: 1, total: runs.Length }
    }
    frs := FindFoldRuns(stoks, ttoks, n)
    if (frs.Length > 0) {
        if (frs[1].p + frs[1].span > stoks.Length)
            return 0
        return { word: stoks[frs[1].p + frs[1].span], replaceLast: false
               , occ: 1, total: frs.Length }
    }

    ; ② 末词半截（≥2 词）：前 n-1 词当锚，把半截词补成紧挨着的那个词（与 ChainStep ② 同规，
    ;    #202 也同规：遍历锚点全部出现位置、按 Fold 去重后恰好 1 个词才收尾，歧义不猜）。
    ;    单 token（k < 1）不进这条：半截词交给 Tab 补全，Ctrl+Tab 不猜。
    k := n - 1
    if (k < 1)
        return 0
    lc := FoldPrefix(ttoks[n])
    cand := "", seen := Map()
    for r in ChainTailRuns(stoks, ttoks, k) {
        if (r.p + r.span > stoks.Length)
            continue
        w := stoks[r.p + r.span], lw := Fold(w)
        if (StrLen(lc) >= StrLen(lw) || SubStr(lw, 1, StrLen(lc)) != lc)
            continue                            ; 既不是前缀也不是整词 → 这个位置不算
        if (seen.Has(lw))
            continue
        seen[lw] := true
        if (cand != "")
            return 0                            ; 不同词 ≥ 2 个 → 歧义，不猜
        cand := w
    }
    return (cand = "") ? 0 : { word: cand, replaceLast: true }
}

; `Ctrl+Tab` 接词后应该落进目标词框的文本；接不上 → ""（调用方静默 no-op）。
; 写入语义与 ChainText 一致：结果以**一个尾随空格**收尾（顺句状态标记，可连打）。
ChainNextText(sentence, typed) {
    step := ChainNext(sentence, typed)
    if (!step)
        return ""
    ; 与 ChainText 同一写回口径（spec §10.3）：新 tokenizer，`2,000` 不拆。
    words := SplitTokens(NormalizeLine(NormalizePunct(typed)))
    if (step.replaceLast && words.Length > 0)
        words[words.Length] := step.word
    else
        words.Push(step.word)
    out := ""
    for w in words
        out .= (out = "" ? "" : " ") . w
    return out . " "
}

; `Ctrl+Tab` 的提示文案（与顺句提示同一「补全候选」档槽位，不压过补全候选提示）。
ChainNextHintText(sentence, typed) {
    step := ChainNext(sentence, typed)
    if (!step)
        return ""
    return "Ctrl+Tab 接词 → " . step.word
}

; #205（上游 #204 决策 B）：重复锚点上的「第 N 处 / 共 M 处」标签；唯一锚点 / 接不上 → ""。
;
; 为什么是「说清」而不是「猜对」：① 的输入只有「整词 + 尾随空格」，歧义没有任何可靠
; 信号 —— 面板手里的 `G_LfLastLocate.target_start` 对同一句话本身就只会给首现
; （#204 实测 `locate("a").target_start=16`，而 `occurrences=[16,40,95,143]`），拿它消歧
; ==把「首现」从 ChainStep 搬到 locate，等于没消歧。而改成「重复锚点一律 no-op」会砸掉
; 顺句抄写主路径（`the`/`a`/`of` 重复 3–5 次是常态）。所以选词不动（仍是首现，用户顺读
; 往前看更可预期），只把「接的是第几处 / 共几处」摆到眼前，接错时用户自己就能看出来。
;
; 判据只看 `total`：唯一锚点说「第 1 处 / 共 1 处」没有信息量，只会占掉提示行的位置。
; ② 末词半截档不带 occ / total（它按 Fold 去重后必须唯一才收尾）→ HasOwnProp 挡住。
ChainOccurrenceText(step) {
    if (!IsObject(step) || !step.HasOwnProp("total") || step.total <= 1)
        return ""
    return "第 " . step.occ . " 处 / 共 " . step.total . " 处"
}

; ---------------------------------------------------------------------------
;  v17 W3（票 #139 / 决策 D4）：Tab 下一动作的**单一判定** —— 提示与动作同源
;
;  `CompTab`（实际动作）与 `ApplyHintState`（提示行）都只消费这里，根治「提示说
;  补全、实际做接词」的两处各算各的漂移。规则表（v4 §3.5 表序 + v17 W3）：
;    · 末尾有空白（顺句状态）：能接 → **接词**；接不上 → 回落补全（保住 `re `
;      这类多打一个空格的半截词用法）；连候选都没有 → 走字段环；
;    · 末尾无空白：有候选 → 补全（现状语义）；无候选但半截词能收尾 → 顺句；
;      否则 → 走字段环。
;  尾随空白判定吃**原始 typed**（不经 NormalizeLine —— 它会把尾随空白 Trim 掉，
;  正是 `the ` 被补成 `there` 的旧病根）。
;
;  返回 `{ action, word, hint }`：
;    action = "chain"  → 接词/收尾；word = **整段落框文本**（ChainText 的结果，
;                        以一个尾随空格收尾）；hint = 「Tab 接词/补全 → X · 顺句」；
;    action = "accept" → 接受候选；word = 候选词；hint = 补全文案（不带来源标签）；
;    action = "ring"   → 无候选走字段环；word = hint = ""。
; ---------------------------------------------------------------------------
TabNextAction(sentence, typed, cands := 0, idx := 0) {
    if (RegExMatch(typed, "\s$")) {
        txt := ChainText(sentence, typed)
        if (txt != "")
            return { action: "chain", word: txt, hint: ChainHintText(sentence, typed) }
        if (IsObject(cands) && cands.Length > 0) {
            w := cands[Mod(idx, cands.Length) + 1]
            return { action: "accept", word: w, hint: CompletionHintText(cands, idx) }
        }
        return { action: "ring", word: "", hint: "" }
    }
    if (IsObject(cands) && cands.Length > 0) {
        w := cands[Mod(idx, cands.Length) + 1]
        return { action: "accept", word: w, hint: CompletionHintText(cands, idx) }
    }
    txt := ChainText(sentence, typed)
    if (txt != "")
        return { action: "chain", word: txt, hint: ChainHintText(sentence, typed) }
    return { action: "ring", word: "", hint: "" }
}

; 来源标签（#102）：提示行要说清这次 Tab 补的是原句里的词、还是吸附来的
; —— 用户才知道该不该信（研究 §5 P2-1 的第 ⑤ 条）。句外兜底已在 2026-10-19 撤掉。
CompletionSourceLabel(src) {
    switch src {
        case "sentence": return "句内"
        case "fuzzy": return "近似"
    }
    return ""
}

; §3.3 补全提示文案。idx 为 0 起的当前候选下标（越界/负值都安全归一到 [0,n)）。
;   sources（可选，与 cands 等长）只用来追加来源标签；不传 → 与旧文案逐字一致。
;   单候选：`Tab 补全 → reinforce · 句内`
;   多候选：`Tab 补全 → reinforce（1/3） · ↑↓ 换 · 句内`
CompletionHintText(cands, idx, sources := 0) {
    n := cands.Length
    if (n = 0)
        return ""
    i := Mod(idx, n)
    if (i < 0)
        i += n
    head := "Tab 补全 → " . cands[i + 1]
    tail := (n = 1) ? "" : "（" . (i + 1) . "/" . n . "） · ↑↓ 换"
    label := (IsObject(sources) && sources.Length > i) ? CompletionSourceLabel(sources[i + 1]) : ""
    return head . tail . (label = "" ? "" : " · " . label)
}

; v5-capture-automation §4.2：**空闲补全提示** —— 目标词还空着、原句里至少有一个
; ≥2 字母的单词时，把功能摆出来（用户报的「Tab 补全没显示」由这条收口）。
; 目标词非空 / 原句没有 ≥2 字母的词 → 空串（不占提示行）。
CompletionIdleHint(sentence, target) {
    if (NormalizeLine(target) != "")
        return ""
    for t in SplitTokens(NormalizePunct(sentence))
        if (StrLen(t) >= 2)
            return "句内补全：打 ≥2 个字母 → Tab 补全"
    return ""
}

; v5 §4.2：**无匹配提示** —— 已打出 ≥2 字母的前缀（无内部空白）、原句非空、句内
; **根本没有以它开头的词**时，解释「为什么 Tab 没反应」。两条提示互斥（一条要求空
; 目标词，一条要求 ≥2 字母前缀）。
; 三条判据（收紧，2026-10-14 冒烟复核）：
;   ① 整词已在句内（OCR 猜词就是从原句里挑的，用户也可能已打全）→ 空：它就在句内，
;      没有东西要补，出「句内没有这个词」是自相矛盾；
;   ② 句内有以它为前缀、更长的词 → 空：那是候选提示（CompletionHintText）的地盘；
;   ③ 句内没有任何以它开头的词 → 「句内没有这个词（照打即可）」。
; `Completions` 保持 v4 §3 的既定语义（整词跳过）不动，这里自己判 ①。
; v5 §4.3 修订（独立复核挖出）：**输入也要过 NormalizePunct** —— 句内词已归一，输入
; 不归一的话 `don’t` / `well–known` 这类整词与句内词比不相等，判定落空、Completions
; 又跳过整词 → 候选为空 → 这里输出「句内没有这个词」，而那个词就在原句里（自相矛盾）。
CompletionMissHint(sentence, target) {
    t := NormalizeLine(NormalizePunct(target))
    if (StrLen(t) < 2 || RegExMatch(t, "\s"))
        return ""
    s := NormalizeLine(NormalizePunct(sentence))
    words := SplitTokens(s)
    if (words.Length = 0)
        return ""
    ; ① 整词命中（标点归一同口径、大小写不敏感）：v17 W2（票 #140）改走与近似档
    ; 同一谓词 `FoldEqualsAnyToken` —— 整词守卫与「整词已在句内 → 静默」两侧永远
    ; 同判；`dont` 之于句内 `don't`、`wellknown` 之于 `well-known` 也算「词就在句内」，
    ; 不再出现「句内没有这个词」的自相矛盾。
    if (FoldEqualsAnyToken(words, Fold(t)))
        return ""
    ; ② 有候选 → 让位给候选提示（这句只是把 ① 之后的情况说清楚）。
    ; v17 W2：不再内部调两参 `Completions`（v4 包装口径），改走主路径同一入口
    ; `CompletionTiers` —— 候选判定只有一个来源；壳里本提示也只在主路径候选为空
    ; 时才可能胜出（HintPick 优先级：补全候选 > 无匹配）。
    if (CompletionTiers(sentence, target).words.Length > 0)
        return ""
    return "句内没有这个词（照打即可）"
}

; §3.3 提示行优先级：按顺序取**第一个非空**来源，返回 `{ kind, text }`，全空 → 0。
; 优先级由调用方的数组决定（高 → 低），不是写死的几个 if —— A2（票 #51）
; 再加「已存 N 词」来源时，只往调用方的数组里插一项，这里不动。
HintPick(sources) {
    for s in sources {
        if (Trim(s.text) != "")
            return { kind: s.kind, text: s.text }
    }
    return 0
}

; ---------------------------------------------------------------------------
;  v17 W5（票 #142 / 决策 D1）：上游疑似粘连「只标记」的外壳消费。全是纯函数：
;  内核在校正后文本上标出 `suspects`（不改文本），外壳据此
;    ① 不把疑似 token 当句内候选（DropSuspectCands，direct/近似出口统一兜底）；
;    ② GuessTarget 预填跳过疑似 token（函数自己带回退，见上）；
;    ③ 输入是疑似 token 前缀且无可用候选时提示「可手动改原句」（SuspectGlueHint）。
;  任何情况下都不自动改原句文本 —— 标记只是提示，绝不比没有它更糟。
; ---------------------------------------------------------------------------

; token（原句形态）是否命中疑似粘连集合：Fold 口径比较（忽略大小写与 `'`/`-`）。
; `suspects` 为 0/空（旧内核没这个字段）时恒 false —— 整条 W5 消费自动退化成现状。
IsSuspectToken(w, suspects) {
    if (!IsObject(suspects) || suspects.Length = 0)
        return false
    fw := Fold(w)
    for rec in suspects {
        if (Fold(IsObject(rec) ? rec[1] : rec) = fw)
            return true
    }
    return false
}

; 从候选数组里剔除疑似粘连 token：words/sources 等长（CompletionTiers 的返回形状）；
; suspects 为 0/空 → 原样返回（不复制、零行为差异）。返回新的 { words, sources }；
; 候选顺序与来源标签保持不变（只做剔除，不重排 —— spec §W5「其余候选顺序不变」）。
DropSuspectCands(words, suspects, sources := 0) {
    if (!IsObject(suspects) || suspects.Length = 0 || !IsObject(words))
        return { words: words, sources: sources }
    out := [], srcs := []
    Loop words.Length {
        if (IsSuspectToken(words[A_Index], suspects))
            continue
        out.Push(words[A_Index])
        srcs.Push(IsObject(sources) && sources.Length >= A_Index ? sources[A_Index] : "")
    }
    return { words: out, sources: srcs }
}

; 「原句疑似粘连」提示（spec §W5 的字面量）：输入是某个疑似 token 的前缀
; （精确或 Fold）且没有可用候选时，告诉用户为什么 Tab 没反应、可以手动改原句。
; 有可用候选（cands 非空）→ 空：让位给补全提示（调用方的优先级数组也保证这点：
; 补全候选之后、已存 N 词之前，不盖 err）。不改原句、不自动接受。
SuspectGlueHint(typed, suspects, cands := 0) {
    if (IsObject(cands) && cands.Length > 0)
        return ""
    if (!IsObject(suspects) || suspects.Length = 0)
        return ""
    t := NormalizeLine(NormalizePunct(typed))
    if (StrLen(t) < 2)
        return ""
    ft := Fold(t)
    for rec in suspects {
        tok := IsObject(rec) ? rec[1] : rec
        fw := Fold(tok)
        if (StrLen(ft) <= StrLen(fw) && SubStr(fw, 1, StrLen(ft)) = ft)
            return "原句疑似粘连：" . tok . "（可手动改原句）"
    }
    return ""
}

; ---------------------------------------------------------------------------
;  一句多词（v4-capture-and-recall §4.1，票 #51 A2）
; ---------------------------------------------------------------------------

; §4.1 保存按钮文案：目标词为空且本框已存过 ≥1 词时是「收工」（空按即关框）；否则
; 「保存」。两个文案同为 2 汉字 + 空格 + ↵，像素宽度一致（改文案不会顶到键位提示）。
SaveButtonLabel(blank, n) {
    return (blank && n >= 1) ? "收工 ↵" : "保存 ↵"
}

; §4.1 提示行第 4 来源：**只在目标词为空、且已存过 ≥1 词**时给一行轻提示（用户一
; 开打，blank 变 false，这行自动让位给补全）。N = 本框累计保存数。
SavedHintText(n, blank) {
    return (blank && n > 0) ? "已存 " . n . " 词 · 再打下一个，或 Esc 收工" : ""
}

; ---------------------------------------------------------------------------
;  v5-capture-automation §5：速记框第一行的宽度分配（纯函数，票 R4 复核补强）
;
;  第一行宽度按优先级 `目标词 > 类型/会话 chip > 站点徽章 > 把手` 分：
;  目标词 = 可用宽 − 两枚 chip − 它们与把手之间的间距 − 把手 −（徽章在时再减徽章 + 间距）。
;  算出的目标词 < targetMin 时**先撤徽章**（长站名上限 96 lp 会把主字段挤到几十 lp）
;  再算一次；撤了还不够就以撤后的结果为准（宁让主字段更小也不许出现负宽）。
;
;  返回 `{ tgtW, siteShown }`：siteShown = 徽章这一行到底显不显示（外壳据此设
;  `C.site.Visible` + 宽度）。单位由调用方定（外壳全程用**物理像素**传进来）。
; ---------------------------------------------------------------------------
RowOneWidths(cw, kchipW, schipW, gripW, siteW, gap, targetMin) {
    ; 一次分配：两枚 chip 各吃一个间距（2*gap），把手吃一个（gap），徽章在时再吃
    ; 一个（gap）+ 自身宽。三处间距都算进去，目标词拿剩下的。
    tgtW := cw - kchipW - schipW - 2 * gap - gripW - gap - (siteW > 0 ? siteW + gap : 0)
    siteShown := (siteW > 0)
    if (tgtW < targetMin && siteShown) {
        siteShown := false                       ; 先撤徽章，主字段优先
        tgtW := cw - kchipW - schipW - 2 * gap - gripW - gap
    }
    return { tgtW: tgtW, siteShown: siteShown }
}

; 类型的三个取值（§3 领域词：word / phrase / sentence）
KindOrder() {
    return ["word", "phrase", "sentence"]
}

; 中文显示（§3：中文显示「词 / 短语 / 句」）
KindLabel(kind) {
    if (kind = "phrase")
        return "短语"
    if (kind = "sentence")
        return "句"
    return "词"
}

; 类型 chip 上的 `←`（dir<0）/ `→`（dir>0），在 词/短语/句 之间环切（§6.3）；
; 取值不在表里（含空串）按 word 起算。
CycleKind(kind, dir) {
    ks := KindOrder()
    i := 1
    for idx, k in ks {
        if (k = kind)
            i := idx
    }
    step := (dir < 0) ? -1 : 1
    return ks[Mod(i - 1 + step + ks.Length, ks.Length) + 1]
}

; 默认类型推断（票 #24 的判据）：有句末标点或 ≥6 词 → 句；≥2 词 → 短语；否则 词。
; 空串按 词（默认值，§4.1 `kind` 默认 word）。
InferKind(target) {
    s := NormalizeLine(target)
    if (s = "")
        return "word"
    if (RegExMatch(s, "[.!?]$"))
        return "sentence"
    n := 0
    for w in StrSplit(s, " ") {
        if (w != "")
            n++
    }
    if (n >= 6)
        return "sentence"
    if (n >= 2)
        return "phrase"
    return "word"
}

; 会话标题的**显示口径**（§6.1）：单行化 + 超 28 字截断加省略号。
; 会话 chip（SessionChipText）与遮罩右上角小字（SessionMaskLabel）都走这里 ——
; 两处口径必须一样，不然同一条会话在两个地方看起来是两个名字。
ShortTitle(title) {
    t := NormalizeLine(title)
    if (StrLen(t) > 28)
        t := SubStr(t, 1, 28) . "…"
    return t
}

; 会话 chip 的文案（§6.1）：`会话: <标题截断 28 字> ×<本会话词数>`；
; 没有会话（无标题）或用户选了「(新会话)」→ `会话: （新会话）`（§6.1 的人工干预）；
; `offline`（hub 不在位）→ 本地截断标题 + `（离线）` —— 词数问不到，不编。
; 例（§6.1 的字面量）：`这样练英语听力 ×7`。
SessionChipText(title, count, forcedNew := false, offline := false) {
    if (forcedNew)
        return "会话: （新会话）"
    t := ShortTitle(title)
    if (t = "")
        return "会话: （新会话）"
    if (offline)
        return "会话: " . t . "（离线）"
    if (count > 0)
        return "会话: " . t . " ×" . count
    return "会话: " . t
}

; 遮罩右上角的小字（v2 §6.4：显示当前会话名，帮用户确认「还在同一条视频里」）。
; 与 chip 共用 ShortTitle 的截断口径；没有会话名 → 空串（那块小字就不出现）。
; hub 没接时传进来的就是本地占位（出处窗口标题），C3 接 session_peek 后自动换真标题。
SessionMaskLabel(title) {
    t := ShortTitle(title)
    if (t = "")
        return ""
    return "会话: " . t
}

; 状态行文案（§6.3 的字面量）。空串 = 不占位（§6.3「无内容时不占位」）。
StatusText(code, n := 0) {
    if (code = "dict-wait")
        return "词典查询中…"
    if (code = "corrected")
        return "已校正 " . n . " 处（原文 …）"
    if (code = "hub-offline")
        return "数据中枢未就绪（已暂存）"
    if (code = "session-new")
        return "会话：新建"
    if (code = "dict-failed")            ; §7 的失败退化文案
        return "词典未返回，可手填"
    if (code = "dict-not-found")         ; v23 S2（B4）：词典明确未收录，不再静默
        return "词典未收录，可手填 / 可在取词台补查"
    if (code = "dict-timeout")           ; v23 S3（B5）：看门狗撤掉「查询中」并给出下一步
        return "词典无响应（可 Ctrl+L 重试）"
    if (code = "meaning-pending")        ; v23 S4：保存时释义为空，中枢后台补齐
        return "已保存；释义后台补齐中（可在取词台补查）"
    if (code = "meaning-kept")           ; §7：用户手打过释义、词典结果没覆盖它
        return "释义已手打，词典结果未覆盖"
    if (code = "meaning-machine")        ; v22 修复（#172）：机翻兜底的明确标注
        return "机翻释义 · 仅供参考（可手改）"
    return ""
}

; 键位串（config 的 AHK 语法，§9.1）→ 人读形态：`^!1` → `Ctrl+Alt+1`。
; 单字符键大写化、`Escape` → `Esc`（按钮与 F1 键位表都这么写，§6.3）。
; `~` 放行前缀（票 #48）挂尾注：`~!1` → `Alt+1（放行）`（写成 `放行 Alt+1` 会把
; 前缀混进修饰键序列，读起来像另一个键）。解析不出/非法串也照样渲染，绝不空串。
PrettyKey(spec) {
    s := Trim(spec)
    out := "", key := "", pass := false
    i := 1
    while (i <= StrLen(s)) {
        c := SubStr(s, i, 1)
        if (c = "^")
            out .= "Ctrl+"
        else if (c = "!")
            out .= "Alt+"
        else if (c = "+")
            out .= "Shift+"
        else if (c = "#")
            out .= "Win+"
        else if (c = "~")
            pass := true
        else
            key .= c
        i++
    }
    if (StrLen(key) = 1)
        key := StrUpper(key)
    if (key = "Escape")
        key := "Esc"
    return out . key . (pass ? "（放行）" : "")
}

; #48（B2）：呼出键是不是「裸 Alt 形态」= 解析得出、带 Alt 修饰符、且没写 `~` 放行前缀。
; 正是这种形态让浏览器收到一次「裸 Alt」（AHK 只吞触发键的 down）→ Chromium 聚焦
; 应用菜单「...」→ 之后的空格废掉（机制见 docs/research/v3-b1-mechanism.md）。
; 非法串一律 false（该走语法错误那条路，别在这里重复报）。
IsBareAltHotkey(spec) {
    r := ParseHotkeyV2(spec)
    return r.ok && (r.mods & 0x0001) && !r.pass
}

; `F1` 键位表的整段文案（§6.3：置顶小窗列出全局键与框内键）。
; 入参 k 是一组键位串（config 的当前值），所以 C5 把键位改成什么都自动跟着变。
; `hotkey_new_session` 默认空（不注册）→ 空值那一行不出。
KeymapText(k) {
    lines := ["全局"
            , "  " . PrettyKey(k.hotkey) . "  呼出速记框"
            , "  " . PrettyKey(k.hotkeyCapture) . "  直接截图取词"
            , "  " . PrettyKey(k.hotkeyReview) . "  打开复习界面"]
    if (k.HasOwnProp("hotkeyNewSession") && Trim(k.hotkeyNewSession) != "")
        lines.Push("  " . PrettyKey(k.hotkeyNewSession) . "  强制新会话")
    lines.Push("")
    lines.Push("框内")
    lines.Push("  Enter  保存并继续（空时收工）")
    lines.Push("  " . PrettyKey(k.keySave) . "  保存并关框")
    lines.Push("  " . PrettyKey(k.keyCancel) . "  收工/取消")
    lines.Push("  " . PrettyKey(k.keyShot) . "  截图取词")
    lines.Push("  " . PrettyKey(k.keyMore) . "  展开 / 收起元数据")
    lines.Push("  " . PrettyKey(k.keyContext) . "  原句展开 3 行")
    lines.Push("  " . PrettyKey(k.keyHelp) . "  本键位表")
    lines.Push("  " . PrettyKey(k.keyLookup) . "  重新查词典")
    lines.Push("  Tab / Shift+Tab / ↑ / ↓  切字段")
    lines.Push("  Tab（目标词处）  补全句内单词 · 连打接下一个词")
    lines.Push("  Ctrl+Tab（目标词处）  接下一个词")
    lines.Push("  ↑↓（有补全时）  换候选")
    lines.Push("  ← / →  切类型 · 切会话")
    out := ""
    for l in lines
        out .= l . "`n"
    return RTrim(out, "`n")
}

; ---------------------------------------------------------------------------
;  config.ini 写单个键（§6.3：主题选择落到 config）
;  自己写 txt 而不是 IniWrite：本机 IniWrite 与 IniRead 同一族毛病（UTF-8 段名
;  在 ANSI 代码页下对不上），且要**保留其他键与注释**（§8.4 的同一条要求）。
;  行为：段内找到键 → 原地改；段内没有 → 追加到段末；整段没有 → 文件末追加整段。
;  行尾风格沿用原文（真 config.ini 是 CRLF）。
; ---------------------------------------------------------------------------

; 落笔判据（票 #185 / A11；纯函数，harness 片 41 钉）。SaveConfigKeys 是
; 「读全文 → 原地改 → 整文件写回」，期间 hub 设置页整文件写 config（不走这里）
; 的话，直接落笔会把别人的改动整个抹掉。stampRead = 读全文**之前**拍的指纹
; （ConfigStamp：mtime+size），stampWrite = 落笔**之前**再拍的那次；两者一致 =
; 读取期间没有外部写入，可以落笔。空指纹（文件已不在）一律不许落笔。
ConfigWriteAllowed(stampRead, stampWrite) {
    return (stampRead != "" && stampRead = stampWrite)
}

IniSetText(text, section, key, value) {
    eol := InStr(text, "`r`n") ? "`r`n" : "`n"
    lines := []
    for l in StrSplit(text, "`n", "`r")
        lines.Push(l)
    if (lines.Length > 0 && lines[lines.Length] = "")
        lines.RemoveAt(lines.Length)          ; 末尾换行分出来的空串

    secStart := 0, secEnd := 0, inSection := false
    for idx, l in lines {
        t := Trim(l)
        if (SubStr(t, 1, 1) = "[") {
            if (inSection) {
                secEnd := idx - 1
                inSection := false
            }
            if (SubStr(t, 2, StrLen(t) - 2) = section) {
                secStart := idx
                inSection := true
            }
        }
    }
    if (secStart && !secEnd)
        secEnd := lines.Length

    if (secStart) {
        for idx, l in lines {
            if (idx <= secStart || idx > secEnd)
                continue
            eq := InStr(l, "=")
            if (eq < 2)
                continue
            if (Trim(SubStr(l, 1, eq - 1)) = key) {    ; `=` 比较不分大小写，与 ReadIniValue 一致
                lines[idx] := key . "=" . value
                return JoinIniLines(lines, eol)
            }
        }
        lines.InsertAt(secEnd + 1, key . "=" . value)
        return JoinIniLines(lines, eol)
    }

    if (lines.Length > 0)
        lines.Push("")
    lines.Push("[" . section . "]")
    lines.Push(key . "=" . value)
    return JoinIniLines(lines, eol)
}

JoinIniLines(lines, eol) {
    out := ""
    for l in lines
        out .= l . eol
    return out
}

; ---------------------------------------------------------------------------
;  日志滚动（spec §10.2：logs\app.log 单文件 2MB、滚动保留 4 个）
;
;  阈值与保留数都由调用方注入（主脚本传 2MB / 4；测试传小阈值，不真写 2MB）。
;  次序是「自旧向新依次让位」：最旧的那个（keep 号）被第 1 步的覆盖写淘汰。
;  内核侧写自己的 kernel.log（同一策略），两个进程不共用滚动文件。
; ---------------------------------------------------------------------------

; 到了上限就该滚（>= 到阈值即滚）。只做判定，不搬文件。
ShouldRotate(path, maxBytes) {
    if (!FileExist(path))
        return false
    try
        return FileGetSize(path) >= maxBytes
    catch
        return false
}

; 滚动计划：返回自旧向新的搬移步骤 [{src, dst}, ...]。
; path="app.log"、keep=4 → [.3→.4, .2→.3, .1→.2, app.log→.1]。
; 次序性质（测试钉住）：任何一步的目标都不是后面某一步的源，
; 按表执行不会覆盖还没搬走的旧日志。
RotationPlan(path, keep) {
    steps := []
    i := keep - 1
    while (i >= 1) {
        steps.Push({ src: path . "." . i, dst: path . "." . (i + 1) })
        i--
    }
    steps.Push({ src: path, dst: path . ".1" })
    return steps
}

; 真滚一次。返回空串 = 没超限或已滚完；非空 = 错误原因（不抛异常，
; 日志写不出去也不能把外壳弄崩）。
RotateLog(path, maxBytes, keep) {
    if (!ShouldRotate(path, maxBytes))
        return ""
    ; keep 号此时已无上位来源，删掉它就是「淘汰最旧」。FileDelete 对不存在的
    ; 文件会抛错（v2 实测），所以先判存在；删不掉也继续，下一步的覆盖写仍会淘汰它。
    if (FileExist(path . "." . keep)) {
        try {
            FileDelete(path . "." . keep)
        } catch {
        }
    }
    for step in RotationPlan(path, keep) {
        if (!FileExist(step.src))
            continue
        try {
            FileMove(step.src, step.dst, 1)      ; 1 = 覆盖
        } catch as err {
            return "轮转 " . step.src . " → " . step.dst . " 失败：" . err.Message
        }
    }
    return ""
}

; ---------------------------------------------------------------------------
;  spool（v2 规格 §2 / §4.1）：一条记录 = `data/inbox.jsonl` 的一行 JSON
;
;  保存路径**不依赖任何进程**：外壳把整行（含末尾换行，一次写完）追加进
;  inbox.jsonl，再 best-effort 通知 hub `flush`。hub 在**启动时 / 每 5s /
;  收到 flush 时** ingest，成功后截断文件 —— hub 死了也丢不了词（v1 的
;  「写 vault 失败就进剪贴板」由它取代）。
;  字段与 hub 的 `store.capture()` 一一对应（§4.1 的表，顺序也照表）：
;    target / kind / context / meaning / phonetic / dict_json /
;    source_app / source_title / position / tags / captured_at
;  会话线索 = source_app + source_title（hub 据此算 §6.1 的 session key），
;  外加 `new_session`：chip 选「(新会话)」时 true → hub 侧跳过复用、强制新建会话。
; ---------------------------------------------------------------------------

; JSON 字符串字面量（含首尾引号）。目标词/原句/释义里完全可能有 `"` 或反斜杠
; （OCR 与手打都会），不转义就会写出坏 JSON —— 那一行 hub 只能当坏行跳过。
; 控制字符一律转义：一行 JSON 里不许出现真换行。
JsonString(s) {
    out := ""
    i := 1
    while (i <= StrLen(s)) {
        c := SubStr(s, i, 1)
        if (c = '"')
            out .= '\"'
        else if (c = "\")
            out .= "\\"
        else if (c = "`n")
            out .= "\n"
        else if (c = "`r")
            out .= "\r"
        else if (c = "`t")
            out .= "\t"
        else if (Ord(c) < 32)
            out .= Format("\u{:04x}", Ord(c))
        else
            out .= c
        i++
    }
    return '"' . out . '"'
}

; JSON 转义串还原（`` \n ``/`` \t ``/`` \" `` 等）。从外壳搬来这里：`JsonField` 与
; 本文件的 `ParseCorrections` 都要用它，放纯逻辑层两处才都能 #Include 到。
UnescapeJson(s) {
    out := "", i := 1, n := StrLen(s)
    while (i <= n) {
        c := SubStr(s, i, 1)
        if (c = "\" && i < n) {
            nx := SubStr(s, i + 1, 1)
            if (nx = "n")
                out .= "`n"
            else if (nx = "t")
                out .= "`t"
            else if (nx = "u")
                out .= SubStr(s, i, 6)      ; 内核用 ensure_ascii=False，正常不会出现
            else
                out .= nx
            i += 2
        } else {
            out .= c
            i++
        }
    }
    return out
}

; `"name":"值"` / `"name":true|false` 一对（字段名是常量，值走转义）
JsonStrPair(name, value) => '"' . name . '":' . JsonString(value)
JsonBoolPair(name, value) => '"' . name . '":' . (value ? "true" : "false")

; 一条记录的 spool 行（§4.1）。`f` 的字段：
;   target / kind / context / meaning / phonetic / dictJson（C4 起是词典快照，
;   现在恒空串）/ app / title / url / position / tags / newSession
;   `url`（v3 §3）= 捕获时的页面 URL（取自 peek 回包的 `browser.url`，没链接就空串）。
;   `meaningSource`（v22 修复 / #172，可选）= 释义来源：`"machine"` 表示机翻兜底
;   （词典命中/手打仍为空串）。**缺省不写**，普通捕获的 spool 行逐字不变。
SpoolLine(f, now) {
    parts := []
    parts.Push(JsonStrPair("target", NormalizeLine(f.target)))
    parts.Push(JsonStrPair("kind", f.kind))
    parts.Push(JsonStrPair("context", NormalizeLine(f.context)))
    parts.Push(JsonStrPair("meaning", NormalizeLine(f.meaning)))
    parts.Push(JsonStrPair("phonetic", NormalizeLine(f.phonetic)))
    parts.Push(JsonStrPair("dict_json", f.dictJson))
    parts.Push(JsonStrPair("source_app", NormalizeLine(f.app)))
    parts.Push(JsonStrPair("source_title", NormalizeLine(f.title)))
    parts.Push(JsonStrPair("url", ObjProp(f, "url")))
    parts.Push(JsonStrPair("position", NormalizeLine(f.position)))
    parts.Push(JsonStrPair("tags", FormatTags(f.tags)))
    parts.Push(JsonStrPair("captured_at", FormatTime(now, "yyyy-MM-dd HH:mm:ss")))
    parts.Push(JsonBoolPair("new_session", f.newSession))
    ; v22 修复（#172）：机翻来源（仅 machine 时写；store.capture 已支持同名列）
    if ((meaningSource := ObjProp(f, "meaningSource")) != "")
        parts.Push(JsonStrPair("meaning_source", meaningSource))
    ; v21 T5（#160）：长文词条的可选归属字段 —— **缺省不写**（旧 hub 忽略未知字段，
    ; 普通捕获的 spool 行一个字节都不变）。doc_id/passage_index 由面板侧传。
    if ((docId := ObjProp(f, "docId")) != "" && docId + 0 > 0)
        parts.Push('"doc_id":' . (docId + 0))
    if ((pi := ObjProp(f, "passageIndex")) != "")
        parts.Push('"passage_index":' . (pi + 0))
    out := ""
    for p in parts
        out .= (out = "" ? "" : ",") . p
    return "{" . out . "}"
}

; `<data>/inbox.jsonl`（§2）：与 hub 同一个口径 —— 数据库文件所在目录 + inbox.jsonl
; （hub 侧 `inbox_path = db_path.parent / "inbox.jsonl"`）。
SpoolPathOf(dbPath) {
    SplitPath(dbPath, , &dir)
    if (dir = "")
        return "inbox.jsonl"
    return dir . "\inbox.jsonl"
}

; spool 追加失败后的重试节奏（票 #75）：返回**每档要等的毫秒数**，用尽即收手。
; 形状与来源：5 档 × 40ms = 总等待 200ms（改前是「失败立刻返回」→ 用户看到假失败）。
;
; 为什么是这个量级 —— 与 ingest 的改名窗口对齐：
;   撞车的那一头是 hub 的 `os.replace(inbox.jsonl → .consuming)`（store.py），
;   那是一次 µs 级系统调用；外壳侧 `FileAppend` 的 CreateFile 只可能在这个 syscall
;   期间拿到共享冲突（#68 隔离实测：只做改名时 2000 次 open 失败 182 次，改名之外 0 次）。
;   所以**一档 40ms 就已经比那个窗口宽了三个数量级**，一档足以让 CreateFile 落在改名
;   之后；再补 4 档是给「hub 恰好在这一瞬 ingest、紧接着又 ingest 一次」留余量。
;   总等待 200ms 是**上界**，也刻意压在人能察觉之前（且 SpoolAppend 跑在外壳的界面
;   线程上，Sleep 会卡消息循环 —— 所以必须有界，不能无限重试）。
SpoolRetryDelays() {
    return [40, 40, 40, 40, 40]
}

; 追加一行到 spool。**整行含末尾换行一次写完** —— hub 把「末行没有换行」当
; 「外壳正写到一半」，分两次写会让它吃掉半条 JSON。
; 编码 UTF-8 无 BOM（"UTF-8-RAW"；带 "UTF-8" 新建文件时会写 BOM）。
; 返回空串 = 成功；非空 = 错误原因。失败按 SpoolRetryDelays() 的节奏**有界重试**
; （#75：撞上 ingest 的改名不是真失败），**重试用尽**才回错误串 —— 调用方
; DoSave / FailSave 的文案与分工一行都不用改。
; 重试只重**整行**：这类撞车是 CreateFile 那一刻的共享冲突（一个字节都还没写），
; 所以重试不会写出半行 —— 真有半行只可能来自「写了一半磁盘满了」，那种重试也无解。
SpoolAppend(path, line) {
    delays := SpoolRetryDelays()
    lastErr := ""
    attempt := 0
    Loop {
        attempt += 1
        try {
            SplitPath(path, , &dir)
            if (dir != "" && !DirExist(dir))
                DirCreate(dir)
            FileAppend(line . "`n", path, "UTF-8-RAW")
            return ""
        } catch as err {
            lastErr := err.Message
        }
        if (attempt > delays.Length)      ; 用尽最后一档 → 收手
            break
        Sleep(delays[attempt])
    }
    return "写入暂存文件失败：" . lastErr
}

; spool 写失败时进剪贴板的那份（沿用 v1 §11 的语义：整条内容给用户留条后路）。
; 用**人可读**形态而不是 JSON 行 —— 剪贴板是给人贴进笔记/聊天窗的，一行 JSON 没用。
; 行序照 v1 条目的元数据段（出处 / 位置 / 出处自动 / 标签），别让「出处自动」丢在路上。
SpoolClipText(v) {
    lines := [NormalizeLine(v.target)]
    if ((s := NormalizeLine(v.context)) != "")
        lines.Push(s)
    if ((s := NormalizeLine(v.meaning)) != "")
        lines.Push(s)
    if ((s := NormalizeLine(v.phonetic)) != "")
        lines.Push(s)
    if ((s := NormalizeLine(v.prov)) != "")
        lines.Push("出处: " . s)
    if ((s := NormalizeLine(v.pos)) != "")
        lines.Push("位置: " . s)
    if ((s := NormalizeLine(v.provAuto)) != "")
        lines.Push("出处自动: " . s)
    if ((s := FormatTags(v.tags)) != "")
        lines.Push("标签: " . s)
    out := ""
    for l in lines
        out .= l . "`n"
    return RTrim(out, "`n")
}

; ---------------------------------------------------------------------------
;  中枢 RPC 的请求行（v2 §2；协议与字段见 hub.py 的模块 docstring）。
;  `session_peek` 要 app + title 的原值（清洗与截断都在 hub 侧，§6.1）。
; v5-capture-automation §2.4：新入参 `hwnd`（整数，缺省/0 = 不做本机探针）——
; hub 侧据此读「本次捕获的那只浏览器窗口」的地址栏（UIA）。`app`/`title` 仍走转义。
; ---------------------------------------------------------------------------
HubPeekRequest(app, title, hwnd := 0) {
    return '{"cmd":"session_peek","app":' . JsonString(NormalizeLine(app))
         . ',"title":' . JsonString(NormalizeLine(title)) . ',"hwnd":' . (hwnd + 0) . '}'
}

; v5 §3 的媒体联动 RPC（冻结）：
;   `{"cmd":"media","op":"pause"|"resume","app":"Edge","title":"<原始窗口标题>"}`
; 回包 `{"ok":…,"paused_by_us":…,"status":"playing|paused|none|unavailable"}`。
; 外壳侧 fire-and-forget（回包只记一行日志，见外壳的 OnMediaLine —— 绝不弹提示）。
HubMediaRequest(op, app, title) {
    return '{"cmd":"media","op":' . JsonString(op)
         . ',"app":' . JsonString(NormalizeLine(app))
         . ',"title":' . JsonString(NormalizeLine(title)) . '}'
}

HubFlushRequest() => '{"cmd":"flush"}'

; ---------------------------------------------------------------------------
;  v3 §3：`session_peek` 回包的**浏览器上下文**（新键 `category`/`url`/`browser`/`site`）。
;  `browser` 采纳不到时是 `null`（§2.4：宁可不给链接，也不给错链接）—— 这里必须
;  认得出 null 与「对象里没有这个键」两种情况，别把顶层那个会话 `url` 当成页面 URL。
; ---------------------------------------------------------------------------

; 取 `"name":"…"` 的字符串值（转义照 Logic.UnescapeJson 解）。外壳里那份 JsonField
; 正则一样，但那是外壳自己的（只用于中枢回话）；Logic 这一份要能独立被 harness 钉。
; v22 P1a（票 #170）：捕获组用 possessive `*+` —— 非 possessive 写法对 ≥~3KB 的
; 长字段会抛 `PCRE execution error`（AHK v2.0.30 / PCRE2 match limit；长字段回归
; 见 tests/test_logic.ahk 片 30）。
JsonStrOf(json, name) {
    if (!RegExMatch(json, '"' . name . '"\s*:\s*"((?:[^"\\]|\\.)*+)"', &m))
        return ""
    return UnescapeJson(m[1])
}

; 取 `"name":true` 的布尔值（缺失 / false → false）。外壳里那份 JsonBool 正则一样，
; 但那是外壳自己的；Logic 这一份要能独立被 harness 钉（v23 S6 的二跳判定用）。
JsonBoolOf(json, name) {
    return RegExMatch(json, '"' . name . '"\s*:\s*true') ? true : false
}

; 取 `"key":{…}` 里那个对象的**完整文本**（括号配对，字符串里的括号/转义不算）。
; 没有这个键、或值不是对象（`null`）→ 空串。
; （局部名叫 `quoted`/`skip` 而不是 `inStr`/`esc`：`InStr` 是内置函数名，
;  拿它当局部变量会让同一个函数里的 InStr(...) 调用被解析成变量调用。）
JsonObjectAt(json, name) {
    if (!RegExMatch(json, '"' . name . '"\s*:\s*\{', &whole))
        return ""
    start := InStr(json, whole[0]) + StrLen(whole[0]) - 1        ; `{` 的下标
    depth := 0, quoted := false, skip := false
    i := start
    while (i <= StrLen(json)) {
        ch := SubStr(json, i, 1)
        if (quoted) {
            if (skip)
                skip := false
            else if (ch = "\")
                skip := true
            else if (ch = '"')
                quoted := false
        } else if (ch = '"') {
            quoted := true
        } else if (ch = "{") {
            depth++
        } else if (ch = "}") {
            depth--
            if (depth = 0)
                return SubStr(json, start, i - start + 1)
        }
        i++
    }
    return ""
}

; `browser` 那条 → `{ url, site, timeText }`。未采纳（null/缺键）→ 三项全空：
;   · url      —— 落 spool 的 `url` 字段（§3）；
;   · site     —— 站点徽章（§7）；
;   · timeText —— 「位置」自动填（§7，`mm:ss` 口径由 hub 侧 FormatTimestamp 出）。
PeekBrowser(line) {
    b := JsonObjectAt(line, "browser")
    if (b = "")
        return { url: "", site: "", timeText: "" }
    return { url: JsonStrOf(b, "url")
           , site: JsonStrOf(b, "site")
           , timeText: JsonStrOf(b, "time_text") }
}

; 顶层 `site`（§3：采纳时的站点短名，未采纳 = 空串）—— 徽章文案取它，
; 空串就是「无链接」（§7：不许出现空徽章）。
PeekSite(line) => JsonStrOf(line, "site")

; 顶层 `category`（§3：会话的 7 类之一，只会出现在日志里，不参与判定）
PeekCategory(line) => JsonStrOf(line, "category")

; 「位置」自动填的覆盖判定（§7）：**当前为空**才填，用户手改过（非空）绝不覆盖。
ShouldFillPosition(current, autoText) {
    return (NormalizeLine(current) = "" && NormalizeLine(autoText) != "")
}

; 启动时探一次中枢（托盘图标一开始就要诚实：蓝 = 内核 + 中枢都在位，§6.5）
HubPingRequest() => '{"cmd":"ping"}'

; 保存回执的 toast 文案（§6.3 的两句字面量 + §10 的暂存句）：
;   "saved"   → `已存 → reluctant`
;   "dedup"   → `已存（本会话第 2 次）`（hub 的 flush 回话里 last.hits）
;   "offline" → `数据中枢未就绪（已暂存）`（与状态行同一句）
; v23 S4：`meaningPending=true` 时把「后台补齐」并进同一条 toast —— 关框保存没有状态行，
; 不能再弹第二条（旧实现两条 toast 叠在一起，前一条被压住）。离线不承诺后台补齐。
SaveToastText(kind, target, hits := 0, meaningPending := false) {
    if (kind = "dedup") {
        text := "已存（本会话第 " . hits . " 次）"
    } else if (kind = "offline") {
        return StatusText("hub-offline")
    } else {
        word := NormalizeLine(target)
        ; v4.1（#54 P2-5）：空词不出「已存 → 」——回执错位时的保险，宁报「已存」不报空词
        text := (word = "") ? "已存" : ("已存 → " . word)
    }
    if (meaningPending)
        text .= "；释义后台补齐中（可在取词台补查）"
    return text
}

; ---------------------------------------------------------------------------
;  释义自动填入（v2 §7 / 票 #27）
;
;  目标词确定后（OCR 回填或手改）去抖 250ms 发一次 `lookup`；回话异步到达，
;  只在「释义仍为空或仍等于上一次自动填入的值」时覆盖（用户手打过就不覆盖），
;  音标显示在释义行左侧（等宽灰字），`key_lookup`（默认 `Ctrl+L`）强制重查。
;  这里只放纯函数：请求行、去抖状态、覆盖判定、文案组装、状态行优先级。
; ---------------------------------------------------------------------------

; 取对象属性（缺失给默认值）。回话 JSON 被解成普通对象，键可能缺（§7 的 `seen`
; 只在 count>0 时出现），直接读属性会抛错，所以统一走这里。
ObjProp(obj, name, def := "") => (IsObject(obj) && obj.HasProp(name)) ? obj.%name% : def

; `lookup` 的请求行（§7）：词走转义（短语/句子带空格与引号）。
HubLookupRequest(word) => '{"cmd":"lookup","word":' . JsonString(NormalizeLine(word)) . '}'

; ---------------------------------------------------------------------------
;  v22 P5（票 #169 / spec §11）：多词目标释义
;
;  判据：`InStr(NormalizeLine(target), " ")` 为真（多词）→ 不走单词词典。
;  外壳给 lookup 请求带 `dict_only`，hub 只做「整条短语的词典查询」（本地 ECDICT +
;  有道词典条目/精确网页词条），不查机翻、不拼逐词义；回包带 `dict_only:true` 才算
;  词典命中。未命中时释义留空，面板释义区只显示上下文提示。
; ---------------------------------------------------------------------------

; 多词目标判据（spec §11 的字面口径：单行化后含空格）。
IsMultiwordTarget(target) => InStr(NormalizeLine(target), " ") > 0

; 多词目标未命中词典时，面板释义区的灰字提示（kind 沿用现有 InferKind 口径）。
MultiwordMeaningHint(kind) => (kind = "sentence") ? "句 · 见原句" : "短语 · 见原句"

; 释义回包能不能填：多词目标只认 hub 的「整条短语词典命中」（回包带 `dict_only:true`）
; 或**明确标注的机翻兜底**（`machine:true`，v22 修复 / #172）；两者都没有（旧 hub /
; v18 的 web_trans·fanyi 机翻回包）一律不填 —— fail-closed：宁愿不显示也不显示错义。
; 单词目标照旧（v18 行为零改动）。
LookupMeaningApplies(target, replyDictOnly, replyMachine := false) {
    if (!IsMultiwordTarget(target))
        return true
    return (replyDictOnly || replyMachine) ? true : false
}

; 目标词变化的去抖（票 #27）：state = {word, at}（当前已安排的待查词与触发时刻），
; 或 0（没有在途）。返回 {state, schedule}：
;   · 空白目标词 → 清掉在途（state=0）、不安排（不查空词）；
;   · 与在途词相同 → 原样保留、不重置定时器（等它自己到点）；
;   · 不同 → 新安排一次（at = now + delayMs），schedule=true（调用方挂 -delayMs 定时器）。
;  纯函数：把「要不要查」从 GUI 的 SetTimer 里摘出来，harness 钉住。
DebounceStep(state, word, now, delayMs) {
    w := NormalizeLine(word)
    if (w = "")
        return { state: 0, schedule: false }
    if (state && state.word = w)
        return { state: state, schedule: false }
    return { state: { word: w, at: now + delayMs }, schedule: true }
}

; 到点真要查时决定查哪个词：目标词又改了（与在途词不同）→ 不查（等新的一次去抖）；
; 相同 → 查它。返回空串 = 本次不查。
LookupDueWord(state, word) {
    if (!state || NormalizeLine(word) != state.word)
        return ""
    return state.word
}

; HubSend 失败后去抖状态的收口（票 #185 / A13；纯函数，harness 片 42 钉）。
; LookupFire / LfLookupFire 都「先记去抖状态再发送」，发送失败 = 这次查询根本没
; 出去（连接也没了），但状态里还挂着这个词 —— hub 恢复后用户重打/重框**同一个
; 词**会被 DebounceStep 的「同词在途」分支拒绝（schedule=false），永不再查。
; 只在「在途仍是本词」时归零（fire 到失败之间无重入，防御式收窄：已被别的词
; 顶掉就不动它）。
LookupDebOnSendFailed(state, word) {
    w := NormalizeLine(word)
    return (IsObject(state) && state.word = w) ? 0 : state
}

; v23 S3（B5 / spec §4.3）：`lookup` 看门狗判定。回话丢失时外壳会永远停在
; 「词典查询中…」，所以发出查询时记一个 deadline，到点由 LookupWatchdog 收手。
; 纯函数（harness 片 34 钉）：只有「确实在等」+「有 deadline」+「到点」三条同时成立
; 才返回 true；任何一条回话（含迟到回话）都会让调用方 SetDictWait(false) → 这里退化成
; no-op；新一轮查询会重置 deadline，旧拍子也不会误伤新请求。
LookupWatchdogFired(waiting, now, deadline) {
    if (!waiting)
        return false
    if (deadline <= 0)
        return false
    return now >= deadline
}

; v4.1（#54 P1-1）：迟到的 `lookup` 回话归属校验。v4 起「保存并继续」不再关框，
; 词 A 的 `lookup` 可能在途时用户已按 Enter 换到词 B（或刚 Reset 成空）——回话里的
; `word` 必须与**当前目标词**一致（归一化 + 忽略大小写），否则整条丢弃，绝不写进
; 下一个词的槽位。当前目标词为空（刚 Reset 还没打字）也判不符。
LookupWordMatches(responseWord, currentTarget) {
    now := StrLower(NormalizeLine(currentTarget))
    if (now = "")
        return false
    return StrLower(NormalizeLine(responseWord)) = now
}

; v23 S6（spec §4.3/§4.4）：两段式回填的**二跳判定**，旧速记框与长文面板共用。
; 回话是 `provisional`（本地先到的预备释义）、不是机翻兜底（machine 永远不二跳，
; 避免死循环）、回话词非空、且与「上一次已经二跳过的词」不同 → 返回要补发 `full`
; 的词；否则空串。
; 词先 NormalizeLine；与 lastFollowed 的比较用 AHK `!=`（默认**忽略大小写**），
; 与旧框 inline 版的 `G_LookupFollowedUp != followed` 口径逐字一致 —— 同一个词的大写
; 变体反复回话时仍然只补一次。
LookupFollowUpWord(replyLine, lastFollowed) {
    if (!JsonBoolOf(replyLine, "ok") || !JsonBoolOf(replyLine, "provisional")
        || JsonBoolOf(replyLine, "machine"))
        return ""
    word := NormalizeLine(JsonStrOf(replyLine, "word"))
    if (word = "" || word = NormalizeLine(lastFollowed))
        return ""
    return word
}

; v24（票 #176 / spec §3.1 F2）：旧框三入口（NewCapture/ShotHotkey/RunShot）的
; 「长文在途」互斥判据。`busy` = G_LongBusy —— 它从长文遮罩拖框**进入前**就置位
; （对齐旧框 G_ShotActive 的「拖框进行中挡热键重入」语义），拖框取消/发送失败/
; 超时/内核拒绝/建文完成各退出路径都会归零；`pending` = G_LongPending（内核请求
; 在途）。内核一次只处理一条请求-回话对，两者任一非空 = 长文链路占线，capture_ocr
; 入口一律礼貌拒绝（可读 toast），双遮罩互踩与回话按 pending-kind 串线不再可能。
; 纯函数（harness 片 36 钉）：三入口共用这一份判据，不许各自再抄一遍条件。
LongformInFlight(busy, pending) {
    return (busy || pending != "") ? true : false
}

; 票 #70：`session_peek` 回话的归属门 —— 与上面那条同一家族（迟到回话不许写进
; 下一轮），但归属键是**捕获代次**：peek 的回包要等 hub 侧本机探针（UIA 冷启动
; 慢时最多 800ms）+ 排队，而 BeginCapture 会把 chip 那批状态清零 —— 上一轮（可能
; 已被 Esc 取消、或已收工关框）的回话于是能写进下一轮：上一条视频的标题、站点
; 徽章、页面 URL，甚至把位置时间填进新一轮。发 peek 时把当时的代次随 ask 入队
; （`{ kind:"peek", gen }`），回话按代次对号入座。
;   askGen = 回话对应的那个 ask 里的代次（"" = 缺失：给不出归属信息）
;   curGen = 当前捕获代次（BeginCapture 每轮 +1）
; 返回 true 才允许它写状态。判定是**严格相等**（同值数字/字符串都认，JSON 回环下
; 形态可能不同）；askGen 缺失也判 false —— 给不出归属就不给写（fail-closed）。
ReplyApplies(askGen, curGen) {
    if (askGen = "")
        return false
    return askGen = curGen
}

; 释义覆盖判定（§7 的字面口径）：当前释义为空、或仍等于上一次自动填入的值
ShouldFillMeaning(current, lastAuto, newText) {
    cur := NormalizeLine(current)
    if (cur = "")
        return true
    return (cur = NormalizeLine(lastAuto))
}

; v5-capture-automation §6：目标词被改动时，**释义残留**的判定 —— 释义框里放着的
; 还是上一个词的自动释义（`lastAuto` 非空、且与当前值相等）→ 立刻清空它。
; 用户手打过的释义（与 lastAuto 不同）永不被动（`ShouldFillMeaning` 的边界保持）。
ShouldClearAutoMeaning(current, lastAuto) {
    la := NormalizeLine(lastAuto)
    if (la = "")
        return false
    return NormalizeLine(current) = la
}

; 从内核 OCR 响应里解析 `corrections`（§5.2 的形状：`[{"from","to","kind"}…]`）。
; AHK 没有 JSON 库，只按已知形状抽 `from`/`to`（可跨 `kind` 等无关键，任一次序）。
; 没有 `corrections` 键 / 空数组 / 形状不符 → 空数组。
; v22 P1a：字符串捕获同样用 possessive `*+`（同 `JsonStrOf`，防长字段 PCRE 回溯爆炸）。
ParseCorrections(json) {
    out := []
    pos := InStr(json, '"corrections"')
    if (!pos)
        return out
    seg := SubStr(json, pos)
    while (RegExMatch(seg, '"from"\s*:\s*"((?:[^"\\]|\\.)*+)"', &mf)) {
        rest := SubStr(seg, mf.Pos + mf.Len)
        if (!RegExMatch(rest, '"to"\s*:\s*"((?:[^"\\]|\\.)*+)"', &mt))
            break
        out.Push([UnescapeJson(mf[1]), UnescapeJson(mt[1])])
        seg := SubStr(rest, mt.Pos + mt.Len)
    }
    return out
}

; v17 W5（票 #142 / 决策 D1）：从内核 OCR 响应里解析 `suspects`（形状：
; `[{"token": "…", "start": N, "end": N}]`，偏移相对校正后 text、0 起左闭右开）。
; 手法与 `ParseCorrections` 同款：AHK 没有 JSON 库，只按已知形状抽字段（任一次序）。
; **字段缺失（旧内核）/ 空数组 / 形状不符 → 空数组** —— 新外壳把「没有这个字段」
; 当「无标记」处理，与旧内核双向兼容（任何消费方对空数组都是 no-op）。
ParseSuspects(json) {
    out := []
    pos := InStr(json, '"suspects"')
    if (!pos)
        return out
    seg := SubStr(json, pos)
    while (RegExMatch(seg, '"token"\s*:\s*"((?:[^"\\]|\\.)*+)"', &mt)) {
        rest := SubStr(seg, mt.Pos + mt.Len)
        if (!RegExMatch(rest, '"start"\s*:\s*(\d+)', &ms))
            break
        rest := SubStr(rest, ms.Pos + ms.Len)
        if (!RegExMatch(rest, '"end"\s*:\s*(\d+)', &me))
            break
        out.Push([UnescapeJson(mt[1]), ms[1] + 0, me[1] + 0])
        seg := SubStr(rest, me.Pos + me.Len)
    }
    return out
}

; 「已校正 N 处」的状态行文案（票 #27 补充 AC 的字面量）：
;   `已校正 2 处：eard→card、tha!f→that`；最多列 2 条，多余用「等 N 处」（N = 总处数）。
; 入参 pairs = [[from, to], …]；0 条 → ""（不显示）。
CorrectionsText(pairs) {
    if (!IsObject(pairs) || pairs.Length = 0)
        return ""
    n := pairs.Length
    shown := ""
    i := 0
    for pair in pairs {
        i++
        if (i > 2)
            break
        shown .= (shown = "" ? "" : "、") . pair[1] . "→" . pair[2]
    }
    if (n > 2)
        shown .= " 等 " . n . " 处"
    return "已校正 " . n . " 处：" . shown
}

; `YYYY-MM-DD HH:MM:SS` → `MM-DD`（不认的输入 → ""）。
MonthDay(ts) {
    if (RegExMatch(Trim(ts), "^\d{4}-(\d{2})-(\d{2})", &m))
        return m[1] . "-" . m[2]
    return ""
}

; 「已记过 N 次」的状态行文案（票 #27 补充 AC / §7 的 `seen`）：
;   `已记过 3 次 · 上次 10-12「这样练英语听力」`；count=0 / 无 seen → ""（不显示）。
; `last_at` 只取 `MM-DD`；`last_session` 为空则省略书名号那段。同理「以前记过没」
; 是用户要求 5「记忆功能」的另一半。
SeenText(seen) {
    if (!IsObject(seen))
        return ""
    count := ObjProp(seen, "count", 0) + 0
    if (count <= 0)
        return ""
    out := "已记过 " . count . " 次"
    md := MonthDay(ObjProp(seen, "last_at", ""))
    if (md != "")
        out .= " · 上次 " . md
    title := NormalizeLine(ObjProp(seen, "last_session", ""))
    if (title != "")
        out .= "「" . title . "」"
    return out
}

; 状态行的优先级合成（票 #27 补充 AC）：错误 > 已校正 > 已记过 > 词典查询中；
; 同屏只显示一条；都不在 → ""（§6.3「无内容时不占位」）。
; `note`（「释义已手打」这类提示）排在已记过之后、查询中之前：它没有前三条要紧，
; 但比「词典查询中…」更该让用户看见 —— 票面没列它，位次由票 #27 的实现要求给出。
StatusPick(errText, correctedText, seenText, dictWait, note := "") {
    if (errText != "")
        return errText
    if (correctedText != "")
        return correctedText
    if (seenText != "")
        return seenText
    if (note != "")
        return note
    return dictWait ? StatusText("dict-wait") : ""
}

; hub 恢复时状态行该被顶掉成什么（票 #184 / A5）：只有 `hub-offline` 是「hub 不
; 在位」的产物，恢复即失实（保存已全部成功）；dict-* 家族的清除在 LookupFire
; （新一轮查询时），不归恢复管。纯函数，harness 片 37 钉。
HubUpClearsStatus(status) {
    return (status = "hub-offline") ? "" : status
}

; ---------------------------------------------------------------------------
;  C5：热键冲突检测（v2 §9.2 三条）与状态文件的序列化 —— 全是纯函数（探测注入）。
;
;  HotkeyStatuses(entries, probe)：
;    entries = [{ action, spec, group, name }]（group ∈ "global" | "box"）
;    返回等长数组，每条：
;      { action, spec, group, name, state, detail, register, mods, vk }
;    state ∈ "ok" | "dup" | "taken" | "bad"；register = 这条是否应当注册。
;    · ③ 语法错误：ParseHotkeyV2 返回 ok:false → bad，detail 带原因；
;    · ① 组内重复：同一 group 内 mods+vk 相同的动作**全部**标 dup、都不注册
;      （全局组与框内组各判一次，两组互不影响）；
;    · ② 被外部占用：只判**全局组**（框内键有 HotIfWinActive 作用域，外部占用
;      不阻断它），probe(hk) 返回 false → taken、不注册。
;    `~`（放行前缀，票 #48）不进 mods/vk → `~!1` 与 `!1` 在①②里就是**同一个物理
;    组合**：重复检测与 OS 占用探测都按同一组合判（§9.2）。注册串始终取 rec.spec
;    原文（Trim 后的 config 值），原样交给 Hotkey()，绝不按解析结果重拼。
; ---------------------------------------------------------------------------
HotkeyStatuses(entries, probe) {
    out := [], groups := Map()
    for e in entries {
        r := ParseHotkeyV2(e.spec, e.group = "box")
        rec := { action: e.action, spec: Trim(e.spec), group: e.group, name: e.name
               , state: "ok", detail: "", register: false }
        if (!r.ok) {
            rec.state := "bad"
            rec.detail := "语法错误：" . r.reason
        } else {
            rec.mods := r.mods, rec.vk := r.vk
            k := e.group . "|" . r.mods . "|" . r.vk
            if (!groups.Has(k))
                groups[k] := []
            groups[k].Push(out.Length + 1)    ; 1-based 下标（本 rec 的落位）
        }
        out.Push(rec)
    }
    ; ① 组内重复（detail 形如 `重复：保存=取消=^s`，取整组动作名 + 键位）
    for k, idxs in groups {
        if (idxs.Length < 2)
            continue
        names := ""
        for i in idxs
            names .= (names = "" ? "" : "=") . out[i].name
        detail := "重复：" . names . "=" . out[idxs[1]].spec
        for i in idxs {
            out[i].state := "dup"
            out[i].detail := detail
        }
    }
    ; ③ → ② 探测（只对全局组里已判定 ok 的条目）
    for i, rec in out {
        if (rec.state != "ok")
            continue
        if (rec.group = "global" && probe && !probe({ mods: rec.mods, vk: rec.vk })) {
            rec.state := "taken"
            rec.detail := "被占用：RegisterHotKey 探测返回 0"
            continue
        }
        rec.register := true
    }
    return out
}

; 状态 → 框内键的屏蔽表：spec(trimmed) → detail（bad / dup 的框内键不注册）。
; 只管框内组；BoxKeys 的 RegBoxKey 查它。
BoxBlockedMap(statuses) {
    m := Map()
    for rec in statuses {
        if (rec.group = "box" && (rec.state = "bad" || rec.state = "dup"))
            m[rec.spec] := rec.detail
    }
    return m
}

; 一条状态 → 一行 JSON（复用 hub 侧同一套转义 JsonStrPair）。
HotkeyStatusLine(rec, probedAt) {
    return "{" . JsonStrPair("probed_at", probedAt)
         . "," . JsonStrPair("action", rec.action)
         . "," . JsonStrPair("spec", rec.spec)
         . "," . JsonStrPair("state", rec.state)
         . "," . JsonStrPair("detail", rec.detail) . "}"
}

; `data/hotkey-status.json` 的路径：与 spool 同目录（db 文件所在目录）。
HotkeyStatusPathOf(dbPath) {
    SplitPath(dbPath, , &dir)
    if (dir = "")
        return "hotkey-status.json"
    return dir . "\hotkey-status.json"
}

; ---------------------------------------------------------------------------
;  托盘菜单（v2 §6.5 / 票 #29）：一级平铺，项与顺序**一处定义**（SetupTray 只
;  照着建菜单、挂回调），harness 钉住。`action` 供主脚本映射到回调，空串 =
;  分隔线（§6.5 的「分隔线 / 退出」形态）。
;  `测试热键（…）` 的括号里是当前 `hotkey`，所以整表随 config 变。
; ---------------------------------------------------------------------------
TrayMenuItems(cfg) {
    return [ { label: "暂停热键",               action: "pause" }
           , { label: "打开取词台",             action: "review" }
           , { label: "打开数据目录",           action: "datadir" }
           ; v3 §7：拖动过的框要能一键回到鼠标处（复位 box_pos=auto）
           , { label: "速记框回到鼠标处",       action: "box-home" }
           , { label: "重启内核",               action: "restart-kernel" }
           , { label: "重启中枢",               action: "restart-hub" }
           , { label: "开机自启动",             action: "autostart" }
           , { label: TrayDefaultLabel(cfg),    action: "test-hotkey" }
           , { label: "切换深色",               action: "dark" }
           , { label: "",                       action: "" }        ; 分隔线
           , { label: "退出",                   action: "exit" } ]
}

; 默认项（托盘图标双击 / 单击的默认动作，§6.5 沿用 v1 的「测试热键」）：
; 与菜单里那条同名 —— 名字取自同一处，改了一处不会两边对不上。
TrayDefaultLabel(cfg) => "测试热键（" . Trim(cfg.hotkey) . "）"

; 数据目录（票 #29 / §6.5「打开数据目录」）= 数据库文件所在目录，即
; `<工具目录>\data`（默认 db_path = `%A_ScriptDir%\data\capture.db`）。
; 与 spool / hotkey-status.json 同一个口径：都以 db 文件为锚点。
DataDirOf(dbPath) {
    SplitPath(dbPath, , &dir)
    return (dir = "") ? "data" : dir
}

; ---------------------------------------------------------------------------
;  票 #178：给 RPC 请求行加编号，中枢原样回显，外壳据此对号入座。
;  字段叫 `_rpc_id` 而**不是** `id`：业务回包里本来就有 `id`（`article_create` 的
;  doc_id、`article_paragraphs` 的条目 id），`JsonNumber` 只能抓到第一个数字 —
;  重名时会把段落 id 当成回话编号，整条回话就被丢弃。
;  返回 { seq: 新序号, line: 带编号的请求行 }。
; ---------------------------------------------------------------------------
HubStampRequest(request, seq) {
    next := seq + 1
    return { seq: next, line: '{"_rpc_id":' . next . ',' . SubStr(request, 2) }
}

; ---------------------------------------------------------------------------
;  票 #178：回话 ↔ 在途请求的对号入座（替换纯位置 FIFO）。
;
;  为什么换：位置 FIFO 假设「一行请求恰好一行回话、顺序一致」，任何一次「少一行 /
;  多一行」（TCP 短写粘行 → hub 回一条 bad json、hub 重启、坏行）都会让队列相对
;  线上**永久**错一格 —— 2026-10-29 实测 41 分钟里 1 Hz 的 pong 被 lookup 槽吃掉，
;  释义框整片空白。改成请求自带 `"id":<seq>`、中枢原样回显、外壳按 id 取 ask。
;
;  参数：asks = 在途队列（元素 { id, kind, gen }）；replyId = 回话里的 id
;        （0 = 回话没带 id → 旧中枢，或无 id 的坏 json 回包）。
;  返回 { ask, mode, dropped }：
;    ask     = 命中的 ask（`""` = 这条回话不归任何在途请求）
;    mode    = "id"（按 id 命中）/ "fifo"（旧中枢退化到队首）/ "unknown" / "empty"
;    dropped = 因这条回话而作废的前序 ask 数（TCP 保序：id 命中时它前面的回话
;              要么已经到过、要么永远不会来）
;  ！副作用：被消费/作废的 ask 从 asks 里移除（数组按引用传递）。
; ---------------------------------------------------------------------------
HubPickAsk(asks, replyId) {
    if (asks.Length = 0)
        return { ask: "", mode: "empty", dropped: 0 }
    if (!replyId)
        ; 旧中枢（回话不带 id）：退回位置 FIFO —— 它仍守「一行请求一行回话」
        return { ask: asks.RemoveAt(1), mode: "fifo", dropped: 0 }
    hit := 0
    for idx, a in asks {
        if (ObjProp(a, "id") = replyId) {
            hit := idx
            break
        }
    }
    if (!hit)
        return { ask: "", mode: "unknown", dropped: 0 }
    ask := asks[hit]
    dropped := hit - 1
    loop dropped
        asks.RemoveAt(1)
    asks.RemoveAt(1)
    return { ask: ask, mode: "id", dropped: dropped }
}

; ---------------------------------------------------------------------------
;  v35 #236：StatusVisual —— 相位 → 视觉的唯一真值（角色窗 / 玻璃卡共用）
;
;  spec：docs/specs/v35-desktop-pet-and-status-overlay.md §相位 → 视觉映射、
;        §两个窗口、一个状态源；票 #236。
;
;  纯函数：只读入参（相位 + 配置快照），不建窗、不碰全局、不拉连接。
;  角色窗只取 face / motion；玻璃卡只取 expanded / text / action / tone / color。
;  **两窗都不许自己判断相位** —— 展开条件与文案只在这一处（片 46 + 源码钉子钉着）。
;
;  输出：
;    phase    原样回显的相位（未知相位也回显，便于日志）
;    known    是否 11 档词表内的相位
;    face     状态脸 id（= data/pet/face/<id>/ 目录名，由 #237 烘焙）
;    motion   一次性动作 id（"" = 不播；= data/pet/motion/<id>/）
;    expanded 玻璃卡是否展开（只有「要用户动手」的四档为 true）
;    textKey  文案稳定键（气泡冷却按它去重，见 #233；静默相位为空串）
;    text     玻璃卡上那 ≤2 行字（键位取 config 真值；静默相位为空串）
;    action   卡上唯一按钮的动作 id（"" = 无按钮）：dismiss / redrag
;    tone     语气档 ok | warn | error | idle（浮球圆点与卡的强调色）
;    color    与 v34 HUD 同一套相位色（既有相位色钉子不许变）
; ---------------------------------------------------------------------------

; --- 11 档相位 → 视觉决策的常量表（#233 的气泡/引导复用同一份 textKey 与 tone）---
;  face / motion 的键名是主 agent D-L7 的烘焙产物契约（expressions / motions），
;  不许自创：换键名等于让 #237 找不到帧。expanded 只有「要用户动手」的四档为真。
StatusPhaseTable() {
    return Map(
          "unarmed",       { face: "drool",    motion: "",           expanded: false, textKey: "",             action: "",        tone: "idle",  color: "cB0BEC5" }
        , "no_heartbeat",  { face: "question", motion: "",           expanded: true,  textKey: "no_heartbeat",  action: "dismiss", tone: "warn",  color: "cFFB300" }
        , "cc_reading",    { face: "focus",    motion: "selfie",     expanded: false, textKey: "",             action: "",        tone: "idle",  color: "cB0BEC5" }
        , "cc_ok",         { face: "focus",    motion: "open_lid",   expanded: false, textKey: "",             action: "",        tone: "ok",    color: "c00E676" }
        , "cc_missing",    { face: "focus",    motion: "",           expanded: false, textKey: "",             action: "",        tone: "idle",  color: "cB0BEC5" }
        , "cc_error",      { face: "sweat",    motion: "",           expanded: false, textKey: "",             action: "",        tone: "warn",  color: "cFFB300" }
        , "sampling",      { face: "focus",    motion: "",           expanded: false, textKey: "",             action: "",        tone: "ok",    color: "c00E676" }
        , "paused",        { face: "drool",    motion: "",           expanded: false, textKey: "",             action: "",        tone: "idle",  color: "cB0BEC5" }
        , "dormant",       { face: "exclaim",  motion: "",           expanded: true,  textKey: "dormant",       action: "redrag",  tone: "warn",  color: "cFFB300" }
        , "redrag_needed", { face: "question", motion: "",           expanded: true,  textKey: "redrag_needed", action: "redrag",  tone: "warn",  color: "cFFB300" }
        , "failed",        { face: "sweat",    motion: "",           expanded: true,  textKey: "failed",        action: "dismiss", tone: "error", color: "cEF5350" }
    )
}

; 视频播完 → 收句该自动停吗（2026-11-10 用户要求「播放完毕自动暂停收句结束」）。
;   armed      = 现在真的在收句（没开就没什么可停，别白发 capture_arm off）
;   ended      = 本拍 ping 的 cap_ended（hub 判的「播到末尾停住」，不是中途暂停）
;   streak     = 连续多少拍 ended（外壳数）
;   needStreak = 连几拍才算（默认 2）
; 为什么要连拍：B站 末尾 `paused` 会抖（缓冲收尾 / 自动连播切换那一瞬），单拍就停
; 容易把「还在记最后一句」误杀；连两拍（≈2s）抖动就被抹掉了。
CaptureEndedShouldStop(armed, ended, streak, needStreak := 2) {
    if (!armed || !ended)
        return false
    return streak >= needStreak
}

; 自动停收句后说的一句话（走唯一发言出口；不带键位，带「去挑词」按钮）。
; 2026-11-11：用户要求「播完主动问我要不要跳转到取词台选词」——按钮动作是
; PetPickOpen（与桌宠菜单第 2 项同一条路径）。
CaptureEndedStopText() {
    return "这条播完了，收句我先停了。要现在挑词吗？"
}

; 2026-11-10：无画面字源 → 也自动停收句（ASR 二期接入前的临时保底）。
; 与「播完」同构：armed 开着 + cap_no_source=1 + 连两拍（hub 判定不该被单拍
; 抖动带跑）。复用同一个 10s 冷却，两个停机入口不会互相打架。
CaptureNoSourceShouldStop(armed, noSource, streak, needStreak := 2) {
    if (!armed || !noSource)
        return false
    return streak >= needStreak
}

; 停机时说的一句：讲清「没有可读字幕」+「ASR 是没启用还是失败了」+「怎么继续」。
; 不叫用户换片 —— 无字幕听力练习本来就是他要的片型。
; 2026-11-11：ASR 已接入，失败后再落回保底时不能再谎称「还没接上」。
CaptureNoSourceStopText(asr := "") {
    if (asr = "error")
        return "这条没有画面字幕，音频转写（ASR）也失败了，收句我先停了。"
    return "这条没有画面字幕，收句我先停了——ASR 未启用或没接上，开启后这类片就能记了。"
}

StatusVisual(phase, cfg := "") {
    p := (Type(phase) = "String") ? Trim(phase) : ""
    table := StatusPhaseTable()
    known := table.Has(p)
    ; 未知相位：兜底到安静待机（不抛错、不展开、不出字；渲染侧按专注脸画）
    row := known ? table[p] : { face: "focus", motion: "", expanded: false, textKey: ""
                              , action: "", tone: "idle", color: "cB0BEC5" }
    return { phase: p, known: known, face: row.face, motion: row.motion
           , expanded: row.expanded
           , textKey: row.textKey
           , text: row.expanded ? StatusCardText(row.textKey, cfg) : ""
           , action: row.action, tone: row.tone, color: row.color }
}

; 文案表：textKey → 玻璃卡上那 ≤2 行字。键位取配置快照真值（改 config 跟着变）。
; 静默相位 textKey 为空 → 空串（不出字）。#233 的气泡复用同一份 textKey 做冷却键。
StatusCardText(key, cfg) {
    if (key = "no_heartbeat")
        return "页面切走了，回到视频我接着记"
    if (key = "dormant")
        return "先框一下字幕带（" . StatusKeyText(cfg, "hotkeyRedrag", "^!6") . "），我才知道记哪儿"
    if (key = "redrag_needed")
        return "字幕位置变了，重新拖一下？（" . StatusKeyText(cfg, "hotkeyRedrag", "^!6") . "）"
    if (key = "failed")
        return "识别引擎挂了，看看日志？"
    return ""
}

; 文案里的键位：取配置快照的真值（缺键 / 空值回落代码缺省），再渲成人读形态。
StatusKeyText(cfg, name, def) {
    v := ObjProp(cfg, name, def)
    if (Trim(v) = "")
        v := def
    return PrettyKey(v)
}

; ---------------------------------------------------------------------------
;  v35 #231：玻璃卡的纯逻辑（材质轨 / 落位 / 按钮文案）
;
;  spec：docs/specs/v35-desktop-pet-and-status-overlay.md §毛玻璃（双轨）、
;        §尺寸、位置与收起；票 #231。窗口与绘制在 lib/GlassCard.ahk，这里只留
;        能纯函数断言的三件（窗口样式/绘制进不了单测，由源码钉子管）。
; ---------------------------------------------------------------------------

; 双轨材质选择：real = 系统背景材质，fake = 假玻璃（深色渐变 + 噪点 + 内高光 + 1px 亮边）。
; auto / 未知值**保守走 fake** —— #230 spike 未产出真材质结论前不押注；要真材质用
; `pet_glass=real` 显式打开，真机验收不过再退回 fake（开关可回滚）。
GlassCardMaterialMode(mode, realOk) {
    m := (Type(mode) = "String") ? Trim(mode) : ""
    if (m = "real")
        return realOk ? "real" : "fake"
    return "fake"
}

; 卡片落位：水平居中在锚点（角色头顶中心）上方 gap 像素处，夹进虚拟屏幕。
; 单位一律物理像素（调用方用 P() 换算）；副屏的负坐标同样成立。
GlassCardPlace(w, h, ax, ay, vr, gap) {
    m := 4                                  ; 屏幕边留 4px 呼吸位
    x := ax - w // 2
    x := Max(vr.x + m, Min(x, vr.x + vr.w - w - m))
    y := ay - h - gap
    if (y < vr.y + m)
        y := vr.y + m
    return { x: x, y: y }
}

; 卡上唯一按钮的文案：只按 StatusVisual 的 action 出（不判断相位）。
GlassCardButtonLabel(action) {
    if (action = "redrag")
        return "重拖一下"
    if (action = "dismiss")
        return "知道了"
    return ""
}

; ---------------------------------------------------------------------------
;  v35 #233：气泡与 4 步引导的纯逻辑
;
;  spec：docs/specs/v35-desktop-pet-and-status-overlay.md §气泡与引导；
;        票 #233。窗口在 lib/PetBubble.ahk，这一节只留能纯函数断言的部分：
;        文案表（键位取 config 真值）/ 同类 10 分钟冷却 / 勿扰静默 / 一次只一条 /
;        60 秒待挑轮询 / 4 步引导的判据分级与进度。
; ---------------------------------------------------------------------------

global PET_BUBBLE_COOLDOWN_MS := 10 * 60 * 1000   ; 同类气泡 10 分钟冷却（spec）
global PET_PENDING_POLL_MS := 60 * 1000           ; 待挑计数 60 秒低频轮询（spec）

; 情境气泡的种类（顺序即优先级：一次只弹一条，先喊最该喊的那句）。
PetBubbleKinds() {
    return ["error", "redrag", "pending", "band_cleanup"]
}

; kind + 配置快照 + 上下文 → 气泡文案（"" = 这类现在没话说，不弹）。
; 文案表外置在这一个函数里（不接大模型、零外部请求）；键位一律 config 真值。
PetBubbleText(kind, cfg, ctx := "") {
    if (kind = "error")
        return "收句出错了，看看日志？"
    if (kind = "redrag")
        return "字幕带还没框好，按 " . StatusKeyText(cfg, "hotkeyRedrag", "^!6") . " 拖一下"
    if (kind = "pending") {
        n := ObjProp(ctx, "pending", 0)
        if (n <= 0)
            return ""
        return "攒了 " . n . " 句待挑，去挑几个词？"
    }
    if (kind = "band_cleanup") {
        bytes := ObjProp(ctx, "bandBytes", 0)
        if (bytes <= 0)
            return ""
        return "这个视频的底图占 " . PetSizeText(bytes) . "，要收掉吗？"
    }
    return ""
}

; 文件占用的人读形态（底图清理气泡；底图线的占用数字原样传进来）。
PetSizeText(bytes) {
    if (bytes >= 1024 * 1024 * 1024)
        return Format("{:.1f}", bytes / (1024 * 1024 * 1024)) . " GB"
    if (bytes >= 1024 * 1024)
        return Round(bytes / (1024 * 1024)) . " MB"
    if (bytes >= 1024)
        return Round(bytes / 1024) . " KB"
    return Round(bytes) . " B"
}

; Map 取值（缺失给默认）。lastAt 用 Map(kind → A_TickCount)，不是普通对象。
PetMapGet(m, k, def := 0) {
    if (Type(m) = "Map" && m.Has(k))
        return m[k]
    return def
}

; 同类 10 分钟冷却 + 勿扰静默。lastAt = Map(kind→上次弹出时刻)；0 = 没弹过。
; `quiet`（勿扰）= 全静默：动作/状态照旧，只是不弹气泡（spec §勿扰）。
PetBubbleAllowed(kind, lastAt, now, quiet) {
    if (quiet)
        return false
    last := PetMapGet(lastAt, kind, 0)
    if (last = 0)
        return true
    d := now - last                        ; TickCount 会回绕，负数按「刚刚弹过」算
    return d >= 0 && d >= PET_BUBBLE_COOLDOWN_MS
}

; 候选按优先级取第一条「该弹且有文案」的（一次只显示一条）。
; 返回 { kind, text }；没有可弹的返回 0。
PetBubbleChoose(candidates, cfg, ctx, lastAt, now, quiet) {
    for kind in candidates {
        if (!PetBubbleAllowed(kind, lastAt, now, quiet))
            continue
        text := PetBubbleText(kind, cfg, ctx)
        if (text = "")
            continue
        return { kind: kind, text: text }
    }
    return 0
}

; 60 秒低频轮询是否到点（不进 1Hz ping、不改扁平字段约定）。
PetPendingDue(lastAt, now) {
    return (lastAt = 0) || (now - lastAt >= PET_PENDING_POLL_MS)
}

; 从 `GET /api/sessions` 回包文本里把每条会话的 `subtitle_pending` 加起来。
; 纯字符串解析：喂一段 JSON 文本就能断言（外壳不做完整 JSON 解析）。
PetPendingCountFromSessions(json) {
    total := 0, pos := 1
    while (pos := RegExMatch(json, '"subtitle_pending"\s*:\s*(\d+)', &m, pos)) {
        total += m[1] + 0
        pos += StrLen(m[0])
    }
    return total
}

; --- 4 步首次引导（顺序即 spec §6 的教学顺序）--------------------------------
PetTourSteps() {
    return ["arm", "redrag", "pick", "vocab"]
}

PetTourText(step, cfg) {
    if (step = "arm")
        return "我是鲸鱼娘，看视频时按 "
             . StatusKeyText(cfg, "hotkeyCaptureArm", "^!7") . " 我才开始记字幕"
    if (step = "redrag")
        return "先框一下字幕带（"
             . StatusKeyText(cfg, "hotkeyRedrag", "^!6") . "），或者等 CC 直读"
    if (step = "pick")
        return "句子攒在池子里，点我 →「挑词」"
    if (step = "vocab")
        return "挑完的词会进词条库，复习我提醒你"
    return ""
}

; 判据分级（研究 §6 的核查结论）：
;   arm    —— armed 只在内存、从不落库 → **判不了**，只在首启讲一次；
;   redrag —— 可判：区域持久化在 config.ini（`region_<w>x<h>`），没有区域就是没拖过；
;   pick   —— 可判：backlog 的 picked_at 非空即挑过；
;   vocab  —— 由 pick 推出（挑词必生成词条）：**挑过之后**再讲它，不另设判据。
; progress = Map(step → true 已讲/已跳过)；evidence = { hasRegion, hasPicked }。
; 返回下一步 step；"" = 不用再讲。
PetTourNext(progress, evidence) {
    if (!PetTourDone(progress, "arm"))
        return "arm"
    if (!PetTourDone(progress, "redrag") && !ObjProp(evidence, "hasRegion", false))
        return "redrag"
    if (!PetTourDone(progress, "pick") && !ObjProp(evidence, "hasPicked", false))
        return "pick"
    if (!PetTourDone(progress, "vocab") && ObjProp(evidence, "hasPicked", false))
        return "vocab"
    return ""
}

PetTourDone(progress, step) {
    if (Type(progress) != "Map")
        return false
    return progress.Has(step) && progress[step]
}

; 跳过引导：剩余步骤全记为已讲，另记 skipped 位（重放入口靠它复位）。
PetTourSkipAll(progress) {
    out := Map()
    if (Type(progress) = "Map") {
        for k, v in progress
            out[k] := v
    }
    for step in PetTourSteps()
        out[step] := true
    out["skipped"] := true
    return out
}

; 重放（右键菜单 / `?tour=1`）：清空进度，下一拍从第 1 步重新讲。
PetTourReplay() {
    return Map()
}

; config.ini 文本里有没有持久化的拖框区域（键名 `region_<w>x<h>`，值非空）。
PetTourHasRegion(configText) {
    for line in StrSplit(configText, "`n", "`r") {
        t := Trim(line)
        if (t = "" || SubStr(t, 1, 1) = ";" || SubStr(t, 1, 1) = "[")
            continue
        if (RegExMatch(t, "^region_[0-9]+x[0-9]+\s*=\s*\S"))
            return true
    }
    return false
}

; `GET /api/sessions/{id}/backlog` 回包文本里有没有挑过的句子（picked_at 非空）。
PetHasPickedFromBacklog(json) {
    return RegExMatch(json, '"picked_at"\s*:\s*"[^"]+"') > 0
}

; ---------------------------------------------------------------------------
;  v35 D5：主动性引擎（「活人感」）—— 场景识别 + 文案池 + 频率闸门
;
;  design：.scratch/v35-ux/design.md §D5（反馈 1/6/8）；接线在 英语取词.ahk 的
;  CaptureStatusApply（场景快照来自 1Hz ping 字段）与 OnHubLine 的 flush 槽
;  （挑词保存成功）。这里只有纯函数：不发请求、不建窗、不碰全局状态。
;
;  结构：
;    PetChatterPool      每类 3–5 条文案变体（常量表，可被测试逐条断言）
;    PetChatterPick      kind + 上下文 + rnd → 文案（键位一律 config 真值）
;    PetChatterGate      频率闸门：全局 ≥60s / 同类 ≥3min（invite ≥6min、
;                        video_invite ≥2min）/ 每日上限（video_invite ≤20、
;                        invite ≤6、深夜关心 ≤1）/ 勿扰静默
;    PetChatterCandidates 场景快照 → 本拍全部候选发言（不判闸门、不排先后）
;    PetSpeechArbitrate  本拍所有候选 + 闸门状态 → 该说哪一条（#244 的唯一真值）
;    PetAmbientShouldPlay / PetAmbientNextAt   随机 idle 插播的判定（A2 的
;                        PetIdleAmbientTick 调它；warn/error 相位一律不插播）
;
; kind 词表：invite / video_invite（v36 场景邀请）/ new_session / milestone /
; caring / praise / idle_talk + night（深夜关心要「每天 ≤1」的独立冷却桶与每日
; 计数，与 30/60 分钟的 caring 里程碑分开，否则一次夜间关心会占掉 caring 的冷却位）。
;
;  人设：元气、爱英语、不啰嗦、不称呼头衔；键位一律 StatusKeyText 的 config
;  真值（文案里写 {arm} 占位，Pick 时替换），不写死 ^!7。
; ---------------------------------------------------------------------------

; v36 #247 重校准（旧值 v35）：闸门数值的唯一真值。旧值「同类 10 分钟 / 邀请
; 20 分钟 / 每日 3 次」与用户现在要的「更主动」直接冲突（spec 决策 5、诊断 §4.2
; 的冲突表），按「换情境可重新开口、同一情境不重复唠叨」重定：
;   · 全局间隔 90s → 60s：它只是「别在一分钟内连弹两句」的地板；真正决定密度的是
;     同类冷却。压到 60s 后「换情境马上开口」不会被上一句任意发言挡住一分钟以上。
;   · 人设类同类冷却 10min → 3min（票里给的 2–4 分钟档取中）：闲聊 / 里程碑 /
;     关心这些「陪伴型」发言一分钟一句没人受得了，三分钟一句才既不哑也不吵。
;   · 泛化邀请 20min → 6min：待命（unarmed）段以前几乎不说话（诊断 §4.2「零报告」
;     是敌人）；6 分钟是「提醒但不唠叨」，因为 invite 是**要用户动手**的那类。
;   · 场景邀请 20min → 2min：同一视频只邀一次由「场景正边沿 + 待发位」保证，冷却
;     只用来挡边沿抖动（暂停/播放、页面内跳转），不需要 20 分钟那么久。
global PET_CHATTER_GLOBAL_GAP_MS := 60 * 1000        ; 任意主动发言的全局间隔
global PET_CHATTER_KIND_GAP_MS := 3 * 60 * 1000      ; 人设类同类发言冷却（2–4 分钟档）
global PET_INVITE_GAP_MS := 6 * 60 * 1000            ; 泛化邀请（待命段的提醒）
global PET_SCENE_INVITE_GAP_MS := 2 * 60 * 1000      ; 场景邀请（带按钮的邀请）
global PET_AMBIENT_MIN_MS := 2 * 60 * 1000           ; 随机插播的下一次最早（2–4 分钟，v35-ux 验收调密）
global PET_AMBIENT_MAX_MS := 4 * 60 * 1000

; 文案池：kind → 变体数组。占位符：{arm} = 收句键位真值；{n} = 句数/词数；
; {m} = 连续记录分钟数。鲸鱼娘人设：元气、爱英语、不啰嗦、不称呼头衔。
PetChatterPool() {
    return Map(
        "invite", [
              "在看视频吗？按 {arm} 我就开始帮你记台词啦"
            , "想开收句的话，按一下 {arm} 就好"
            , "这段挺有意思的吧？按 {arm}，台词交给我"
            , "台词池还空着呢，按 {arm} 我就开始记"
        ],
        "video_invite", [
              "在看 B站视频？要我帮你记台词吗"
            , "这个视频挺对味，开始收句吗？"
            , "看上了？我可以边播边帮你记台词哦"
        ],
        "new_session", [
              "新的视频？我已经就位了"
            , "换新视频啦，我盯着呢"
            , "就位！这部的台词都归我记"
        ],
        "milestone", [
              "本会话第 {n} 句！节奏很好哦"
            , "{n} 句啦，你记台词的手感正热"
            , "第 {n} 句达成，继续保持"
        ],
        "caring", [
              "已经连续记了 {m} 分钟，歇歇眼睛吧"
            , "{m} 分钟啦，喝口水再继续"
            , "连轴转 {m} 分钟，肩膀别僵住呀"
        ],
        "night", [
              "这么晚了还在看，记得早点休息哦"
            , "深夜学习达人就是你，不过别熬太晚啦"
            , "夜深了，我陪你把这段记完"
        ],
        "praise", [
              "收了 {n} 个词，今天也满载而归"
            , "挑词 +{n}，词条库又胖了一点"
            , "{n} 个词入库，这波赚到了"
        ],
        "idle_talk", [
              "这段已经记了 {n} 句啦"
            , "悄悄说，这段已经 {n} 句了哦"
            , "第 {n} 句了，素材越攒越多啦"
        ]
    )
}

; 每类发言的每日上限（0 = 不限次，只受间隔管）。#247 起这些上限的角色变了：
; 它们是**跑飞保险**（防场景边沿抖动 / 候选每拍重复触发把气泡刷满），不再承担
; 频率控制 —— 频率由同类冷却管。
;   · 场景邀请 20：同一视频只邀一次由边沿保证，一天看 20 个以上视频才可能碰到；
;   · 泛化邀请 6：待命段总共 6 次提醒，够「偶尔开口」又不至于变成催办；
;   · 深夜关心 1：再暖的话一天一次也够了。
PetChatterDailyMax(kind) {
    if (kind = "video_invite")
        return 20
    if (kind = "invite")
        return 6
    if (kind = "night")
        return 1
    return 0
}

; 同类发言的最小间隔（#247 分档）：场景邀请 2min（边沿抖动挡板）、泛化邀请
; 6min（要用户动手，最克制）、其余人设类统一 3min。
PetChatterKindGapMs(kind) {
    if (kind = "video_invite")
        return PET_SCENE_INVITE_GAP_MS
    if (kind = "invite")
        return PET_INVITE_GAP_MS
    return PET_CHATTER_KIND_GAP_MS
}

; kind + 上下文 + rnd → 文案（"" = 这类现在没话说）。rnd 决定挑哪条变体：
; 同参必同果（单测断言决定性）；键位占位 {arm} 一律 StatusKeyText 的 config
; 真值（缺键回落 ^!7 缺省），数字占位 {n}/{m} 由调用方塞进 ctx。
; ctx 只读 cfg / n / m 三个字段（PetChatterOut 会替各场景填好）。
PetChatterPick(kind, ctx, rnd) {
    pool := PetChatterPool()
    if (!pool.Has(kind))
        return ""
    variants := pool[kind]
    n := variants.Length
    if (n = 0)
        return ""
    text := variants[Mod(Abs(rnd), n) + 1]
    if (InStr(text, "{arm}"))
        text := StrReplace(text, "{arm}", StatusKeyText(ObjProp(ctx, "cfg", ""), "hotkeyCaptureArm", "^!7"))
    if (InStr(text, "{n}"))
        text := StrReplace(text, "{n}", String(ObjProp(ctx, "n", 0)))
    if (InStr(text, "{m}"))
        text := StrReplace(text, "{m}", String(ObjProp(ctx, "m", 0)))
    return text
}

; 频率闸门（全纯）。参数：
;   now            当前时刻（A_TickCount 口径，测试可传任意单调数）
;   lastAnyAt      上次**任意**主动发言的时刻（0 = 从未说过）
;   kindLastAt     上次**该类**发言的时刻（0 = 从未说过）
;   kind           发言种类（决定间隔与每日上限）
;   kindCountToday 该类今日已发言次数（kindDay ≠ todayStamp 时视为 0）
;   kindDay        该类计数所属的日戳（接线侧换天整体清零）
;   todayStamp     当前日戳
;   quiet          勿扰（spec：动作/状态照旧，主动发言全静默）
; TickCount 会回绕：负差值按「刚刚说过」算（同 PetBubbleAllowed 手法）。
PetChatterGate(now, lastAnyAt, kindLastAt, kind, kindCountToday, kindDay, todayStamp, quiet := false) {
    if (quiet)
        return false
    d := now - lastAnyAt
    if (lastAnyAt > 0 && (d < 0 || d < PET_CHATTER_GLOBAL_GAP_MS))
        return false
    d := now - kindLastAt
    if (kindLastAt > 0 && (d < 0 || d < PetChatterKindGapMs(kind)))
        return false
    cap := PetChatterDailyMax(kind)
    if (cap > 0) {
        count := (kindDay = todayStamp) ? kindCountToday : 0
        if (count >= cap)
            return false
    }
    return true
}

; chatter 命中时的动作表情（烘焙契约的 6 个动作键，不自创键名；design D5）。
; rnd 只用来在同级里随机（milestone 的自拍/快速自拍）。
PetChatterAction(kind, rnd) {
    if (kind = "new_session")
        return "open_lid"
    if (kind = "video_invite")
        return PetSceneInviteAction(rnd)
    if (kind = "milestone")
        return (Mod(Abs(rnd), 2) = 0) ? "selfie" : "selfie_fast"
    if (kind = "caring" || kind = "night")
        return "stretch"
    if (kind = "praise")
        return "selfie_fast"
    return ""
}

; 本拍 chatter 侧的全部候选（纯函数：只按场景快照判「有没有话说」，不判闸门、
; 不判静默、也不定先后 —— 闸门与优先级是 PetSpeechArbitrate 的事）。返回候选
; 数组（可能为空）。场景规则（design D5）：
;   1. milestone   armed 中本会话句数跨过 10/50/100（同会话内增加才算）
;   2. new_session 会话 id 从 0/异值切到新值（>0）
;   3. night       深夜（0–6 点）armed 中（每天 ≤1，闸门管）
;   4. caring      连续 armed 记录跨过 30/60 分钟
;   5. invite      收句未开且 ping 活着（含「刚把会话记完」—— 同一文案池，
;                  频率由闸门管：#247 起全局 60s + 同类 6min + 每天 ≤6）
;   6. idle_talk   正常收句中（sampling / cc_ok）低频闲聊，句数 >0 才说
;
; ctx 字段（全部 ObjProp 容错读取，缺省即上表默认）：
;   now / rnd / cfg / quiet / bubbleBusy
;   armed / pingAlive / hour / sceneInvite
;   session / prevSession / added / prevAdded
;   armedMin / armedMinPrev    （连续 armed 的分钟数：本拍与上一拍，判「跨过」）
;   phase / lastAnyAt / lastKindAt(Map) / kindCount(Map) / kindDay(Map) / today
PetChatterCandidates(ctx) {
    out := []
    armed := ObjProp(ctx, "armed", false)
    session := ObjProp(ctx, "session", 0)
    prevSession := ObjProp(ctx, "prevSession", 0)
    added := ObjProp(ctx, "added", 0)
    prevAdded := ObjProp(ctx, "prevAdded", 0)
    rnd := ObjProp(ctx, "rnd", 0)
    ; 1) milestone：10/50/100 的跨点（一拍跨多点取最大的那档）
    if (armed && session > 0 && session = prevSession && added > prevAdded) {
        hit := 0
        for t in [10, 50, 100] {
            if (prevAdded < t && added >= t)
                hit := t
        }
        if (hit > 0)
            PetPushCandidate(out, PetChatterOut("milestone", ctx, added, 0, rnd))
    }
    ; 2) new_session：新会话就位（开盖动作）
    if (session > 0 && session != prevSession)
        PetPushCandidate(out, PetChatterOut("new_session", ctx, 0, 0, rnd))
    ; 3) night：深夜关心（hour 0–5，armed 中）
    hour := ObjProp(ctx, "hour", -1)
    if (armed && hour >= 0 && hour < 6)
        PetPushCandidate(out, PetChatterOut("night", ctx, 0, 0, rnd))
    ; 4) caring：连续 armed 跨过 30/60 分钟（一拍跨两档取大的）
    armedMin := ObjProp(ctx, "armedMin", 0)
    armedMinPrev := ObjProp(ctx, "armedMinPrev", 0)
    if (armed) {
        crossed := 0
        for t in [30, 60] {
            if (armedMinPrev < t && armedMin >= t)
                crossed := t
        }
        if (crossed > 0)
            PetPushCandidate(out, PetChatterOut("caring", ctx, 0, crossed, rnd))
    }
    ; 5) invite：没开收句 + ping 活着（心跳没断或从未收到都算「活着」——
    ;    unarmed 时中枢不追心跳，只有明确断了才算「不在」）。
    ;    P0-C：场景邀请在场时**不出泛化邀请** —— 同一个意思只留带按钮的那条，
    ;    否则「先弹一句没按钮的话」会继续存在（只是换了个先后而已）。
    if (!armed && ObjProp(ctx, "pingAlive", false) && !ObjProp(ctx, "sceneInvite", false))
        PetPushCandidate(out, PetChatterOut("invite", ctx, 0, 0, rnd))
    ; 6) idle_talk：正常收句中的低频闲聊（基于真实句数，0 句不开口）
    if (armed && added > 0 && PetChatterIdlePhase(ObjProp(ctx, "phase", "")))
        PetPushCandidate(out, PetChatterOut("idle_talk", ctx, added, 0, rnd))
    return out
}

; 候选数组只收真正的对象（PetChatterOut 在「这类没话可说」时返回空串）。
PetPushCandidate(list, cand) {
    if (IsObject(cand))
        list.Push(cand)
}

; 正常收句中的相位（idle_talk 只在这些档说；相位字面量只出现在本文件）。
PetChatterIdlePhase(phase) {
    return phase = "sampling" || phase = "cc_ok"
}

; ---------------------------------------------------------------------------
;  v36 #244：发言仲裁 —— 一拍只出一条（spec 决策 4/5）
;
;  过去多条发言链各自判条件、先算完的先说话，互相抢气泡位（诊断 §1.4 P0-C：
;  泛化 invite 抢死了带按钮的 video_invite）。现在接线侧只「收集本拍所有候选
;  发言」，由这里的纯函数按固定优先级 + 闸门选一条；执行（气泡 / 动作 /
;  记冷却 / 日志）只有英语取词.ahk 的 PetSpeechSay 一处。
; ---------------------------------------------------------------------------

; 发言种类 → 优先级（数字越小越先说话）。阶梯（spec 决策 4）：
;   场景邀请 10 > 字幕带问询 20 > 需动手相位 30 > 里程碑/会话 40–44
;   > 泛化邀请 50 > 闲聊 60（引导 48：首次教用法，排在闲聊前、人设发言后）。
; #246 的「来源播报」15：排在场景邀请之后、字幕带问询之前 —— 它是**这一条怎么记**
; 的真相，用户听完才知道要不要动手；同时它必须压过 band_ask，否则「没有 CC」会
; 先被问询说一遍、来源播报再补一遍（同一个事实说两遍）。
; 未登记的种类 = 900（最低）—— 新种类在这里加一行就能进同一份优先级。
PetSpeechPriority(kind) {
    static table := Map(
          "video_invite", 10
        , "cc_report", 15                  ; 来源播报：这条有 CC / 只能屏幕采
        , "band_ask", 20
        , "no_source", 25                  ; 无画面字源终态：压过故障/重拖提醒
        , "capture_end", 25                ; 视频播完终态（带「去挑词」按钮）
        , "asr_done", 25                   ; ASR 转写完成（带「去挑词」按钮）
        , "redrag", 30                     ; 需动手相位：要用户拖框
        , "error", 30                      ; 需动手相位：故障提醒
        , "asr_error", 30                  ; ASR 转写失败：降级说明
        , "milestone", 40
        , "new_session", 41
        , "praise", 42
        , "night", 43
        , "caring", 44
        , "tour", 48
        , "invite", 50
        , "idle_talk", 60
        , "pending", 65)                  ; 待挑低频提醒（#244 收尾复核：也进同一仲裁阶梯）
    if (table.Has(kind))
        return table[kind]
    return 900
}

; 闸门策略（唯一的闸门真值仍是 PetChatterGate，这里只决定闸哪几道）：
;   full —— 全局间隔 + 同类间隔 + 每日上限（人设发言与场景邀请）
;   kind —— 只同类间隔（需动手相位提醒：故障/拖框提示不能被「刚说过一句话」
;           压掉，但也不能每分钟重复唠叨）
;   none —— 不设闸门（字幕带问询：dormant 正边沿 + 待弹位天然只出一次；
;           来源播报：候选层按 cap_session 去重，同一视频一次 —— 全局 60s /
;           同类 3min 会压掉「换视频重新开口」，所以不能吃闸门；
;           无画面字源：一次性终态，靠 Armed/off 边沿天然只出一回）。
PetSpeechGateMode(kind) {
    if (kind = "band_ask" || kind = "cc_report" || kind = "no_source" || kind = "capture_end" || kind = "asr_done")
        return "none"
    if (kind = "redrag" || kind = "error" || kind = "asr_error")
        return "kind"
    return "full"
}

; 哪些种类的情境身份是**可观测**的（#247「换情境可重新开口」的受益面）：
;   · video_invite —— 情境边界 = 场景正边沿（未 armed 时没有视频 id，见 PetSceneEdge）；
;   · new_session  —— 情境边界 = cap_session 真的换了（警醒：这才是视频身份键）。
; 只有「重新打个招呼」的这两类吃豁免；闲聊/里程碑等陪伴型发言照旧吃冷却，
; 免得「情境变了」变成一刀切放开（那会重新变吵）。
PetSpeechSituationKind(kind) {
    return kind = "video_invite" || kind = "new_session"
}

; 这一拍「情境是不是换了」（纯函数）：场景正边沿（换视频 / 重新播放）或换了视频
; 会话（cap_session 变且有效）。两边都没会话（视频没确定）不算 —— 没有身份就没有
; 「换了」这回事，宁可不说也不误判。
PetSpeechSituationChanged(prev, cur) {
    if (PetSceneEdge(cur, prev))
        return true
    session := ObjProp(cur, "session", 0)
    return (session > 0 && session != ObjProp(prev, "session", 0))
}

; 仲裁的闸门状态（接线侧从全局冷却位/每日计数装出来；测试可直接构造）。
; situationChanged = 这一拍情境变了（PetSpeechSituationChanged 的结论）。
PetSpeechGateState(now, lastAnyAt, lastKindAt, kindCount, kindDay, today, situationChanged := false) {
    return { now: now, lastAnyAt: lastAnyAt, lastKindAt: lastKindAt
           , kindCount: kindCount, kindDay: kindDay, today: today
           , situationChanged: situationChanged }
}

; 某个候选过不过闸门（策略见 PetSpeechGateMode；数值全在 PetChatterGate）。
PetSpeechGateOk(kind, gate) {
    mode := PetSpeechGateMode(kind)
    if (mode = "none")
        return true
    lastAnyAt := (mode = "full") ? ObjProp(gate, "lastAnyAt", 0) : 0
    lastKindAt := PetMapGet(ObjProp(gate, "lastKindAt", 0), kind, 0)
    ; #247：换了情境（换视频 / 场景重新出现）→ 「重新打招呼」的两类不吃旧冷却，
    ; 所以换视频时她真的会重新开口，而不是被上一个视频的冷却压住。
    ; 每日上限不参与豁免（防边沿抖动刷屏），是全天的跑飞保险。
    if (ObjProp(gate, "situationChanged", false) && PetSpeechSituationKind(kind)) {
        lastAnyAt := 0
        lastKindAt := 0
    }
    return PetChatterGate(ObjProp(gate, "now", 0), lastAnyAt, lastKindAt, kind
        , PetMapGet(ObjProp(gate, "kindCount", 0), kind, 0)
        , PetMapGet(ObjProp(gate, "kindDay", 0), kind, "")
        , ObjProp(gate, "today", ""))
}

; 发言仲裁（纯函数）：本拍所有候选 + 闸门状态 → 该说哪一条（"" = 都不说）。
;   · 勿扰 / 已有气泡 / 玻璃卡展开 → 全静默（spec：勿扰只压主动发言）；
;   · 其余按 PetSpeechPriority 取最高的一条过闸门候选（同优先级取数组靠前者）；
;   · 高优先级被闸门挡住时轮到下一条（沿用旧 chatter 决策的让位语义）。
; 不弹气泡、不记冷却、不写日志 —— 那些都在接线侧的唯一出口。
PetSpeechArbitrate(candidates, gate, quiet, bubbleBusy) {
    if (quiet || bubbleBusy)
        return ""
    best := ""
    bestRank := 0
    for cand in candidates {
        if (!IsObject(cand))
            continue
        kind := ObjProp(cand, "kind", "")
        if (kind = "")
            continue
        if (!PetSpeechGateOk(kind, gate))
            continue
        rank := PetSpeechPriority(kind)
        if (best = "" || rank < bestRank) {
            best := cand
            bestRank := rank
        }
    }
    return best
}

; 唯一出口的「怎么弹」决策（纯函数）：带按钮 → 走 PetBubbleShowEx，否则 PetBubbleShow。
; 返回 { ex, label, fn, action, onSaid }；cand 不是对象 → 0（调用方直接放弃）。
;
; **必须用 ObjProp（即 HasProp）读字段**：cand 是普通 Object 字面量，没有 Map 那套
; `Has(key)` 方法。2026-11-10 复核实测：`cand.Has("label")` 抛
; 「This value of type "Object" has no method named "Has"」，而日志在弹气泡**之前**
; 就记了「选中 X」——于是每次主动发言都抛异常、气泡永不出现，日志却显示链路通。
; 这段从接线层搬进纯函数，就是为了让 harness 用真对象跑一遍（片 61）。
PetSpeechPlan(cand) {
    if (!IsObject(cand))
        return 0
    label := ObjProp(cand, "label", "")
    fn := ObjProp(cand, "fn", 0)
    return { ex: (label != "" && IsObject(fn)), label: label, fn: fn
           , action: ObjProp(cand, "action", "")
           , onSaid: ObjProp(cand, "onSaid", 0) }
}

; 「这一拍为什么一条都没说」的人读原因（只喂日志，不参与判定）。
PetSpeechMissReason(candidates, quiet, bubbleBusy) {
    if (quiet)
        return "勿扰"
    if (bubbleBusy)
        return "气泡或玻璃卡占位"
    for cand in candidates {
        if (IsObject(cand) && ObjProp(cand, "kind", "") != "")
            return "闸门未到"
    }
    return "无候选"
}

; Decide 内部：挑文案 + 配动作，凑一条发言决策（文案空 = 这类没话说 → ""）。
PetChatterOut(kind, ctx, n, m, rnd) {
    pickCtx := {cfg: ObjProp(ctx, "cfg", ""), n: n, m: m}
    text := PetChatterPick(kind, pickCtx, rnd)
    if (text = "")
        return ""
    return { kind: kind, text: text, action: PetChatterAction(kind, rnd) }
}

; v36（#240）可观测性：发言出口的日志行（纯函数，可单测）。
; kind = 这一拍最终选中的发言种类（"" = 没选）；source = 来源/被拦原因。
; **只记动作与判定，不记正文**（§2.7 隐私纪律）；#244 的发言仲裁出口复用同一格式
; （选中 = 选了哪一条，未选 = 为什么都没选）。
PetSpeechLogText(kind, source := "") {
    if (kind = "")
        return "发言仲裁：未选（" . (source = "" ? "无候选" : source) . "）"
    return "发言仲裁：选中 " . kind . (source = "" ? "" : "（" . source . "）")
}

; v36（决策 12 收尾）：信息流写行的日志行（纯函数）。tag = 行类别（相位 /
; 来源 / 会话 / +1 句 / armed / 心跳断 / 持续状态 / 挑词）。**只记类别不记正文**
; ——「+1 句」行的正文是原句预览，同发言日志的隐私纪律（§2.7）。
PetFeedLogText(tag) {
    if (tag = "")
        return "信息流：写行（未分类）"
    return "信息流：写行（" . tag . "）"
}

; ---------------------------------------------------------------------------
;  v35 D5：随机 idle 插播的判定（A2 在 PetWindow.ahk 的 PetIdleAmbientTick 里
;  调 PetAmbientShouldPlay；随机步进的 nextAt 由调用方保存——用 PetAmbientNextAt
;  算。纯函数，warn/error 相位一律不插播）。
; ---------------------------------------------------------------------------

; face（当前状态脸键）与 lastAt（**下次允许插播的时刻**，0 = 从未安排）+
; now + phase → 是否允许插一次非冲突动作（stretch / selfie_fast）。
; 相位语气取 StatusVisual 的唯一真值：warn（要用户动手）/ error（故障）时不插。
; face 参数保留（未来若要按表情避冲突在此扩展），当前不参与判定。
PetAmbientShouldPlay(face, lastAt, now, phase) {
    tone := StatusVisual(phase).tone
    if (tone = "warn" || tone = "error")
        return false
    if (lastAt = 0)
        return true
    return now >= lastAt
}

; 下次允许插播的时刻：now + 随机 3–6 分钟（rnd 由调用方给，纯函数可单测）。
PetAmbientNextAt(now, rnd) {
    return now + PET_AMBIENT_MIN_MS + Mod(Abs(rnd), PET_AMBIENT_MAX_MS - PET_AMBIENT_MIN_MS + 1)
}

; 随机 idle 插播的动作键池（键名 = manifest motions 的词表，与 StatusVisual 的输出
; 同源；角色窗不许写死动作键——源码钉子钉着。rnd 决定性，可单测）。
PetAmbientPick(rnd) {
    keys := ["stretch", "selfie", "selfie_fast", "open_lid", "spray"]
    return keys[Mod(Abs(rnd), keys.Length) + 1]
}

; 闲时（tone=idle：unarmed / paused）表情不规律轮换（v35-ux 验收：别老是一张
; 打瞌睡脸）。lastAt=**下次允许换脸的时刻**（0=立即）；到点且相位语气为 idle 才
; 换，且换的 ≠ 当前脸；armed 等认真相位一律不轮换（那张脸是「干活中」的语义）。
; 返回新脸键或 ""（键名 = manifest expressions 词表，同源）。
PetIdleFacePick(curFace, lastAt, now, phase, rnd) {
    tone := StatusVisual(phase).tone
    if (tone != "idle")
        return ""
    if (lastAt != 0 && now < lastAt)
        return ""
    faces := ["focus", "question", "exclaim", "sweat", "heart_eyes", "drool"]
    idx := Mod(Abs(rnd), faces.Length)
    f := faces[idx + 1]
    if (f = curFace)
        f := faces[Mod(idx + 1, faces.Length) + 1]
    return f
}

; 下次允许换脸的时刻：now + 随机 25–70 秒（不规律感就是从这里来的）。
PetIdleFaceGapMs(now, rnd) {
    return now + 25000 + Mod(Abs(rnd), 45001)
}

; ---------------------------------------------------------------------------
;  v36：场景识别 —— 桌宠主动邀请收句 + 字幕带引导的纯逻辑
;
;  场景真值在 hub（scene_of → ping 的扁平字段 cap_scene / cap_video）；外壳只
;  消费。这里只留纯逻辑：
;    · PetSceneValues         外壳侧合法的 cap_scene 词表
;    · PetSceneInviteGate     场景邀请的边沿判定（cap_scene + cap_video）
;    · PetSceneInviteCandidate 边沿 →「待发位 + 本拍候选」（#244：不再自己弹）
;    · PetBandAskEdge         状态边沿 → 该不该弹「带字幕问询」气泡
;    · PetBandAskText         问询文案（全屏问 / 拖框问，键位一律 config 真值）
;    · PetPhaseAskKind        「需动手相位」提醒的种类（#244 同一条发言出口）
;
;  纪律：相位/场景字面量只在本文件（#236 钉子同款）；窗口侧（PetBubble）不写
;  任何场景/相位字符串。
; ---------------------------------------------------------------------------

; cap_scene 的唯一合法值（外壳侧词表）。hub 新增值时这里同步。
PetSceneValues() {
    return ["video_invite"]
}

; 场景邀请的**候选**（纯函数，票 #244）：把「正边沿」变成「待发位」。
;   · 正边沿（PetSceneInviteGate：cap_scene=video_invite 且 cap_video=1 且未
;     armed）→ 置待发位；
;   · 场景还在播、还没 armed → 待发位跨拍保持：这一拍被闸门/占位挡住，下一拍
;     接着排队（P0-C 修好后，边沿不会因为一次没抢到气泡位就永久熄火）；
;   · armed / 场景消失 / 视频停了 → 清待发位。
; 返回 {hold: 新待发位, cand: 本拍候选（"" = 这拍没得说）}。说过之后由接线侧
; 清掉待发位（PetSpeechMaybe 的返回值负责），所以这里不管「说没说过」。
PetSceneInviteCandidate(hold, cur, prev, cfg, rnd) {
    if (cur.armed || cur.scene != "video_invite" || cur.video != 1)
        return { hold: false, cand: "" }
    hold := hold || PetSceneInviteGate(cur, prev, false, false)
    if (!hold)
        return { hold: false, cand: "" }
    text := PetChatterPick("video_invite", { cfg: cfg, n: 0, m: 0 }, rnd)
    if (text = "")
        return { hold: true, cand: "" }
    return { hold: true
           , cand: { kind: "video_invite", text: text
                   , action: PetSceneInviteAction(rnd), source: "场景邀请" } }
}

; 「需动手相位」提醒的种类（纯函数，票 #244）：玻璃卡展开的四档里，redrag =
; 要用户拖框（dormant / redrag_needed），其余（no_heartbeat / failed）归故障
; 提醒。"" = 这一拍没有相位提醒（未展开 / 用户已经把卡「知道了」收掉）。
PetPhaseAskKind(visual) {
    if (!IsObject(visual) || !visual.expanded || visual.textKey = "")
        return ""
    return (visual.action = "redrag") ? "redrag" : "error"
}

; video_invite 的动作表情：热情一点（开盖打招呼）。
PetSceneInviteAction(rnd) {
    return (Mod(Abs(rnd), 2) = 0) ? "open_lid" : "spray"
}

; 场景邀请的**边沿判定**（纯函数，v36 票 #241）：这一拍该不该弹带按钮的邀请。
; cur / prev 是两拍的状态快照（接线侧组 {armed, scene, video}），quiet（勿扰）/
; bubbleBusy（气泡显示中 / 玻璃卡展开）由接线侧合入。
;
; 两个条件缺一不可：
;   · 中枢的 `cap_scene` = video_invite（**正**边沿：持续在看不再重复邀请）；
;   · 外壳自证的 `cap_video` = 1（B站视频页且在播，扁平 0/1）。
; 为什么两个都要：cap_scene 是「此刻该不该邀」的合成结论，cap_video 是**原始观测**
; —— 外壳读一眼就知道「她现在真的看见视频在播」，而不是只信一个场景名。字段缺失
; （旧中枢）时 cap_video 缺省 0，而旧中枢也不会给 cap_scene，所以两边同源、不误伤。
;
; v36 #247：边沿判定拆成 PetSceneEdge（这一段情境边界也是「情境变了」的唯一真值，
; 见 PetSpeechSituationChanged），这里只再加「勿扰 / 占位」两条守卫。
PetSceneInviteGate(cur, prev, quiet, bubbleBusy) {
    if (quiet || bubbleBusy)
        return false
    return PetSceneEdge(cur, prev)
}

; 场景正边沿（纯函数）：cap_scene 从「非 video_invite」跳到 video_invite，且外壳
; 自证视频在播、还没 armed。**这一段情境的边界**就是它 —— 未 armed 时 cap_session
; 还是 0，外壳拿不到视频身份键（#247：别硬造不存在的键），能观测到的「换了情境」
; 只有这个边沿。
PetSceneEdge(cur, prev) {
    if (cur.armed)
        return false
    if (cur.scene != "video_invite" || prev.scene = "video_invite")
        return false
    return (cur.video = 1)
}

; 「字幕带问询」的边沿判定（纯函数）。
; 场景 = 「armed 开着、这条没有英文 CC（sampling 兜底路径）、屏幕采样还没跑起来
; （ocr 正好是 dormant——无区域休眠）」的那一拍。三个状态都从 1Hz ping 的扁平
; 字段来；上一拍快照 prev* 用来判边沿（同状态只问一次，之后由重拖/引导接管）。
; armed 刚开（prevArmed=false）的**前 3 拍内不问**（armed-on 边沿的 toast/信息流
; 行已经说了「已开启」，同屏再弹问询会连环打扰）——用 armedHoldUntil 透传。
; v36 #246：`ccCovers` = 这一条会话的来源播报已经覆盖了「没有 CC」这句话（待说
; 或已说）→ 问询让位。两者的「没有 CC」是同一件事，只留先说出口的那一条
; （来源播报带原因分档，信息更多）；要用户动手的部分由「需动手相位」候选
; （redrag）继续提示，不靠这条问询。旧中枢没有 cap_cc 时 ccCovers=false，
; 问询照旧工作（向后兼容）。
PetBandAskEdge(armed, ocr, prevOcr, armedJustNow, quiet, bubbleBusy, ccCovers := false) {
    if (quiet || bubbleBusy || ccCovers)
        return false
    if (!armed)
        return false
    if (armedJustNow)
        return false
    return (ocr = "dormant" && prevOcr != "dormant")
}

; 「字幕带问询」的**持续条件**判定（纯函数，2026-11-10 新增）。
;
; 为什么还要这一条：`PetBandAskEdge` 只认「刚掉进 dormant」那一个**边沿**。真机上
; 最常见的路径是先开收句、后发现这条没有 CC —— 此时 ocr 一上来就是 dormant，
; 边沿在 armed 之前就用掉了，于是**一次都不问**（用户实测：她能说「没有 CC」，
; 却从不问要不要框选）。这里改成「已 armed + 一直是 dormant + 过了宽限期 + 这个
; 会话还没问过」→ 问一次；问出口后把 session 记进 askedSession（只问一次）。
;
; 参数：armed / ocr（= cap_phase）/ cc（= cap_cc 来源判定）
;       session（cap_session，0 = 视频身份未确定，不问）
;       askedSession = 已经问过的会话 id（0 = 没问过）
;       armedAtMs = 本次连续 armed 的起点（A_TickCount；0 = 未开）
;       now = 当前 tick；delayMs = armed 之后等多久才问（避开 armed-on 的 toast）
;
; 2026-11-10 第三次真机反馈（「起完服务先用默认框采了好几句，才想起来问我」）：
; 原来等 8 秒，正好等于「先采错几句再问」。**问得早才有意义** —— 用户越早框对，
; 后面漏的句越少。所以默认宽限收到 1.5 秒：只要 cc 判明「没有英文 CC」就立刻问。
; 1.5 秒仍然存在的理由：`cc` 要等查轨回来（实测 ~0.7s）才有值；太早问会问错
; （有 CC 的视频不该问框）。若那一拍正好被别的气泡占位，PetSpeechMaybe 会静默，
; 下一拍照样重试（onSaid 没记账 = 没问过）。
;
; 2026-11-10 第二次真机反馈：只在 `ocr = dormant` 时问还是不够 —— 他还留着上一条
; 视频拖的字幕带，相位是 `sampling`（区域存在、正在采），可换了个视频后那条区域
; 未必还压在字幕上，采出来是花的。用户要的是「每条没有 CC 的视频都问我一次」。
; 所以判据放宽到 dormant **或** sampling，前提是 cc 明确判成「没有英文 CC」
; （`no_english_track`）——有 CC 的视频根本不走屏幕采样，不该问。
PetBandOfferShould(armed, ocr, cc, session, askedSession, armedAtMs, now, delayMs := 1500, noSource := 0, regionKnown := 0) {
    if (!armed)
        return false
    ; 2026-11-10：已判「无画面字源」→ 这条压根没有字幕带可框，再问「你框一下」
    ; 就是把用户推进无解的死胡同（真机：弹了问询、框了也没字、空转 5 分钟）。
    ; 默认 0，既有 7/8 参调用与 harness 断言零影响。
    if (noSource)
        return false
    ; 2026-11-11：已经有字幕带矩形（自动复用或用户重拖入账）→ 不再问。
    ; 用户能从 1Hz ping 驱动的红框预览（1.6s）一眼看到系统在用哪个框；
    ; 框不对时按 ^!6 重拖。默认 0，旧调用零影响。
    if (regionKnown)
        return false
    if (cc != "no_english_track")
        return false
    if (ocr != "dormant" && ocr != "sampling")
        return false
    if (session <= 0 || askedSession = session)
        return false
    if (armedAtMs <= 0)
        return false
    return (now - armedAtMs) >= delayMs
}

; 「字幕带问询」文案：full=true 问要不要进全屏（看片模式），false 问拖框。
; 键位一律 config 真值；两行 ≤ 气泡一行的宽度人设（不啰嗦）。
; 2026-11-10：full 分支改成「暂停一下帮你框」（不再强推全屏——用户要的是拖框 +
; 框完自动续播，全屏是额外动作，正文里说明白）。
PetBandAskText(full, cfg) {
    if (full)
        return "这条没有 CC 字幕。我暂停一下，你框一下字幕带？（框完自动续播）"
    return "这条没有 CC 字幕，要记台词就拖一下字幕带（"
         . StatusKeyText(cfg, "hotkeyRedrag", "^!6") . "）"
}

; 问询的按钮文案（可交互气泡的唯一按钮）。
; 2026-11-10：按钮真的去**拖框**（暂停 → 遮罩 → 框完自动续播），不是开全屏。
PetBandAskButtonLabel() {
    return "好，我框一下"
}

; ---------------------------------------------------------------------------
;  v36 #246：来源播报 ——「这条有 CC / 只能屏幕采」（spec 决策 6、诊断 §4.1 / §5 F6）
;
;  真值链路：capture_scheduler._source_locked → status.cc → ping 扁平字段 `cap_cc`
;  （与相位并行、互不覆盖）。外壳只消费：
;    · PetCcValues        外壳侧认可的来源词表（与中枢枚举一致，不自造值）；
;    · PetCcKnown         来源是否已判明（off = 还没判明，不播报）；
;    · PetCcReportText    来源 → 一句人话（按原因分档；未知值有兜底档）；
;    · PetCcReportCandidate  armed + 会话确定 + 来源判明 → 候选（同一会话只说一次）；
;    · PetCcFeedLine      来源 → 信息流行（与发言同步出现，单点在 PetFeedProject）。
;
;  去重键用 `cap_session`：中枢每段（每个视频/切 P）建一次会话，id 变化即
;  「换视频」，同一会话不再说第二遍。去重放在候选层而非闸门 —— 这样
;  「换视频要能马上开口」不被全局 90s 压住（PetSpeechGateMode：cc_report 无闸门）。
; ---------------------------------------------------------------------------

; 中枢来源枚举（spec 决策 6；hub 的 CAPTURE_CC_VALUES 同一份真值）。
PetCcValues() {
    return ["ok", "no_english_track", "error", "off"]
}

; 来源是否已判明：off / 缺字段（旧中枢）= 还没判明；未知非空值也算判明（走兜底文案）。
PetCcKnown(cc) {
    return cc != "" && cc != "off"
}

; 来源 → 一句人话（按原因分档；不知道就直说不知道，不假装成功）。
; 文案只说「这条怎么记」，不下发动作指令 —— 要用户动手的话术归「需动手相位」
; 与字幕带问询，同一个动作不说两遍（#246 与 band_ask 的协调）。
PetCcReportText(cc) {
    if (cc = "ok")
        return "这条有英文 CC，我直接读字幕，不用你框"
    if (cc = "no_english_track")
        return "这条没有英文 CC，我改屏幕采样了"
    if (cc = "error")
        return "字幕接口报错，这条先用屏幕采样"
    if (cc = "off" || cc = "")
        return ""
    return "这条的来源我没认出来，先按屏幕采样盯着"
}

; 来源播报是否覆盖了这次字幕带问询（#246 与 band_ask 的唯一协调判据）：
; armed + 有会话 + 来源已判明（还没说 = 下一拍会说；已说过 = 记了会话 id）。
; 覆盖 = 问询让位。旧中枢没有 cap_cc（恒 off）→ 不覆盖，问询照旧工作（向后兼容）。
PetCcCoversBandAsk(armed, session, cc, reportedSession) {
    if (!armed || session <= 0)
        return false
    return (session = reportedSession) || PetCcKnown(cc)
}

; 来源播报的候选（纯函数）：armed + 有会话（场景/视频确定）+ 来源判明 +
; 这个会话还没说过。reportedSession = 已经播报过的会话 id（0 = 没说过）。
; 返回候选对象或 ""（= 这一拍没得说）。
PetCcReportCandidate(cur, reportedSession) {
    if (!ObjProp(cur, "armed", false))
        return ""
    session := ObjProp(cur, "session", 0)
    if (session <= 0)
        return ""
    cc := ObjProp(cur, "cc", "")
    if (!PetCcKnown(cc))
        return ""
    if (session = reportedSession)
        return ""
    text := PetCcReportText(cc)
    if (text = "")
        return ""
    return { kind: "cc_report", text: text, action: "", source: "来源播报" }
}

; ASR 终态候选（2026-11-11 P2）：done 带「去挑词」按钮；error 说清降级与原因。
; 只在状态边沿出现一次（调用方比较 prev 与 cur）；必须走唯一发言出口。
PetAsrReportCandidate(asr, prevAsr, added := 0, reason := "") {
    if (asr = prevAsr)
        return ""
    if (asr = "done")
        return { kind: "asr_done"
               , text: "音频转写完成了，共 " . added . " 句。要现在挑词吗？"
               , action: "", source: "ASR 转写"
               , label: "去挑词" }
    if (asr = "error") {
        why := PetAsrReasonText(reason)
        return { kind: "asr_error"
               , text: "音频转写失败了" . (why = "" ? "" : "（" . why . "）")
                     . "，这条先按屏幕采样盯着。"
               , action: "", source: "ASR 转写" }
    }
    return ""
}

; 转写失败原因码 → 人话（空/未知原样回传，不吞信息）。
PetAsrReasonText(reason) {
    if (reason = "auth")
        return "API key 无效"
    if (reason = "rate_limit")
        return "接口限流"
    if (reason = "too_big")
        return "音频过大"
    if (reason = "empty")
        return "没转出文本"
    if (reason = "store")
        return "落库失败"
    if (reason = "network")
        return "网络失败"
    return reason
}

; ---------------------------------------------------------------------------
;  v35 D1：信息流面板（PetFeed，A2 实现窗口）的**行文案**纯逻辑。
;  接线在 英语取词.ahk 的 CaptureStatusApply（边沿触发喂 PetFeedLine）；
;  这里只管「这一拍该写什么字、什么语气」。相位字面量只出现在本文件。
;  v36 #245：边沿判定与文案一起收成 PetFeedProject(prev, cur) 纯函数，
;  接线侧只喂快照、不再自己判边沿（散落 if 全部搬到这里）。
; ---------------------------------------------------------------------------

global PET_FEED_STATUS_EVERY := 60   ; 持续状态行：同状态每 60 拍（≈1 分钟）补一行

; 相位变化那一拍的信息流行：{text, tone} 或 ""（不在表里的相位不写行）。
; v36 #245：11 档全部有文案 —— 过去 4 个静默档（unarmed / cc_reading /
; cc_missing / cc_error）返回空串，于是「未开收句」这个常态下一行都不产生。
PetFeedPhaseLine(phase, cfg := "") {
    if (phase = "sampling")
        return { text: "▶ 采集中（屏幕采样）", tone: "ok" }
    if (phase = "cc_ok")
        return { text: "▶ 采集中（CC 直读）", tone: "ok" }
    if (phase = "paused")
        return { text: "⏸ 已暂停（回来自动续）", tone: "idle" }
    if (phase = "dormant")
        return { text: "⚠ 需要拖字幕带", tone: "warn" }
    if (phase = "redrag_needed")
        return { text: "⚠ 字幕位置变了", tone: "warn" }
    if (phase = "failed")
        return { text: "✖ 识别引擎不可用", tone: "error" }
    if (phase = "no_heartbeat")
        return { text: "❓ 页面切走了", tone: "warn" }
    ; --- v36 #245：4 个静默档补文案（不再是空串）---
    ; 2026-11-10 竖排改版：面板收到 ~196lp，这行去掉「按…开始」，控制在 1 行内
    ; （窄条里折行会把面板撑高，反而更不紧凑）。
    if (phase = "unarmed")
        return { text: "💤 待命中 · 未开收句（"
                     . StatusKeyText(cfg, "hotkeyCaptureArm", "^!7") . "）", tone: "idle" }
    if (phase = "cc_reading")
        return { text: "🔎 正在读 CC 字幕…", tone: "idle" }
    if (phase = "cc_missing")
        return { text: "⚠ 这条没有英文 CC（改屏幕采样）", tone: "warn" }
    if (phase = "cc_error")
        return { text: "⚠ 字幕接口报错（稍后重试）", tone: "warn" }
    return ""
}

; 持续状态行（v36 #245）：**任何一拍**都能说清「现在是什么状态」。
; 与相位边的区别：相位边是「变了才写」，这一条是低频心跳（同状态每
; PET_FEED_STATUS_EVERY 拍补一行），让面板不是只有事件那一拍才有字。
; 未 armed 一律按「待命中 · 未开收句」说（旧中枢没有 cap_phase 字段也照样有字）；
; armed 时优先复用相位文案，未知相位给一句「收句中 · 等下一句」兜底。
PetFeedStatusLine(armed, phase, cfg := "") {
    if (!armed)
        return PetFeedPhaseLine("unarmed", cfg)
    l := PetFeedPhaseLine(phase, cfg)
    if (l != "")
        return l
    return { text: "▶ 收句中 · 等下一句", tone: "idle" }
}

; 来源行（v36 #246）：`cap_cc` 判明那一拍写一行，与主动播报同档说法。
; 未知值给兜底行（面板不留空白）；off / 缺字段不写（还没判明，不制造噪音）。
PetCcFeedLine(cc) {
    if (cc = "ok")
        return { text: "▶ 来源：英文 CC 直读", tone: "ok" }
    if (cc = "no_english_track")
        return { text: "▶ 来源：无英文轨 → 屏幕采样（无字源时自动转写）", tone: "warn" }
    if (cc = "error")
        return { text: "✖ 来源：字幕接口报错 → 屏幕采样", tone: "warn" }
    if (cc = "off" || cc = "")
        return ""
    return { text: "❓ 来源：未知（先按屏幕采样）", tone: "warn" }
}

; ASR 转写状态行（2026-11-11, P2）：queued / done / error 各一行；其余不写。
; error 带原因码的人话，用户能区分「key 错了 / 限流 / 没转出文本」。
PetAsrFeedLine(asr, added := 0, reason := "") {
    if (asr = "queued")
        return { text: "🎧 ASR 转写排队中…", tone: "idle" }
    if (asr = "done")
        return { text: "🎧 ASR 转写完成（" . added . " 句）", tone: "ok" }
    if (asr = "error") {
        why := PetAsrReasonText(reason)
        return { text: "🎧 ASR 转写失败" . (why = "" ? "" : "（" . why . "）")
                       . "，先按屏幕采样", tone: "warn" }
    }
    return ""
}

; 事件投影纯函数（v36 #245 的核心）：(上一拍快照, 本拍快照) → [{text, tone}, …]。
; 快照字段（都缺省安全）：
;   armed / phase / session / added / last / bad = 1Hz ping 的扁平字段派生
;   cc = cap_cc 来源枚举（#246；缺字段 = "" 不写来源行）
;   idleTicks = 「距上一条信息流行过了几拍」（接线侧维护；持续状态行据此去噪）
; cfg 只用来取键位真值（可选，缺省回落代码缺省）。
; 顺序固定 = 相位 → 来源 → 会话 → +1 句 → armed → 心跳断；同拍多事件按这个顺序出行。
; 持续状态行只在「这一拍一行都没有」且空闲计数到点时补一行 —— 同状态不重复刷屏，
; 但面板永远不会停在「最后一行是几分钟前的启动文案」。
PetFeedProject(prev, cur, cfg := "") {
    out := []
    ; 1. 相位边沿（11 档全覆盖，静默档也有文案）
    ph := ObjProp(cur, "phase", "")
    if (ph != "" && ph != ObjProp(prev, "phase", "")) {
        l := PetFeedPhaseLine(ph, cfg)
        if (l != "")
            out.Push({ text: l.text, tone: l.tone, tag: "phase" })
    }
    ; 2. 来源判明（#246）：来源变化，或换会话（换视频）后重新声明一次 ——
    ;    同一会话同一来源不重复刷屏（与发言的去重同口径）。
    sess := ObjProp(cur, "session", 0)
    prevSess := ObjProp(prev, "session", 0)
    ccNow := ObjProp(cur, "cc", "")
    if (ccNow != "" && (ccNow != ObjProp(prev, "cc", "")
        || (sess > 0 && sess != prevSess))) {
        l := PetCcFeedLine(ccNow)
        if (l != "")
            out.Push({ text: l.text, tone: l.tone, tag: "cc" })
    }
    ; 2b. ASR 状态边沿（2026-11-11 P2）：queued / done / error 各写一行
    asrNow := ObjProp(cur, "asr", "")
    if (asrNow != ObjProp(prev, "asr", "")) {
        l := PetAsrFeedLine(asrNow, ObjProp(cur, "asrAdded", 0), ObjProp(cur, "asrReason", ""))
        if (l != "")
            out.Push({ text: l.text, tone: l.tone, tag: "asr" })
    }
    ; 3. 会话开始 / 结束
    if (sess != prevSess) {
        l := PetFeedSessionLine(sess, prevSess, ObjProp(prev, "added", 0))
        if (l != "")
            out.Push({ text: l.text, tone: l.tone, tag: "session" })
    }
    ; 4. +1 句（预览最近一句，截 40 字符）
    if (ObjProp(cur, "added", 0) > ObjProp(prev, "added", 0))
        out.Push({ text: PetFeedAddedLine(ObjProp(cur, "last", "")), tone: "ok", tag: "added" })
    ; 5. armed 边沿（键位取 config 真值）
    armedNow := ObjProp(cur, "armed", false) ? true : false
    if (armedNow != (ObjProp(prev, "armed", false) ? true : false)) {
        l := PetFeedArmLine(armedNow, cfg)
        out.Push({ text: l.text, tone: l.tone, tag: "armed" })
    }
    ; 6. 心跳断边沿（只写「刚断」那一拍）
    if (ObjProp(cur, "bad", false) && !ObjProp(prev, "bad", false)) {
        l := PetFeedHeartbeatLine(true, false)
        out.Push({ text: l.text, tone: l.tone, tag: "heartbeat" })
    }
    ; 7. 持续状态行：本拍没有事件 + 空闲到点（60 拍 ≈ 1 分钟）才补一行
    idle := ObjProp(cur, "idleTicks", 0)
    if (out.Length = 0 && idle > 0 && Mod(idle, PET_FEED_STATUS_EVERY) = 0) {
        l := PetFeedStatusLine(armedNow, ph, cfg)
        out.Push({ text: l.text, tone: l.tone, tag: "status" })
    }
    return out
}

; 显隐位写入口径（v36 #245）：一个动作 → 一组位，两个位怎么组合只在这里决定。
;   hidden = 用户关闭（面板 × / 菜单项）→ userHidden=true 且 pinned=false（互斥）
;   shown  = 用户固定显示              → userHidden=false 且 pinned=true
;   auto   = 开收句自动恢复            → 只解 userHidden，pinned 原样
;   load   = 读盘归一（含历史脏状态）  → 原样，但清掉「隐藏 + 固定」并存
; 过去 × 热区只写 userHidden、不清 pinned，于是落盘出现 hidden=true 与 pinned=true
; 的矛盾组合，pinned 被污染成死位。现在两个写入点共用这一份真值。
PetFeedVisFlags(action, userHidden, pinned) {
    uh := userHidden ? true : false
    pn := pinned ? true : false
    if (action = "hidden")
        return { userHidden: true, pinned: false }
    if (action = "shown")
        return { userHidden: false, pinned: true }
    if (action = "auto")
        return { userHidden: false, pinned: pn }
    ; load / 未知动作：清矛盾态（隐藏优先，等下次开收句自动恢复）
    if (uh && pn)
        pn := false
    return { userHidden: uh, pinned: pn }
}


; 最近一句的人读预览：截 40 字符，截断处加省略号（design D1 的「+1 句」行）。
PetFeedClip40(s) {
    t := Trim(String(s))
    if (StrLen(t) <= 40)
        return t
    return SubStr(t, 1, 40) . "…"
}

; cap_added 增加那一拍：`+1 句 「…」`（ok）。
PetFeedAddedLine(last) {
    return "+1 句 「" . PetFeedClip40(last) . "」"
}

; 会话边沿：开始 `📺 会话 #N 开始记录`（ok）/ 结束 `会话结束，共 N 句`（idle，
; N = 结束前那拍的句数）。其余（0→0 等异常态）不出行。
PetFeedSessionLine(session, prevSession, added) {
    if (session > 0 && session != prevSession)
        return { text: "📺 会话 #" . session . " 开始记录", tone: "ok" }
    if (session = 0 && prevSession > 0)
        return { text: "会话结束，共 " . added . " 句", tone: "idle" }
    return ""
}

; armed 边沿：开启带键位真值（design D1 的「^!7 随时关」口径）/ 关闭一行灰。
PetFeedArmLine(armed, cfg) {
    if (armed)
        return { text: "🔴 收句已开启（" . StatusKeyText(cfg, "hotkeyCaptureArm", "^!7") . " 随时关）", tone: "ok" }
    return { text: "⚫ 收句已关闭", tone: "idle" }
}

; 心跳断边沿（接线侧把「ageMs 缺失/超时」归一成 bad 布尔再喂进来）。
PetFeedHeartbeatLine(badNow, badPrev) {
    if (badNow && !badPrev)
        return { text: "⚠ 心跳断了（页面不在前台）", tone: "warn" }
    return ""
}

; 挑词保存成功那一拍：`💖 挑了 N 个词 · 本段 M 句`（ok）。M = 本段句数快照。
PetFeedPickLine(hits, added) {
    return "💖 挑了 " . hits . " 个词 · 本段 " . added . " 句"
}

; ---------------------------------------------------------------------------
;  v36：拖动的纯逻辑 —— 按下快照 + 累计位移（幂等）+ 虚拟屏夹取 + 统一拖动谓词
;
;  拖动期位置的唯一真值是**按下那一刻的快照** + 自按下起的累计位移；绝不允许
;  「读上一次移动后的当前位置再叠加」（那会 n 倍累加，几帧就飞出屏幕）。
;  位置计算是纯函数：同一个鼠标位置重复送达多少次，结果都一样（幂等）。
; ---------------------------------------------------------------------------

; 拖动落点：快照 + 累计位移 → 夹进虚拟屏（物理像素；vr 与 VirtualScreenRect 同口径）。
; 夹取对多屏 / 负坐标一视同仁 —— 左屏的 x 是负的，照样是「屏幕内」，不能当屏幕外重置。
; 角色比虚拟屏还宽时钉在左上（不产生越界的负值）。
PetDragPlace(snapX, snapY, dx, dy, w, h, vr) {
    x := Max(vr.x, Min(snapX + dx, vr.x + vr.w - w))
    y := Max(vr.y, Min(snapY + dy, vr.y + vr.h - h))
    return { x: x, y: y }
}

; 拖动谓词（唯一真值）：角色拖动 / 信息流面板拖动，任一为真都算「拖动中」。
; 拖动期的跟随重排一律跳过（位置由 move-only 维护），菜单也不判「点了外面」。
PetDragActive(petDragging, panelDragging := false) {
    return (petDragging = true) || (panelDragging = true)
}

; 拖动期一个跟随浮层的落点：**只读按下快照** + 累计位移（幂等）。
; snap 是按下那一刻的快照（key → {x,y}；没有快照的记 0）。shown=false 的层不搬
; （位置由各自的 PlaceAt 决定）；拖动中才弹出来的层就地重锚（锚 = 当前位置 −
; 已累计位移）：这一下不跳，之后继续跟手。返回落点 {x,y}，不搬返回 0。
PetDragLayerPos(snap, key, shown, curX, curY, dx, dy) {
    if (!shown)
        return 0
    p := snap[key]
    if (!IsObject(p)) {
        p := { x: curX - dx, y: curY - dy }
        snap[key] := p
    }
    return { x: p.x + dx, y: p.y + dy }
}


; ---------------------------------------------------------------------------
;  v36 票 #243：点击立刻有反应（spec 决策 8/9）—— 菜单**先渲染后补数据**与
;  绘制缓存里能脱机断言的那部分。窗口/缓存接线在 lib/PetMenu.ahk、
;  lib/PanelPaint.ahk、lib/PetNet.ahk；断言在 tests/test_shell_logic.ahk 片 56。
; ---------------------------------------------------------------------------

; 位图 + 逐像素 alpha 结果的缓存键：(宽, 高, hover 行, disabled 位)。
; 四项任一变化都必须换一个新键 —— 否则会把上一帧的位图贴到新状态上。
; hover 用 0 表示「没有 hover 行」（-1 与 0 同键；AHK 里 -1 是「不在任何项上」）。
PetMenuPaintKey(w, h, hover, disabledMask) {
    hv := (hover >= 1) ? hover : 0
    return w . "x" . h . "|h" . hv . "|d" . disabledMask
}

; 菜单项的「禁用位图」：第 i 项（1 起）对应第 i-1 位。
; 它是缓存键的一位输入 —— 挑词项在「无待选」与「有待选」之间切换就是新画面。
PetMenuDisabledMask(flags) {
    mask := 0
    i := 0
    for d in flags {
        i += 1
        if (d)
            mask |= 1 << (i - 1)
    }
    return mask
}

; 异步回包的代次号是否还是当前这一代：不是就丢弃（菜单已关 / 已重开 / 换了面板）。
; 代次号 0 表示「没有在等回包」，一律当过期。
PetMenuNetFresh(gen, curGen) {
    return gen >= 1 && gen = curGen
}

; 点击链路网络超时上限（ms）：异步化之后仍然钉住「不会阻塞数秒」。
; 调用方给多大都夹到 300；给 0/负数按上限走。改这里要同步改票 #243 的钉子。
PetNetTimeoutMs(ms := 0) {
    maxMs := 300
    if (ms <= 0 || ms > maxMs)
        return maxMs
    return ms
}

; `/api/sessions/<id>/backlog/count` 回包 → 待挑条数。
; 拿不到 / 端点异常 / 负值一律按 0：菜单项禁用并显示原因，不空白、不崩。
PetBacklogCount(body) {
    if (!RegExMatch(body, '"pending"\s*:\s*(-?\d+)', &m))
        return 0
    return Max(0, m[1] + 0)
}
