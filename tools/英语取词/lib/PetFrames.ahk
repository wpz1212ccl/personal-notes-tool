; ============================================================================
;  lib/PetFrames.ahk —— v35 #232：桌宠帧数据模型与调度（纯函数）
;
;  职责（只做三件事，全部可在 tests/test_logic.ahk 片 47 断言）：
;    1. 解析烘焙产物 manifest（`v35.pet.frames/1`，键名契约见主 agent D-L7 /
;       spec §素材与烘焙）：motions + expressions 两组 clip，每 clip =
;       {dir, filePattern, frames, fps, loop}；
;    2. 帧调度：给定 clip 与已播时间（毫秒）→ 该播第几帧；循环 clip 取模，
;       一次性 clip 播完停末帧；LRU 缓存顺序（只影响加载，不影响帧序）；
;    3. 把 `StatusVisual()` 的输出映射成 clip 选择：脸 = expression clip，
;       一次性动作 = motion clip，动作播完回脸。
;
;  **不建窗、不读盘、不判相位、不碰全局 GUI 状态** —— 窗口与帧缓存由
;  `lib/PetWindow.ahk` 负责。这样帧调度不用起窗口就能测。
;
;  素材来源：https://github.com/Andersen216/dsh-whale-girl-live2d
;    代码 MIT / 模型素材 CC BY-NC-SA 4.0；作者 上善无形 / ZipZipPipe / 氵六青。
;    帧由 tools/pet_bake/ 离线烘焙，产物落 data/pet/（不进 git）。
; ============================================================================

; 帧缓存上限（spec §素材与烘焙：LRU 上限约 24 帧）
global PET_FRAME_CACHE_CAP := 24

; ---------------------------------------------------------------------------
;  manifest 解析（窄 JSON：只认固定结构；仓库不引 JSON 依赖）
; ---------------------------------------------------------------------------

; 取对象里第一个同名数值字段。manifest 里同名字段只在固定位置出现，
; 用「先截区块再取字段」避免跨段误匹配。
PetJsonNumber(text, key, def := 0) {
    if RegExMatch(text, '"' key '"\s*:\s*(-?\d+(?:\.\d+)?)', &m)
        return m[1] + 0
    return def
}

PetJsonString(text, key, def := "") {
    if RegExMatch(text, '"' key '"\s*:\s*"([^"]*)"', &m)
        return m[1]
    return def
}

; 取一个 section（`"motions": { … }`）的原文区间：从 section 名之后到下一个 section 名之前。
PetSectionText(text, name, nextName := "") {
    i := InStr(text, '"' . name . '"')
    if (!i)
        return ""
    rest := SubStr(text, i)
    if (nextName != "") {
        j := InStr(rest, '"' . nextName . '"', , 2)
        if (j)
            return SubStr(rest, 1, j - 1)
    }
    return rest
}

; 解析一个 section 里的所有 clip：`"key": { "dir": …, "frames": N, "fps": N, "loop": bool }`
; 入参是 section 原文；先跳过 section 自己的 `{`，否则第一条匹配会把 section 名当 clip。
PetParseClips(section) {
    clips := Map()
    pos := 1
    i := InStr(section, "{")
    if (i)
        pos := i + 1
    while RegExMatch(section, '"([A-Za-z_]\w*)"\s*:\s*\{([^}]*)\}', &m, pos) {
        body := m[2]
        clips[m[1]] := {
            dir: PetJsonString(body, "dir", m[1])
          , filePattern: PetJsonString(body, "filePattern", "%04d.bgra")
          , frames: Round(PetJsonNumber(body, "frames", 0))
          , fps: Round(PetJsonNumber(body, "fps", 0))
          , loop: RegExMatch(body, '"loop"\s*:\s*false') ? false : true
          , missing: InStr(body, '"missing"\s*:\s*true') ? true : false
        }
        pos := m.Pos + m.Len
    }
    return clips
}

; manifest 文本 → { canvasW, canvasH, stride, frameBytes, motions: Map, expressions: Map }
; 解析失败（格式不认识）返回 0（调用方据此降级，不崩外壳）。
PetFramesParse(text) {
    if (Type(text) != "String" || InStr(text, "v35.pet.frames/1") = 0)
        return 0
    canvas := PetSectionText(text, "canvas", "stride")
    man := {
        canvasW: Round(PetJsonNumber(canvas, "width", 260))
      , canvasH: Round(PetJsonNumber(canvas, "height", 340))
      , stride: Round(PetJsonNumber(text, "stride", 1040))
      , frameBytes: Round(PetJsonNumber(text, "frameBytes", 353600))
      , motions: PetParseClips(PetSectionText(text, "motions", "expressions"))
      , expressions: PetParseClips(PetSectionText(text, "expressions"))
    }
    return man
}

; ---------------------------------------------------------------------------
;  帧调度
; ---------------------------------------------------------------------------

; clip + 已播毫秒 → 帧下标（从 0 起）。
;   · 循环 clip：`floor(ms*fps/1000) mod frames`（4s@24fps 循环点无跳变）；
;   · 一次性 clip：播完**停在末帧**（不回绕、不越界）；
;   · 坏 clip（0 帧 / 0 fps）不崩，回落第 0 帧。
PetClipFrameAt(clip, elapsedMs) {
    if (!IsObject(clip))
        return 0
    n := clip.frames
    fps := clip.fps
    if (n <= 0 || fps <= 0)
        return 0
    ms := (elapsedMs > 0) ? elapsedMs : 0
    i := Floor(ms * fps / 1000)
    if (clip.loop)
        return Mod(i, n)
    return Min(i, n - 1)
}

; 一次性 clip 是否播完（循环 clip 永远 false）
PetClipDone(clip, elapsedMs) {
    if (!IsObject(clip) || clip.loop)
        return false
    dur := clip.frames * 1000 / clip.fps
    return elapsedMs >= dur
}

; LRU touch：把 key 移到末尾；超过上限淘汰最旧（数组头）。
; **只影响缓存加载顺序，不影响帧序**（帧序只由 clip 决定）。
PetLruTouch(order, key, cap := PET_FRAME_CACHE_CAP) {
    out := []
    for k in order {
        if (k != key)
            out.Push(k)
    }
    out.Push(key)
    while (out.Length > cap)
        out.RemoveAt(1)
    return out
}

; ---------------------------------------------------------------------------
;  StatusVisual → clip 选择（唯一输入是 StatusVisual 的输出，不自己判相位）
; ---------------------------------------------------------------------------

; 状态脸键：取 visual.face；空/缺省回落 "focus"（bake 一定有的那张）。
PetFaceKey(visual) {
    if (!IsObject(visual))
        return "focus"
    f := visual.HasProp("face") ? visual.face : ""
    if (Type(f) != "String" || Trim(f) = "")
        return "focus"
    return Trim(f)
}

; 一次性动作键：取 visual.motion；空 = 不播。
; 传了 manifest 时校验 clip 是否存在 —— 素材缺失就当场降级为「不播」，不报错。
PetMotionKey(visual, manifest := "") {
    if (!IsObject(visual))
        return ""
    m := visual.HasProp("motion") ? visual.motion : ""
    if (Type(m) != "String" || Trim(m) = "")
        return ""
    m := Trim(m)
    if (IsObject(manifest) && IsObject(manifest.motions) && !manifest.motions.Has(m))
        return ""
    return m
}

; 按 key 取 clip；kind = "motions" | "expressions"；取不到返回 0。
PetClipResolve(manifest, kind, key) {
    if (!IsObject(manifest))
        return 0
    if (kind = "motions")
        table := manifest.motions
    else if (kind = "expressions")
        table := manifest.expressions
    else
        return 0
    if (!IsObject(table))
        return 0
    return table.Has(key) ? table[key] : 0
}

; 当前该用哪个 clip：一次性动作优先（且必须素材存在），否则状态脸，最后 idle。
PetActiveClip(idleClip, faceClip, motionKey := "", manifest := "") {
    if (motionKey != "" && IsObject(manifest)) {
        mc := PetClipResolve(manifest, "motions", motionKey)
        if (IsObject(mc))
            return mc
    }
    if (IsObject(faceClip))
        return faceClip
    return IsObject(idleClip) ? idleClip : 0
}

; 播完一次性动作后回哪一档：{current, done} → "face" / 动作名。
PetNextClipName(current, done, fallback := "face") {
    if (current != "" && !done)
        return current
    return fallback
}

; 素材缺失时的静态兜底 clip：优先 focus 状态脸，其次任意状态脸，
; 再次 idle 循环，最后任意动作。全都没有 → 0（调用方据此整体降级到 HUD）。
; #237 验收「素材缺失时退化为静态首帧，玻璃卡仍可用」。
PetStaticClip(manifest) {
    if (!IsObject(manifest))
        return 0
    for kind in ["expressions", "motions"] {
        table := 0
        if (kind = "expressions")
            table := manifest.expressions
        else
            table := manifest.motions
        if (!IsObject(table))
            continue
        if (kind = "expressions" && table.Has("focus"))
            return table["focus"]
        if (kind = "motions" && table.Has("idle"))
            return table["idle"]
        for key in table {
            clip := table[key]
            if (IsObject(clip))
                return clip
        }
    }
    return 0
}