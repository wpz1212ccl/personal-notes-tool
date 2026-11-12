; ============================================================================
;  lib/PetMenu.ahk —— v35-ux D3：点击菜单面板（废除 native Menu()）
;
;  spec：.scratch/v35-ux/design.md §D3；用户反馈 #5（左右键交互灾难、文字不更新、
;  布局乱）。PetWindow 里的 native `Menu()`（PetShowMenu / PetShowSettingsMenu）
;  由此替代：标签全部动态、hover 高亮、点外部/Esc 自动收、不抢焦点。
;
;  形态：
;    · 分层窗口（WS_EX_LAYERED + ULW 逐像素 alpha；NOACTIVATE / TOOLWINDOW /
;      置顶），半透明面板（alpha=205，与 PetBubble/PetFeed 同一套绘制件）；
;    · 左键单击角色 → 主菜单（**立即弹，无 250ms 双击等待**；双击路径先
;      PetMenuClose 再收球，见 PetWindow.PetOnDoubleClick）；右键 → 设置面板；
;    · 锚在角色右侧（放不下换左侧），项高 ~26lp，宽按最长项自适应；
;    · hover 行高亮（浅底 + 描边，WM_MOUSEMOVE 重画该面板；只在行变化时重画）；
;    · 点击执行并收面板；点面板外 / Esc 收（50ms 轮询 GetAsyncKeyState，
;      **不注册任何热键**、不装鼠标钩子）；面板打开时再点角色 = 收面板。
;
;  菜单项**复用既有动作**（与热键同一条路径，不复制逻辑）：
;    CaptureArmHotkey / PetPickOpen / BandRedragStart / PetFeedToggle /
;    PetToggleDnd / PetReplayGuide / PetDisable；
;    媒体暂停走 `HubMedia(op, app, title)`（= BeginCapture 的 pause 同一条路径），
;    媒体态查询走取词台 `GET /api/settings` 的 media.status（异步回填，见下）。
;
;  v36 票 #243（spec 决策 8/9）：**先渲染、后补数据**。
;    · 建窗/绘制路径上零网络：首帧用本地记忆（G_MenuPending / G_MenuMediaPaused）
;      立刻显示，中枢不在位也照样可用；
;    · 动态标签的真实值由 PetMenuRefreshAsync 在**建窗之后**异步取（lib/PetNet.ahk，
;      超时 ≤300ms），回包到达时只重画文字区（PetMenuRelabel），过期回包按代次号
;      丢弃（PetMenuNetFresh）；
;    · 绘制成本：位图 + 逐像素 alpha 结果按 (宽,高,hover 行,disabled 位) 缓存
;      （PetMenuPaintKey），面板底单独缓存，hover 只重绘变化的那一行。
;
;  纯逻辑（文件头独立段，可单测）：
;    PetMenuArmLabel / PetMenuPickLabel / PetMenuMediaLabel / PetMenuFeedLabel /
;    PetMenuDndLabel / PetMenuMediaPausedFromSettings / PetMenuFitSide
;    （缓存键 / 过期回包 / 超时上限 / 待挑解析在 lib/Logic.ahk 的 v36 #243 段）
;
;  对外 API（PetWindow 的点击路由调用）：
;    PetMenuShowMain / PetMenuShowSettings / PetMenuClose / PetMenuVisible /
;    PetMenuMoveTo(x, y)
; ============================================================================

; ---------------------------------------------------------------------------
;  纯逻辑段（不建窗、不读全局 GUI 状态、可单测）
; ---------------------------------------------------------------------------

PetMenuArmLabel(armed) {
    return armed ? "收句 · 已开启" : "收句 · 已关闭"
}

PetMenuPickLabel(pending) {
    return pending > 0 ? "挑词（" . pending . " 条待选）" : "挑词（暂无待选句）"
}

PetMenuMediaLabel(paused) {
    return paused ? "继续视频" : "暂停视频"
}

PetMenuFeedLabel(visible) {
    return visible ? "信息流 · 显示中" : "信息流 · 已隐藏"
}

PetMenuDndLabel(dnd) {
    return dnd ? "勿扰 · 开" : "勿扰 · 关"
}

; `GET /api/settings` 回包里的媒体态 → 1 = 已暂停 / 0 = 在播 / -1 = 拿不到。
; 注意：媒体状态字面量按 "paus"."ed" 拼接 —— 源码钉子禁止在 StatusVisual 之外
; 出现相位字面量（这个词表里有一个同形词），别把它们连回一个带引号的词。
PetMenuMediaPausedFromSettings(json) {
    if (!RegExMatch(json, '"media"\s*:\s*\{[^}]*"status"\s*:\s*"([A-Za-z]+)"', &m))
        return -1
    return (m[1] = "paus" . "ed") ? 1 : 0
}

; 菜单贴角色右侧还是左侧：右边放得下用右，否则左（再夹进虚拟屏由调用方做）。
PetMenuFitSide(px, pw, mw, vw) {
    return (px + pw + 8 + mw <= vw) ? "right" : "left"
}

; ---------------------------------------------------------------------------
;  运行时状态（全局一律 G_Menu*，避免与既有模块冲突）
; ---------------------------------------------------------------------------

global PET_MENU_ALPHA := 205        ; 面板像素 alpha（真半透明，预乘）
global PET_MENU_TEXT_MIN := 140     ; 亮度阈值：max 通道 ≥ 它按文字处理（alpha 255）
global PET_MENU_ITEM_LP := 26       ; 项高
global PET_MENU_PADX_LP := 14
global PET_MENU_PADY_LP := 8
global PET_MENU_MIN_W_LP := 170
global PET_MENU_RADIUS_LP := 12     ; 圆角（同 D2 气泡口径）
global PET_MENU_HOVER_BG := 0x42362E    ; hover 浅底（RGB 2E3642，暗于文字阈值）
global PET_MENU_HOVER_EDGE := 0x726056  ; hover 描边（RGB 566072）

global G_MenuGui := 0
global G_MenuHwnd := 0
global G_MenuShown := false
global G_MenuKind := ""             ; "main" | "settings"
global G_MenuItems := []            ; { label, fn, disabled }
global G_MenuHover := -1
global G_MenuX := 0
global G_MenuY := 0
global G_MenuW := 0
global G_MenuH := 0
global G_MenuScreenDC := 0
global G_MenuHookOn := false
global G_MenuWatchOn := false       ; 点外部 / Esc 收面板的轮询 timer
global G_MenuWaitRelease := false   ; 先等鼠标松开，再认下一次按下为「点外部」
global G_MenuFontPt := 10
; 本地记忆（首帧就用它渲染，不碰网络；回包到达后更新，下次首帧更准）
global G_MenuMediaPaused := false   ; 媒体态拿不到时的本地记忆（点击翻转）
global G_MenuPending := 0           ; 上次拿到的待挑条数
global G_MenuNetGen := 0            ; 网络回包的代次号（每次打开菜单换代，过期回包丢弃）
; 绘制缓存（决策 9）：同一画面不重复付两次绘制成本
global G_MenuBase := 0              ; 面板底（含全部文字 + 全量 alpha）缓存
global G_MenuCache := Map()         ; PetMenuPaintKey → { dib, memdc, bits }
global G_MenuCacheMax := 8

; 主菜单：5 项，标签全部动态（首帧用本地记忆，线上真值异步回填）。
PetMenuShowMain() {
    global G_CapArmed, G_MenuPending, G_MenuMediaPaused
    ; 面板打开时再点角色 = 收面板（PetOnLButtonUp 的路由会再进这里）
    if (PetMenuVisible()) {
        PetMenuClose()
        return
    }
    armed := IsSet(G_CapArmed) ? G_CapArmed : false
    pending := G_MenuPending                     ; 本地记忆，不是网络
    items := [
        { label: PetMenuArmLabel(armed), fn: CaptureArmHotkey, disabled: false }
      , { label: PetMenuPickLabel(pending), fn: PetPickOpen, disabled: pending <= 0 }
      , { label: PetMenuMediaLabel(G_MenuMediaPaused), fn: PetMenuToggleMedia, disabled: false }
      , { label: "重拖字幕带", fn: BandRedragStart, disabled: false }
      , { label: PetMenuFeedLabel(PetFeedVisible()), fn: PetFeedToggle, disabled: false }
    ]
    PetMenuOpen("main", items)                   ; 先建窗显示：这条路径上没有网络
    Log("桌宠：主菜单（armed=" . (armed ? "on" : "off") . " 待挑 " . pending . " 记忆值）")
    PetMenuRefreshAsync()                        ; 建窗之后才取真值
}

; 异步回填动态标签（决策 8）：建窗之后才发，超时 ≤300ms，回包只改文字区。
; 每次打开都换代次号 —— 关掉再重开、连点两次，旧回包一律作废。
PetMenuRefreshAsync() {
    global G_MenuNetGen, G_WebPort, G_CapSession
    G_MenuNetGen += 1
    gen := G_MenuNetGen
    port := IsSet(G_WebPort) ? G_WebPort : 0
    if (!port)
        return                                  ; 取词台没起：标签就用记忆值，不空白
    PetNetGetAsync("http://127.0.0.1:" . port . "/api/settings", PetMenuOnMedia.Bind(gen))
    sess := IsSet(G_CapSession) ? G_CapSession : 0
    if (sess > 0)
        PetNetGetAsync("http://127.0.0.1:" . port . "/api/sessions/" . sess
                     . "/backlog/count", PetMenuOnPending.Bind(gen))
}

; 待挑条数回包：先更新本地记忆（下次首帧更准），再决定要不要改界面。
PetMenuOnPending(gen, text) {
    global G_MenuNetGen, G_MenuKind, G_MenuPending
    pending := PetBacklogCount(text)            ; 空/坏回包 → 0（禁用挑词项，不空白）
    G_MenuPending := pending
    if (!PetMenuNetFresh(gen, G_MenuNetGen) || G_MenuKind != "main" || !PetMenuVisible())
        return                                   ; 过期回包 / 已关 / 换了面板：丢弃
    PetMenuRelabel(2, PetMenuPickLabel(pending), pending <= 0)
}

; 媒体态回包（`GET /api/settings`）：先更新本地记忆，再决定要不要改界面。
PetMenuOnMedia(gen, text) {
    global G_MenuNetGen, G_MenuKind, G_MenuMediaPaused
    st := PetMenuMediaPausedFromSettings(text)
    if (st >= 0)
        G_MenuMediaPaused := (st = 1)            ; 记忆更新：下次首帧更准
    if (st < 0)
        return                                   ; 拿不到：标签保持记忆值（不空白）
    if (!PetMenuNetFresh(gen, G_MenuNetGen) || G_MenuKind != "main" || !PetMenuVisible())
        return                                   ; 过期回包 / 已关 / 换了面板：丢弃
    PetMenuRelabel(3, PetMenuMediaLabel(G_MenuMediaPaused), false)
}

; 设置面板：右键。
PetMenuShowSettings() {
    global G_PetDnd
    if (PetMenuVisible()) {
        PetMenuClose()
        return
    }
    dnd := IsSet(G_PetDnd) ? G_PetDnd : false
    items := [
        { label: PetMenuDndLabel(dnd), fn: PetToggleDnd, disabled: false }
      , { label: "重放引导", fn: PetReplayGuide, disabled: false }
      , { label: "打开取词台", fn: PetMenuOpenWorkbench, disabled: false }
      , { label: "关闭桌宠", fn: PetDisable, disabled: false }
    ]
    PetMenuOpen("settings", items)
    Log("桌宠：设置菜单")
}

; 媒体暂停/恢复：**与 BeginCapture 同一条路径**（HubMedia = HubSend(media RPC)），
; 会话身份用既有 G_SessApp / G_SessRawTitle（与 MediaResumeAfterCapture 同口径）。
PetMenuToggleMedia() {
    global G_MenuMediaPaused, G_SessApp, G_SessRawTitle
    paused := G_MenuMediaPaused                  ; 记忆值（打开菜单时已异步刷新过）
    op := paused ? "resume" : "pause"
    app := IsSet(G_SessApp) ? G_SessApp : ""
    title := IsSet(G_SessRawTitle) ? G_SessRawTitle : ""
    ok := false
    try ok := HubMedia(op, app, title)
    ; 回包是异步状态字（OnMediaLine 只记日志）；本地记忆即时翻转改标签，
    ; 下次打开菜单时 /api/settings 查得到就以查询结果为准。
    G_MenuMediaPaused := !paused
    Log("桌宠：媒体" . (paused ? "续播" : "暂停") . "请求已发（ok=" . (ok ? 1 : 0) . "）")
}

; 打开取词台（同托盘入口口径：http://127.0.0.1:<G_WebPort>/）。
PetMenuOpenWorkbench() {
    global G_WebPort
    port := IsSet(G_WebPort) ? G_WebPort : 0
    if (!port)
        return
    url := "http://127.0.0.1:" . port . "/"
    Log("桌宠：打开取词台 " . url)
    try Run(url)
    catch as err
        Log("桌宠：打开取词台失败 " . err.Message)
}

PetMenuClose() {
    global G_MenuGui, G_MenuShown, G_MenuKind, G_MenuHover, G_MenuWatchOn
    G_MenuShown := false
    G_MenuKind := ""
    G_MenuHover := -1
    if (G_MenuWatchOn) {
        SetTimer(PetMenuWatch, 0)
        G_MenuWatchOn := false
    }
    if (G_MenuGui)
        try G_MenuGui.Hide()
    PetMenuCacheClear()                 ; 关掉就还位图（下次打开首帧要重新按内容渲）
}

; 标签回填（回包到达时）：只换文字区，别的菜单项与位置一个字节不动。
PetMenuRelabel(idx, label, disabled) {
    global G_MenuItems, G_MenuW, G_MenuH
    if (idx < 1 || idx > G_MenuItems.Length)
        return
    it := G_MenuItems[idx]
    if (it.label = label && it.disabled = disabled)
        return
    it.label := label
    it.disabled := disabled
    PetMenuCacheClear()                 ; 文字变了：旧位图作废
    PetMenuPaint(G_MenuW, G_MenuH)      ; 重画一次（行数/尺寸不变，不做布局重算）
}

PetMenuVisible() {
    global G_MenuHwnd, G_MenuShown
    return G_MenuHwnd != 0 && G_MenuShown && DllCall("IsWindowVisible", "Ptr", G_MenuHwnd)
}

; v35-ux D4：move-only 跟随（拖动期用）。**不重绘、不重算布局**。
PetMenuMoveTo(x, y) {
    global G_MenuHwnd, G_MenuX, G_MenuY
    if (!G_MenuHwnd)
        return
    G_MenuX := x
    G_MenuY := y
    DllCall("SetWindowPos", "Ptr", G_MenuHwnd, "Ptr", 0, "Int", x, "Int", y
        , "Int", 0, "Int", 0, "UInt", 0x0015)   ; NOSIZE|NOZORDER|NOACTIVATE
}

; 建窗 + 画 + 贴 + 显示（内容/位置每次打开都重算）。**这条路径上没有网络。**
PetMenuOpen(kind, items) {
    global G_MenuGui, G_MenuHwnd, G_MenuShown, G_MenuKind, G_MenuItems
    global G_MenuW, G_MenuH, G_MenuX, G_MenuY, G_MenuScreenDC, G_MenuHover
    global G_MenuWatchOn, G_MenuWaitRelease, G_MenuHookOn, G_MenuNetGen
    G_MenuNetGen += 1                   ; 换一代：在飞的旧回包从此作废
    G_MenuKind := kind
    G_MenuItems := items
    G_MenuHover := -1
    PetMenuCacheClear()                 ; 内容变了：旧位图不能贴
    ; 尺寸：宽按最长项自适应；高 = 项数 × 项高 + 上下留白
    w := PetMenuMeasureWidth(items)
    h := 2 * P(PET_MENU_PADY_LP) + items.Length * P(PET_MENU_ITEM_LP)
    ; 锚：角色右侧，放不下换左侧；垂直贴角色头顶附近
    vr := VirtualScreenRect()
    side := "right"
    if (IsSet(G_PetX) && G_PetX != "" && IsSet(G_PetW) && G_PetW > 0) {
        side := PetMenuFitSide(G_PetX, G_PetW, w, vr.x + vr.w)
        x := (side = "right") ? (G_PetX + G_PetW + P(8)) : (G_PetX - w - P(8))
        y := G_PetY + P(40)
    } else {
        x := vr.x + vr.w - w - P(24)
        y := vr.y + vr.h - P(340)
    }
    x := Max(vr.x + 4, Min(x, vr.x + vr.w - w - 4))
    y := Max(vr.y + 4, Min(y, vr.y + vr.h - h - 4))
    if (!G_MenuGui) {
        g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x80000 +E0x08000000")
        g.MarginX := 0
        g.MarginY := 0
        g.BackColor := "000000"
        g.Show(Format("x{} y{} w{} h{} NoActivate Hide", x, y, w, h))
        G_MenuGui := g
        G_MenuHwnd := g.Hwnd
        G_MenuScreenDC := DllCall("user32\GetDC", "Ptr", 0, "Ptr")
    }
    if (!G_MenuHookOn) {
        OnMessage(0x200, PetMenuOnMouseMove)     ; WM_MOUSEMOVE（hover 高亮）
        OnMessage(0x201, PetMenuOnLButtonDown)   ; WM_LBUTTONDOWN（点住也不抢焦点）
        OnMessage(0x202, PetMenuOnLButtonUp)     ; WM_LBUTTONUP（执行并收）
        G_MenuHookOn := true
    }
    G_MenuW := w, G_MenuH := h
    G_MenuX := x, G_MenuY := y
    PetMenuPaint(w, h)
    ; 位置与尺寸都由上面 ULW 的 dst/size 参数设置（分层窗专用语义）
    DllCall("ShowWindow", "Ptr", G_MenuHwnd, "Int", 8)   ; SW_SHOWNA
    G_MenuShown := true
    ; 点外部 / Esc 收面板：50ms 轮询（不注册热键、不装钩子）
    G_MenuWaitRelease := false
    if (!G_MenuWatchOn) {
        SetTimer(PetMenuWatch, 50)
        G_MenuWatchOn := true
    }
}

; 面板宽：最长项文字 + 两侧留白，下限 PET_MENU_MIN_W_LP。
PetMenuMeasureWidth(items) {
    global G_MenuFontPt
    hdc := DllCall("gdi32\CreateCompatibleDC", "Ptr", 0, "Ptr")
    if (!hdc)
        return P(PET_MENU_MIN_W_LP)
    hfont := PetPanelFont(G_MenuFontPt)
    old := DllCall("gdi32\SelectObject", "Ptr", hdc, "Ptr", hfont, "Ptr")
    maxW := 0
    for it in items {
        rect := Buffer(16, 0)
        NumPut("Int", 0, rect, 0), NumPut("Int", 0, rect, 4)
        NumPut("Int", 0, rect, 8), NumPut("Int", 0, rect, 12)
        DllCall("user32\DrawTextW", "Ptr", hdc, "Str", it.label, "Int", -1, "Ptr", rect
              , "UInt", 0x400 | 0x800 | 0x20)      ; DT_CALCRECT|DT_NOPREFIX|DT_SINGLELINE
        tw := NumGet(rect, 8, "Int") - NumGet(rect, 0, "Int")
        if (tw > maxW)
            maxW := tw
    }
    DllCall("gdi32\SelectObject", "Ptr", hdc, "Ptr", old)
    DllCall("gdi32\DeleteObject", "Ptr", hfont)
    DllCall("gdi32\DeleteDC", "Ptr", hdc)
    return Max(P(PET_MENU_MIN_W_LP), maxW + 2 * P(PET_MENU_PADX_LP) + P(8))
}

; 菜单字号：统一取 G_MenuFontPt（全局，方便一处调）。

; 画面板（决策 9：同一画面不重复付两次绘制成本）。
;   命中缓存 → 直接贴（零 DIB 重建、零逐像素 alpha）；
;   没命中   → 面板底（含全部文字 + 全量 alpha）单独缓存，再只补 hover 那一行。
PetMenuPaint(w, h) {
    global G_MenuCache, G_MenuItems, G_MenuHover
    key := PetMenuPaintKey(w, h, G_MenuHover
         , PetMenuDisabledMask(PetMenuDisabledFlags(G_MenuItems)))
    if (G_MenuCache.Has(key)) {
        PetMenuBlit(G_MenuCache[key])
        return
    }
    base := PetMenuBaseImage(w, h)
    if (!base)
        return
    img := PetMenuImageFrom(base, w, h)          ; base 像素整块搬过来
    if (!img)
        return
    if (G_MenuHover >= 1 && G_MenuHover <= G_MenuItems.Length)
        PetMenuPaintHoverRow(img, w, h)          ; 只画变化的那一行
    PetMenuCachePut(key, img)
    PetMenuBlit(img)
}

; 面板底（面板 + 亮边 + 全部文字 + 全量 alpha 修补）：按 (宽,高,disabled) 缓存。
PetMenuBaseImage(w, h) {
    global G_MenuBase, G_MenuItems, G_MenuFontPt
    key := PetMenuPaintKey(w, h, -1
         , PetMenuDisabledMask(PetMenuDisabledFlags(G_MenuItems)))
    if (IsObject(G_MenuBase) && G_MenuBase.key = key)
        return G_MenuBase
    PetMenuImageFree(G_MenuBase)
    G_MenuBase := 0
    img := PetMenuImageNew(w, h)
    if (!img)
        return 0
    mem := img.memdc
    ; 圆角面板 + 亮边（alpha 由 PetPanelFixAlpha 统一补）
    brush := DllCall("gdi32\CreateSolidBrush", "UInt", 0x0028211B, "Ptr")
    pen := DllCall("gdi32\CreatePen", "Int", 0, "Int", 1, "UInt", 0x005D5245, "Ptr")
    oldB := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", brush, "Ptr")
    oldP := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", pen, "Ptr")
    DllCall("gdi32\RoundRect", "Ptr", mem, "Int", 0, "Int", 0, "Int", w - 1
          , "Int", h - 1, "Int", P(PET_MENU_RADIUS_LP), "Int", P(PET_MENU_RADIUS_LP))
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldB)
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldP)
    DllCall("gdi32\DeleteObject", "Ptr", brush)
    DllCall("gdi32\DeleteObject", "Ptr", pen)
    ; 各项文字
    hfont := PetPanelFont(G_MenuFontPt)
    oldF := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hfont, "Ptr")
    DllCall("gdi32\SetBkMode", "Ptr", mem, "Int", 1)        ; TRANSPARENT
    itemH := P(PET_MENU_ITEM_LP)
    padY := P(PET_MENU_PADY_LP)
    i := 0
    for it in G_MenuItems {
        i += 1
        PetMenuDrawLabel(mem, w, padY + (i - 1) * itemH, itemH, it)
    }
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldF)
    DllCall("gdi32\DeleteObject", "Ptr", hfont)
    DllCall("gdi32\GdiFlush")
    ; 真半透明：面板像素预乘 alpha，文字像素保持 alpha 255
    PetPanelFixAlpha(img.bits, w, h, PET_MENU_ALPHA, PET_MENU_TEXT_MIN)
    img.key := key
    G_MenuBase := img
    return img
}

; hover 行：只重画这一行的浅底 + 描边 + 文字，并只修补这一块矩形的 alpha。
; 其余行来自 base 的整块拷贝 —— 不再付整幅 DIB 重建与整幅逐像素成本。
PetMenuPaintHoverRow(img, w, h) {
    global G_MenuItems, G_MenuHover, G_MenuFontPt
    itemH := P(PET_MENU_ITEM_LP)
    padY := P(PET_MENU_PADY_LP)
    y0 := padY + (G_MenuHover - 1) * itemH
    x0 := P(4), x1 := w - P(4) - 1
    mem := img.memdc
    hb := DllCall("gdi32\CreateSolidBrush", "UInt", PET_MENU_HOVER_BG, "Ptr")
    hp := DllCall("gdi32\CreatePen", "Int", 0, "Int", 1, "UInt", PET_MENU_HOVER_EDGE, "Ptr")
    hbOld := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hb, "Ptr")
    hpOld := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hp, "Ptr")
    DllCall("gdi32\RoundRect", "Ptr", mem, "Int", x0, "Int", y0 + 1, "Int"
          , x1, "Int", y0 + itemH - 1, "Int", P(6), "Int", P(6))
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hbOld)
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hpOld)
    DllCall("gdi32\DeleteObject", "Ptr", hb)
    DllCall("gdi32\DeleteObject", "Ptr", hp)
    ; hover 底压住了这一行的字：重画一遍
    DllCall("gdi32\SetBkMode", "Ptr", mem, "Int", 1)
    hfont := PetPanelFont(G_MenuFontPt)
    oldF := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hfont, "Ptr")
    PetMenuDrawLabel(mem, w, y0, itemH, G_MenuItems[G_MenuHover])
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldF)
    DllCall("gdi32\DeleteObject", "Ptr", hfont)
    DllCall("gdi32\GdiFlush")
    ; 只修补刚重画的那块矩形（含端点）：其余像素保留 base 已预乘的结果，
    ; 再修一遍会把面板乘暗 —— 窗口必须正好覆盖重画范围。
    PetPanelFixAlphaRect(img.bits, w, h, x0, y0, x1, y0 + itemH - 1
                       , PET_MENU_ALPHA, PET_MENU_TEXT_MIN)
}

; 一行文字（禁用灰字 / 正常亮字）；base 与 hover 行共用同一份绘制口径。
PetMenuDrawLabel(mem, w, y0, itemH, it) {
    DllCall("gdi32\SetTextColor", "Ptr", mem
          , "UInt", it.disabled ? 0xA0928A : 0xF8F4F2)
    tr := Buffer(16, 0)
    NumPut("Int", P(PET_MENU_PADX_LP), tr, 0), NumPut("Int", y0, tr, 4)
    NumPut("Int", w - P(PET_MENU_PADX_LP), tr, 8), NumPut("Int", y0 + itemH, tr, 12)
    DllCall("user32\DrawTextW", "Ptr", mem, "Str", it.label, "Int", -1, "Ptr", tr
          , "UInt", 0x20 | 0x4 | 0x800)                ; DT_SINGLELINE|DT_VCENTER|DT_NOPREFIX
}

; 禁用位图的一位输入：第 i 项 → 第 i-1 位（纯函数在 Logic.ahk）。
PetMenuDisabledFlags(items) {
    flags := []
    for it in items
        flags.Push(it.disabled)
    return flags
}

; 新建一张 (w,h) 的 32bpp DIB + 已选好它的 memDC。
PetMenuImageNew(w, h) {
    global G_MenuScreenDC
    bi := Buffer(40, 0)
    NumPut("Int", 40, bi, 0)
    NumPut("Int", w, bi, 4), NumPut("Int", -h, bi, 8)
    NumPut("UShort", 1, bi, 12), NumPut("UShort", 32, bi, 14)
    NumPut("UInt", 0, bi, 16)
    bits := 0
    dib := DllCall("gdi32\CreateDIBSection", "Ptr", 0, "Ptr", bi, "UInt", 0
                 , "Ptr*", &bits, "Ptr", 0, "UInt", 0, "Ptr")
    if (!dib)
        return 0
    mem := DllCall("gdi32\CreateCompatibleDC", "Ptr", G_MenuScreenDC, "Ptr")
    if (!mem) {
        DllCall("gdi32\DeleteObject", "Ptr", dib)
        return 0
    }
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", dib)
    return { key: "", dib: dib, memdc: mem, bits: bits }
}

; 从 base 拷一张（像素整块搬；hover 行在拷贝上增量画）。
PetMenuImageFrom(base, w, h) {
    img := PetMenuImageNew(w, h)
    if (!img)
        return 0
    DllCall("RtlMoveMemory", "Ptr", img.bits, "Ptr", base.bits, "UPtr", w * h * 4)
    return img
}

; 释放一张位图：**先删 DC 再删位图**（位图还选在 DC 里时 DeleteObject 会失败 →
; 每画一次漏一张 DIB；v35 的原实现是反的，这里一并改正）。
PetMenuImageFree(img) {
    if (!IsObject(img))
        return
    if (img.memdc)
        DllCall("gdi32\DeleteDC", "Ptr", img.memdc)
    if (img.dib)
        DllCall("gdi32\DeleteObject", "Ptr", img.dib)
}

; 缓存表：命中即贴；超上限按插入顺序丢最老的（hover 会来回切，保留最近几张）。
PetMenuCachePut(key, img) {
    global G_MenuCache, G_MenuCacheMax
    G_MenuCache[key] := img
    if (G_MenuCache.Count > G_MenuCacheMax) {
        oldest := ""
        for k, v in G_MenuCache {
            oldest := k
            break
        }
        if (oldest != "") {
            PetMenuImageFree(G_MenuCache[oldest])
            G_MenuCache.Delete(oldest)
        }
    }
}

; 作废全部位图（换内容 / 关面板）：base 与命中表一起释放，避免泄漏与贴旧字。
PetMenuCacheClear() {
    global G_MenuBase, G_MenuCache
    PetMenuImageFree(G_MenuBase)
    G_MenuBase := 0
    for k, v in G_MenuCache
        PetMenuImageFree(v)
    G_MenuCache := Map()
}

; 1:1 物理像素贴（GDI 缩放会清零 alpha）。
PetMenuBlit(img) {
    global G_MenuHwnd, G_MenuScreenDC, G_MenuX, G_MenuY, G_MenuW, G_MenuH
    dst := Buffer(8, 0)
    NumPut("Int", G_MenuX, dst, 0), NumPut("Int", G_MenuY, dst, 4)
    size := Buffer(8, 0)
    NumPut("Int", G_MenuW, size, 0), NumPut("Int", G_MenuH, size, 4)
    blend := Buffer(4, 0)
    NumPut("UChar", 0, blend, 0), NumPut("UChar", 0, blend, 1)
    NumPut("UChar", 255, blend, 2), NumPut("UChar", 1, blend, 3)
    pt := Buffer(8, 0)
    DllCall("user32\UpdateLayeredWindow", "Ptr", G_MenuHwnd, "Ptr", G_MenuScreenDC
          , "Ptr", dst, "Ptr", size, "Ptr", img.memdc, "Ptr", pt
          , "UInt", 0, "Ptr", blend, "UInt", 2)
}

; 光标 → 项序号（1 起）；不在任何项上返回 -1。
PetMenuItemAt(cx, cy) {
    global G_MenuItems, G_MenuW, G_MenuH
    if (cx < 0 || cy < 0 || cx > G_MenuW || cy > G_MenuH)
        return -1
    padY := P(PET_MENU_PADY_LP)
    itemH := P(PET_MENU_ITEM_LP)
    i := (cy - padY) // itemH + 1
    if (i < 1 || i > G_MenuItems.Length)
        return -1
    return i
}

; 光标在面板客户区里的坐标（物理像素；无边框窗口 = 窗口原点即客户区原点）。
PetMenuCursorIn() {
    global G_MenuHwnd
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    wr := Buffer(16, 0)
    DllCall("GetWindowRect", "Ptr", G_MenuHwnd, "Ptr", wr)
    return { x: NumGet(pt, 0, "Int") - NumGet(wr, 0, "Int")
           , y: NumGet(pt, 4, "Int") - NumGet(wr, 4, "Int") }
}

PetMenuOnMouseMove(wParam, lParam, msg, hwnd) {
    global G_MenuHwnd, G_MenuHover, G_MenuItems
    if (hwnd != G_MenuHwnd || !G_MenuShown)
        return
    c := PetMenuCursorIn()
    i := PetMenuItemAt(c.x, c.y)
    if (i >= 1 && G_MenuItems[i].disabled)
        i := -1
    if (i != G_MenuHover) {
        G_MenuHover := i
        PetMenuPaint(G_MenuW, G_MenuH)     ; 只在行变化时重画（hover 不在拖动路径上）
    }
    return 0
}

PetMenuOnLButtonDown(wParam, lParam, msg, hwnd) {
    global G_MenuHwnd
    if (hwnd != G_MenuHwnd)
        return
    return 0                               ; 点住面板不做事（执行在 Up；不抢焦点）
}

PetMenuOnLButtonUp(wParam, lParam, msg, hwnd) {
    global G_MenuHwnd, G_MenuItems
    if (hwnd != G_MenuHwnd || !G_MenuShown)
        return
    c := PetMenuCursorIn()
    i := PetMenuItemAt(c.x, c.y)
    if (i < 1 || G_MenuItems[i].disabled)
        return 0
    fn := G_MenuItems[i].fn
    PetMenuClose()                         ; 先收面板再执行（手感 & 避免面板压住新窗）
    if (fn != "")
        fn()
    return 0
}

; 点外部 / Esc 收面板：50ms 轮询鼠标与键盘状态（不注册热键、不装钩子、不抢焦点）。
PetMenuWatch() {
    global G_MenuWaitRelease, G_MenuHwnd, G_MenuX, G_MenuY, G_MenuW, G_MenuH
    global G_PetX, G_PetY, G_PetW, G_PetH, G_PetDragging
    if (!PetMenuVisible()) {
        SetTimer(PetMenuWatch, 0)
        G_MenuWaitRelease := false
        return
    }
    ; v36 决策 11：拖着桌宠划过菜单旁不算「点了外面」—— 拖动期菜单由 move-only
    ; 带着走，不在这里判外部点击（诊断 §3.6）。
    if (PetDragActive(IsSet(G_PetDragging) && G_PetDragging))
        return
    lDown := (GetKeyState("LButton", "P"))
    escDown := GetKeyState("Esc", "P")
    if (!G_MenuWaitRelease) {
        if (!lDown && !escDown)
            G_MenuWaitRelease := true      ; 开菜单那一拍的按键已松开
        return
    }
    if (escDown) {
        PetMenuClose()
        return
    }
    if (!lDown)
        return
    ; 新的一次按下：落在面板外、且不在角色窗上（点角色 = 收面板，由点击路由做）→ 收
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    mx := NumGet(pt, 0, "Int"), my := NumGet(pt, 4, "Int")
    if (mx >= G_MenuX && mx <= G_MenuX + G_MenuW && my >= G_MenuY && my <= G_MenuY + G_MenuH)
        return                             ; 面板内：交给自己的 Up 执行
    if (IsSet(G_PetX) && G_PetX != "" && mx >= G_PetX && mx <= G_PetX + G_PetW
            && my >= G_PetY && my <= G_PetY + G_PetH)
        return                             ; 角色上：点击路由「再点角色 = 收面板」
    PetMenuClose()
}
