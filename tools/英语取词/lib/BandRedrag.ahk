; ============================================================================
;  lib/BandRedrag.ahk —— v34 T4（票 #217 / spec §B15）：字幕带重拖并入主壳
;
;  原独立脚本「字幕带重拖.ahk」（v33 修复轮）的遮罩/拖框/发 RPC 逻辑整段搬进来，
;  成为主壳的一个普通全局热键：随外壳启动、进设置页、参与冲突检测、受托盘
;  「暂停热键」管住，改键约 2 秒热生效 —— 这些都是统一热键表（HotkeyEntries +
;  RegisterGlobalHotkeys）白拿的，本模块只负责「按下之后」。
;
;  入口：HotkeyEntries 的 `hotkey_redrag`（默认 `^!6`）→ `BandRedragStart()`。
;  流程：全屏半透明遮罩 + 拖框 → 松手把矩形（**物理像素**，与截图取词
;  capture_ocr 同一坐标系）经主壳唯一的中枢 RPC 通道发
;  `{"cmd":"band_redrag","rect":[x,y,w,h]}` → 调度器负责持久化 + 解除「请重拖」；
;  成功/失败由 `OnBandRedragLine` 如实提示（hub 侧零改动）。
;
;  并入后与原脚本的差异只有这四处（其余逐段照搬）：
;   1. 互斥：与截图取词共用 `G_ShotActive`（拖框期间占住、结束释放），一方进行中
;      另一方按了不生效，不出现双层遮罩；长文遮罩在途也拒绝。
;   2. Esc：窗口条件绑定（`HotIfWinExist` + 遮罩窗口），拖框结束立刻解绑 ——
;      不再像独立脚本那样全局注册 Esc（那会吞掉遮罩窗口以外的按键）。
;   3. DPI / 几何助手换主壳的：`P()` / `D` / `VirtualScreenRect()` / `LP` /
;      `MASK_ALPHA` / `SHOT_IDLE_MS`；选区下限保留原脚本的 8lp（不动截图取词的 4lp）。
;   4. RPC 不再自建 socket：走 `HubSend`（主壳同一套连接 / 探测 / 代次纪律）。
;
;  视觉不变量照抄：纯黑 + 整窗 alpha 90（≈35% 不透明，绝不 255 —— 不透明会让
;  Chrome 判定窗口被遮挡而暂停视频，#5 八组实测）；`NoActivate` 不抢焦点。
;  30 秒无操作自动收掉；取消 / 选区过小都不发 RPC，区域保持不变。
; ============================================================================

; --- 运行时状态 --------------------------------------------------------------
global G_RedragCancel := false          ; 本次拖框的取消位（Esc / 30s 兜底置位）
; 选区最小边长 lp。原脚本是 8lp；主壳截图取词是 4lp —— 两个手势各保持各的手感，
; 故意不复用 MIN_SELECT_LP（改它会连带改变三条老手势里的截图取词）。
global REDRAG_MIN_SELECT_LP := 8

; 热键回调：全屏遮罩 + 拖框 → 松手发 band_redrag。重入由 G_ShotActive 挡掉。
BandRedragStart() {
    global G_ShotActive, G_RedragCancel, SHOT_IDLE_MS, REDRAG_MIN_SELECT_LP
    global G_LongBusy, G_LongPending
    ; v35 D6（v35-ux design.md，用户反馈 #9）：框选期间的媒体暂停联动。
    ; 复用 BeginCapture 的同一条 pause 漏斗：HubMedia("pause", app, title) +
    ; 「是我们暂停的」标志（G_MediaPauseSent + 会话线索），收尾用
    ; MediaResumeAfterCapture() 原样恢复 —— 不复制暂停/恢复逻辑。
    global G_MediaPauseSent, G_SessApp, G_SessRawTitle
    if (G_ShotActive) {
        Log("拖框进行中，忽略字幕带重拖热键")
        return
    }
    ; 长文拖框/识别在途也拒绝（与截图取词两入口同一判据）：任何两条全屏遮罩
    ; 都不许叠在一起 —— 长文入口看 G_ShotActive，这里对称地看它的 busy 位。
    if (LongformInFlight(G_LongBusy, G_LongPending)) {
        Log("长文在途：字幕带重拖已拒绝（避免双遮罩）")
        ShowToast("长文识别进行中，请稍后再拖字幕带", "err")
        return
    }

    ; v35 D6：遮罩出现**之前**先停播 —— 此刻前台还是用户的视频窗口，hub 的门
    ; （app token + 标题宽松匹配）才对得上（BeginCapture 同一时刻约束）。
    ; 幂等：截图取词/呼出框已经压着暂停（G_MediaPauseSent）就不再发，恢复也
    ; 不归我们管（redragPausedByUs 收窄到「这次我们自己发的 pause」）；拿不到
    ; 出处（没有前台窗口）就不暂停也不恢复 —— 绝不把用户没暂停的播错。
    redragPausedByUs := false
    if (!G_MediaPauseSent) {
        prov := CaptureProvenance()
        if (prov.app != "") {
            if (HubMedia("pause", prov.app, prov.title)) {
                G_MediaPauseSent := true
                G_SessApp := prov.app               ; MediaResumeAfterCapture 的
                G_SessRawTitle := prov.title        ; resume 门用的就是这两个值
                redragPausedByUs := true
            }
        }
    }

    G_ShotActive := true                   ; 与截图取词共用的互斥锁
    G_RedragCancel := false
    vr := VirtualScreenRect()
    ov := 0, hintG := 0, roG := 0, bars := []
    rect := ""
    tooSmall := false
    escScoped := false
    try {
        ; 遮罩：纯黑 + 整窗 alpha 90 + NoActivate（三条不变量照抄主壳 RunShot）
        ov := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000")
        ov.BackColor := LP.BLACK
        ov.Show("x" . vr.x . " y" . vr.y . " w" . vr.w . " h" . vr.h . " NoActivate")
        WinSetTransparent(MASK_ALPHA, ov)

        ; 提示文字：独立小窗（画在遮罩上会被 35% 透明一起吃成灰字）+ 鼠标穿透
        hintG := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
        hintG.BackColor := LP.BLACK
        hintG.MarginX := P(14), hintG.MarginY := P(8)
        hintG.SetFont("s12 cFFFFFF", "Segoe UI")
        hintG.AddText(, "拖框圈住字幕带 · Esc 取消 · 松手即生效")
        hintG.Show("AutoSize NoActivate Hide")
        WinSetTransparent(220, hintG)
        hintG.GetPos(, , &hw)
        hintG.Move(vr.x + Round((vr.w - hw) / 2), vr.y + P(24))
        hintG.Show("NoActivate")

        ; 选区描边：外圈 2lp 黑 + 内圈 3lp 蓝（主壳 DrawSel 同款双层）
        Loop 2 {
            col := (A_Index = 1) ? LP.BLACK : LP.ACCENT
            Loop 4 {
                b := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
                b.BackColor := col
                b.Show("x0 y0 w0 h0 NoActivate")
                bars.Push(b)
            }
        }

        ; 尺寸读数：选区左上外侧（主壳 roG 同款）
        roG := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
        roG.BackColor := LP.BLACK
        roG.MarginX := P(8), roG.MarginY := P(3)
        roG.SetFont("s10 cFFFFFF", "Segoe UI")
        roCtl := roG.AddText("w" . P(92), "0 × 0")
        roG.Show("AutoSize NoActivate Hide")
        WinSetTransparent(220, roG)
        roG.Show("NoActivate")

        ; Esc 只在遮罩窗口存在期间生效（窗口条件绑定，不做全局注册）：
        ; 遮罩是 NoActivate、永远不会成为活动窗口，所以条件用 WinExist 而不是
        ; WinActive —— 用后者意味着 Esc 永远不生效（票 #217 的取消 AC 就没了）。
        HotIfWinExist("ahk_id " . ov.Hwnd)
        escScoped := true
        ; AHK v2 的 Hotkey() 只收函数对象：零参数裸函数名会抛
        ; 「Invalid callback function.」——2026-11-08 真机 ^!6 实测（遮罩一闪即退）。
        Hotkey("Esc", (*) => CancelRedrag(), "On")
        SetTimer(CancelRedrag, -SHOT_IDLE_MS)          ; 兜底：30s 无操作自动收掉

        ; 等按下（遮罩出现前的按下也算起点 —— 与主壳 RunShot 同语义）
        while (!GetKeyState("LButton", "P") && !G_RedragCancel)
            Sleep(15)
        if (!G_RedragCancel) {
            CoordMode("Mouse", "Screen")
            MouseGetPos(&x1, &y1)
            x2 := x1, y2 := y1
            rx := x1, ry := y1, rw := 0, rh := 0   ; 先给初值：按住前松开也不读未定义变量
            lastX := -1, lastY := -1, lastW := -1, lastH := -1
            while (GetKeyState("LButton", "P") && !G_RedragCancel) {
                MouseGetPos(&x2, &y2)
                rx := Min(x1, x2), ry := Min(y1, y2)
                rw := Abs(x2 - x1), rh := Abs(y2 - y1)
                if (rx != lastX || ry != lastY || rw != lastW || rh != lastH) {
                    lastX := rx, lastY := ry, lastW := rw, lastH := rh
                    DrawRedragSel(bars, rx, ry, rw, rh)
                    roCtl.Text := Round(rw / D) . " × " . Round(rh / D)
                    roG.Move(rx, Max(vr.y + P(8), ry - P(24)))
                }
                Sleep(10)
            }
            if (!G_RedragCancel) {
                ; 阈值是 lp，比较的是物理像素（主壳 RunShot 同款）
                if (rw < P(REDRAG_MIN_SELECT_LP) || rh < P(REDRAG_MIN_SELECT_LP)) {
                    tooSmall := true
                } else {
                    rect := { x: rx, y: ry, w: rw, h: rh }   ; 物理像素（进程是 per-monitor aware）
                }
            }
        }
    } catch as err {
        Log("字幕带重拖：拖框流程异常 " . err.Message)
        ; 拖框失败不许只留日志：遮罩会瞬间消失，用户必须看得到「这次没生效」。
        ; -1 拍延后到 finally 收完遮罩之后再弹，避免被自己的全屏遮罩盖住。
        SetTimer(() => ShowToast("字幕带拖框出错，本次没生效（详见 logs\app.log）", "err"), -1)
    } finally {
        SetTimer(CancelRedrag, 0)
        ; 只解绑我们绑过的那一个窗口条件变体；遮罩没建起来时别误碰别人的 Esc
        if (escScoped) {
            try Hotkey("Esc", "Off")
            HotIf()
        }
        for b in bars
            try b.Destroy()
        if (roG)
            try roG.Destroy()
        if (hintG)
            try hintG.Destroy()
        if (ov)
            try ov.Destroy()
        Sleep(30)                           ; 等窗口真的从屏幕上消失（主壳 RunShot 同款）
        G_ShotActive := false
        ; v35 D6：选完 / Esc 取消 / 30s 超时 / 出错，都恢复播放 —— 只在我们
        ; 自己发过 pause 时（MediaResumeAfterCapture 内部还有 G_MediaPauseSent
        ; 闸 + 清标志，重复调用不重复发 resume）。截图取词压着的暂停不碰。
        if (redragPausedByUs)
            MediaResumeAfterCapture()
    }

    if (rect = "") {
        ; Esc / 30s 超时 / 误操作 / 异常：不发 RPC，区域保持不变
        if (tooSmall)
            ShowToast("选区过小，已取消", "err")
        return
    }
    if (SendRedrag(rect.x, rect.y, rect.w, rect.h)) {
        Log("字幕带重拖：band_redrag 已发出 " . rect.x . "," . rect.y . "," . rect.w . "," . rect.h
          . "（物理像素），等中枢回话")
        ; v36：框选收尾的「主动活跃」——信息流跟着收句流程自动打开（已开着不闪）。
        ; 放在一拍后（toast 也在一拍后，避免同一回调里叠两条提示）。
        SetTimer(PetFeedOpenAfterRedrag, -100)
    }
}

; v36：redrag 发出成功后的信息流自动开启（PetFeedOpenPinned 在 PetFeed.ahk）。
; 静态 #Include 必然在位，直接调用即可 —— AHK v2.0.30 里 `Func("名")` 会抛
; `Invalid base.`（与 PetFeedSafeLine 同一个坑），旧写法等于整条功能死掉。
PetFeedOpenAfterRedrag() {
    PetFeedOpenPinned()
}

CancelRedrag() {
    global G_RedragCancel
    G_RedragCancel := true
}

; 发一条 band_redrag（一行 JSON，主壳 HubSend 统一盖章/排队；回话见 OnBandRedragLine）。
; false = 中枢不在位（已提示「区域没更新」，不假装成功）。
SendRedrag(x, y, w, h) {
    if (!HubSend('{"cmd":"band_redrag","rect":['
        . x . "," . y . "," . w . "," . h . ']}', "band_redrag")) {
        ShowToast("数据中枢不在位，字幕带区域没更新", "err")
        return false
    }
    return true
}

; `band_redrag` 的回话（v34 T4）：成功 = 区域已持久化、「请重拖」由调度器解除、
; 下一拍恢复采样；失败按 reason 如实说，不假装成功。toast 推迟一拍（socket 回调里
; 尽早返回，与 OnCaptureArmLine 同款）。
OnBandRedragLine(line) {
    global G_CapSession
    if (!JsonBool(line, "ok")) {
        reason := JsonField(line, "reason")
        if (reason = "bad_region")
            SetTimer(() => ShowToast("中枢没接受这个区域（坐标或尺寸不合法），请重拖", "err"), -1)
        else if (reason = "no_screen")
            SetTimer(() => ShowToast("中枢读不到屏幕尺寸，这次重拖没生效", "err"), -1)
        else
            SetTimer(() => ShowToast("字幕带区域没更新（" . (reason = "" ? "未知原因" : reason) . "）", "err"), -1)
        Log("字幕带重拖：中枢未接受（reason=" . (reason = "" ? "unknown" : reason) . "）")
        return
    }
    SetTimer(() => ShowToast("字幕带区域已更新，下一拍恢复采样"), -1)
    Log("字幕带重拖：区域已入账（下一拍恢复采样）")
    ; 2026-11-10：刚框过就别再问「要不要框一下」——把本会话记成已处理。
    PetBandAskMark(G_CapSession)
}

DrawRedragSel(bars, x, y, w, h) {
    if (w < 1 || h < 1)
        return
    t := P(2), u := P(3)
    ; 外圈黑（1-4）：上 / 下 / 左 / 右；内圈蓝（5-8）：细条贴边
    bars[1].Move(x - t, y - t, w + 2 * t, t)
    bars[2].Move(x - t, y + h, w + 2 * t, t)
    bars[3].Move(x - t, y, t, h)
    bars[4].Move(x + w, y, t, h)
    bars[5].Move(x, y, w, u)
    bars[6].Move(x, y + h - u, w, u)
    bars[7].Move(x + u, y + u, u, Max(1, h - 2 * u))
    bars[8].Move(x + w - u, y + u, u, Max(1, h - 2 * u))
}
