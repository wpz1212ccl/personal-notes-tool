; ============================================================================
;  lib/PetBubble.ahk —— v35 #233：桌宠气泡 + 待挑计数低频轮询
;  v35-ux D2 重做（2026-11-09）：真半透明逐像素 alpha + move-only 跟随
;
;  spec：docs/specs/v35-desktop-pet-and-status-overlay.md §气泡与引导；票 #233。
;
;  形态：
;    · 分层窗口（WS_EX_LAYERED + UpdateLayeredWindow 逐像素 alpha）——
;      **不用整窗 alpha**（WinSetTransparent 会糊字，也不给逐像素透明）；
;    · 面板像素 alpha=205（预乘 BGRA，真半透明）；**文字像素 alpha=255**
;      （预乘后不透明，保证字不跟着面板一起变淡）；
;    · 锚在角色头顶、尖角朝下指向角色（带描边）；一次只显示一条；到点自己收；
;    · 不出声、零外部请求（文案全在 Logic.ahk 的常量表里）；
;    · **拖动跟手（v35-ux D4）**：`PetBubbleFollow` 与 `PetBubbleMoveTo` 只做
;      SetWindowPos 移动，**不重建 DIB** —— 整幅重绘是拖动卡顿的根因。
;
;  纯逻辑（文案表 / 同类 10 分钟冷却 / 勿扰 / 一次只一条 / 4 步引导判据）都在
;  lib/Logic.ahk 的 #233 段；本文件只做「窗口 + 轮询」。
;
;  v36 #244：相位提醒（PetPhaseAskKind）与 4 步引导不再自己弹气泡 —— 它们各自
;  产出候选，交给外壳的**唯一发言出口**（英语取词.ahk 的 PetSpeechMaybe →
;  PetSpeechSay），由纯函数 PetSpeechArbitrate 统一排优先级与闸门。本文件只剩
;  「窗口 + 待挑计数轮询 + 候选构造（PetTourCandidate）」。
;
;  待挑计数：**不走 1Hz ping**（那会变成每秒一次 DB 查询、也违反扁平字段约定），
;  走 60 秒一次的低频轮询（GET /api/sessions，复用取词台的会话摘要，
;  `subtitle_pending` 求和——解析是 Logic.PetPendingCountFromSessions 纯函数）。
;
;  接线：角色窗移动时调 PetBubbleFollow(x, y)（1Hz ping）/ PetBubbleMoveTo(x, y)
;  （拖动期，PetWindow 的 PetOnMouseMove）；情境提示由外壳调
;  情境提示已改投候选给唯一发言出口（PetSpeechMaybe）；引导进度 Map 与勿扰位由
;  pet-state（#232）保存，重放/跳过调 Logic 的 PetTourReplay / PetTourSkipAll。
; ============================================================================

global G_BubGui := 0
global G_BubHwnd := 0
global G_BubShown := false
global G_BubKind := ""
global G_BubText := ""
global G_BubHideTimer := 0
global G_BubAnchorX := 0
global G_BubAnchorY := 0
global G_BubFollowed := false
global G_BubLastAt := Map()          ; kind → 上次弹出时刻（A_TickCount）
global G_BubDib := 0
global G_BubBits := 0
global G_BubMemDC := 0
global G_BubScreenDC := 0
global G_BubW := 0                   ; 当前气泡物理尺寸（move-only 跟随要用）
global G_BubH := 0
global G_BubX := 0                   ; 当前窗口物理位置（拖动平移的基准）
global G_BubY := 0
global G_BubPending := 0             ; 最近一次轮询到的待挑总数
global G_BubPendingAt := 0           ; 上次轮询时刻（0 = 还没轮询过）
global G_BubEnabled := true          ; 总开关：pet 不可用（素材缺失/关闭）时气泡全静默

; --- v36：可交互气泡（0/1 个按钮；只有带按钮的气泡才开 50ms 轮询）-------------
; 气泡窗口仍是整块点击穿透（PetBubbleHitTest 恒 HTTRANSPARENT），所以按钮命中
; 不走窗口消息，走 PetBubbleWatch 的全局轮询（PetMenuWatch 同款：先等松开，
; 再认下一次按下；Esc / 点面板外收，按按钮 = 先收再执行）。
global G_BubButtons := []            ; 按钮数组（0/1 个：{label, fn}）
global G_BubBtnRect := 0             ; 按钮热区（客户区物理像素 {x,y,w,h}；无按钮 = 0）
global G_BubWatchOn := false         ; 轮询开着吗
global G_BubWaitRelease := false     ; 先等按下松开，再认下一次按下
global PET_BUBBLE_BTN_H_LP := 26     ; 按钮高
global PET_BUBBLE_BTN_GAP_LP := 8    ; 文字与按钮的间距

; #234 降级路径③：素材目录缺失 / 用户关闭桌宠 → 气泡不出现（状态退回 HUD）。
; 外壳在 pet 状态变化时调这里；未接线时默认开。
PetBubbleSetEnabled(enabled) {
    global G_BubEnabled
    G_BubEnabled := enabled ? true : false
    if (!G_BubEnabled)
        PetBubbleReset()
}

global PET_BUBBLE_MS := 8000         ; 一条气泡自己收掉的时限
global PET_BUBBLE_PAD_LP := 14       ; 气泡内边距（v35-ux D2：12 → 14）
global PET_BUBBLE_TAIL_LP := 9       ; 尖角高度
global PET_BUBBLE_FONT_PT := 11      ; 正文字号（v35-ux D2：10pt 太小 → 11pt）
global PET_BUBBLE_ALPHA := 205       ; 面板像素 alpha（v35-ux D2：真半透明）
global PET_BUBBLE_TEXT_MIN := 140    ; 亮度阈值：max 通道 ≥ 它的像素按文字处理（alpha 255）
global PET_TOUR_STEP_MS := 12000     ; 引导两步之间至少隔这么久（防连弹四条）
; 2026-11-10 真机反馈「我都没能点击」：带按钮的气泡要读、要瞄准、要点，8 秒不够。
global PET_BUBBLE_BTN_MS := 20000    ; 带按钮气泡的存活时长（鼠标停在上面还会自动续命）
global G_TourAt := 0                 ; 上一条引导气泡的时刻（A_TickCount）

; 直接弹一条（kind 用于冷却记账；text 为空则忽略）。
PetBubbleShow(kind, text) {
    global G_BubShown, G_BubKind, G_BubLastAt, G_BubHideTimer, G_BubButtons, G_BubBtnRect
    if (Trim(text) = "")
        return false
    G_BubButtons := []                  ; 普通气泡无按钮
    G_BubBtnRect := 0
    PetBubbleBuild(text)
    G_BubKind := kind
    G_BubLastAt[kind] := A_TickCount
    G_BubShown := true
    if (G_BubHideTimer)
        SetTimer(PetBubbleAutoHide, 0)
    G_BubHideTimer := 1
    SetTimer(PetBubbleAutoHide, -PET_BUBBLE_MS)
    return true
}

; ---------------------------------------------------------------------------
;  v36：可交互气泡（0/1 个按钮）。窗口整块穿透（点视频照旧），按钮命中走
;  PetBubbleWatch 的全局轮询；Esc / 点面板外收（先收再执行，PetMenuOnLButtonUp
;  同款手感）；到点自己收（与普通气泡同款 PET_BUBBLE_MS）。
; ---------------------------------------------------------------------------

; 带单按钮的气泡：text + {label, fn}。fn 执行前气泡必已收起。返回 true = 弹了。
PetBubbleShowEx(kind, text, label, fn) {
    global G_BubShown, G_BubKind, G_BubLastAt, G_BubHideTimer, G_BubButtons
    if (Trim(text) = "" || label = "" || !IsObject(fn))
        return false
    G_BubButtons := [{label: label, fn: fn}]
    PetBubbleBuild(text)
    G_BubKind := kind
    G_BubLastAt[kind] := A_TickCount
    G_BubShown := true
    if (G_BubHideTimer)
        SetTimer(PetBubbleAutoHide, 0)
    G_BubHideTimer := 1
    SetTimer(PetBubbleAutoHide, -PET_BUBBLE_BTN_MS)
    PetBubbleWatchStart()
    return true
}

; 50ms 轮询（只服务带按钮的气泡）：命中按钮热区才执行；Esc / 点面板外收。
PetBubbleWatchStart() {
    global G_BubWatchOn, G_BubWaitRelease
    if (!G_BubWatchOn) {
        SetTimer(PetBubbleWatch, 50)
        G_BubWatchOn := true
    }
    G_BubWaitRelease := false
}

PetBubbleWatchStop() {
    global G_BubWatchOn
    if (G_BubWatchOn) {
        SetTimer(PetBubbleWatch, 0)
        G_BubWatchOn := false
    }
}

PetBubbleWatch() {
    global G_BubShown, G_BubWaitRelease, G_BubBtnRect, G_BubX, G_BubY, G_BubW, G_BubH
    global G_PetDragging
    if (!G_BubShown) {
        PetBubbleWatchStop()
        return
    }
    ; v36 决策 11：拖桌宠的那一次按下不算「点了外面」——气泡跟着她一起走
    ; （否则一按下去气泡就被自己收掉，四个浮层只剩三个）。
    if (PetDragActive(IsSet(G_PetDragging) && G_PetDragging))
        return
    escDown := GetKeyState("Esc", "P")
    lDown := GetKeyState("LButton", "P")
    if (!G_BubWaitRelease) {
        if (!lDown && !escDown)
            G_BubWaitRelease := true       ; 弹出那拍的按键已松开
        return
    }
    if (escDown) {
        PetBubbleHide()
        return
    }
    if (!lDown)
        return
    ; 新的一次按下：按钮热区内 → 执行；其余（面板内非按钮 / 面板外）→ 收
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    mx := NumGet(pt, 0, "Int"), my := NumGet(pt, 4, "Int")
    cx := mx - G_BubX, cy := my - G_BubY
    inBtn := false
    if (IsObject(G_BubBtnRect))
        inBtn := (cx >= G_BubBtnRect.x && cx <= G_BubBtnRect.x + G_BubBtnRect.w
               && cy >= G_BubBtnRect.y && cy <= G_BubBtnRect.y + G_BubBtnRect.h)
    fn := (G_BubButtons.Length > 0) ? G_BubButtons[1].fn : 0
    PetBubbleHide()                        ; 先收再执行
    if (inBtn && IsObject(fn))
        fn()
}

; 到点自动收（定时器入口）。与「显式收」（Esc / 点外面 / 点按钮）分开：
; 只有自动收才看光标——带按钮的气泡，鼠标停在气泡上就说明用户正在瞄准，
; 续 5 秒再判，别把他手上的点击抢掉。
PetBubbleAutoHide() {
    global G_BubShown, G_BubBtnRect
    if (!G_BubShown)
        return
    if (IsObject(G_BubBtnRect) && PetBubbleCursorInside()) {
        SetTimer(PetBubbleAutoHide, -5000)
        return
    }
    PetBubbleHide()
}

; 光标是否在气泡窗口矩形内（物理像素）。
PetBubbleCursorInside() {
    global G_BubX, G_BubY, G_BubW, G_BubH
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    mx := NumGet(pt, 0, "Int"), my := NumGet(pt, 4, "Int")
    return (mx >= G_BubX && mx <= G_BubX + G_BubW && my >= G_BubY && my <= G_BubY + G_BubH)
}

PetBubbleHide() {
    global G_BubGui, G_BubShown, G_BubHideTimer, G_BubBtnRect, G_BubButtons
    G_BubShown := false
    G_BubHideTimer := 0
    G_BubBtnRect := 0
    G_BubButtons := []
    try SetTimer(PetBubbleAutoHide, 0)
    PetBubbleWatchStop()
    if (G_BubGui)
        try G_BubGui.Hide()
}

; 中枢掉线 / 关闭桌宠：收起并清掉「刚弹过」的记忆（下次恢复还能提醒）。
PetBubbleReset() {
    global G_BubLastAt
    G_BubLastAt := Map()
    PetBubbleHide()
}

; 外壳启动：待挑计数 60 秒低频轮询（先立刻轮一次，之后每 60s 一拍）。
PetBubbleStartup() {
    SetTimer(PetPendingTimer, 60000)
    PetPendingTimer()
}

; 外壳退出 / 关闭桌宠：停轮询 + 收气泡。
PetBubbleShutdown() {
    try SetTimer(PetPendingTimer, 0)
    PetBubbleWatchStop()
    PetBubbleHide()
}

; #232 角色窗接线口：锚点 = 角色头顶中心（物理像素）。
; v35-ux D4：**只记账 + move-only**，不再整幅重绘 —— 气泡位置变化是纯平移，
; 像素一个都不用重画；1Hz ping 的 Follow 在拖动中跳过重排（拖动由
; PetWindow.PetOnMouseMove 的 PetBubbleMoveTo 同帧带走）。
PetBubbleFollow(x, y) {
    global G_BubAnchorX, G_BubAnchorY, G_BubFollowed, G_BubShown
    global G_BubW, G_BubH, G_PetDragging
    G_BubAnchorX := x
    G_BubAnchorY := y
    G_BubFollowed := true
    if (!G_BubShown || G_BubW <= 0)
        return
    if (PetDragActive(IsSet(G_PetDragging) && G_PetDragging))
        return                          ; 拖动中：位置由 PetOnMouseMove 维护，不重算布局
    PetBubblePlaceAt()
}

; v35-ux D4：move-only 跟随（拖动期用）。**不重建 DIB、不重算布局**，
; 一个 SetWindowPos 完事；x/y 是目标左上角（物理像素）。
PetBubbleMoveTo(x, y) {
    global G_BubHwnd, G_BubX, G_BubY
    if (!G_BubHwnd)
        return
    G_BubX := x
    G_BubY := y
    DllCall("SetWindowPos", "Ptr", G_BubHwnd, "Ptr", 0, "Int", x, "Int", y
        , "Int", 0, "Int", 0, "UInt", 0x0015)   ; NOSIZE|NOZORDER|NOACTIVATE
}

; 按锚点重算夹屏后的左上角并 move-only（非拖动期的位置维护）。
PetBubblePlaceAt() {
    global G_BubW, G_BubH
    if (G_BubW <= 0 || G_BubH <= 0)
        return
    a := PetBubbleAnchor()
    vr := VirtualScreenRect()
    x := Max(vr.x + 4, Min(a.x - G_BubW // 2, vr.x + vr.w - G_BubW - 4))
    y := Max(vr.y + 4, a.y - G_BubH)
    PetBubbleMoveTo(x, y)
}

; 默认锚点（#232 接线前）：右下角 260×340lp 角色的头顶中心，与玻璃卡同一口径。
PetBubbleAnchor() {
    global G_BubAnchorX, G_BubAnchorY, G_BubFollowed
    if (G_BubFollowed)
        return { x: G_BubAnchorX, y: G_BubAnchorY }
    vr := VirtualScreenRect()
    return { x: vr.x + vr.w - P(24) - P(130)
           , y: vr.y + vr.h - P(24) - P(340) }
}

; 建/更新气泡窗口：定尺寸 → 画 DIB（圆角面板 + 尖角 + 文字 [+ 按钮]）→ ULW 贴上去。
PetBubbleBuild(text) {
    global G_BubGui, G_BubHwnd, G_BubText, G_BubShown, G_BubW, G_BubH, G_BubX, G_BubY
    global G_BubButtons, G_BubBtnRect
    G_BubText := text
    maxW := P(340) - 2 * P(PET_BUBBLE_PAD_LP)
    ; 先量文字（DT_CALCRECT 拿换行后的宽高）
    m := PetBubbleMeasure(text, maxW)
    pad := P(PET_BUBBLE_PAD_LP)
    tail := P(PET_BUBBLE_TAIL_LP)
    ; v36：带按钮时文字下方留一行按钮（高 26lp + 上方 8lp 间距）
    hasBtn := (G_BubButtons.Length > 0)
    btnH := hasBtn ? P(PET_BUBBLE_BTN_H_LP) : 0
    btnGap := hasBtn ? P(PET_BUBBLE_BTN_GAP_LP) : 0
    w := m.w + 2 * pad
    h := m.h + 2 * pad + tail + btnGap + btnH
    a := PetBubbleAnchor()
    vr := VirtualScreenRect()
    x := Max(vr.x + 4, Min(a.x - w // 2, vr.x + vr.w - w - 4))
    y := Max(vr.y + 4, a.y - h)
    if (!G_BubGui) {
        ; WS_EX_LAYERED 必须 Show 前设；NOACTIVATE 不抢焦点；TOOLWINDOW 不进任务栏。
        ; 不加 WS_EX_TRANSPARENT：透明区穿透靠 WM_NCHITTEST 返回 HTTRANSPARENT。
        g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x80000 +E0x08000000")
        g.MarginX := 0
        g.MarginY := 0
        g.BackColor := "000000"
        g.Show(Format("x{} y{} w{} h{} NoActivate Hide", x, y, w, h))
        G_BubGui := g
        G_BubHwnd := g.Hwnd
        OnMessage(0x0084, PetBubbleHitTest)      ; WM_NCHITTEST
        PetBubbleInitDib()
    }
    PetBubblePaint(w, h, m, pad, tail, hasBtn)
    ; 1:1 物理像素贴（GDI 缩放会清零 alpha）
    dst := Buffer(8, 0)
    NumPut("Int", x, dst, 0), NumPut("Int", y, dst, 4)
    size := Buffer(8, 0)
    NumPut("Int", w, size, 0), NumPut("Int", h, size, 4)
    blend := Buffer(4, 0)
    NumPut("UChar", 0, blend, 0), NumPut("UChar", 0, blend, 1)
    NumPut("UChar", 255, blend, 2), NumPut("UChar", 1, blend, 3)
    pt := Buffer(8, 0)
    DllCall("user32\UpdateLayeredWindow", "Ptr", G_BubHwnd, "Ptr", G_BubScreenDC
          , "Ptr", dst, "Ptr", size, "Ptr", G_BubMemDC, "Ptr", pt
          , "UInt", 0, "Ptr", blend, "UInt", 2)
    G_BubW := w, G_BubH := h
    G_BubX := x, G_BubY := y
    ; 第一次 ULW 完再显示，避免「先黑后透明」闪一下
    if (!G_BubShown)
        WinShow("ahk_id " . G_BubHwnd)
}

; 一次建好 1:1 的 DIB + 内存 DC（尺寸随气泡变，直接复用这块可容纳上限的 DIB？）
; 尺寸每次不同 → 每次重建最省心（气泡几秒才一次，代价可忽略）。
PetBubbleInitDib() {
    global G_BubScreenDC
    G_BubScreenDC := DllCall("user32\GetDC", "Ptr", 0, "Ptr")
}

; 量文字：用 DT_CALCRECT 拿换行后的真实宽高（纯 GDI，不建控件）。
PetBubbleMeasure(text, maxW) {
    hdc := DllCall("gdi32\CreateCompatibleDC", "Ptr", 0, "Ptr")
    if (!hdc)
        return { w: maxW, h: P(20) }
    hfont := PetPanelFont(PET_BUBBLE_FONT_PT)
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
    return { w: Max(P(60), w), h: Max(P(20), h) }
}

; 画气泡：圆角半透明深色面板 + 1px 半透明亮边 + 底部尖角（带描边）+ 文字 [+ 按钮]。
PetBubblePaint(w, h, m, pad, tail, hasBtn := false) {
    global G_BubDib, G_BubBits, G_BubMemDC, G_BubText, G_BubScreenDC
    global G_BubButtons, G_BubBtnRect
    ; 新 DIB（尺寸变了就重建；旧的处理掉）
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
    mem := DllCall("gdi32\CreateCompatibleDC", "Ptr", G_BubScreenDC, "Ptr")
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", dib)
    ; 面板底（#1B2128）与亮边（#45525D）—— alpha 由 PetPanelFixAlpha 统一补
    brush := DllCall("gdi32\CreateSolidBrush", "UInt", 0x0028211B, "Ptr")
    pen := DllCall("gdi32\CreatePen", "Int", 0, "Int", 1, "UInt", 0x005D5245, "Ptr")
    oldB := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", brush, "Ptr")
    oldP := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", pen, "Ptr")
    rc := Buffer(16, 0)
    NumPut("Int", 0, rc, 0), NumPut("Int", 0, rc, 4)
    NumPut("Int", w - 1, rc, 8), NumPut("Int", h - tail - 1, rc, 12)
    DllCall("gdi32\RoundRect", "Ptr", mem, "Int", 0, "Int", 0, "Int", w - 1
          , "Int", h - tail - 1, "Int", P(12), "Int", P(12))
    ; 尖角（朝下，指向角色；Polygon 自带描边）
    cx := w // 2
    tri := Buffer(24, 0)
    NumPut("Int", cx - P(8), tri, 0), NumPut("Int", h - tail - 1, tri, 4)
    NumPut("Int", cx + P(8), tri, 8), NumPut("Int", h - tail - 1, tri, 12)
    NumPut("Int", cx, tri, 16), NumPut("Int", h - 1, tri, 20)
    DllCall("gdi32\Polygon", "Ptr", mem, "Ptr", tri, "Int", 3)
    ; 文字
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldB)
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldP)
    DllCall("gdi32\DeleteObject", "Ptr", brush)
    DllCall("gdi32\DeleteObject", "Ptr", pen)
    hfont := PetPanelFont(PET_BUBBLE_FONT_PT)
    oldF := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", hfont, "Ptr")
    DllCall("gdi32\SetBkMode", "Ptr", mem, "Int", 1)        ; TRANSPARENT
    DllCall("gdi32\SetTextColor", "Ptr", mem, "UInt", 0x00F8F4F2)
    tr := Buffer(16, 0)
    NumPut("Int", pad, tr, 0), NumPut("Int", pad, tr, 4)
    ; 绘制矩形给到面板内沿（不要卡在实测文字宽度上：差 1px 就会被 DT_WORDBREAK 折行）
    NumPut("Int", w - pad, tr, 8), NumPut("Int", h - tail - pad, tr, 12)
    DllCall("user32\DrawTextW", "Ptr", mem, "Str", G_BubText, "Int", -1, "Ptr", tr
          , "UInt", 0x800 | 0x10 | 0x100)                     ; DT_NOPREFIX|DT_WORDBREAK|DT_NOCLIP
    DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldF)
    DllCall("gdi32\DeleteObject", "Ptr", hfont)
    ; v36：按钮（0/1 个）：文字下方通栏圆角块 + 居中亮字；热区记给 PetBubbleWatch。
    G_BubBtnRect := 0
    if (hasBtn && G_BubButtons.Length > 0) {
        btnH := P(PET_BUBBLE_BTN_H_LP)
        bx := pad
        by := h - tail - pad - btnH          ; 底部内沿贴着（面板内沿 = h-tail-pad）
        bw := w - 2 * pad
        bBrush := DllCall("gdi32\CreateSolidBrush", "UInt", 0x00363B33, "Ptr")   ; RGB 333B36
        bPen := DllCall("gdi32\CreatePen", "Int", 0, "Int", 1, "UInt", 0x00667A5A, "Ptr")
        oldB2 := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", bBrush, "Ptr")
        oldP2 := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", bPen, "Ptr")
        DllCall("gdi32\RoundRect", "Ptr", mem, "Int", bx, "Int", by
              , "Int", bx + bw, "Int", by + btnH, "Int", P(8), "Int", P(8))
        DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldB2)
        DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldP2)
        DllCall("gdi32\DeleteObject", "Ptr", bBrush)
        DllCall("gdi32\DeleteObject", "Ptr", bPen)
        ; 按钮字：13px 亮字（≥ 文字阈值 → alpha 255，不随面板变淡）
        bfont := PetPanelFont(PET_BUBBLE_FONT_PT)
        oldF2 := DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", bfont, "Ptr")
        DllCall("gdi32\SetTextColor", "Ptr", mem, "UInt", 0x00E8F0D8)   ; RGB D8F0E8
        br := Buffer(16, 0)
        NumPut("Int", bx, br, 0), NumPut("Int", by, br, 4)
        NumPut("Int", bx + bw, br, 8), NumPut("Int", by + btnH, br, 12)
        label := G_BubButtons[1].label
        DllCall("user32\DrawTextW", "Ptr", mem, "Str", label, "Int", -1, "Ptr", br
              , "UInt", 0x20 | 0x4 | 0x800)                 ; SINGLELINE|VCENTER|NOPREFIX
        DllCall("gdi32\SelectObject", "Ptr", mem, "Ptr", oldF2)
        DllCall("gdi32\DeleteObject", "Ptr", bfont)
        G_BubBtnRect := { x: bx, y: by, w: bw, h: btnH }
    }
    DllCall("gdi32\GdiFlush")
    ; v35-ux D2：真半透明。GDI 往 32bpp BI_RGB DIB 上画**不管 alpha 字节**——
    ; 面板像素补成预乘 alpha（PET_BUBBLE_ALPHA），文字像素保持 alpha 255（不跟着变淡）。
    PetPanelFixAlpha(bits, w, h, PET_BUBBLE_ALPHA, PET_BUBBLE_TEXT_MIN)
    ; 旧的 DIB/DC 释放
    if (G_BubDib) {
        DllCall("gdi32\DeleteObject", "Ptr", G_BubDib)
        DllCall("gdi32\DeleteDC", "Ptr", G_BubMemDC)
    }
    G_BubDib := dib
    G_BubBits := bits
    G_BubMemDC := mem
}

; 气泡不接收任何点击：整块穿透（点她/点视频都照旧）。返回 -1 = HTTRANSPARENT。
PetBubbleHitTest(wParam, lParam, msg, hwnd) {
    global G_BubHwnd
    if (hwnd != G_BubHwnd)
        return
    return -1
}

; ---------------------------------------------------------------------------
;  半透明面板的公共绘制件（v36 票 #243 起移到 lib/PanelPaint.ahk，三个浮层共用）
; ---------------------------------------------------------------------------

; ---------------------------------------------------------------------------
;  待挑计数：60 秒一次的低频轮询（不进 1Hz ping）
; ---------------------------------------------------------------------------

; 走本地取词台 HTTP（127.0.0.1:<web_port>），不碰外网；失败返回 -1（下一拍再试）。
PetPendingPoll() {
    global G_WebPort
    port := IsSet(G_WebPort) ? G_WebPort : 0
    if (!port)
        return -1
    ; 2026-11-10：中枢还没起时，WinHttp 连一个没人听的端口要**等满 2 秒**
    ; （实测：外壳启动后这一步把「桌宠就绪 → 拉起中枢」拖到 4.1 秒）。
    ; 先用 15ms 的裸 connect 探活，没人听就直接当这次没轮询。
    if (!PortListening(port))
        return -1
    try {
        req := ComObject("WinHttp.WinHttpRequest.5.1")
        req.Open("GET", "http://127.0.0.1:" . port . "/api/sessions", false)
        req.SetTimeouts(1000, 1000, 2000, 2000)
        req.Send()
        if (req.Status != 200)
            return -1
        return PetPendingCountFromSessions(req.ResponseText)
    } catch {
        return -1
    }
}

; 轮询到点才真的发请求；返回 -1 = 这次没轮询/失败。
PetPendingTick() {
    global G_BubPendingAt, G_BubPending
    now := A_TickCount
    if (!PetPendingDue(G_BubPendingAt, now))
        return -1
    G_BubPendingAt := now
    n := PetPendingPoll()
    if (n >= 0)
        G_BubPending := n
    return n
}

; ---------------------------------------------------------------------------
;  接线口（#232 PetWindow / 外壳 ping 的调用点）
;
;  #244 起这里不再有「自己弹」的入口：相位提醒由 PetPhaseAskKind（Logic）定种类、
;  由外壳的 1Hz 接线投候选；4 步引导由 PetTourCandidate 产出候选。两者都进
;  英语取词.ahk 的唯一发言出口（PetSpeechMaybe → PetSpeechSay）。
; ---------------------------------------------------------------------------

; 待挑计数：外壳的 60 秒 timer 调这里（内部再按 PET_PENDING_POLL_MS 判到点）。
; 有 N 句待挑就投一条 pending 候选给唯一发言出口（#244 单出口纪律：本文件不
; 自己弹）；文案与 10 分钟同类冷却仍由 Logic 的 PetBubbleChoose 管，闸门与全局
; 节奏由 PetSpeechMaybe 统一裁决（勿扰 / 占位 / 冷却挡了就是不说，只留日志）。
PetPendingTimerTick(cfg) {
    global G_GlassShown, G_BubLastAt, G_PetDnd
    if (!G_BubEnabled || !PetHealthy())
        return -1
    if (IsSet(G_GlassShown) && G_GlassShown)
        return -1
    n := PetPendingTick()
    if (n > 0) {
        quiet := (IsSet(G_PetDnd) && G_PetDnd)
        d := PetBubbleChoose(["pending"], cfg, { pending: n }, G_BubLastAt, A_TickCount, quiet)
        if (IsObject(d))
            PetSpeechMaybe([{ kind: d.kind, text: d.text, source: "待挑" }])
    }
    return n
}

; 外壳的 SetTimer 回调形状（SetTimer 不带参数，这里包一层拿全局配置）。
PetPendingTimer() {
    global G_Cfg
    PetPendingTimerTick(G_Cfg)
}

; 4 步引导的**候选**（1Hz ping 里每拍调一次；#244：只产出候选，不自己弹）。
; 进度存 pet-core 的 G_PetGuide（0..4 = 已讲几步）。一次只讲一步、步间至少
; PET_TOUR_STEP_MS；缺啥补啥的跳过由 PetTourNext 判据完成。返回候选对象
; {kind:"tour", text, action:"", source, onSaid} 或 ""（这一拍没得讲）。
; 讲解进度只在**真的说出来之后**推进（onSaid），被高优先级抢了下一拍再讲。
PetTourCandidate(cfg) {
    global G_PetGuide, G_PetDnd, G_BubShown, G_TourAt, G_GlassShown
    if (!G_BubEnabled || !PetHealthy())
        return ""
    ; 勿扰 / 卡在显示 / 已有气泡时先不查证据（省一次本机 HTTP）；仲裁那一拍也会挡。
    if (IsSet(G_PetDnd) && G_PetDnd)
        return ""
    if (IsSet(G_GlassShown) && G_GlassShown)
        return ""
    if (G_PetGuide >= 4 || G_BubShown)
        return ""
    if (A_TickCount - G_TourAt < PET_TOUR_STEP_MS)
        return ""
    steps := PetTourSteps()
    progress := Map()
    loop Min(G_PetGuide, steps.Length)
        progress[steps[A_Index]] := true
    next := PetTourNext(progress, PetTourEvidence())
    if (next = "") {
        ; 四步都做过了：把进度收口（这是一次状态对账，不是发言）。
        G_PetGuide := steps.Length
        try PetStateSave()
        return ""
    }
    idx := 0
    for i, s in steps {
        if (s = next) {
            idx := i
            break
        }
    }
    if (idx = 0)
        return ""
    return { kind: "tour", text: PetTourText(next, cfg), action: "", source: "引导"
           , onSaid: PetTourSaid.Bind(idx) }
}

; 引导真的说出来之后的记账（onSaid）：步间计时重启 + 进度推进 + 落盘。
PetTourSaid(idx) {
    global G_PetGuide, G_TourAt
    G_TourAt := A_TickCount
    G_PetGuide := Max(G_PetGuide, idx)
    try PetStateSave()
}

; 引导判据的证据：拖框看区域持久化（本地 config.ini），挑词看 backlog 的 picked_at。
; 只打本机取词台 HTTP；拿不到就当「没做过」（会多讲一步，不会崩）。
PetTourEvidence() {
    global G_WebPort
    hasRegion := false
    try {
        txt := FileRead(CONFIG_PATH, "UTF-8")
        hasRegion := PetTourHasRegion(txt)
    }
    hasPicked := false
    try {
        whr := ComObject("WinHttp.WinHttpRequest.5.1")
        whr.Open("GET", "http://127.0.0.1:" . G_WebPort . "/api/sessions", false)
        whr.SetTimeouts(300, 300, 500, 900)
        whr.Send()
        if (whr.Status = 200 && RegExMatch(whr.ResponseText, '"id"\s*:\s*(\d+)', &m)) {
            sid := m[1]
            whr2 := ComObject("WinHttp.WinHttpRequest.5.1")
            whr2.Open("GET", "http://127.0.0.1:" . G_WebPort . "/api/sessions/" . sid . "/backlog", false)
            whr2.SetTimeouts(300, 300, 500, 900)
            whr2.Send()
            if (whr2.Status = 200)
                hasPicked := PetHasPickedFromBacklog(whr2.ResponseText)
        }
    }
    return { hasRegion: hasRegion, hasPicked: hasPicked }
}
