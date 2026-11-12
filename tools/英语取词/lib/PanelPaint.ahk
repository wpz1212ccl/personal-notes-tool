#Requires AutoHotkey v2.0

; ============================================================================
;  lib/PanelPaint.ahk —— 半透明面板的公共绘制件（v36 票 #243 / 决策 9）
;
;  v35-ux D1/D2/D3 的气泡 / 信息流 / 菜单三个逐像素分层窗共用同一套绘制件：
;    · PetPanelFont       —— Segoe UI 字体（pt → CreateFontW 参数）；
;    · PetPanelFixAlpha   —— 整幅 alpha 修补（GDI 画完 → 预乘 BGRA）；
;    · PetPanelFixAlphaRect —— 只修补一个矩形窗口（hover 行增量重绘用）。
;
;  为什么抽出来：v35 时这段逐像素循环挂在 PetBubble.ahk 里被另外两个模块跨
;  文件调用（菜单/信息流都依赖气泡模块），票 #243 把它收成一份独立实现，并且
;  补上「矩形窗口」入口 —— 菜单 hover 只重绘变化的那一行，不必再付整幅成本。
;
;  纪律（与 ADR-0016 一致）：32bpp BI_RGB DIB 上 GDI 不写 alpha 字节，所以 alpha
;  只能在这里逐像素补；不许整窗 alpha（WinSetTransparent）、不许 GDI 缩放
;  （StretchBlt 会把 alpha 清零）。
; ============================================================================

; Segoe UI 字体（pt → 像素高的 CreateFontW 参数；负高 = 字符高度）。
PetPanelFont(pt) {
    return DllCall("gdi32\CreateFontW", "Int", -Round(pt * D * 96 / 72)
                  , "Int", 0, "Int", 0, "Int", 0, "Int", 400, "UInt", 0, "UInt", 0
                  , "UInt", 0, "UInt", 1, "UInt", 0, "UInt", 0, "UInt", 0
                  , "UInt", 0, "Str", "Segoe UI", "Ptr")
}

; GDI 画完后的 alpha 修补（32bpp BI_RGB DIB → 预乘 BGRA）：
;   · 没画过东西的像素（BGR 全 0）→ 保持 0 = 全透明；
;   · max 通道 ≥ textMin 的像素按**文字**处理：alpha 255（预乘 = 原色，字不随面板变淡）；
;   · 其余（面板/描边）→ 预乘 panelAlpha（真半透明；面板色 #1B2128 / 边 #45525D
;     的 max 通道都远低于阈值，不会被误判成文字；文字抗锯齿的中间调按亮度归边）。
PetPanelFixAlpha(bits, w, h, panelAlpha, textMin) {
    PetPanelFixAlphaRect(bits, w, h, 0, 0, w - 1, h - 1, panelAlpha, textMin)
}

; 只修补 [x0..x1] × [y0..y1]（含端点）这块矩形：按整像素 32 位读、按整行推进，
; 不做逐通道寻址；命中不了这块的像素一个字节都不碰（增量重绘的正确性前提）。
PetPanelFixAlphaRect(bits, w, h, x0, y0, x1, y1, panelAlpha, textMin) {
    if (x0 < 0)
        x0 := 0
    if (y0 < 0)
        y0 := 0
    if (x1 > w - 1)
        x1 := w - 1
    if (y1 > h - 1)
        y1 := h - 1
    if (x1 < x0 || y1 < y0)
        return
    rowBytes := w * 4
    bandBytes := (x1 - x0 + 1) * 4
    y := y0
    while (y <= y1) {
        off := y * rowBytes + x0 * 4
        end := off + bandBytes
        while (off < end) {
            c := NumGet(bits, off, "UInt") & 0x00FFFFFF
            if (c) {
                mx := (c & 0xFF) > ((c >> 8) & 0xFF) ? (c & 0xFF) : ((c >> 8) & 0xFF)
                mx := ((c >> 16) & 0xFF) > mx ? ((c >> 16) & 0xFF) : mx
                if (mx >= textMin) {
                    NumPut("UInt", c | 0xFF000000, bits, off)
                } else {
                    b := (c & 0xFF) * panelAlpha // 255
                    g := ((c >> 8) & 0xFF) * panelAlpha // 255
                    r := ((c >> 16) & 0xFF) * panelAlpha // 255
                    NumPut("UInt", b | (g << 8) | (r << 16) | (panelAlpha << 24), bits, off)
                }
            }
            off += 4
        }
        y += 1
    }
}
