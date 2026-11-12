; ============================================================================
;  lib/LongformPanel.ahk —— v21「长文取词」T5（GitHub #160）：长文面板窗口
;
;  本文件是**独立新窗口**的全部实现，通过 `#Include "lib\LongformPanel.ahk"` 挂进
;  英语取词.ahk。主脚本只加：一个 #Include、ReadConfig 里的 4 个几何键 + 2 个热键、
;  HotkeyEntries 的两条、启动处一个 hook（LongformStartup / demo 分流）。
;  **旧速记框（BuildBox/LayoutBox/BoxKeys）一行不改** —— 长文面板有自己的窗口、
;  自己的键、自己的主题，回滚只需不注册两条新热键（规格 §0 的不变量）。
;
;  本阶段（T5）范围：窗口 + 七种视觉状态 + 高亮机制 + 位置持久化 + 跨屏 DPI 路径。
;  **不接内核/中枢真实数据链路**（T2/T4 的结果由主控在下一阶段接进来）；数据用
;  内置示例文章，`--lf-demo=<state>` 逐状态渲染（demo 不注册任何热键，避免抢键盘）。
;
;  视觉依据（都已实测，不是推测）：
;  · 视觉定稿 docs/research/2026-10-27-longform-panel-visual.md +
;    docs/research/longform-panel/panel-v2.html（七种状态的设计稿）
;  · 规格 docs/specs/v21-longform-capture.md §9.1（视觉规范）/ §9.2（AHK 可行性）
;  · RichEdit 全部坑 tools/英语取词/docs/research/2026-10-26-long-article-ahk-ui.md
;
;  以下几条是本机**实测**得到的硬约束（写在这里，避免以后被"优化"回去）：
;  1. 只读只能用**创建样式** ES_READONLY(0x800)：`EM_SETREADONLY`/`EM_SETOPTIONS`
;     拦不住直投的 WM_CHAR（研究 §5）。
;  2. `CHARFORMAT2W` 的 `crBackColor` 在本机 msftedit 上在偏移 **96**，不是 SDK 的 94；
;     `crTextColor` 在 20 是对的。这里做**自校准**（写入 + 回读校验，两候选 94/96），
;     并额外用像素级自检（demo 里截图数高亮底色像素）证明它真的画出来了。
;  3. 圆角只能用 DWM（DWMWA_WINDOW_CORNER_PREFERENCE=33 / DWMWCP_ROUND=2）：
;     无边框 + ToolWindow + AlwaysOnTop + Resize 上实测生效（本文件开头注释处的
;     自检会回读该属性值 = 2）；**不用 SetWindowRgn**（硬锯齿）、**不做子窗口**
;     （+Parent 上 DWM 圆角失效）。
;  4. 跨显示器 DPI：一律 `GetDpiForWindow(panel.Hwnd)/96`，**不用主脚本的全局 D**
;     （A_ScreenDPI 只反映主屏；面板会被拖到任意屏），并处理 WM_DPICHANGED。
;
;  一个必须写清的设计决定 —— **几何键与 config 热重载的关系**：
;  config.ini 每 2s 由 WatchConfig/ReloadConfig 热重载，而 SaveConfigKeys 是整文件
;  重写。若把 longform_x/y/w/h 也纳入热重载的 apply，用户刚拖完的位置会在 2 秒内被
;  旧值摆回去（研究 §7.2 点名的真实风险）。因此：
;    · 几何在启动时**读一次**（LongformStartup → LfGeomFromConfig），存进 G_LfGeom；
;    · ReloadConfig 只重读 G_Cfg，**从不**把 longform_* 应用到已打开的面板；
;    · 落盘只发生在 WM_EXITSIZEMOVE（拖动/缩放结束那一刻，一次），不在 WM_SIZE 里
;      （拖动中每像素写一次文件会卡）。
;  这三条在 tests/test_shell_source.py 里有静态断言钉住。
; ============================================================================

; --- 面板模块的运行时状态 ----------------------------------------------------
; 命名一律 Lf*/Longform* 前缀：AHK v2 标识符大小写不敏感，`P`/`D`/`Gui` 这类短名
; 在旧框里已踩过坑（局部变量 p 遮蔽全局函数 P()，研究 §12）。
global G_LfGui := 0                 ; 面板 Gui（0 = 不存在）
global G_LfC := {}                  ; 控件引用
global G_LfDpi := 96                ; GetDpiForWindow(面板)（不是 A_ScreenDPI）
global G_LfScale := 1.0             ; G_LfDpi / 96（面板窗口自己的缩放）
global G_LfGeom := { x: "", y: "", w: "", h: "" }  ; config 里的几何（逻辑 px，启动读一次）
global G_LfGeomLoaded := false
global G_LfState := ""              ; 当前状态名（hit/empty/...）
global G_LfData := 0                ; 当前状态的数据对象（LfDemoData 的产物）
global G_LfPal := 0                 ; 当前调色板（浅/深）
global G_LfDemo := ""               ; 非空 = `--lf-demo=<state>`（demo 模式）
global G_LfOut := []                ; 自检输出行（demo 写结果文件）
global G_LfResultPath := ""         ; demo 的结果文件路径（OnError 兜底写它）
global G_LfBackOff := 0             ; crBackColor 校准出的偏移（94/96；0 = 还没校准）
global G_LfRichLoaded := false      ; Msftedit.dll 是否已加载（建 RichEdit 前必须先加载）
global G_LfFonts := Map()           ; HFONT 缓存（key → HFONT）
global G_LfFontSpecs := Map()       ; hwnd → {px, weight, face}（DPI 变化时重建字体用）
global G_LfBrushes := Map()         ; HBRUSH 缓存（hex → HBRUSH）
global G_LfCtlStyles := Map()       ; 控件 hwnd → {fg, bg}（WM_CTLCOLOR 上色）
global G_LfBtnKind := Map()         ; 自绘按钮 hwnd → "primary"|"plain"|"soft"|"close"
global G_LfCaretOn := true          ; 空态强调色光标的闪烁相位
global G_LfBlinkN := 0              ; 闪烁次数（demo 自检用它证明定时器真的在跑）
global G_LfNaturalH := 0            ; 当前状态的自然高度（物理 px）
global G_LfDrag := { active: false, x: 0, y: 0 }   ; 拖动起点（WM_ENTERSIZEMOVE 记）
global G_LfKeysOn := false          ; 面板内键（Esc）是否已挂
global G_LfClosing := false
; 目标词行的行几何（LfLayout 每次排版时记下：x0 / y / innerW / wordH / kindH）。
; 为什么缓存：chip 的位置依赖**每键变化**的文本宽，Change 路径要用同一份行几何做
; 轻量重摆（LfPlaceKindChip），不能为此再算一套布局。
global G_LfWordRow := 0
; 底栏提示行（footHint）的几何与可用宽度（同上：LfLayout 记一份，Change 路径的
; 「第 N 处 / 共 M 处」提示只改这一格，绝不为它重排整窗）—— 见 LfUpdateChainHint。
global G_LfFootRow := 0
; 当前挂在底栏上的顺句次数标签（"" = 没挂）。它同时是「值没变就别碰控件」的判据：
; 每键都 Move 一次控件会闪，也让无关键击白跑一遍测量。
global G_LfChainHint := ""
; v21 round2 (B)：本篇文章的来源标题（session 标题同源；空 = 拿不到来源）——
; 顶栏「来源短标题」用它，article_create 回包时写入，重绘/重建面板都从这里取。
global G_LfSourceTitle := ""
; 长文在途标志（**独立于旧框的 G_Pending**：RunShot 的守卫只认 G_Pending，长文的
; OCR/定位绝对不能借用它去挡旧框的截图链路）。阶段 2 接真实数据链路时用它。
global G_LongPending := false

; 设计尺寸（逻辑 px，来自规格 §9.1 / 视觉稿 §5；**不是随意值**）。位置/尺寸一律
; 经 LfPx() 按窗口 DPI 转物理像素；字体按设计 px × 缩放建 HFONT（见 LfFont）。
global LF := {
    PANEL_W:        428,   ; 面板宽（内容 386 + 左右 20）
    PAD:             20,
    HEAD_PAD_TOP:    14,
    HEAD_ROW:        20,   ; 顶栏 34 = 14 + 20
    BODY_PAD_TOP:    16,
    ; 目标词行高 42（用户验收 Bug 2b）：旧值 34 装不下 22–24px 字体的 g/y/j/p/q
    ; 下伸部，真机截图里下半身被裁。字号同时从 28/600 降到 24/500（用户要求小一点、
    ; 细一点，长短语能显示更多字符）。
    WORD_H:          42,
    SUB_GAP:          7,
    SUB_H:           20,
    ; 列间距（原音标列；音标按用户要求撤掉后，仍用于全文目录的段号↔标题间距）
    PHON_GAP:        11,
    PAPER_GAP:       15,
    PAPER_PAD_X:     18,
    PAPER_PAD_TOP:   16,
    PAPER_PAD_BOT:   17,
    CLIP_H:         118,   ; 全文展开时纸面最大高（含内边距，视觉稿 §5）
    NOTE_GAP:        11,
    NOTE_H:          17,
    RULE_GAP:        14,
    ACT_PAD_TOP:     14,
    ACT_PAD_BOT:     14,
    BTN_H:           29,
    BTN_RADIUS:       8,
    OUTLINE_GAP:     13,
    OUTLINE_PAD:     11,
    OUTLINE_HEAD_H:  17,
    OUTLINE_ROW_H:   20,
    ; v22 P2b（§9「顺手修」）：4 → 8。高度不够时 LfToggleOutline 会先长窗到自然高度
    ; （上限 = 主屏工作区，MonitorGetPrimary），仍装不下才少显示几行（Fit 逻辑在 LfLayout 里）。
    OUTLINE_ROWS:     8,
    MIN_W:          360,
    MIN_H:          240,
    SHOT_MARGIN:     24    ; 截图时在窗口外多抓一圈（看得到圆角与投影）
}

; 七种状态（视觉稿 §3）：demo 与真实渲染共用同一套数据形状。
global LF_STATES := ["hit", "empty", "multi", "outline", "low", "nomatch", "dark"]

; ============================================================================
;  小工具：颜色 / 缩放 / 字体 / GDI
; ============================================================================

; "RRGGBB" → 0x00BBGGRR（COLORREF）。**一律这个字节序**，写错就是颜色静默不生效。
LfRef(hex) {
    v := Integer("0x" . hex)
    return ((v & 0xFF) << 16) | (v & 0xFF00) | ((v >> 16) & 0xFF)
}

; 颜色混合：把 fg 按 alpha（0..1）压到 bg 上得到**实色**。
; 为什么必须实色：AHK 的 RichEdit 高亮/CSS 的 rgba 不同，CHARFORMAT2W 只吃 COLORREF，
; 没有透明度。规格 §9.1 要的「荧光笔」= 设计色按透明度混到纸面底色上（当前词 0.42 /
; 已存词 0.17；深色 0.26 / 0.10），混出来的就是这里的结果。
LfMix(fg, bg, alpha) {
    f := Integer("0x" . fg), b := Integer("0x" . bg)
    fr := (f >> 16) & 0xFF, fg2 := (f >> 8) & 0xFF, fb := f & 0xFF
    br := (b >> 16) & 0xFF, bg2 := (b >> 8) & 0xFF, bb := b & 0xFF
    r := Round(fr * alpha + br * (1 - alpha))
    g := Round(fg2 * alpha + bg2 * (1 - alpha))
    bl := Round(fb * alpha + bb * (1 - alpha))
    return Format("{:02X}{:02X}{:02X}", r, g, bl)
}

; 逻辑 px（96dpi 的设计值）→ 本窗口的物理像素。
; **只用 G_LfScale（GetDpiForWindow/96）**，绝不用主脚本的全局 D —— 面板在副屏时
; `A_ScreenDPI` 只反映主屏，按它算尺寸会整体错 50%（研究 §6 的架构级发现）。
LfPx(n) {
    global G_LfScale
    return Round(n * G_LfScale)
}

; 浅色 / 深色调色板（规格 §9.1 的色彩表 + 视觉稿 §4）。深色**不是浅色的反色**：
; 底色偏冷 #17191C，纸面 #1E2126 比底色亮（纸要浮起来），高亮透明度 0.26/0.10。
LfPalette(dark) {
    if (dark) {
        p := { panel: "17191C", paper: "1E2126", ink: "EDEFF2", ink2: "B4BAC3"
             , ink3: "868D97", ink4: "5F666F", accent: "8E92F5", warn: "E0A44A"
             , warnPip: "E0A44A", green: "3F9F72", close: "868D97"
             , btnText: "FFFFFF", kbdOnBtn: "" , hintRight: "5F666F" }
        p.line := LfMix("FFFFFF", p.panel, 0.075)          ; 分隔线（rgba(255,255,255,.075)）
        p.paperLine := LfMix("FFFFFF", p.paper, 0.055)     ; 纸面内描边
        p.accentSoft := LfMix(p.accent, p.panel, 0.13)
        p.softBtn := LfMix("FFFFFF", p.panel, 0.065)
        p.markCur := LfMix("F4BE44", p.paper, 0.26)        ; 当前词荧光笔
        p.markSoft := LfMix("F4BE44", p.paper, 0.10)       ; 已存词淡笔触
        p.markCurText := "FFF6DF"                          ; 深色高亮字改暖白，避免刺眼
        p.warnEdge := LfMix("D97706", p.paper, 0.34)
        p.sentText := "E2E5E9"                             ; 原句正文（视觉稿 .panel.dark .sent）
        p.meaningHint := "5F666F"
        p.kbdOnBtn := LfMix("FFFFFF", p.accent, 0.55)
        p.kbdPlain := LfMix(p.ink3, p.panel, 0.55)
        p.paperHint := "6D747E"
        p.outlineText := "868D97"
    } else {
        p := { panel: "FFFFFF", paper: "FBFAF7", ink: "14161A", ink2: "4A5058"
             , ink3: "767D87", ink4: "A3A9B2", accent: "4F53C8", warn: "A2620A"
             , warnPip: "D97706", green: "3F9F72", close: "A3A9B2"
             , btnText: "FFFFFF", kbdOnBtn: "", hintRight: "A3A9B2" }
        p.line := LfMix("101216", p.panel, 0.075)
        p.paperLine := LfMix("101216", p.paper, 0.045)
        p.accentSoft := LfMix(p.accent, p.panel, 0.085)
        p.softBtn := LfMix("101216", p.panel, 0.045)
        p.markCur := LfMix("F4BE44", p.paper, 0.42)
        p.markSoft := LfMix("F4BE44", p.paper, 0.17)
        p.markCurText := "14161A"                          ; 浅色高亮字 = 正文色（视觉稿 color:inherit）
        p.warnEdge := LfMix("D97706", p.paper, 0.34)
        p.sentText := "14161A"
        p.meaningHint := "A3A9B2"
        p.kbdOnBtn := LfMix("FFFFFF", p.accent, 0.55)
        p.kbdPlain := LfMix(p.ink3, p.panel, 0.55)
        p.paperHint := "8B9099"
        p.outlineText := "767D87"
    }
    return p
}

; Msftedit.dll 必须在**任何 CreateWindowEx("RICHEDIT50W") 之前**加载，否则类不存在
; （研究 §1）。幂等；放函数里而不是 #Include 顶层，避免影响正常启动路径的时序。
LfEnsureRich() {
    global G_LfRichLoaded
    if (G_LfRichLoaded)
        return
    DllCall("LoadLibraryW", "Str", "msftedit.dll", "Ptr")
    G_LfRichLoaded := true
}

LfFontKey(px, weight, face) => px . "|" . weight . "|" . face

; 建/取 HFONT。高度用**物理像素** = 设计 px × 窗口缩放：AHK 的 Gui.SetFont 是
; point 口径、对 -DPIScale 的窗口换算不透明，所以这里直接 CreateFontW 精确控制
; （研究 §1 实测：RichEdit 也必须显式 WM_SETFONT 才不会用系统默认字号）。
LfFont(px, weight, face) {
    global G_LfFonts, G_LfScale
    key := LfFontKey(px, weight, face)
    if (G_LfFonts.Has(key))
        return G_LfFonts[key]
    h := -Round(px * G_LfScale)
    if (h > -1)
        h := -1
    hf := DllCall("gdi32\CreateFontW", "Int", h, "Int", 0, "Int", 0, "Int", 0, "Int", weight
        , "UInt", 0, "UInt", 0, "UInt", 0, "UInt", 1, "UInt", 0, "UInt", 0, "UInt", 0, "UInt", 0
        , "Str", face, "Ptr")
    G_LfFonts[key] := hf
    return hf
}

LfDelFonts() {
    global G_LfFonts
    for _, hf in G_LfFonts
        try DllCall("DeleteObject", "Ptr", hf)
    G_LfFonts := Map()
}

; 给控件设字体（WM_SETFONT）+ 记下规格，DPI 变化时能整批重建。
LfSetFont(ctl, px, weight, face) {
    global G_LfFontSpecs
    hf := LfFont(px, weight, face)
    DllCall("SendMessageW", "Ptr", ctl.Hwnd, "UInt", 0x0030, "Ptr", hf, "Ptr", 1)
    G_LfFontSpecs[ctl.Hwnd] := { px: px, weight: weight, face: face }
}

; 记一个控件的文字色/底色（WM_CTLCOLOR* 里用）。AHK 的 Gui.SetFont 颜色与自定义
; HFONT 混用会打架，所以颜色这一层干脆自己接管：STATIC/EDIT 的 WM_CTLCOLOR 我们
; 返回画刷，AHK 的默认上色就轮不到（OnMessage 返回非空 = 消息被消费）。
LfStyleCtl(ctl, fg, bg) {
    global G_LfCtlStyles
    G_LfCtlStyles[ctl.Hwnd] := { fg: fg, bg: bg }
}

LfBrush(hex) {
    global G_LfBrushes
    if (G_LfBrushes.Has(hex))
        return G_LfBrushes[hex]
    br := DllCall("gdi32\CreateSolidBrush", "UInt", LfRef(hex), "Ptr")
    G_LfBrushes[hex] := br
    return br
}

LfDelBrushes() {
    global G_LfBrushes
    for _, br in G_LfBrushes
        try DllCall("DeleteObject", "Ptr", br)
    G_LfBrushes := Map()
}

LfOnCtlColor(wp, lp, msg, hwnd) {
    global G_LfGui, G_LfCtlStyles
    global G_LfClosing
    if (G_LfClosing || !G_LfGui || !G_LfCtlStyles.Has(lp))
        return
    st := G_LfCtlStyles[lp]
    DllCall("gdi32\SetTextColor", "Ptr", wp, "UInt", LfRef(st.fg))
    DllCall("gdi32\SetBkColor", "Ptr", wp, "UInt", LfRef(st.bg))
    return LfBrush(st.bg)
}

; 控件客户区尺寸（物理 px）
LfClientSize(hwnd) {
    rc := Buffer(16, 0)
    DllCall("GetClientRect", "Ptr", hwnd, "Ptr", rc)
    return { w: NumGet(rc, 8, "Int"), h: NumGet(rc, 12, "Int") }
}

; 窗口矩形（物理 px，屏幕坐标）
LfWindowRect(hwnd) {
    rc := Buffer(16, 0)
    DllCall("GetWindowRect", "Ptr", hwnd, "Ptr", rc)
    l := NumGet(rc, 0, "Int"), t := NumGet(rc, 4, "Int")
    r := NumGet(rc, 8, "Int"), b := NumGet(rc, 12, "Int")
    return { x: l, y: t, w: r - l, h: b - t }
}

LfScreenPoint(lp) {
    x := (lp & 0xFFFF), y := (lp >> 16) & 0xFFFF
    if (x > 32767)
        x -= 65536
    if (y > 32767)
        y -= 65536
    return { x: x, y: y }
}

; 用控件自己的字体现量一段文字的物理像素宽（音标列/占位提示要按文字走）
LfTextWidth(ctl, text) {
    hfont := DllCall("SendMessageW", "Ptr", ctl.Hwnd, "UInt", 0x0031, "Ptr", 0, "Ptr", 0, "Ptr")
    hdcScr := DllCall("GetDC", "Ptr", 0, "Ptr")
    old := DllCall("SelectObject", "Ptr", hdcScr, "Ptr", hfont, "Ptr")
    ext := Buffer(8, 0)
    DllCall("GetTextExtentPoint32W", "Ptr", hdcScr, "Str", text, "Int", StrLen(text), "Ptr", ext)
    ww := NumGet(ext, 0, "Int")
    DllCall("SelectObject", "Ptr", hdcScr, "Ptr", old, "Ptr")
    DllCall("ReleaseDC", "Ptr", 0, "Ptr", hdcScr)
    return ww
}

; 量 RichEdit 里这段文字的真实高度（物理 px）。
; **不能**用 DrawText：本机实测 RichEdit 的 WM_GETFONT 返回 0（拿不到它正在用的
; HFONT），DrawText 会拿 DC 默认字体量 → 823 字符量出 80px，而实际 5 行 144px
; （probe4：EM_GETLINECOUNT=5、Georgia -24 的 tmHeight=29、真实 144/5=28.8≈29）。
; 所以：行数用 RichEdit 自己的 EM_GETLINECOUNT，行高用我们建的 HFONT 的
; tmHeight + tmExternalLeading。宽度必须先定（折行随宽度变，EM_GETLINECOUNT 即时反映）。
; 单行行高（物理 px）：我们建的 HFONT 的 tmHeight + tmExternalLeading。
LfLineHeight(px, weight, face) {
    hf := LfFont(px, weight, face)
    hdc := DllCall("GetDC", "Ptr", 0, "Ptr")
    old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hf, "Ptr")
    tm := Buffer(60, 0)
    DllCall("GetTextMetricsW", "Ptr", hdc, "Ptr", tm)
    lineH := NumGet(tm, 0, "Int") + NumGet(tm, 16, "Int")
    DllCall("SelectObject", "Ptr", hdc, "Ptr", old, "Ptr")
    DllCall("ReleaseDC", "Ptr", 0, "Ptr", hdc)
    return (lineH < 1) ? 1 : lineH
}

LfRichTextHeight(ctl, width, px, weight, face) {
    ctl.Move(, , width, Max(1, LfPx(40)))
    lines := DllCall("SendMessageW", "Ptr", ctl.Hwnd, "UInt", 0x00BA, "Ptr", 0, "Ptr", 0, "Ptr")
    if (lines < 1)
        lines := 1
    return lines * LfLineHeight(px, weight, face) + LfPx(2)
}


; ---- 屏幕抓图（BitBlt）+ 像素统计：自检的**证据来源**，不是"应该没问题" ----------
LfGrabRect(x, y, w, h) {
    hdcScr := DllCall("GetDC", "Ptr", 0, "Ptr")
    hdc := DllCall("CreateCompatibleDC", "Ptr", hdcScr, "Ptr")
    bi := Buffer(40, 0)
    NumPut("UInt", 40, bi, 0), NumPut("Int", w, bi, 4), NumPut("Int", -h, bi, 8)
    NumPut("UShort", 1, bi, 12), NumPut("UShort", 32, bi, 14)
    ppv := Buffer(8, 0)
    hbm := DllCall("CreateDIBSection", "Ptr", hdc, "Ptr", bi, "UInt", 0, "Ptr", ppv, "Ptr", 0, "UInt", 0, "Ptr")
    old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hbm, "Ptr")
    DllCall("BitBlt", "Ptr", hdc, "Int", 0, "Int", 0, "Int", w, "Int", h
        , "Ptr", hdcScr, "Int", x, "Int", y, "UInt", 0x00CC0020)
    buf := Buffer(w * h * 4, 0)
    DllCall("RtlMoveMemory", "Ptr", buf, "Ptr", NumGet(ppv, 0, "Ptr"), "Ptr", w * h * 4)
    DllCall("SelectObject", "Ptr", hdc, "Ptr", old)
    DllCall("DeleteObject", "Ptr", hbm)
    DllCall("DeleteDC", "Ptr", hdc)
    DllCall("ReleaseDC", "Ptr", 0, "Ptr", hdcScr)
    return { w: w, h: h, bits: buf }
}

; 两张图的差异像素数（自检：高亮前后 / 光标闪烁前后）
LfDiffPx(a, b) {
    n := 0
    loop a.h {
        yy := A_Index - 1
        loop a.w {
            xx := A_Index - 1
            off := (yy * a.w + xx) * 4
            if (NumGet(a.bits, off, "UChar") != NumGet(b.bits, off, "UChar")
             || NumGet(a.bits, off + 1, "UChar") != NumGet(b.bits, off + 1, "UChar")
             || NumGet(a.bits, off + 2, "UChar") != NumGet(b.bits, off + 2, "UChar"))
                n++
        }
    }
    return n
}

; 数某个目标色（容差）的像素数。高亮自检就靠它：高亮底色必须真的出现在屏幕上。
LfCountColor(img, want, tol) {
    wr := (want >> 16) & 0xFF, wg := (want >> 8) & 0xFF, wb := want & 0xFF
    n := 0
    loop img.h {
        yy := A_Index - 1
        loop img.w {
            xx := A_Index - 1
            off := (yy * img.w + xx) * 4
            rr := NumGet(img.bits, off + 2, "UChar")
            gg := NumGet(img.bits, off + 1, "UChar")
            bb := NumGet(img.bits, off, "UChar")
            if (Abs(rr - wr) <= tol && Abs(gg - wg) <= tol && Abs(bb - wb) <= tol)
                n++
        }
    }
    return n
}

; 一个像素算不算"墨"：亮度阈值判定（不数某个具体色）。LfInkProfile / LfCountInkBand
; 共用这一处口径 —— 两处各写一遍的话，以后改阈值就会出现"同一张图两个数"。
; 为什么用亮度而不是 LfCountColor 的目标色：文字是 ClearType 抗锯齿，颜色是渐变的；
; "字形到底画没画出来 / 下伸部有没有被裁"用亮度阈值最稳（第二轮修复 Bug 2b 的证据源）。
; dark=true 时反过来数**亮**像素（深色主题：纸面/面板是深底、字是暖白）。
LfIsInk(img, off, maxLum := 480, dark := false) {
    lum := NumGet(img.bits, off, "UChar") + NumGet(img.bits, off + 1, "UChar")
         + NumGet(img.bits, off + 2, "UChar")
    return dark ? (lum > 600) : (lum < maxLum)
}

; 字形墨迹的行分布：返回 { lowest, total }（lowest = 最下一条有字的行，-1 = 没有墨）。
LfInkProfile(img, maxLum := 480, dark := false) {
    lowest := -1, total := 0
    loop img.h {
        yy := A_Index - 1, row := 0
        loop img.w {
            if (LfIsInk(img, (yy * img.w + (A_Index - 1)) * 4, maxLum, dark))
                row++
        }
        total += row
        if (row > 0)
            lowest := yy
    }
    return { lowest: lowest, total: total }
}

; 指定纵向带 [y0, y1) 内的墨迹像素数（g/y/j/p/q 的下伸部就在基线以下那条带里）。
LfCountInkBand(img, y0, y1, maxLum := 480, dark := false) {
    n := 0
    y0 := Max(0, y0), y1 := Min(img.h, y1)
    loop (y1 - y0) {
        yy := y0 + A_Index - 1
        loop img.w {
            if (LfIsInk(img, (yy * img.w + (A_Index - 1)) * 4, maxLum, dark))
                n++
        }
    }
    return n
}

; 抓屏幕矩形存 PNG（GDI+ flat API；本机 probe13 已验证可用）。截图是**证据**，
; 所以连窗口外一圈 margin 一起抓：能看见 DWM 圆角与投影，而不是"应该圆角了"。
LfShotRect(path, x, y, w, h) {
    try {
        SplitPath(path, , &dir)
        if (dir != "" && !DirExist(dir))
            DirCreate(dir)
    }
    hdcScr := DllCall("GetDC", "Ptr", 0, "Ptr")
    hdc := DllCall("CreateCompatibleDC", "Ptr", hdcScr, "Ptr")
    bi := Buffer(40, 0)
    NumPut("UInt", 40, bi, 0), NumPut("Int", w, bi, 4), NumPut("Int", -h, bi, 8)
    NumPut("UShort", 1, bi, 12), NumPut("UShort", 32, bi, 14)
    ppv := Buffer(8, 0)
    hbm := DllCall("CreateDIBSection", "Ptr", hdc, "Ptr", bi, "UInt", 0, "Ptr", ppv, "Ptr", 0, "UInt", 0, "Ptr")
    old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hbm, "Ptr")
    DllCall("BitBlt", "Ptr", hdc, "Int", 0, "Int", 0, "Int", w, "Int", h
        , "Ptr", hdcScr, "Int", x, "Int", y, "UInt", 0x00CC0020)
    ok := false
    try {
        DllCall("LoadLibraryW", "Str", "gdiplus.dll", "Ptr")
        tok := Buffer(A_PtrSize, 0)
        si := Buffer(A_PtrSize = 8 ? 24 : 16, 0)
        NumPut("UInt", 1, si, 0)
        DllCall("gdiplus\GdiplusStartup", "Ptr", tok, "Ptr", si, "Ptr", 0)
        pBitmap := 0
        DllCall("gdiplus\GdipCreateBitmapFromHBITMAP", "Ptr", hbm, "Ptr", 0, "Ptr*", &pBitmap)
        if (pBitmap) {
            clsid := Buffer(16, 0)
            ; PNG 编码器 CLSID {557CF406-1A04-11D3-9A73-0000F81EF32E}
            DllCall("ole32\CLSIDFromString", "Str", "{557CF406-1A04-11D3-9A73-0000F81EF32E}", "Ptr", clsid)
            hr := DllCall("gdiplus\GdipSaveImageToFile", "Ptr", pBitmap, "WStr", path, "Ptr", clsid, "Ptr", 0)
            DllCall("gdiplus\GdipDisposeImage", "Ptr", pBitmap)
            ok := (hr = 0)
        }
    }
    DllCall("SelectObject", "Ptr", hdc, "Ptr", old)
    DllCall("DeleteObject", "Ptr", hbm)
    DllCall("DeleteDC", "Ptr", hdc)
    DllCall("ReleaseDC", "Ptr", 0, "Ptr", hdcScr)
    return ok
}

; ============================================================================
;  RichEdit：文本 + 局部高亮
; ============================================================================

; CHARFORMAT2W 一次填充。cbSize 固定 116（本机回读 = 116）。
; mask/effects 是 CFM_*/CFE_*；fg/bg 传空串表示不改那一项（传 6 位 hex 才写）。
; **底色偏移传参**：crBackColor 在本机 msftedit 上在 96（实测，见文件头），
; 所以不能硬编码 94/96 之一，必须由 LfBackOffset() 校准后传进来。
LfFmt(hwnd, scf, mask, fg, bg, effects, offBg) {
    b := Buffer(116, 0)
    NumPut("UInt", 116, b, 0)
    NumPut("UInt", mask, b, 4)
    NumPut("UInt", effects, b, 8)
    if (fg != "")
        NumPut("UInt", LfRef(fg), b, 20)          ; crTextColor 实测在偏移 20（正确）
    if (bg != "")
        NumPut("UInt", LfRef(bg), b, offBg)
    return DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0444, "Ptr", scf, "Ptr", b, "Ptr")
}

; 自校准 crBackColor 的偏移（94 还是 96）：往候选偏移写一个标记色到**整篇**
; （SCF_ALL），再用 EM_GETCHARFORMAT(SCF_ALL) 回读**同一偏移**比对。
;
; 实测依据（本机 2026-10-27，msftedit 10.0.26100.8875）：
;   · 写 96 → 回读 [96] == 标记；屏幕数到 12365 个标记色像素（真的生效）
;   · 写 94 → 回读 [94] != 标记（值跑到 [96] 的高位）；屏幕 0 个像素（没生效）
; 所以回读**在同一偏移上**是能判别的（这正是本节要的校验），而"按 SDK 在 94 读
; crBackColor"那种读法不可靠。校准失败兜底 96（本机唯一生效值），demo 的像素自检
; 会继续证明这个兜底值是真的画出颜色了。
LfBackOffset(hwnd) {
    global G_LfBackOff
    if (G_LfBackOff)
        return G_LfBackOff
    marker := 0x00112233
    for off in [94, 96] {
        b := Buffer(116, 0)
        NumPut("UInt", 116, b, 0)
        NumPut("UInt", 0x04000000, b, 4)          ; CFM_BACKCOLOR
        NumPut("UInt", marker, b, off)
        DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0444, "Ptr", 4, "Ptr", b, "Ptr")  ; SCF_ALL
        rb := Buffer(116, 0)
        NumPut("UInt", 116, rb, 0)
        DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x043A, "Ptr", 4, "Ptr", rb, "Ptr")
        if ((NumGet(rb, off, "UInt") & 0xFFFFFF) = marker) {
            G_LfBackOff := off
            Log("长文面板：crBackColor 偏移自校准 = " . off)
            return off
        }
    }
    G_LfBackOff := 96
    Log("长文面板：crBackColor 偏移自校准失败，兜底 96（本机实测生效值）")
    return 96
}

LfRichSetText(hwnd, text) {
    DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x000C, "Ptr", 0, "Str", text, "Ptr")   ; WM_SETTEXT
}

; 整篇复位：字色/底色/粗体/斜体一次刷（SCF_ALL）。不置 CFM_SIZE/CFM_FACE ——
; 字体由 WM_SETFONT 给（研究 §2：顺手置 SIZE|FACE 会把整篇字体重置）。
LfRichReset(hwnd, fg, bg, italic) {
    off := LfBackOffset(hwnd)
    mask := 0x40000000 | 0x04000000 | 0x00000001 | 0x00000002  ; COLOR|BACKCOLOR|BOLD|ITALIC
    eff := italic ? 0x00000002 : 0                             ; CFE_ITALIC
    LfFmt(hwnd, 4, mask, fg, bg, eff, off)                     ; SCF_ALL
}

LfRichSetSel(hwnd, a, b) {
    cr := Buffer(8, 0)
    NumPut("Int", a, cr, 0), NumPut("Int", b, cr, 4)
    DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0437, "Ptr", 0, "Ptr", cr, "Ptr")
}

LfRichGetSel(hwnd) {
    cr := Buffer(8, 0)
    DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0434, "Ptr", 0, "Ptr", cr, "Ptr")
    return { min: NumGet(cr, 0, "Int"), max: NumGet(cr, 4, "Int") }
}

; 把 runs 拼成文本并按 marks 上高亮。
;   runs = [{ text, mark: ""|"cur"|"soft" }, ...]
;   opts = { fg, bg, italic }（fg/bg 是 6 位 hex）
;
; 「荧光笔」语义：高亮 = 实色底（设计色按透明度混到纸面，见 LfMix）+ 加粗；
; **非高亮正文不灰化**（规格 §9.1 的关键决定 4：灰化会让整句像禁用状态）。
; 每次改格式前后都**还原用户原来的选择**（研究 §2.1：不还原会多出一块选中底色）。
LfRichApply(runs, opts) {
    global G_LfC, G_LfPal, G_LfBackOff
    hwnd := G_LfC.sent.Hwnd
    off := LfBackOffset(hwnd)
    text := ""
    spans := []
    for run in runs {
        start := StrLen(text)
        text .= run.text
        if (run.mark != "")
            spans.Push({ start: start, end: StrLen(text), mark: run.mark })
    }
    LfRichSetText(hwnd, text)
    LfRichReset(hwnd, opts.fg, opts.bg, opts.italic ? true : false)
    ; 逐区间上色。SCF_SELECTION=1；只置需要的位，别顺手清掉整篇字体。
    for sp in spans {
        old := LfRichGetSel(hwnd)
        LfRichSetSel(hwnd, sp.start, sp.end)
        if (sp.mark = "cur") {
            mask := 0x04000000 | 0x00000001                     ; BACKCOLOR|BOLD
            if (G_LfPal.markCurText != opts.fg)
                mask |= 0x40000000                              ; 深色高亮要改字色
            LfFmt(hwnd, 1, mask, G_LfPal.markCurText, G_LfPal.markCur, 0x00000001, off)
        } else {
            LfFmt(hwnd, 1, 0x04000000, "", G_LfPal.markSoft, 0, off)
        }
        ; 还原选择（不还原 = 多出一块选中蓝底，研究 §2.1 实测 3772 px）
        LfRichSetSel(hwnd, old.min, old.max)
    }
    ; 选择归零，避免光标/选中态露出
    LfRichSetSel(hwnd, 0, 0)
}

; RichEdit 背景色（EM_SETBKGNDCOLOR，COLORREF = 0x00BBGGRR）
LfRichSetBg(hwnd, hex) {
    DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0443, "Ptr", 0, "Ptr", LfRef(hex), "Ptr")
}

; ============================================================================
;  示例文章（本阶段的数据源；T6 会换成内核/中枢的真实文章）
; ============================================================================
; 一段真实的英文长文（12 段）。只有第 9 段被"命中"，因为视觉稿的核心状态都围绕它：
; 命中态/一句多词/全文展开/质量偏低共用这一段的句子。
LfArticle() {
    return [
          "How Reading Habits Shape Vocabulary Growth"
        , "Vocabulary growth is usually described as a byproduct of reading volume. The more words a reader meets, the more likely some of them stick. This account is simple, intuitive, and almost certainly incomplete."
        , "Researchers have spent decades trying to predict which words survive a first encounter. Frequency helps, but only weakly. Words that appear in emotionally charged passages are remembered far better than words in neutral lists."
        , "The missing element appears to be attention. Readers who skim a page may meet more words, but they rarely pause long enough for a new word to leave a trace. Depth of processing matters more than raw exposure."
        , "This is why marginal notes and underlining work. They force a reader to stop, decide what matters, and connect the new word to something already known. The act of deciding is the act of encoding."
        , "Digital reading complicates the picture. Screens invite scanning, and scanning is precisely the habit that starves vocabulary growth. A reader can finish a long article and remember almost nothing of its language."
        , "None of this means screens are hopeless. It means the interface must make pausing cheap. A single keystroke is cheap. A page reload is not. A trip to a dictionary tab is somewhere in between."
        , "Designers of reading tools rarely measure vocabulary outcomes. They measure time on page, scroll depth, and clicks. Those numbers reward speed, and speed is the enemy of the very thing readers say they want."
        , "A series of experiments at several universities traced this pattern across thousands of participants. The researchers observed that participants who procrastinated tended to underestimate the complexity of the assignment. The gap widened whenever the task felt ambiguous or the deadline sat weeks away."
        , "The pattern held across ages, languages, and education levels. What changed was not the size of the gap but how quickly it closed. Structured pauses closed it faster than any amount of rereading."
        , "One practical lesson follows. If a word is worth noticing, it is worth capturing at the moment of noticing. Every extra step between noticing and recording is a step where the word is lost."
        , "The best reading companions therefore stay out of the way. They wait in a corner of the screen, they open on a single keystroke, and they close without asking whether anything should be saved. The words save themselves."
    ]
}

; 一段纸面提示（空态用）
LfEmptyHint() {
    return "光标停在词框里，打完这个词再按 ↵ 存入本篇会话。原句会在命中时出现在这里。"
}

LfNoMatchHint(word) {
    return "这篇里没有找到 “" . word . "”。可以直接保存，或换个词再试。"
}

; ============================================================================
;  七种状态的视觉数据（demo 与真实数据的形状一致；真实数据下一阶段接）
; ============================================================================
LfRunSplit(sentence, marks) {
    ; marks = [{word, mark}]，按出现顺序切成 runs（只切第一次出现，够 demo 用）
    runs := [], rest := sentence
    for m in marks {
        p := InStr(rest, m.word)
        if (!p)
            continue
        if (p > 1)
            runs.Push({ text: SubStr(rest, 1, p - 1), mark: "" })
        runs.Push({ text: m.word, mark: m.mark })
        rest := SubStr(rest, p + StrLen(m.word))
    }
    if (rest != "")
        runs.Push({ text: rest, mark: "" })
    return runs
}

LfDemoData(state) {
    global LF_STATES
    if (!LfHas(LF_STATES, state))
        state := "hit"
    art := LfArticle()
    p9 := art[9]                       ; 第 9 段（1 基；视觉稿的命中段）
    ; 命中态只显示前两句（视觉稿的纸面），全文展开态显示整段（会截断）
    i3 := InStr(p9, "The gap widened")
    p9short := (i3 > 1) ? SubStr(p9, 1, i3 - 2) : p9
    ; kind 一律存**枚举** `word/phrase/sentence`（与 G_LfTarget.kind 同口径），中文标签
    ; 只在渲染层由 Logic.KindLabel() 映射 —— 数据里写"词"会让 LfRender 静默显示成"词"。
    d := { state: state, dark: false, segCount: 12, segIndex: 9
         , segLabel: "12 段 · 第 9 段", segFill: 0.75
         , word: "researchers", kind: "word", phonetic: "/rɪˈsɜːrtʃərz/"
         , meaning: "n. 研究人员", meaningHint: false
         , paperText: p9short
         , runs: LfRunSplit(p9short, [{ word: "researchers", mark: "cur" }])
         , italic: false, paperFg: ""
         , note: { kind: "ok", text: "本篇已存 3 词" }
         , outline: false, lowQuality: false, clip: false
         , showQuit: true, hintRight: "全文 Alt ↓", reocr: false }
    ; ---- 阶段 2 契约字段（名称与 T2 内核 / T4 中枢一致，先按契约填 demo 值）----
    ; 内核 page_ocr 回包：page_id / passage_breaks / quality / words_per_line / weird
    ; 中枢 article_locate 回包：found / sentence / passage_index / target_start... / occurrences
    ; 保存时 SpoolLine 的可选字段：doc_id / passage_index（缺省不写）
    d.pageId := "p-demo0001"
    d.passages := art
    d.passageBreaks := []                     ; demo 不算偏移（真实链路才由内核给段偏移）
    d.quality := d.lowQuality ? "low" : "ok"
    d.docId := 0                              ; 0 = 还没建文章（demo）
    tgtStart := (d.word != "") ? (InStr(d.paperText, d.word) - 1) : -1
    d.locate := { found: (tgtStart >= 0), sentence: (tgtStart >= 0) ? d.paperText : ""
                , passage_index: d.segIndex
                , sentence_start: 0, sentence_end: StrLen(d.paperText)
                , target_start: tgtStart
                , target_end: (tgtStart >= 0) ? (tgtStart + StrLen(d.word)) : -1
                , occurrences: [] }
    switch state {
        case "hit":
            ; 核心形态：目标词 + 音标释义 + 纸面高亮 + 已存计数
            d.segIndex := 9
        case "dark":
            d.dark := true
        case "empty":
            d.word := "", d.kind := "word", d.phonetic := "", d.meaning := ""  ; kind 也走枚举口径
            d.meaningHint := true
            d.meaningText := "音标与释义自动填入 · Tab 补全句中原词"
            d.paperText := LfEmptyHint()
            d.runs := [{ text: d.paperText, mark: "" }]
            d.paperFg := "hint"
            d.segIndex := 0, d.segLabel := "12 段 · 已读 0", d.segFill := 0
            d.note := { kind: "none", text: "" }
        case "multi":
            ; 一句多词：已存词淡笔触 + 当前词重笔触（视觉稿 ④）
            d.word := "procrastinated", d.phonetic := "/prəˈkræstɪneɪtɪd/"
            d.meaning := "v. 拖延（过去式）"
            d.runs := LfRunSplit(p9short, [{ word: "researchers", mark: "soft" }
                , { word: "procrastinated", mark: "cur" }
                , { word: "complexity", mark: "soft" }])
            d.note := { kind: "ok", text: "本篇 3 词 · 本句 3 词" }
        case "outline":
            ; 全文展开：长句截断 + 段落目录（当前段高亮）。AHK 做不出 CSS 渐隐，
            ; 用固定高度裁切替代（如实记在报告里），目录是真实文章的前两段 + 当前段 + 下一段。
            d.paperText := p9
            d.runs := LfRunSplit(p9, [{ word: "researchers", mark: "cur" }])
            d.outline := true, d.clip := true
            d.note := { kind: "none", text: "" }
            d.hintRight := "收起 Alt ↑"
            d.outlineRows := [
                  { n: "1", t: art[1] }
                , { n: "2", t: art[2] }
                , { n: "9", t: art[9] }
                , { n: "10", t: art[10] }
            ]
            d.outlineHere := 3
        case "low":
            ; 质量偏低：橙色进度条 + 纸面橙色内描边 + 警示行 + 「重新识别」
            d.lowQuality := true
            d.segFill := 0.75
            d.note := { kind: "warn", text: "这段识别质量偏低 · 建议缩小选区或改用粘贴" }
            d.showQuit := false, d.reocr := true
        case "nomatch":
            ; 未命中：斜体说明 + 灰色 pip + 仍可保存（视觉稿 ⑦）
            d.word := "ubiquitous", d.phonetic := "/juːˈbɪkwɪtəs/"
            d.meaning := "adj. 无处不在的"
            d.paperText := LfNoMatchHint(d.word)
            d.runs := [{ text: d.paperText, mark: "" }]
            d.italic := true, d.paperFg := "hint"
            d.note := { kind: "nomatch", text: "未命中原文 · 仍可保存" }
    }
    return d
}

LfHas(arr, v) {
    for x in arr
        if (x = v)
            return true
    return false
}

; ============================================================================
;  建窗（不动旧框）
; ============================================================================
LfBuild(data) {
    global G_LfGui, G_LfC, G_LfData, G_LfPal, G_LfDpi, G_LfScale, G_LfState
    global G_LfFontSpecs, G_LfCtlStyles, G_LfBtnKind, G_LfBackOff, G_LfChainHint
    LfEnsureRich()
    LfDestroyWindow()                       ; 重建前先清旧的（幂等）
    G_LfData := data
    G_LfState := data.state
    G_LfPal := LfPalette(data.dark)
    pal := G_LfPal

    ; 无边框 + ToolWindow + AlwaysOnTop + Resize。DWM 圆角在本组合上实测生效
    ; （有标题栏时圆角被标题栏遮住；+Parent 子窗口上失效 —— 两条都实测过，见规格 §9.2）。
    g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +Resize"
           , G_LfDemo ? "长文面板（demo：" . data.state . "）" : "长文面板")
    g.BackColor := pal.panel
    g.MarginX := 0
    g.MarginY := 0
    G_LfGui := g
    LfShowHidden(g)
    ; DWM 圆角 + 深色标题栏属性（Win11；Win10 上调用失败无害）
    DllCall("dwmapi\DwmSetWindowAttribute", "Ptr", g.Hwnd, "Int", 33, "Int*", 2, "Int", 4)
    dark := data.dark ? 1 : 0
    DllCall("dwmapi\DwmSetWindowAttribute", "Ptr", g.Hwnd, "Int", 20, "Int*", dark, "Int", 4)

    ; --- 窗口 DPI：**必须是窗口自己的 DPI**，不是 A_ScreenDPI（跨屏时后者只反映主屏）。
    G_LfDpi := DllCall("GetDpiForWindow", "Ptr", g.Hwnd, "UInt")
    G_LfScale := G_LfDpi / 96
    LfDelFonts()
    G_LfFontSpecs := Map()
    G_LfCtlStyles := Map()
    G_LfBtnKind := Map()
    G_LfBackOff := 0                        ; 新控件 → 重新校准

    C := {}
    ; 纸面**先建**（后建的 RichEdit/文字压在上面）：纸边 1px（质量偏低时变橙色内描边）
    C.paperEdge := g.AddText("x0 y0 w1 h1 Background" . (data.lowQuality ? pal.warnEdge : pal.paperLine), "")
    C.paper := g.AddText("x0 y0 w1 h1 Background" . pal.paper, "")
    ; 原句控件：RichEdit（局部高亮只能在富文本控件上做，研究 §8 方案 a）。
    ; 只读**只能**在创建样式里给 ES_READONLY(0x800)（研究 §5：EM_SETREADONLY 拦不住 WM_CHAR）。
    C.sent := g.AddCustom("ClassRICHEDIT50W x0 y0 w1 h1 +0x4 +0x40 +0x800 -0x10000", "")
    LfSetFont(C.sent, 16, 400, "Georgia")
    LfRichSetBg(C.sent.Hwnd, pal.paper)
    ; RichEdit 左右内边距交给纸面 padding 做（EM_SETMARGINS 归零，避免双重缩进）
    DllCall("SendMessageW", "Ptr", C.sent.Hwnd, "UInt", 0x00D3, "Ptr", 0x0001 | 0x0002, "Ptr", 0, "Ptr")

    ; --- 顶栏：几乎隐形（段进度条 + 第几段 + 拖动把手 + ✕）---
    C.meterTrack := g.AddText("x0 y0 w1 h1 Background" . pal.line, "")
    C.meterFill := g.AddText("x0 y0 w1 h1 Background" . (data.lowQuality ? pal.warnPip : pal.accent), "")
    C.meterText := g.AddText("x0 y0 w1 h1 +0x4000", "")   ; SS_ENDELLIPSIS：超长显示 …
    LfSetFont(C.meterText, 11, 400, "Segoe UI")
    LfStyleCtl(C.meterText, pal.ink4, pal.panel)
    ; v21 round2 (B)：session 来源短标题（取词台侧栏里的同一个标题）—— 顶栏右侧、
    ; 拖动把手之前；空标题整块隐藏、不占位。只读 Static + SS_ENDELLIPSIS，
    ; 长标题由 LfLayout 按 LfTextWidth 收缩（口径复用旧框 ShortTitle：28 字 + …）。
    C.srcTitle := g.AddText("x0 y0 w1 h1 +0x4000", "")
    LfSetFont(C.srcTitle, 11, 400, "Segoe UI")
    LfStyleCtl(C.srcTitle, pal.ink4, pal.panel)
    C.grip := g.AddText("x0 y0 w1 h1", "⋮⋮")          ; Static 的 WM_NCHITTEST 是 HTTRANSPARENT
    LfSetFont(C.grip, 13, 400, "Segoe UI")             ; → 按在把手上落到父窗口 → HTCAPTION 拖动
    LfStyleCtl(C.grip, pal.ink4, pal.panel)
    C.close := g.AddButton("x0 y0 w1 h1", "")
    LfMakeOwnerDraw(C.close, "close")

    ; --- 目标词行：24px/500，不加框不加底（规格 §9.1 关键决定 2；字号/字重按用户
    ;     验收 Bug 2b 从 28/600 下调 —— 太大太粗、长短语显示字符太少）---
    C.word := g.AddEdit("x0 y0 w1 h1 -E0x200 -VScroll +Background" . pal.panel)
    LfSetFont(C.word, 24, 500, "Segoe UI")
    LfStyleCtl(C.word, pal.ink, pal.panel)
    ; 目标词是**英文词**：关掉这个词框的 IME。否则中文输入法的候选框会吞掉 Enter
    ; （「Enter=保存」变成「选候选词」，本机 E2E 实测踩到一次），数字键同理。
    try DllCall("imm32\ImmAssociateContextEx", "Ptr", C.word.Hwnd, "Ptr", 0, "UInt", 0)
    ; 空态占位提示 + 强调色闪烁光标（设计稿的空态形态）。占位用 ReadOnly Edit：
    ; 可点击（点它 → Focus 事件把焦点交给真词框），Static 会把点击吞掉。
    C.wordPh := g.AddEdit("x0 y0 w1 h1 ReadOnly Center -E0x200 -0x10000 +Background" . pal.panel)
    LfSetFont(C.wordPh, 22, 400, "Segoe UI")
    LfStyleCtl(C.wordPh, pal.ink4, pal.panel)
    C.wordPh.Value := "输入目标词"
    C.caret := g.AddText("x0 y0 w1 h1 Background" . pal.accent, "")
    C.kind := g.AddText("x0 y0 w1 h1", "词")
    LfSetFont(C.kind, 10, 700, "Segoe UI")
    LfStyleCtl(C.kind, pal.accent, pal.accentSoft)
    ; 释义（第二层信息）。**音标按用户验收 Bug 4 撤掉**：数据层仍保留 phonetic
    ; （LfLookupFire / G_LfTarget.phonetic / spool 行 / 字典回包一律不动），只是不显示。
    C.meaning := g.AddEdit("x0 y0 w1 h1 -E0x200 -VScroll +Background" . pal.panel)
    LfSetFont(C.meaning, 14, 400, "Segoe UI")
    LfStyleCtl(C.meaning, pal.ink2, pal.panel)

    ; --- 状态行（只在该出现时出现，不占常驻高度）---
    C.pip := g.AddText("x0 y0 w1 h1", "●")
    LfSetFont(C.pip, 7, 400, "Segoe UI")
    C.note := g.AddText("x0 y0 w1 h1", "")
    LfSetFont(C.note, 12, 400, "Segoe UI")

    ; --- 全文折叠区（段落目录，当前段高亮；仅展开态出现）---
    C.outlineRule := g.AddText("x0 y0 w1 h1 Background" . pal.line, "")
    C.caps := g.AddText("x0 y0 w1 h1", "全文")
    LfSetFont(C.caps, 10, 700, "Segoe UI")
    LfStyleCtl(C.caps, pal.ink4, pal.panel)
    C.capsN := g.AddText("x0 y0 w1 h1", "")
    LfSetFont(C.capsN, 10, 400, "Segoe UI")
    LfStyleCtl(C.capsN, pal.ink4, pal.panel)
    C.outlineHint := g.AddText("x0 y0 w1 h1", "j / k 跳段")
    LfSetFont(C.outlineHint, 11, 400, "Segoe UI")
    LfStyleCtl(C.outlineHint, pal.ink4, pal.panel)
    C.outNum := [], C.outTxt := []
    loop LF.OUTLINE_ROWS {
        n := g.AddText("x0 y0 w1 h1", "")
        LfSetFont(n, 11, 400, "Cascadia Mono")
        t := g.AddText("x0 y0 w1 h1 +0x4000", "")   ; SS_ENDELLIPSIS（视觉稿的 text-overflow:ellipsis）
        LfSetFont(t, 13, 400, "Georgia")
        C.outNum.Push(n)
        C.outTxt.Push(t)
    }

    ; --- 分隔线 + 按钮行（保存蓝色实心、收工纯文字；键位字等宽 55% 透明）---
    C.rule := g.AddText("x0 y0 w1 h1 Background" . pal.line, "")
    C.save := g.AddButton("x0 y0 w1 h1", "")
    LfMakeOwnerDraw(C.save, "primary")
    C.quit := g.AddButton("x0 y0 w1 h1", "")
    LfMakeOwnerDraw(C.quit, "plain")
    C.reocr := g.AddButton("x0 y0 w1 h1", "")
    LfMakeOwnerDraw(C.reocr, "soft")
    ; v22 P2b（票 #167 / spec §9）：分段预览窗入口。按钮常显；没有 doc_id 时由
    ; LfInspectorToggle() 短路（自有绘制按钮的 disabled 态画不出来，短路比置灰诚实）。
    C.inspect := g.AddButton("x0 y0 w1 h1", "")
    LfMakeOwnerDraw(C.inspect, "inspect")
    C.footHint := g.AddText("x0 y0 w1 h1", data.hintRight)
    LfSetFont(C.footHint, 11, 400, "Segoe UI")
    LfStyleCtl(C.footHint, pal.ink4, pal.panel)

    ; --- 事件 ---
    C.save.OnEvent("Click", (*) => LfOnSaveClick())
    C.quit.OnEvent("Click", (*) => LfClose())
    C.close.OnEvent("Click", (*) => LfClose())
    C.reocr.OnEvent("Click", (*) => LfOnReocrClick())
    C.inspect.OnEvent("Click", (*) => LfInspectorToggle())
    C.word.OnEvent("Change", (*) => LfOnTargetChange())
    C.word.OnEvent("Focus", (*) => LfWordFocus(true))
    C.word.OnEvent("LoseFocus", (*) => LfWordFocus(false))
    C.wordPh.OnEvent("Focus", (*) => LfFocusWord())
    g.OnEvent("Size", LfOnSize)
    g.OnEvent("Close", (*) => LfClose())

    G_LfC := C
    LfSyncInspectButton()
    ; #205：新面板 = 新会话，次数标签归零（它记的是「上一串词算出来的标签」，跨面板留着
    ; 会让新会话的第一次 Change 误判「值没变」而不写提示）。
    G_LfChainHint := ""
    ; 首帧装载：控件是新建的空框，数据里可能已有词（demo 的七状态 / 恢复后的文章）
    ; → 显式走收口写一次（LfRender 只读不写，见 Bug 2a 的根因）。
    ; 缺省用 Logic.ahk 的 ObjProp（不存在 → ""），别自己再造一个属性判断。
    LfWriteWord(ObjProp(data, "word"))
    LfRender(data)
    SetTimer(LfBlinkTick, 550)
    ; v21 round2 (B)：来源缓存定时器（只随面板存在而跑：LfDestroyWindow 摘掉）。
    ; 700ms 低频——足够覆盖「复制 → 切回面板 → Ctrl+V」的间隔，又不轮询成忙循环。
    SetTimer(PanelProvCacheTick, 700)
    return g
}

LfShowHidden(g) {
    ; 先建出 HWND（Show "Hide" 不显示）—— GetDpiForWindow 需要真实窗口
    g.Show("Hide x100 y100 w" . LfPx(LF.PANEL_W) . " h" . LfPx(400))
}

LfMakeOwnerDraw(btn, kind) {
    global G_LfBtnKind
    ; AHK 的 "+0x0B" 会被它自己的默认按钮样式吃掉（实测 +0x0B 后 type 仍是 0x1），
    ; 所以建完再改样式位：清掉低 4 位（BS_TYPEMASK）后置 BS_OWNERDRAW(0x0B)。
    st := DllCall("GetWindowLongW", "Ptr", btn.Hwnd, "Int", -16, "Int")
    st := ((st & ~0xF) & 0xFFFFFFFF) | 0x0B
    DllCall("SetWindowLongW", "Ptr", btn.Hwnd, "Int", -16, "Int", st)
    G_LfBtnKind[btn.Hwnd] := kind
}

; ============================================================================
;  渲染：数据 → 控件
; ============================================================================
LfRender(d) {
    global G_LfC, G_LfPal, G_LfState, G_LfClosing, G_LfGui
    ; 票 #175（A2）：关面板后迟到的 locate/lookup 回包仍会调这里 —— 而 LfClose 末尾
    ; 已 `G_LfC := {}`（G_LfData 留着旧对象，`d.word = G_LfTarget.word` 照样通过），
    ; 再读 C.word 等控件就抛 PropertyError（回话跑在 Critical 回调里 → 阻塞错误框）。
    ; 守卫与 LfOnHubDown 的 `G_LfC.HasOwnProp("note")` 同款：GUI 已拆 / 控件表已清
    ; → 本次渲染整条丢弃。
    if (G_LfClosing || !G_LfGui || !G_LfC.HasOwnProp("word"))
        return
    C := G_LfC, pal := G_LfPal
    ; 目标词（空态：占位 + 强调色光标；有词：真词 + 类型 chip）
    ; **渲染只读不写词框**（用户验收 Bug 2a 的根因）：旧实现在这里
    ; `if (C.word.Value != d.word) C.word.Value := d.word`，而定位回包把 d.word
    ; 规范成去掉尾随空格 → 用户刚敲完空格（框里 `word `）就被程序重写，AHK 的
    ; `Value :=` 把光标塌回 0；回包迟到时还会覆盖用户新输入。程序写值一律走
    ; LfWriteWord / LfAcceptTarget 这一个收口（写后光标到末尾）。
    empty := (NormalizeLine(C.word.Value) = "")
    C.wordPh.Visible := empty
    C.caret.Visible := empty
    C.kind.Visible := !empty
    C.kind.Text := KindLabel(d.kind)
    LfStyleCtl(C.kind, pal.accent, pal.accentSoft)
    ; 释义（音标不再显示：位置全部让给释义，见 LfLayout）
    C.meaning.Value := d.meaningHint ? d.meaningText : d.meaning
    LfStyleCtl(C.meaning, d.meaningHint ? pal.meaningHint : pal.ink2, pal.panel)
    ; 原句纸面：高亮 + 颜色（未命中/空态是斜体灰字整段说明）
    fg := (d.paperFg = "hint") ? pal.paperHint : pal.sentText
    LfRichApply(d.runs, { fg: fg, bg: pal.paper, italic: d.italic })
    ; 状态行
    kind := d.note.kind
    C.pip.Visible := (kind != "none")
    C.note.Visible := (kind != "none")
    C.note.Text := d.note.text
    pipColor := pal.green
    noteColor := pal.ink3
    if (kind = "warn")
        pipColor := pal.warnPip, noteColor := pal.warn
    else if (kind = "nomatch")
        pipColor := "9AA0A9", noteColor := pal.ink3
    LfStyleCtl(C.pip, pipColor, pal.panel)
    LfStyleCtl(C.note, noteColor, pal.panel)
    ; 按钮/提示。#205：底栏这一格可能挂着顺句次数标签（Change 路径写的局部状态，
    ; 见 LfUpdateChainHint）—— 重渲染时按数据里的 chainHint 还原，否则下一次定位回包
    ; 会把它擦掉，而那时输入框还是「尾随空格」的顺句态。
    C.reocr.Visible := d.reocr
    C.quit.Visible := d.showQuit
    chainHint := ObjProp(d, "chainHint")
    C.footHint.Text := (chainHint != "") ? chainHint : d.hintRight
    ; v21 round2 (B)：来源短标题文本（**可见性只由 LfLayout 一处决定**：
    ; 按文本量算 srcW 后 `C.srcTitle.Visible := (srcW > 0)`，空标题不占位）
    C.srcTitle.Text := LfSourceTitleText(d)
    ; 全文目录
    C.outlineRule.Visible := d.outline
    C.caps.Visible := d.outline
    C.capsN.Visible := d.outline
    C.outlineHint.Visible := d.outline
    if (d.outline)
        C.capsN.Text := d.segCount . " 段"
    for i, n in C.outNum {
        n.Visible := d.outline
        C.outTxt[i].Visible := d.outline
    }
    LfFit()
    LfInvalidate()
}

; 窗口内需重绘（改颜色/字体后）。W公Inv 用 RedrawWindow 比 InvalidateRect(TRUE) 稳。
LfInvalidate() {
    global G_LfGui
    if (G_LfGui)
        DllCall("RedrawWindow", "Ptr", G_LfGui.Hwnd, "Ptr", 0, "Ptr", 0
            , "UInt", 0x0001 | 0x0400 | 0x0080)      ; RDW_INVALIDATE|RDW_ALLCHILDREN|RDW_UPDATENOW
}

; ============================================================================
;  布局：一次算完全部位置与显隐；返回"内容自然高度"
;  跨屏 DPI 的所有尺寸都经 LfPx()（= 设计 px × GetDpiForWindow/96）。
; ============================================================================
LfLayout(extra := 0, forceOutlineRows := false) {
    global G_LfGui, G_LfC, G_LfData, G_LfPal, G_LfClosing, G_LfWordRow, G_LfOutlineShown
    global G_LfFootRow
    C := G_LfC, d := G_LfData
    if (!G_LfGui || G_LfClosing || !IsObject(d))
        return 0
    sz := LfClientSize(G_LfGui.Hwnd)
    cw := sz.w
    x0 := LfPx(LF.PAD)
    innerW := cw - 2 * x0
    pal := G_LfPal
    y := LfPx(LF.HEAD_PAD_TOP)
    headRowH := LfPx(LF.HEAD_ROW)
    ; ---- 顶栏（34）----
    headBottom := y + headRowH
    closeW := LfPx(18)
    gripW := LfPx(18)
    C.close.Move(cw - x0 - closeW, y, closeW, headRowH)
    C.grip.Move(cw - x0 - closeW - gripW, y, gripW, headRowH)
    ; v21 round2 (B)：来源短标题占顶栏右侧（把手之前）；空标题不占位。
    ; 宽度按文字量、上限 170 逻辑 px —— 段进度文字先让位，但不让到看不见。
    srcText := LfSourceTitleText(d)
    srcW := 0
    if (srcText != "") {
        srcW := Min(LfTextWidth(C.srcTitle, srcText), LfPx(170))
        C.srcTitle.Move(cw - x0 - closeW - gripW - LfPx(9) - srcW, y, srcW, headRowH)
    }
    C.srcTitle.Visible := (srcW > 0)
    meterW := LfPx(44)
    meterY := y + (headRowH - LfPx(3)) // 2
    C.meterTrack.Move(x0, meterY, meterW, Max(1, LfPx(3)))
    fillW := Round(meterW * (d.segFill + 0.0))
    C.meterFill.Visible := (fillW > 0)
    C.meterFill.Move(x0, meterY, Max(1, fillW), Max(1, LfPx(3)))
    meterTextW := cw - x0 - closeW - gripW - meterW - LfPx(9)
    if (srcW > 0)
        meterTextW -= srcW + LfPx(9)
    C.meterText.Move(x0 + meterW + LfPx(9), y, Max(LfPx(40), meterTextW), headRowH)
    C.meterText.Text := d.segLabel
    y := headBottom + LfPx(LF.BODY_PAD_TOP)

    ; ---- 目标词行（42）----
    wordH := LfPx(LF.WORD_H)
    kindH := LfPx(16)
    ; 行几何先记下来：Change 路径的 chip 轻量重摆（LfPlaceKindChip）用同一份。
    G_LfWordRow := { x0: x0, y: y, innerW: innerW, wordH: wordH, kindH: kindH }
    ; chip 宽度按**实际文本**量 + chip 位置按文本宽（Bug 2c/3）都收口在 LfPlaceKindChip；
    ; 词框按 chip 宽度让位（旧值写死 LfPx(24)，phrase/sentence 被裁成 "phra…"）。
    ; LfTextWidth 返回物理 px，直接进 Move（不再乘一次缩放）。
    kindW := LfPlaceKindChip()
    C.word.Move(x0, y, innerW - (C.kind.Visible ? kindW + LfPx(9) : 0), wordH)
    C.wordPh.Move(x0, y, Min(innerW, LfPx(150)), wordH)
    phW := LfTextWidth(C.wordPh, "输入目标词")
    caretH := LfPx(26)
    C.caret.Move(x0 + phW + LfPx(9), y + (wordH - caretH) // 2, Max(2, LfPx(2)), caretH)
    y += wordH

    ; ---- 释义行（+7，占满整行：音标按用户要求撤掉，位置让给释义）----
    y += LfPx(LF.SUB_GAP)
    subH := LfPx(LF.SUB_H)
    C.meaning.Move(x0, y, innerW, subH)
    y += subH

    ; ---- 纸面（+15）----
    y += LfPx(LF.PAPER_GAP)
    paperTop := y
    padX := LfPx(LF.PAPER_PAD_X), padT := LfPx(LF.PAPER_PAD_TOP), padB := LfPx(LF.PAPER_PAD_BOT)
    sentW := innerW - 2 * padX
    ; 先给 RichEdit 定宽再量高（折行随宽度变，行数即时可查）
    textH := LfRichTextHeight(C.sent, sentW, 16, 400, "Georgia")
    if (d.clip) {
        ; 视觉稿 .paper.clip 的上限 118；AHK 做不出 CSS 的渐隐（GDI 没有 alpha 渐变），
        ; 退而求其次：**按整行**裁切，别把一行从中间切一半（本机首版截图踩到过）。
        lineH := LfLineHeight(16, 400, "Georgia")
        maxH := LfPx(LF.CLIP_H) - LfPx(2) - padT - padB
        textH := Max(1, maxH // lineH) * lineH
    } else if (extra > 0) {
        textH += extra
    }
    textH := Max(textH, LfPx(24))
    paperH := LfPx(2) + padT + textH + padB
    C.paperEdge.Move(x0, paperTop, innerW, paperH)
    C.paper.Move(x0 + LfPx(1), paperTop + LfPx(1), innerW - LfPx(2), paperH - LfPx(2))
    C.sent.Move(x0 + LfPx(1) + padX, paperTop + LfPx(1) + padT, sentW, textH)
    y := paperTop + paperH

    ; ---- 状态行（+11，仅出现时占位）----
    noteVisible := (d.note.kind != "none")
    noteH := LfPx(LF.NOTE_H)
    if (noteVisible) {
        y += LfPx(LF.NOTE_GAP)
        C.pip.Move(x0 + LfPx(1), y, LfPx(8), noteH)
        C.note.Move(x0 + LfPx(12), y, innerW - LfPx(12), noteH)
        y += noteH
    }

    ; ---- 全文目录（+13，仅展开态）----
    if (d.outline) {
        y += LfPx(LF.OUTLINE_GAP)
        C.outlineRule.Move(x0, y, innerW, Max(1, LfPx(1)))
        y += Max(1, LfPx(1)) + LfPx(LF.OUTLINE_PAD)
        capsW := LfTextWidth(C.caps, "全文")
        C.caps.Move(x0, y, capsW, LfPx(LF.OUTLINE_HEAD_H))
        C.capsN.Move(x0 + capsW + LfPx(5), y, LfPx(60), LfPx(LF.OUTLINE_HEAD_H))
        hintW := LfTextWidth(C.outlineHint, C.outlineHint.Text)
        C.outlineHint.Move(x0 + innerW - hintW, y, hintW, LfPx(LF.OUTLINE_HEAD_H))
        y += LfPx(LF.OUTLINE_HEAD_H) + LfPx(7)
        rowH := LfPx(LF.OUTLINE_ROW_H)
        ; v22 P2b：行数上限 8（OUTLINE_ROWS），且**面板高度允许时**才显示那么多：
        ; 底部固定区（分隔线 + 按钮行）之上还剩多少高度就显示多少行（至少 1 行，
        ; 展开时 LfToggleOutline 已先把窗口长到自然高度，所以正常都能到 8）。
        bottomFixed := LfPx(LF.RULE_GAP) + Max(1, LfPx(1)) + LfPx(LF.ACT_PAD_TOP)
        bottomFixed += LfPx(LF.BTN_H) + LfPx(LF.ACT_PAD_BOT)
        maxRows := (forceOutlineRows) ? LF.OUTLINE_ROWS : Max(1, (sz.h - y - bottomFixed) // rowH)
        maxRows := Min(maxRows, LF.OUTLINE_ROWS)
        shown := 0
        loop C.outNum.Length {
            idx := A_Index
            row := (d.outlineRows.Length >= idx) ? d.outlineRows[idx] : 0
            vis := (row != 0 && idx <= maxRows)
            C.outNum[idx].Visible := vis
            C.outTxt[idx].Visible := vis
            if (!vis)
                continue
            ; v22 P2b（§9 顺手修）：高亮按 **passage_index**（row.p 是 0-based）匹配当前段，
            ; 旧实现拿显示槽位 idx 跟 G_LfSegIndex 比 → 当前段号 > 行数上限时永远错行。
            here := (row.p + 1 = d.outlineHere)
            C.outNum[idx].Text := row.n
            C.outTxt[idx].Text := row.t
            LfStyleCtl(C.outNum[idx], here ? pal.accent : pal.ink4, pal.panel)
            LfStyleCtl(C.outTxt[idx], here ? pal.accent : pal.outlineText, pal.panel)
            C.outNum[idx].Move(x0, y, LfPx(17), rowH)
            C.outTxt[idx].Move(x0 + LfPx(17) + LfPx(LF.PHON_GAP), y, innerW - LfPx(17) - LfPx(LF.PHON_GAP), rowH)
            y += rowH
            shown += 1
        }
        G_LfOutlineShown := shown
    }

    ; ---- 分隔线 + 按钮行（+14 / 线 / +14）----
    y += LfPx(LF.RULE_GAP)
    C.rule.Visible := true
    C.rule.Move(x0, y, innerW, Max(1, LfPx(1)))
    y += Max(1, LfPx(1)) + LfPx(LF.ACT_PAD_TOP)
    btnH := LfPx(LF.BTN_H)
    saveW := LfPx(78)
    C.save.Move(x0, y, saveW, btnH)
    bx := x0 + saveW + LfPx(4)
    btnRight := x0 + saveW
    ; v22 P2b（spec §9）：底栏「分段」入口（保存之后、重新识别之前）
    C.inspect.Move(bx, y, LfPx(66), btnH)
    btnRight := bx + LfPx(66)
    bx += LfPx(66) + LfPx(4)
    if (d.reocr) {
        C.reocr.Move(bx, y, LfPx(96), btnH)
        btnRight := bx + LfPx(96)
        bx += LfPx(96) + LfPx(4)
    }
    if (d.showQuit) {
        C.quit.Move(bx, y, LfPx(64), btnH)
        btnRight := bx + LfPx(64)
    }
    ; 底栏右侧提示：宽度按文字量，但**不许压到按钮**（#205：顺句次数标签比「全文 Alt ↓」
    ; 长，窄窗下会把「收工」盖住）。上限 = 最后一个可见按钮的右沿到内容右沿，留 8px 缝。
    hintMax := Max(LfPx(24), innerW - (btnRight - x0) - LfPx(8))
    hintW2 := Min(LfTextWidth(C.footHint, C.footHint.Text), hintMax)
    hintY := y + (btnH - LfPx(16)) // 2
    C.footHint.Move(x0 + innerW - hintW2, hintY, hintW2, LfPx(16))
    ; 同一份几何留给 Change 路径的 LfUpdateChainHint（轻量重摆，不跑整窗布局）。
    G_LfFootRow := { right: x0 + innerW, y: hintY, h: LfPx(16), maxW: hintMax }
    y += btnH + LfPx(LF.ACT_PAD_BOT)
    return y
}

; 两遍布局：先把内容按量出来的纸面高度排完，窗口比它高就把多出来的高度给纸面
; （用户拉大窗口 = 原句区变大，不是底下一片空白）。
LfFit() {
    global G_LfGui, G_LfNaturalH, G_LfClosing
    if (!G_LfGui || G_LfClosing)
        return 0
    natural := LfLayout(0)
    sz := LfClientSize(G_LfGui.Hwnd)
    if (sz.h > natural + 2)
        natural := LfLayout(sz.h - natural)
    G_LfNaturalH := natural
    return natural
}

LfOnSize(g, minMax, w, h) {
    global G_LfGui, G_LfClosing
    ; 销毁窗口的过程里也会收到 WM_SIZE；此时控件正在被销毁，再去 Move 它们就是
    ; "The control is destroyed"（2026-10-26 真机踩到：关闭面板时弹 AHK 错误框）。
    if (G_LfClosing || !G_LfGui || minMax = -1)
        return
    LfFit()
}

; ============================================================================
;  窗口显示 / 几何 / 跨屏 DPI
; ============================================================================
; config 里的几何是**逻辑 px**（换缩放/换机器位置仍然成立）；这里按窗口 DPI 换算。
LfGeomFromConfig() {
    global CONFIG_PATH, SECTION
    return { x: ReadIniValue(CONFIG_PATH, SECTION, "longform_x", "")
           , y: ReadIniValue(CONFIG_PATH, SECTION, "longform_y", "")
           , w: ReadIniValue(CONFIG_PATH, SECTION, "longform_w", "")
           , h: ReadIniValue(CONFIG_PATH, SECTION, "longform_h", "") }
}

; 启动时**只读一次**（主脚本 StartNormal 调；ReloadConfig 不调 —— 看文件头注释）。
LongformStartup() {
    global G_LfGeom, G_LfGeomLoaded
    G_LfGeom := LfGeomFromConfig()
    G_LfGeomLoaded := true
    ; E2E 钩子：**只在这个进程的环境变量 LF_E2E_ACTIONS 存在时**生效（副本/临时实例用）。
    ; 正常用户进程没有这个变量 → 这段等于不存在，入口/键位/界面全不受影响。
    if (EnvGet("LF_E2E_ACTIONS") != "")
        SetTimer(LfE2ERun, -3500)
    Log("长文面板：几何只在此读一次 x=" . G_LfGeom.x . " y=" . G_LfGeom.y
      . " w=" . G_LfGeom.w . " h=" . G_LfGeom.h . "（config 热重载不重读/不应用）")
}

; 面板显示：先隐藏建窗→按窗口 DPI 算尺寸→摆到记住的位置→再显示。
; 位置可能是负坐标（左/上副屏），尺寸全按 GetDpiForWindow 换算（不用全局 D）。
LfPresent(pos) {
    global G_LfGui, G_LfC, G_LfGeom, G_LfScale
    g := G_LfGui
    if (!pos)
        pos := LfDefaultPos()
    ; 几何：宽固定 428 逻辑（可被记住的宽覆盖）；高先给自然高度占位
    wantW := (G_LfGeom.w != "") ? (G_LfGeom.w + 0) : LF.PANEL_W
    wantH := (G_LfGeom.h != "") ? (G_LfGeom.h + 0) : 0
    w := LfPx(wantW)
    h := (wantH > 0) ? LfPx(wantH) : LfPx(360)
    g.Move(pos.x, pos.y, w, h)
    ; 移到目标位置后重新取一次窗口 DPI（可能已经落到另一块屏上）
    dpi2 := DllCall("GetDpiForWindow", "Ptr", g.Hwnd, "UInt")
    if (dpi2 != G_LfDpi) {
        LfApplyDpi(dpi2)
        w := LfPx(wantW)
        h := (wantH > 0) ? LfPx(wantH) : LfPx(360)
        g.Move(pos.x, pos.y, w, h)
    }
    LfApplyFonts()
    LfRender(G_LfData)
    natural := LfFit()
    h := Max(h, natural)          ; 记住的高度不够装内容 → 至少装得下
    sz := LfClientSize(g.Hwnd)
    if (sz.w != w || sz.h != h)
        g.Move(pos.x, pos.y, w, h)
    LfFit()
    LfApplyGeomCache(pos.x, pos.y, w, h)
    if (EnvGet("LF_E2E_ACTIONS") != "")
        g.Show("NoActivate")
    else
        g.Show()
    return { x: pos.x, y: pos.y, w: w, h: h }
}

; 第一次显示时窗口是隐藏的：Show() 之前的位置通过 move 生效。这里把坐标记进
; G_LfGeom 只是本进程的展示状态，**不落盘**（落盘只在拖动结束，LfSaveGeom）。
LfApplyGeomCache(x, y, w, h) {
    global G_LfGeom, G_LfScale
    G_LfGeom.x := "" . Round(x / G_LfScale)
    G_LfGeom.y := "" . Round(y / G_LfScale)
    G_LfGeom.w := "" . Round(w / G_LfScale)
    G_LfGeom.h := "" . Round(h / G_LfScale)
}

LfDefaultPos() {
    ; 默认放到主屏工作区左上偏一点（不挡正在读的正文右侧是用户自己拖的事）
    MonitorGetWorkArea(MonitorGetPrimary(), &l, &t, &r, &b)
    return { x: l + LfPx(80), y: t + LfPx(60) }
}

; 记住的几何 → 物理坐标。越界（显示器拔了/分辨率变了）就夹回主屏工作区。
LfRestorePos() {
    global G_LfGeom, G_LfScale
    if (G_LfGeom.x = "" || G_LfGeom.y = "")
        return LfDefaultPos()
    x := Round((G_LfGeom.x + 0) * G_LfScale)
    y := Round((G_LfGeom.y + 0) * G_LfScale)
    ; 可见性校正：落在任何显示器之外 → 回默认位（与旧框 BoxPosResolve 同一语义）
    onAny := false
    Loop MonitorGetCount() {
        MonitorGet(A_Index, &l, &t, &r, &b)
        if (x + LfPx(80) > l && x < r - LfPx(80) && y + LfPx(40) > t && y < b - LfPx(40))
            onAny := true
    }
    if (!onAny)
        return LfDefaultPos()
    return { x: x, y: y }
}

; 按新 DPI 重建字体缓存并重排（WM_DPICHANGED 的调用点）。
LfApplyDpi(dpi) {
    global G_LfDpi, G_LfScale, G_LfFontSpecs, G_LfCtlStyles
    G_LfDpi := dpi
    G_LfScale := dpi / 96
    LfDelFonts()
    LfApplyFonts()
}

LfApplyFonts() {
    global G_LfFontSpecs
    for hwnd, spec in G_LfFontSpecs {
        if (!hwnd)
            continue
        hf := LfFont(spec.px, spec.weight, spec.face)
        DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0030, "Ptr", hf, "Ptr", 1)
    }
}

; WM_DPICHANGED：系统在跨屏时发。wParam 低 16 位 = 新 DPI；lParam = 建议 RECT。
; 这里按 **wParam 的窗口 DPI** 重算缩放（不用全局 D），并按建议矩形重摆。
; lParam 为 0 的合成消息（自检用）只重算缩放，不改窗口矩形。
LfOnDpiChanged(wp, lp, msg, hwnd) {
    global G_LfGui
    if (!G_LfGui || hwnd != G_LfGui.Hwnd)
        return
    dpi := wp & 0xFFFF
    if (dpi < 48 || dpi > 960)
        return
    LfApplyDpi(dpi)
    if (lp) {
        r := { l: NumGet(lp, 0, "Int"), t: NumGet(lp, 4, "Int")
             , r: NumGet(lp, 8, "Int"), b: NumGet(lp, 12, "Int") }
        G_LfGui.Move(r.l, r.t, r.r - r.l, r.b - r.t)
    }
    LfFit()
    LfInvalidate()
    Log("长文面板：WM_DPICHANGED → dpi=" . dpi . " scale=" . G_LfScale)
    return 1        ; 自己处理了（别让默认 wndproc 再按旧矩形折腾）
}

; 拖动/缩放结束才落盘（WM_EXITSIZEMOVE）。**不在 WM_SIZE 里写**：拖动中每像素一次
; 文件重写会卡（研究 §7.2）。几何存**逻辑 px**（÷ 当前窗口 DPI），换缩放仍成立。
LfSaveGeom() {
    global G_LfGui, G_LfScale, G_LfGeom
    if (!G_LfGui)
        return
    r := LfWindowRect(G_LfGui.Hwnd)
    lx := Round(r.x / G_LfScale), ly := Round(r.y / G_LfScale)
    lw := Round(r.w / G_LfScale), lh := Round(r.h / G_LfScale)
    SaveConfigKeys(Map("longform_x", "" . lx, "longform_y", "" . ly
                     , "longform_w", "" . lw, "longform_h", "" . lh), "长文面板几何")
    G_LfGeom := { x: "" . lx, y: "" . ly, w: "" . lw, h: "" . lh }
    Log("长文面板：几何已记住 x=" . lx . " y=" . ly . " w=" . lw . " h=" . lh . "（逻辑 px）")
}

LfOnEnterSizeMove(wp, lp, msg, hwnd) {
    global G_LfGui, G_LfDrag
    if (!G_LfGui || hwnd != G_LfGui.Hwnd)
        return
    G_LfDrag.active := true
    r := LfWindowRect(hwnd)
    G_LfDrag.x := r.x, G_LfDrag.y := r.y, G_LfDrag.w := r.w, G_LfDrag.h := r.h
}

LfOnExitSizeMove(wp, lp, msg, hwnd) {
    global G_LfGui, G_LfDrag
    if (!G_LfGui || hwnd != G_LfGui.Hwnd)
        return
    was := G_LfDrag.active
    G_LfDrag.active := false
    r := LfWindowRect(hwnd)
    ; 用户真的拖过/缩放过才写盘：单击顶栏也会走这对消息，位置没变就不该写
    ; （与旧框 SaveBoxPos 同一条纪律）。合成消息（自检）没有 ENTERSIZEMOVE，照写。
    if (was && r.x = G_LfDrag.x && r.y = G_LfDrag.y && r.w = G_LfDrag.w && r.h = G_LfDrag.h) {
        Log("长文面板：拖动结束但位置尺寸未变，不写几何")
        return
    }
    LfSaveGeom()
}

; ---- 命中测试：顶栏 = HTCAPTION（可拖）；四边交给 DefWindowProc（WS_THICKFRAME 缩放）----
LfOnHitTest(wp, lp, msg, hwnd) {
    global G_LfGui
    if (!G_LfGui || hwnd != G_LfGui.Hwnd)
        return
    p := LfScreenPoint(lp)
    pt := Buffer(8, 0)
    NumPut("Int", p.x, pt, 0), NumPut("Int", p.y, pt, 4)
    DllCall("ScreenToClient", "Ptr", hwnd, "Ptr", pt)
    x := NumGet(pt, 0, "Int"), y := NumGet(pt, 4, "Int")
    sz := LfClientSize(hwnd)
    edge := Max(5, LfPx(5))
    if (x < edge || y < edge || x >= sz.w - edge || y >= sz.h - edge)
        return                          ; 交回默认：WS_THICKFRAME 自己回 HTLEFT/HTTOP...
    if (y < LfPx(LF.HEAD_PAD_TOP + LF.HEAD_ROW))
        return 2                        ; HTCAPTION：顶栏整条可拖
    return 1                            ; HTCLIENT
}

LfOnGetMinMaxInfo(wp, lp, msg, hwnd) {
    global G_LfGui
    if (!G_LfGui || hwnd != G_LfGui.Hwnd)
        return
    ; MINMAXINFO: ptMinTrackSize 在偏移 24（x64：POINT 8 字节）
    NumPut("Int", LfPx(LF.MIN_W), lp, 24)
    NumPut("Int", LfPx(LF.MIN_H), lp, 28)
}

; ============================================================================
;  焦点 / 空态光标 / 关闭
; ============================================================================
LfFocusWord() {
    global G_LfC
    if (!G_LfC.HasOwnProp("word"))
        return
    G_LfC.word.Focus()
    SendMessage(0x00B1, 0x7FFFFFFF, 0x7FFFFFFF, , G_LfC.word.Hwnd)   ; 光标到末尾
}

LfWordFocus(focused) {
    global G_LfC, G_LfGui, G_LfClosing
    ; 关窗时控件先销毁、事件后到（本机 demo open 路径踩到过 "The control is destroyed"）
    if (G_LfClosing || !G_LfGui || !G_LfC.HasOwnProp("word"))
        return
    empty := (NormalizeLine(G_LfC.word.Value) = "")
    ; 聚焦时用系统原生闪烁光标（真控件），未聚焦时才显示设计稿的强调色光标+占位
    G_LfC.wordPh.Visible := empty && !focused
    G_LfC.caret.Visible := empty && !focused
}

LfBlinkTick() {
    global G_LfC, G_LfCaretOn, G_LfBlinkN, G_LfGui, G_LfClosing
    ; 同上：Destroy() 期间定时器仍可能被投递一次，此时 G_LfC 里是已销毁的控件
    if (G_LfClosing || !G_LfGui || !G_LfC.HasOwnProp("caret"))
        return
    ; 只在"空态且词框**未**聚焦"时闪：聚焦时是系统原生光标在闪，不能同时有第二个光标
    if (NormalizeLine(G_LfC.word.Value) != "") {
        G_LfC.caret.Visible := false
        return
    }
    if (DllCall("GetFocus", "Ptr") = G_LfC.word.Hwnd) {
        G_LfC.caret.Visible := false
        return
    }
    G_LfCaretOn := !G_LfCaretOn
    G_LfBlinkN += 1
    G_LfC.caret.Visible := G_LfCaretOn
}

LfOnSaveClick() {
    ; 蓝色「保存」= 保存并继续（与旧框 Enter 同语义：落盘后清词、原句保留）
    LfSave("next")
}

LfOnReocrClick() {
    Log("长文面板：重新识别按钮（本阶段只有 UI，T6 接整页重识别）")
}

; 面板内键（Esc 收工）。只有**正常打开**时挂；demo 不注册任何热键。
LfRegisterKeys() {
    global G_LfGui, G_LfKeysOn
    if (G_LfKeysOn || !G_LfGui)
        return
    HotIfWinActive("ahk_id " . G_LfGui.Hwnd)
    try {
        Hotkey("Escape", (*) => LfClose(), "On")
        Hotkey("Enter", (*) => LfSave("next"), "On")     ; 保存并继续（框不关）
        Hotkey("Tab", (*) => LfCompTab(), "On")          ; 句内补全/顺句接词（复用 lib 纯函数）
        Hotkey("^Tab", (*) => LfCompCtrlTab(), "On")     ; 强制接词（main 的 Ctrl+Tab 手感）
        Hotkey("^v", (*) => LfPasteFromClipboard(), "On") ; T6：粘贴成文（manual 档）
        Hotkey("!Down", (*) => LfToggleOutline(), "On")   ; T6：全文展开/收起（outline 档）
        ; v22 P2b（spec §9）：分段预览窗入口（避开 Esc/Enter/Tab/^Tab/^v/!↓ 的既有键）
        Hotkey("!+p", (*) => LfInspectorToggle(), "On")   ; Alt+Shift+P
        G_LfKeysOn := true
    } catch as err {
        Log("长文面板：面板内键挂不上（不影响 ✕ 关闭）：" . err.Message)
    }
    HotIf()
}

LfUnregisterKeys() {
    global G_LfKeysOn, G_LfGui
    if (!G_LfKeysOn)
        return
    ; 票 #185（A12）实测（AHK v2.0.30）：Off 必须在**同一条 HotIf 上下文**里做 ——
    ; 不带上下文的 `Hotkey(name, "Off")` 报 "Nonexistent hotkey variant (HotIf)"，
    ; 旧的裸调写法被 try 吞掉＝变体没摘（函数名不副实）。调用点在 LfDestroyWindow
    ; 里 G_LfGui.Destroy() 之前，HWND 仍有值，上下文可用。
    if (G_LfGui) {
        HotIfWinActive("ahk_id " . G_LfGui.Hwnd)
        try Hotkey("Escape", "Off")
        try Hotkey("Enter", "Off")
        try Hotkey("Tab", "Off")
        try Hotkey("^Tab", "Off")
        try Hotkey("^v", "Off")
        try Hotkey("!Down", "Off")
        try Hotkey("!+p", "Off")
        HotIf()
    }
    G_LfKeysOn := false
}

LfDestroyWindow() {
    global G_LfGui, G_LfClosing
    LfUnregisterKeys()
    G_LfClosing := true
    if (G_LfGui) {
        try G_LfGui.Destroy()
        G_LfGui := 0
    }
    SetTimer(LfBlinkTick, 0)
    ; v21 round2 (B)：面板关了就不再刷新「最近非面板前台」缓存
    SetTimer(PanelProvCacheTick, 0)
    ; v22 P2b：面板收工 → 分段预览窗一起收（避免留下无主窗口；几何在这里落盘）
    LfInspectorShutdown()
    G_LfClosing := false
}

LfClose() {
    global G_LfGui, G_LfC, G_LfClosing, G_LfState
    if (G_LfClosing)
        return
    G_LfClosing := true
    LfDestroyWindow()
    LfDelFonts()
    LfDelBrushes()
    G_LfC := {}
    G_LfState := ""
    G_LfClosing := false
    Log("长文面板已关闭")
}

; ============================================================================
;  两条入口热键（本阶段只负责打开面板；OCR/粘贴链路是 T6）
; ============================================================================
LongformHotkeyOcr() {
    ; v21 T5 阶段2：真实链路 —— 遮罩拖大框 → page_ocr → page_text → article_create
    ; → page_drop → 面板（核心实现在本文件的 LongformCaptureStart）
    Log("热键：长文整页识别（^!4）")
    LongformCaptureStart()
}

LongformHotkeyPaste() {
    Log("热键：长文粘贴成文（^!5）")
    ; v21 round2 (B)：出处必须在 LfOpen **之前**采集 —— 面板一出现，前台就是面板
    ; 自己（`CaptureProvenance` 会抓到我们的窗口）。这条与 OCR 入口同一纪律。
    PanelFillLongProvenance()
    if (!G_LfGui) {
        LfOpen("empty")
    } else {
        WinActivate("ahk_id " . G_LfGui.Hwnd)
    }
    ; 打开时剪贴板已有文本 → 直接预填（T6 票面）
    txt := ""
    try txt := A_Clipboard
    if (Trim(txt) = "") {
        Log("长文粘贴：剪贴板为空，保持空面板（等 Ctrl+V）")
        return
    }
    LfPasteText(txt)
}

; 正常打开（用户按热键）：空态、焦点在词框、按记住的几何摆位、挂 Esc。
LfOpen(state := "empty") {
    global G_LfGui, G_LfC, G_LfData, G_LfDemo
    if (G_LfGui) {
        ; 已开着 → 聚焦（不重建，避免丢用户输入）
        WinActivate("ahk_id " . G_LfGui.Hwnd)
        LfFocusWord()
        return
    }
    G_LfDemo := ""
    ; 真实数据：已建过文章 → 显示那篇；否则空面板提示按 Ctrl+Alt+4（demo 不走这里）
    data := LfRealData()
    data.dark := G_LfDarkState()          ; 跟随旧框同一主题开关（theme=dark/light）
    LfBuild(data)
    pos := LfRestorePos()
    LfPresent(pos)
    LfRegisterKeys()
    LfFocusWord()
    Log("长文面板已打开（state=" . state . "，独立窗口，未接内核/中枢数据）")
}

G_LfDarkState() {
    global G_Dark
    return G_Dark ? true : false
}

; 票 #185（A8）：主题切换时已打开的长文面板按新主题重建（旧框 ToggleDark → Rebuild
; 的先例）。重建走 LfBuild（自拆旧窗、幂等）；三条恢复与 LfOpen 同口径：
; 几何（LfPresent ← LfRestorePos / G_LfGeom 缓存）、键位（LfDestroyWindow 摘了要挂
; 回）、在途输入（LfWriteWord 不触发 Change，所以词框当前值在拆窗前带走、经
; data.word 让首帧装载写回 —— G_LfTarget.word 只在 Change 里同步，重建绕过它）。
; 隐藏中的面板不重建 —— 重建会把它弹出来（与旧框 Rebuild 同一条守卫）。
; 分段预览窗是面板的附属窗（LfDestroyWindow 会一起收）：重建前开着就重开一次。
LfRebuildForTheme() {
    global G_LfGui, G_LfData, G_LfC, G_LfInspGui
    if (!G_LfGui || !DllCall("IsWindowVisible", "Ptr", G_LfGui.Hwnd))
        return
    word := ""
    try word := G_LfC.word.Value            ; 在途输入（未到 Change 的也算）
    inspWasOpen := (G_LfInspGui != 0)
    G_LfData.word := word
    G_LfData.dark := G_LfDarkState()        ; 新主题 → 调色板 + DWM 深色属性
    LfBuild(G_LfData)
    LfPresent(LfRestorePos())
    LfRegisterKeys()
    if (inspWasOpen)
        LfInspectorOpen()                   ; 附属预览窗跟着回来（数据重拉一次）
    Log("长文面板：主题切换，已按新主题重建")
}

; ============================================================================
;  自绘按钮（保存蓝色实心 / 收工纯文字 / 重新识别 / ✕）
;  AHK 原生按钮在 Windows 上恒为浅底、不能改底色（旧框的实测结论），要设计稿
;  的"蓝色实心 + 键位等宽小字 55% 透明"只能自绘：BS_OWNERDRAW + WM_DRAWITEM。
; ============================================================================
; 入口按钮的可用态跟着 G_LongDocId 走（建窗 / 换文章 / 复位三处调用）：
; 无 doc_id → Enabled=false（点击不会进回调）+ 自绘灰态（LfOnDrawItem）。
LfSyncInspectButton() {
    global G_LfC, G_LongDocId
    if (!IsObject(G_LfC) || !G_LfC.HasProp("inspect"))
        return
    ctl := G_LfC.inspect
    ctl.Enabled := G_LongDocId ? true : false
    try DllCall("InvalidateRect", "Ptr", ctl.Hwnd, "Ptr", 0, "Int", 1)
}

LfOnDrawItem(wp, lp, msg, hwnd) {
    global G_LfGui, G_LfPal, G_LfBtnKind, G_LfClosing, G_LongDocId
    if (G_LfClosing || !G_LfGui || hwnd != G_LfGui.Hwnd)
        return
    hItem := NumGet(lp, 24, "Ptr")
    if (!G_LfBtnKind.Has(hItem))
        return
    kind := G_LfBtnKind[hItem]
    hdc := NumGet(lp, 32, "Ptr")
    l := NumGet(lp, 40, "Int"), t := NumGet(lp, 44, "Int")
    r := NumGet(lp, 48, "Int"), b := NumGet(lp, 52, "Int")
    w := r - l, h := b - t
    if (w < 1 || h < 1)
        return 1
    state := NumGet(lp, 16, "UInt")
    pressed := (state & 0x0001)         ; ODS_SELECTED
    pal := G_LfPal
    ; 各按钮的底/字/键位（设计稿 §9.1：保存蓝实心、收工纯文字、软按钮灰底）
    bg := pal.panel, fg := pal.ink2, kbd := ""
    label := "", kbdLabel := ""
    radius := LfPx(LF.BTN_RADIUS)
    switch kind {
        case "primary":
            bg := pressed ? LfMix("000000", pal.accent, 0.10) : pal.accent
            fg := pal.btnText, kbd := pal.kbdOnBtn
            label := "保存", kbdLabel := "↵"
        case "plain":
            bg := pal.panel
            fg := pal.ink3, kbd := pal.kbdPlain
            label := "收工", kbdLabel := "Esc"
            radius := 0
        case "soft":
            bg := pressed ? LfMix("000000", pal.softBtn, 0.06) : pal.softBtn
            fg := pal.ink2
            label := "重新识别"
            radius := LfPx(LF.BTN_RADIUS)
        case "inspect":
            ; v22 P2b（spec §9「仅在有 doc_id 时可用」）：没有 doc_id 时走**禁用视觉态**
            ; （灰字 + 底色往面板底收），并配合 LfSyncInspectButton() 把 Enabled 置 false
            ; —— 不是只靠点击后再 toast 短路（review 指出那样按钮看起来仍可点）。
            avail := G_LongDocId ? true : false
            bg := !avail ? LfMix(pal.panel, pal.softBtn, 0.6)
                        : (pressed ? LfMix("000000", pal.softBtn, 0.06) : pal.softBtn)
            fg := avail ? pal.ink2 : pal.ink4
            kbd := avail ? pal.kbdPlain : pal.ink4
            label := "分段", kbdLabel := "!⇧P"
            radius := LfPx(LF.BTN_RADIUS)
        case "close":
            bg := pal.panel, fg := pal.ink4
            label := "✕"
            radius := 0
    }
    ; 底色：**必须每次都铺**。BS_OWNERDRAW 的按钮在 WM_DRAWITEM 之前已由默认窗口
    ; 过程擦成系统按钮灰脸，不铺面板色就露出一块灰（本机首版截图实测踩到）。
    br := DllCall("gdi32\CreateSolidBrush", "UInt", LfRef(bg), "Ptr")
    pen := DllCall("gdi32\CreatePen", "Int", 0, "Int", 1, "UInt", LfRef(bg), "Ptr")
    ob := DllCall("SelectObject", "Ptr", hdc, "Ptr", br, "Ptr")
    op := DllCall("SelectObject", "Ptr", hdc, "Ptr", pen, "Ptr")
    if (radius > 0)
        DllCall("gdi32\RoundRect", "Ptr", hdc, "Int", 0, "Int", 0, "Int", w + 1, "Int", h + 1, "Int", radius, "Int", radius)
    else
        DllCall("gdi32\Rectangle", "Ptr", hdc, "Int", 0, "Int", 0, "Int", w + 1, "Int", h + 1)
    DllCall("SelectObject", "Ptr", hdc, "Ptr", ob, "Ptr")
    DllCall("SelectObject", "Ptr", hdc, "Ptr", op, "Ptr")
    DllCall("DeleteObject", "Ptr", br)
    DllCall("DeleteObject", "Ptr", pen)
    ; 文字：label + 等宽键位（55% 透明 = 预先混到底色上的实色）
    hfMain := LfFont(13, 500, "Segoe UI")
    hfKbd := LfFont(10, 400, "Cascadia Mono")
    lblW := LfHdcTextW(hdc, hfMain, label)
    kbdW := (kbdLabel != "") ? LfHdcTextW(hdc, hfKbd, kbdLabel) : 0
    gap := (kbdLabel != "") ? LfPx(7) : 0
    total := lblW + gap + kbdW
    startX := Max(0, (w - total) // 2)
    LfHdcText(hdc, hfMain, label, startX, 0, w - startX, h, fg)
    if (kbdLabel != "")
        LfHdcText(hdc, hfKbd, kbdLabel, startX + lblW + gap, 0, w, h, kbd)
    return 1
}

LfHdcTextW(hdc, hf, text) {
    old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hf, "Ptr")
    ext := Buffer(8, 0)
    DllCall("GetTextExtentPoint32W", "Ptr", hdc, "Str", text, "Int", StrLen(text), "Ptr", ext)
    ww := NumGet(ext, 0, "Int")
    DllCall("SelectObject", "Ptr", hdc, "Ptr", old, "Ptr")
    return ww
}

LfHdcText(hdc, hf, text, x, y, w, h, hex) {
    old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hf, "Ptr")
    DllCall("gdi32\SetBkMode", "Ptr", hdc, "Int", 1)                  ; TRANSPARENT
    DllCall("gdi32\SetTextColor", "Ptr", hdc, "UInt", LfRef(hex))
    r := Buffer(16, 0)
    NumPut("Int", x, r, 0), NumPut("Int", y, r, 4), NumPut("Int", w, r, 8), NumPut("Int", h, r, 12)
    DllCall("user32\DrawTextW", "Ptr", hdc, "Str", text, "Int", -1, "Ptr", r
        , "UInt", 0x0004 | 0x0020 | 0x0000)   ; DT_VCENTER|DT_SINGLELINE|DT_LEFT
    DllCall("SelectObject", "Ptr", hdc, "Ptr", old, "Ptr")
}

; 消息处理只在模块级挂**一次**（与旧框同一条纪律：重建窗口不重复挂，避免累积旧 HWND）
OnMessage(0x0084, LfOnHitTest)          ; WM_NCHITTEST（拖顶栏 / 缩放边）
OnMessage(0x0231, LfOnEnterSizeMove)    ; WM_ENTERSIZEMOVE
OnMessage(0x0232, LfOnExitSizeMove)     ; WM_EXITSIZEMOVE（几何落盘点）
OnMessage(0x02E0, LfOnDpiChanged)       ; WM_DPICHANGED（跨屏 DPI）
OnMessage(0x002B, LfOnDrawItem)         ; WM_DRAWITEM（自绘按钮）
OnMessage(0x0024, LfOnGetMinMaxInfo)    ; WM_GETMINMAXINFO（最小尺寸）
OnMessage(0x0138, LfOnCtlColor)         ; WM_CTLCOLORSTATIC
OnMessage(0x0133, LfOnCtlColor)         ; WM_CTLCOLOREDIT
OnMessage(0x0132, LfOnCtlColor)         ; WM_CTLCOLORBTN

; ============================================================================
;  demo / 自检（`--lf-demo=<state>`）
;
;  demo 的三条纪律：
;   1. 不注册任何热键、不拉内核/中枢、不碰 spool（本函数是唯一入口，StartNormal 不在路径上）
;   2. 数据全部来自内置示例（LfDemoData）；关闭开关时对正常启动路径零影响
;   3. 自检一律写**数字**到结果文件：窗口样式位、窗口 DPI、高亮前后像素差、
;      几何落盘再恢复的位置。截图用屏幕 BitBlt，不看"应该没问题"
; ============================================================================
LongformDemoArg(args) {
    for a in args {
        if (SubStr(a, 1, 10) = "--lf-demo=")
            return SubStr(a, 11)
    }
    return ""
}

LfArgValue(args, prefix) {
    n := StrLen(prefix)
    for a in args {
        if (SubStr(a, 1, n) = prefix)
            return SubStr(a, n + 1)
    }
    return ""
}

LongformDemoRun(state, args) {
    global G_LfDemo, CONFIG_PATH, G_LfGeom, G_LfOut, G_LfBackOff, G_LfC, G_LfCaretOn, G_LfBlinkN
    global G_LfGui, G_LfKeysOn, G_LfState, G_LfResultPath
    shot := LfArgValue(args, "--lf-shot=")
    res := LfArgValue(args, "--lf-result=")
    cfg := LfArgValue(args, "--lf-cfg=")
    expect := LfArgValue(args, "--lf-expect=")
    G_LfDemo := state
    G_LfResultPath := res
    OnError(LfDemoOnError)
    ; 几何证据用**临时 config**（拷一份工作区 config 当底），不污染 config.ini。
    if (cfg != "") {
        if (!FileExist(cfg)) {
            SplitPath(cfg, , &dir)
            if (dir != "" && !DirExist(dir))
                DirCreate(dir)
            try FileCopy(CONFIG_PATH, cfg)
        }
        CONFIG_PATH := cfg
    }
    LfEnsureRich()
    LfGeomInit()
    ok := true
    fail := ""
    try {
        if (state = "open") {
            ; 走**正常打开路径**（LfOpen：按记住的几何摆位 + 挂面板内 Esc + 聚焦词框），
            ; 证明真实启动路径没被 demo 分支影响。demo 本身仍不注册 ^!4/^!5 两条全局键。
            LfOpen("empty")
            Sleep(260)
            foc := DllCall("GetFocus", "Ptr")
            LfSay("state=open")
            LfSay("panel_open=" . (G_LfGui ? 1 : 0))
            LfSay("esc_key_on=" . (G_LfKeysOn ? 1 : 0))
            LfSay("word_focused=" . ((G_LfC.HasOwnProp("word") && foc = G_LfC.word.Hwnd) ? 1 : 0))
            LfSay("render_state=" . G_LfState)
            ok := ok && (G_LfGui != 0) && G_LfKeysOn && (G_LfState = "empty")
            LfClose()
            LfSay("closed=" . (G_LfGui ? 0 : 1))
            ok := ok && (G_LfGui = 0)
            LfSay("RESULT=" . (ok ? "PASS" : "FAIL"))
            LfWriteResult(res)
            ExitApp ok ? 0 : 1
        }
        if (state = "geom-restore") {
            ; 第二个进程：从落盘的几何**恢复**（真正跨进程的重启恢复证据）
            data := LfDemoData("hit")
            LfBuild(data)
            pos := LfRestorePos()
            size := LfPresent(pos)
            Sleep(250)
            r := LfWindowRect(G_LfGui.Hwnd)
            LfSay("state=geom-restore")
            LfSay("restored_x=" . r.x . " restored_y=" . r.y . " restored_w=" . r.w . " restored_h=" . r.h)
            LfSay("geom=" . G_LfGeom.x . "," . G_LfGeom.y . "," . G_LfGeom.w . "," . G_LfGeom.h)
            if (expect != "") {
                parts := StrSplit(expect, ",")
                ; 允许 2px 误差：几何存的是**逻辑 px**，跨进程恢复要 ÷scale 再 ×scale，
                ; 4 个值各自四舍五入后可能差 1（本机 150%：Round(246/1.5)*1.5=246 恰好相等，
                ; 但换 125% 之类的缩放就会有 ±1）。差 1px 不叫"位置丢了"。
                same := (Abs(r.x - (parts[1] + 0)) <= 2 && Abs(r.y - (parts[2] + 0)) <= 2
                      && Abs(r.w - (parts[3] + 0)) <= 2 && Abs(r.h - (parts[4] + 0)) <= 2)
                LfSay("expect=" . expect)
                LfSay("POS_RESTORE=" . (same ? "PASS" : "FAIL"))
                ok := ok && same
            }
            LfSay("RESULT=" . (ok ? "PASS" : "FAIL"))
            LfDestroyWindow()
        } else {
            data := LfDemoData(state)
            LfBuild(data)
            pos := LfDemoPos()
            size := LfPresent(pos)
            WinActivate("ahk_id " . G_LfGui.Hwnd)
            Sleep(420)                  ; 等首帧 + DWM 合成（截图必须抓的是最终形态）
            ; ---- 自检数字 ----
            LfSay("state=" . state)
            hw := G_LfGui.Hwnd
            style := DllCall("GetWindowLongW", "Ptr", hw, "Int", -16, "Int")
            ex := DllCall("GetWindowLongW", "Ptr", hw, "Int", -20, "Int")
            LfSay("style=0x" . Format("{:08X}", style & 0xFFFFFFFF))
            LfSay("ws_caption=" . ((style & 0x00C00000) ? 1 : 0))
            LfSay("ws_thickframe=" . ((style & 0x00040000) ? 1 : 0))
            LfSay("exstyle=0x" . Format("{:08X}", ex & 0xFFFFFFFF))
            LfSay("ex_toolwindow=" . ((ex & 0x80) ? 1 : 0))
            LfSay("ex_topmost=" . ((ex & 0x08) ? 1 : 0))
            cv := Buffer(4, 0)
            hr := DllCall("dwmapi\DwmGetWindowAttribute", "Ptr", hw, "Int", 33, "Ptr", cv, "Int", 4)
            corner := (hr = 0) ? NumGet(cv, 0, "Int") : -1
            LfSay("dwm_corner=" . corner)
            LfSay("dpi_window=" . DllCall("GetDpiForWindow", "Ptr", hw, "UInt"))
            LfSay("dpi_screen=" . A_ScreenDPI)
            LfSay("scale=" . Format("{:.3f}", G_LfScale))
            r := LfWindowRect(hw)
            LfSay("window_rect=" . r.x . "," . r.y . "," . r.w . "," . r.h)
            LfSay("window_logical_w=" . Round(r.w / G_LfScale))
            ; ---- 目标词行自检（第二轮修复 Bug 2b/2c 的证据源）----
            ; 「字体/行高设置成功」不等于 g/y 的下伸部真的画出来了：数词框里的墨迹
            ; 最下沿 + 基线以下那条带的像素。chip 宽度按文本量（截图里不许再是 phra…）。
            if (data.HasOwnProp("word") && data.word != "") {
                wimg := LfGrabCtl(G_LfC.word)
                wp := LfInkProfile(wimg, 480, data.dark)
                ; 词框里的**墨迹**（不是"字体设置成功"）：文字真的画出来了吗？
                ; lowest = 最下一条有字的行；bottom_band = 基线以下那条带里的像素
                ; （g/y/j/p/q 的下伸部就在这里；"researchers" 这类无下伸部的词应为 0）。
                LfSay("word_value=" . G_LfC.word.Value)
                LfSay("word_ctl_w=" . wimg.w)
                LfSay("word_ctl_h=" . wimg.h)
                LfSay("word_ink_total=" . wp.total)
                LfSay("word_ink_lowest_row=" . wp.lowest)
                LfSay("word_ink_bottom_band="
                    . LfCountInkBand(wimg, Round(wimg.h * 0.62), wimg.h, 480, data.dark))
                LfSay("chip_text=" . G_LfC.kind.Text)
                LfSay("chip_text_w=" . LfTextWidth(G_LfC.kind, G_LfC.kind.Text))
                LfSay("word_ink_dark_theme=" . (data.dark ? 1 : 0))
            }
            ; RichEdit：类名 + 只读样式位（必须是创建样式给的）
            rstyle := DllCall("GetWindowLongW", "Ptr", G_LfC.sent.Hwnd, "Int", -16, "Int")
            buf := Buffer(256, 0)
            DllCall("GetClassNameW", "Ptr", G_LfC.sent.Hwnd, "Ptr", buf, "Int", 128)
            LfSay("richedit_class=" . StrGet(buf, 128, "UTF-16"))
            LfSay("richedit_readonly_bit=" . ((rstyle & 0x800) ? 1 : 0))
            LfSay("rich_back_color_offset=" . G_LfBackOff)
            ; 高亮像素自检：先只放正文（不上高亮），抓图；再上真实高亮，抓图；比像素
            if (LfHasMarks(data)) {
                plainRuns := [{ text: data.paperText, mark: "" }]
                fg := (data.paperFg = "hint") ? G_LfPal.paperHint : G_LfPal.sentText
                LfRichApply(plainRuns, { fg: fg, bg: G_LfPal.paper, italic: data.italic })
                Sleep(220)
                before := LfGrabCtl(G_LfC.sent)
                LfRichApply(data.runs, { fg: fg, bg: G_LfPal.paper, italic: data.italic })
                Sleep(260)
                after := LfGrabCtl(G_LfC.sent)
                diff := LfDiffPx(before, after)
                markHex := G_LfPal.markCur
                markPx := LfCountColor(after, Integer("0x" . markHex), 10)
                LfSay("highlight_diff_px=" . diff)
                LfSay("highlight_marker_px=" . markPx)
                LfSay("highlight_marker_color=" . markHex)
                ok := ok && (diff > 200) && (markPx > 100)
                if (!(diff > 200 && markPx > 100))
                    fail .= " 高亮像素不足"
            }
            ; 空态：强调色光标闪烁。证据两件：
            ;   ① SetTimer 真的在翻相位（记翻转次数，不是"应该会闪"）；
            ;   ② 亮/灭两帧的像素差 > 0（真的能看见）。
            if (state = "empty") {
                n0 := G_LfBlinkN
                Sleep(1300)                       ; 550ms 一拍 → 1.3s 内至少翻 2 次
                flips := G_LfBlinkN - n0
                LfSay("caret_timer_flips=" . flips)
                f1 := 0, f2 := 0
                loop 25 {
                    if (G_LfC.caret.Visible) {
                        f1 := LfGrabCtl(G_LfC.caret)
                        break
                    }
                    Sleep(60)
                }
                loop 25 {
                    if (!G_LfC.caret.Visible) {
                        f2 := LfGrabCtl(G_LfC.caret)
                        break
                    }
                    Sleep(60)
                }
                bd := (IsObject(f1) && IsObject(f2)) ? LfDiffPx(f1, f2) : -1
                LfSay("caret_blink_diff_px=" . bd)
                LfSay("caret_visible_now=" . (G_LfC.caret.Visible ? 1 : 0))
                ok := ok && (flips >= 2) && (bd > 0)
                ; 截图前把光标点亮（否则可能拍到熄灭相位）
                G_LfCaretOn := true
                G_LfC.caret.Visible := true
                Sleep(140)
            }
            ; WM_DPICHANGED 代码路径（本机只有一块屏，用合成的 dpi 值走一遍并还原）：
            ; wParam 低位 = 新 DPI；lParam 为 0（不改矩形）→ 只重算缩放 + 重排。
            ; WM_DPICHANGED 是 send-only 消息（PostMessage 返回 1159 ERROR_MESSAGE_SYNC_ONLY，
            ; 实测），同线程 SendMessage 又绕过 OnMessage 的队列监视。所以这里**直接调用**
            ; 处理函数（body 与 OnMessage 回调完全同一份），证明"按窗口 DPI 重算"这条路真的走通。
            dpi0 := G_LfDpi
            LfOnDpiChanged(0x78 | (0x78 << 16), 0, 0x02E0, hw)      ; 120 = 125%
            Sleep(200)
            LfSay("dpi_path_synthetic=120 scale=" . Format("{:.3f}", G_LfScale))
            dpiOk := (G_LfScale > 1.24 && G_LfScale < 1.26)
            LfOnDpiChanged(dpi0 | (dpi0 << 16), 0, 0x02E0, hw)       ; 还原
            Sleep(200)
            LfSay("dpi_path_restored=" . G_LfDpi . " scale=" . Format("{:.3f}", G_LfScale))
            ok := ok && dpiOk && (G_LfDpi = dpi0)
            if (!dpiOk)
                fail .= " WM_DPICHANGED 路径未按窗口 DPI 重算"
            ; ---- 截图（窗口矩形 + margin：看得到 DWM 圆角/投影）----
            if (shot != "") {
                rr := LfWindowRect(hw)
                m := LfPx(LF.SHOT_MARGIN)
                WinActivate("ahk_id " . hw)
                Sleep(360)
                shotOk := LfShotRect(shot, rr.x - m, rr.y - m, rr.w + 2 * m, rr.h + 2 * m)
                LfSay("screenshot=" . shot)
                LfSay("screenshot_ok=" . (shotOk ? 1 : 0))
                ok := ok && shotOk
                if (!shotOk)
                    fail .= " 截图失败"
            }
            ; ---- 几何：拖动 → WM_EXITSIZEMOVE 落盘 → 重建 → 恢复位置 ----
            if (cfg != "") {
                r := LfWindowRect(hw)
                ; 同时验证「可调大小」：移动 + 改尺寸（+40 / +32 逻辑 px）后落盘
                newX := r.x + LfPx(64), newY := r.y + LfPx(48)
                newW := r.w + LfPx(40), newH := r.h + LfPx(32)
                G_LfGui.Move(newX, newY, newW, newH)
                Sleep(200)
                ; WM_EXITSIZEMOVE 走**投递**（经消息队列 → 模块级处理真的被调到）
                DllCall("PostMessageW", "Ptr", hw, "UInt", 0x0232, "Ptr", 0, "Ptr", 0)
                Sleep(320)
                saved := LfGeomFromConfig()
                LfSay("geom_saved_logical=" . saved.x . "," . saved.y . "," . saved.w . "," . saved.h)
                LfSay("geom_saved_physical_expected=" . newX . "," . newY . "," . newW . "," . newH)
                saveOk := (saved.x != "" && saved.y != "" && saved.w != "" && saved.h != "")
                ok := ok && saveOk
                if (!saveOk)
                    fail .= " 几何没落盘"
                ; 重建（等价启动恢复路径）再读回位置
                LfDestroyWindow()
                data2 := LfDemoData(state)
                LfBuild(data2)
                pos2 := LfRestorePos()
                LfPresent(pos2)
                Sleep(220)
                r2 := LfWindowRect(G_LfGui.Hwnd)
                LfSay("geom_rebuilt_physical=" . r2.x . "," . r2.y . "," . r2.w . "," . r2.h)
                roundtrip := (Abs(r2.x - newX) <= 2 && Abs(r2.y - newY) <= 2 && Abs(r2.w - newW) <= 2 && Abs(r2.h - newH) <= 2)
                LfSay("GEOM_ROUNDTRIP=" . (roundtrip ? "PASS" : "FAIL"))
                ok := ok && roundtrip
                if (!roundtrip)
                    fail .= " 几何恢复不一致"
            }
            LfSay("fail=" . (fail = "" ? "none" : Trim(fail)))
            LfSay("RESULT=" . (ok ? "PASS" : "FAIL"))
            LfDestroyWindow()
        }
    } catch as err {
        LfSay("EXCEPTION=" . err.Message . " @ line " . err.Line)
        LfSay("RESULT=FAIL")
        ok := false
        try LfDestroyWindow()
    }
    LfWriteResult(res)
    ExitApp ok ? 0 : 1
}

; demo 的固定落点（物理 px）：主屏工作区左上 + 固定偏移，配合截图证据。
LfDemoPos() {
    MonitorGetWorkArea(MonitorGetPrimary(), &l, &t, &r, &b)
    return { x: l + 150, y: t + 90 }
}

LfGeomInit() {
    global G_LfGeom, G_LfGeomLoaded
    G_LfGeom := LfGeomFromConfig()
    G_LfGeomLoaded := true
}

LfHasMarks(data) {
    for run in data.runs
        if (run.mark != "")
            return true
    return false
}

LfGrabCtl(ctl) {
    r := LfWindowRect(ctl.Hwnd)
    return LfGrabRect(r.x, r.y, r.w, r.h)
}

LfSay(s) {
    global G_LfOut
    G_LfOut.Push(s)
}

; demo 跑批的错误处理：AHK 的运行时错误默认弹**阻塞式**对话框（/ErrorStdOut 只保证
; 语法错误走 stdout，本机实测运行时错误仍会弹），批处理脚本会就此卡死。所以 demo
; 一进来就装 OnError：把异常写进结果文件、退出码 1 —— 与 tests/README 里的纪律一致。
LfDemoOnError(err, mode) {
    global G_LfResultPath, G_LfOut
    LfSay("EXCEPTION=" . err.Message . " @ line " . err.Line . " (mode=" . mode . ")")
    LfSay("RESULT=FAIL")
    LfWriteResult(G_LfResultPath)
    ExitApp 1
    return true
}

LfWriteResult(path) {
    global G_LfOut
    if (path = "")
        return
    try {
        SplitPath(path, , &dir)
        if (dir != "" && !DirExist(dir))
            DirCreate(dir)
        try FileDelete(path)
        f := FileOpen(path, "w", "UTF-8")
        for l in G_LfOut
            f.WriteLine(l)
        f.Close()
    }
}





















; ============================================================================
;  v21 T5 **阶段 2**：真实链路（内核 page_ocr/page_text/page_drop + 中枢
;  article_create/article_locate + lookup + spool 保存）
;
;  三条纪律（票面要求，别改回去）：
;   1. 长文链路有自己的在途标志 `G_LongPending` 与自己的超时，**绝不共用**旧框的
;      `G_Pending`/`G_OCRTimeout`。`RunShot` 的守卫只认 G_Pending；共用会把两条
;      完全不同的流程焊在一起（架构 §1.2 的死约束）。
;   2. **外壳只在 article_create 期间瞬时持有全文**：page_text 拼出来的 G_LongText
;      在 create 回包校验通过后立刻清掉，只保留「当前句 + 段落偏移 + 段数 + 目录预览」。
;      （规格 §0.35 的"外壳不持有全文"按"不常驻、不放进原句字段"解释，T7 文档会写明。）
;   3. 内核/中枢的失败**不许触发 KernelDied**：page_ocr 失败只让本次长文失败（toast +
;      日志），内核本身照常存活；hub 不在位只影响定位/释义，保存照走 spool。
; ============================================================================

; 长文在途（与旧框 G_Pending 完全独立）
global G_LongPending := ""          ; "" = 空闲；否则 "page_ocr"|"page_text"|"page_drop"
global G_LongDeadline := 0          ; 在途请求的墙钟上限
global G_LongPhase := ""            ; "ocr"|"text"|"create"|"idle"
global G_LongPageId := ""
global G_LongSource := ""           ; page_ocr 回包的原始分段输入（ASCII base64；原样转发 article_create）
global G_LongText := ""             ; **瞬时**全文（create 成功后立刻置空）
global G_LongPassages := []         ; [[start,end],...]（内核 passage_breaks 原样）
global G_LongOffset := 0
global G_LongQuality := "ok"
global G_LongDocId := 0
global G_LongPassageCount := 0
global G_LongTitle := ""
global G_LongApp := ""
global G_LongUrl := ""
global G_LongOutline := []          ; [{i, preview}]（outline 真实数据等 T4 owner 的档）
global G_LongBusy := false
global G_LongCreateMode := ""      ; "ocr"|"manual"（article_create 走哪条档）
global G_LongArticleFormat := ""  ; 面板当前文章来源（ocr/manual，证据用）
global G_LfOutlineOn := false
global G_LfOutlineRowsCache := []
; v22 P2b：outline 实际显示了几行 / 为了展开长到多高（0 = 没长窗）。
; `LfOutlineRowsCache` 的每行形状统一成 `{ n: 段号(1-based 文本), p: passage_index(0-based), t: 预览 }`
; —— 高亮按 `p` 匹配当前段（G_LfSegIndex），**不再按显示槽位**（旧 bug：段号>4 错行）。
global G_LfOutlineShown := 0
global G_LfOutlineGrewH := 0
global G_LongPickCancel := false
global G_LongOcrMs := 0
global G_LongLog := ""              ; 阶段 2 证据用：一条链路的时间线（只记数字/长度）

; 面板里的「当前词」状态（不复用旧框的 V/C/G_*）
global G_LfTarget := { word: "", kind: "word", phonetic: "", meaning: "", dictJson: "", meaningSource: "" }
global G_LfSegIndex := 1            ; 1 基段号
global G_LfSavedCount := 0
global G_LfSavedWords := []         ; 已存词（本句软笔触用；按保存顺序）
global G_LfLocateDeb := 0
global G_LfLocateGen := 0           ; 定位回话归属（换词后旧回包丢弃）
global G_LfLookupDeb := 0
; v23 S6：上一个已补发过 full 二跳的词（与旧框 G_LookupFollowedUp 同一口径）。
; 目标词变化/清空时归零；同词只补一次，换词可再补。
global G_LfLookupFollowedUp := ""
global G_LfLastFound := false
global G_LfLastLocate := { found: false, sentence: "", target_start: -1, target_end: -1, passage_index: -1, occurrences: 0 }
global G_LongPageChars := 0
global G_LongEscalated := false
global G_LfE2ESpoolLine := ""
global G_LfE2ELocateHits := 0
global G_LfE2ELookupRaw := ""
global G_LfE2ELookupReq := ""
global G_LfE2ELookupWord := ""
global G_LfE2ELocateMiss := 0
global G_LfE2ET0 := 0

; 长文请求的墙钟上限：page_ocr 整页识别实测 665~1227ms（规格 §3.2）；给 20s 兜底。
; 注意这是**长文自己的**超时，不是旧框的 G_OCRTimeout。
global LF_LONG_TIMEOUT := 20000

; ---- 长文链路时间线（证据；只记数字/长度，不记正文与目标词）----------------
LfTrace(s) {
    global G_LongLog
    G_LongLog .= (G_LongLog = "" ? "" : " | ") . s
    Log("长文：" . s)
}

; ============================================================================
;  内核链路：page_ocr → page_text* → （create）→ page_drop
; ============================================================================
LfKernelSend(request, kind) {
    global G_Sock, G_LongPending, G_LongDeadline
    if (!G_Sock) {
        LfLongFail("OCR 内核未就绪")
        return false
    }
    try {
        G_Sock.SendText(request . "`n")
    } catch as err {
        LfLongFail("发送失败：" . err.Message)
        return false
    }
    G_LongPending := kind
    G_LongDeadline := A_TickCount + LF_LONG_TIMEOUT
    SetTimer(LfLongWatch, 250)
    return true
}

; 内核回话的路由点（主脚本 OnKernelLine 在 pong 之后、G_Pending 之前调它）。
; 注意：这个回调跑在 Socket 的 Critical 里 —— 只解析 + 置状态，真正干活交给一次性拍子。
LfOnKernelLine(line) {
    global G_LongPending
    kind := G_LongPending
    if (kind = "")
        return
    G_LongPending := ""
    SetTimer(LfLongWatch, 0)
    parsed := { kind: kind, ok: JsonBool(line, "ok"), line: line }
    SetTimer(() => LfLongStep(parsed), -1)
}

; 一次性拍子：按阶段推进链路（不在 Critical 里做 UI/大字符串拼接）。
LfLongStep(r) {
    global G_LongPageId, G_LongQuality, G_LongPassages, G_LongText, G_LongPhase
    global G_LongOffset, G_LongOcrMs, G_LongPageChars, G_LongEscalated, G_LongSource
    if (!r.ok) {
        err := JsonField(r.line, "error")
        LfLongFail("内核拒绝：" . (err = "" ? "未知错误" : err))
        return
    }
    if (r.kind = "page_ocr") {
        G_LongPageId := JsonField(r.line, "page_id")
        G_LongSource := JsonField(r.line, "segmentation_source")
        G_LongQuality := JsonField(r.line, "quality")
        G_LongPassages := LfParsePairs(r.line, "passage_breaks")
        G_LongPageChars := JsonNumber(r.line, "chars")
        G_LongOcrMs := JsonNumber(r.line, "ms")
        G_LongEscalated := JsonBool(r.line, "escalated")
        LfTrace("page_ocr ok id=" . G_LongPageId . " 段=" . G_LongPassages.Length
              . " 字符=" . G_LongPageChars . " 质量=" . G_LongQuality . " 内核 " . G_LongOcrMs . "ms")
        if (G_LongPassages.Length < 1) {
            LfLongFail("整页识别没切出段落")
            return
        }
        G_LongPhase := "text", G_LongOffset := 0, G_LongText := ""
        LfRequestText()
        return
    }
    if (r.kind = "page_text") {
        G_LongText .= JsonField(r.line, "text")
        ; `next` 是 null = 读完（JsonNumber 抓 null 会给 0，所以单独判）
        if (RegExMatch(r.line, '"next"\s*:\s*null')) {
            LfTrace("page_text 读完 累计 " . StrLen(G_LongText) . " 字")
            G_LongPhase := "create"
            LfCreateArticle()
        } else {
            G_LongOffset := JsonNumber(r.line, "next")
            LfRequestText()
        }
        return
    }
    if (r.kind = "page_drop") {
        Log("长文：page_drop 完成（内核内存已释放）")
        return
    }
}

LfRequestText() {
    global G_LongPageId, G_LongOffset
    req := '{"cmd":"page_text","page_id":' . JsonString(G_LongPageId)
         . ',"offset":' . G_LongOffset . ',"limit":16384}'
    if (!LfKernelSend(req, "page_text"))
        return
}

; 段偏移数组解析：[[start,end],...]（JSON 数字对）。返回 [{s,e},...]
LfParsePairs(line, name) {
    ; 从 key 的 `[` 之后一路配对到结尾：不能用 `\[[^\]]*\]` 截包（那是**第一个**内层 `]`，
    ; 40 段时只会切出 1 段 —— 本机 E2E 实测踩到，粘贴档被判"分段数不一致"）。
    out := []
    if (!RegExMatch(line, '"' . name . '"\s*:\s*\[', &m))
        return out
    pos := m.Pos(0) + m.Len(0)
    while (RegExMatch(line, "\[\s*(\d+)\s*,\s*(\d+)\s*\]", &p, pos)) {
        out.Push({ s: p[1] + 0, e: p[2] + 0 })
        pos := p.Pos(0) + p.Len(0)
    }
    return out
}

; 墙钟兜底：内核在途超过 LF_LONG_TIMEOUT 就判本次长文失败（不判内核死）。
LfLongWatch() {
    global G_LongPending, G_LongDeadline
    if (G_LongPending = "")
        return
    if (A_TickCount < G_LongDeadline)
        return
    kind := G_LongPending
    G_LongPending := ""
    SetTimer(LfLongWatch, 0)
    LfLongFail("长文请求超时（" . kind . "）")
}

; 长文失败：只让本次长文失败 —— 绝不触发 KernelDied（内核本身可能好好的）。
LfLongFail(msg) {
    global G_LongBusy, G_LongPhase, G_LongText, G_LongPending, G_LongSource
    G_LongBusy := false
    G_LongPhase := "idle"
    G_LongPending := ""
    G_LongText := ""
    G_LongSource := ""
    SetTimer(LfLongWatch, 0)
    LfTrace("失败：" . msg)
    Log("长文失败：" . msg . "（内核/中枢不判死，旧框不受影响）")
    try ShowToast("长文识别失败：" . msg, "err")
}

; 中枢不在位：长文在途的 article/locate/lookup 回话不会来了。只降级本次长文
; （词仍可手打原句、仍可保存进 spool），**不判内核死**。
LfOnHubDown() {
    global G_LfBusyDummy, G_LfData, G_LfC
    LfTrace("中枢不在位：定位/释义不可用（手打原句仍可保存）")
    if (G_LfC.HasOwnProp("note"))
        LfSetHintText("中枢不在位 · 仍可手打原句保存", false)
}
; ============================================================================
;  article_create（全文在这条请求里发完；回包通过后立刻清 G_LongText）
; ============================================================================
LfCreateArticle() {
    global G_LongText, G_LongPassages, G_LongTitle, G_LongApp, G_LongUrl, G_LongPhase
    global G_LongSource
    ; passages 原样传（内核 page_ocr 的 passage_breaks）
    pj := ""
    for p in G_LongPassages
        pj .= (pj = "" ? "" : ",") . "[" . p.s . "," . p.e . "]"
    req := '{"cmd":"article_create","text":' . JsonString(G_LongText)
         . ',"passages":[' . pj . ']'
         . ',"title":' . JsonString(G_LongTitle)
         . ',"app":' . JsonString(G_LongApp)
         . ',"url":' . JsonString(G_LongUrl)
    ; v22 P1a（spec §6.7）：原始分段输入原样转发（ASCII base64，无引号/转义问题）；
    ; 旧内核没这个字段 → 空串，不追加（hub 按「无原始输入」落 NULL）。
    if (G_LongSource != "")
        req .= ',"segmentation_source":' . JsonString(G_LongSource)
    req .= '}'
    LfTrace("article_create 发出：全文 " . StrLen(G_LongText) . " 字 / " . G_LongPassages.Length . " 段"
          . " / 原始输入 " . StrLen(G_LongSource) . " 字节")
    ; 发送方向实测可一次发 48~400KB（主控探针），不需要分块协议（#156 只影响 recv）
    if (!HubSend(req, "article")) {
        LfLongFail("中枢不在位，文章建不起来（词不会丢，可稍后重试）")
        return
    }
    G_LongPhase := "create"
    G_LongPassagesJson := pj
}

; v21 round2 (B)：记下这篇文章的来源标题（article_create 回包里的 title 就是 hub
; `clean_title` 口径 = session 标题的来源；回包里没有才退回外壳采到的原始窗口标题）。
LfNoteSourceTitle(line) {
    global G_LfSourceTitle, G_LongTitle
    t := JsonField(line, "title")
    if (NormalizeLine(t) = "")
        t := G_LongTitle
    G_LfSourceTitle := NormalizeLine(t)
}

; 面板显示口径：复用旧框的 ShortTitle（单行化 + 28 字 + …），不另写截断。
; demo 数据没有 sourceTitle 字段 → 空串（demo 不显示来源）。
LfSourceTitleText(d) {
    if (!IsObject(d) || !d.HasOwnProp("sourceTitle"))
        return ""
    return ShortTitle(d.sourceTitle)
}

LfOnArticleLine(line) {
    global G_LongDocId, G_LongPassageCount, G_LongText, G_LongPassages, G_LongBusy, G_LongPhase
    global G_LongCreateMode, G_LongArticleFormat, G_LongSource
    if (!JsonBool(line, "ok")) {
        LfLongFail("中枢拒绝 article_create：" . JsonField(line, "error"))
        return
    }
    ; v21 round2 (B)：先记下本文的来源短标题（回包里的 title = hub clean_title 口径，
    ; 就是取词台 session 标题的那一份），再走两条入口的落库分支。
    LfNoteSourceTitle(line)
    docId := JsonNumber(line, "doc_id")
    pc := JsonNumber(line, "passage_count")
    ; v22 P2a 去重四态（§6.5）：`dedup: new|reused|updated`。reused = 同文已存在，
    ; 回包是**旧文章**的 doc_id/passage_count/passage_breaks —— 同文重录但分段数不同时
    ; 拿它与本地 OCR 段数比对会误报「段落数不一致」（Lead 2026-10-27 联调实测）。
    dedup := JsonField(line, "dedup")
    if (G_LongCreateMode = "manual") {
        ; 粘贴档：**passages 是 hub 从原文切出来的**（请求里不带 passages）。
        ; 校验 passage_breaks 段数 == passage_count，再用瞬时原文算目录预览，随后释放原文。
        breaks := LfParsePairs(line, "passage_breaks")
        if (breaks.Length != pc) {
            LfLongFail("粘贴分段数不一致（breaks " . breaks.Length . " / passage_count " . pc . "）")
            return
        }
        G_LongDocId := docId
        G_LongPassageCount := pc
        G_LongPassages := breaks
        G_LongArticleFormat := "manual"
        LfBuildOutlineFromText()
        G_LongText := ""
        G_LongSource := ""
        G_LongBusy := false
        G_LongPhase := "idle"
        G_LongCreateMode := ""
        LfTrace("article_create(manual) ok doc_id=" . docId . " 段=" . pc
              . " session=" . JsonNumber(line, "session_id"))
        LfResetArticleState()
        LfShowArticle()
        LfToastDedup(dedup)
        return
    }
    G_LongArticleFormat := "ocr"
    if (dedup = "reused") {
        ; 复用旧文：以 hub 回包的分段为准（正文指纹相同，只是分段可能来自旧算法版本）。
        ; 旧 hub 不回 passage_breaks 时退回本地（仅用于目录预览，不再据此报错）。
        breaks := LfParsePairs(line, "passage_breaks")
        if (pc > 0 && breaks.Length = pc)
            G_LongPassages := breaks
        Log("长文：article_create 命中已存在文章（dedup=reused doc_id=" . docId . " 段=" . pc
          . "），跳过本地段数一致性校验")
    } else if (pc != G_LongPassages.Length) {
        ; 票面要求：不一致立刻报错，别静默（否则后面 locate 的段号全错）
        LfLongFail("段落数不一致（本地 " . G_LongPassages.Length . " / 中枢 " . pc . "）")
        return
    }
    G_LongDocId := docId
    G_LongPassageCount := pc
    LfTrace("article_create ok doc_id=" . docId . " 段=" . pc . " session=" . JsonNumber(line, "session_id"))
    ; ---- 这里起外壳**不再常驻全文**：目录预览先算好（小），再清瞬时全文 ----
    LfBuildOutlineFromText()
    G_LongText := ""
    G_LongSource := ""
    G_LongBusy := false
    G_LongPhase := "idle"
    G_LongCreateMode := ""
    ; 释放内核内存（正文已进 hub）
    LfKernelSend('{"cmd":"page_drop","page_id":' . JsonString(G_LongPageIdSafe()) . '}', "page_drop")
    ; 打开/刷新面板
    LfResetArticleState()
    LfShowArticle()
    LfToastDedup(dedup)
}

; 去重四态的提示（§6.5 / §14 验收第 8 步）：new 不提示（正常新建）。
; reused = 这篇已存在（一个字都没写）；updated = 同标题旧文被新内容更新（旧身份保留）。
LfToastDedup(dedup) {
    if (dedup = "reused")
        ShowToast("这篇已存在，已为你保留", "ok")
    else if (dedup = "updated")
        ShowToast("同标题文章已更新", "ok")
}

G_LongPageIdSafe() {
    global G_LongPageId
    return G_LongPageId
}

; 目录预览：段落首 60 字（真实 outline 档到了以后换成 hub 给的数据）
LfBuildOutlineFromText() {
    global G_LongText, G_LongOutline, G_LongPassages
    out := []
    for i, p in G_LongPassages {
        t := SubStr(G_LongText, p.s + 1, Max(1, Min(60, p.e - p.s)))
        t := NormalizeLine(t)
        out.Push({ i: i, preview: t })
    }
    G_LongOutline := out
}

; ============================================================================
;  入口：Ctrl+Alt+4 = 遮罩拖框 → 整页 OCR → 建面板
; ============================================================================
LongformCaptureStart() {
    global G_LongBusy, G_ShotActive, G_LongText, G_LongPassages, G_LongPhase
    global G_LongTitle, G_LongApp, G_LongUrl, G_LongDocId, G_LongOutline, G_LongOcrMs
    global G_LongPageChars, G_LongEscalated, G_LfE2ET0
    ; 旧框有在途截图请求时不许再发长文请求：内核一次只处理一条（请求-回话对号入座）。
    ; 走主脚本的 KernelBusy()（它读旧的 G_Pending；面板自己不碰那个标志）。
    if (G_LongBusy || G_ShotActive || KernelBusy()) {
        Log("长文：已有截图请求在途，忽略本次")
        return
    }
    if (!G_Ready || !G_Sock) {
        ShowToast("OCR 内核未就绪，稍后再试", "err")
        Log("长文：内核未就绪，拒绝整页识别")
        return
    }
    ; 出处必须在**任何窗口出现之前**采集（旧框同一条纪律）。v21 round2 (B)：
    ; 抽成 LongformCaptureProvenance（面板自己在前台时退回最近非面板前台），
    ; E2E 钩子调同一个函数 —— 真实入口与证据跑的是同一条采集路径。
    LongformCaptureProvenance()
    ; 票 #176（A3）：busy 位在遮罩拖框**进入前**置位（对齐旧框 G_ShotActive 的
    ; 「拖框进行中挡热键重入」语义）—— 拖框窗口（Esc / 30s 兜底取消，最长 30s）内
    ; 旧框三入口按 Logic.LongformInFlight(G_LongBusy, G_LongPending) 拒绝，双遮罩
    ; 互踩 / capture_ocr 与 page_ocr 回话串线不再可能。
    ; 归零收口（spec §5 风险项）：拖框取消/选区过小/异常（rect=""）在下面还；
    ; 发送失败在 LongformCaptureWithRect；链路失败走 LfLongFail；建文完成走
    ; LfOnArticleLine —— 任何退出路径 busy 位必须归零（harness 片 36 钉）。
    G_LongBusy := true
    rect := LfPickRect()
    if (rect = "") {
        G_LongBusy := false
        Log("长文：拖框取消")
        return
    }
    G_LongBusy := true
    G_LongText := ""
    G_LongPassages := []
    G_LongDocId := 0
    G_LongOutline := []
    LongformCaptureWithRect(rect)
}

; v21 round2 (B)：OCR 入口的出处采集（遮罩之前）。G_LongUrl 保持空：旧框的 URL
; 来自 hub 的 browser context（session_peek），长文入口没有那条回包，不为它新增
; 窗口/浏览器依赖（工单 §2.1 的边界）。采集口径复用 CapturePanelProvenance。
; 无返回值（void）：两个调用点（真实入口 / E2E 钩子）都只用它写 G_Long* 的副作用。
LongformCaptureProvenance() {
    global G_LongTitle, G_LongApp, G_LongUrl
    PanelFillLongProvenance()
    Log("长文：出处已在任何窗口出现之前采集（自动出处"
      . (NormalizeLine(G_LongTitle) = "" ? "没抓到" : "已抓到") . "）")
}

; 拖框结果已经拿到后的链路起点（E2E 钩子也走这里 —— 与真实入口**同一套代码**）
LongformCaptureWithRect(rect) {
    global G_LongBusy, G_LongText, G_LongPassages, G_LongPhase, G_LongDocId, G_LongOutline
    global G_LongOcrMs, G_LongPageChars, G_LongEscalated, G_LfE2ET0, G_LongCreateMode
    global G_LongSource
    G_LongBusy := true
    G_LongText := ""
    G_LongSource := ""
    G_LongPassages := []
    G_LongDocId := 0
    G_LongOutline := []
    G_LongPhase := "ocr"
    G_LongCreateMode := "ocr"
    G_LongOcrMs := 0, G_LongPageChars := 0, G_LongEscalated := false
    G_LfE2ET0 := A_TickCount
    LfTrace("拖框 rect=" . rect.x . "," . rect.y . "," . rect.w . "," . rect.h)
    req := '{"cmd":"page_ocr","rect":[' . rect.x . "," . rect.y . "," . rect.w . "," . rect.h . ']}'
    if (!LfKernelSend(req, "page_ocr")) {
        G_LongBusy := false
        G_LongPhase := "idle"
    }
}

; ---- 长文专用遮罩拖框（独立模式；复用主脚本的 DragRect/DrawSel/Mask 端点消息）----
LfPickRect() {
    global LP, G_MaskOv, G_MaskOx, G_MaskOy, G_MaskDownX, G_MaskDownY, G_MaskUpX, G_MaskUpY
    global DragHwnds, G_LongPickCancel, SHOT_IDLE_MS, MIN_SELECT_LP, G_LfScale
    G_LongPickCancel := false
    ov := 0, hintG := 0, bars := [], rect := ""
    try {
        ov := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000")
        ov.BackColor := LP.BLACK
        ov.Show("x0 y0 w" . A_ScreenWidth . " h" . A_ScreenHeight . " NoActivate")
        WinSetTransparent(LP.DIM, ov)
        DragHwnds := [ov.Hwnd]
        G_MaskOv := ov.Hwnd
        ov.GetPos(&G_MaskOx, &G_MaskOy)
        G_MaskDownX := "", G_MaskDownY := "", G_MaskUpX := "", G_MaskUpY := ""
        hintG := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
        hintG.BackColor := LP.BLACK
        hintG.MarginX := LfPx(14), hintG.MarginY := LfPx(8)
        hintG.SetFont("s12 cFFFFFF", "Segoe UI")
        hintG.AddText(, "拖框圈住整块英文正文 · Esc 取消 · 松手开始整页识别")
        hintG.Show("AutoSize NoActivate Hide")
        WinSetTransparent(LP.TOAST_A, hintG)
        hintG.GetPos(, , &hw)
        hintG.Move(Round((A_ScreenWidth - hw) / 2), LfPx(LP.HINT_TOP))
        hintG.Show("NoActivate")
        DragHwnds.Push(hintG.Hwnd)
        bars := []
        Loop 2 {
            col := (A_Index = 1) ? LP.BLACK : LP.ACCENT
            Loop 4 {
                b := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
                b.BackColor := col
                b.Show("x0 y0 w0 h0 NoActivate")
                bars.Push(b)
                DragHwnds.Push(b.Hwnd)
            }
        }
        roG := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
        roG.BackColor := LP.BLACK
        roG.MarginX := LfPx(8), roG.MarginY := LfPx(3)
        roG.SetFont("s10 cFFFFFF", "Segoe UI")
        roCtl := roG.AddText("w" . LfPx(92), "0 × 0")
        roG.Show("AutoSize NoActivate Hide")
        WinSetTransparent(LP.TOAST_A, roG)
        roG.GetPos(, , , &roH)
        roG.Show("NoActivate")
        DragHwnds.Push(roG.Hwnd)
        ; 回调必须是「可接受参数的函数对象」：裸零参函数在 AHK v2 里被拒（本机实测
        ; "Invalid callback function"），所以统一用箭头包一层（与旧框 RunShot 同款）。
        Hotkey("Esc", (*) => LfPickCancelHotkey(), "On")
        SetTimer(LfPickIdle, -SHOT_IDLE_MS)
        while (!GetKeyState("LButton", "P") && !G_LongPickCancel)
            Sleep(15)
        if (!G_LongPickCancel) {
            if (G_MaskDownX != "")
                x1 := G_MaskDownX, y1 := G_MaskDownY
            else
                MouseGetPos(&x1, &y1)
            x2 := x1, y2 := y1
            lastX := -1, lastY := -1, lastW := -1, lastH := -1
            while (GetKeyState("LButton", "P") && !G_LongPickCancel) {
                MouseGetPos(&x2, &y2)
                rx := Min(x1, x2), ry := Min(y1, y2)
                rw := Abs(x2 - x1), rh := Abs(y2 - y1)
                if (rx != lastX || ry != lastY || rw != lastW || rh != lastH) {
                    lastX := rx, lastY := ry, lastW := rw, lastH := rh
                    DrawSel(bars, rx, ry, rw, rh)
                    roCtl.Text := Round(rw / G_LfScale) . " × " . Round(rh / G_LfScale)
                    roG.Move(rx, Max(LfPx(LP.EDGE), ry - roH - LfPx(6)))
                }
                Sleep(10)
            }
            if (!G_LongPickCancel) {
                if (G_MaskUpX != "")
                    x2 := G_MaskUpX, y2 := G_MaskUpY
                r := DragRect(x1, y1, x2, y2)
                if (r.w < LfPx(MIN_SELECT_LP) || r.h < LfPx(MIN_SELECT_LP)) {
                    Log("长文：选区过小，按误操作取消")
                } else {
                    rect := { x: r.x, y: r.y, w: r.w, h: r.h }
                }
            }
        }
    } catch as err {
        Log("长文：拖框流程异常：" . err.Message)
    } finally {
        SetTimer(LfPickIdle, 0)
        Hotkey("Esc", "Off")
        DragHwnds := []
        G_MaskOv := 0
        for b in bars
            try b.Destroy()
        if (IsSet(roG) && roG)
            try roG.Destroy()
        if (hintG)
            try hintG.Destroy()
        if (ov)
            try ov.Destroy()
        Sleep(30)
    }
    return rect
}

LfPickCancelHotkey() {
    global G_LongPickCancel
    G_LongPickCancel := true
}

LfPickIdle() {
    global G_LongPickCancel
    G_LongPickCancel := true
    Log("长文：遮罩 30s 无操作，自动取消")
}

; ============================================================================
;  真实文章 → 面板数据（七种状态继续用同一套渲染）
; ============================================================================
LfRealData() {
    global G_LfData, G_LongDocId, G_LongPassageCount, G_LongQuality, G_LfSegIndex, G_LfTarget
    global G_LfSavedCount, G_LfSavedWords, G_LfSourceTitle
    d := { state: "empty", dark: G_LfDarkState()
         , pageId: G_LongPageId, passages: G_LongPassages, passageBreaks: G_LongPassages
         , quality: G_LongQuality, docId: G_LongDocId, segCount: G_LongPassageCount
         ; v21 round2 (B)：来源标题（session 标题同源），顶栏短标题用它。
         ; **只属于**当前这篇文章：`G_LongDocId=0`（还没建好文章 / 建库失败）时不暴露 ——
         ; 否则关面板再空态重开会把上一篇的标题带到「未载入文章」的面板上
         ; （2026-10-26 review 收口；复现与前后证据见 docs/research/v21-session-title/）。
         , sourceTitle: (G_LongDocId ? G_LfSourceTitle : "")
         , segIndex: G_LfSegIndex, segFill: 0
         ; kind 存枚举（word/phrase/sentence），中文标签只在渲染层由 KindLabel() 映射
         , word: "", kind: "word", phonetic: "", meaning: "", meaningHint: true
         , meaningText: (G_LongDocId ? "打一个词，原句会自动出现在这里" : "按 Ctrl+Alt+4 框住正文，整页识别后在这里取词")
         , paperText: "", runs: [], italic: false, paperFg: "hint"
         , note: { kind: "none", text: "" }, outline: false, lowQuality: (G_LongQuality = "low")
         , clip: false, showQuit: true, hintRight: "全文 Alt ↓", reocr: (G_LongQuality = "low") }
    if (G_LongDocId) {
        d.segLabel := G_LongPassageCount . " 段 · 第 " . G_LfSegIndex . " 段"
        d.segFill := (G_LongPassageCount > 1) ? ((G_LfSegIndex - 1) / (G_LongPassageCount - 1)) : 1
        d.paperText := "原句会在命中时出现在这里 · 目标词会高亮"
        d.runs := [{ text: d.paperText, mark: "" }]
        if (d.lowQuality)
            d.note := { kind: "warn", text: "这段识别质量偏低 · 建议缩小选区或改用粘贴" }
        d.outlineRows := LfOutlineRows()
        ; v22 P2b：当前段号是 **1-based**；渲染层拿它与每行 passage_index+1 比较。
        ; （旧实现这里写 0，展开时又是槽位比较 → 高亮基本永远错行。）
        d.outlineHere := G_LfSegIndex
    } else {
        d.segLabel := "未载入文章"
        d.paperText := "还没有文章。按 Ctrl+Alt+4 拖框圈住整块英文正文，整页识别完成后这里会出现原句。"
        d.runs := [{ text: d.paperText, mark: "" }]
    }
    return d
}

LfOutlineRows() {
    global G_LongOutline, G_LfSegIndex
    rows := []
    ; 目录兜底（真实回包到之前的占位）：以当前段为中心取一段窗口，最多 OUTLINE_ROWS 行。
    ; 每行带 p（0-based passage_index）—— 渲染层按 p 匹配高亮，不按槽位。
    start := Max(1, G_LfSegIndex - 2)
    loop LF.OUTLINE_ROWS {
        i := start + A_Index - 1
        if (i < 1 || i > G_LongOutline.Length)
            continue
        rows.Push({ n: "" . i, p: i - 1, t: G_LongOutline[i].preview })
    }
    return rows
}

; 面板展示当前文章（没开就开，已开就刷新）
LfShowArticle() {
    global G_LfGui, G_LfData
    G_LfData := LfRealData()
    LfSyncInspectButton()
    if (G_LfGui) {
        ; 已经是另一篇文章了（粘贴/重新识别在面板开着时进来）：词框与目标词状态显式清一次。
        ; 渲染只读不写（Bug 2a），所以这里必须走收口，否则会留着上一篇的词。
        LfWriteWord("")
        LfRender(G_LfData)
        WinActivate("ahk_id " . G_LfGui.Hwnd)
        LfFocusWord()
        return
    }
    LfBuild(G_LfData)
    if (EnvGet("LF_E2E_ACTIONS") != "") {
        ; E2E 副本：**绝不抢焦点**（用户可能正在打字）、不放键位热键，面板钉在左下角；
        ; 程序化 SetText 不需要焦点。证据跑完即关。
        LfPresent({ x: LfPx(40), y: Max(LfPx(40), A_ScreenHeight - LfPx(560)) })
    } else {
        LfPresent(LfRestorePos())
        LfRegisterKeys()
        LfFocusWord()
    }
    Log("长文面板已打开（真实文章 doc_id=" . G_LongDocId . "）")
}

; 新文章就位：段号/计数/当前词归零（两条入口共用）
LfResetArticleState() {
    global G_LfSegIndex, G_LfSavedCount, G_LfSavedWords, G_LfTarget, G_LfLastLocate, G_LfOutlineOn
    G_LfSegIndex := 1
    G_LfSavedCount := 0
    G_LfSavedWords := []
    G_LfLastLocate := { found: false, sentence: "", target_start: -1, target_end: -1
                      , passage_index: -1, occurrences: 0 }
    G_LfTarget := { word: "", kind: "word", phonetic: "", meaning: "", dictJson: "", meaningSource: "" }
    G_LfOutlineOn := false
}

; 面板内 Ctrl+V / ^!5（剪贴板有文本时）：原始多行文本 → article_create(format=manual)
LfPasteFromClipboard() {
    txt := ""
    try txt := A_Clipboard
    catch {
        ShowToast("读不到剪贴板，请重试", "err")
        return
    }
    if (Trim(txt) = "") {
        Log("长文粘贴：剪贴板为空（不建文章）")
        ShowToast("剪贴板里没有文本", "err")
        return
    }
    ; v21 round2 (B)：面板内 Ctrl+V 时前台是面板自己 → 退回「最近一次非面板前台」
    ; 缓存（面板开着时 700ms 定时器维护），session 标题才不会变成「长文面板」。
    PanelFillLongProvenance()
    LfPasteText(txt)
}

LfPasteText(text) {
    global G_LongBusy, G_LongText, G_LongCreateMode, G_LongTitle, G_LongApp, G_LongUrl
    global G_LongDocId, G_LongOutline, G_LongPassages, G_LongPageChars, G_LongQuality
    global G_LongOcrMs, G_LongEscalated, G_LfE2ET0, G_LongArticleFormat, G_LongSource
    ; v21 round2 (B) 调用契约：**调用方必须先填好出处**（G_LongTitle/G_LongApp/
    ; G_LongUrl）—— 真实入口是 `LongformHotkeyPaste()` 与 `LfPasteFromClipboard()`，
    ; 两者都先走 `PanelFillLongProvenance()`。本函数自己不清空也不采集（它不碰前台，
    ; 面板内调用时前台就是面板自己）。直接调它的 E2E 动作 `paste_file` 同样要先采集。
    if (G_LongBusy) {
        Log("长文粘贴：已有长文请求在途，忽略本次")
        ShowToast("长文识别/粘贴进行中，请稍候", "err")
        return
    }
    ; 只去掉首尾空白行，**内部换行保留**（hub 的 manual 档自己分词/去页眉页码）
    text := Trim(Trim(text, "`r`n"), " `t")
    if (Trim(text) = "") {
        Log("长文粘贴：纯空白文本，不建文章")
        ShowToast("剪贴板里没有可用的文本", "err")
        return
    }
    if (StrLen(text) > 500000) {
        Log("长文粘贴：文本过长（" . StrLen(text) . " 字），hub 上限 512KB")
        ShowToast("剪贴板文本过长（>512KB）", "err")
        return
    }
    G_LongBusy := true
    G_LongCreateMode := "manual"
    G_LongText := text
    G_LongSource := ""
    G_LongDocId := 0
    G_LongPassages := []
    G_LongOutline := []
    ; v21 round2 (B)：G_LongTitle/G_LongApp/G_LongUrl 由调用方（^!5 / 面板 Ctrl+V 的
    ; PanelFillLongProvenance）在进来之前填好，这里**不许清空** —— 清空就是上一轮
    ; 取词台永远「（未命名）」的根因。
    G_LongQuality := "ok"
    G_LongPageChars := StrLen(text)
    G_LongOcrMs := 0, G_LongEscalated := false
    G_LfE2ET0 := A_TickCount
    LfTrace("article_create(manual) 发出：原文 " . StrLen(text) . " 字")
    ; 关键契约：manual 档**不带 passages 字段**（带了哪怕 [] 都会被当显式分段）
    ; 出处（title/app/url）随请求进 hub：session 标题 / 取词台侧栏就靠它。
    req := '{"cmd":"article_create","text":' . JsonString(text)
         . ',"format":"manual"'
         . ',"title":' . JsonString(G_LongTitle) . ',"app":' . JsonString(G_LongApp)
         . ',"url":' . JsonString(G_LongUrl) . '}'
    if (!HubSend(req, "article")) {
        G_LongBusy := false
        G_LongCreateMode := ""
        G_LongText := ""
        LfLongFail("中枢不在位，粘贴的文章建不起来")
        return
    }
    G_LongArticleFormat := "manual"
}

; ============================================================================
;  全文展开（Alt+↓）：article_get(outline:true) 取段落目录（T6 后端档）
; ============================================================================
; 展开态内容比窗口高 → 把面板长到自然高度（上限 = 主屏工作区，留 24px 边）。
; 没有展开 / 已经装得下时是 no-op（回包到达后会再调一次，见 LfOnOutlineLine）。
LfOutlineGrowToFit() {
    global G_LfGui, G_LfData, G_LfOutlineGrewH
    if (!G_LfGui || !IsObject(G_LfData) || !G_LfData.outline)
        return
    need := LfLayout(0, true)
    sz := LfClientSize(G_LfGui.Hwnd)
    if (need <= sz.h)
        return
    r := LfWindowRect(G_LfGui.Hwnd)
    ; LfLayout 量的是**客户区**内容高度，Gui.Move 的 h 是窗口高度：补上非客户区
    ; （无标题栏 + WS_THICKFRAME 仍有几像素边框；不补的话刚好少 1 行）。
    frame := Max(0, r.h - sz.h)
    MonitorGetWorkArea(MonitorGetPrimary(), &wl, &wt, &wr, &wb)
    target := Min(need + frame, Max(LfPx(LF.MIN_H), wb - wt - LfPx(24)))
    if (target <= r.h)
        return
    G_LfGui.Move(r.x, r.y, r.w, target)
    G_LfOutlineGrewH := target
    LfFit()
    Log("长文：outline 展开长窗 " . r.h . " → " . target . "px（客户区需要 " . need . "）")
}

LfToggleOutline() {
    global G_LfGui, G_LfData, G_LongDocId, G_LfOutlineOn, G_LfOutlineGrewH, G_LfSegIndex
    if (!G_LfGui || !IsObject(G_LfData))
        return
    if (!G_LongDocId) {
        Log("长文：还没有文章，全文展开不可用")
        return
    }
    G_LfOutlineOn := !G_LfOutlineOn
    G_LfData.outline := G_LfOutlineOn
    if (G_LfOutlineOn) {
        G_LfData.outlineRows := []
        G_LfData.outlineHere := G_LfSegIndex
        LfRender(G_LfData)
        LfRequestOutline(false)
        ; v22 P2b（§9「面板高度允许时」）：展开后内容自然高度可能超过窗口 —— 先长窗到
        ; 自然高度（上限 = 主屏工作区），仍装不下才由 LfLayout 少显示几行；
        ; 收起时把窗口收回到**内容自然高度**（不是还原展开前的用户高度：用户手动调过的
        ; 高度只在本次展开期间被临时覆盖，收起后按内容重算）。
        ; 目录行是**异步**回来的：这里先试一次（兜底目录），回包到了会再试一次。
        LfOutlineGrowToFit()
    } else {
        LfRender(G_LfData)
        if (G_LfOutlineGrewH > 0) {
            r := LfWindowRect(G_LfGui.Hwnd)
            frame := Max(0, r.h - LfClientSize(G_LfGui.Hwnd).h)
            target := Max(LfPx(LF.MIN_H), LfLayout(0) + frame)
            G_LfGui.Move(r.x, r.y, r.w, target)
            G_LfOutlineGrewH := 0
        }
        LfFit()
    }
}

; aroundSelect=false → 带当前段；aroundSelect=true → 显式 `"around":null`（从头取）
LfRequestOutline(nullAround := false) {
    global G_LongDocId, G_LfSegIndex
    if (!G_LongDocId)
        return
    around := nullAround ? "null" : (G_LfSegIndex - 1)
    ; `"outline":true` 必须是 JSON true（发 1 不生效 —— 契约）
    ; v22 P2b：before/after 2/2 → 3/4，回包最多 8 段，正好填满 OUTLINE_ROWS。
    req := '{"cmd":"article_get","doc_id":' . G_LongDocId
         . ',"outline":true,"around":' . around . ',"before":3,"after":4}'
    if (HubSend(req, "lf_outline"))
        Log("长文：article_get(outline) 已发出（around=" . around . "）")
}

LfOnOutlineLine(line) {
    global G_LongDocId, G_LfSegIndex, G_LfOutlineOn, G_LfOutlineRowsCache, G_LfData
    if (!JsonBool(line, "ok")) {
        Log("长文：article_get(outline) 失败：" . JsonField(line, "error"))
        return
    }
    if (JsonNumber(line, "doc_id") != G_LongDocId)
        return
    body := ""
    if (RegExMatch(line, '"outline"\s*:\s*\[(.*?)\]\s*}?\s*$', &m))
        body := m[1]
    rows := []
    pos := 1
    while (RegExMatch(body, "\{[^{}]*\}", &om, pos)) {
        obj := om[0]
        pi := JsonNumber(obj, "passage_index")
        pv := JsonField(obj, "preview")
        rows.Push({ n: "" . (pi + 1), p: pi, t: pv })
        pos := om.Pos(0) + om.Len(0)
    }
    G_LfOutlineRowsCache := rows
    if (G_LfOutlineOn && IsObject(G_LfData)) {
        G_LfData.outlineRows := rows
        G_LfData.outlineHere := G_LfSegIndex
        LfRender(G_LfData)
        ; 真实目录行到了：此时才知道展开态的自然高度，再试一次长窗（8 行需要更多高度）。
        LfOutlineGrowToFit()
    }
    Log("长文：outline 回包 " . rows.Length . " 段，around=" . JsonNumber(line, "around"))
}

; ============================================================================
;  打词 → 去抖 250ms → article_locate → 原句/高亮；音标释义走 lookup
; ============================================================================
LfOnTargetChange() {
    global G_LfC, G_LfTarget, G_LfLocateDeb, G_LfLocateGen, G_LfData, G_LfLookupDeb
    global G_LfLookupFollowedUp
    prevWord := G_LfTarget.word
    word := NormalizeLine(G_LfC.word.Value)
    ; v23 S6：换了词 → 二跳防重入标记归零（新词可以再补一次 full）。
    ; 清空路径另在下面显式归零：空态提前 return，别让标记跨过空态活到下一个词。
    if (word != prevWord)
        G_LfLookupFollowedUp := ""
    G_LfTarget.word := word
    ; 类型随输入**实时**推断（用户验收 Bug 3）：旧实现只在下一次定位回包
    ; （LfApplyHit）里算一次，打字过程中 chip 一直是旧值。写回 G_LfTarget.kind ——
    ; 保存（SpoolLine 的 kind）读的是它，所以旧实现连落库的 kind 都永远是初始值。
    kind := InferKind(word)
    G_LfTarget.kind := kind
    G_LfLocateGen += 1
    ; #205：先把底栏的次数标签跟上**这一键**（顺句态才有；空态/非顺句态会自动还原）。
    ; 放在空态早退之前 —— 否则清空后 chainHint 还挂着上一串词的次数，下一次
    ; LfShowEmptyState 的重渲染会照它画出来。
    LfUpdateChainHint()
    ; 面板的补全都用单句（当前命中句 / 未命中时为空）
    d := G_LfData
    if (word = "") {
        ; 清空：**去抖状态必须一起清**（旧实现对空词提前 return，两个 Deb 状态都留着上
        ; 一个词）。否则清空后重打**同一个词**时 DebounceStep 判 `state.word = w` →
        ; schedule:false → 不排定时器 → 原句/高亮（以及释义）永远不刷新。
        ; main 的清空路径同样把 `G_LookupDeb := 0`，这里把 locate 与 lookup 两个都归零。
        G_LfLocateDeb := 0
        G_LfLookupDeb := 0
        G_LfLookupFollowedUp := ""            ; v23 S6：空态不许带着上一个词的二跳标记
        ; 清空：回到空态（LfShowEmptyState 会重渲染，chip 随之隐藏）
        LfShowEmptyState()
        SetTimer(LfLocateFire, 0)
        return
    }
    ; v29：目标词一旦变化，上一条词的自动释义 / 音标 / 词典快照立即作废。
    ; 旧版只处理「多词 → 单词」；单词 → 单词（先查 si、再改成 simulations）会把
    ; 上一条的释义带进新词落库（生产库 2208：target=simulations、dict_json.word=SI）。
    ; 面板没有手打释义，任何换词都按必清处理；新词的回包到站后再按身份校验填回。
    if (word != prevWord)
        LfClearMeaningHint()
    ; chip 立即刷新：只更新 chip 文本/宽度，不重跑整窗渲染（每键全量渲染会白跑
    ; 一遍 RichEdit 高亮格式化；这里只在 chip 宽度变化时复用 LfFit 的唯一一套布局）。
    if (IsObject(d))
        d.kind := kind
    LfUpdateKindChip(kind)
    ; chip 的 x 依赖**每键变化**的文本宽（同一 kind 内连续输入时也要跟手，否则 chip 停在
    ; 旧位置、可能压住正在输入的文字）。轻量重摆：只量文本宽 + Move，不跑整窗布局。
    LfPlaceKindChip()
    ; v22 P5（#169 / spec §11）：多词目标 → 释义区显示「短语 · 见原句」/「句 · 见原句」。
    ; 上一条词的释义/音标/词典快照已由上面的 v29「换词即清」统一处理，
    ; 不再区分「多词 → 单词」（面板没有手打释义，任何换词都清）。
    if (IsMultiwordTarget(word))
        LfShowMultiwordMeaningHint()
    else if (IsObject(d) && d.meaningHint && d.meaning = "")
        LfClearMeaningHint()
    st := DebounceStep(G_LfLocateDeb, word, A_TickCount, LOOKUP_DEBOUNCE_MS)
    G_LfLocateDeb := st.state
    if (st.schedule)
        SetTimer(LfLocateFire, -LOOKUP_DEBOUNCE_MS)
}

; 目标词行的"词框让位 + chip 摆位"**唯一一处**：chip 宽度按实际文本量，
; chip 的 x 按控件里**当前**文本宽 kw（含尾随空格）算，紧跟文字、不压字。
; 行几何（x0/y/innerW/wordH/kindH）由 LfLayout 记进 G_LfWordRow —— 这里是同一份几何的
; 轻量重摆，不是第二套布局（Change 路径每键都调，不跑 RichEdit 测量/整窗排版）。
; 返回 chip 宽度（物理 px），LfLayout 用它给词框让位。
LfPlaceKindChip() {
    global G_LfC, G_LfWordRow
    if (!G_LfWordRow || !G_LfC.HasOwnProp("kind"))
        return 0
    C := G_LfC, row := G_LfWordRow
    kindW := C.kind.Visible ? (LfTextWidth(C.kind, C.kind.Text) + LfPx(10)) : 0
    kw := (NormalizeLine(C.word.Value) != "") ? LfTextWidth(C.word, C.word.Value) : 0
    kw := Min(kw, row.innerW - kindW - LfPx(9))
    C.kind.Move(row.x0 + kw + LfPx(9)
        , row.y + (row.wordH - row.kindH) // 2, Max(1, kindW), row.kindH)
    return kindW
}

; #205（上游 #204 决策 B）：顺句态把「第 N 处 / 共 M 处」摆到底栏右侧那个提示格。
;
; 为什么是 footHint 而不是状态行（C.note）：note 行的高度只在它**可见**时才由 LfLayout
; 预留（NOTE_GAP 11 + NOTE_H 17 = 28 逻辑 px，见 LfLayout 的「状态行」段）。Change 路径
; 上一旦让它显隐，整条底栏（分隔线 + 全部按钮）就得上下挪 28px —— 那就是布局跳动；而且
; 挪完还会顶出客户区（LfFit 把多余高度全给纸面，内容底边本来就贴着窗口底）。footHint
; 是常驻可见的提示格：只改它自己的文本 / 颜色 / 宽度，其余控件一个都不动 —— 与
; LfPlaceKindChip 同一种「量文本宽 + Move 一格」的轻量重摆，**不跑 LfRender / LfFit**。
;
; 为什么这一格只放次数标签、不带「Tab 接词 → X」：默认 428 面板的底栏按钮右侧只剩
; 172 逻辑 px 净宽（innerW 388 − 保存/分段/收工 216，再留 8px 缝 = 164）；本机 11pt 实测
; 整句文案 204 px、连「Tab 接词 · 第 1 处 / 共 4 处」也要 176 px，都会压在「收工」上；
; `第 1 处 / 共 4 处` 只有 107 px。而「Tab 接词 → X」按一下 Tab 就自然可见，次数才是用户
; 在按 Tab **之前**唯一拿不到的信息（#204：选词仍是首现，没有可靠的消歧信号，只能告知）。
LfUpdateChainHint() {
    global G_LfC, G_LfData, G_LfPal, G_LfFootRow, G_LfChainHint
    if (!G_LfFootRow || !G_LfC.HasOwnProp("footHint") || !IsObject(G_LfData))
        return
    raw := G_LfC.word.Value
    ; 快速出口：既不在顺句态（框尾无空白）当前也没挂标签 → 这个键与次数提示无关。
    if (G_LfChainHint = "" && !RegExMatch(raw, "\s$"))
        return
    ; 只有**命中态**的纸面是真原句：空态与未命中的纸面是说明文字（中文占位 / 斜体），
    ; 拿它数次数只会给出一个假标签 —— 宁可不提示。
    lbl := "", paper := ObjProp(G_LfData, "paperText")
    hit := (ObjProp(G_LfData, "state") = "hit" && !ObjProp(G_LfData, "italic", false))
    if (RegExMatch(raw, "\s$") && hit && paper != "")
        lbl := ChainOccurrenceText(ChainStep(paper, raw))
    if (lbl = G_LfChainHint)
        return                      ; 值没变：一个字都不碰（每键 Move 一次会闪）
    G_LfChainHint := lbl
    G_LfData.chainHint := lbl       ; 同步数据：下一次 LfRender（定位回包）按它还原
    C := G_LfC, pal := G_LfPal, row := G_LfFootRow
    C.footHint.Text := (lbl != "") ? lbl : ObjProp(G_LfData, "hintRight")
    w := Min(LfTextWidth(C.footHint, C.footHint.Text), row.maxW)
    C.footHint.Move(row.right - w, row.y, w, row.h)
    LfStyleCtl(C.footHint, (lbl != "") ? pal.ink2 : pal.ink4, pal.panel)
}

; 打字路径的 chip 刷新（Bug 3）：只动 chip，不重建窗口、不重画原句。
; 文案走 Logic 的 KindLabel（词 / 短语 / 句）—— 旧实现把枚举 word/phrase/sentence
; 直接贴上去，用户看到的是 "phra…"。
; **空态不归这里**：空串在 LfOnTargetChange 就早退到 LfShowEmptyState / LfRender 收口
; （那边把 chip 置隐藏）；本函数只处理"有词"的非空状态，所以 label 恒非空。
LfUpdateKindChip(kind) {
    global G_LfC, G_LfPal
    if (!G_LfC.HasOwnProp("kind") || !G_LfPal)
        return
    label := KindLabel(kind)
    visible := (label != "")
    ; 有词了：占位提示/强调色光标都不该再露出来（它们只在空态显示）
    G_LfC.wordPh.Visible := false
    G_LfC.caret.Visible := false
    if (G_LfC.kind.Text = label && G_LfC.kind.Visible = visible)
        return
    G_LfC.kind.Text := label
    G_LfC.kind.Visible := visible
    LfStyleCtl(G_LfC.kind, G_LfPal.accent, G_LfPal.accentSoft)
    ; chip 宽度变了 → 词框要让位：复用 LfLayout（唯一的一套布局，不另起一份）
    LfFit()
}

LfLocateFire() {
    global G_LfTarget, G_LfLocateDeb, G_LongDocId, G_LfSegIndex
    word := LookupDueWord(G_LfLocateDeb, G_LfTarget.word)
    if (word = "")
        return
    G_LfLocateDeb := { word: word, at: A_TickCount }
    if (!G_LongDocId) {
        Log("长文：还没有文章，定位跳过（保存照常可用）")
        return
    }
    req := '{"cmd":"article_locate","doc_id":' . G_LongDocId
         . ',"target":' . JsonString(word)
         . ',"current_passage":' . (G_LfSegIndex - 1) . '}'
    ; 代次随 ask 入队：回话按发出顺序到，但应用时要再确认还是当前这次定位（连打快时
    ; 旧词回话可能在新词之后才被处理）。gen 不符 = 迟到回包，整条丢弃。
    if (!HubSend(req, "lf_locate", G_LfLocateGen))
        Log("长文：中枢不在位，定位不可用（仍可手打原句保存）")
}

LfOnLocateLine(line, gen := 0) {
    global G_LfTarget, G_LfLocateGen, G_LfSegIndex, G_LfLastFound, G_LfLastLocate
    ; 归属门：这条回话是不是**当前这次**定位（换过词/又发过新的 → gen 对不上，丢弃）
    if (gen != "" && gen + 0 != G_LfLocateGen) {
        Log("长文：丢弃迟到的 locate 回包（代次 " . gen . " ≠ " . G_LfLocateGen . "）")
        return
    }
    if (!JsonBool(line, "ok")) {
        Log("长文：article_locate 失败：" . JsonField(line, "error"))
        return
    }
    ; 回话词与当前词不符（用户又改了）→ 丢弃
    found := JsonBool(line, "found")
    if (!found) {
        LfApplyNoMatch()
        return
    }
    sentence := JsonField(line, "sentence")
    ; hub 契约（规格 §6 / 架构 §3.2）：target_start/target_end 是**全文绝对偏移**，
    ; 而 sentence 是其中一句 —— 句内偏移必须减 sentence_start。本机 E2E 实测：
    ; sentence 长 128、target_start=434（绝对），句内 57。之前直接把绝对值当句内偏移，
    ; span 越界被 RichEdit 钳到文末 → 选中区为空 → **高亮根本没画出来**（主控发现）。
    ss := JsonNumber(line, "sentence_start")
    tsRaw := JsonNumber(line, "target_start")
    teRaw := JsonNumber(line, "target_end")
    ts := tsRaw - ss
    te := teRaw - ss
    pi := JsonNumber(line, "passage_index")
    if (sentence = "" || te <= ts || ts < 0 || te > StrLen(sentence)) {
        Log("长文：locate 偏移不可用（sentence=" . StrLen(sentence) . " 字，绝对值 "
          . tsRaw . ".." . teRaw . "，sentence_start=" . ss . "）→ 按未命中处理")
        LfApplyNoMatch()
        return
    }
    G_LfLastFound := true
    G_LfLastLocate := { found: true, sentence: sentence, target_start: ts, target_end: te
                      , passage_index: pi, occurrences: 0
                      , sentence_start: ss, target_start_abs: tsRaw, target_end_abs: teRaw }
    if (pi >= 0)
        G_LfSegIndex := pi + 1
    LfApplyHit(sentence, ts, te)
}

LfApplyHit(sentence, ts, te) {
    global G_LfData, G_LfSavedWords, G_LfSavedCount, G_LfSegIndex, G_LongPassageCount, G_LfE2ELocateHits
    global G_LfTarget
    G_LfE2ELocateHits += 1
    d := G_LfData
    d.state := "hit"
    ; 回包只驱动**展示**：词/类型以用户当前输入为准（LfOnTargetChange 已写 G_LfTarget），
    ; 绝不用回包里的旧词反向覆盖输入框（Bug 2a/3：迟到回包不得覆盖用户新输入）。
    d.word := G_LfTarget.word
    d.kind := G_LfTarget.kind
    d.paperText := sentence
    d.italic := false, d.paperFg := ""
    d.runs := LfMarkRuns(sentence, ts, te, G_LfSavedWords)
    d.segIndex := G_LfSegIndex
    d.segLabel := G_LongPassageCount . " 段 · 第 " . G_LfSegIndex . " 段"
    d.segFill := (G_LongPassageCount > 1) ? ((G_LfSegIndex - 1) / (G_LongPassageCount - 1)) : 1
    if (G_LfSavedCount > 0)
        d.note := { kind: "ok", text: "本篇已存 " . G_LfSavedCount . " 词" }
    else
        d.note := { kind: "none", text: "" }
    d.outlineRows := LfOutlineRows()
    LfRender(d)
    ; #205：纸面句子换了 → 次数标签按**新句**重算（输入串没变，句变了，次数就会变）。
    ; 回包本身不碰词框，这里只做一次「文本 + 宽度」的局部更新。
    LfUpdateChainHint()
    LfScheduleLookup()
}

LfApplyNoMatch() {
    global G_LfData, G_LfLastFound, G_LfLastLocate, G_LfE2ELocateMiss, G_LfTarget
    G_LfE2ELocateMiss += 1
    G_LfLastLocate := { found: false, sentence: "", target_start: -1, target_end: -1
                      , passage_index: -1, occurrences: 0 }
    d := G_LfData
    G_LfLastFound := false
    d.state := "nomatch"
    ; 同上：未命中也要让 chip 跟着**当前输入**（旧实现不更新 kind → stale）
    d.word := G_LfTarget.word
    d.kind := G_LfTarget.kind
    d.paperText := LfNoMatchHint(G_LfTarget.word)
    d.runs := [{ text: d.paperText, mark: "" }]
    d.italic := true, d.paperFg := "hint"
    d.note := { kind: "nomatch", text: "未命中原文 · 仍可保存" }
    LfRender(d)
    LfUpdateChainHint()             ; #205：纸面已变成斜体说明 → 次数标签必须撤掉
    LfScheduleLookup()
}

; 当前词上「荧光笔重笔触」，本句里已存过的词「淡笔触」；非高亮正文不灰化。
LfMarkRuns(sentence, ts, te, savedWords) {
    ; 守卫：越界的 span 会让 EM_SETSEL 被 RichEdit 钳到文末（选到空区 → 没有高亮）。
    ; 宁可不高亮也要留一行日志，别让"看起来有高亮"掩盖偏移错误。
    if (ts < 0 || te <= ts || te > StrLen(sentence)) {
        Log("长文：高亮区间越界，降级为不高亮（ts=" . ts . " te=" . te
          . " 句长=" . StrLen(sentence) . "）")
        return [{ text: sentence, mark: "" }]
    }
    spans := []
    for w in savedWords {
        if (w = "" || StrLower(w) = StrLower(G_LfTarget.word))
            continue
        p := InStr(sentence, w)
        if (!p)
            continue
        ps := p - 1, pe := ps + StrLen(w)
        if (pe <= ts || ps >= te)          ; 与当前词重叠的不再叠一层
            spans.Push({ s: ps, e: pe, mark: "soft" })
    }
    spans.Push({ s: ts, e: te, mark: "cur" })
    ; 按起点排序后拼 runs
    spans := LfSortSpans(spans)
    runs := [], pos := 0
    for sp in spans {
        if (sp.s > pos)
            runs.Push({ text: SubStr(sentence, pos + 1, sp.s - pos), mark: "" })
        runs.Push({ text: SubStr(sentence, sp.s + 1, sp.e - sp.s), mark: sp.mark })
        pos := sp.e
    }
    if (pos < StrLen(sentence))
        runs.Push({ text: SubStr(sentence, pos + 1), mark: "" })
    return runs
}

LfSortSpans(spans) {
    ; 简单插入排序（spans ≤ 十来个）
    i := 2
    while (i <= spans.Length) {
        cur := spans[i], j := i - 1
        while (j >= 1 && spans[j].s > cur.s) {
            spans[j + 1] := spans[j]
            j--
        }
        spans[j + 1] := cur
        i++
    }
    return spans
}

; 音标 / 释义：复用旧框同一个 hub lookup RPC 与纯函数覆盖规则（V 的等价逻辑在面板侧）
LfScheduleLookup() {
    global G_LfTarget, G_LfLookupDeb, G_LfC
    word := G_LfTarget.word
    if (word = "")
        return
    st := DebounceStep(G_LfLookupDeb, word, A_TickCount, LOOKUP_DEBOUNCE_MS)
    G_LfLookupDeb := st.state
    if (st.schedule)
        SetTimer(LfLookupFire, -LOOKUP_DEBOUNCE_MS)
}

LfLookupFire() {
    global G_LfTarget, G_LfLookupDeb, G_LfE2ELookupWord, G_LfE2ELookupRaw, G_LfE2ELookupReq
    word := LookupDueWord(G_LfLookupDeb, G_LfTarget.word)
    if (word = "")
        return
    G_LfLookupDeb := { word: word, at: A_TickCount }
    G_LfE2ELookupWord := word
    G_LfE2ELookupRaw := ""
    G_LfE2ELookupReq := HubLookupRequestFast(word, IsMultiwordTarget(word))
    if (HubSend(G_LfE2ELookupReq, "lf_lookup")) {
        Log("长文：lookup 已发出（" . StrLen(word) . " 字）")
        return
    }
    ; 票 #185（A13）：发送失败收口与主框 LookupFire 同构 —— 去抖状态归零，hub 恢复
    ; 后同词能重新排程（面板没有 Ctrl+L，唯一入口就是这条去抖路径）。
    G_LfLookupDeb := LookupDebOnSendFailed(G_LfLookupDeb, word)
}

; v22 P5（#169 / spec §11）：多词目标的释义区口径。
; 未命中词典 → 释义区灰字「短语 · 见原句」/「句 · 见原句」、meaning 留空（spool 行
; 的 meaning 为空）；命中词典（回包带 dict_only:true）→ 正常填义、kind 不变。
; 这里同时清掉上一条残留的释义/音标/词典快照（防串义），主框侧同一口径在
; 英语取词.ahk 的 OnLookupLine / OnTargetChange。
LfShowMultiwordMeaningHint() {
    global G_LfTarget, G_LfData, G_LfC, G_LfPal
    if (!IsMultiwordTarget(G_LfTarget.word))
        return
    G_LfTarget.meaning := "", G_LfTarget.phonetic := "", G_LfTarget.dictJson := ""
    G_LfTarget.meaningSource := ""
    text := MultiwordMeaningHint(G_LfTarget.kind)
    d := G_LfData
    if (IsObject(d)) {
        d.meaning := "", d.phonetic := ""
        d.meaningHint := true
        d.meaningText := text
    }
    ; 打字路径不跑整窗渲染（与 chip 的轻量更新同一纪律）：直接更新释义控件
    if (IsObject(G_LfC) && G_LfC.HasOwnProp("meaning")) {
        G_LfC.meaning.Value := text
        if (IsObject(G_LfPal))
            LfStyleCtl(G_LfC.meaning, G_LfPal.meaningHint, G_LfPal.panel)
    }
}

; 从多词目标改回单词（或任何需要清掉多词命中残留的时机）：释义/音标/词典快照一起清空
; （面板没有「手打释义」概念，多词命中的释义只属于那条短语，不能带给下一个词）。
LfClearMeaningHint() {
    global G_LfTarget, G_LfData, G_LfC, G_LfPal
    G_LfTarget.meaning := "", G_LfTarget.phonetic := "", G_LfTarget.dictJson := ""
    G_LfTarget.meaningSource := ""
    d := G_LfData
    if (IsObject(d)) {
        d.meaning := "", d.phonetic := ""
        d.meaningHint := false
        d.meaningText := ""
    }
    if (IsObject(G_LfC) && G_LfC.HasOwnProp("meaning")) {
        G_LfC.meaning.Value := ""
        if (IsObject(G_LfPal))
            LfStyleCtl(G_LfC.meaning, G_LfPal.ink2, G_LfPal.panel)
    }
}

LfOnLookupLine2(line) {
    global G_LfTarget, G_LfData, G_LfE2ELookupRaw, G_LfLookupFollowedUp
    ; E2E 证据：原始回包原样留一份（只在带 LF_E2E_ACTIONS 的副本进程里写文件）
    if (EnvGet("LF_E2E_ACTIONS") != "")
        G_LfE2ELookupRaw := line
    if (!LookupWordMatches(JsonField(line, "word"), G_LfTarget.word))
        return
    ; v22 P5（#169 / spec §11）：多词目标只认 hub 的「整条短语词典命中」回包
    ; （dict_only:true）或**明确标注的机翻兜底**（machine:true，v22 修复 / #172）；
    ; 未命中 / 旧 hub 的无标记回包 → 释义留空 + 上下文提示，绝不显示
    ; 「whether the → 无论天气好不好」这种错义（单词目标口径不变）。
    machine := JsonBool(line, "machine")
    if (!JsonBool(line, "ok")
        || !LookupMeaningApplies(G_LfTarget.word, JsonBool(line, "dict_only"), machine)) {
        G_LfTarget.meaningSource := ""
        LfShowMultiwordMeaningHint()
        return
    }
    phonetic := JsonField(line, "phonetic")
    meaning := JsonField(line, "cn")
    if (meaning = "")
        meaning := JsonField(line, "en")
    if (phonetic != "")
        G_LfTarget.phonetic := phonetic
    if (meaning != "")
        G_LfTarget.meaning := meaning
    G_LfTarget.meaningSource := machine ? "machine" : ""
    G_LfTarget.dictJson := line
    d := G_LfData
    if (d.word = G_LfTarget.word) {
        d.phonetic := G_LfTarget.phonetic
        ; 机翻义在面板里直接带「机翻：」前缀显示（G_LfTarget.meaning 保持干净，
        ; 落库的 meaning 不带前缀；`d.meaning` 只用于展示，见 LfRender）。
        d.meaning := machine ? ("机翻：" . G_LfTarget.meaning) : G_LfTarget.meaning
        d.meaningHint := false
        LfRender(d)
    }
    ; v23 S6（§4.4）：面板补上旧框早就有的两段式二跳 —— provisional（本地先到的
    ; 预备释义）且非机翻、且这个词还没补过 → 同词补发一次 `full` 拿在线全量。
    ; 判定与旧框共用 Logic.LookupFollowUpWord；机翻/重复 provisional 不补发。
    ; 旧 hub 的回包不带 provisional → 空串 → 面板行为与 v22 逐字一致。
    ; 只记词长，不记目标词（README 日志纪律）。
    followed := LookupFollowUpWord(line, G_LfLookupFollowedUp)
    if (followed != "") {
        G_LfLookupFollowedUp := followed
        if (HubSend(HubLookupRequestFull(followed, IsMultiwordTarget(followed)), "lf_lookup"))
            Log("长文：provisional 已显示，二跳 full 已发出（" . StrLen(followed) . " 字）")
    }
}

; 空态：清空目标词后回到提示态（原句不保留）
LfShowEmptyState() {
    global G_LfData, G_LfTarget
    G_LfTarget.word := ""
    G_LfTarget.phonetic := ""
    G_LfTarget.meaning := ""
    G_LfTarget.dictJson := ""
    G_LfTarget.meaningSource := ""
    ; 空态的"必须写一次"也走收口（写空值 + 光标归位；不触发 Change，避免回环）
    LfWriteWord("")
    d := G_LfData
    d.state := "empty"
    d.word := "", d.phonetic := "", d.meaning := ""
    d.kind := G_LfTarget.kind            ; chip 文案跟着回到默认值（此时 chip 是隐藏的）
    d.meaningHint := true
    d.meaningText := (d.docId ? "打一个词，原句会自动出现在这里" : "按 Ctrl+Alt+4 框住正文，整页识别后在这里取词")
    d.paperText := (d.docId ? "原句会在命中时出现在这里 · 目标词会高亮" : "还没有文章。按 Ctrl+Alt+4 拖框圈住整块英文正文，整页识别完成后这里会出现原句。")
    d.runs := [{ text: d.paperText, mark: "" }]
    d.paperFg := "hint", d.italic := false
    d.note := { kind: "none", text: "" }
    LfRender(d)
}

; ============================================================================
;  保存：Enter → spool（带可选 doc_id/passage_index）→ HubFlush（旧框同一套离线语义）
; ============================================================================
LfSave(mode := "next") {
    global G_LfTarget, G_LfData, G_LfSavedCount, G_LfSavedWords, G_LongDocId
    global G_LfSegIndex, G_SpoolPath, G_LongApp, G_LongTitle, G_LongUrl
    global G_LfLocateDeb, G_LfLookupDeb
    word := NormalizeLine(G_LfTarget.word)
    if (word = "") {
        if (G_LfSavedCount >= 1)
            return LfClose()
        LfSetHintText("目标词不能为空", true)
        return
    }
    d := G_LfData
    ; v29 兜底：待存的释义快照必须属于当前目标词。换词清空之外，任何路径漏清
    ; （旧版卡在中间态、未来新入口）都不许把别的词的释义写进这一条。
    ; 老快照没有 word 键时不动它（只有能证明「不是这个词」才丢）。
    snapshotWord := (G_LfTarget.dictJson != "") ? JsonField(G_LfTarget.dictJson, "word") : ""
    if (snapshotWord != "" && !LookupWordMatches(snapshotWord, word)) {
        Log("长文：释义快照与目标词不符，已丢弃（防串义）")
        G_LfTarget.meaning := "", G_LfTarget.phonetic := ""
        G_LfTarget.dictJson := "", G_LfTarget.meaningSource := ""
    }
    line := SpoolLine({ target: word
                       , kind: G_LfTarget.kind
                       , context: (d.paperText != "" && !d.italic) ? d.paperText : ""
                       , meaning: G_LfTarget.meaning
                       , meaningSource: G_LfTarget.meaningSource
                       , phonetic: G_LfTarget.phonetic
                       , dictJson: G_LfTarget.dictJson
                       , app: G_LongApp, title: G_LongTitle, url: G_LongUrl
                       , position: "", tags: "", newSession: false
                       , docId: G_LongDocId
                       , passageIndex: (G_LongDocId ? (G_LfSegIndex - 1) : "") }, A_Now)
    if ((err := SpoolAppend(G_SpoolPath, line)) != "") {
        Log("长文：保存失败：" . err)
        try {
            A_Clipboard := SpoolClipText({ target: word, context: d.paperText
                , meaning: G_LfTarget.meaning, phonetic: G_LfTarget.phonetic
                , prov: G_LongTitle, pos: "" })
            LfSetHintText(err . "（整条内容已复制到剪贴板）", true)
        } catch {
            LfSetHintText(err, true)
        }
        return
    }
    ; E2E 证据：flush 之前先把 spool 行原文抓一份（hub 会很快 ingest 掉），只在 test 进程里做
    if (EnvGet("LF_E2E_ACTIONS") != "") {
        global G_LfE2ESpoolLine
        try G_LfE2ESpoolLine := line
    }
    Log("长文：已暂存一条（" . StrLen(line) . " 字节，doc_id=" . G_LongDocId
      . " passage_index=" . (G_LongDocId ? (G_LfSegIndex - 1) : "无") . "）")
    ; 已存词记进本句软笔触集合
    G_LfSavedWords.Push(word)
    G_LfSavedCount += 1
    HubFlush(word)                         ; 离线不算失败：spool 已落盘
    ; 存档后清词、保留原句、焦点回词框
    G_LfTarget.word := "", G_LfTarget.phonetic := "", G_LfTarget.meaning := ""
    G_LfTarget.dictJson := "", G_LfTarget.meaningSource := ""
    ; 票 #175（A1）：去抖状态随清词一起归零（B1 同款漏改，主框 ResetForNextWord 的
    ; `G_LookupDeb := 0`、面板空态路径 2910-2911 同口径）—— 否则「保存并继续」后重打
    ; **同一个词**时 DebounceStep 判 state.word = w → 不再排程 → 定位/释义永不回填。
    G_LfLocateDeb := 0, G_LfLookupDeb := 0
    d.word := ""
    LfWriteWord("")                        ; 清词也走收口（渲染只读不写，见 Bug 2a）
    ; 原句保留 + 已存词软笔触（当前词没了 → 只留 soft）
    if (d.paperText != "" && !d.italic)
        d.runs := LfMarkRuns(d.paperText, -1, -1, G_LfSavedWords)
    d.phonetic := "", d.meaning := "", d.meaningHint := true
    d.meaningText := "已存 " . G_LfSavedCount . " 词 · 继续打下一个"
    d.note := { kind: "ok", text: "本篇已存 " . G_LfSavedCount . " 词" }
    LfRender(d)
    LfFocusWord()
    if (mode = "close")
        LfClose()
}

; 面板内提示行：借用 note 行显示（只在需要时出现）
LfSetHintText(text, isErr := false) {
    global G_LfData
    G_LfData.note := { kind: isErr ? "warn" : "ok", text: text }
    LfRender(G_LfData)
}

; ============================================================================
;  Tab 补全 / 顺句接词 / Ctrl+Tab 强制接词
;  **判定一律复用 lib 纯函数**（TabNextAction / ChainText / ChainNextText / CompletionTiers），
;  口径与 main 的 CompTab/CompCtrlTab 完全一致 —— 面板不复制第二套状态机。
; ============================================================================
LfCompTab() {
    global G_LfC, G_LfTarget, G_LfData, G_Common, G_Known
    ; **原始文本**参与顺句判定：尾随空格是「该接下一个词了」的状态标记（main 语义）。
    ; NormalizeLine 会把尾随空白 Trim 掉（正是 `the ` 被补成 `there` 的旧病根），
    ; 只用于空判与候选。
    raw := G_LfC.word.Value
    word := NormalizeLine(raw)
    if (word = "")
        return
    sentence := (G_LfData.paperText != "" && !G_LfData.italic) ? G_LfData.paperText : ""
    ; 候选入口保持面板既有实测形状（bench_completion 量的就是这一行）
    t := CompletionTiers(sentence, word, G_Known, G_Common)
    cands := t.words
    act := TabNextAction(sentence, raw, cands, 0)
    if (act.action = "chain") {
        ; ChainText 的结果自带一个尾随空格（顺句状态标记，能连打）
        LfAcceptTarget(act.word)
        return
    }
    if (act.action = "accept") {
        ; 补完一个词补一个尾随空格（main 的 AcceptTarget(w . " ")）：它既是顺句状态，
        ; 也让用户接着手打下一个词时不用自己补空格。光标由收口移到末尾。
        LfAcceptTarget(act.word . " ")
        return
    }
    ; ring：面板没有字段环 —— 不写值、不聚焦，但**不许再静默**（#205）：按了 Tab 什么都
    ; 不发生，用户唯一需要的就是一句解释。两种「没反应」的成因必须分开说，否则用户会去
    ; 怀疑是 Tab 没生效：sentence 为空 = 这句根本没命中原文（没有词库可接）；有句子却
    ; 接不上 = 锚点对不上（照打即可，Ctrl+Tab 是强制接词那条路）。
    if (act.action = "ring") {
        if (sentence = "")
            ShowToast("未命中原文 · 无法补全", "err")
        else
            ShowToast("这句里接不上 · 照打即可（Ctrl+Tab 可强制接词）", "ok")
        return
    }
}

; `Ctrl+Tab` 强制接词（main 的 CompCtrlTab 面板版）：允许单 token、不要求尾随空格，
; 把框里已有的词串当锚点接上原句紧挨着的下一个词；接不上 → 静默 no-op。
LfCompCtrlTab() {
    global G_LfC, G_LfData
    raw := G_LfC.word.Value
    if (NormalizeLine(raw) = "")
        return
    sentence := (G_LfData.paperText != "" && !G_LfData.italic) ? G_LfData.paperText : ""
    txt := ChainNextText(sentence, raw)
    if (txt = "")
        return                      ; 定位失败 → 不改值、不移动焦点、不报错
    LfAcceptTarget(txt)
}

; 程序化写目标词框的**唯一收口**：写值 → 光标移到末尾。
; 为什么必须显式移光标：AHK 的 `Value :=`（WM_SETTEXT）会把光标重置到 0（main 同坑）。
; 它**不触发** Change：要同步状态/去抖的调用方自己调 LfOnTargetChange（LfAcceptTarget 做了）。
; 2026-10-26 实测（AHK v2.0.30，探针 4 种写法：Value:= 首次/同值/异值、SetWindowTextW）
; → Change 事件数全为 **0**，所以"程序写值不触发 Change"这条不是推测。
LfWriteWord(text) {
    global G_LfC
    if (!G_LfC.HasOwnProp("word"))
        return
    G_LfC.word.Value := text
    ; 只给**已经有真实宽度**的控件定位光标：首帧装载时词框还是 `w1 h1` 的占位尺寸，
    ; 此时 EM_SETSEL 到末尾会让单行 Edit 把文本横向滚出可视区（真机实测：文字整个不可见），
    ; 而且随后的 Move 不会复位滚动位置。首帧的末尾定位由用户聚焦时的 LfFocusWord 负责。
    cs := LfClientSize(G_LfC.word.Hwnd)
    if (cs.w > LfPx(8))
        SendMessage(0x00B1, 0x7FFFFFFF, 0x7FFFFFFF, , G_LfC.word.Hwnd)   ; EM_SETSEL → 末尾
}

; 接受候选 / 接词：写值（光标末尾）+ 手动推一次 Change。
; 为什么手动推：值是本程序写进去的，Edit 的 Change 不会触发（类型推断/去抖查询同效）。
LfAcceptTarget(text) {
    global G_LfC, G_LfTarget
    LfWriteWord(text)
    G_LfTarget.word := NormalizeLine(text)
    LfOnTargetChange()
}

; ============================================================================
;  E2E 钩子（**只在副本进程设置了环境变量 LF_E2E_ACTIONS 时才会被挂上**）
;
;  为什么需要它：阶段的真机验收要跑穿「真实 OCR → article_create → 面板 → 打词 →
;  article_locate → 高亮 → Enter → spool → hub records」，但用 SendInput 模拟鼠标
;  拖框 + 中文输入法在自动化环境下不稳定（本机踩过两次：遮罩起点采错、IME 候选框
;  吞掉 Enter）。这个钩子直接调**同一条链路的入口**（LongformCaptureWithRect /
;  LfOnTargetChange / LfSave），不复制第二套实现；正常用户进程没有这个环境变量，
;  行为与没有这段代码完全一样。
;
;  动作文件：一行一个 动作=参数，见 tools/英语取词/tests/run_longform_e2e.ps1
; ============================================================================
LfWriteLines(path, lines) {
    if (path = "")
        return
    try {
        SplitPath(path, , &dir)
        if (dir != "" && !DirExist(dir))
            DirCreate(dir)
        try FileDelete(path)
        f := FileOpen(path, "w", "UTF-8")
        for l in lines
            f.WriteLine(l)
        f.Close()
    }
}

JsArrOf(rows) {
    out := ""
    for r in rows
        out .= (out = "" ? "" : " | ") . r.n . ":" . SubStr(r.t, 1, 24)
    return out
}

; E2E 专用：OS 级按下一组 VK（含修饰键），按相反顺序释放。
; 为什么不用 Send：AHK 的 Send 默认不触发本进程热键（SendLevel），测不到真实入口。
LfE2EPressKeys(vks) {
    for vk in vks {
        DllCall("user32\keybd_event", "UChar", vk, "UChar", 0, "UInt", 0, "UPtr", 0)
        Sleep(40)
    }
    i := vks.Length
    while (i >= 1) {
        DllCall("user32\keybd_event", "UChar", vks[i], "UChar", 0, "UInt", 2, "UPtr", 0)
        Sleep(30)
        i -= 1
    }
}

LfE2EOnError(err, mode) {
    ; 只在 LF_E2E_ACTIONS 门后由 LfE2ERun 注册：把运行时错误落进结果文件再退出，
    ; 不弹阻塞式错误框（否则自动化跑挂死到超时，什么证据都拿不到）。
    Log("E2E 运行时错误：" . err.Message . "（" . err.What . " " . err.File . ":" . err.Line . "）")
    try LfWriteLines(EnvGet("LF_E2E_RESULT"), ["e2e_error=" . err.Message
        , "e2e_error_what=" . err.What, "e2e_error_line=" . err.Line])
    ExitApp 2
}

LfE2EPickWord() {
    global G_LongOutline
    ; 从真实 OCR 结果的段落预览里挑一个够长的词（**不硬编码**目标词）
    for row in G_LongOutline {
        if (RegExMatch(row.preview, "[A-Za-z]{7,}", &m))
            return StrLower(m[0])
    }
    return ""
}

LfE2ESetWord(w) {
    global G_LfC
    if (w = "")
        return
    G_LfC.word.Value := w
    LfOnTargetChange()          ; 与真实按键走同一个 Change 处理（含 250ms 去抖）
}

; ---------------------------------------------------------------------------
;  第二轮修复（Bug 1–4）的 E2E 探针。全部只在 `LF_E2E_ACTIONS` 门后；
;  正常用户进程没有这个环境变量 → 这些函数不会被调用、行为与没有它们一致。
; ---------------------------------------------------------------------------

; 见证用的转义：空格写成 ·（尾随空格要一眼看得见 —— 那是顺句状态标记）。
LfVisText(text) {
    t := StrReplace(text, " ", "·")
    t := StrReplace(t, "`r", "")
    t := StrReplace(t, "`n", "⏎")
    return t
}

; 词框的选区（EM_GETSEL）：光标在不在末尾、有没有选中区都靠它。
LfE2EEmSel(ctl) {
    ps := Buffer(4, 0), pe := Buffer(4, 0)
    DllCall("SendMessageW", "Ptr", ctl.Hwnd, "UInt", 0x00B0, "Ptr", ps, "Ptr", pe)
    return { start: NumGet(ps, 0, "UInt"), end: NumGet(pe, 0, "UInt") }
}

; 光标证据（Bug 2a）：词框文本 + EM_GETSEL 起点/终点 + 是否在末尾 + 尾随空格。
LfE2ECaretInfo(path) {
    global G_LfC, G_LfTarget
    v := G_LfC.word.Value
    caret := LfE2EEmSel(G_LfC.word)
    LfWriteLines(path, ["word_value=" . LfVisText(v)
        , "value_len=" . StrLen(v)
        , "caret_start=" . caret.start
        , "caret_end=" . caret.end
        , "caret_at_end=" . ((caret.start = StrLen(v) && caret.end = StrLen(v)) ? 1 : 0)
        , "ends_with_space=" . ((v != "" && SubStr(v, -1) = " ") ? 1 : 0)
        , "target_word=" . LfVisText(G_LfTarget.word)
        , "target_kind=" . G_LfTarget.kind
        , "chip_text=" . G_LfC.kind.Text
        , "chip_visible=" . (G_LfC.kind.Visible ? 1 : 0)])
}

; 词框/释义行的实测几何与像素（Bug 2b/2c/3/4 的证据源）：
;   · 词框墨迹（字形真的画出来了吗、下伸部有没有被裁）；
;   · chip 文案/显隐/实测宽（按文本量宽，不再写死 24）；
;   · 释义是否从 x0 起占满 innerW（音标撤掉后必须整行）；
;   · 面板里是否真的没有音标控件，而数据层仍带 phonetic。
LfE2EWordInfo(path) {
    global G_LfC, G_LfGui, G_LfTarget
    cw := LfClientSize(G_LfGui.Hwnd)
    x0 := LfPx(LF.PAD)
    innerW := cw.w - 2 * x0
    ; 词框与面板的屏幕矩形差 = 控件在窗口内的实际摆位（含厚边框偏移）
    wr := LfWindowRect(G_LfC.word.Hwnd)
    mr := LfWindowRect(G_LfC.meaning.Hwnd)
    gr := LfWindowRect(G_LfGui.Hwnd)
    cr := LfWindowRect(G_LfC.kind.Hwnd)
    kwpx := (NormalizeLine(G_LfC.word.Value) != "") ? LfTextWidth(G_LfC.word, G_LfC.word.Value) : 0
    wimg := LfGrabCtl(G_LfC.word)
    wp := LfInkProfile(wimg)
    caret := LfE2EEmSel(G_LfC.word)
    LfWriteLines(path, ["panel_client_w=" . cw.w
        , "panel_inner_w=" . innerW
        , "word_value=" . LfVisText(G_LfC.word.Value)
        , "word_len=" . StrLen(G_LfC.word.Value)
        , "caret_start=" . caret.start
        , "caret_end=" . caret.end
        , "caret_at_end=" . ((caret.start = StrLen(G_LfC.word.Value) && caret.end = StrLen(G_LfC.word.Value)) ? 1 : 0)
        , "word_ctl_w=" . wimg.w
        , "word_ctl_h=" . wimg.h
        , "word_ink_total=" . wp.total
        , "word_ink_lowest_row=" . wp.lowest
        , "word_ink_bottom_band=" . LfCountInkBand(wimg, Round(wimg.h * 0.62), wimg.h)
        , "word_rel_x=" . (wr.x - gr.x)
        , "word_rel_y=" . (wr.y - gr.y)
        , "kw_px=" . kwpx
        , "chip_rel_x=" . (cr.x - gr.x)
        , "chip_rel_y=" . (cr.y - gr.y)
        , "chip_clears_text=" . (((cr.x - gr.x) >= (wr.x - gr.x) + kwpx + LfPx(9) - 1) ? 1 : 0)
        , "target_kind=" . G_LfTarget.kind
        , "kind_label=" . KindLabel(G_LfTarget.kind)
        , "chip_text=" . G_LfC.kind.Text
        , "chip_visible=" . (G_LfC.kind.Visible ? 1 : 0)
        , "chip_text_w=" . LfTextWidth(G_LfC.kind, G_LfC.kind.Text)
        , "meaning_rel_x=" . (mr.x - gr.x)
        , "meaning_w=" . mr.w
        , "meaning_full_width=" . ((mr.w >= innerW - LfPx(2)) ? 1 : 0)
        , "no_phonetic_control=" . ((!G_LfC.HasOwnProp("phon")) ? 1 : 0)
        , "data_phonetic_len=" . StrLen(G_LfTarget.phonetic)])
}

; chip 跟手（Bug 3 回归）：**同一个 kind 内**文本逐键变长时，chip 的 x 必须跟着文本宽
; kw 右移（旧实现只在 kind 变化时才重摆，同 kind 内 chip 停在旧位置、可能压住输入文字），
; 且 chip 左缘始终不侵入文字占位：chip.x ≥ word.x + kw + gap - 1。
LfE2EChipTrack(path) {
    global G_LfC, G_LfGui, G_LfTarget
    lines := []
    gr := LfWindowRect(G_LfGui.Hwnd)
    for t in ["res", "rese", "resea", "resear", "research"] {
        LfWriteWord(t)
        LfOnTargetChange()          ; 与真实敲键同一个 Change 路径
        wr := LfWindowRect(G_LfC.word.Hwnd)
        cr := LfWindowRect(G_LfC.kind.Hwnd)
        kw := LfTextWidth(G_LfC.word, G_LfC.word.Value)
        lines.Push("value=" . t
            . "|kind=" . G_LfTarget.kind
            . "|chip_text=" . G_LfC.kind.Text
            . "|word_rel_x=" . (wr.x - gr.x)
            . "|chip_rel_x=" . (cr.x - gr.x)
            . "|kw_px=" . kw
            . "|chip_clears_text=" . (((cr.x - gr.x) >= (wr.x - gr.x) + kw + LfPx(9) - 1) ? 1 : 0))
    }
    LfWriteLines(path, lines)
}

; chip 四态（Bug 3）：word → phrase → sentence → 空（隐藏）。每步都走**真实 Change 路径**
; （LfOnTargetChange），不是直接把 chip 文案贴上去。
LfE2EChipStates(path) {
    global G_LfC, G_LfTarget
    lines := []
    for t in ["reluctant", "reluctant pattern", "one two three four five six", ""] {
        LfWriteWord(t)
        LfOnTargetChange()
        lines.Push("value=" . LfVisText(t)
            . "|infer_kind=" . G_LfTarget.kind
            . "|chip=" . G_LfC.kind.Text
            . "|chip_visible=" . (G_LfC.kind.Visible ? 1 : 0)
            . "|chip_text_w=" . LfTextWidth(G_LfC.kind, G_LfC.kind.Text))
    }
    LfWriteLines(path, lines)
}

; g/y/j/p/q 下伸部（Bug 2b）：同一控件、同一字体下「无下伸部 vs 全下伸部」的差分像素证据。
; "test" 最下墨行一定比 "gyp" 高；"gyp" 的最下墨行必须**不到控件底边**（否则就是被裁了）。
; 两组都**实测**（对照组也调 LfCountInkBand —— 证据文件里的数字必须是量出来的，不许写常量）。
LfE2EDescenderInfo(path) {
    global G_LfC
    keep := G_LfC.word.Value
    G_LfC.word.Value := "test"
    Sleep(220)
    aimg := LfGrabCtl(G_LfC.word)
    a := LfInkProfile(aimg)
    aband := LfCountInkBand(aimg, Round(aimg.h * 0.62), aimg.h)
    G_LfC.word.Value := "gyp"
    Sleep(220)
    bimg := LfGrabCtl(G_LfC.word)
    b := LfInkProfile(bimg)
    band := LfCountInkBand(bimg, Round(bimg.h * 0.62), bimg.h)
    ; 走收口还原（保持光标在末尾；直接赋 Value 会把光标塌到 0，污染后面的 caret 证据）
    LfWriteWord(keep)
    Sleep(150)
    LfWriteLines(path, ["ctl_h=" . bimg.h
        , "no_descender_lowest_row=" . a.lowest
        , "no_descender_bottom_band_ink=" . aband
        , "with_descender_lowest_row=" . b.lowest
        , "with_descender_bottom_band_ink=" . band
        , "with_descender_total_ink=" . b.total
        , "clipped_at_bottom=" . ((b.lowest >= bimg.h - 1) ? 1 : 0)])
}

; Tab 逐词接句 / Ctrl+Tab 强制接词（Bug 1）的**行为**证据。
; 目标词从真实句子里挑（不硬编码），由面板真实的 LfCompTab / LfCompCtrlTab 驱动，
; 每步断言：长度增长、以空格结尾、光标在末尾、接出来的串确实是原句的一截。
LfE2ETabChain(path) {
    global G_LfC, G_LfData
    lines := []
    sent := (G_LfData.paperText != "" && !G_LfData.italic) ? G_LfData.paperText : ""
    lines.Push("sentence=" . SubStr(sent, 1, 200))
    if (!RegExMatch(sent, "[A-Za-z]{5,}", &m)) {
        LfWriteLines(path, ["PASS=0", "reason=no_5letter_word_in_sentence"])
        return
    }
    anchor := m[0]
    stem := SubStr(anchor, 1, 3)
    lines.Push("anchor=" . anchor . " stem=" . stem)
    ; --- Tab：半截词 → 补全+空格 → 再连按两次接词 ---
    LfWriteWord(stem)
    LfOnTargetChange()
    Sleep(300)                     ; 让半截词的 locate 回包按真实时序到（不该再动输入框）
    loop 3 {
        before := G_LfC.word.Value
        LfCompTab()
        after := G_LfC.word.Value
        caret := LfE2EEmSel(G_LfC.word)
        lines.Push("tab_step=" . A_Index
            . "|before=" . LfVisText(before)
            . "|after=" . LfVisText(after)
            . "|len_from=" . StrLen(before)
            . "|len_to=" . StrLen(after)
            . "|grew=" . ((StrLen(after) > StrLen(before)) ? 1 : 0)
            . "|ends_space=" . ((SubStr(after, -1) = " ") ? 1 : 0)
            . "|caret_end=" . ((caret.end = StrLen(after)) ? 1 : 0)
            . "|in_sentence=" . ((InStr(sent, RTrim(after)) > 0) ? 1 : 0))
    }
    tabFinal := G_LfC.word.Value
    ; --- Ctrl+Tab：整词锚点、无尾随空格也能接（main 的强制接词手感）---
    LfWriteWord(anchor)
    LfOnTargetChange()
    Sleep(300)
    before := G_LfC.word.Value
    LfCompCtrlTab()
    after := G_LfC.word.Value
    caret := LfE2EEmSel(G_LfC.word)
    lines.Push("ctrl_tab|before=" . LfVisText(before) . "|after=" . LfVisText(after)
        . "|grew=" . ((StrLen(after) > StrLen(before)) ? 1 : 0)
        . "|ends_space=" . ((SubStr(after, -1) = " ") ? 1 : 0)
        . "|caret_end=" . ((caret.end = StrLen(after)) ? 1 : 0)
        . "|starts_with_anchor=" . ((InStr(after, anchor) = 1) ? 1 : 0))
    ; --- 负样例：整句当锚点 = 原句走完 → 静默 no-op（不改值、不报错）---
    LfWriteWord(sent)
    LfOnTargetChange()
    Sleep(300)
    before := G_LfC.word.Value
    LfCompCtrlTab()
    lines.Push("ctrl_tab_noop|unchanged=" . ((G_LfC.word.Value = before) ? 1 : 0))
    ; 收尾：回到 Tab 接出来的那句（后面的截图看得到"顺句"形态）
    LfWriteWord(tabFinal)
    LfOnTargetChange()
    lines.Push("restored=" . LfVisText(G_LfC.word.Value))
    LfWriteLines(path, lines)
}

; 真实 OCR 结果的段落预览（证明面板里是自渲染测试窗口那段文字，不是硬编码）。
; OCR 档的 `G_LongPassages` 是**偏移对**（{s,e}）不是文本，所以文本从 G_LongOutline 的
; preview 里取（与面板"全文"目录同一来源），偏移照实记下来。
LfE2EPassages(path) {
    global G_LongPassages, G_LongOutline, G_LongArticleFormat, G_LongDocId, G_LongQuality
    lines := ["doc_id=" . G_LongDocId, "format=" . G_LongArticleFormat
        , "quality=" . G_LongQuality]
    for i, p in G_LongPassages {
        if (IsObject(p))
            lines.Push("passage_" . i . "_span=" . ObjProp(p, "s") . ".." . ObjProp(p, "e"))
        else
            lines.Push("passage_" . i . "=" . SubStr(p, 1, 240))
    }
    for i, row in G_LongOutline
        lines.Push("outline_" . i . "=" . SubStr(row.preview, 1, 120))
    LfWriteLines(path, lines)
}

LfE2ERun() {
    global G_Ready, G_HubUp, G_LfGui, G_LfData, G_LfC, G_LongLog, G_LfE2ESpoolLine
    global G_LfLastLocate, G_LongDocId, G_LongPassageCount, G_LongPageChars, G_LongQuality
    global G_LongOcrMs, G_LongEscalated, G_LfE2ET0, G_LfSavedCount, G_LfTarget, G_LfPal
    global G_LfOutlineRowsCache, G_LfOutlineShown, G_LfOutlineGrewH, G_LfInspGui
    global G_LongArticleFormat, G_LfSourceTitle, G_LongApp, G_LfKeysOn
    path := EnvGet("LF_E2E_ACTIONS")
    if (path = "" || !FileExist(path))
        return
    ; E2E 兜底：动作里的运行时错误不许弹阻塞式错误框（无人点击 → 证据跑挂死）。
    OnError(LfE2EOnError)
    out := []
    ; 等内核/中枢就绪（副本是冷启动）
    loop 300 {
        if (G_Ready && G_HubUp)
            break
        Sleep(100)
    }
    out.Push("ready=" . ((G_Ready && G_HubUp) ? 1 : 0))
    lines := StrSplit(RTrim(FileRead(path, "UTF-8"), "`r`n"), "`n", "`r")
    for raw in lines {
        line := Trim(raw)
        if (line = "" || SubStr(line, 1, 1) = "#")
            continue
        if (RegExMatch(line, "^([A-Za-z_]+)=(.*)$", &m))
            act := m[1], arg := m[2]
        else
            act := line, arg := ""
        ; E2E 诊断：每个动作落一行日志（只在 LF_E2E_ACTIONS 门后跑，正常进程没有这段）
        Log("E2E 动作：" . act)
        ; v21 round2 (B)：session 出处 E2E 动作（见文件末尾 LfE2EProvAction；
        ; 同样只在 LF_E2E_ACTIONS 门后，正常用户进程没有这个环境变量）
        if (LfE2EProvAction(act, arg, out))
            continue
        if (act = "rect") {
            q := StrSplit(arg, ",")
            LongformCaptureWithRect({ x: q[1] + 0, y: q[2] + 0, w: q[3] + 0, h: q[4] + 0 })
        } else if (act = "wait_panel") {
            ok := false
            loop 300 {
                if (G_LfGui && IsObject(G_LfData) && G_LfData.docId > 0) {
                    ok := true
                    break
                }
                if (G_LongPhase = "idle" && !G_LfGui)
                    break
                Sleep(100)
            }
            out.Push("panel_ok=" . (ok ? 1 : 0))
        } else if (act = "info") {
            LfWriteLines(arg, ["doc_id=" . G_LongDocId
                , "passage_count=" . G_LongPassageCount
                , "page_chars=" . G_LongPageChars
                , "quality=" . G_LongQuality
                , "escalated=" . (G_LongEscalated ? 1 : 0)
                , "ocr_kernel_ms=" . G_LongOcrMs
                , "chain_ms=" . (A_TickCount - G_LfE2ET0)
                , "panel_seg_label=" . G_LfData.segLabel
                , "article_format=" . G_LongArticleFormat
                , "outline_rows=" . G_LongOutline.Length
                , "timeline=" . G_LongLog])
        } else if (act = "paste_file") {
            txt := FileRead(arg, "UTF-8")
            out.Push("paste_chars=" . StrLen(txt))
            ; v21 round2 (B)：与真实 Ctrl+V 同一条采集路径 —— 否则直接调 LfPasteText
            ; 会带上一篇的 stale 出处（Lead review 2026-10-26 第 2 点，供 verifier 复跑）
            PanelFillLongProvenance()
            LfPasteText(txt)
        } else if (act = "word_auto") {
            w := LfE2EPickWord()
            out.Push("picked_word=" . w)
            LfE2ESetWord(w)
        } else if (act = "word") {
            out.Push("set_word=" . arg)
            LfE2ESetWord(arg)
        } else if (act = "raw") {
            ; 精确写值（`\s` = 空格）：动作文件的行尾会被 Trim，尾随空格只能这样表达。
            txt := StrReplace(arg, "\s", " ")
            LfWriteWord(txt)
            LfOnTargetChange()          ; 与真实敲键同一个 Change 处理（含去抖）
            out.Push("raw=" . LfVisText(G_LfC.word.Value))
        } else if (act = "tab") {
            LfCompTab()
            out.Push("tab_value=" . LfVisText(G_LfC.word.Value))
        } else if (act = "ctrl_tab") {
            LfCompCtrlTab()
            out.Push("ctrl_tab_value=" . LfVisText(G_LfC.word.Value))
        } else if (act = "late_hit") {
            ; 模拟「定位回包迟到」：回包里的词是 arg（旧词），而框里已是用户更新的文本。
            ; LfApplyHit 不再收回包词（一律以 G_LfTarget 为准）→ 输入框/光标都不该动；
            ; 旧实现会按回包词重写 Value → 光标塌回 0。
            sent := (G_LfData.paperText != "" ? G_LfData.paperText : arg)
            LfApplyHit(sent, 0, StrLen(arg))
            out.Push("late_hit=" . arg . " box_after=" . LfVisText(G_LfC.word.Value))
        } else if (act = "caret_info") {
            LfE2ECaretInfo(arg)
        } else if (act = "word_info") {
            LfE2EWordInfo(arg)
        } else if (act = "chip_states") {
            LfE2EChipStates(arg)
        } else if (act = "chip_track") {
            LfE2EChipTrack(arg)
        } else if (act = "descender_info") {
            LfE2EDescenderInfo(arg)
        } else if (act = "tab_chain_info") {
            LfE2ETabChain(arg)
        } else if (act = "passages") {
            LfE2EPassages(arg)
        } else if (act = "word_shot") {
            r := LfWindowRect(G_LfC.word.Hwnd)
            out.Push("word_shot_ok=" . (LfShotRect(arg, r.x, r.y, r.w, r.h) ? 1 : 0))
        } else if (act = "wait") {
            Sleep(arg + 0)
        } else if (act = "locate_info") {
            Sleep(150)
            lo := G_LfLastLocate
            ; **高亮验证不能数 ClearType 彩边**（主控指出的误判：全图 679 个"黄"像素散布在
            ; 文字边缘）。正确做法：同一纸面控件先只上正文抓基线，再上真实 marks 抓一张，
            ; 数 **荧光笔实色** 像素 + 两张图的差值像素。
            fg := (G_LfData.paperFg = "hint") ? G_LfPal.paperHint : G_LfPal.sentText
            LfRichApply([{ text: G_LfData.paperText, mark: "" }], { fg: fg, bg: G_LfPal.paper, italic: false })
            Sleep(220)
            baseImg := LfGrabCtl(G_LfC.sent)
            LfRichApply(G_LfData.runs, { fg: fg, bg: G_LfPal.paper, italic: false })
            Sleep(220)
            markedImg := LfGrabCtl(G_LfC.sent)
            diffPx := LfDiffPx(baseImg, markedImg)
            markPx := LfCountColor(markedImg, Integer("0x" . G_LfPal.markCur), 8)
            LfWriteLines(arg, ["word=" . G_LfTarget.word
                , "found=" . (lo.found ? 1 : 0)
                , "sentence=" . lo.sentence
                , "sentence_len=" . StrLen(lo.sentence)
                , "sentence_start=" . ObjProp(lo, "sentence_start")
                , "target_start_abs=" . ObjProp(lo, "target_start_abs")
                , "target_end_abs=" . ObjProp(lo, "target_end_abs")
                , "target_start_in_sentence=" . lo.target_start
                , "target_end_in_sentence=" . lo.target_end
                , "passage_index=" . lo.passage_index
                , "highlight_diff_px=" . diffPx
                , "highlight_marker_px=" . markPx
                , "highlight_marker_color=" . G_LfPal.markCur
                , "locate_hits=" . G_LfE2ELocateHits
                , "locate_miss=" . G_LfE2ELocateMiss
                , "paper_text=" . G_LfData.paperText])
        } else if (act = "outline") {
            LfToggleOutline()
            out.Push("outline_on=" . (G_LfOutlineOn ? 1 : 0))
        } else if (act = "outline_null") {
            LfRequestOutline(true)
            out.Push("outline_null=1")
        } else if (act = "outline_info") {
            Sleep(1500)
            ; v22 P2b：把「高亮的是哪一段」落成数字 —— 断言 here_n == 当前段号，
            ; 且 here_slot != 当前段号（证明不是按显示槽位高亮）。
            hereN := 0, hereSlot := 0
            for i, row in G_LfOutlineRowsCache {
                if (IsObject(G_LfData) && row.p + 1 = G_LfData.outlineHere) {
                    hereN := row.p + 1, hereSlot := i
                    break
                }
            }
            ns := ""
            for row in G_LfOutlineRowsCache
                ns .= (ns = "" ? "" : ",") . row.n
            LfWriteLines(arg, ["outline_on=" . (G_LfOutlineOn ? 1 : 0)
                , "rows=" . G_LfOutlineRowsCache.Length
                , "previews=" . JsArrOf(G_LfOutlineRowsCache)
                , "here=" . (IsObject(G_LfData) ? G_LfData.outlineHere : "")
                , "here_n=" . hereN
                , "here_slot=" . hereSlot
                , "rows_ns=" . ns
                , "shown=" . G_LfOutlineShown
                , "grew_h=" . G_LfOutlineGrewH
                , "article_format=" . G_LongArticleFormat])
        } else if (act = "load_doc") {
            ; E2E 专用（只在本函数里出现；用户流程没有「打开已存在文章」入口）：
            ; 让面板指向 Python 播种好的文章，预览窗证据才落在真实的「已存在文章」上。
            ; 参数：id[,passage_count[,format[,title]]]
            q := StrSplit(arg, ",")
            G_LongDocId := q[1] + 0
            G_LongPassageCount := (q.Length >= 2) ? (q[2] + 0) : 0
            G_LongArticleFormat := (q.Length >= 3 && q[3] != "") ? q[3] : "manual"
            G_LfSourceTitle := (q.Length >= 4 && q[4] != "") ? q[4] : ("文章 #" . G_LongDocId)
            G_LongApp := "Edge"
            LfResetArticleState()
            LfShowArticle()
            ; E2E 截图友好：面板挪到屏幕左上（正常用户流程不经过这里）
            r := LfWindowRect(G_LfGui.Hwnd)
            G_LfGui.Move(LfPx(40), LfPx(40), r.w, r.h)
            out.Push("loaded_doc=" . G_LongDocId)
            out.Push("loaded_segments=" . G_LongPassageCount)
        } else if (act = "keys_on") {
            ; E2E：正常打开时 LfShowArticle 会挂面板内键；E2E 分支刻意不挂（不抢用户
            ; 键盘）。这条动作只在副本里显式挂上，用来走**真实热键**而不是直接调函数。
            LfRegisterKeys()
            out.Push("keys_on=" . (G_LfKeysOn ? 1 : 0))
        } else if (act = "inspector_hotkey") {
            ; spec §9 入口验证：激活面板后真的按 Alt+Shift+P，由面板作用域 Hotkey 打开预览窗。
            ; 用 keybd_event（OS 级注入）而不是 Send：AHK 的 Send 默认不触发本进程热键，
            ; 那样测的就不是真实入口了。
            ok := false
            if (G_LfGui) {
                try WinActivate("ahk_id " . G_LfGui.Hwnd)
                Sleep(180)
                LfE2EPressKeys([0x12, 0x10, 0x50])
                loop 60 {
                    if (G_LfInspGui) {
                        ok := true
                        break
                    }
                    Sleep(100)
                }
            }
            out.Push("inspector_open=" . (ok ? 1 : 0))
        } else if (act = "inspector_info") {
            Sleep(500)
            LfInspectorE2EInfo(arg)
            out.Push("inspector_info=" . arg)
        } else if (act = "inspector_select") {
            LfInspectorE2ESelect(arg + 0)
            out.Push("inspector_select=" . (arg + 0))
        } else if (act = "inspector_resegment") {
            LfInspectorResegment()
            out.Push("inspector_resegment=1")
        } else if (act = "inspector_escape") {
            out.Push("inspector_closed_by_esc=" . (LfInspectorE2EEscape() ? 1 : 0))
        } else if (act = "inspector_shot") {
            out.Push("inspector_shot_ok=" . (LfInspectorE2EShot(arg) ? 1 : 0))
        } else if (act = "sentence") {
            r := LfWindowRect(G_LfC.sent.Hwnd)
            out.Push("sentence_shot=" . (LfShotRect(arg, r.x, r.y, r.w, r.h) ? 1 : 0))
        } else if (act = "lookup_info") {
            Sleep(250)
            raw := G_LfE2ELookupRaw
            cn := JsonField(raw, "cn")
            LfWriteLines(arg, ["request_line=" . G_LfE2ELookupReq
                , "request_word=" . G_LfE2ELookupWord
                , "reply_word=" . JsonField(raw, "word")
                , "reply_phonetic=" . JsonField(raw, "phonetic")
                , "reply_cn=" . SubStr(cn, 1, 80)
                , "reply_source=" . JsonField(raw, "source")
                , "reply_cached=" . (JsonBool(raw, "cached") ? 1 : 0)
                , "reply_word_matches_current=" . (LookupWordMatches(JsonField(raw, "word"), G_LfTarget.word) ? 1 : 0)
                , "raw_prefix=" . SubStr(raw, 1, 260)])
        } else if (act = "shot") {
            r := LfWindowRect(G_LfGui.Hwnd)
            ; 只抓面板自己的矩形（不抓桌面，避免把用户屏幕内容带进交付物）
            out.Push("shot_ok=" . (LfShotRect(arg, r.x, r.y, r.w, r.h) ? 1 : 0))
        } else if (act = "enter") {
            LfSave("next")
            out.Push("saved_count=" . G_LfSavedCount)
        } else if (act = "spool") {
            LfWriteLines(arg, [G_LfE2ESpoolLine])
        } else if (act = "dump") {
            LfWriteLines(arg, out)
        } else if (act = "exit_app") {
            LfWriteLines(EnvGet("LF_E2E_RESULT"), out)
            ExitApp 0
        }
        Sleep(80)
    }
    LfWriteLines(EnvGet("LF_E2E_RESULT"), out)
    ExitApp 0
}

; v21 round2 (B)：session 出处 E2E 动作（只在 `LF_E2E_ACTIONS` 门后挂，正常进程不存在）。
;
; 为什么要它们：Bug 5 的证据必须穿过**真实入口函数**，不能拿别的实现糊一份：
;   · `hotkey_paste` → `LongformHotkeyPaste()`（= 真实 `^!5`，内部在 LfOpen 之前采出处）
;   · `panel_paste` → `LfPasteFromClipboard()`（= 面板内 Ctrl+V 的真实处理）
;   · `ocr_rect`    → `LongformCaptureProvenance()` + `LongformCaptureWithRect()`
;                     （= 真实 `^!4` 的「采集 + 拖框之后」两段，同一套代码）
;   · `activate` / `activate_panel`：把自渲染测试窗 / 面板切到前台，证明采集来源是谁
;   · `clip_save` / `clip_restore`：E2E 会写剪贴板，跑完还回去（不污染用户剪贴板）
;   · `prov_info`：把 G_Long* 与面板 sourceTitle 落到证据文件
LfE2EProvAction(act, arg, out) {
    global G_LfGui, G_LongTitle, G_LongApp, G_LongUrl, G_LfData, G_LfC
    global G_E2EClipBackup, G_E2EClipSaved, G_PanelProvLast, G_LongDocId
    if (act = "clip_save") {
        G_E2EClipBackup := ""
        try G_E2EClipBackup := A_Clipboard
        G_E2EClipSaved := true
        out.Push("clip_saved=1")
        return true
    }
    if (act = "clip_restore") {
        if (G_E2EClipSaved) {
            try A_Clipboard := G_E2EClipBackup
        }
        out.Push("clip_restored=" . (G_E2EClipSaved ? 1 : 0))
        return true
    }
    if (act = "activate") {
        ok := false
        try {
            WinActivate(arg)
            ok := !!WinWaitActive(arg, , 2)
        }
        Sleep(150)
        out.Push("activated=" . (ok ? 1 : 0))
        return true
    }
    if (act = "activate_panel") {
        ok := false
        if (G_LfGui) {
            try {
                WinActivate("ahk_id " G_LfGui.Hwnd)
                ok := !!WinWaitActive("ahk_id " G_LfGui.Hwnd, , 2)
            }
        }
        Sleep(150)
        out.Push("panel_active=" . (ok ? 1 : 0))
        return true
    }
    if (act = "ocr_rect_raw") {
        ; 生命周期证据（review 收口）：走**真实失败路径**让 G_LongDocId 归 0 ——
        ; 小矩形 OCR 切不出段落 → LfLongFail（只让本次长文失败，内核不判死），
        ; 不依赖中枢掉线（中枢掉线会被 HubWatch 自动拉起，复现不稳定）。
        q := StrSplit(arg, ",")
        LongformCaptureWithRect({ x: q[1] + 0, y: q[2] + 0, w: q[3] + 0, h: q[4] + 0 })
        return true
    }
    if (act = "close_panel") {
        if (G_LfGui)
            LfClose()
        Sleep(200)
        out.Push("panel_closed=" . (G_LfGui ? 0 : 1))
        return true
    }
    if (act = "hotkey_paste_empty") {
        ; 真实 ^!5 在**剪贴板为空**时只打开空面板（不建文章）——用于验证空态不显示上一篇来源。
        A_Clipboard := ""
        Sleep(120)
        LongformHotkeyPaste()
        Sleep(300)
        src := ""
        if (IsObject(G_LfData) && G_LfData.HasOwnProp("sourceTitle"))
            src := G_LfData.sourceTitle
        out.Push("empty_panel_open=" . (G_LfGui ? 1 : 0))
        out.Push("empty_panel_doc_id=" . G_LongDocId)
        out.Push("empty_panel_source_title=" . src)
        return true
    }
    if (act = "hotkey_paste") {
        ; 参数：`<预期来源窗标题>|<粘贴文件>`。共享机器上别的 agent 可能抢前台
        ; （Windows 前台锁 / 其他 E2E 切窗）→ 先确认前台确实是预期来源窗再调真实入口，
        ; 最多重试 3 次；拿不到就照常跑并留证据（orchestrator 的断言会失败，不静默通过）。
        parts := StrSplit(arg, "|", , 2)
        expect := parts[1], file := parts.Length >= 2 ? parts[2] : parts[1]
        tries := 0
        loop 3 {
            try {
                WinActivate(expect)
                WinWaitActive(expect, , 2)
            }
            Sleep(250)
            if (WinGetTitle("A") = expect)
                break
            tries++
        }
        out.Push("hotkey_paste_foreground=" . WinGetTitle("A"))
        out.Push("hotkey_paste_retries=" . tries)
        txt := FileRead(file, "UTF-8")
        A_Clipboard := txt
        LongformHotkeyPaste()
        out.Push("hotkey_paste_chars=" . StrLen(txt))
        return true
    }
    if (act = "panel_paste") {
        ; 参数：`<预期来源窗标题>|<粘贴文件>`。面板内 Ctrl+V 的出处来自「最近非面板前台」
        ; 缓存 —— 先把来源窗放前台等缓存刷新到它（700ms 定时器），再切回面板按 Ctrl+V。
        parts := StrSplit(arg, "|", , 2)
        expect := parts[1], file := parts.Length >= 2 ? parts[2] : parts[1]
        tries := 0
        loop 4 {
            try {
                WinActivate(expect)
                WinWaitActive(expect, , 2)
            }
            Sleep(900)
            if (InStr(G_PanelProvLast.title, expect))
                break
            tries++
        }
        out.Push("panel_paste_cache=" . G_PanelProvLast.title)
        out.Push("panel_paste_retries=" . tries)
        if (G_LfGui)
            WinActivate("ahk_id " G_LfGui.Hwnd)
        Sleep(200)
        txt := FileRead(file, "UTF-8")
        A_Clipboard := txt
        LfPasteFromClipboard()
        out.Push("panel_paste_chars=" . StrLen(txt))
        return true
    }
    if (act = "ocr_rect") {
        ; 参数：`<预期来源窗标题>|x,y,w,h`。同上：先占住前台再采出处，抢前台就重试。
        parts := StrSplit(arg, "|", , 2)
        expect := parts[1]
        q := StrSplit(parts.Length >= 2 ? parts[2] : arg, ",")
        tries := 0
        loop 3 {
            try {
                WinActivate(expect)
                WinWaitActive(expect, , 2)
            }
            LongformCaptureProvenance()
            if (InStr(G_LongTitle, expect))
                break
            tries++
            Sleep(400)
        }
        out.Push("ocr_foreground=" . WinGetTitle("A"))
        out.Push("ocr_prov_title=" . G_LongTitle)
        out.Push("ocr_prov_retries=" . tries)
        LongformCaptureWithRect({ x: q[1] + 0, y: q[2] + 0, w: q[3] + 0, h: q[4] + 0 })
        return true
    }
    if (act = "prov_info") {
        srcCtl := "", srcVisible := 0
        if (IsObject(G_LfC) && G_LfC.HasOwnProp("srcTitle")) {
            try srcCtl := G_LfC.srcTitle.Text
            try srcVisible := (G_LfC.srcTitle.Visible ? 1 : 0)
        }
        LfWriteLines(arg, ["long_title=" . G_LongTitle
            , "long_app=" . G_LongApp
            , "long_url=" . G_LongUrl
            , "long_doc_id=" . G_LongDocId
            , "panel_source_title=" . ((IsObject(G_LfData) && G_LfData.HasOwnProp("sourceTitle")) ? G_LfData.sourceTitle : "")
            , "panel_src_ctl_text=" . srcCtl
            , "panel_src_ctl_visible=" . srcVisible
            , "foreground_title=" . WinGetTitle("A")])
        return true
    }
    return false
}

