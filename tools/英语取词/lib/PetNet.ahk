#Requires AutoHotkey v2.0

; ============================================================================
;  lib/PetNet.ahk —— 桌宠点击链路的异步 HTTP 小件（v36 票 #243 / 决策 8）
;
;  为什么要它：v35 的菜单在**建窗之前**串行跑两条同步 WinHttp（最坏 3500ms 全在
;  UI 线程），用户看到的是「点了没反应，过一会儿才弹」。决策 8 把「打开菜单」定
;  为 UI 动作：先渲染，线上数据后补 —— 同步请求做不到「立刻返回」，所以这里用
;  WinHttp 的**异步模式**（Open(..., true)）+ 20ms 定时轮询 WaitForResponse(0)：
;  Send 立刻返回，回包到达时才回调，UI 线程一帧都不等。
;
;  超时一律走 PetNetTimeoutMs()（Logic.ahk 的纯函数，上限 300ms）：即便回包永远
;  不来，也只是一次 ≤300ms 的窗口期，不会再阻塞数秒。
;
;  回调签名：onDone(text)（text = 响应正文；失败/超时/非 200 = ""）。协议解析不在
;  这里：解析是纯函数的事（PetBacklogCount / PetMenuMediaPausedFromSettings）。
;  过期回包由调用方按代次号丢弃（PetMenuNetFresh）。
; ============================================================================

global G_PetNetReqs := []      ; 在跑/在等的请求 { req, onDone, timer, at, timeoutMs }

; 发一条异步 GET 就返回（不发结果）；到达/失败都只回调一次。
PetNetGetAsync(url, onDone, timeoutMs := 0) {
    global G_PetNetReqs
    token := { req: 0, onDone: onDone, timer: 0, at: A_TickCount, timeoutMs: 0 }
    try {
        req := ComObject("WinHttp.WinHttpRequest.5.1")
        req.Open("GET", url, true)              ; 第 3 参数 true = 异步
        t := PetNetTimeoutMs(timeoutMs)
        req.SetTimeouts(t, t, t, t)
        token.req := req
        token.timeoutMs := t
        G_PetNetReqs.Push(token)
        req.Send()                              ; 异步：立刻返回
    } catch as err {
        ; 发不出去也要留一行：否则「标签从不更正」在现场无从判断（决策 12）。
        try Log("桌宠：异步请求发不出去 " . err.Message)
        PetNetDeliver(token, "")
        return
    }
    token.timer := PetNetPoll.Bind(token)       ; 存住这个对象，才能用同一对象停表
    SetTimer(token.timer, 20)
}

; 轮询：就绪 → 收正文并收尾；没就绪 → 等下一拍（兜底超时也不会永久轮询）。
PetNetPoll(token) {
    global G_PetNetReqs
    ready := false
    try {
        ready := token.req.WaitForResponse(0)
    } catch {
        ready := true                           ; 连不上/已中止 → 当空回包收尾
    }
    if (!ready) {
        if (A_TickCount - token.at > token.timeoutMs * 4 + 200)
            PetNetDeliver(token, "")
        return
    }
    text := ""
    try {
        if (token.req.Status = 200)
            text := token.req.ResponseText
    } catch {
        text := ""
    }
    PetNetDeliver(token, text)
}

; 收尾：停表、出队、回调一次（拿不到就回调空串，让标签退化成已知值）。
PetNetDeliver(token, text) {
    global G_PetNetReqs
    if (token.timer) {
        try SetTimer(token.timer, 0)
        token.timer := 0
    }
    for i, t in G_PetNetReqs {
        if (t = token) {
            G_PetNetReqs.RemoveAt(i)
            break
        }
    }
    cb := token.onDone
    token.onDone := 0
    if (cb)
        try cb(text)
}
