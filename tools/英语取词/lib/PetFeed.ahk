; ============================================================================
;  lib/PetFeed.ahk —— v35-ux D1：信息流面板（常驻流式状态）
;
;  spec：.scratch/v35-ux/design.md §D1；用户反馈 #2/#7（「信息流不见了，看视频
;  不知道后端在干嘛」）。相位判断仍只在 lib/Logic.ahk 的 StatusVisual()——
;  本文件只消费外壳喂进来的「行事件」（PetFeedLine）与 armed 位
;  （PetFeedSetArmed，PetWindow.PetApplyVisual 里接线）。
;
;  形态：
;    · 分层窗口（WS_EX_LAYERED + UpdateLayeredWindow 逐像素 alpha；NOACTIVATE /
;      TOOLWINDOW / 置顶）——**不用整窗 alpha**（与逐像素透明互斥）；
;    · **竖向窄条**（2026-11-10 用户要求「更紧凑、竖向」）：宽 208lp（原 300），
;      一条事件竖着堆两段 —— 上段时间（8pt 灰小字），下段正文（9pt，tone 着色，
;      最多折 2 行，超出用省略号截断）。条与条之间 7lp 空隙。
;      为什么不再横排：横排「时间列 + 文本列」把每条都顶成一行长条，面板越拉越宽；
;      竖排后宽度只由 208lp 决定，面板自然变成贴着角色的细长竖条。
;    · 视野按**像素高度**裁剪（PET_FEED_VIEW_H_LP），不再是「7 行」——竖向布局里
;      一条 1~2 行不等，按行数算会溢出；环形缓冲 30 条，面板隐藏时也记账，重开回放；
;    · 面板像素 alpha=205（预乘 BGRA 真半透明）、圆角 10lp、1px 半透明亮边；
;      文本按 tone 着色 ok=绿 / warn=琥珀 / error=红 / idle=灰；文字像素 alpha 255；
;    · 右上角 ~16×16lp `×` 热区：点击隐藏。v36 #245 起 × 与菜单项写**同一组**
;      显隐位（互斥，唯一入口是 lib/Logic.ahk 的 PetFeedVisFlags）—— 不会再出现
;      「已隐藏」与「已固定」并存，`×` 也不再等于永久失联：开收句自动解除用户隐藏；
;    · 默认停靠角色左侧（垂直居中，留 12lp）；可拖（面板拖动，松手落
;      pet-state.json）；armed=false 自动隐藏（用户固定过除外）。
;
;  纯逻辑（不依赖窗口全局，可被测试 harness 单独断言）放在文件头部独立段：
;    PetFeedTrimLines / PetFeedFormatLine / PetFeedShouldAutoShow /
;    PetFeedToneColor / PetFeedToneValid / PetFeedPickView
;  （显隐位的组合口径与「事件 → 行」的判定在 lib/Logic.ahk：PetFeedVisFlags /
;   PetFeedProject —— 本文件只消费结果。）
;
;  对外 API（A3 在 英语取词.ahk 调用；PetWindow 的 PetApplyVisual 内部接线）：
;    PetFeedStartup / PetFeedShutdown / PetFeedLine(text, tone)
;    PetFeedMoveTo(x, y) / PetFeedReanchor(ax, ay) / PetFeedToggle
;    PetFeedVisible / PetFeedSetArmed(armed)
; ============================================================================

; ---------------------------------------------------------------------------
;  纯逻辑段（不建窗、不读全局 GUI 状态、可单测）
; ---------------------------------------------------------------------------

; 环形缓冲裁剪：只保留最新 maxKeep 条（lines = Array of 行对象；非数组给空）。
; 注意：参数名不能用 max/min —— AHK 内置函数名不区分大小写，会被参数名遮蔽。
PetFeedTrimLines(lines, maxKeep) {
    out := []
    if (Type(lines) != "Array" || maxKeep <= 0)
        return out
    start := Max(1, lines.Length - maxKeep + 1)
    loop lines.Length - start + 1
        out.Push(lines[start + A_Index - 1])
    return out
}

; 一行的展示文本：`HH:MM  文本`（时间灰色小号由绘制层负责，这里只拼口径）。
; 空文本 → 空串（调用方跳过，不记空行）。
PetFeedFormatLine(text, tone, hh, mm) {
    t := Trim(text)
    if (t = "")
        return ""
    return Format("{:02}:{:02}", hh, mm) . "  " . t
}

; armed=false 自动隐藏，**除非用户固定过**；用户 × 关掉的手动优先。
PetFeedShouldAutoShow(armed, pinned, userHidden) {
    return !userHidden && (armed || pinned)
}

; tone 是否在四个合法档里（非法回落 idle 的判定也在本文件绘制层复用它）。
PetFeedToneValid(tone) {
    return tone = "ok" || tone = "warn" || tone = "error" || tone = "idle"
}

; tone → GDI COLORREF（0x00BBGGRR）：ok 绿 / warn 琥珀 / error 红 / idle 灰。
PetFeedToneColor(tone) {
    if (tone = "ok")
        return 0x6ED966          ; RGB 66D96E
    if (tone = "warn")
        return 0x00B3FF          ; RGB FFB300
    if (tone = "error")
        return 0x5053EF          ; RGB EF5350
    return 0xC5BEB0              ; RGB B0BEC5
}

; 从缓冲尾部挑显示段：从最新往回累计**像素高度**（一条 = 时间行 + 正文 1~2 行），
; 累计超过 maxH 就把更旧的丢出视野（不是删除，缓冲还在）。至少留一条。
; lines = Array of {text, tone, hh, mm}；heightOf(e) 回调返回该条的像素高。
; v35 原本按「行数」预算（一条最多 2 行）；竖向布局改按像素，理由见文件头。
PetFeedPickView(lines, maxH, heightOf) {
    view := []
    if (Type(lines) != "Array" || maxH <= 0)
        return view
    used := 0
    i := lines.Length
    while (i >= 1) {
        h := heightOf(lines[i])
        if (used + h > maxH && view.Length > 0)
            break
        view.InsertAt(1, lines[i])
        used += h
        i -= 1
    }
    return view
}

; ---------------------------------------------------------------------------
;  运行时状态（全局一律 G_Feed*，避免与既有模块冲突）
; ---------------------------------------------------------------------------

global PET_FEED_ALPHA := 205        ; 面板像素 alpha（真半透明，预乘）
global PET_FEED_TEXT_MIN := 140     ; 亮度阈值：max 通道 ≥ 它按文字处理（alpha 255）
global PET_FEED_MAX_LINES := 30     ; 环形缓冲条数
global PET_FEED_VIEW_H_LP := 200    ; 视野高度预算（像素；按条高累加裁剪，不按行数）
global PET_FEED_W_LP := 196         ; 面板宽（竖向窄条；2026-11-10 由 300 一路收窄）
global PET_FEED_PAD_LP := 8
global PET_FEED_HEADER_LP := 14     ; 顶部 × 热区条
global PET_FEED_TIME_H_LP := 11     ; 每条上段：时间行（7pt）
global PET_FEED_ROW_LP := 15        ; 正文行高（8pt）
global PET_FEED_BODY_MAX := 2       ; 正文最多折几行（超出用省略号截断）
global PET_FEED_ENTRY_GAP_LP := 6   ; 条与条之间的空隙
global PET_FEED_BODY_PT := 8        ; 正文字号（竖排后 9pt 太占，收到 8pt）
global PET_FEED_TIME_PT := 7        ; 时间字号
global PET_FEED_CLOSE_LP := 16      ; × 热区边长
global PET_FEED_DOCK_GAP_LP := 12   ; 与角色的横向间距

global G_FeedGui := 0
global G_FeedHwnd := 0
global G_FeedShown := false
global G_FeedArmed := false
global G_FeedUserHidden := false    ; 用户 × 关过（手动优先于 armed）
global G_FeedPinned := false        ; 用户固定显示过（armed=false 也显示）
global G_FeedLines := []            ; 环形缓冲：{text, tone, hh, mm}
global G_FeedX := 0
global G_FeedY := 0
global G_FeedW := 0
global G_FeedH := 0
global G_FeedUserX := ""            ; 用户拖过的位置（"" = 跟默认停靠走）
global G_FeedUserY := ""
global G_FeedDib := 0
global G_FeedBits := 0
global G_FeedMemDC := 0
global G_FeedScreenDC := 0
global G_FeedHookOn := false
global G_FeedDragging := false
global G_FeedMoved := false
global G_FeedDownX := 0
global G_FeedDownY := 0
global G_FeedWinX := 0
global G_FeedWinY := 0

; 外壳启动（PetStartup 里接线）：读 pet-state.json 的 feed_* 字段，建窗（先隐藏）。
PetFeedStartup() {
    global G_FeedGui, G_FeedHwnd, G_FeedUserHidden, G_FeedPinned, G_FeedUserX, G_FeedUserY
    global G_FeedW, G_FeedScreenDC, G_FeedHookOn
    if (G_FeedHwnd)
        return
    st := PetStateRead()
    ; v36 #245：两个显隐位从落盘进内存时先过统一写入口 —— 历史脏状态里
    ; 「feed_hidden=true + feed_pinned=true」并存过（× 不清 pinned 导致），
    ; 这里归一成「用户关过」，等下次开收句自动恢复。
    f := PetFeedVisFlags("load"
        , st.HasProp("feedHidden") ? st.feedHidden : false
        , st.HasProp("feedPinned") ? st.feedPinned : false)
    G_FeedUserHidden := f.userHidden
    G_FeedPinned := f.pinned
    if (st.HasProp("feedX") && st.feedX >= 0 && st.HasProp("feedY") && st.feedY >= 0) {
        G_FeedUserX := st.feedX
        G_FeedUserY := st.feedY
    }
    G_FeedW := P(PET_FEED_W_LP)
    g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x80000 +E0x08000000")
    g.MarginX := 0
    g.MarginY := 0
    g.BackColor := "000000"
    g.Show("x0 y0 w" . G_FeedW . " h" . P(40) . " NoActivate Hide")
    G_FeedGui := g
    G_FeedHwnd := g.Hwnd
    G_FeedScreenDC := DllCall("user32\GetDC", "Ptr", 0, "Ptr")
    if (!G_FeedHookOn) {
        OnMessage(0x201, PetFeedOnLButtonDown)   ; WM_LBUTTONDOWN
        OnMessage(0x200, PetFeedOnMouseMove)     ; WM_MOUSEMOVE
        OnMessage(0x202, PetFeedOnLButtonUp)     ; WM_LBUTTONUP
        G_FeedHookOn := true
    }
    PetFeedApplyVisibility()
    Log("信息流：面板就绪（宽 " . G_FeedW . "，缓冲 " . PET_FEED_MAX_LINES . " 条）")
}

; 外壳退出 / 关闭桌宠：收窗（缓冲留在内存里，重开回放）。
PetFeedShutdown() {
    global G_FeedGui, G_FeedHwnd, G_FeedShown, G_FeedDib, G_FeedMemDC
    G_FeedShown := false
    if (G_FeedGui)
        try G_FeedGui.Destroy()
    G_FeedGui := 0
    G_FeedHwnd := 0
    if (G_FeedDib) {
        try DllCall("gdi32\DeleteObject", "Ptr", G_FeedDib)
        G_FeedDib := 0
    }
    if (G_FeedMemDC) {
        try DllCall("gdi32\DeleteDC", "Ptr", G_FeedMemDC)
        G_FeedMemDC := 0
    }
}

; armed 边沿（PetWindow.PetApplyVisual 每拍喂）：false 自动隐藏、恢复自动再出现
; （用户固定过 / × 关过的手动优先，见 PetFeedShouldAutoShow）。
; v36 #245：**开收句 = 自动解除「用户隐藏」** —— × 不再等于永久失联。
PetFeedSetArmed(armed) {
    global G_FeedArmed, G_FeedUserHidden, G_FeedPinned
    armed := armed ? true : false
    if (G_FeedArmed = armed)
        return
    wasArmed := G_FeedArmed
    G_FeedArmed := armed
    if (armed && !wasArmed && G_FeedUserHidden) {
        ; 显隐位只由 PetFeedVisFlags 产出（auto = 只解 userHidden，pinned 不动）
        f := PetFeedVisFlags("auto", G_FeedUserHidden, G_FeedPinned)
        G_FeedUserHidden := f.userHidden
        G_FeedPinned := f.pinned
        try PetStateSave()
        Log("信息流：开收句自动解除用户隐藏（恢复自动显示）")
    }
    PetFeedApplyVisibility()
    Log("信息流：armed=" . (armed ? "on（自动显示）" : "off（自动隐藏）"))
}

; v36 收尾复核（US11 补齐）：armed 中换了会话（新视频）也自动解除「用户隐藏」
; ——原来只有 armed 上升沿恢复，armed 期间换视频点过 × 就永久失联。口径同
; PetFeedSetArmed：显隐位只由 PetFeedVisFlags 产出（auto = 只解 userHidden，
; pinned 不动）。已经在显示 / 没 hidden 过就不动。
PetFeedRestoreOnSession() {
    global G_FeedArmed, G_FeedUserHidden, G_FeedPinned
    if (!G_FeedArmed || !G_FeedUserHidden)
        return
    f := PetFeedVisFlags("auto", G_FeedUserHidden, G_FeedPinned)
    G_FeedUserHidden := f.userHidden
    G_FeedPinned := f.pinned
    try PetStateSave()
    Log("信息流：换会话自动解除用户隐藏（恢复自动显示）")
    PetFeedApplyVisibility()
}

; 按 armed / pinned / userHidden 三个位把可见性收敛到该在的状态。
PetFeedApplyVisibility() {
    global G_FeedArmed, G_FeedPinned, G_FeedUserHidden, G_FeedShown, G_FeedHwnd
    if (!G_FeedHwnd)
        return
    want := PetFeedShouldAutoShow(G_FeedArmed, G_FeedPinned, G_FeedUserHidden)
    if (want && !G_FeedShown)
        PetFeedShow()
    else if (!want && G_FeedShown)
        PetFeedHide()
}

; 追加一行（面板隐藏时也记账；超过 30 条丢最旧）。tone 非法回落 idle。
; 返回 true = 记上了（供调用方记日志/测试）。
PetFeedLine(text, tone) {
    global G_FeedLines, G_FeedShown
    t := Trim(text)
    if (t = "")
        return false
    if (!PetFeedToneValid(tone))
        tone := "idle"
    G_FeedLines.Push({ text: t, tone: tone, hh: A_Hour, mm: A_Min })
    G_FeedLines := PetFeedTrimLines(G_FeedLines, PET_FEED_MAX_LINES)
    if (G_FeedShown)
        PetFeedRender()
    return true
}

; v35-ux D4：move-only 跟随（拖动期用）。**不重绘、不重算布局**。
PetFeedMoveTo(x, y) {
    global G_FeedHwnd, G_FeedX, G_FeedY
    if (!G_FeedHwnd)
        return
    G_FeedX := x
    G_FeedY := y
    DllCall("SetWindowPos", "Ptr", G_FeedHwnd, "Ptr", 0, "Int", x, "Int", y
        , "Int", 0, "Int", 0, "UInt", 0x0015)   ; NOSIZE|NOZORDER|NOACTIVATE
}

; 按锚点重算默认停靠位（1Hz ping / 展开收起时的位置维护）。
; 用户拖过（G_FeedUserX 非空）→ 尊重用户位置不动；拖动中跳过。
PetFeedReanchor(ax, ay) {
    global G_FeedHwnd, G_FeedUserX, G_FeedDragging, G_FeedShown, G_PetDragging
    if (!G_FeedHwnd || !G_FeedShown)
        return
    ; v36 决策 11：跟随重排统一判同一个拖动谓词 —— 拖桌宠（1Hz ping 每秒回调这里）
    ; 与拖面板一样跳过，否则每秒把信息流拽回默认停靠位（诊断 §3.5）。
    if (PetDragActive(IsSet(G_PetDragging) && G_PetDragging, IsSet(G_FeedDragging) && G_FeedDragging))
        return                          ; 拖动中：位置由鼠标 move 维护
    if (G_FeedUserX != "" && G_FeedUserY != "")
        return                          ; 用户固定过位置，不跟默认停靠
    PetFeedPlaceAt()
}

; 菜单项入口：显示中 → 用户隐藏；隐藏中 → 固定显示（armed=false 也在）。
; v36 #245：与面板 × 共用 PetFeedVisFlags 这一份写入口径（互斥，不再出现
; 「已隐藏 + 已固定」并存）。
PetFeedToggle() {
    global G_FeedUserHidden, G_FeedPinned, G_FeedShown, G_FeedHwnd
    if (!G_FeedHwnd)
        return
    if (PetFeedVisible()) {
        f := PetFeedVisFlags("hidden", G_FeedUserHidden, G_FeedPinned)
        G_FeedUserHidden := f.userHidden
        G_FeedPinned := f.pinned
        PetFeedHide()
        Log("信息流：用户隐藏（× / 菜单）")
    } else {
        f := PetFeedVisFlags("shown", G_FeedUserHidden, G_FeedPinned)
        G_FeedUserHidden := f.userHidden
        G_FeedPinned := f.pinned
        PetFeedShow()
        Log("信息流：用户固定显示")
    }
    try PetStateSave()
}

; v36：主动开启（收句真的跑起来那一拍叫她打开）：清「用户 × 关过」+ 固定显示，
; 不改用户拖过的位置。已经显示着就不动（不闪面板）。返回 true = 这次打开了。
PetFeedOpenPinned() {
    global G_FeedUserHidden, G_FeedPinned, G_FeedShown, G_FeedHwnd
    if (!G_FeedHwnd)
        return false
    if (PetFeedVisible())
        return false
    f := PetFeedVisFlags("shown", G_FeedUserHidden, G_FeedPinned)
    G_FeedUserHidden := f.userHidden
    G_FeedPinned := f.pinned
    PetFeedShow()
    try PetStateSave()
    Log("信息流：随收句自动打开（pinned）")
    return true
}

PetFeedVisible() {
    global G_FeedHwnd, G_FeedShown
    return G_FeedHwnd != 0 && G_FeedShown && DllCall("IsWindowVisible", "Ptr", G_FeedHwnd)
}

PetFeedHide() {
    global G_FeedHwnd, G_FeedShown
    G_FeedShown := false
    if (G_FeedHwnd)
        DllCall("ShowWindow", "Ptr", G_FeedHwnd, "Int", 0)
}

PetFeedShow() {
    global G_FeedHwnd, G_FeedShown
    if (!G_FeedHwnd)
        return false
    PetFeedPlaceAt()
    PetFeedRender()
    if (!G_FeedShown) {
        DllCall("ShowWindow", "Ptr", G_FeedHwnd, "Int", 8)   ; SW_SHOWNA（不抢焦点）
        G_FeedShown := true
    }
    return true
}

; 落位：用户拖过 → 夹屏沿用；否则默认停靠角色左侧（垂直居中，留 12lp）；
; 左侧放不下换右侧。只记账 + move，不重绘（重绘由 PetFeedShow/Render 负责）。
PetFeedPlaceAt() {
    global G_FeedX, G_FeedY, G_FeedW, G_FeedH, G_FeedUserX, G_FeedUserY
    vr := VirtualScreenRect()
    w := G_FeedW
    h := (G_FeedH > 0) ? G_FeedH : P(120)
    if (G_FeedUserX != "" && G_FeedUserY != "") {
        x := Max(vr.x + 4, Min(G_FeedUserX, vr.x + vr.w - w - 4))
        y := Max(vr.y + 4, Min(G_FeedUserY, vr.y + vr.h - h - 4))
    } else if (IsSet(G_PetX) && G_PetX != "" && IsSet(G_PetW) && G_PetW > 0) {
        x := G_PetX - w - P(PET_FEED_DOCK_GAP_LP)
        y := G_PetY + (G_PetH - h) // 2
        if (x < vr.x + 4)
            x := G_PetX + G_PetW + P(PET_FEED_DOCK_GAP_LP)
        x := Max(vr.x + 4, Min(x, vr.x + vr.w - w - 4))
        y := Max(vr.y + 4, Min(y, vr.y + vr.h - h - 4))
    } else {
        x := vr.x + vr.w - w - P(24) + P(24)    ; 无角色锚点：贴右缘兜底
        x := Max(vr.x + 4, Min(x, vr.x + vr.w - w - 4))
        y := vr.y + vr.h - h - P(180)
        y := Max(vr.y + 4, y)
    }
    G_FeedX := x
    G_FeedY := y
}

; 正文占几行（1..PET_FEED_BODY_MAX）：按文字在正文宽内 DT_CALCRECT 折行后的高度算。
; 上限截断是有意的——竖向窄条里一条事件不许吃掉半屏（绘制层用 DT_END_ELLIPSIS 收尾）。
PetFeedBodyRows(e, textW) {
    rect := PetFeedMeasureText(e.text, textW, PET_FEED_BODY_PT)
    rowH := P(PET_FEED_ROW_LP)
    n := (rect.h + rowH - 1) // rowH
    return Max(1, Min(n, PET_FEED_BODY_MAX))
}

; 一条事件的像素高：时间行 + 正文 n 行 + 条间空隙。
PetFeedEntryH(e, textW) {
    return P(PET_FEED_TIME_H_LP) + PetFeedBodyRows(e, textW) * P(PET_FEED_ROW_LP)
         + P(PET_FEED_ENTRY_GAP_LP)
}

; 量文本（纯 GDI）：返回 { w, h }（DT_CALCRECT|DT_NOPREFIX|DT_WORDBREAK）。
PetFeedMeasureText(text, maxW, pt) {
    hdc := DllCall("gdi32\CreateCompatibleDC", "Ptr", 0, "Ptr")
    if (!hdc)
        return { w: maxW, h: P(PET_FEED_ROW_LP) }
    hfont := PetPanelFont(pt)
    old := DllCall("gdi32\SelectObject", "Ptr", hdc, "Ptr", hfont, "Ptr")
    rect := Buffer(16, 0)
    NumPut("Int", 0, rect, 0), NumPut("Int", 0, rect, 4)
    NumPut("Int", maxW, rect, 8), NumPut("Int", 0, rect, 12)
    DllCall("user32\DrawTextW", "Ptr", hdc, "Str", text, "Int", -1, "Ptr", rect
          , "UInt", 0x400 | 0x800 | 0x10)          ; DT_CALCRECT|DT_NOPREFIX|DT_WORDBREAK
    w := NumGet(rect, 8, "Int") - NumGet(rect, 0, "Int")
    h := NumGet(rect, 12, "Int") - NumGet(rect, 4, "Int")
    DllCall("gdi32\SelectObject", "Ptr", hdc, "Ptr", old)
    DllCall("gdi32\DeleteObject", "Ptr", hfont)
    DllCall("gdi32\DeleteDC", "Ptr", hdc)
    return { w: w, h: h }
}

; 建/更新面板：定尺寸（按显示段的像素高）→ 画 DIB → ULW 贴上去。
; 竖向布局（2026-11-10）：每条 = 上段时间小字 + 下段正文，正文宽 = 面板宽 - 两侧留白。
PetFeedRender() {
    global G_FeedGui, G_FeedHwnd, G_FeedW, G_FeedH, G_FeedX, G_FeedY
    global G_FeedDib, G_FeedBits, G_FeedMemDC, G_FeedScreenDC
    global G_FeedLines, G_FeedShown
    if (!G_FeedHwnd)
        return
    pad := P(PET_FEED_PAD_LP)
    headerH := P(PET_FEED_HEADER_LP)
    rowH := P(PET_FEED_ROW_LP)
    timeH := P(PET_FEED_TIME_H_LP)
    entryGap := P(PET_FEED_ENTRY_GAP_LP)
    w := G_FeedW
    textW := w - 2 * pad
    ; 显示段（最新最下；按像素高从新往旧累计，超预算丢最旧）
    view := PetFeedPickView(G_FeedLines, P(PET_FEED_VIEW_H_LP), (e) => PetFeedEntryH(e, textW))
    bodyH := 0
    for e in view
        bodyH += PetFeedEntryH(e, textW)
    ; 空缓冲占位（v35-ux 验收修复）：行全是边沿触发，刚启动/手动打开时缓冲可能
    ; 是空的——渲染两行灰字提示，别让用户看到一块只有 × 的空面板。
    placeholder := (bodyH < 1)
    if (placeholder)
        bodyH := 2 * rowH + entryGap
    else
        bodyH -= entryGap            ; 最后一条下面不留空隙
    h := 2 * pad + headerH + bodyH
    G_FeedH := h
    ; 32bpp DIB
    bi := Buffer(40, 0)
    NumPut("Int", 40, bi, 0)
    NumPut("Int", w, bi, 4), NumPut("Int", -h, bi, 8)
    NumPut("UShort", 1, bi, 12), NumPut("UShort", 32, bi, 14)
    NumPut("UInt", 0, bi, 16)
    bits := 0
    dib := DllCall("gdi32\CreateDIBSection", "Ptr", 0, "Ptr", bi, "UInt", 0
                 , "Ptr*", &bits, "Ptr", 0, "UInt", 0, "Ptr")
    if (!dib)
        return
    mem := DllCall("gdi32\CreateCompatibleDC", "Ptr", G_FeedScreenDC, "Ptr")
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", dib)
    ; 圆角面板 + 1px 亮边（alpha 由 PetPanelFixAlpha 统一补成半透明）
    brush := DllCall("gdi32\CreateSolidBrush", "UInt", 0x0028211B, "Ptr")
    pen := DllCall("gdi32\CreatePen", "Int", 0, "Int", 1, "UInt", 0x005D5245, "Ptr")
    oldB := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", brush, "Ptr")
    oldP := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", pen, "Ptr")
    DllCall("gdi32\RoundRect", "Ptr", mem, "Int", 0, "Int", 0, "Int", w - 1
          , "Int", h - 1, "Int", P(10), "Int", P(10))
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldB)
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldP)
    DllCall("gdi32\DeleteObject", "Ptr", brush)
    DllCall("gdi32\DeleteObject", "Ptr", pen)
    ; 右上角 ×（灰亮色，alpha 修复后不透明，可点）
    hfontX := PetPanelFont(10)
    oldF := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hfontX, "Ptr")
    DllCall("gdi32\SetBkMode", "Ptr", mem, "Int", 1)
    DllCall("gdi32\SetTextColor", "Ptr", mem, "UInt", 0xDAD0C9)     ; RGB C9D0DA
    xr := Buffer(16, 0)
    NumPut("Int", w - P(6) - P(PET_FEED_CLOSE_LP), xr, 0), NumPut("Int", 2, xr, 4)
    NumPut("Int", w - P(6), xr, 8), NumPut("Int", 2 + headerH, xr, 12)
    DllCall("user32\DrawTextW", "Ptr", mem, "Str", "×", "Int", -1, "Ptr", xr
          , "UInt", 0x20 | 0x4 | 0x800)                  ; DT_SINGLELINE|DT_VCENTER|DT_NOPREFIX
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldF)
    DllCall("gdi32\DeleteObject", "Ptr", hfontX)
    ; 竖向布局：一条事件 = 上段时间（8pt 灰） + 下段正文（9pt tone 色，最多 2 行）
    y := pad + headerH
    if (placeholder) {
        ; 空缓冲占位：两行灰字（窄面板放不下一行），面板不再是空的
        hfontP := PetPanelFont(PET_FEED_BODY_PT)
        oldPd := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hfontP, "Ptr")
        DllCall("gdi32\SetBkMode", "Ptr", mem, "Int", 1)
        DllCall("gdi32\SetTextColor", "Ptr", mem, "UInt", PetFeedToneColor("idle"))
        prc := Buffer(16, 0)
        NumPut("Int", pad, prc, 0), NumPut("Int", y, prc, 4)
        NumPut("Int", w - pad, prc, 8), NumPut("Int", y + 2 * rowH, prc, 12)
        DllCall("user32\DrawTextW", "Ptr", mem
              , "Str", "（暂无事件）`n开收句后，这里实时汇报", "Int", -1, "Ptr", prc
              , "UInt", 0x800 | 0x10)                    ; DT_NOPREFIX|DT_WORDBREAK
        DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldPd)
        DllCall("gdi32\DeleteObject", "Ptr", hfontP)
    }
    for e in view {
        n := PetFeedBodyRows(e, textW)
        ; 上段：时间（小号灰）
        hfontT := PetPanelFont(PET_FEED_TIME_PT)
        oldT := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hfontT, "Ptr")
        DllCall("gdi32\SetTextColor", "Ptr", mem, "UInt", 0xB2A49A)   ; RGB 9AA4B2
        tr := Buffer(16, 0)
        NumPut("Int", pad, tr, 0), NumPut("Int", y, tr, 4)
        NumPut("Int", w - pad, tr, 8), NumPut("Int", y + timeH, tr, 12)
        DllCall("user32\DrawTextW", "Ptr", mem
              , "Str", Format("{:02}:{:02}", e.hh, e.mm), "Int", -1, "Ptr", tr
              , "UInt", 0x20 | 0x4 | 0x800)              ; 单行垂直居中
        DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldT)
        DllCall("gdi32\DeleteObject", "Ptr", hfontT)
        ; 下段：正文（tone 色；超出行数用省略号截断，不让一条吃掉半屏）
        hfontS := PetPanelFont(PET_FEED_BODY_PT)
        oldS := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hfontS, "Ptr")
        DllCall("gdi32\SetTextColor", "Ptr", mem, "UInt", PetFeedToneColor(e.tone))
        sr := Buffer(16, 0)
        NumPut("Int", pad, sr, 0), NumPut("Int", y + timeH, sr, 4)
        NumPut("Int", w - pad, sr, 8), NumPut("Int", y + timeH + n * rowH, sr, 12)
        DllCall("user32\DrawTextW", "Ptr", mem, "Str", e.text, "Int", -1, "Ptr", sr
              , "UInt", 0x800 | 0x10 | 0x8000)           ; NOPREFIX|WORDBREAK|END_ELLIPSIS
        DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldS)
        DllCall("gdi32\DeleteObject", "Ptr", hfontS)
        y += timeH + n * rowH + entryGap
    }
    DllCall("gdi32\GdiFlush")
    ; 真半透明：面板像素预乘 alpha，文字像素保持 alpha 255
    PetPanelFixAlpha(bits, w, h, PET_FEED_ALPHA, PET_FEED_TEXT_MIN)
    ; 旧的 DIB/DC 释放
    if (G_FeedDib) {
        DllCall("gdi32\DeleteObject", "Ptr", G_FeedDib)
        DllCall("gdi32\DeleteDC", "Ptr", G_FeedMemDC)
    }
    G_FeedDib := dib
    G_FeedBits := bits
    G_FeedMemDC := mem
    ; 1:1 物理像素贴（GDI 缩放会清零 alpha）
    dst := Buffer(8, 0)
    NumPut("Int", G_FeedX, dst, 0), NumPut("Int", G_FeedY, dst, 4)
    size := Buffer(8, 0)
    NumPut("Int", w, size, 0), NumPut("Int", h, size, 4)
    blend := Buffer(4, 0)
    NumPut("UChar", 0, blend, 0), NumPut("UChar", 0, blend, 1)
    NumPut("UChar", 255, blend, 2), NumPut("UChar", 1, blend, 3)
    pt := Buffer(8, 0)
    DllCall("user32\UpdateLayeredWindow", "Ptr", G_FeedHwnd, "Ptr", G_FeedScreenDC
          , "Ptr", dst, "Ptr", size, "Ptr", G_FeedMemDC, "Ptr", pt
          , "UInt", 0, "Ptr", blend, "UInt", 2)
}

; × 热区（客户区坐标）：右上角 PET_FEED_CLOSE_LP 见方。
PetFeedCloseHit(cx, cy) {
    global G_FeedW
    x1 := G_FeedW - P(6) - P(PET_FEED_CLOSE_LP)
    y1 := 0
    x2 := G_FeedW - P(2)
    y2 := P(PET_FEED_HEADER_LP) + P(4)
    return (cx >= x1 && cx <= x2 && cy >= y1 && cy <= y2)
}

; --- 鼠标：拖面板 / 点 × 关闭 -------------------------------------------------
PetFeedOnLButtonDown(wParam, lParam, msg, hwnd) {
    global G_FeedHwnd, G_FeedDragging, G_FeedMoved, G_FeedDownX, G_FeedDownY
    global G_FeedWinX, G_FeedWinY, G_FeedX, G_FeedY
    if (hwnd != G_FeedHwnd)
        return
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    G_FeedDownX := NumGet(pt, 0, "Int"), G_FeedDownY := NumGet(pt, 4, "Int")
    G_FeedWinX := G_FeedX, G_FeedWinY := G_FeedY
    G_FeedDragging := true
    G_FeedMoved := false
    return 0
}

PetFeedOnMouseMove(wParam, lParam, msg, hwnd) {
    global G_FeedHwnd, G_FeedDragging, G_FeedMoved, G_FeedDownX, G_FeedDownY
    global G_FeedWinX, G_FeedWinY
    if (hwnd != G_FeedHwnd || !G_FeedDragging)
        return
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    cx := NumGet(pt, 0, "Int"), cy := NumGet(pt, 4, "Int")
    dx := cx - G_FeedDownX, dy := cy - G_FeedDownY
    if (!G_FeedMoved && Abs(dx) + Abs(dy) < 4)
        return
    G_FeedMoved := true
    PetFeedMoveTo(G_FeedWinX + dx, G_FeedWinY + dy)
    return 0
}

PetFeedOnLButtonUp(wParam, lParam, msg, hwnd) {
    global G_FeedHwnd, G_FeedDragging, G_FeedMoved, G_FeedX, G_FeedY
    global G_FeedUserX, G_FeedUserY, G_FeedUserHidden, G_FeedPinned
    if (hwnd != G_FeedHwnd)
        return
    wasMoved := G_FeedMoved
    G_FeedDragging := false
    G_FeedMoved := false
    if (!wasMoved) {
        ; 单击：落在 × 热区 → 关闭（用户手动优先）；其余位置不动
        pt := Buffer(8, 0)
        DllCall("GetCursorPos", "Ptr", pt)
        wr := Buffer(16, 0)
        DllCall("GetWindowRect", "Ptr", G_FeedHwnd, "Ptr", wr)
        cx := NumGet(pt, 0, "Int") - NumGet(wr, 0, "Int")
        cy := NumGet(pt, 4, "Int") - NumGet(wr, 4, "Int")
        if (PetFeedCloseHit(cx, cy)) {
            ; v36 #245：× 与菜单项写同一组位（互斥）—— 过去只写 userHidden、
            ; 不清 pinned，落盘出现「已隐藏 + 已固定」矛盾态，pinned 变死位。
            f := PetFeedVisFlags("hidden", G_FeedUserHidden, G_FeedPinned)
            G_FeedUserHidden := f.userHidden
            G_FeedPinned := f.pinned
            PetFeedHide()
            try PetStateSave()
            Log("信息流：× 关闭（用户隐藏；下次开收句自动恢复，菜单可固定）")
        }
        return 0
    }
    ; 拖完：记住用户位置 + 落 pet-state.json（松手才写，拖动中不写盘）
    G_FeedUserX := G_FeedX
    G_FeedUserY := G_FeedY
    try PetStateSave()
    Log("信息流：位置已记住 x=" . G_FeedX . " y=" . G_FeedY)
    return 0
}
