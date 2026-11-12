; ============================================================================
;  lib/LongformInspector.ahk —— v22 P2b（票 #167 / spec §9）：面板「分段预览窗」
;
;  独立只读窗口（`-DPIScale -Caption +AlwaysOnTop +ToolWindow +Resize`），由
;  长文面板底栏「分段」按钮 / 面板内 Alt+Shift+P 打开；Esc 关闭。**不复制面板视觉**
;  （面板是自绘的深色卡，这扇窗是普通浅色列表窗——校验/重分段是工具，不是阅读面）。
;
;  数据契约（P2a，`docs/research/v22-inspector-resegment/README.md` §2.1）：
;    · `article_paragraphs {doc_id}` → `{ok, paragraphs:[…], stats:{…}}`；
;      整篇一次拉完（≤60 段），**展开/悬停/选择不再发网络**；
;    · `article_resegment {doc_id}` → `{ok, doc_id, segments, remapped, needs_review}`；
;      无原始分段输入（旧 v21 文章）→ `no segmentation source`，如实提示、不伪造。
;
;  几条硬约束（写死在这里，避免以后被"优化"回去）：
;  1. **不提供删除入口**：删文章只在取词台文章页（spec §9，避免面板误操作）。
;  2. 窗口只读：列表是 ListView（无编辑能力）、全文区是 ReadOnly 多行 Edit。
;  3. 几何键 `longform_inspector_x/y/w/h` 与面板同一纪律：**启动只读一次**
;     （LongformStartup → LfInspectorStartup），拖动/缩放结束才落盘；config 热重载
;     不 apply 它们（否则 2s 热重载会把用户刚拖到的位置按旧值摆回去）。
;  4. 归属门：回包按发出顺序对应 HubSend 的 ask kind（`lf_paragraphs` /
;     `lf_resegment`），页面切走/关窗后迟到的回包直接丢弃。
;  5. suspicious 只按接口给的 `paragraphs[].suspicious`（= stats.suspicious 同源）
;     渲染成黄色图标；接口没标的一律不标（未返回的不伪造）。
; ============================================================================

; --- 运行时状态 --------------------------------------------------------------
global G_LfInspGui := 0                 ; 预览窗 Gui（0 = 不存在）
global G_LfInspC := {}                  ; 控件引用
global G_LfInspDpi := 96                ; GetDpiForWindow(预览窗)
global G_LfInspScale := 1.0             ; G_LfInspDpi / 96
global G_LfInspGeom := { x: "", y: "", w: "", h: "" }  ; config 几何（逻辑 px，启动读一次）
global G_LfInspGeomLoaded := false
global G_LfInspClosing := false
global G_LfInspBusy := false            ; 重新分段在途（按钮置灰，防连点）
global G_LfInspDocId := 0               ; 当前预览的文章（0 = 窗口里的数据不可用）
global G_LfInspData := 0                ; { paragraphs:[…], stats:{…} }
global G_LfInspKeysOn := false
global G_LfInspDrag := { active: false, x: 0, y: 0, w: 0, h: 0 }
global G_LfInspLoads := 0               ; article_paragraphs 发出次数（E2E 钉「展开不再发网络」）
global G_LfInspDetailIndex := -1        ; 当前在全文区展开的段号（0-based）
global G_LfInspLastResegToast := ""     ; 最近一次重新分段成功的 toast 文案（E2E 断言用）

; 设计尺寸（逻辑 px；一律经 LfInspPx 按窗口 DPI 换算）
global INSP := {
    W:             560,
    H:             640,
    MIN_W:         440,
    MIN_H:         320,
    PAD:            16,
    TOP_H:          34,
    TITLE_H:        24,     ; 合并行：标题 · N 段 / M 词 / 共 X 字 · 平均 Y 字/段
    META_H:         18,     ; 第二行：来源应用 · format · 异常计数
    DETAIL_H:       96,
    BTN_H:          30,
    BTN_W:          96,
    CLOSE_W:        64,
    LV_HEAD_H:      24,
    SUSPICIOUS_BG: "FFF3C4" ; suspicious 行背景（浅黄；E2E 数这个实色像素）
}

; 段落类型的中文标签（与取词台段落卡同一个映射口径；未知类型按正文显示）
global INSP_TYPE_LABEL := { body: "正文", heading: "标题", list: "列表", quote: "引用" }

; ============================================================================
;  几何 / 生命周期
; ============================================================================

LfInspPx(n) {
    global G_LfInspScale
    return Round(n * G_LfInspScale)
}

; config 里的几何是**逻辑 px**（换缩放/换机器位置仍然成立）；启动时只读一次。
LfInspectorGeomFromConfig() {
    global CONFIG_PATH, SECTION
    return { x: ReadIniValue(CONFIG_PATH, SECTION, "longform_inspector_x", "")
           , y: ReadIniValue(CONFIG_PATH, SECTION, "longform_inspector_y", "")
           , w: ReadIniValue(CONFIG_PATH, SECTION, "longform_inspector_w", "")
           , h: ReadIniValue(CONFIG_PATH, SECTION, "longform_inspector_h", "") }
}

; 启动 hook（主脚本 StartNormal → LongformStartup → 这里）。与面板同理：
; **只读一次**，config 热重载不重读/不应用。
LfInspectorStartup() {
    global G_LfInspGeom, G_LfInspGeomLoaded
    G_LfInspGeom := LfInspectorGeomFromConfig()
    G_LfInspGeomLoaded := true
    Log("分段预览窗：几何只在启动读一次 x=" . G_LfInspGeom.x . " y=" . G_LfInspGeom.y
      . " w=" . G_LfInspGeom.w . " h=" . G_LfInspGeom.h . "（config 热重载不重读/不应用）")
}

; 面板底栏「分段」按钮 / 面板内 Alt+Shift+P 的唯一入口。
; 仅在有 doc_id 时可用：没文章 → 静默日志 + 一枚 toast，不开空窗（spec §9）。
LfInspectorToggle() {
    global G_LongDocId, G_LfInspGui, G_LfInspDocId, G_LfInspData, G_LfInspDetailIndex
    if (!G_LongDocId) {
        Log("分段预览：还没有文章，入口短路")
        ShowToast("还没有文章，先框选一段正文", "err")
        return
    }
    if (G_LfInspGui) {
        ; 窗口已开但面板换了文章（重新框选/粘贴）→ 按新 doc_id 重载，别拿旧文数据骗人。
        if (G_LfInspDocId != G_LongDocId) {
            G_LfInspDocId := G_LongDocId
            G_LfInspData := 0
            G_LfInspDetailIndex := -1
            LfInspectorRender()
            LfInspectorLoad()
            Log("分段预览窗：文章已切换，按新 doc_id=" . G_LfInspDocId . " 重载")
        }
        ; 同一篇 → 只聚焦（不重建，避免列表滚动位置丢失）
        WinActivate("ahk_id " . G_LfInspGui.Hwnd)
        return
    }
    LfInspectorOpen()
}

LfInspectorOpen() {
    global G_LfInspGui, G_LfInspDocId, G_LfInspClosing, G_LfInspData, G_LfInspDetailIndex
    global G_LongDocId, G_LfInspLoads
    if (!G_LongDocId)
        return
    if (G_LfInspGui) {
        WinActivate("ahk_id " . G_LfInspGui.Hwnd)
        return
    }
    G_LfInspDocId := G_LongDocId
    G_LfInspData := 0
    G_LfInspDetailIndex := -1
    G_LfInspClosing := false
    LfInspectorBuild()
    LfInspectorPresent()
    LfInspectorRegisterKeys()
    LfInspectorRender()
    LfInspectorLoad()
    Log("分段预览窗已打开（doc_id=" . G_LfInspDocId . "）")
}

; `saveGeom`：用户关窗/程序退出时落盘（拖动结束的落盘在 WM_EXITSIZEMOVE）。
LfInspectorClose(saveGeom := true) {
    global G_LfInspGui, G_LfInspC, G_LfInspClosing, G_LfInspData, G_LfInspDocId
    global G_LfInspBusy, G_LfInspDetailIndex, G_LfInspSusRows, G_LfInspKeysOn
    if (G_LfInspClosing || !G_LfInspGui)
        return
    G_LfInspClosing := true
    if (saveGeom)
        LfInspectorSaveGeom()
    LfInspectorUnregisterKeys()
    try G_LfInspGui.Destroy()
    G_LfInspGui := 0
    G_LfInspC := {}
    G_LfInspData := 0
    G_LfInspDocId := 0
    G_LfInspBusy := false
    G_LfInspDetailIndex := -1
    G_LfInspSusRows := Map()
    G_LfInspClosing := false
    Log("分段预览窗已关闭")
}

; 主脚本退出 / 面板关闭时调用：落盘几何并收窗（不留下无主窗口）。
LfInspectorShutdown() {
    global G_LfInspGui
    if (!G_LfInspGui)
        return
    LfInspectorClose(true)
}

; ============================================================================
;  窗口 / 控件
; ============================================================================

LfInspectorBuild() {
    global G_LfInspGui, G_LfInspC, G_LfInspDpi, G_LfInspScale, G_LfInspSusRows
    g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +Resize", "分段预览")
    g.BackColor := "F5F6F7"
    g.MarginX := 0
    g.MarginY := 0
    G_LfInspGui := g
    ; 先 Show("Hide") 建出真窗口：GetDpiForWindow 需要 HWND
    g.Show("Hide x100 y100 w" . LfInspPx(INSP.W) . " h" . LfInspPx(INSP.H))
    G_LfInspDpi := DllCall("GetDpiForWindow", "Ptr", g.Hwnd, "UInt")
    G_LfInspScale := G_LfInspDpi / 96

    C := {}
    ; 顶栏：窗口标题 + ✕（顶栏整条可拖，见 LfInspectorOnHitTest）
    C.winTitle := g.AddText("x0 y0 w1 h1", "分段预览")
    C.winTitle.SetFont("s10 c6B7280", "Segoe UI")
    C.close := g.AddButton("x0 y0 w1 h1", "✕")
    ; 文章头（spec §9）：第一行合并成 `标题 · N 段 / M 词 / 共 X 字 · 平均 Y 字/段`
    ; （SS_ENDELLIPSIS，长标题省略），第二行才是来源应用 / format / 异常计数。
    C.head := g.AddText("x0 y0 w1 h1 +0x4000", "")   ; SS_ENDELLIPSIS
    C.head.SetFont("s12 c111827 bold", "Segoe UI")
    C.meta := g.AddText("x0 y0 w1 h1 +0x4000", "")
    C.meta.SetFont("s9 c6B7280", "Segoe UI")
    ; 段落列表：每段一行（段号 / 类型 / 字数 / 段首 60 字）；suspicious 行整行浅黄（自绘）
    ; WM_NOTIFY 自绘把 suspicious 行整行刷成浅黄（ListView 没有逐行换底色的 API）。
    ; `FullRowSelect` 不是 AddListView 的创建选项（真机报 Invalid option），用
    ; LVM_SETEXTENDEDLISTVIEWSTYLE(0x1036) + LVS_EX_FULLROWSELECT(0x20) 补。
    C.lv := g.AddListView("x0 y0 w1 h1 -Multi +Grid", ["段号", "类型", "字数", "段首（60 字）"])
    C.lv.SetFont("s10", "Segoe UI")
    DllCall("SendMessageW", "Ptr", C.lv.Hwnd, "UInt", 0x1036, "Ptr", 0x20, "Ptr", 0x20)
    ; 全文区（点击列表行展开；只读，不触发任何网络）
    C.detailLabel := g.AddText("x0 y0 w1 h1", "全文")
    C.detailLabel.SetFont("s9 c6B7280", "Segoe UI")
    C.detail := g.AddEdit("x0 y0 w1 h1 ReadOnly +Multi -Wrap +VScroll", "")
    C.detail.SetFont("s10 c111827", "Segoe UI")
    C.detail.Value := "点击上方任一段查看全文（数据已一次读完，展开不再发请求）"
    ; 底栏按钮：重新分段 / 关闭（**没有删除** —— spec §9）
    C.reseg := g.AddButton("x0 y0 w1 h1", "重新分段")
    C.closeBtn := g.AddButton("x0 y0 w1 h1", "关闭")
    C.hint := g.AddText("x0 y0 w1 h1", "Esc 关闭 · 可拖动/缩放")
    C.hint.SetFont("s9 c9CA3AF", "Segoe UI")

    ; --- 事件 ---
    C.close.OnEvent("Click", (*) => LfInspectorClose())
    C.closeBtn.OnEvent("Click", (*) => LfInspectorClose())
    C.reseg.OnEvent("Click", (*) => LfInspectorResegment())
    ; ListView 在 AHK v2.0.30 的安全事件（本机运行时实测 2026-10-31）：Click /
    ; DoubleClick / ColClick / ItemFocus / ItemExpand；ItemSelect 挂回调不报错但
    ; 事件永不触发（"Invalid callback function."，旧注释说的就是它）。点击后读焦点
    ; 行即可 —— 单击一行 = 展开全文；票 #185（A12）补键盘路径：方向键换焦点行时
    ; ItemFocus 跟着刷全文区（与 Click 同一落点），Enter 的显式展开在
    ; LfInspectorRegisterKeys（~Enter，窗口作用域）。
    C.lv.OnEvent("Click", (*) => LfInspectorShowDetail(LfInspectorFocusedRow()))
    C.lv.OnEvent("ItemFocus", (*) => LfInspectorShowDetail(LfInspectorFocusedRow()))
    g.OnEvent("Size", LfInspectorOnSize)
    g.OnEvent("Close", (*) => LfInspectorClose())
    G_LfInspC := C
    return g
}

; 窗口显示：先按记住的几何/默认 560×640 摆位（窗口 DPI 换算），再按客户区排版。
LfInspectorPresent() {
    global G_LfInspGui, G_LfInspGeom, G_LfInspC, G_LfInspScale
    g := G_LfInspGui
    wantW := (G_LfInspGeom.w != "") ? (G_LfInspGeom.w + 0) : INSP.W
    wantH := (G_LfInspGeom.h != "") ? (G_LfInspGeom.h + 0) : INSP.H
    pos := LfInspectorRestorePos()
    w := LfInspPx(wantW), h := LfInspPx(wantH)
    g.Move(pos.x, pos.y, w, h)
    dpi2 := DllCall("GetDpiForWindow", "Ptr", g.Hwnd, "UInt")
    if (dpi2 != G_LfInspDpi) {
        G_LfInspScale := dpi2 / 96
        g.Move(pos.x, pos.y, LfInspPx(wantW), LfInspPx(wantH))
    }
    LfInspectorLayout()
    if (EnvGet("LF_E2E_ACTIONS") != "")
        g.Show("NoActivate")
    else
        g.Show()
}

LfInspectorRestorePos() {
    global G_LfInspGeom, G_LfInspScale
    if (G_LfInspGeom.x = "" || G_LfInspGeom.y = "")
        return LfInspectorDefaultPos()
    x := Round((G_LfInspGeom.x + 0) * G_LfInspScale)
    y := Round((G_LfInspGeom.y + 0) * G_LfInspScale)
    ; 可见性校正：落在任何显示器之外 → 回默认位（与面板同一语义）
    onAny := false
    Loop MonitorGetCount() {
        MonitorGet(A_Index, &l, &t, &r, &b)
        if (x + LfInspPx(80) > l && x < r - LfInspPx(80) && y + LfInspPx(40) > t && y < b - LfInspPx(40))
            onAny := true
    }
    if (!onAny)
        return LfInspectorDefaultPos()
    return { x: x, y: y }
}

LfInspectorDefaultPos() {
    ; 默认放在主屏工作区中偏右（面板在左，预览窗不压面板）
    MonitorGetWorkArea(MonitorGetPrimary(), &l, &t, &r, &b)
    w := LfInspPx(INSP.W)
    x := r - w - LfInspPx(48)
    if (x < l + LfInspPx(40))
        x := l + LfInspPx(40)
    return { x: x, y: t + LfInspPx(60) }
}

LfInspectorLayout() {
    global G_LfInspGui, G_LfInspC, G_LfInspClosing
    if (!G_LfInspGui || G_LfInspClosing)
        return
    C := G_LfInspC
    sz := LfClientSize(G_LfInspGui.Hwnd)
    pad := LfInspPx(INSP.PAD)
    topH := LfInspPx(INSP.TOP_H)
    closeW := LfInspPx(46)
    C.winTitle.Move(pad, 0, Max(1, sz.w - 2 * pad - closeW), topH)
    C.close.Move(sz.w - closeW - LfInspPx(6), LfInspPx(4), closeW, topH - LfInspPx(8))
    y := topH + LfInspPx(4)
    headW := Max(LfInspPx(120), sz.w - 2 * pad)
    C.head.Move(pad, y, headW, LfInspPx(INSP.TITLE_H))
    y += LfInspPx(INSP.TITLE_H) + LfInspPx(1)
    C.meta.Move(pad, y, headW, LfInspPx(INSP.META_H))
    y += LfInspPx(INSP.META_H) + LfInspPx(6)

    btnH := LfInspPx(INSP.BTN_H)
    btnW := LfInspPx(INSP.BTN_W)
    closeBW := LfInspPx(INSP.CLOSE_W)
    bottom := sz.h - pad
    C.reseg.Move(sz.w - pad - closeBW - LfInspPx(8) - btnW, bottom - btnH, btnW, btnH)
    C.closeBtn.Move(sz.w - pad - closeBW, bottom - btnH, closeBW, btnH)
    C.hint.Move(pad, bottom - btnH + LfInspPx(8), Max(1, sz.w - 2 * pad - btnW - closeBW - LfInspPx(24)), LfInspPx(16))

    detailH := LfInspPx(INSP.DETAIL_H)
    detailTop := bottom - btnH - LfInspPx(10) - detailH
    ; 「全文」标签放在列表与全文区之间：列表底边要留出标签那 20px，否则被列表盖住
    C.detailLabel.Move(pad, detailTop - LfInspPx(19), headW, LfInspPx(15))
    C.detail.Move(pad, detailTop, headW, detailH)
    lvTop := y
    lvH := Max(LfInspPx(60), detailTop - LfInspPx(24) - lvTop)
    C.lv.Move(pad, lvTop, headW, lvH)
    ; 列宽跟着窗口走（第 4 列吃掉剩余宽度）
    colRest := Max(LfInspPx(80), headW - LfInspPx(58 + 64 + 64 + 22))
    C.lv.ModifyCol(1, LfInspPx(58))
    C.lv.ModifyCol(2, LfInspPx(64))
    C.lv.ModifyCol(3, LfInspPx(64))
    C.lv.ModifyCol(4, colRest)
}

LfInspectorOnSize(g, minMax, w, h) {
    global G_LfInspGui, G_LfInspClosing
    if (G_LfInspClosing || !G_LfInspGui || minMax = -1)
        return
    LfInspectorLayout()
}

; ============================================================================
;  数据：一次拉全篇 → 内存；展开/选择零网络
; ============================================================================

LfInspectorLoad() {
    global G_LfInspDocId, G_LfInspLoads
    if (!G_LfInspDocId)
        return
    req := '{"cmd":"article_paragraphs","doc_id":' . G_LfInspDocId . '}'
    if (!HubSend(req, "lf_paragraphs")) {
        ShowToast("数据中枢不在位，分段预览暂时不可用", "err")
        LfInspectorSetHeadText("数据中枢不在位 · 分段预览不可用")
        return
    }
    G_LfInspLoads += 1
    LfInspectorSetHeadText("加载中…")
}

; RPC 回包（OnHubLine kind=lf_paragraphs）。解析 newsletters：AHK 没有 JSON 库，
; 但这里只认 P2a 的扁平段落对象；字符串里的 `{`/`}`/引号由 LfInspScanArray 按
; 字符串状态跳过 —— 正文含花括号也不会切错（比正则 `\{[^{}]*\}` 稳）。
LfOnInspectorParagraphs(line) {
    global G_LfInspData, G_LfInspGui, G_LfInspDocId
    if (!JsonBool(line, "ok")) {
        err := JsonField(line, "error")
        LfInspectorSetHeadText("加载失败：" . (err = "" ? "unknown" : err))
        if (err = "not found")
            ShowToast("文章不存在（可能已被删除）", "err")
        else
            ShowToast("分段数据加载失败：" . err, "err")
        return
    }
    ; 归属门：迟到的 / 换篇之后的回包一律丢弃（新 hub 的 additive `doc_id`；
    ; 旧 hub 没有这个字段时跳过校验 —— 回包结构里没有别的方式认领）。
    if (InStr(line, "doc_id") > 0 && JsonNumber(line, "doc_id") != G_LfInspDocId) {
        Log("分段预览：丢弃过期回包（doc_id=" . JsonNumber(line, "doc_id")
          . " ≠ 当前 " . G_LfInspDocId . "）")
        return
    }
    body := LfInspJsonArrayText(line, "paragraphs")
    objs := LfInspSplitObjects(body)
    paragraphs := []
    for obj in objs {
        index := JsonNumber(obj, "index")
        typ := JsonField(obj, "type")
        chars := JsonNumber(obj, "chars")
        text := JsonField(obj, "text")
        suspicious := JsonBool(obj, "suspicious")
        recs := LfInspSplitObjects(LfInspJsonArrayText(obj, "records"))
        paragraphs.Push({ index: index, type: typ, chars: chars, text: text
                         , suspicious: suspicious, records: recs.Length })
    }
    statsText := LfInspJsonObjectText(line, "stats")
    ; records_total 是 P2b review 的 **additive** 字段（= 已定位 + 待复核）；
    ; 旧 hub 没有它 → 回落「段落内 records 之和」（旧口径，不报错）。
    stats := { segments: JsonNumber(statsText, "segments")
             , chars: JsonNumber(statsText, "chars")
             , avg_chars: JsonNumber(statsText, "avg_chars")
             , records_total: JsonNumber(statsText, "records_total")
             , has_records_total: InStr(statsText, "records_total") > 0 }
    G_LfInspData := { paragraphs: paragraphs, stats: stats }
    LfInspectorRender()
    Log("分段预览：doc_id=" . G_LfInspDocId . " 收到 " . paragraphs.Length . " 段（一次拉全篇）")
}

LfInspectorRender() {
    global G_LfInspC, G_LfInspData, G_LfInspSusRows
    C := G_LfInspC
    if (!C.HasProp("lv"))
        return
    d := G_LfInspData
    C.lv.Delete()
    G_LfInspSusRows := Map()
    if (!IsObject(d)) {
        LfInspectorSetHeadText("加载中…")
        return
    }
    stats := d.stats
    C.head.Text := LfInspectorHeadLine(stats, d.paragraphs)
    suspicious := 0
    row := 0
    for p in d.paragraphs {
        isSus := p.suspicious ? true : false
        if (isSus) {
            suspicious += 1
            G_LfInspSusRows[row] := true      ; WM_NOTIFY 自绘按 0-based 行号上浅黄
        }
        typeLabel := LfInspTypeLabel(p.type)
        preview := SubStr(NormalizeLine(p.text), 1, 60)
        C.lv.Add("", "#" . (p.index + 1), typeLabel, p.chars . " 字", preview)
        row += 1
    }
    C.meta.Text := LfInspectorMetaText(suspicious)
    C.detail.Value := "点击上方任一段查看全文（数据已一次读完，展开不再发请求）"
    C.reseg.Enabled := true
}

LfInspectorSetHeadText(text) {
    global G_LfInspC
    if (G_LfInspC.HasProp("head"))
        G_LfInspC.head.Text := text
}

; M 词口径 = 已定位 + 待复核（与取词台文章页 `articleTotalWords` 一致）。
; hub 的 `stats.records_total` 是 additive 的权威值；旧回包没有该字段时回落旧口径。
LfInspectorWordCount(stats, paragraphs) {
    if (stats.has_records_total)
        return stats.records_total
    words := 0
    for p in paragraphs
        words += p.records
    return words
}

; spec §9 的顶部合并行：`标题 · N 段 / M 词 / 共 X 字 · 平均 Y 字/段`
LfInspectorHeadLine(stats, paragraphs) {
    words := LfInspectorWordCount(stats, paragraphs)
    return LfInspectorArticleTitle() . " · " . stats.segments . " 段 / " . words . " 词 / 共 "
        . stats.chars . " 字 · 平均 " . stats.avg_chars . " 字/段"
}

LfInspectorArticleTitle() {
    global G_LfSourceTitle, G_LfInspDocId
    t := NormalizeLine(G_LfSourceTitle)
    if (t = "")
        t := "文章 #" . G_LfInspDocId
    return t
}

LfInspectorMetaText(suspicious := 0) {
    global G_LongApp, G_LongArticleFormat
    app := (G_LongApp = "") ? "未知来源" : G_LongApp
    fmt := (G_LongArticleFormat = "ocr") ? "OCR" : "粘贴"
    text := "来源 " . app . " · " . fmt
    if (suspicious > 0)
        text .= " · 异常 " . suspicious . " 段（黄色标记）"
    return text
}

LfInspTypeLabel(typ) {
    global INSP_TYPE_LABEL
    return INSP_TYPE_LABEL.HasProp(typ) ? INSP_TYPE_LABEL.%typ% : INSP_TYPE_LABEL.body
}

; 点击/选择一段 → 下方只读全文区展开。**只读内存，不发网络**（spec §9）。
LfInspectorFocusedRow() {
    global G_LfInspC
    if (!G_LfInspC.HasProp("lv"))
        return 0
    row := G_LfInspC.lv.GetNext(0, "F")      ; 点击后焦点行 = 刚点的那一行
    if (row < 1)
        row := G_LfInspC.lv.GetNext(0)       ; 键盘选中态兜底
    return row
}

LfInspectorShowDetail(item) {
    global G_LfInspC, G_LfInspData, G_LfInspDetailIndex
    if (!IsObject(G_LfInspData) || item < 1 || item > G_LfInspData.paragraphs.Length)
        return
    p := G_LfInspData.paragraphs[item]
    G_LfInspDetailIndex := p.index
    G_LfInspC.detail.Value := "#" . (p.index + 1) . " · " . LfInspTypeLabel(p.type)
        . " · " . p.chars . " 字" . (p.suspicious ? " · 异常段" : "") . "`r`n" . p.text
}

; ============================================================================
;  重新分段（`article_resegment`）
; ============================================================================

LfInspectorResegment() {
    global G_LfInspBusy, G_LfInspDocId, G_LfInspC
    if (G_LfInspBusy || !G_LfInspDocId)
        return
    G_LfInspBusy := true
    C := G_LfInspC
    C.reseg.Enabled := false
    C.reseg.Text := "重新分段中…"
    req := '{"cmd":"article_resegment","doc_id":' . G_LfInspDocId . '}'
    if (!HubSend(req, "lf_resegment")) {
        LfInspectorResegmentDone()
        ShowToast("数据中枢不在位，无法重新分段", "err")
        return
    }
    Log("分段预览：重新分段已发出 doc_id=" . G_LfInspDocId)
}

; RPC 回包（OnHubLine kind=lf_resegment）
LfOnInspectorResegment(line) {
    global G_LfInspDocId, G_LfInspLastResegToast
    LfInspectorResegmentDone()
    if (!JsonBool(line, "ok")) {
        err := JsonField(line, "error")
        if (err = "no segmentation source") {
            ; spec §9/§14：旧 v21 文章没有原始分段输入 —— 如实提示，不伪造
            ShowToast("该文无原始分段输入，请重新录入", "err")
        } else if (err = "bad segmentation source") {
            ShowToast("原始分段输入损坏，无法重新分段", "err")
        } else if (err = "not found") {
            ShowToast("文章不存在（可能已被删除）", "err")
        } else {
            ShowToast("重新分段失败：" . err, "err")
        }
        return
    }
    if (JsonNumber(line, "doc_id") != G_LfInspDocId)
        return
    segments := JsonNumber(line, "segments")
    remapped := JsonNumber(line, "remapped")
    review := JsonNumber(line, "needs_review")
    G_LfInspLastResegToast := segments . " 段 · 重映射 " . remapped . " 条 · 待复核 " . review . " 条"
    ShowToast(G_LfInspLastResegToast, "ok")
    ; 回包后刷新列表（重新分段是本窗唯一会在成功后重新拉数据的路径）
    LfInspectorLoad()
    Log("分段预览：重新分段完成 " . segments . " 段 / 重映射 " . remapped
      . " / 待复核 " . review)
}

LfInspectorResegmentDone() {
    global G_LfInspBusy, G_LfInspC
    G_LfInspBusy := false
    if (G_LfInspC.HasProp("reseg")) {
        G_LfInspC.reseg.Enabled := true
        G_LfInspC.reseg.Text := "重新分段"
    }
}

; ============================================================================
;  几何落盘 / 窗口消息 / 键位
; ============================================================================

LfInspectorSaveGeom() {
    global G_LfInspGui, G_LfInspScale, G_LfInspGeom
    if (!G_LfInspGui)
        return
    r := LfWindowRect(G_LfInspGui.Hwnd)
    lx := Round(r.x / G_LfInspScale), ly := Round(r.y / G_LfInspScale)
    lw := Round(r.w / G_LfInspScale), lh := Round(r.h / G_LfInspScale)
    SaveConfigKeys(Map("longform_inspector_x", "" . lx, "longform_inspector_y", "" . ly
                     , "longform_inspector_w", "" . lw, "longform_inspector_h", "" . lh)
                 , "分段预览窗几何")
    G_LfInspGeom := { x: "" . lx, y: "" . ly, w: "" . lw, h: "" . lh }
    Log("分段预览窗：几何已记住 x=" . lx . " y=" . ly . " w=" . lw . " h=" . lh . "（逻辑 px）")
}

LfInspectorOnHitTest(wp, lp, msg, hwnd) {
    global G_LfInspGui
    if (!G_LfInspGui || hwnd != G_LfInspGui.Hwnd)
        return
    p := LfScreenPoint(lp)
    pt := Buffer(8, 0)
    NumPut("Int", p.x, pt, 0), NumPut("Int", p.y, pt, 4)
    DllCall("ScreenToClient", "Ptr", hwnd, "Ptr", pt)
    y := NumGet(pt, 4, "Int")
    ; 顶栏整条 = HTCAPTION 可拖；四边交给 DefWindowProc（WS_THICKFRAME 缩放）
    if (y >= 0 && y < LfInspPx(INSP.TOP_H))
        return 2
    return
}

LfInspectorOnGetMinMaxInfo(wp, lp, msg, hwnd) {
    global G_LfInspGui
    if (!G_LfInspGui || hwnd != G_LfInspGui.Hwnd)
        return
    NumPut("Int", LfInspPx(INSP.MIN_W), lp, 24)
    NumPut("Int", LfInspPx(INSP.MIN_H), lp, 28)
}

LfInspectorOnEnterSizeMove(wp, lp, msg, hwnd) {
    global G_LfInspGui, G_LfInspDrag
    if (!G_LfInspGui || hwnd != G_LfInspGui.Hwnd)
        return
    G_LfInspDrag.active := true
    r := LfWindowRect(hwnd)
    G_LfInspDrag.x := r.x, G_LfInspDrag.y := r.y, G_LfInspDrag.w := r.w, G_LfInspDrag.h := r.h
}

LfInspectorOnExitSizeMove(wp, lp, msg, hwnd) {
    global G_LfInspGui, G_LfInspDrag
    if (!G_LfInspGui || hwnd != G_LfInspGui.Hwnd)
        return
    was := G_LfInspDrag.active
    G_LfInspDrag.active := false
    r := LfWindowRect(hwnd)
    ; 与面板同一纪律：位置尺寸真的变了才写盘（单击顶栏也会走这对消息）
    if (was && r.x = G_LfInspDrag.x && r.y = G_LfInspDrag.y && r.w = G_LfInspDrag.w && r.h = G_LfInspDrag.h)
        return
    LfInspectorSaveGeom()
}

; 预览窗内的键：Esc（关窗）+ Enter（展开当前焦点行，票 #185 / A12 的键盘路径）。
; 键位是窗口作用域，关窗必须注销。
LfInspectorRegisterKeys() {
    global G_LfInspGui, G_LfInspKeysOn
    if (G_LfInspKeysOn || !G_LfInspGui)
        return
    HotIfWinActive("ahk_id " . G_LfInspGui.Hwnd)
    try {
        Hotkey("Escape", (*) => LfInspectorClose(), "On")
        ; 票 #185（A12）：Enter = 展开当前焦点行（详情只读内存，不发网络）。
        ; ~ 前缀不吞键：焦点在只读全文区时 Enter 照常无副作用；与面板的 "Enter"
        ; 热键是两条变体（各自的 HotIf 上下文），互不影响。
        Hotkey("~Enter", (*) => LfInspectorShowDetail(LfInspectorFocusedRow()), "On")
        G_LfInspKeysOn := true
    } catch as err {
        Log("分段预览窗：键位挂不上（不影响 ✕ 关闭）：" . err.Message)
    }
    HotIf()
}

LfInspectorUnregisterKeys() {
    global G_LfInspGui, G_LfInspKeysOn
    if (!G_LfInspKeysOn)
        return
    ; 票 #185（A12）：Off 必须在**同一条 HotIf 上下文**里做 —— 本机实测（AHK
    ; v2.0.30）不带上下文的 `Hotkey(name, "Off")` 报 "Nonexistent hotkey variant
    ; (HotIf)"，旧写法是 try 吞掉的无操作（变体其实一直挂着，只是窗口销毁后永远
    ; 不会触发）。现在两条都真注销；调用点在 G_LfInspGui.Destroy() 之前，HWND 还有值。
    if (G_LfInspGui) {
        HotIfWinActive("ahk_id " . G_LfInspGui.Hwnd)
        try Hotkey("Escape", "Off")
        try Hotkey("~Enter", "Off")
        HotIf()
    }
    G_LfInspKeysOn := false
}

; ============================================================================
;  suspicious 标黄：ListView 的 WM_NOTIFY 自定义绘制（NM_CUSTOMDRAW）
;  ListView 没有「逐行换底色」的公开接口；图标方案在真机上报废（图标吃列宽/不显示）。
;  这里处理 CDDS_ITEMPREPAINT（含 subitem）：命中行把 clrTextBk 设成浅黄，其余走默认。
; ============================================================================

LfInspectorOnNotify(wp, lp, msg, hwnd) {
    global G_LfInspGui, G_LfInspC, G_LfInspSusRows, INSP
    if (!G_LfInspGui || hwnd != G_LfInspGui.Hwnd)
        return
    if (!G_LfInspC.HasProp("lv"))
        return
    ; NMHDR：hwndFrom(0) / idFrom(8) / code(16)。只认 ListView 的 NM_CUSTOMDRAW(-12)。
    if (NumGet(lp, 0, "Ptr") != G_LfInspC.lv.Hwnd)
        return
    if (NumGet(lp, 16, "Int") != -12)
        return
    stage := NumGet(lp, 24, "UInt")          ; NMCUSTOMDRAW.dwDrawStage
    if (stage = 1)                            ; CDDS_PREPAINT
        return 0x20                           ; CDRF_NOTIFYITEMDRAW
    ; CDDS_ITEMPREPAINT(0x10001) / CDDS_SUBITEM|CDDS_ITEMPREPAINT(0x30001)
    if ((stage & 0x00010001) != 0x00010001)
        return
    row := NumGet(lp, 56, "UPtr")            ; dwItemSpec = 行号（0-based）
    if (G_LfInspSusRows.Has(row)) {
        ; NMLVCUSTOMDRAW：clrText(80) / clrTextBk(84)；COLORREF = 0x00BBGGRR（LfRef 口径）
        NumPut("UInt", LfRef(INSP.SUSPICIOUS_BG), lp, 84)
    }
    return 0                                  ; CDRF_DODEFAULT
}

; ============================================================================
;  JSON 切片（只服务 P2a 的段落回包；字符串内的引号/花括号由状态机跳过）
; ============================================================================

; 取 `"key":[...]` 的**数组内部文本**（不含方括号）；找不到 → ""。
LfInspJsonArrayText(json, key) {
    if (!RegExMatch(json, '"' . key . '"\s*:\s*\[', &m))
        return ""
    start := m.Pos(0) + m.Len(0) - 1                ; 指向 '['
    depth := 0, inStr := false, esc := false, i := start
    total := StrLen(json)
    while (i <= total) {
        ch := SubStr(json, i, 1)
        if (inStr) {
            if (esc)
                esc := false
            else if (ch = "\")
                esc := true
            else if (ch = '"')
                inStr := false
        } else {
            if (ch = '"')
                inStr := true
            else if (ch = "[")
                depth += 1
            else if (ch = "]") {
                depth -= 1
                if (depth = 0)
                    return SubStr(json, start + 1, i - start - 1)
            }
        }
        i += 1
    }
    return ""
}

; 取 `"key":{...}` 的**对象内部文本**（不含花括号）；找不到 → ""。
LfInspJsonObjectText(json, key) {
    if (!RegExMatch(json, '"' . key . '"\s*:\s*\{', &m))
        return ""
    start := m.Pos(0) + m.Len(0) - 1                ; 指向 '{'
    depth := 0, inStr := false, esc := false, i := start
    total := StrLen(json)
    while (i <= total) {
        ch := SubStr(json, i, 1)
        if (inStr) {
            if (esc)
                esc := false
            else if (ch = "\")
                esc := true
            else if (ch = '"')
                inStr := false
        } else {
            if (ch = '"')
                inStr := true
            else if (ch = "{")
                depth += 1
            else if (ch = "}") {
                depth -= 1
                if (depth = 0)
                    return SubStr(json, start + 1, i - start - 1)
            }
        }
        i += 1
    }
    return ""
}

; 把数组内部文本按**顶层对象**切开（字符串里的花括号不算）。返回对象原文数组。
LfInspSplitObjects(arrText) {
    objs := []
    depth := 0, inStr := false, esc := false, start := 0, i := 1
    total := StrLen(arrText)
    while (i <= total) {
        ch := SubStr(arrText, i, 1)
        if (inStr) {
            if (esc)
                esc := false
            else if (ch = "\")
                esc := true
            else if (ch = '"')
                inStr := false
        } else {
            if (ch = '"')
                inStr := true
            else if (ch = "{") {
                if (depth = 0)
                    start := i
                depth += 1
            } else if (ch = "}") {
                depth -= 1
                if (depth = 0 && start > 0) {
                    objs.Push(SubStr(arrText, start, i - start + 1))
                    start := 0
                }
            }
        }
        i += 1
    }
    return objs
}

; 中枢掉线（HubDown 的 kind 队列里带 lf_paragraphs/lf_resegment）：在途回包不会来了，
; 只让预览窗降级（不清用户数据、不判死内核），按钮复位避免永远停在「重新分段中…」。
LfInspectorOnHubDown() {
    global G_LfInspGui
    if (!G_LfInspGui)
        return
    LfInspectorResegmentDone()
    LfInspectorSetHeadText("数据中枢不在位 · 分段预览不可用")
    Log("分段预览窗：中枢不在位，本次请求作废")
}

; ============================================================================
;  E2E（只在副本进程的 LF_E2E_ACTIONS 门后由 LongformPanel 的 LfE2ERun 调用）
; ============================================================================

; 把预览窗的可断言状态落盘：标题/统计/行数/类型/标黄段/按钮/几何 + 黄色像素数。
LfInspectorE2EInfo(path) {
    global G_LfInspGui, G_LfInspC, G_LfInspData, G_LfInspDocId, G_LfInspBusy
    global G_LfInspLoads, G_LfInspDetailIndex, INSP, G_LfInspLastResegToast
    lines := []
    lines.Push("open=" . (G_LfInspGui ? 1 : 0))
    if (!G_LfInspGui) {
        LfWriteLines(path, lines)
        return
    }
    C := G_LfInspC
    lines.Push("doc_id=" . G_LfInspDocId)
    lines.Push("head=" . C.head.Text)
    lines.Push("meta=" . C.meta.Text)
    lines.Push("hint=" . C.hint.Text)
    lines.Push("rows=" . (IsObject(G_LfInspData) ? G_LfInspData.paragraphs.Length : 0))
    lines.Push("lv_rows=" . C.lv.GetCount())
    lines.Push("loads=" . G_LfInspLoads)
    lines.Push("busy=" . (G_LfInspBusy ? 1 : 0))
    lines.Push("reseg_text=" . C.reseg.Text)
    lines.Push("reseg_enabled=" . (C.reseg.Enabled ? 1 : 0))
    lines.Push("buttons=" . C.reseg.Text . "|" . C.closeBtn.Text)
    lines.Push("detail_index=" . G_LfInspDetailIndex)
    lines.Push("detail_chars=" . StrLen(C.detail.Value))
    lines.Push("detail_value=" . SubStr(StrReplace(C.detail.Value, "`r`n", " / "), 1, 120))
    lines.Push("last_reseg_toast=" . G_LfInspLastResegToast)
    previews := "", types := "", sus := ""
    if (IsObject(G_LfInspData)) {
        for p in G_LfInspData.paragraphs {
            types .= (types = "" ? "" : ",") . p.type
            if (p.suspicious)
                sus .= (sus = "" ? "" : ",") . p.index
            if (previews = "")
                previews := SubStr(NormalizeLine(p.text), 1, 60)
        }
    }
    lines.Push("types=" . types)
    lines.Push("suspicious_indices=" . sus)
    lines.Push("first_preview=" . previews)
    ; 黄色像素：整窗截图里数 suspicious 行背景实色（容差 8）—— 标黄的像素级证据。
    ; 一个 10 行列表里只有 1 行异常 → 黄色面积应远小于「所有行都黄」。
    r := LfWindowRect(G_LfInspGui.Hwnd)
    img := LfGrabRect(r.x, r.y, r.w, r.h)
    yellow := Integer("0x" . INSP.SUSPICIOUS_BG)
    lines.Push("yellow_px=" . (img ? LfCountColor(img, yellow, 8) : -1))
    lines.Push("win=" . r.x . "," . r.y . "," . r.w . "," . r.h)
    LfWriteLines(path, lines)
}

; 程序化选中第 n 行（1-based）并展开全文（E2E 用；展开本身不发网络）。
LfInspectorE2ESelect(n) {
    global G_LfInspC
    if (!G_LfInspGui || !G_LfInspC.HasProp("lv"))
        return
    G_LfInspC.lv.Modify(n, "Select")
    LfInspectorShowDetail(n)
}

; 预览窗截图（只抓窗口矩形，不带桌面）。
LfInspectorE2EShot(path) {
    global G_LfInspGui
    if (!G_LfInspGui)
        return false
    r := LfWindowRect(G_LfInspGui.Hwnd)
    return LfShotRect(path, r.x, r.y, r.w, r.h)
}

; Esc 关窗 E2E：走真实键位（HotIfWinActive + Hotkey）而不是直接调 LfInspectorClose。
LfInspectorE2EEscape() {
    global G_LfInspGui
    if (!G_LfInspGui)
        return false
    try WinActivate("ahk_id " . G_LfInspGui.Hwnd)
    Sleep(150)
    ; OS 级注入（Send 默认不触发本进程热键）：VK_ESCAPE = 0x1B
    DllCall("user32\keybd_event", "UChar", 0x1B, "UChar", 0, "UInt", 0, "UPtr", 0)
    Sleep(60)
    DllCall("user32\keybd_event", "UChar", 0x1B, "UChar", 0, "UInt", 2, "UPtr", 0)
    Sleep(300)
    return !G_LfInspGui
}

; ============================================================================
;  消息钩子（模块级只挂一次，与面板同一纪律：重建窗口不重复挂）
; ============================================================================
OnMessage(0x004E, LfInspectorOnNotify)        ; WM_NOTIFY（ListView suspicious 行底色）
OnMessage(0x0084, LfInspectorOnHitTest)       ; WM_NCHITTEST
OnMessage(0x0231, LfInspectorOnEnterSizeMove) ; WM_ENTERSIZEMOVE
OnMessage(0x0232, LfInspectorOnExitSizeMove)  ; WM_EXITSIZEMOVE（几何落盘点）
OnMessage(0x0024, LfInspectorOnGetMinMaxInfo) ; WM_GETMINMAXINFO
