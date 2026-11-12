; ============================================================================
;  lib/PetWindow.ahk —— v35 #232：角色窗（她活起来 + 点击 4 项菜单）
;
;  形态（spec §两个窗口、一个状态源 / §尺寸位置与收起 / §点击与菜单）：
;    · 分层窗口（WS_EX_LAYERED + WS_EX_NOACTIVATE + ToolWindow），260×340 物理像素，
;      默认右下角；置顶、无标题、不进任务栏、不抢焦点；
;    · 逐像素 alpha：预乘 BGRA 帧 1:1 贴（`UpdateLayeredWindow`）——透明像素天然
;      穿透，角色本体仍可点（`WM_NCHITTEST` 按 alpha 返回 HTTRANSPARENT）；
;    · 静止时播「状态脸」expression clip（8fps，spec 的空闲降帧），
;      `StatusVisual().motion` 非空时插播一次性动作（24fps），播完回状态脸；
;    · 帧按需加载 + LRU（上限 24 帧，来自 data/pet/manifest.json）；
;    · 单击 → 4 项菜单（收句开关 / 挑词 / 重拖字幕带 / 暂停），每项都调**既有实现**
;      （与热键同一条路径，不复制逻辑）；右键 → 勿扰 / 重放引导 / 关闭桌宠；
;    · 拖动跟手、松手落位置到 `data/pet-state.json`（D-L2：不写 config.ini）；
;    · 素材缺失 / 用户关闭 → 桌宠整体不启动，老 HUD 原样工作（D-L3）。
;
;  **相位判断只在 `StatusVisual()`（lib/Logic.ahk）**：本文件只消费它输出的
;  `face` / `motion`，不出现任何相位字面量（tests/test_shell_source.py 钉着）。
;
;  素材来源：https://github.com/Andersen216/dsh-whale-girl-live2d
;    代码 MIT / 模型素材 CC BY-NC-SA 4.0；作者 上善无形 / ZipZipPipe / 氵六青。
;    帧由 tools/pet_bake/ 离线烘焙，产物落 data/pet/（不进 git）。
; ============================================================================

; 仅调试/演示用的数据目录覆盖（默认空 = 正常 data/pet；产品路径不变，见 #234 演示脚本）
global PET_DEBUG_DATA_DIR := EnvGet("PET_DEBUG_DATA_DIR")
global G_PetManifest := 0          ; Manifest object（见 lib/PetFrames.ahk）
global G_PetGui := 0
global G_PetHwnd := 0
global G_PetFrameCache := Map()    ; 帧路径 → Buffer（LRU ≤24 帧）
global G_PetCacheOrder := []
global G_PetCacheCap := PET_FRAME_CACHE_CAP
global G_PetW := 0
global G_PetH := 0
global G_PetStride := 0
global G_PetFrameBytes := 0
global G_PetBits := 0
global G_PetDib := 0
global G_PetMemDC := 0
global G_PetScreenDC := 0
global G_PetBlend := 0
global G_PetPtDst := 0
global G_PetSize := 0
global G_PetPtSrc := 0
global G_PetX := 0
global G_PetY := 0
global G_PetFace := "focus"        ; 当前状态脸 key（expression clip）
global G_PetStaticClip := 0        ; 素材缺失时的静态兜底 clip（#237 降级）
global G_PetFaceStart := 0
global G_PetMotion := ""           ; 正在播的一次性动作 key（"" = 没有）
global G_PetMotionStart := 0
global G_PetMotionSeen := ""       ; 上一次 ping 看到的动作键（边沿触发，避免 1Hz 重播）
global G_PetPhase := ""            ; 最近一拍的相位（PetApplyVisual 记账；ambient 判定用）
global G_PetAmbientNextAt := 0     ; 下次允许随机 idle 插播的时刻（PetAmbientNextAt 算步进）
global G_PetIdleFace := ""         ; 闲时轮换脸（"" = 跟相位走；armed 时清空）
global G_PetIdleFaceAt := 0        ; 下次允许闲时换脸的时刻（PetIdleFaceGapMs 算步进）
global G_PetLastIdx := -1
global G_PetLastPath := ""
global G_PetLastData := 0          ; 当前帧的 Buffer（命中测试读 alpha 用）
global G_PetStatePath := ""
global G_PetEnabled := true
global G_PetDnd := false
global G_PetGuide := 0
global G_PetDownX := 0
global G_PetDownY := 0
global G_PetWinX := 0
global G_PetWinY := 0
global G_PetDragging := false
global G_PetMoved := false
global G_PetDragSnap := 0          ; v36：拖动开始一刻的浮层原点快照（决策 10）
global G_PetDragAt := 0            ; v36：拖动开始的时刻（停表 / 续播时间轴校正）
global G_PetHookOn := false
global G_PetMissCount := 0         ; 连续读帧失败计数（素材/帧缺失时降级，不空转）
global G_PetCollapsed := false     ; 浮球态（双击收起 / 单击展开）
global G_PetRestoreX := 0          ; 展开时要回去的位置（收起前记录）
global G_PetRestoreY := 0
global G_PetToneColor := "cB0BEC5" ; 当前语气色（浮球圆点跟 StatusVisual.color 走）
global G_PetBallData := 0          ; 浮球帧像素（bs×bs BGRA 预乘）
global G_PetBallSize := 0          ; 浮球物理边长

; --- 本地状态（D-L2：data/pet-state.json；不写 config.ini）------------------
PetStatePath() {
    return A_ScriptDir . "\data\pet-state.json"
}

PetStateRead() {
    global G_PetStatePath
    ; v35-ux D1：feed_* = 信息流面板的用户状态（位置 / × 关过 / 固定显示过）
    st := { enabled: true, x: -1, y: -1, dnd: false, guide: 0, collapsed: false
          , feedX: -1, feedY: -1, feedHidden: false, feedPinned: false }
    if (G_PetStatePath = "")
        G_PetStatePath := PetStatePath()
    if (!FileExist(G_PetStatePath))
        return st
    try t := FileRead(G_PetStatePath, "UTF-8")
    catch
        return st
    if (RegExMatch(t, '"enabled"\s*:\s*false'))
        st.enabled := false
    if (RegExMatch(t, '"dnd"\s*:\s*true'))
        st.dnd := true
    if (RegExMatch(t, '"collapsed"\s*:\s*true'))
        st.collapsed := true
    if (RegExMatch(t, '"guide"\s*:\s*(-?\d+)', &m))
        st.guide := m[1] + 0
    if (RegExMatch(t, '"x"\s*:\s*(-?\d+)', &m))
        st.x := m[1] + 0
    if (RegExMatch(t, '"y"\s*:\s*(-?\d+)', &m))
        st.y := m[1] + 0
    if (RegExMatch(t, '"feed_x"\s*:\s*(-?\d+)', &m))
        st.feedX := m[1] + 0
    if (RegExMatch(t, '"feed_y"\s*:\s*(-?\d+)', &m))
        st.feedY := m[1] + 0
    if (RegExMatch(t, '"feed_hidden"\s*:\s*true'))
        st.feedHidden := true
    if (RegExMatch(t, '"feed_pinned"\s*:\s*true'))
        st.feedPinned := true
    return st
}

PetStateSave() {
    global G_PetStatePath, G_PetX, G_PetY, G_PetEnabled, G_PetDnd, G_PetGuide, G_PetCollapsed
    global G_FeedUserX, G_FeedUserY, G_FeedUserHidden, G_FeedPinned
    if (G_PetStatePath = "")
        G_PetStatePath := PetStatePath()
    ; v35-ux D1：信息流的位置（用户拖过才有值）与显隐位一起落盘
    feedX := (IsSet(G_FeedUserX) && G_FeedUserX != "") ? G_FeedUserX : -1
    feedY := (IsSet(G_FeedUserY) && G_FeedUserY != "") ? G_FeedUserY : -1
    feedHidden := (IsSet(G_FeedUserHidden) && G_FeedUserHidden) ? true : false
    feedPinned := (IsSet(G_FeedPinned) && G_FeedPinned) ? true : false
    txt := "{`"x`": " . G_PetX . ", `"y`": " . G_PetY
        . ", `"enabled`": " . (G_PetEnabled ? "true" : "false")
        . ", `"dnd`": " . (G_PetDnd ? "true" : "false")
        . ", `"guide`": " . G_PetGuide
        . ", `"collapsed`": " . (G_PetCollapsed ? "true" : "false")
        . ", `"feed_x`": " . feedX
        . ", `"feed_y`": " . feedY
        . ", `"feed_hidden`": " . (feedHidden ? "true" : "false")
        . ", `"feed_pinned`": " . (feedPinned ? "true" : "false") . "}`n"
    tmp := G_PetStatePath . ".tmp"
    try FileDelete(tmp)
    try {
        FileAppend(txt, tmp, "UTF-8-RAW")
        FileMove(tmp, G_PetStatePath, 1)
    } catch as err {
        Log("桌宠：写 pet-state.json 失败 " . err.Message)
    }
}

; 帧数据目录：正常是 <工具目录>\data\pet；演示/降级测试可用 PET_DEBUG_DATA_DIR 覆盖。
PetDataDir() {
    global PET_DEBUG_DATA_DIR
    if (PET_DEBUG_DATA_DIR != "")
        return PET_DEBUG_DATA_DIR
    return A_ScriptDir . "\data\pet"
}

; --- 健康度（HUD gating 的唯一判据，D-L3）-----------------------------------
PetHealthy() {
    global G_PetHwnd
    return G_PetHwnd != 0 && DllCall("IsWindowVisible", "Ptr", G_PetHwnd)
}

; --- 帧读取（按需 + LRU）-----------------------------------------------------
PetFramePath(clip, idx) {
    global G_PetManifest
    sep := "\"
    base := PetDataDir()
    pat := clip.filePattern
    patAHK := StrReplace(pat, "%04d", "{:04d}")
    patAHK := StrReplace(patAHK, "%d", "{:d}")
    return base . sep . clip.dir . sep . Format(patAHK, idx)
}

PetFrameLoad(path) {
    global G_PetFrameCache, G_PetCacheOrder, G_PetCacheCap, G_PetFrameBytes
    if (G_PetFrameCache.Has(path)) {
        G_PetCacheOrder := PetLruTouch(G_PetCacheOrder, path, G_PetCacheCap)
        return G_PetFrameCache[path]
    }
    if (!FileExist(path)) {
        Log("桌宠：帧缺失 " . path)       ; 素材/帧缺失降级证据（#237/#234）
        return 0
    }
    buf := Buffer(G_PetFrameBytes, 0)
    try {
        f := FileOpen(path, "r")
        n := f.RawRead(buf, G_PetFrameBytes)
        f.Close()
    } catch as err {
        Log("桌宠：读帧失败 " . path . "：" . err.Message)
        return 0
    }
    if (n != G_PetFrameBytes) {
        Log("桌宠：帧短读 " . path . "（" . n . "/" . G_PetFrameBytes . "）")
        return 0
    }
    G_PetFrameCache[path] := buf
    G_PetCacheOrder := PetLruTouch(G_PetCacheOrder, path, G_PetCacheCap)
    if (G_PetCacheOrder.Length >= G_PetCacheCap) {
        ; LRU 只淘汰缓存，不改变帧序：下一次用到会重新读盘
        cut := G_PetCacheOrder[1]
        if (G_PetFrameCache.Has(cut) && cut != path)
            G_PetFrameCache.Delete(cut)
    }
    return buf
}


; --- 浮球（双击收起 40×40lp；点一下/双击展开）--------------------------------
; 浮球同样是分层窗（ULW 透明、置顶、不抢焦点）；只画一个圆：深色底盘 + 语气色
; 圆点 + 左上高光。收起态**不播动画、不读帧**（CPU 更低），语气色跟
; StatusVisual().color 走；位置写 pet-state.json（collapsed 字段 + 展开位置）。
PetBallSizePx() {
    return P(40)
}

PetResizeDib(w, h) {
    global G_PetBits, G_PetDib, G_PetMemDC, G_PetScreenDC
    if (G_PetMemDC) {
        try DllCall("gdi32\DeleteDC", "Ptr", G_PetMemDC)
        G_PetMemDC := 0
    }
    if (G_PetDib) {
        try DllCall("gdi32\DeleteObject", "Ptr", G_PetDib)
        G_PetDib := 0
    }
    bi := Buffer(40, 0)
    NumPut("Int", 40, bi, 0), NumPut("Int", w, bi, 4), NumPut("Int", -h, bi, 8)
    NumPut("UShort", 1, bi, 12), NumPut("UShort", 32, bi, 14), NumPut("Int", 0, bi, 16)
    G_PetDib := DllCall("gdi32\CreateDIBSection", "Ptr", 0, "Ptr", bi, "UInt", 0
        , "Ptr*", &G_PetBits, "Ptr", 0, "UInt", 0, "Ptr")
    G_PetMemDC := DllCall("gdi32\CreateCompatibleDC", "Ptr", G_PetScreenDC, "Ptr")
    DllCall("gdi32\SelectObject", "Ptr", G_PetMemDC, "Ptr", G_PetDib)
}

; 语气色 "cRRGGBB" → [r,g,b]（畸形值回落 v34 HUD 的闲灰）
PetToneRGB(color) {
    hex := SubStr(color, 2)
    if (StrLen(hex) < 6)
        hex := "B0BEC5"
    return [Integer("0x" . SubStr(hex, 1, 2))
          , Integer("0x" . SubStr(hex, 3, 2))
          , Integer("0x" . SubStr(hex, 5, 2))]
}

; 生成浮球帧：bs×bs BGRA 预乘；深底 + 语气色圆点 + 左上高光 + 1px 软边
PetRenderBall() {
    global G_PetBallSize, G_PetToneColor, G_PetBallData
    bs := G_PetBallSize
    rgb := PetToneRGB(G_PetToneColor)
    buf := Buffer(bs * bs * 4, 0)
    cx := (bs - 1) / 2.0, cy := (bs - 1) / 2.0
    R := bs / 2.0 - 1.0
    inner := R * 0.44
    br := 30, bgc := 36, bb := 46
    i := 0
    while (i < bs) {
        j := 0
        while (j < bs) {
            dx := j - cx, dy := i - cy
            d := Sqrt(dx * dx + dy * dy)
            a := 0
            if (d <= R)
                a := 255
            else if (d <= R + 1.2)
                a := Round((R + 1.2 - d) / 1.2 * 255)
            if (a > 0) {
                rr := br, gg := bgc, bl := bb
                if (d <= inner + 1.2) {
                    k := d <= inner ? 1.0 : Max(0.0, (inner + 1.2 - d) / 1.2)
                    rr := Round(rgb[1] * k + br * (1 - k))
                    gg := Round(rgb[2] * k + bgc * (1 - k))
                    bl := Round(rgb[3] * k + bb * (1 - k))
                } else if (dx < 0 && dy < 0 && d > R * 0.55 && d < R * 0.9) {
                    rr := Min(255, rr + 22), gg := Min(255, gg + 24), bl := Min(255, bl + 26)
                }
                off := (i * bs + j) * 4
                NumPut("UChar", Min(255, Round(bl * a / 255)), buf, off)
                NumPut("UChar", Min(255, Round(gg * a / 255)), buf, off + 1)
                NumPut("UChar", Min(255, Round(rr * a / 255)), buf, off + 2)
                NumPut("UChar", a, buf, off + 3)
            }
            j += 1
        }
        i += 1
    }
    G_PetBallData := buf
    return buf
}

; 把浮球帧贴到分层窗（窗口同时缩到 40×40lp）
PetApplyBallFrame() {
    global G_PetGui, G_PetHwnd, G_PetBallSize, G_PetBits, G_PetMemDC, G_PetScreenDC
    global G_PetBlend, G_PetPtDst, G_PetSize, G_PetPtSrc, G_PetX, G_PetY
    if (!G_PetHwnd)
        return
    bs := PetBallSizePx()
    G_PetBallSize := bs
    buf := PetRenderBall()
    NumPut("Int", bs, G_PetSize, 0), NumPut("Int", bs, G_PetSize, 4)
    NumPut("Int", G_PetX, G_PetPtDst, 0), NumPut("Int", G_PetY, G_PetPtDst, 4)
    try G_PetGui.Move(G_PetX, G_PetY, bs, bs)
    PetResizeDib(bs, bs)
    DllCall("RtlMoveMemory", "Ptr", G_PetBits, "Ptr", buf.Ptr, "UInt", bs * bs * 4)
    DllCall("user32\UpdateLayeredWindow"
        , "Ptr", G_PetHwnd, "Ptr", G_PetScreenDC
        , "Ptr", G_PetPtDst, "Ptr", G_PetSize
        , "Ptr", G_PetMemDC, "Ptr", G_PetPtSrc
        , "UInt", 0, "Ptr", G_PetBlend, "UInt", 2)
}

; 收起：角色窗（右下角对齐）→ 浮球；位置同时落 pet-state.json
PetCollapse(silent := false) {
    global G_PetCollapsed, G_PetRestoreX, G_PetRestoreY, G_PetX, G_PetY, G_PetW, G_PetH
    global G_PetLastIdx
    if (G_PetCollapsed)
        return
    G_PetRestoreX := G_PetX, G_PetRestoreY := G_PetY
    bs := PetBallSizePx()
    G_PetCollapsed := true
    ; 先落状态：x/y 存**展开位置**（浮球位置由它右下角对齐推出来），重启才回得去
    if (!silent)
        PetStateSave()
    G_PetX := G_PetRestoreX + G_PetW - bs
    G_PetY := G_PetRestoreY + G_PetH - bs
    G_PetLastIdx := -1
    SetTimer(PetTick, 0)
    PetApplyBallFrame()
    PetFeedReanchor(0, 0)                  ; v35-ux D1：角色收球后信息流重新停靠
    if (!silent)
        Log("桌宠：已收成 40×40 浮球 x=" . G_PetX . " y=" . G_PetY)
}

; 展开：浮球 → 角色窗（回到收起前的位置）
PetExpand() {
    global G_PetCollapsed, G_PetRestoreX, G_PetRestoreY, G_PetX, G_PetY, G_PetW, G_PetH
    global G_PetGui, G_PetSize, G_PetPtDst, G_PetLastIdx, G_PetLastPath, G_PetBallData
    if (!G_PetCollapsed)
        return
    G_PetCollapsed := false
    G_PetBallData := 0
    G_PetX := G_PetRestoreX, G_PetY := G_PetRestoreY
    NumPut("Int", G_PetW, G_PetSize, 0), NumPut("Int", G_PetH, G_PetSize, 4)
    NumPut("Int", G_PetX, G_PetPtDst, 0), NumPut("Int", G_PetY, G_PetPtDst, 4)
    try G_PetGui.Move(G_PetX, G_PetY, G_PetW, G_PetH)
    PetResizeDib(G_PetW, G_PetH)
    G_PetLastIdx := -1
    G_PetLastPath := ""
    PetTick()
    PetFeedReanchor(0, 0)                  ; v35-ux D1：展开回角色后信息流重新停靠
    PetStateSave()
    Log("桌宠：已展开回角色窗 x=" . G_PetX . " y=" . G_PetY)
}

; v35-ux D3：单击**立即**弹主菜单（不再等 250ms 双击判定——用户反馈「点击手感
; 不拖沓」）；双击路径先收菜单再收球（PetOnDoubleClick），两不相扰。
PetOnDoubleClick(wParam, lParam, msg, hwnd) {
    global G_PetHwnd, G_PetCollapsed
    if (hwnd != G_PetHwnd || G_PetCollapsed)
        return
    PetMenuClose()
    PetCollapse()
}
; --- 分层窗口 ---------------------------------------------------------------
PetShowFrame(clip, idx) {
    global G_PetHwnd, G_PetBits, G_PetMemDC, G_PetScreenDC, G_PetBlend
    global G_PetPtDst, G_PetSize, G_PetPtSrc, G_PetLastData, G_PetLastIdx, G_PetFrameBytes
    global G_PetLastPath, G_PetMissCount
    path := PetFramePath(clip, idx)
    if (path = G_PetLastPath && idx = G_PetLastIdx)
        return 1
    buf := PetFrameLoad(path)
    if (!buf) {
        ; 帧读不出来：画面停在上一张好帧（静态首帧），慢速重试；连续失败过多才整体降级
        G_PetMissCount += 1
        if (G_PetMissCount >= 20) {
            Log("桌宠：连续 " . G_PetMissCount . " 帧读不出来，降级不启动（HUD 接管）")
            PetShutdown()
        }
        return 0
    }
    G_PetMissCount := 0
    DllCall("RtlMoveMemory", "Ptr", G_PetBits, "Ptr", buf.Ptr, "UInt", G_PetFrameBytes)
    DllCall("user32\UpdateLayeredWindow"
        , "Ptr", G_PetHwnd, "Ptr", G_PetScreenDC
        , "Ptr", G_PetPtDst, "Ptr", G_PetSize
        , "Ptr", G_PetMemDC, "Ptr", G_PetPtSrc
        , "UInt", 0, "Ptr", G_PetBlend, "UInt", 2)
    G_PetLastData := buf
    G_PetLastIdx := idx
    G_PetLastPath := path
    return 1
}

PetBuildWindow(x, y) {
    global G_PetGui, G_PetHwnd, G_PetW, G_PetH, G_PetBits, G_PetDib, G_PetMemDC
    global G_PetScreenDC, G_PetBlend, G_PetPtDst, G_PetSize, G_PetPtSrc
    global G_PetX, G_PetY, G_PetHookOn
    ; 绝不加 +E0x20（WS_EX_TRANSPARENT 会连角色本体一起穿透）；
    ; 绝不用 WinSetTransparent（整窗 alpha，逐像素透明互斥）。
    g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x80000 +E0x08000000")
    g.MarginX := 0, g.MarginY := 0
    g.BackColor := "000000"
    g.Show(Format("x{} y{} w{} h{} NoActivate", x, y, G_PetW, G_PetH))
    G_PetGui := g, G_PetHwnd := g.Hwnd
    G_PetX := x, G_PetY := y

    bi := Buffer(40, 0)
    NumPut("Int", 40, bi, 0), NumPut("Int", G_PetW, bi, 4), NumPut("Int", -G_PetH, bi, 8)
    NumPut("UShort", 1, bi, 12), NumPut("UShort", 32, bi, 14), NumPut("Int", 0, bi, 16)
    G_PetDib := DllCall("gdi32\CreateDIBSection", "Ptr", 0, "Ptr", bi, "UInt", 0
        , "Ptr*", &G_PetBits, "Ptr", 0, "UInt", 0, "Ptr")
    G_PetScreenDC := DllCall("user32\GetDC", "Ptr", 0, "Ptr")
    G_PetMemDC := DllCall("gdi32\CreateCompatibleDC", "Ptr", G_PetScreenDC, "Ptr")
    DllCall("gdi32\SelectObject", "Ptr", G_PetMemDC, "Ptr", G_PetDib)
    G_PetBlend := Buffer(4, 0)
    NumPut("UChar", 0, G_PetBlend, 0)      ; AC_SRC_OVER
    NumPut("UChar", 0, G_PetBlend, 1)      ; flags
    NumPut("UChar", 255, G_PetBlend, 2)    ; SourceConstantAlpha
    NumPut("UChar", 1, G_PetBlend, 3)      ; AC_SRC_ALPHA
    G_PetPtDst := Buffer(8, 0)
    NumPut("Int", x, G_PetPtDst, 0), NumPut("Int", y, G_PetPtDst, 4)
    G_PetSize := Buffer(8, 0)
    NumPut("Int", G_PetW, G_PetSize, 0), NumPut("Int", G_PetH, G_PetSize, 4)
    G_PetPtSrc := Buffer(8, 0)
    if (!G_PetHookOn) {
        OnMessage(0x84, PetOnHitTest)    ; WM_NCHITTEST
        OnMessage(0x201, PetOnLButtonDown)
        OnMessage(0x200, PetOnMouseMove)
        OnMessage(0x202, PetOnLButtonUp)
        OnMessage(0x203, PetOnDoubleClick)   ; WM_LBUTTONDBLCLK：双击收起浮球
        OnMessage(0x205, PetOnRButtonUp)
        G_PetHookOn := true
    }
}

; 透明像素穿透、角色本体可点（分层窗的 alpha 命中语义）
PetAlphaAt(cx, cy) {
    global G_PetLastData, G_PetStride, G_PetW, G_PetH, G_PetCollapsed, G_PetBallData, G_PetBallSize
    if (G_PetCollapsed) {
        if (!IsObject(G_PetBallData) || G_PetBallSize <= 0)
            return 0
        if (cx < 0 || cy < 0 || cx >= G_PetBallSize || cy >= G_PetBallSize)
            return 0
        return NumGet(G_PetBallData, cy * G_PetBallSize * 4 + cx * 4 + 3, "UChar")
    }
    if (!IsObject(G_PetLastData))
        return 0
    if (cx < 0 || cy < 0 || cx >= G_PetW || cy >= G_PetH)
        return 0
    return NumGet(G_PetLastData, cy * G_PetStride + cx * 4 + 3, "UChar")
}

PetOnHitTest(wParam, lParam, msg, hwnd) {
    global G_PetHwnd, G_PetX, G_PetY
    if (hwnd != G_PetHwnd)
        return
    sx := lParam & 0xFFFF
    sy := (lParam >> 16) & 0xFFFF
    if (PetAlphaAt(sx - G_PetX, sy - G_PetY) = 0)
        return -1                          ; HTTRANSPARENT → 点击落到下层
    return 1                               ; HTCLIENT → 角色本体可点
}

; --- 鼠标：拖动跟手 / 单击弹菜单 / 右键设置 ---------------------------------
PetOnLButtonDown(wParam, lParam, msg, hwnd) {
    global G_PetHwnd, G_PetDownX, G_PetDownY, G_PetWinX, G_PetWinY
    global G_PetDragging, G_PetMoved, G_PetX, G_PetY, G_PetDragSnap, G_PetDragAt
    global G_BubX, G_BubY, G_BubShown, G_FeedX, G_FeedY, G_FeedShown
    global G_MenuX, G_MenuY, G_MenuShown, G_GlassWinX, G_GlassWinY, G_GlassShown
    if (hwnd != G_PetHwnd)
        return
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    G_PetDownX := NumGet(pt, 0, "Int"), G_PetDownY := NumGet(pt, 4, "Int")
    G_PetWinX := G_PetX, G_PetWinY := G_PetY
    ; v36 决策 11：拖动是纯 move 阶段 —— 停表，不再逐帧 memcpy + UpdateLayeredWindow
    ; 和鼠标回调抢同一个消息线程（诊断 §3.4）。松手由 PetOnLButtonUp 重新起表。
    G_PetDragAt := A_TickCount
    SetTimer(PetTick, 0)
    ; v36 决策 10：按下这一刻快照四个跟随浮层的原点。拖动期只用「快照 + 自按下
    ; 起的累计位移」重算绝对位置（幂等），绝不读「上一次移动后的当前位置」再叠加
    ; —— 那是浮层乱飞的根因（坐标 double-apply）。隐藏中的层记 0：拖动中才弹
    ; 出来的（气泡 / 玻璃卡）在第一次 move 时就地重锚，不会按 0,0 飞出屏幕。
    G_PetDragSnap := Map(
        "bub", G_BubShown ? { x: G_BubX, y: G_BubY } : 0
      , "feed", G_FeedShown ? { x: G_FeedX, y: G_FeedY } : 0
      , "menu", G_MenuShown ? { x: G_MenuX, y: G_MenuY } : 0
      , "glass", G_GlassShown ? { x: G_GlassWinX, y: G_GlassWinY } : 0)
    G_PetDragging := true
    G_PetMoved := false
    return 0
}

PetOnMouseMove(wParam, lParam, msg, hwnd) {
    global G_PetHwnd, G_PetDragging, G_PetMoved, G_PetDownX, G_PetDownY
    global G_PetWinX, G_PetWinY, G_PetX, G_PetY, G_PetPtDst, G_PetDragSnap
    global G_PetW, G_PetH
    global G_BubX, G_BubY, G_BubShown, G_FeedX, G_FeedY, G_FeedShown
    global G_MenuX, G_MenuY, G_MenuShown, G_GlassWinX, G_GlassWinY, G_GlassShown
    if (hwnd != G_PetHwnd || !G_PetDragging)
        return
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    cx := NumGet(pt, 0, "Int"), cy := NumGet(pt, 4, "Int")
    dx := cx - G_PetDownX, dy := cy - G_PetDownY
    if (!G_PetMoved && Abs(dx) + Abs(dy) < 4)
        return
    G_PetMoved := true
    ; v36 决策 10：本体 = 按下快照 + 累计位移，再夹进虚拟屏（多屏 / 负坐标 /
    ; DPI 缩放同一口径；拖到屏幕外就再也找不回来）。
    pos := PetDragPlace(G_PetWinX, G_PetWinY, dx, dy, G_PetW, G_PetH, VirtualScreenRect())
    G_PetX := pos.x, G_PetY := pos.y
    NumPut("Int", G_PetX, G_PetPtDst, 0)
    NumPut("Int", G_PetY, G_PetPtDst, 4)
    DllCall("SetWindowPos", "Ptr", G_PetHwnd, "Ptr", 0, "Int", G_PetX, "Int", G_PetY
        , "Int", 0, "Int", 0, "UInt", 0x0015)   ; NOSIZE|NOZORDER|NOACTIVATE
    ; 夹取后角色的实际位移可能小于鼠标位移：浮层跟**实际**位移走，相对位置不漂。
    ex := G_PetX - G_PetWinX, ey := G_PetY - G_PetWinY
    ; v35-ux D4：同一 (ex,ey) 依次 move 各浮层 —— 全部 move-only
    ; （SetWindowPos NOSIZE|NOZORDER|NOACTIVATE），**拖动中不重建任何 DIB**。
    ; v36 决策 10：位置一律由按下快照 + 累计位移重算，不读上次结果叠加。
    p := PetDragLayerPos(G_PetDragSnap, "bub", G_BubShown, G_BubX, G_BubY, ex, ey)
    if (IsObject(p))
        PetBubbleMoveTo(p.x, p.y)
    p := PetDragLayerPos(G_PetDragSnap, "feed", G_FeedShown, G_FeedX, G_FeedY, ex, ey)
    if (IsObject(p))
        PetFeedMoveTo(p.x, p.y)
    p := PetDragLayerPos(G_PetDragSnap, "menu", G_MenuShown, G_MenuX, G_MenuY, ex, ey)
    if (IsObject(p))
        PetMenuMoveTo(p.x, p.y)
    p := PetDragLayerPos(G_PetDragSnap, "glass", G_GlassShown, G_GlassWinX, G_GlassWinY, ex, ey)
    if (IsObject(p))
        GlassCardMoveTo(p.x, p.y)
}

PetOnLButtonUp(wParam, lParam, msg, hwnd) {
    global G_PetHwnd, G_PetDragging, G_PetMoved, G_PetX, G_PetY
    global G_PetCollapsed, G_PetDragAt, G_PetFaceStart, G_PetMotion, G_PetMotionStart
    if (hwnd != G_PetHwnd)
        return
    wasMoved := G_PetMoved
    G_PetDragging := false
    G_PetMoved := false
    ; v36 决策 11：拖动期停过表。把动画时间轴校正回「冻结那一刻」再按当前剪辑
    ; 重新起表 —— 松手从当前状态接着播（不跳过整段拖动时长，也不空转）。
    ; 拖动期若换过脸 / 换过动作（1Hz 边沿），它的起点已经晚于冻结时刻，不能再挪。
    if (G_PetDragAt > 0) {
        frozenAt := G_PetDragAt
        spent := A_TickCount - frozenAt
        G_PetDragAt := 0
        if (G_PetFaceStart <= frozenAt)
            G_PetFaceStart += spent
        if (G_PetMotion != "" && G_PetMotionStart <= frozenAt)
            G_PetMotionStart += spent
        SetTimer(PetTick, -1)
    }
    if (wasMoved) {
        PetStateSave()
        Log("桌宠：位置已记住 x=" . G_PetX . " y=" . G_PetY)
        return
    }
    if (G_PetCollapsed) {
        PetExpand()                        ; 浮球：点一下/双击都展开
        return
    }
    ; v35-ux D3：展开态单击 → 立即弹主菜单（打开时再点 = 收面板，路由在
    ; PetMenuShowMain 里）；双击由 PetOnDoubleClick 先收菜单再收球。
    PetMenuShowMain()
}

PetOnRButtonUp(wParam, lParam, msg, hwnd) {
    global G_PetHwnd
    if (hwnd != G_PetHwnd)
        return
    PetMenuShowSettings()
}

; --- 菜单：v35-ux D3 起改走 lib/PetMenu.ahk 的半透明面板 --------------------
;  native `Menu()` 已废除（标签静态、样式突兀、位置乱）；单击路由直接调
;  PetMenuShowMain() / PetMenuShowSettings()，菜单项复用的既有动作
;  （CaptureArmHotkey / PetPickOpen / BandRedragStart / PetToggleDnd /
;  PetReplayGuide / PetDisable）留在本文件，热键与菜单仍同一条路径。

PetToggleDnd() {
    global G_PetDnd
    G_PetDnd := !G_PetDnd
    PetStateSave()
    ShowToast(G_PetDnd ? "桌宠：已勿扰（不再主动说话）" : "桌宠：勿扰已关")
    Log("桌宠：勿扰=" . (G_PetDnd ? "on" : "off"))
}

PetReplayGuide() {
    global G_PetGuide
    G_PetGuide := 0
    PetStateSave()
    ShowToast("桌宠：引导已重置（下次开机重讲四步）")
    Log("桌宠：引导进度已重置")
}

; 关闭桌宠开关（pet_enabled）：收窗 + 落状态；下一拍 ping 起 HUD 自动接管（D-L3）
PetDisable() {
    global G_PetEnabled
    G_PetEnabled := false
    PetStateSave()
    PetShutdown()
    ShowToast("桌宠已关闭；状态回到右上角浮窗")
    Log("桌宠：已关闭（pet_enabled=false）")
}

; 挑词：进既有挑词页（#/pick/<会话 id>）；无待选句时菜单项已禁用并写明原因
PetPickOpen() {
    global G_WebPort, G_CapSession
    if (G_CapSession <= 0) {
        ShowToast("还没有正在记的会话，挑词先等攒到句子")
        return
    }
    url := "http://127.0.0.1:" . G_WebPort . "/#/pick/" . G_CapSession
    Log("桌宠：打开挑词页 " . url)
    try Run(url)
    catch as err
        Log("桌宠：打开挑词页失败 " . err.Message)
}

; 待挑 N：v36 票 #243 起**不在点击链路上同步查**了 —— 菜单先建窗显示（用记忆值），
; 回包由 PetMenuRefreshAsync 异步取（lib/PetNet.ahk，超时 ≤300ms）；解析是纯函数
; PetBacklogCount（lib/Logic.ahk），拿不到按 0 处理 → 菜单项禁用并显示原因，不崩。

; --- 帧调度 -----------------------------------------------------------------
; 当前该播的 clip 与起点：一次性动作优先；播完自动回状态脸（片 47 的纯函数）。
PetCurrentClip() {
    global G_PetManifest, G_PetFace, G_PetMotion, G_PetMotionStart, G_PetStaticClip
    faceClip := PetClipResolve(G_PetManifest, "expressions", G_PetFace)
    idleClip := PetClipResolve(G_PetManifest, "motions", "idle")
    if (!IsObject(faceClip) && !IsObject(idleClip))
        return { clip: G_PetStaticClip, start: 0, motion: "", static: true }
    motionKey := G_PetMotion
    if (motionKey != "") {
        mc := PetClipResolve(G_PetManifest, "motions", motionKey)
        elapsed := A_TickCount - G_PetMotionStart
        if (IsObject(mc) && !PetClipDone(mc, elapsed))
            return { clip: mc, start: G_PetMotionStart, motion: motionKey }
        G_PetMotion := ""                 ; 播完回待机
    }
    return { clip: PetActiveClip(idleClip, faceClip, "", G_PetManifest)
           , start: G_PetFaceStart, motion: "" }
}

PetTick() {
    global G_PetHwnd, G_PetLastIdx, G_PetCollapsed, G_PetDragging
    if (!G_PetHwnd || !PetHealthy())
        return
    ; v36 决策 11：拖动期是纯 move 阶段 —— 谁把表重新打开都一律停（1Hz 边沿的
    ; PetApplyVisual 会直接调 PetTick，不能让它把帧调度在拖动中重新点着）。
    if (IsSet(G_PetDragging) && G_PetDragging) {
        SetTimer(PetTick, 0)
        return
    }
    if (G_PetCollapsed) {
        ; 浮球态不播动画（CPU 更低）：只有语气色变了才重画
        SetTimer(PetTick, 0)
        return
    }
    st := PetCurrentClip()
    clip := st.clip
    if (st.HasProp("static") && st.static) {
        ; 素材只够静态首帧：贴一帧就停表（不空转、不黑窗）
        if (IsObject(clip))
            PetShowFrame(clip, 0)
        SetTimer(PetTick, 0)
        return
    }
    if (IsObject(clip)) {
        elapsed := A_TickCount - st.start
        idx := PetClipFrameAt(clip, elapsed)
        if (idx != G_PetLastIdx || st.motion != "") {
            if (!PetShowFrame(clip, idx)) {
                SetTimer(PetTick, -250)      ; 缺帧：画面静止、慢速重试，不空转
                return
            }
        }
        fps := clip.fps > 0 ? clip.fps : 8
        per := 1000.0 / fps
        delay := Round(per - Mod(elapsed, per)) + 1
        SetTimer(PetTick, -Max(5, delay))
    } else {
        SetTimer(PetTick, -100)
    }
}

; --- 生命周期 ---------------------------------------------------------------
PetStartup() {
    global G_PetManifest, G_PetW, G_PetH, G_PetStride, G_PetFrameBytes
    global G_PetEnabled, G_PetDnd, G_PetGuide, G_PetX, G_PetY, G_PetFace, G_PetFaceStart
    global G_PetStatePath, G_PetStaticClip, G_PetLastData
    ; v35-ux 顺手修复（#234 遗留）：G_PetRestoreX/Y 只写进了局部，浮球重启恢复位置永远回 0,0
    global G_PetRestoreX, G_PetRestoreY
    ; v35-ux：ambient/换脸的下次时刻（缺 global 会静默写局部，插播永远不触发）
    global G_PetAmbientNextAt, G_PetIdleFaceAt
    G_PetStatePath := PetStatePath()
    st := PetStateRead()
    G_PetEnabled := st.enabled
    G_PetDnd := st.dnd
    G_PetGuide := st.guide
    if (!G_PetEnabled) {
        Log("桌宠：pet_enabled=false，按关闭处理（HUD 原样工作）")
        return
    }
    manifestPath := PetDataDir() . "\manifest.json"
    if (!FileExist(manifestPath)) {
        ; 素材缺失 → 桌宠整体不启动，状态退回 v34 朴素浮窗（spec §降级）。
        ; 标记 missing manifest，不建窗、不改 HUD 行为。
        Log("桌宠：素材缺失（missing manifest " . manifestPath . "），桌宠不启动")
        return
    }
    try text := FileRead(manifestPath, "UTF-8")
    catch as err {
        Log("桌宠：读 manifest 失败 " . err.Message . "，桌宠不启动")
        return
    }
    G_PetManifest := PetFramesParse(text)
    if (!IsObject(G_PetManifest)) {
        Log("桌宠：manifest 格式不认识，桌宠不启动")
        return
    }
    G_PetStaticClip := PetStaticClip(G_PetManifest)
    if (!IsObject(G_PetStaticClip)) {
        Log("桌宠：manifest 里没有任何可用 clip，桌宠不启动")
        return
    }
    G_PetW := G_PetManifest.canvasW
    G_PetH := G_PetManifest.canvasH
    G_PetStride := G_PetManifest.stride
    G_PetFrameBytes := G_PetManifest.frameBytes
    ; 位置：上次拖到哪就回哪（多屏 / 负坐标也是合法位置，不能一律当「屏幕外」）；
    ; 没有记录（-1,-1）落右下角；落在虚拟屏外（显示器拔了）就地夹回来 —— 与拖动
    ; 夹取同一口径（v36 决策 10），DPI 缩放已在物理像素上。
    x := st.x, y := st.y
    if (x = -1 && y = -1) {
        x := A_ScreenWidth - G_PetW - P(24)
        y := A_ScreenHeight - G_PetH - P(40)
    } else {
        pos := PetDragPlace(x, y, 0, 0, G_PetW, G_PetH, VirtualScreenRect())
        x := pos.x, y := pos.y
    }
    PetBuildWindow(x, y)
    G_PetFace := "focus"
    G_PetFaceStart := A_TickCount
    ; 先贴第一帧再开拍子（避免空白窗）；脸 clip 缺失时贴静态兜底首帧
    firstClip := PetClipResolve(G_PetManifest, "expressions", G_PetFace)
    if (!IsObject(firstClip))
        firstClip := G_PetStaticClip
    PetShowFrame(firstClip, 0)
    if (!IsObject(G_PetLastData) && firstClip != G_PetStaticClip) {
        ; 首帧读不出来：退到静态兜底 clip 再试一次；还不行就整体降级（HUD 接管）
        firstClip := G_PetStaticClip
        PetShowFrame(firstClip, 0)
    }
    if (!IsObject(G_PetLastData)) {
        Log("桌宠：首帧读不出来，桌宠不启动（HUD/玻璃卡继续工作）")
        PetShutdown()
        return
    }
    if (st.collapsed) {
        G_PetRestoreX := x, G_PetRestoreY := y
        PetCollapse(true)                  ; 静默收起（不重复落状态）
    }
    SetTimer(PetTick, -30)
    ; v35-ux D5：随机 idle 插播的调度（判定在 Logic.ahk 纯函数；首个插播等 3–6 分钟）
    G_PetAmbientNextAt := PetAmbientNextAt(A_TickCount, Random(0, 100000))
    G_PetIdleFaceAt := PetIdleFaceGapMs(A_TickCount, Random(0, 100000))
    SetTimer(PetIdleAmbientTick, 15000)   ; 15s 一查：换脸 25–70s 的不规律感不被 30s 粒度吃掉
    ; v35-ux D1：信息流面板随桌宠一起就位（读 pet-state.json 的 feed_* 字段）
    PetFeedStartup()
    Log("桌宠：已启动 " . G_PetW . "x" . G_PetH . " @ " . x . "," . y
        . "（motions=" . G_PetManifest.motions.Count . " expressions=" . G_PetManifest.expressions.Count . "）")
}

; 1Hz ping 钩子：只消费 StatusVisual 的输出（face / motion），不判断相位。
PetApplyVisual(phase) {
    global G_PetManifest, G_PetFace, G_PetFaceStart, G_PetMotion, G_PetMotionStart
    global G_PetMotionSeen, G_Cfg, G_PetToneColor, G_CapArmed, G_PetPhase, G_PetIdleFace
    ; v35-ux 顺手修复（#237 遗留）：换脸的 G_PetLastIdx := -1 只写了局部，「1 帧内换脸」不生效
    global G_PetLastIdx
    if (!PetHealthy() || !IsObject(G_PetManifest))
        return
    G_PetPhase := phase                ; ambient 判定用（warn/error 不插播，见 Logic.ahk）
    ; v35-ux D1 接线：armed 位驱动信息流自动显隐（PetFeedSetArmed 内部按边沿收敛）；
    ; 默认停靠位随角色维护（用户拖过 / 拖动中都不动，见 PetFeedReanchor）。
    PetFeedSetArmed(IsSet(G_CapArmed) ? G_CapArmed : false)
    PetFeedReanchor(G_PetX, G_PetY)
    visual := StatusVisual(phase, G_Cfg)
    G_PetToneColor := visual.color
    if (G_PetCollapsed) {
        PetApplyBallFrame()                ; 浮球只跟语气色变（不播动画）
        return
    }
    face := PetFaceKey(visual)
    ; v35-ux 验收：闲时（tone=idle）用轮换脸（PetIdleAmbientTick 每 25–70s 换一张，
    ; 别老是一张打瞌睡脸）；认真相位（armed 等）清轮换、脸跟相位走（那是「干活中」）。
    if (visual.tone = "idle") {
        if (G_PetIdleFace != "" && IsObject(PetClipResolve(G_PetManifest, "expressions", G_PetIdleFace)))
            face := G_PetIdleFace
    } else {
        G_PetIdleFace := ""
    }
    if (face != G_PetFace) {
        if (IsObject(PetClipResolve(G_PetManifest, "expressions", face))) {
            G_PetFace := face
            G_PetFaceStart := A_TickCount
            G_PetLastIdx := -1            ; 立刻重画：脸在 1 帧内换过来（#237 验收）
            SetTimer(PetTick, -1)
        }
    }
    motion := PetMotionKey(visual, G_PetManifest)
    ; 边沿触发：同一个动作键只在首次出现时播一次（1Hz ping 不重播）
    if (motion != "" && motion != G_PetMotionSeen) {
        mc := PetClipResolve(G_PetManifest, "motions", motion)
        if (IsObject(mc)) {
            G_PetMotion := motion
            G_PetMotionStart := A_TickCount
            PetTick()
        }
    }
    G_PetMotionSeen := motion
}

; --- 随机 idle 插播（v35-ux D5）---------------------------------------------
; 每 15s 醒一次，判定交给 Logic.ahk 的纯函数（warn/error
; 相位一律不插播；步进 3–6 分钟，PetAmbientNextAt 算）。插播的只是「卖萌动作」——
; stretch / selfie_fast 里随机一个，走与 1Hz ping 动作同一条通道
; （G_PetMotion + PetTick），播完自动回状态脸；勿扰只静说话、不禁动作（spec）。
PetIdleAmbientTick() {
    global G_PetHwnd, G_PetCollapsed, G_PetFace, G_PetPhase, G_PetAmbientNextAt
    global G_PetMotion, G_PetMotionStart, G_PetManifest
    global G_PetIdleFace, G_PetIdleFaceAt
    ; 2026-11-10 复核补：换脸这两笔原先没声明，写成了**局部变量**——新表情的起始
    ; 时刻与帧指针都没落到全局（AHK v2 脚本顶层的 global 不是超全局，函数里不写
    ; 就另起一个局部）。后果是换脸后 PetTick 仍按上一张脸的起点算帧、G_PetLastIdx
    ; 不复位 → 新表情从旧帧号接着画（同 784 行那条 #237 遗留同一族）。
    global G_PetFaceStart, G_PetLastIdx
    if (!G_PetHwnd || G_PetCollapsed || !IsObject(G_PetManifest))
        return
    if (G_PetMotion != "")             ; 一次性动作播着呢，不打断
        return
    now := A_TickCount
    ; ① 闲时表情轮换（25–70s 不规律一张；armed 相位 PetIdleFacePick 直接拒绝）
    nf := PetIdleFacePick(G_PetFace, G_PetIdleFaceAt, now, G_PetPhase, Random(0, 100000))
    if (nf != "" && IsObject(PetClipResolve(G_PetManifest, "expressions", nf))) {
        G_PetIdleFace := nf
        G_PetIdleFaceAt := PetIdleFaceGapMs(now, Random(0, 100000))
        if (nf != G_PetFace) {
            G_PetFace := nf
            G_PetFaceStart := now
            G_PetLastIdx := -1
            PetTick()
            return                   ; 这拍先看新脸，动作让下一拍再说
        }
    }
    ; ② 随机小动作插播（2–4 分钟不规律一次）
    if (!PetAmbientShouldPlay(G_PetFace, G_PetAmbientNextAt, now, G_PetPhase))
        return
    key := PetAmbientPick(Random(0, 100000))   ; 键池在 Logic.ahk（角色窗不写死动作键）
    mc := PetClipResolve(G_PetManifest, "motions", key)
    if (!IsObject(mc))
        return
    G_PetMotion := key
    G_PetMotionStart := now
    G_PetAmbientNextAt := PetAmbientNextAt(now, Random(0, 100000))
    PetTick()
}

PetShutdown() {
    global G_PetGui, G_PetHwnd
    try SetTimer(PetTick, 0)
    try SetTimer(PetIdleAmbientTick, 0)
    ; v35-ux D1/D3：桌宠收掉，信息流与菜单面板一起收（不留无主分层窗）
    PetMenuClose()
    PetFeedShutdown()
    if (G_PetGui) {
        try G_PetGui.Destroy()
    }
    G_PetGui := 0
    G_PetHwnd := 0
}
