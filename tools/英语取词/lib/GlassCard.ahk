; ============================================================================
;  lib/GlassCard.ahk —— v35 #231：玻璃卡（只在「要用户动手」时出现的毛玻璃小卡）
;
;  spec：docs/specs/v35-desktop-pet-and-status-overlay.md §两个窗口、一个状态源 /
;        §毛玻璃（双轨）/ §尺寸、位置与收起 / §降级；票 #231。
;
;  形态：
;    · **普通窗口**（不是分层窗口：`WS_EX_LAYERED` 吃不到系统背景材质），置顶、
;      无标题、不进任务栏、不抢焦点（`WS_EX_NOACTIVATE`）；
;    · 只在 StatusVisual().expanded 为真的四档展开（心跳中断 / 需要拖框 /
;      区域失效 / OCR 不可用）。**本文件不做任何相位判断** —— 相位 → 视觉的
;      唯一真值在 lib/Logic.ahk 的 StatusVisual()，这里只消费外壳喂进来的
;      {expanded, text, action, tone}；源码钉子钉着「新模块不得复刻相位表」；
;    · ≤2 行字 + 1 个动作按钮；异常态 ≤360×96lp；正常态整窗隐藏（0×0 占屏）；
;    · 卡锚在角色头顶居中、可拖（WM_NCHITTEST → HTCAPTION）、会话内记住位置；
;      GlassCardFollow(x, y) 是 #232 角色窗的接线口（角色动，卡跟着动）；
;    · 材质双轨：真材质（DWM system backdrop）不成 → 假玻璃（GDI 渐变 + 噪点 +
;      内高光 + 1px 亮边）。**绝不用整窗 alpha**（WinSetTransparent 会与系统材质
;      互斥，且把文字一起弄糊）；文字的半透明由颜色本身承担。
;
;  数据面：只吃 ping 里的相位（经 StatusVisual），不新增 schema / 不改扁平约定。
; ============================================================================

; --- 运行时状态 --------------------------------------------------------------
global G_GlassGui := 0
global G_GlassPic := 0
global G_GlassText := 0
global G_GlassBtn := 0
global G_GlassShown := false
global G_GlassSig := ""
global G_GlassKey := ""              ; 当前卡对应的 textKey（按「知道了」后本相位不再弹）
global G_GlassAction := ""
global G_GlassDismissed := ""
global G_GlassAnchorX := 0
global G_GlassAnchorY := 0
global G_GlassFollowed := false      ; #232 角色窗接线后为 true
global G_GlassUserX := ""
global G_GlassUserY := ""            ; 用户拖过的位置（会话内记住）
global G_GlassWinX := 0              ; 卡当前物理位置（v35-ux D4 拖动跟手要用）
global G_GlassWinY := 0
global G_GlassOldProc := 0
global G_GlassProc := 0              ; 子类化回调（只建一次；窗口重建只换 WndProc）
global G_GlassBitmap := 0            ; 假玻璃 DIB（重建时删旧的，防 GDI 句柄泄漏）
global G_GlassMaterial := ""         ; "real" | "fake"
global G_GlassEnabled := true        ; 总开关：pet 不可用（素材缺失/用户关闭）时整卡让位 HUD

; #234 降级路径③：素材目录缺失 / 用户关闭桌宠 → 玻璃卡不出现，状态退回朴素浮窗（HUD）。
; 外壳在 pet 状态变化时调这里；未接线时默认开（#231 单测/演示不受影响）。
GlassCardSetEnabled(enabled) {
    global G_GlassEnabled
    G_GlassEnabled := enabled ? true : false
    if (!G_GlassEnabled)
        GlassCardReset()
}

; 尺寸上限（spec：异常态 ≤360×96lp；正常态不可见）。
; 文案列宽 = 360 - 两侧各 14lp 留白 - 按钮/圆角余量 → 取 300。
global GLASS_CARD_MAX_W_LP := 360
global GLASS_CARD_MAX_H_LP := 96
global GLASS_CARD_TEXT_W_LP := 300
global GLASS_CARD_GAP_LP := 8        ; 卡底边与角色头顶之间的空隙

; 每拍 ping 调用；visual = StatusVisual() 的输出（无相位判断，只看 expanded）。
GlassCardApply(visual, glassMode := "auto") {
    global G_GlassShown, G_GlassSig, G_GlassDismissed, G_GlassEnabled
    if (!G_GlassEnabled)
        return
    if (!IsObject(visual) || !visual.expanded) {
        G_GlassDismissed := ""
        GlassCardHide()
        return
    }
    ; 用户按过「知道了」的同一条（同一相位 + 同一文案 key）不再弹，相位一变自然复位
    if (G_GlassDismissed != "" && G_GlassDismissed = visual.textKey)
        return
    sig := visual.textKey . "|" . visual.text . "|" . visual.action . "|" . visual.tone . "|" . glassMode
    if (G_GlassShown && sig = G_GlassSig)
        return
    GlassCardBuild(visual, glassMode)
    G_GlassSig := sig
    G_GlassShown := true
}

; #232 角色窗接线口：把卡绑到角色头顶中心（物理像素）。角色动，卡跟着动。
; v35-ux D4：拖动中跳过重排 —— 位置由 PetOnMouseMove 的 GlassCardMoveTo 同帧带走。
GlassCardFollow(x, y) {
    global G_GlassAnchorX, G_GlassAnchorY, G_GlassFollowed, G_GlassShown, G_PetDragging
    G_GlassAnchorX := x
    G_GlassAnchorY := y
    G_GlassFollowed := true
    if (!G_GlassShown)
        return
    if (PetDragActive(IsSet(G_PetDragging) && G_PetDragging))
        return                          ; 拖动中：只 move 不重算布局（PetOnMouseMove 已带）
    GlassCardPlaceAt()
}

; v35-ux D4：move-only 跟随（拖动期用）。**不重建位图、不重算布局**。
GlassCardMoveTo(x, y) {
    global G_GlassGui, G_GlassShown, G_GlassWinX, G_GlassWinY
    if (!G_GlassGui || !G_GlassShown)
        return
    G_GlassWinX := x
    G_GlassWinY := y
    DllCall("SetWindowPos", "Ptr", G_GlassGui.Hwnd, "Ptr", 0, "Int", x, "Int", y
        , "Int", 0, "Int", 0, "UInt", 0x0015)   ; NOSIZE|NOZORDER|NOACTIVATE
}

; 建窗（内容或材质变了就整块重建 —— 一秒最多一次，够便宜）。
GlassCardBuild(visual, glassMode) {
    global G_GlassGui, G_GlassPic, G_GlassText, G_GlassBtn, G_GlassAction
    global G_GlassKey, G_GlassUserX, G_GlassUserY, G_GlassMaterial
    global G_GlassOldProc, G_GlassBitmap
    if (G_GlassGui) {
        ; 窗口销毁后旧 WndProc 指针失效 —— 必须复位，否则新窗口被子类化哨兵挡住，
        ; 拖拽（WM_NCHITTEST）与真材质轨的背景屏蔽从第二张卡起失效。
        G_GlassOldProc := 0
        try G_GlassGui.Destroy()
        G_GlassGui := 0, G_GlassPic := 0, G_GlassText := 0, G_GlassBtn := 0
    }
    if (G_GlassBitmap) {
        DllCall("gdi32\DeleteObject", "Ptr", G_GlassBitmap)
        G_GlassBitmap := 0
    }
    ; 材质轨先定（决定背景是否交给 DWM 画）
    realOk := GlassCardMaterialRealOk()
    mode := GlassCardMaterialMode(glassMode, realOk)
    G_GlassMaterial := mode
    g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000")
    ; 真材质轨：客户区不画背景（子类里屏蔽 WM_ERASEBKGND / WM_PAINT 的背景），
    ; 让 DWM 的 system backdrop 透出来；假玻璃轨：先给深色底，随后贴 DIB。
    g.BackColor := mode = "real" ? "000000" : "101319"
    g.MarginX := P(8)
    g.MarginY := P(8)
    ; 1×1 占位图先建（z 序垫底），拿到真实尺寸后再换成整块假玻璃位图。
    G_GlassPic := g.AddPicture("x0 y0 w1 h1", "HBITMAP:*" . GlassCardTinyBitmap())
    g.SetFont("s" . Round(10 * D) . " cF2F4F8", "Segoe UI")
    G_GlassText := g.AddText("x" . P(8) . " y" . P(8) . " w" . P(GLASS_CARD_TEXT_W_LP)
                            , visual.text)
    ; STATIC 控件不会自己按换行长高（AutoSize 只看创建时的控件尺寸），
    ; 先用 DrawText(DT_CALCRECT|DT_WORDBREAK) 量出真实行高再定高。
    G_GlassText.Move(, , , GlassCardTextHeight(G_GlassText, visual.text, P(GLASS_CARD_TEXT_W_LP)))
    label := GlassCardButtonLabel(visual.action)
    if (label != "") {
        g.SetFont("s" . Round(9 * D) . " cFFFFFF", "Segoe UI")
        G_GlassBtn := g.AddButton("x" . (P(8) + P(GLASS_CARD_TEXT_W_LP) - P(90))
                                . " y+" . P(4) . " w" . P(90) . " h" . P(26), label)
        G_GlassBtn.OnEvent("Click", GlassCardButtonClick)
    }
    G_GlassGui := g
    G_GlassKey := visual.textKey
    G_GlassAction := visual.action
    ; 真材质 + 圆角（Win10 上属性不存在，调用失败无害）；真实材质失败当场退假玻璃
    DllCall("dwmapi\DwmSetWindowAttribute", "Ptr", g.Hwnd, "Int", 20, "Int*", 1, "Int", 4)
    DllCall("dwmapi\DwmSetWindowAttribute", "Ptr", g.Hwnd, "Int", 33, "Int*", 2, "Int", 4)
    if (mode = "real" && !GlassCardEnableRealMaterial(g.Hwnd)) {
        G_GlassMaterial := "fake"
        g.BackColor := "101319"
    }
    ; 先量尺寸（AutoSize 只认控件位置，不显示出来不闪）
    g.Show("AutoSize Hide")
    g.GetPos(, , &w, &h)
    ; 上限兜底：异常态 ≤360×96lp（文案再长也不许撑爆屏幕）
    if (w > P(GLASS_CARD_MAX_W_LP) || h > P(GLASS_CARD_MAX_H_LP)) {
        w := Min(w, P(GLASS_CARD_MAX_W_LP))
        h := Min(h, P(GLASS_CARD_MAX_H_LP))
        g.Move(, , w, h)
    }
    GlassCardSubclass(g.Hwnd)
    if (G_GlassMaterial = "fake")
        GlassCardPaintFake(w, h)
    GlassCardPlaceAt(w, h)
    g.Show("NoActivate")
}

; 用户拖过就沿用（夹进虚拟屏幕）；否则锚在角色头顶。
GlassCardPlaceAt(w := 0, h := 0) {
    global G_GlassGui, G_GlassUserX, G_GlassUserY, G_GlassWinX, G_GlassWinY
    if (!G_GlassGui)
        return
    G_GlassGui.GetPos(, , &gw, &gh)
    if (w = 0)
        w := gw
    if (h = 0)
        h := gh
    vr := VirtualScreenRect()
    if (G_GlassUserX != "") {
        x := Max(vr.x + 4, Min(G_GlassUserX, vr.x + vr.w - w - 4))
        y := Max(vr.y + 4, Min(G_GlassUserY, vr.y + vr.h - h - 4))
        G_GlassGui.Move(x, y)
        G_GlassWinX := x, G_GlassWinY := y
        return
    }
    a := GlassCardAnchor()
    pos := GlassCardPlace(w, h, a.x, a.y, vr, P(GLASS_CARD_GAP_LP))
    G_GlassGui.Move(pos.x, pos.y)
    G_GlassWinX := pos.x, G_GlassWinY := pos.y
}

; 角色头顶中心的默认落点（#232 接线前：右下角 260×340lp 角色的头顶）。
GlassCardAnchor() {
    global G_GlassAnchorX, G_GlassAnchorY, G_GlassFollowed
    if (G_GlassFollowed)
        return { x: G_GlassAnchorX, y: G_GlassAnchorY }
    vr := VirtualScreenRect()
    return { x: vr.x + vr.w - P(24) - P(130)
           , y: vr.y + vr.h - P(24) - P(340) }
}

GlassCardButtonClick(*) {
    global G_GlassAction, G_GlassDismissed, G_GlassKey, G_GlassSig
    action := G_GlassAction
    key := G_GlassKey
    GlassCardHide()
    G_GlassSig := ""
    G_GlassDismissed := key
    ; 与热键**同一条路径**（不复制语义）：拖框仍走 BandRedragStart()
    if (action = "redrag")
        SetTimer(() => BandRedragStart(), -1)
}

GlassCardHide() {
    global G_GlassGui, G_GlassShown, G_GlassSig
    G_GlassShown := false
    G_GlassSig := ""
    if (G_GlassGui)
        try G_GlassGui.Hide()
}

; 中枢掉线（CaptureStatusReset）：收起卡，清掉「打发过」的记忆。
GlassCardReset() {
    global G_GlassDismissed
    G_GlassDismissed := ""
    GlassCardHide()
}

; ---------------------------------------------------------------------------
;  材质双轨
; ---------------------------------------------------------------------------

; Win11 22H2+（build 22621+）才有 DWMWA_SYSTEMBACKDROP_TYPE；更老的系统直接假玻璃。
GlassCardMaterialRealOk() {
    return VerCompare(A_OSVersion, "10.0.22621") >= 0
}

; 真材质：system backdrop = acrylic(3) + 客户区透明（DwmExtendFrameIntoClientArea -1）。
; 返回 true = 两个调用都成功；任何一步失败 → 调用方退假玻璃（可回滚的降级路径）。
GlassCardEnableRealMaterial(hwnd) {
    ok := DllCall("dwmapi\DwmSetWindowAttribute", "Ptr", hwnd, "Int", 38
                , "Int*", 3, "Int", 4)
    if (ok != 0)
        return false
    m := Buffer(16, 0)
    NumPut("Int", -1, m, 0), NumPut("Int", -1, m, 4)
    NumPut("Int", -1, m, 8), NumPut("Int", -1, m, 12)
    return DllCall("dwmapi\DwmExtendFrameIntoClientArea", "Ptr", hwnd, "Ptr", m) = 0
}

; 假玻璃：深色竖向渐变 + 稀疏噪点 + 顶部 1px 内高光 + 1px 亮边（spec 的回退形态）。
; 全部画在 32bpp DIB 上再贴成 Picture，**不碰整窗 alpha**。
GlassCardPaintFake(w, h) {
    global G_GlassPic, G_GlassBitmap
    hbm := GlassCardGradientBitmap(w, h)
    if (!hbm || !G_GlassPic)
        return
    G_GlassBitmap := hbm
    G_GlassPic.Value := "HBITMAP:*" . hbm
    G_GlassPic.Move(0, 0, w, h)
}

; 1×1 黑位图（占位图控件用；真位图拿到尺寸后再换）。
GlassCardTinyBitmap() {
    static hbm := 0
    if (hbm)
        return hbm
    bmi := Buffer(40, 0)
    NumPut("UInt", 40, bmi, 0)
    NumPut("Int", 1, bmi, 4), NumPut("Int", -1, bmi, 8)
    NumPut("UShort", 1, bmi, 12), NumPut("UShort", 32, bmi, 14)
    NumPut("UInt", 0, bmi, 16)
    pBits := 0
    hbm := DllCall("gdi32\CreateDIBSection", "Ptr", 0, "Ptr", bmi, "UInt", 0
                 , "Ptr*", &pBits, "Ptr", 0, "UInt", 0, "Ptr")
    return hbm
}

; 按控件自己的字体量「这段文字在 maxW 内换行后有多高」（STATIC 不会自己长高）。
GlassCardTextHeight(hctl, text, maxW) {
    hwnd := hctl.Hwnd
    hfont := DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0031, "Ptr", 0, "Ptr", 0, "Ptr")
    hdc := DllCall("GetDC", "Ptr", hwnd, "Ptr")
    if (!hdc)
        return P(22)
    old := 0
    if (hfont)
        old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hfont, "Ptr")
    ; DT_CALCRECT(0x400) | DT_WORDBREAK(0x10) | DT_NOPREFIX(0x800)
    rect := Buffer(16, 0)
    NumPut("Int", 0, rect, 0), NumPut("Int", 0, rect, 4)
    NumPut("Int", maxW, rect, 8), NumPut("Int", 0, rect, 12)
    DllCall("user32\DrawTextW", "Ptr", hdc, "Str", text, "Int", -1, "Ptr", rect
          , "UInt", 0x400 | 0x10 | 0x800)
    h := NumGet(rect, 12, "Int") - NumGet(rect, 4, "Int")
    if (old)
        DllCall("SelectObject", "Ptr", hdc, "Ptr", old)
    DllCall("ReleaseDC", "Ptr", hwnd, "Ptr", hdc)
    return Max(P(20), h)
}

; 假玻璃位图：GradientFill 竖向渐变 → 噪点 → 内高光/亮边。
GlassCardGradientBitmap(w, h) {
    bmi := Buffer(40, 0)
    NumPut("UInt", 40, bmi, 0)
    NumPut("Int", w, bmi, 4), NumPut("Int", -h, bmi, 8)   ; 负高 = top-down
    NumPut("UShort", 1, bmi, 12), NumPut("UShort", 32, bmi, 14)
    NumPut("UInt", 0, bmi, 16)                            ; BI_RGB
    pBits := 0
    hbm := DllCall("gdi32\CreateDIBSection", "Ptr", 0, "Ptr", bmi, "UInt", 0
                 , "Ptr*", &pBits, "Ptr", 0, "UInt", 0, "Ptr")
    if (!hbm)
        return 0
    hdc := DllCall("gdi32\CreateCompatibleDC", "Ptr", 0, "Ptr")
    old := DllCall("gdi32\SelectObject", "Ptr", hdc, "Ptr", hbm, "Ptr")
    ; 竖向渐变：上 2C3240 → 下 171B22（深色玻璃底）
    vert := Buffer(32, 0)
    NumPut("Int", 0, vert, 0), NumPut("Int", 0, vert, 4)
    NumPut("UShort", 0x2C00, vert, 8), NumPut("UShort", 0x3200, vert, 10)
    NumPut("UShort", 0x4000, vert, 12), NumPut("UShort", 0xFF00, vert, 14)
    NumPut("Int", w, vert, 16), NumPut("Int", h, vert, 20)
    NumPut("UShort", 0x1700, vert, 24), NumPut("UShort", 0x1B00, vert, 26)
    NumPut("UShort", 0x2200, vert, 28), NumPut("UShort", 0xFF00, vert, 30)
    rect := Buffer(8, 0)
    NumPut("UInt", 0, rect, 0), NumPut("UInt", 1, rect, 4)
    DllCall("msimg32\GradientFill", "Ptr", hdc, "Ptr", vert, "UInt", 2
          , "Ptr", rect, "UInt", 1, "UInt", 1)            ; GRADIENT_FILL_RECT_V = 1
    ; 稀疏噪点（视觉上的「磨砂颗粒」；数量按面积给，封顶 320 点）
    n := Min(320, Max(48, (w * h) // 900))
    Loop n {
        px := Random(0, Max(0, w - 1)), py := Random(0, Max(0, h - 1))
        c := (Mod(A_Index, 2) = 0) ? 0x3A4250 : 0x232833
        DllCall("gdi32\SetPixelV", "Ptr", hdc, "Int", px, "Int", py, "UInt", c)
    }
    ; 顶部 1px 内高光（贴内边的一道亮线）
    brush := DllCall("gdi32\CreateSolidBrush", "UInt", 0x3F4A5C, "Ptr")
    hl := Buffer(16, 0)
    NumPut("Int", 1, hl, 0), NumPut("Int", 1, hl, 4)
    NumPut("Int", w - 1, hl, 8), NumPut("Int", 2, hl, 12)
    DllCall("user32\FillRect", "Ptr", hdc, "Ptr", hl, "Ptr", brush)
    DllCall("gdi32\DeleteObject", "Ptr", brush)
    ; 1px 亮边
    rect2 := Buffer(16, 0)
    NumPut("Int", 0, rect2, 0), NumPut("Int", 0, rect2, 4)
    NumPut("Int", w, rect2, 8), NumPut("Int", h, rect2, 12)
    edge := DllCall("gdi32\CreateSolidBrush", "UInt", 0x5B6678, "Ptr")
    DllCall("user32\FrameRect", "Ptr", hdc, "Ptr", rect2, "Ptr", edge)
    DllCall("gdi32\DeleteObject", "Ptr", edge)
    ; 清 GDI 对象（位图要交给 Picture 控件，不删）
    DllCall("gdi32\SelectObject", "Ptr", hdc, "Ptr", old)
    DllCall("gdi32\DeleteDC", "Ptr", hdc)
    return hbm
}

; ---------------------------------------------------------------------------
;  窗口子类：整块卡可拖 + 真材质轨屏蔽背景绘制
; ---------------------------------------------------------------------------

GlassCardSubclass(hwnd) {
    global G_GlassOldProc, G_GlassProc
    ; 回调只建一次（窗口重建不重复 CallbackCreate，避免回调泄漏）
    if (!G_GlassProc)
        G_GlassProc := CallbackCreate(GlassCardWndProc)
    ; 每张新窗口都要重新子类化（旧窗口的 WndProc 指针随 Destroy 失效）
    G_GlassOldProc := DllCall("SetWindowLongPtrW", "Ptr", hwnd, "Int", -4
                            , "Ptr", G_GlassProc, "Ptr")
}

GlassCardWndProc(hwnd, msg, wp, lp) {
    global G_GlassOldProc, G_GlassMaterial
    ; 真材质轨：客户区不许画背景（画了就把 DWM 材质盖住）
    if (G_GlassMaterial = "real") {
        if (msg = 0x0014) {                  ; WM_ERASEBKGND
            return 1
        }
        if (msg = 0x000F) {                  ; WM_PAINT
            DllCall("user32\ValidateRect", "Ptr", hwnd, "Ptr", 0)
            return 0
        }
    }
    ; 整块卡都能拖（按钮是子窗口，命中它自己的区域时走不到这里）
    if (msg = 0x0084)                        ; WM_NCHITTEST
        return 2                             ; HTCAPTION
    if (msg = 0x0232)                        ; WM_EXITSIZEMOVE：拖完记住位置
        GlassCardDragged()
    return DllCall("CallWindowProcW", "Ptr", G_GlassOldProc, "Ptr", hwnd
                 , "UInt", msg, "Ptr", wp, "Ptr", lp, "Ptr")
}

; 拖完：把卡片位置记成新锚点（角色窗接线后由外壳的 ping 同步挪她）。
GlassCardDragged() {
    global G_GlassGui, G_GlassAnchorX, G_GlassAnchorY, G_GlassUserX, G_GlassUserY
    global G_GlassWinX, G_GlassWinY
    if (!G_GlassGui)
        return
    G_GlassGui.GetPos(&x, &y, &w, &h)
    G_GlassUserX := x
    G_GlassUserY := y
    G_GlassWinX := x, G_GlassWinY := y
    G_GlassAnchorX := x + w // 2
    G_GlassAnchorY := y + h + P(GLASS_CARD_GAP_LP)
}
