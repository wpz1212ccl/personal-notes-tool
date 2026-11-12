; ============================================================================
;  lib/CaptureHud.ahk —— v34 验收追加（#220）：看视频时常驻的收句状态浮窗
;
;  问题：T5 的状态只在取词台顶栏 + 托盘 tooltip；用户小窗看视频时看不到任何
;  常驻状态（首轮验收实测心跳中断 3m51s，用户毫无感知）。
;
;  形态：
;    · 屏幕右上角常驻，置顶、NoActivate、鼠标穿透（+E0x20），不挡视频操作；
;    · 主行「● 收句中 · <相位>」，颜色区分正常绿 / 需动手琥珀 / 故障红 / 闲灰；
;    · 副行「会话 N · 本段 M 句 · 最近 hh:mm:ss · 心跳 Ns 前」；
;    · armed 关掉 / 中枢掉线立即隐藏（CaptureStatusReset / OnCaptureArmLine）。
;
;  数据源：ping 回包的扁平 cap_*（hub.capture_ping_fields），每秒更新。
;  不引入任何新配置；键位取 G_Cfg 真值。
; ============================================================================

global G_HudGui := 0
global G_HudMain := 0
global G_HudSub := 0
global G_HudShown := false
global G_HudSig := ""
global G_HudArmed := false
global G_HudW := 0

; ping 每拍调用；armed=false 时收起。值没变（相位/句数/心跳秒/会话/最近）不重排。
CaptureHudApply(armed, phase, added, ageMs, session, last) {
    global G_HudGui, G_HudMain, G_HudSub, G_HudShown, G_HudSig, G_HudArmed, G_HudW
    G_HudArmed := armed ? true : false
    if (!G_HudArmed) {
        CaptureHudHide()
        return
    }
    if (!G_HudGui)
        CaptureHudBuild()
    visual := CaptureHudVisual(phase)
    sub := CaptureHudSubText(added, ageMs, session, last)
    sig := visual.text . "|" . visual.color . "|" . sub
    if (G_HudShown && sig = G_HudSig)
        return
    G_HudSig := sig
    mainText := "● " . visual.text
    G_HudMain.SetFont("s" . Round(10 * D) . " bold " . visual.color, "Segoe UI")
    G_HudMain.Text := mainText
    G_HudSub.Text := sub
    ; 流式宽度：按当前内容实测，只增不减（心跳秒数不会让窗口来回抖）。
    w := Max(CaptureHudTextWidth(G_HudMain, mainText),
             CaptureHudTextWidth(G_HudSub, CaptureHudWidthProbe(sub)),
             P(200))
    grew := w > G_HudW
    if (grew) {
        G_HudW := w
        G_HudMain.Move(,, G_HudW)
        G_HudSub.Move(,, G_HudW)
    }
    if (!G_HudShown) {
        G_HudGui.Show("AutoSize Hide")
        G_HudGui.GetPos(, , &w, &h)
        vr := VirtualScreenRect()
        G_HudGui.Move(vr.x + vr.w - w - P(24), vr.y + P(24))
        WinSetTransparent(238, G_HudGui)
        G_HudGui.Show("NoActivate")
        G_HudShown := true
    } else if (grew) {
        G_HudGui.Show("AutoSize NoActivate")
        G_HudGui.GetPos(, , &w, &h)
        vr := VirtualScreenRect()
        G_HudGui.Move(vr.x + vr.w - w - P(24), vr.y + P(24))
    }
}

; 量宽度时把心跳秒数归一成两位，避免 "9s"→"10s" 让浮窗每秒长一点。
CaptureHudWidthProbe(sub) {
    return RegExReplace(sub, "心跳 \d+s 前", "心跳 99s 前")
}

CaptureHudBuild() {
    global G_HudGui, G_HudMain, G_HudSub, G_HudW
    g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
    g.BackColor := "1F1F1F"
    g.MarginX := P(14), g.MarginY := P(9)
    ; 字号必须乘 D：-DPIScale 下 s10 是 10pt 物理字号，150% 屏上只有约 13px 高，
    ; 看视频时看不清（2026-11-08 真机反馈）。
    g.SetFont("s" . Round(10 * D) . " bold cFFFFFF", "Segoe UI")
    G_HudMain := g.AddText(, "● 收句中 · 心跳中断（页面不在前台）")
    g.SetFont("s" . Round(9 * D) . " cB0BEC5", "Segoe UI")
    G_HudSub := g.AddText(, "会话 9999 · 本段 9999 句 · 最近 23:59:59 · 心跳 9s 前")
    ; 先给最小宽度建窗（高度由占位文案撑开）；实际宽度在每次 Apply 按内容增长。
    G_HudW := P(200)
    G_HudMain.Move(,, G_HudW)
    G_HudSub.Move(,, G_HudW)
    g.Show("AutoSize Hide")
    WinSetTransparent(238, g)
    RoundCorners(g.Hwnd)
    G_HudGui := g
}

; 用控件自己的字体量文字像素宽（GDI）；拿不到 DC 时按 8px/字兜底。
CaptureHudTextWidth(hctl, text) {
    hwnd := hctl.Hwnd
    hfont := DllCall("SendMessageW", "Ptr", hwnd, "UInt", 0x0031, "Ptr", 0, "Ptr", 0, "Ptr")
    hdc := DllCall("GetDC", "Ptr", hwnd, "Ptr")
    if (!hdc)
        return StrLen(text) * P(8)
    old := 0
    if (hfont)
        old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hfont, "Ptr")
    size := Buffer(8, 0)
    DllCall("GetTextExtentPoint32W", "Ptr", hdc, "Str", text, "Int", StrLen(text), "Ptr", size)
    w := NumGet(size, 0, "Int")
    if (old)
        DllCall("SelectObject", "Ptr", hdc, "Ptr", old)
    DllCall("ReleaseDC", "Ptr", hwnd, "Ptr", hdc)
    return w
}

CaptureHudHide() {
    global G_HudGui, G_HudShown, G_HudSig, G_HudArmed, G_HudW
    G_HudArmed := false
    G_HudSig := ""
    G_HudW := 0
    if (G_HudGui && G_HudShown) {
        try G_HudGui.Hide()
        G_HudShown := false
    }
}

; 11 档相位 → 主行文案与颜色（正常绿 / 需动手琥珀 / 故障红 / 闲灰）。
CaptureHudVisual(phase) {
    global G_Cfg
    if (phase = "no_heartbeat")
        return { color: "cFFB300", text: "收句中 · 心跳中断（页面不在前台）" }
    if (phase = "cc_reading")
        return { color: "cB0BEC5", text: "收句中 · 查轨中…" }
    if (phase = "cc_ok")
        return { color: "c00E676", text: "收句中 · CC 直读" }
    if (phase = "cc_missing")
        return { color: "cB0BEC5", text: "收句中 · 无英文 CC · 转屏幕采样" }
    if (phase = "cc_error")
        return { color: "cFFB300", text: "收句中 · CC 故障 · 转屏幕采样" }
    if (phase = "sampling")
        return { color: "c00E676", text: "收句中 · 采集中" }
    if (phase = "paused")
        return { color: "cB0BEC5", text: "收句中 · 已暂停（回来自动续）" }
    if (phase = "dormant")
        return { color: "cFFB300", text: "收句中 · 需要拖一次字幕带（" . G_Cfg.hotkeyRedrag . "）" }
    if (phase = "redrag_needed")
        return { color: "cFFB300", text: "收句中 · 区域失效，请重拖（" . G_Cfg.hotkeyRedrag . "）" }
    if (phase = "failed")
        return { color: "cEF5350", text: "收句中 · OCR 引擎不可用" }
    return { color: "cB0BEC5", text: "收句中 · 等待播放" }
}

CaptureHudSubText(added, ageMs, session, last) {
    parts := []
    if (session > 0)
        parts.Push("会话 " . session)
    parts.Push("本段 " . Max(0, added) . " 句")
    if (last != "")
        parts.Push("最近 " . SubStr(last, 12))
    if (ageMs >= 0 && ageMs <= CAPTURE_HEARTBEAT_STALE_MS)
        parts.Push("心跳 " . Round(ageMs / 1000) . "s 前")
    text := ""
    for i, part in parts
        text .= (i > 1 ? " · " : "") . part
    return text
}
