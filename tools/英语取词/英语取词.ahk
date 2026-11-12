#Requires AutoHotkey v2.0
#SingleInstance Force
Persistent
; 只把 VarUnset 这一种警告改成写 stdout。默认的 VarUnset 是**阻塞式 MsgBox**——
; Socket.ahk 里一处历史遗留的 Exception(...)（v2 没有这个类）就足以让脚本停在
; 启动处等点击。不写 #Warn All：那会连 LocalSameAsGlobal 一起打开，尽是噪音。
#Warn VarUnset, StdOut

; ============================================================================
;  英语取词.ahk —— 产品外壳（spec §3 目标布局的主脚本）
;
;  #12 把两半合成产品：
;    · 内核客户端 / 生命周期 / 日志 —— 来自票 #11 的贯通探针（probe.ahk，已删）
;    · 速记框 / toast / 遮罩定稿视觉 —— 来自票 #10 的 UI 定稿（ui-shell.ahk，
;      去掉 Ctrl+Alt+3..8 那组原型拍板开关；定稿理由见 docs/research/issue-10-ui-shell.md）
;    · 归一化 / 猜词 / 条目组装 / 落盘 —— 纯逻辑在 lib/Logic.ahk（TDD 接缝）
;
;  #13 补上常驻形态（spec §10 / §11）：
;    · 托盘完整菜单（暂停/打开捕获库/重启内核/测试热键/切换深色/退出）+ 自绘图标，
;      内核死时图标置灰；启动自检（vault / 热键 / 握手）走托盘气泡（决策 D10）
;    · 守护：判死 → 自动重启一次 + toast + 置灰；握手恢复后复原（决策 D8/D9）
;    · 日志 2MB×4 滚动、开机自启单一入口（幂等）、保存失败整条进剪贴板
;
;  一个热键两岔口（spec §4.3）：Ctrl+Alt+1 → 速记框；框内「截图取词…」走拖框，
;  直接打字则跳过 OCR。落盘 = spec §8 的一天一个文件 + UTF-8 无 BOM。
;
;  #25 按 v2 §6.2 加「直接截图取词」入口（默认 `Ctrl+Alt+2`），两条入口共用
;  一条流水线 `RunShot(ctx)`（`ctx.hadBox` = 开始前有没有框）：
;    · 无框：先采集出处 → 立刻遮罩 → 拖框 → **OCR 回来才建框**（`ShowBoxAtCursor`），
;      字段已填、焦点落释义；有框：沿用 v1 的隐藏 / 重现语义（框内 `Ctrl+S` 也走它）
;    · 出处必须在**任何窗口出现之前**采集（v1 §6.2 的硬约束）—— 无框入口最容易踩
;    · 取消（`Esc` / 30s / 选区 <4 lp）：无框入口**不留空框**，有框入口按原样重现
;    · 遮罩提示改成 §6.4 的字面量，右上角小字显示会话名（hub 未接时留空，C3 填）
;    · 全局热键的注册 / 探测 / 失败提示收敛在 `RegisterGlobalHotkeys()`（C5 接着推广）
;  跑法与已知坑：同目录 README.md
;
;  #24 按 v2 规格 §6.3 重做速记框（紧凑形态 + 键盘优先 + 键位提示 + chip）：
;    · 无标题栏卡片 400 lp × 折叠 ≤170 lp（v1 是 480×~400 的六字段长表单，挡死视频）
;    · 三行常显（目标词 / 音标+释义 / 原句）+ 按钮行；元数据按 `F2` 才展开
;    · 去逐字段标签，改灰色占位提示（EM_SETCUEBANNER）
;    · 焦点环 `Tab`/`Shift+Tab`/`↑`/`↓`；`类型`/`会话` chip 用 `←`/`→` 切
;    · 按钮文字自带键位 + 右下角常驻 `F1 全部快捷键`；`F1` 出键位表
;    · 状态行（词典 / 校正 / 中枢 / 会话）；默认深色，主题落 config
;    · `/demo` 开关：不注册热键、不拉内核与中枢，直接显示速记框（截图验收用）
;  纯逻辑（环 / chip 文案 / 类型推断 / 键位表文案）在 lib\Logic.ahk，harness 钉住。
;
;  #26 按 v2 §2 / §4.1 / §6.1 把保存路径换成 **spool**，会话做实：
;    · 保存 = 一条记录序列化成一行 JSON **追加**进 `<data>\inbox.jsonl`，再
;      best-effort 发 `flush`（不依赖任何进程；**不再写 vault / markdown**）
;    · 中枢不在位：spool 落盘即算成功（状态行 / toast 报「数据中枢未就绪（已暂存）」，
;      托盘置灰 —— §6.5 的蓝 = 内核 + 中枢都在位）；hub 起来后自己 ingest 回来
;    · 会话 chip 接 `session_peek`（hub 清洗后的标题 + 本会话词数），hub 不在位退化成
;      「本地截断标题（离线）」；两条入口（呼出框 / 直拍）都 peek —— 直拍开始时没有框
;    · chip 切到「(新会话)」→ 本行带 `new_session`（hub 侧跳过复用、强制新建会话），
;      保存后自动回到自动归属
;    · 重复词：hub 的 ingest 去重（同会话同 target 折叠大小写）→ 外壳把 flush 回话
;      映射成 `已存（本会话第 N 次）`
;  跑法与已知坑：同目录 README.md
;
;  #29 按 v2 §6.5 / §10 把生命周期做全（外壳是 kernel 与 hub 的**唯一所有者**）：
;    · 托盘换成 §6.5 的项（`暂停热键`/`打开取词台`/`打开数据目录`/`重启内核`/
;      `重启中枢`/`测试热键（…）`/`切换深色`/分隔线/`退出`），项与顺序一处定义在
;      Logic.TrayMenuItems（harness 钉住），SetupTray 只照建并记一条菜单日志
;    · 启动时像拉内核一样把 **hub** 拉起来（`pythonw hub.py`，工作目录工具目录）+
;      端口接管（D12）；拉不起来显式气泡 + 日志，**保存照走 spool（词不丢）**
;    · hub 存活探测：每秒一枚 ping（Logic.HubProbeStep），在途 3s 没回话即判死 →
;      HubDeathAction（存活期判死重启一次、重试期再死不重启不刷 toast）→ 拉起新的；
;      探测永远在跑，**hub 回来后自动重连、托盘复蓝**（#29 要修的缺口：以前断了
;      再也不重连，托盘一直灰）
;    · 退出：kernel 与 hub 的整棵进程树都 taskkill /F /T 收干净
;
;  #47（B1）修「保存后按键/焦点泄漏给浏览器」（机制判定见 docs/research/v3-b1-mechanism.md）：
;    · 主因 H1：呼出热键是 Alt 系，AHK 吞掉非修饰键让浏览器收到一次「裸 Alt」→
;      Chromium 聚焦应用菜单「...」；框关掉后空格命中那按钮而不是播放视频
;    · 主修：呼出时留下前台 hwnd（G_PrevHwnd），DestroyBox 后 WinActivate 回去
;      （RestorePrevFocus；窗口没了就跳过）
;    · 「吞 Alt up 尾声」的兜底**不采用**：快按（Alt up 早于框拿到焦点）时框根本
;      不在作用域里、吞不到；慢按时又会让浏览器残留 Alt（下一个空格变 Alt+Space）。
;      正解是让呼出键不再产生裸 Alt（`~!1` 放行形态或含 Ctrl 的组合，详见报告）。
;
;  #48（B2，B1 的收口）从**源头**去掉裸 Alt：热键串支持 `~` 放行前缀
;  （`~!1` → 那次组合原样送达浏览器，浏览器看到完整 `Alt+1`，不构成裸 Alt）。
;    · 解析/冲突/显示：Logic.ahk 的 ParseHotkeyV2（`pass` 标记）/ HotkeyStatuses
;      （`~` 不进 mods+vk → 与不带 `~` 的同组合互相判冲突）/ PrettyKey（「（放行）」）；
;    · 注册串原样透传（`Hotkey(rec.spec, …)`，不重拼）；
;    · 启动时若呼出键是裸 Alt 形态（含 `!` 不含 `~`）记**一行**日志提示（WarnBareAltHotkey，
;      不弹气泡）；用户配置由用户自己决定改不改（#47 的实测证据）。
;
;  v5 增量（docs/specs/v5-capture-automation.md；本节只列外壳这一半）：
;    · §2.4 peek 带 `hwnd`：呼出前抓到的那只前台窗口交给 hub，本机探针（UIA 读地址栏）
;      只读**这一只**；`BeginCapture` / `ResetForNextWord` 两处都带（有测试钉字面量）。
;    · §3 媒体联动：`BeginCapture` 顶部发 `media pause`（两条入口的唯一漏斗，标志防重复），
;      `DestroyBox` 与 RunShot 的**无框终止路径**发 `media resume`（Enter 保存并继续不续播、
;      取消后又把框重现不续播）。全 fire-and-forget，回包只记一行状态字（`OnMediaLine`）。
;    · §4.1 补全可见性：OCR 猜词记进 `G_TargetAuto`，首次聚焦整段全选（打一个字即替换）；
;      用户一改 / 换词重置即清标记。提示行加「空闲补全 / 无匹配」两条来源（Logic 纯函数）。
;    · §6 释义残留：手改目标词时若释义仍是上一个词的自动值 → 立刻清释义/音标/dict_json
;      （判定在 Logic.ShouldClearAutoMeaning；手打过的释义永不动）。
; ============================================================================

; --- DPI 感知（spec §4.2：坐标一律物理像素；-4 = PER_MONITOR_AWARE_V2）------
; 必须在任何取坐标之前调用。内核侧同样设了 per-monitor-v2，两边一致才不会偏移。
DllCall("SetProcessDpiAwarenessContext", "Ptr", -4)
CoordMode "Mouse", "Screen"
CoordMode "ToolTip", "Screen"

; 速记框所在屏的 DPI 标度。初始取主屏（A_ScreenDPI）；票 #184（A6）起 PresentBox
; 每次落位后按 GetDpiForWindow 校正 —— 框拖到副屏，尺寸/字体跟那块屏走
; （A_ScreenDPI 只反映主屏，混合 DPI 下整体错；长文面板同款先例）。
global D := A_ScreenDPI / 96
P(n) => Round(n * D)                       ; lp（96dpi 逻辑像素）→ 物理像素

; Win11 窗口圆角。33 = DWMWA_WINDOW_CORNER_PREFERENCE，2 = round。
; Win11 本来大概率就给圆角，这里是幂等兜底；Win10 上该属性不存在，调用无效
; 但无害（DwmSetWindowAttribute 返回失败即可）。
RoundCorners(hwnd) {
    DllCall("dwmapi\DwmSetWindowAttribute", "Ptr", hwnd, "Int", 33, "Int*", 2, "Int", 4)
}

; --- 纯逻辑层 + 唯一的第三方依赖 --------------------------------------------
#Include "lib\Logic.ahk"
#Include "lib\Socket.ahk"
; v36 票 #243（spec 决策 8/9）：半透明面板的公共绘制件（气泡/信息流/菜单共用的
; 字体 + 逐像素 alpha 修补，v35 时寄居在 PetBubble.ahk 里被跨文件调用）+ 桌宠
; 点击链路的**异步** HTTP 小件（菜单先渲染后补数据，超时 ≤300ms）。
#Include "lib\PanelPaint.ahk"
#Include "lib\PetNet.ahk"
; v21 T5（#160）：长文面板是**独立新窗口**，实现全部在 lib\LongformPanel.ahk；
; 主脚本这边只有：这个 #Include、ReadConfig 的 4 个几何键 + 2 条热键、
; HotkeyEntries 的两条、启动处一个 hook。旧速记框一行不改。
#Include "lib\LongformPanel.ahk"
; v22 P2b（票 #167 / spec §9）：面板「分段预览窗」独立窗口（只读校验/重分段入口）。
; 与面板同一纪律：独立文件、独立状态、独立窗口；面板只管入口（底栏按钮 / Alt+Shift+P）。
#Include "lib\LongformInspector.ahk"
; v34 T4（票 #217 / spec §B15）：字幕带重拖从独立脚本并入主壳（遮罩/拖框/
; band_redrag RPC）。入口是统一热键表的 `hotkey_redrag`（默认 ^!6）；与截图取词
; 共用 G_ShotActive 互斥锁，Esc 只在遮罩窗口内生效，30s 无操作自解。
#Include "lib\BandRedrag.ahk"
; v34 验收追加（#220）：看视频时常驻的收句状态浮窗
#Include "lib\CaptureHud.ahk"
; v35 #231（票 #231）：玻璃卡 —— 只在「要用户动手」的四档相位展开的毛玻璃小卡。
; 与 CaptureHud 并列：pet 线健康时由它承担「该动手」的表达（HUD 的 gating 归 #232）。
#Include "lib\GlassCard.ahk"
; ==== v35 #233 接线点 B（票 #233）：气泡 + 60s 待挑轮询 ====
; 气泡文案/冷却/勿扰/引导判据全在 lib/Logic.ahk 的 #233 段；本文件只挂窗口与轮询。
#Include "lib\PetBubble.ahk"
; ==== v35 #233 接线点 B 结束 ====
; ==== v35 D1/D3 接线点（v35-ux design.md；lib/PetFeed.ahk / lib/PetMenu.ahk 归 A2）====
; A2 已建出两个文件，正常 include（A3 接线处另有函数存在守卫，未就位时安全降级）。
#Include "lib\PetFeed.ahk"
#Include "lib\PetMenu.ahk"
; ==== v35 D1/D3 接线点结束 ====

; ============================================================================
;  v35 #232 接线点 A（票 #232）：桌宠角色窗 —— 帧调度纯函数 + 分层窗口
;  与 #231 的玻璃卡接线点 B 分属两段注释块，互不改写；本段由 #232 拥有。
;  D-L3：pet 健康时 HUD 让位（下面的 gating），关桌宠/素材缺失时 HUD 原样工作。
; ============================================================================
#Include "lib\PetFrames.ahk"
#Include "lib\PetWindow.ahk"
; ==== v35 #232 接线点 A 结束 ====

; socket 回调里绝不能阻塞读：Socket.ahk 的 OnMessage 带 Critical，阻塞读会把
; 整个脚本（连全局热键和 Esc 一起）焊死。关掉 Blocking，自己攒行缓冲。
Socket.Blocking := False

; --- 常量 -------------------------------------------------------------------
global SECTION := "英语取词"
global TOOL_DIR := A_ScriptDir
global CONFIG_PATH := TOOL_DIR . "\config.ini"
global LOG_PATH := TOOL_DIR . "\logs\app.log"
global WORDS_PATH := TOOL_DIR . "\data\common_words.txt"
; #102：补全的「词表校验」集要更宽（2000 表连 reluctant / reinforce 都不收），
; 这份 10k 与内核 corrections 的 known_words 同源；**惰性**读（见 KnownWords）。
global WORDS_10K_PATH := TOOL_DIR . "\data\words_10k.txt"
global VAULT_DEFAULT := ".\vault"   ; spec §9 默认值
global MASK_ALPHA := 90                    ; ≈35% 不透明；绝不 255（spec §4.2）
global MIN_SELECT_LP := 4                  ; 小于它视为误操作（单位 lp，spec §4.2）
global PING_INTERVAL := 300                ; 握手节奏：300ms × 10s（spec §5.3）
global PING_TIMEOUT := 10000
global WATCH_MS := 100                     ; 在途期间的超时检查
; 中枢存活探测（票 #29）：每秒探一枚 ping，在途 3s 没回话即判死（§6.5 的图标
; 要诚实 —— 中枢被重启后外壳得自己发现并重连，不能等下次重启外壳）。
global HUB_PROBE_MS := 1000                ; 探测节奏
; 在途 ping 超时即判死。票 #184（A7）实测后维持 3000 不动：隔离实例（tmp db）上
; 512KB article_create（协议最大正文）OCR 路 5 轮 377–427ms、粘贴档顶格 613ms、
; 重分段 112ms、paragraphs 89ms —— 最坏单请求 ~0.61s，3s 有约 5 倍余量；「放宽超时」没有
; 实测依据，「重命令在途暂停判死」反而会拆掉冻结 hub 的唯一安全网（同连接上的
; ping 排在卡死的命令后面，正是超时判死的形状）。残余风险只有 store 全局锁上的
; 大批积压 ingest（研究报告 H12，第三批），它动的是锁不是探测。
global HUB_PROBE_TIMEOUT := 3000
; 2026-11-10：判死要**连续**这么多次超时（每次 = HUB_PROBE_TIMEOUT）。
; 实测一天里 28 次「中枢判死并重启」，全发生在用户操作后：hub 正在做重活
; （查轨网络请求 / ECDICT 词表 / 底图清理）时 Python 的 GIL 被占住，pong 迟到两三秒，
; 3 秒超时就误杀。代价很高——重启要 4 秒、而且 armed 只活在 hub 内存里，用户刚点过的
; 「同意收句」会凭空消失（所以另有 CaptureArmReplay 兜底）。两次 ≈ 最多 7 秒仍能抓到真冻结。
global HUB_DEAD_STRIKES := 2
; 内核就绪后的存活探测（票 #185 / A9）：照 HubWatch 的模式，频率从俭 —— hub 1s
; 一枚，内核 5s 一枚（探测对象是「挂而不死」的内核，不是延迟敏感的保活）。在途
; 超时判死用 3000：空闲内核不回 pong 只可能是挂死；OCR/长文在途期间探测整体收手
; （Logic.KernelProbeStep 的 inflight 分支），判死归 Watch / LfLongWatch 的墙钟管。
global KERNEL_PROBE_MS := 5000
global KERNEL_PROBE_TIMEOUT := 3000
global HUB_START_TIMEOUT := 10000          ; 拉起后 10s 仍不位 → 气泡一次（继续重连）
global SHOT_IDLE_MS := 30000               ; 遮罩兜底：30s 无操作自动取消
global LOOKUP_DEBOUNCE_MS := 250           ; 目标词确定后去抖 250ms 才发 lookup（§7）
global CONFIG_WATCH_MS := 2000            ; config 热重载轮询间隔（spec §9.2：2s）
global LOG_MAX_BYTES := 2 * 1024 * 1024    ; 日志单文件上限 2MB（spec §10.2）
global LOG_KEEP := 4                       ; 滚动保留 4 个（spec §10.2）
global ICON_NORMAL := TOOL_DIR . "\assets\tray.ico"    ; #10 自绘图标（spec §10.1）
global ICON_OFF := TOOL_DIR . "\assets\tray-off.ico"   ; 内核死时灰阶（spec §11）
; v34 T5（票 #218 / spec §C25）：托盘收句三态。灰（ICON_OFF）仍然只表达
; 「内核/中枢不在位」（决策 D9）—— 收句中/异常是另一个维度，不复用置灰。
global ICON_REC := TOOL_DIR . "\assets\tray-rec.ico"     ; 收句中（蓝底红点）
global ICON_ALERT := TOOL_DIR . "\assets\tray-alert.ico" ; 异常（区域失效/引擎不可用/心跳断）
; v34 T5（spec §C20）：心跳年龄超过 10s = 「心跳中断」；与 hub 侧
; capture_scheduler.HEARTBEAT_STALE_MS 同一个口径。
global CAPTURE_HEARTBEAT_STALE_MS := 10000
; v34 T5（spec §C24）：外壳侧提示冷却 5 分钟（同 kind 只弹一次）。
global CAPTURE_EVENT_COOLDOWN_MS := 300000

; 设计令牌（v1 取值与理由见 docs/research/issue-10-ui-shell.md；v2 的形态见
; docs/specs/v2-workbench.md §6.3 与 §8.1 的颜色令牌）
global LP := {
    W:         400,    ; 速记框宽 lp（§6.3 的硬指标）
    BORDER:      1,    ; 自绘 1px 边（§6.3：#2A2F36）
    RADIUS:     12,    ; 圆角 lp（§6.3）
    PAD:        10,    ; 框内边距 lp
    GAP:         4,    ; 行间距 lp（折叠态高是硬指标 ≤170 lp，这里省一点）
    CHIP_K:     40,    ; 类型 chip 宽 lp
    CHIP_S:    150,    ; 会话 chip 宽 lp
    CHIP_GAP:    8,    ; 目标词 ↔ chip、chip ↔ chip lp
    GRIP_W:     16,    ; 拖拽把手宽 lp（v5 §5：第一行最右，与 chip 同高）
    TARGET_MIN: 80,    ; 目标词下限 lp（v5 §5：低于它就撤站点徽章，主字段优先）
    CHIP_SITE:  96,    ; 站点徽章上限宽 lp（§7；文字宽度实测，超长 host 才吃这个上限）
    CHIP_SITE_PAD: 5,  ; 站点徽章左右留白 lp
    PHON:       76,    ; 音标列宽 lp
    BTN_H:      25,    ; 按钮定高 lp（s9 的原生按钮高 28 lp，定高才收得住）
    BTN_GAP:     6,    ; 保存 ↔ 取消 lp
    BTN_GAP2:   12,    ; 取消 ↔ 截图、截图 ↔ 更多 lp
    KEYMAP_W:  190,    ; F1 键位表宽 lp
    KEYMAP_PAD: 10,    ; F1 键位表内边距 lp
    POS_W:      72,    ; 「位置」输入宽 lp（≈8 个数字字符）
    PASTE_GAP:   6,    ; 「位置」→「粘贴」lp
    POS_TAG_GAP: 12,   ; 「粘贴」→「标签」lp
    OFFSET:     16,    ; 框相对鼠标的偏置 lp
    EDGE:        8,    ; 距屏幕边缘的最小留白 lp
    TOAST_R:    24,    ; toast 距右 lp
    TOAST_B:    64,    ; toast 距下 lp（避开任务栏）
    TOAST_GAP:   8,    ; toast 叠层相邻两枚的缝 lp（票 #185 / A14）
    HINT_TOP:   64,    ; 遮罩提示文字距顶 lp
    BTN_TEXT: "1A1A1A", ; 按钮字色：原生按钮在浅色 Windows 下恒为浅底，故恒用深字
    ACCENT: "4DA3FF",  ; 选区描边内圈色
    BLACK:  "000000",  ; 遮罩底色
    DIM:       90,     ; 遮罩整窗 alpha（≈35% 不透明；绝不 255）
    TOAST_A:  235      ; toast 背景 alpha
}

; --- 运行时状态 -------------------------------------------------------------
global G_Cfg := 0
global G_Port := 47800
global G_VaultDir := VAULT_DEFAULT
; 在途 capture_ocr 的中止上限（spec §5.3 / §11，2026-10-14 修订：2000 → 20000）。
; 内核是「一连接一线程」，单行捕获只跑 2 遍识别；**多行段落**每行一遍、双引擎每行
; 两遍，实测活内核单次 capture_ocr：1400×200 约 3.6s、2000×400 约 7.0s、
; 整屏 2560×1600 约 11.3s —— 全都远超旧的 2s，于是「截一段自然段」必被判死重启。
; 真死的判据是不变的「socket 断开」（OnKernelLost 立刻触发）；这个值只是兜底，
; 取「整屏最坏 11.3s 的约 1.8 倍」，宁可多等也不能误杀一次正确但在途的识别。
; 等待期间外壳始终可响应（Esc 可取消），所以兜底放宽的代价很小。
; ［2026-10-15 #76/#78 更新］上面那组段落耗时已过时：整段并带 + 噪声门回落落地后，干净段落降到
; 0.1s 量级、有噪档按设计回落逐行（0.5~0.8s）。这个 20000 保留不动（兜底上限；见
; docs/decisions/2026-10-15-review-fixes-orchestration.md 的 W6）。
global G_OcrTimeout := 20000
global G_LogEnabled := true
global G_Common := 0                       ; 猜词词表（Map）
global G_Known := 0                        ; #102 补全校验词表（common ∪ 10k；惰性）
global G_Dark := true                      ; 主题：v2 默认深色（§6.3）；托盘
;                                           「切换深色」可切并落到 config（决策 D6）
global G_KernelPid := 0                    ; 我们拉起的那个内核的 PID（单所有者）
global G_Ready := false
global G_Sock := 0
global G_Pending := false
global G_PendingAt := 0
global G_Deadline := 0
global G_ToldNotReady := false
global G_ShotActive := false               ; 遮罩拖框进行中（挡热键重入）
global G_ShotCancelled := false
; 本轮截图 OCR 回来时要不要**新建**框（v2 §6.2 的无框入口）；有框入口恒为 false
global G_ShotBuildBox := false
; 呼出速记框之前的前台窗口 hwnd（票 #47 B1）：框关闭后要把焦点交回它。Alt 系热键
; 会把呼出弦里的非修饰键吞掉，浏览器因此看到一次「裸 Alt」并把焦点给了应用菜单
; 按钮（「...」）——框关掉以后那按键还在焦点上，空格就变成「激活按钮」而不是
; 播放视频。呼出时（任何窗口出现之前）把这个 hwnd 留下来，关闭时 WinActivate 回去。
global G_PrevHwnd := 0
; 已注册的全局热键串（RegisterGlobalHotkeys 登记、托盘「暂停热键」照这份名单开关）
global G_GlobalKeys := []
global G_HotkeyPaused := false             ; 暂停热键（托盘勾选态表达，D9）
global G_TestHotkey := false               ; 「测试热键」pending 标志位
; C5：框内键屏蔽表（spec → 原因）；bad/dup 的框内键不注册（RegBoxKey 查它）
global G_BoxBlocked := Map()
; C5：`<data>\hotkey-status.json`（冲突检测结果的落点，W3 设置页轮询它）
global G_HotkeyStatusPath := ""
; C5：config.ini 的 mtime+size 指纹（2s 轮的 config 热重载比对用）
global G_ConfigStamp := ""
global G_WatchPhase := "retrying"          ; 守护期：启动即在握手/重试期
global G_TrayGrayed := false               ; 托盘图标是否已置灰（内核或中枢不在位）
global G_RestartN := 0                     ; 判死重启轮次（只记日志）
global G_RestartPending := false           ; 已排一枚重启拍子、还没跑完（票 #69：判死不叠加）
global G_KernelProbeSent := 0              ; 票 #185（A9）：上次发存活 ping 的时刻（0 = 从未发）
global G_KernelProbePending := 0           ; 票 #185（A9）：在途存活 ping 的发出时刻（0 = 没有在途）
; 中枢（v2 §2）：RPC 是 best-effort —— 连不上只是「不在位」（chip 离线 / 保存走
; spool / 图标灰），永不阻塞外壳，也永不成为保存的必要条件。
global G_HubPort := 47810
global G_WebPort := 47811                  ; hub 的 HTTP 端（hotkey_review 打开它）
global G_HubSock := 0                      ; hub RPC 连接（0 = 还没连上）
global G_HubUp := false                    ; 中枢是否在位（**收到过回话**才算，§6.5）
global G_HubSeq := 0                        ; RPC 序列号（票 #178：请求/回话自带 _rpc_id）
global G_HubShortSeen := 0                  ; 已记过日志的短写次数（口径同 socket 的 ShortWrites）
global G_HubAsks := []                     ; 在途命令（每条含 id / kind / gen；响应按 id 对号入座，见 HubPickAsk）
; v4.1（#54 P2-5）：待回执的 flush 目标改**队列**（按发出顺序对号入座）——连存两条时
; 第 1 条回话报的必须是第 1 个词（单槽会被后一条覆盖 → toast 串词/空词）。
global G_HubFlushQ := []                   ; 在途 flush 的收据队列（元素 { target }）
global G_SpoolPath := ""                   ; `<data>\inbox.jsonl`（§2 的 spool 真身）
; 中枢生命周期（票 #29；与内核同一套语义：外壳是唯一所有者，拉起/判死/重启/收树）
global G_HubPid := 0                       ; 我们拉起的那个中枢的 PID（单所有者）
global G_HubWatchPhase := "retrying"       ; 中枢链的守护相位（与内核各一条链）
global G_HubProbeSent := 0                 ; 上次发存活 ping 的时刻（0 = 从未发）
global G_HubProbePending := 0              ; 在途 ping 的发出时刻（0 = 没有在途）
global G_HubProbeStrikes := 0              ; 连续超时次数（达 HUB_DEAD_STRIKES 才判死）
global G_HubDeadline := 0                  ; 拉起后到这刻仍不位 → 气泡一次（不重试风暴）
global G_HubToldNotReady := false
global G_HubEverUp := false                ; 曾经就绪过 → 恢复时给一枚「已恢复」toast
global G_HubPongLogged := false            ; 本次在位期是否已记过一次 pong 耗时（探测每秒一拍，不刷日志）
global G_CaptureArmed := false             ; v34 T3（#216）：本机缓存的收句扳机（中枢内存态的影子；HubDown 清位）
global G_ArmIntent := ""                    ; 用户最后一次表态（"on"/"off"；中枢重启后重放 "on"）
global G_ArmReplay := false                 ; 正在重放（这次 capture_arm 回话不是用户的动作 → 不弹 toast）
; v34 T5（票 #218 / spec §C22–C25）：ping 回包带来的收句状态影子。全部由
; `CaptureStatusApply` 边沿触发更新（1/s 的 ping 不每拍写 tooltip / 弹气泡）。
global G_CapArmed := false                 ; cap_armed
global G_CapOcr := ""                      ; cap_ocr（off/active/idle/dormant/redrag_needed/failed）
global G_CapSession := 0                   ; cap_session（当前段会话 id，无段 = 0）
global G_CapAgeMs := -1                    ; cap_age_ms（-1 = 从未收到心跳）
global G_CapTip := ""                      ; 上次写进 A_IconTip 的字符串（变化才写）
global G_TrayMode := "normal"              ; rec / normal / alert（灰不在这条轴上）
global G_CapEventAt := Map()               ; 事件 kind → 上次弹出的 A_TickCount（5 分钟冷却）
; ==== v35 D1/D5（v35-ux design.md）接线状态：信息流行 + 主动性引擎 ====
; 这些是 CaptureStatusApply 的「上一拍快照」与 chatter 冷却账本；纯逻辑全在
; lib/Logic.ahk 的 D5/D1 段（PetSpeechArbitrate / PetFeed*Line），这里只有状态。
global G_CapPhase := ""                    ; 上一拍 cap_phase（相位变化行边沿）
global G_CapAdded := 0                     ; 上一拍 cap_added（+1 句 / 会话结束行）
global G_CapLast := ""                     ; 上一拍 cap_last（+1 句的预览文本）
global G_CapHeartbeatBad := false          ; 上一拍心跳是否已断（ageMs 缺失/超时）
global G_CapArmedSince := 0                ; 本次连续 armed 的起点（0 = 未开启）
global G_CapArmedMin := 0                  ; 上一拍的连续 armed 分钟数（caring 边沿）
global G_CapFeedIdleTicks := 0              ; v36 #245：距上一条信息流行过了几拍（持续状态行去噪）
global G_ChatterLastAnyAt := 0             ; 上次任意主动发言（60s 全局间隔）
; 2026-11-10 用户要求「视频播完自动停收句」：cap_ended 的连续拍计数与冷却。
global G_CapEnded := 0                     ; 上一拍 cap_ended（0/1）
global G_CapEndedStreak := 0               ; 连续多少拍判出「播到末尾」（抖动过滤，见 CaptureEndedShouldStop）
global G_CapAutoStopAt := 0                ; 上次自动停收句的 tick（10s 冷却，off 没发出去时别每拍重试）
; 2026-11-10 临时保底：cap_no_source（10 秒帧签名窗口判「无画面字源」）。
global G_CapNoSource := 0                  ; 上一拍 cap_no_source（0/1）
global G_CapNoSourceStreak := 0            ; 连续多少拍判出「无字源」（与播完同款连两拍）
global G_ChatterLastAt := Map()            ; kind → 上次该类发言（3/6/2 分钟冷却）
global G_ChatterDay := ""                  ; 当日计数所属日戳（yyyyMMdd，换天清零）
global G_ChatterCount := Map()             ; kind → 当日已发言次数（邀请类有日上限）
; ==== v36：场景识别（cap_scene）的状态 ====
global G_CapScene := ""                    ; 上一拍 cap_scene（"" = 无场景/旧中枢）
global G_CapVideo := 0                     ; 上一拍 cap_video（0/1 = B站视频页且在播；扁平标量）
global G_SceneInviteHold := false           ; 场景邀请待发位（正边沿置位，说过/离开场景清位）
; v36 #246：来源播报（cap_cc）—— 与相位并行的独立扁平字段，不进相位（spec 决策 6）。
global G_CapCc := ""                       ; 上一拍 cap_cc（ok/no_english_track/error/off）
global G_CcReportSession := 0              ; 已经播报过来源的会话 id（同一视频只说一次）
global G_BandAskSession := 0                ; 已经问过「要不要框字幕带」的会话 id（每会话一次）
; 2026-11-11：字幕带红框预览（ping 的 cap_band_rect / cap_band_auto 驱动）
global G_CapBandRect := ""                  ; 当前自动/已入账字幕带矩形（x/y/宽/高；空 = 无）
global G_BandPreviewGuis := []              ; 红框预览的 4 条边（Gui 对象）
global G_BandPreviewAt := 0                 ; 上次红框预览的 A_TickCount（区域跟随滚动时防闪烁）
; 2026-11-11 ASR P2：转写状态（ping 的 cap_asr / cap_asr_added 驱动）
global G_CapAsr := ""                       ; off / queued / done / error
global G_CapAsrAdded := 0                   ; 本次转写入池句数
global G_CapAsrReason := ""                 ; error 时的原因码（auth/rate_limit/empty/...）
global G_HubRestartPending := false        ; 中枢的重启位（与内核同一个 RestartPhase，票 #69）
global G_DataDir := ""                     ; `<工具目录>\data`（§6.5「打开数据目录」）
global G_SpeechMissLogged := ""            ; 上次已记过「这一拍为什么没说」（同因不刷屏）

; 速记框状态（隐藏→重现时靠 V 保住已输入的内容）
global Bgui := 0                           ; 速记框 Gui（0 = 不存在）
global C := {}                             ; 控件引用（普通对象，不是 Map）
global V := BlankV()
global Hint := 0                           ; 0 或 { kind: "hint"|"err", text }
global G_ErrText := ""                     ; 提示行来源①（最高优先）：保存失败 / 目标词为空的红字
global G_CompIdx := 0                      ; 提示行来源②：句内补全的当前候选下标（0 起）
global G_SavedCount := 0                   ; 本框累计保存数（v4 §4.1，票 #51）—— 提示行第 4 来源 + 保存按钮文案
global BoxPos := { x: 0, y: 0 }
global DragHwnds := []                     ; 遮罩期间要变十字光标的窗口
; 遮罩拖框的两个端点（spec §4.2）。**坐标来自鼠标消息，不是轮询采样**：
; `WM_LBUTTONDOWN/UP` 的 lParam 就是那一刻的客户区坐标，零延迟；轮询
; `GetKeyState` 会晚 0~15ms 才补采，手一快框选就整块朝拖动方向缩掉几十像素
; （2026-10-15 真机复现：1px/ms 匀速拖动测得起点晚 0/16/23ms）。
; 空串 = 这条消息还没到（按下发生在遮罩出现之前等）→ 调用方回落到轮询。
global G_MaskOv := 0                       ; 遮罩主窗 HWND（只有它的消息算数，0 = 没遮罩）
global G_MaskOx := 0                       ; 遮罩客户区原点（物理像素的虚拟屏坐标；票 #184（A6）起遮罩罩整个虚拟屏，左/上副屏布局下是负数）
global G_MaskOy := 0
global G_MaskDownX := ""                   ; 按下点（屏幕物理像素）
global G_MaskDownY := ""
global G_MaskUpX := ""                     ; 松开点
global G_MaskUpY := ""
; v2 速记框的状态（§6.3）；文案/环序等纯逻辑在 lib\Logic.ahk，这里只有状态
global G_Expanded := false                 ; `F2`：元数据展开态
global G_CtxRows := 1                      ; `F3`：原句 1 行 / 3 行
global G_Kind := "word"                    ; 类型 chip 的值（§3）
global G_KindTouched := false              ; 用户手动切过类型 → 不再按目标词自动推断
global G_SessTitle := ""                   ; 会话 chip 的标题（hub 的 session_peek 填）
global G_SessCount := 0                    ; 本会话已有词数（hub 报；离线恒 0）
global G_SessNew := false                  ; `←→` 切到「(新会话)」→ 本行 `new_session`
global G_SessOffline := false              ; hub 不在位 → chip 退化成「本地标题（离线）」
global G_SessApp := ""                     ; 会话线索 / spool 的 source_app（友好名）
global G_SessRawTitle := ""                ; 会话线索 / spool 的 source_title（原始标题）
; 票 #70：捕获代次（每次 BeginCapture +1）。peek 的回包要等 hub 侧本机探针 + 排队，
; 上一轮（已取消 / 已收工）的回话能落进下一轮 —— 发 peek 时把代次随 ask 入队，
; 回话只在代次相等时才允许写上面那批 chip 状态（判定 = Logic.ReplyApplies）
global G_CaptureGen := 0
; v3 §7：站点徽章 / 位置自动填 / spool 的 url 三者都来自同一次 peek 回包
global G_SessSite := ""                    ; 站点短名（§2.5；空串 = 无链接 → 徽章整块不出现）
global G_BrowserUrl := ""                  ; 采纳到的页面 URL（空串 = 无；落 spool 的 url）
; v21 round2 (B)：长文面板开着时，「最近一次非面板前台」缓存（面板内 Ctrl+V 的出处）。
; 只在面板存在期间由 700ms 低频定时器刷新 —— 不忙循环，也不在面板关闭后继续跑。
global G_PanelProvLast := { title: "", app: "", hwnd: 0 }
global G_BoxDragging := false              ; 用户正在拖框（WM_ENTERSIZEMOVE…EXITSIZEMOVE）
; v5 §5：拖动起点（物理像素）。WM_EXITSIZEMOVE 拿它比一比 —— 只有**真的移动过**
; 才写 box_pos=custom（单击把手不许把框「钉」在原地）。
global G_BoxDragX := 0
global G_BoxDragY := 0
; v5 §5（真机致命回归 F1 的修法）：把手的 **HWND 整数**单独存一份。
; 为什么不能直接用 `C.grip.Hwnd`：DestroyBox 只销毁 Bgui，控件对象还挂在 C 上；
; 之后再读 `C.grip.Hwnd` 会抛 `Error: The control is destroyed.` —— 而 CursorHandler
; 是**全局**消息监视器（遮罩拖框时也收 WM_SETCURSOR），于是「呼出→Esc→截图拖框」
; 一按就崩。整数读不碰控件对象，框销毁时复位成 0 即可。
global G_GripHwnd := 0
global G_Status := ""                      ; 状态行代码（文案由 Logic.StatusText 出）
global G_StatusArg := 0
; 票 #185（A14）：活动 toast 的队列（叠层管理）。元素 { gui, w, h }，ShowToast 入队、
; ToastDismiss 出队；空队列 = 没有在显示的 toast。
global G_Toasts := []
; 释义自动填入（v2 §7 / 票 #27）：目标词确定后去抖发 lookup，回话异步到达
global G_LookupDeb := 0                    ; 去抖状态 {word, at}；0 = 没有在途（Logic.DebounceStep）
; v23 S3（B5）：本次 lookup 的看门狗 deadline（A_TickCount 口径；0 = 没有在途）。
; 回话丢失时外壳会永远停在「词典查询中…」，LookupWatchdog 到点收手。
global G_LookupDeadline := 0
global G_LastAutoMeaning := ""             ; 上一次自动填入的释义（覆盖判定的基准，§7）
; v18 W4（#149）两段式回填：已为哪个词补发过 full 二跳（同词只补一次；
; OnTargetChange / ResetForNextWord 清零）
global G_LookupFollowedUp := ""
; v5 §4.1：目标词框里那个值是不是**自动猜词**（OCR 回填写入）。非空时首次聚焦整段全选
; （打第一个字即替换）；用户一改（OnTargetChange）/ 换词重置（ResetForNextWord）即清空。
global G_TargetAuto := ""
; v5 §3：媒体联动（停播/续播）的状态标志 —— 我们发过 `media pause` 才允许 `resume`
; （原本暂停的视频不动它、用户中途自己按了播放也不留痕迹）。hub 不在位时 `HubSend`
; 失败、标志不置位，于是也不会去发一条没人听的 `resume`。
global G_MediaPauseSent := false
; 2026-10-23：框内取消键（默认 Esc）的**抬起**处理 —— keydown 只置位，抬起时才真收工。
; 原因：keydown 被框内热键吞掉，但收工/还原焦点一旦在 keydown 里做，keyup 就落到刚激活的
; 浏览器窗口上；浏览器**全屏**播放时 Esc 抬起会退出全屏（用户每次都得重进）。
global G_CancelOnUp := false               ; 已收到取消键按下，等抬起再收工
global G_BoxCancelUpReady := false         ; 「取消键抬起」热键是否真的挂上了
global G_CorrText := ""                    ; 「已校正 N 处：…」状态行（Logic.CorrectionsText）
; v17 W5（票 #142 / 决策 D1）：内核标的「疑似粘连」清单（Logic.ParseSuspects 的产物，
; [[token, start, end], …]）。只标记、不改原句 —— 外壳用它跳候选/预填并给提示；
; 旧内核没有这个字段 → 空数组 → 整条消费自动退化成现状。
global G_OcrSuspects := 0
global G_SeenText := ""                    ; 「已记过 N 次 · 上次…」状态行（Logic.SeenText）
global G_DictWait := false                 ; 「词典查询中…」（§7 异步）
global G_NoteText := ""                    ; 「释义已手打，未覆盖」一类的低优先级状态行
global G_KeymapGui := 0                    ; `F1` 键位表小窗
global G_BoxWin := ""                      ; 框内键的作用域判据（HotIfWinActive）
global G_Title := "英语取词 速记"          ; 窗口标题（无标题栏，仅供 WinTitle 定位）
global G_Demo := false                     ; `/demo`：不注册热键、不拉内核/中枢，只显示框

; ---------------------------------------------------------------------------
;  日志（spec §10.2：写 logs/app.log；只记事件，不记用户内容——原句/释义不进日志）
; ---------------------------------------------------------------------------
WriteLine(path, s) {
    try {
        SplitPath(path, , &dir)
        if (dir != "" && !DirExist(dir))
            DirCreate(dir)
        FileAppend(FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss") . "." . Format("{:03d}", Mod(A_MSec, 1000))
                 . " " . s . "`n", path, "UTF-8-RAW")
    }
}

Log(s) {
    global LOG_PATH, G_LogEnabled, LOG_MAX_BYTES, LOG_KEEP
    if (!G_LogEnabled)
        return
    ; 单文件到 2MB 先滚动再写（spec §10.2：旧文件轮转、写新、保留 4 个）。
    ; 滚动失败不丢当前这条：照写，错误另记一行。
    if ((err := RotateLog(LOG_PATH, LOG_MAX_BYTES, LOG_KEEP)) != "")
        WriteLine(LOG_PATH, "日志滚动失败：" . err)
    WriteLine(LOG_PATH, s)
}

; ---------------------------------------------------------------------------
;  配置（spec §9 的表；v2 新增 key_* / theme / hotkey_capture / hotkey_review）
;  读 [英语取词] 段；整段缺失或单键缺失都回落到默认值。
;  读取用 Logic.ahk 的 ReadIniValue 而不是 IniRead：本机实测 IniRead 读不了
;  UTF-8 的 ini（中文段名按 GBK 解对不上，静默回落默认值），理由见 Logic.ahk。
; ---------------------------------------------------------------------------
ReadConfig() {
    global CONFIG_PATH, SECTION, VAULT_DEFAULT
    return {
        hotkey:     ReadIniValue(CONFIG_PATH, SECTION, "hotkey", "^!1"),
        ; 三条全局入口（§6.2/§9.1）：呼出速记框 / 直接截图取词 / 打开取词台
        hotkeyCapture: ReadIniValue(CONFIG_PATH, SECTION, "hotkey_capture", "^!2"),
        hotkeyReview:  ReadIniValue(CONFIG_PATH, SECTION, "hotkey_review", "^!3"),
        ; hotkey_new_session 默认空 = 不注册（§9.1 键表）
        hotkeyNewSession: ReadIniValue(CONFIG_PATH, SECTION, "hotkey_new_session", ""),
        ; 框内键（§9.1 的表）：BoxKeys 按这些值注册，C5 接热重载与冲突检测
        keySave:    ReadIniValue(CONFIG_PATH, SECTION, "key_save", "^Enter"),
        keyCancel:  ReadIniValue(CONFIG_PATH, SECTION, "key_cancel", "Escape"),
        keyShot:    ReadIniValue(CONFIG_PATH, SECTION, "key_shot", "^s"),
        keyMore:    ReadIniValue(CONFIG_PATH, SECTION, "key_more", "F2"),
        ; §6.3 的线框图给了 `F3`（原句展开 3 行）—— C5 起正式进 §9.2 的冲突检测范围
        keyContext: ReadIniValue(CONFIG_PATH, SECTION, "key_context", "F3"),
        keyHelp:    ReadIniValue(CONFIG_PATH, SECTION, "key_help", "F1"),
        keyLookup:  ReadIniValue(CONFIG_PATH, SECTION, "key_lookup", "^l"),
        theme:      ReadIniValue(CONFIG_PATH, SECTION, "theme", "dark"),
        ; 开机自启（托盘菜单勾选态；缺键 = 开，老用户升级后行为不变）。
        ; 真实状态是 Startup 里那个 .lnk，这个键只记住用户意图，见 ApplyStartupAutostart。
        autostart:  ReadIniValue(CONFIG_PATH, SECTION, "autostart", "1"),
        ; v3 §7：速记框位置记忆（拖动结束由外壳写回；见 SaveBoxPos / BoxHome）。
        ; `box_pos=auto` = 现状（鼠标右下 + 越界翻转 + 夹进屏幕）
        boxPos:     ReadIniValue(CONFIG_PATH, SECTION, "box_pos", "auto"),
        boxX:       ReadIniValue(CONFIG_PATH, SECTION, "box_x", ""),
        boxY:       ReadIniValue(CONFIG_PATH, SECTION, "box_y", ""),
        ; v21 T5（#160）长文面板：两条入口热键（默认 ^!4 / ^!5，缺键时用代码缺省值；
        ; 本阶段只负责打开面板，整页 OCR / 粘贴链路由 T6 接）。
        hotkeyLongformOcr:   ReadIniValue(CONFIG_PATH, SECTION, "hotkey_longform_ocr", "^!4"),
        hotkeyLongformPaste: ReadIniValue(CONFIG_PATH, SECTION, "hotkey_longform_paste", "^!5"),
        ; v34 T2（#215）：两条 v34 全局键 —— 字幕带拖框 / 开始·停止收句。
        ; 键名与设置页 settings.py 的 HOTKEY_ACTIONS 同名；缺键时用代码缺省值。
        ; **行为**接线不是 T2：拖框归 T4(#217)、收句扳机归 T3(#216)，这里只挂占位回调。
        hotkeyRedrag:       ReadIniValue(CONFIG_PATH, SECTION, "hotkey_redrag", "^!6"),
        hotkeyCaptureArm:   ReadIniValue(CONFIG_PATH, SECTION, "hotkey_capture_arm", "^!7"),
        ; v35 #231（票 #231）：玻璃卡材质轨 —— auto/fake 走假玻璃，real 尝试 DWM 真材质。
        ; 缺键 = auto（#230 spike 未出真材质结论前不押注；真机验收通过再切 real）。
        petGlass:           ReadIniValue(CONFIG_PATH, SECTION, "pet_glass", "auto"),
        ; v21 T5 面板几何（逻辑 px，96dpi 口径；拖动/缩放结束才写回，见 LfSaveGeom）。
        ; 这四键**故意不进 config 热重载的 apply**：几何只在启动读一次（LongformStartup），
        ; 否则 2s 热重载会把用户刚拖到的位置按旧值摆回去（研究 §7.2 的真实风险）。
        longformX:  ReadIniValue(CONFIG_PATH, SECTION, "longform_x", ""),
        longformY:  ReadIniValue(CONFIG_PATH, SECTION, "longform_y", ""),
        longformW:  ReadIniValue(CONFIG_PATH, SECTION, "longform_w", ""),
        longformH:  ReadIniValue(CONFIG_PATH, SECTION, "longform_h", ""),
        ; v22 P2b（spec §9）的分段预览窗几何 `longform_inspector_x/y/w/h` **不进这里**：
        ; 它只由 LongformInspector 自己直读一次（LfInspectorStartup），没有第二份消费方；
        ; 放进来只会变成「读两遍、谁也不用」的重复（review 收口）。
        vaultDir:   ReadIniValue(CONFIG_PATH, SECTION, "vault_dir", VAULT_DEFAULT),
        port:       ReadIniValue(CONFIG_PATH, SECTION, "port", "47800") + 0,
        ; v2 中枢（§9.1）：RPC 端口 + HTTP 端口 + 数据库路径（spool = 数据库同目录）
        hubPort:    ReadIniValue(CONFIG_PATH, SECTION, "hub_port", "47810") + 0,
        webPort:    ReadIniValue(CONFIG_PATH, SECTION, "web_port", "47811") + 0,
        dbPath:     ReadIniValue(CONFIG_PATH, SECTION, "db_path", "%A_ScriptDir%\data\capture.db"),
        pythonExe:  ReadIniValue(CONFIG_PATH, SECTION, "python_exe", "%A_ScriptDir%\.venv\Scripts\pythonw.exe"),
        ; minHeight 仅为启动日志回显；行为阈值由内核自读 config.ini（kernel.py）
        minHeight:  ReadIniValue(CONFIG_PATH, SECTION, "upsample_min_height", "150") + 0,
        ocrTimeout: ReadIniValue(CONFIG_PATH, SECTION, "ocr_timeout_ms", "20000") + 0,
        logEnabled: ReadIniValue(CONFIG_PATH, SECTION, "log_enabled", "1")
    }
}

; %A_ScriptDir% 是 AHK 的变量而不是环境变量，ExpandEnvironmentStrings 不认它，
; 所以先手工替换，再让系统去展开 %LOCALAPPDATA% 这一类真环境变量。
ResolveExe(template) {
    global TOOL_DIR
    s := StrReplace(template, "%A_ScriptDir%", TOOL_DIR)
    buf := Buffer(8192, 0)
    if (DllCall("ExpandEnvironmentStringsW", "Str", s, "Ptr", buf, "UInt", 4096))
        return StrGet(buf, "UTF-16")
    return s
}

; ---------------------------------------------------------------------------
;  热键占用探测（spec §4.1）
;  AHK v2 的 Hotkey() 在被别的程序占用时**不会抛异常**，所以用 OS 级探测：
;  RegisterHotKey 返回 0 即已被占用，随后立刻 UnregisterHotKey 放掉探测。
; ---------------------------------------------------------------------------
HotkeyIsFree(hk) {
    if (!DllCall("RegisterHotKey", "Ptr", 0, "Int", 1, "UInt", hk.mods, "UInt", hk.vk))
        return false
    DllCall("UnregisterHotKey", "Ptr", 0, "Int", 1)
    return true
}

; ---------------------------------------------------------------------------
;  热键表（v2 §9.1/§9.2，C5 起）：**一处定义、一处注册**。
;  每条 = { action(cfg 键名，写进 hotkey-status.json), spec, group("global"|"box"),
;           name(中文点名), fn(回调) }。
;  · 空 spec = 未设置 → 不进表（不注册、不检测；`hotkey_new_session` 默认如此）；
;  · `key_lookup` 的 fn = 0：重查词典的回调是 C4 的票，C5 只把它纳入键表/冲突检测
;    （BoxKeys 里也没注册它），别在这里替 C4 接回调。
; ---------------------------------------------------------------------------
HotkeyEntries() {
    global G_Cfg
    e := []
    if (Trim(G_Cfg.hotkey) != "")
        e.Push({ action: "hotkey", spec: G_Cfg.hotkey, group: "global"
               , name: "呼出速记框", fn: (*) => NewCapture() })
    if (Trim(G_Cfg.hotkeyCapture) != "")
        e.Push({ action: "hotkey_capture", spec: G_Cfg.hotkeyCapture, group: "global"
               , name: "直接截图取词", fn: (*) => ShotHotkey() })
    if (Trim(G_Cfg.hotkeyReview) != "")
        e.Push({ action: "hotkey_review", spec: G_Cfg.hotkeyReview, group: "global"
               , name: "打开取词台", fn: (*) => OpenReview() })
    if (Trim(G_Cfg.hotkeyNewSession) != "")
        e.Push({ action: "hotkey_new_session", spec: G_Cfg.hotkeyNewSession, group: "global"
               , name: "强制新会话", fn: (*) => ForceNewSession() })
    ; v21 T5（#160）长文模式两条入口（规格 §2）：Ctrl+Alt+4 整页 OCR / Ctrl+Alt+5 粘贴；
    ; 本阶段两个回调都只负责打开长文面板（真实链路 T6 接），但键必须进
    ; HotkeyEntries 才享受与旧键同等的冲突检测 / 占用探测 / 热重载重新注册。
    if (Trim(G_Cfg.hotkeyLongformOcr) != "")
        e.Push({ action: "hotkey_longform_ocr", spec: G_Cfg.hotkeyLongformOcr, group: "global"
               , name: "长文整页识别", fn: (*) => LongformHotkeyOcr() })
    if (Trim(G_Cfg.hotkeyLongformPaste) != "")
        e.Push({ action: "hotkey_longform_paste", spec: G_Cfg.hotkeyLongformPaste, group: "global"
               , name: "长文粘贴成文", fn: (*) => LongformHotkeyPaste() })
    ; v34 T2（#215）：统一热键表补两条 —— 字幕带拖框 / 开始·停止收句。进表才有
    ; 冲突检测、OS 占用探测、托盘暂停与 2s 热重载；行为在 T4(#217) / T3(#216) 接线。
    ; 回调不碰三条老手势：呼出框 / 截图取词 / 长文 ^!4·^!5。
    ; 顺序与设置页 settings.py 的 HOTKEY_ACTIONS 一致（状态表逐条对齐，读起来不打架）。
    if (Trim(G_Cfg.hotkeyCaptureArm) != "")
        e.Push({ action: "hotkey_capture_arm", spec: G_Cfg.hotkeyCaptureArm, group: "global"
               , name: "开始/停止收句", fn: (*) => CaptureArmHotkey() })
    if (Trim(G_Cfg.hotkeyRedrag) != "")
        e.Push({ action: "hotkey_redrag", spec: G_Cfg.hotkeyRedrag, group: "global"
               , name: "字幕带拖框", fn: (*) => BandRedragStart() })
    if (Trim(G_Cfg.keySave) != "")
        e.Push({ action: "key_save", spec: G_Cfg.keySave, group: "box"
               , name: "保存", fn: (*) => DoSave() })
    if (Trim(G_Cfg.keyCancel) != "")
        e.Push({ action: "key_cancel", spec: G_Cfg.keyCancel, group: "box"
               , name: "取消", fn: (*) => BoxEsc() })
    if (Trim(G_Cfg.keyShot) != "")
        e.Push({ action: "key_shot", spec: G_Cfg.keyShot, group: "box"
               , name: "截图取词", fn: (*) => ShotFromBox() })
    if (Trim(G_Cfg.keyMore) != "")
        e.Push({ action: "key_more", spec: G_Cfg.keyMore, group: "box"
               , name: "展开/收起元数据", fn: (*) => ToggleMore() })
    if (Trim(G_Cfg.keyContext) != "")
        e.Push({ action: "key_context", spec: G_Cfg.keyContext, group: "box"
               , name: "原句展开 3 行", fn: (*) => ToggleCtxRows() })
    if (Trim(G_Cfg.keyHelp) != "")
        e.Push({ action: "key_help", spec: G_Cfg.keyHelp, group: "box"
               , name: "键位表", fn: (*) => ToggleKeymap() })
    if (Trim(G_Cfg.keyLookup) != "")
        e.Push({ action: "key_lookup", spec: G_Cfg.keyLookup, group: "box"
               , name: "重新查词典", fn: 0 })
    return e
}

; 全局热键注册 + 冲突检测（v2 §6.2/§9.1/§9.2）。
; 流程：摘旧 → 建表 → HotkeyStatuses（解析 + 组内重复 + OS 级占用探测）→ 只注册
; state=ok 的全局键 → 缓存框内屏蔽表 → 原子写 data/hotkey-status.json → 有问题弹托盘气泡。
; 失败一律日志 + 气泡点名（v1 §4.1「不许静默失效」）。热重载会反复调用它。
; 注册用的是 rec.spec 原文（config 的值，含 `~` 也一并原样交给 Hotkey()）——不重拼。
RegisterGlobalHotkeys() {
    global G_Cfg, G_GlobalKeys, G_BoxBlocked, G_HotkeyPaused
    for spec in G_GlobalKeys                       ; 先 Off 旧的（§9.2 热重载语义）
        try Hotkey(spec, "Off")
    G_GlobalKeys := []
    entries := HotkeyEntries()
    statuses := HotkeyStatuses(entries, HotkeyIsFree)
    i := 0
    for rec in statuses {
        i++
        if (rec.group != "global" || !rec.register)
            continue
        try {
            Hotkey(rec.spec, entries[i].fn, G_HotkeyPaused ? "Off" : "On")
            G_GlobalKeys.Push(rec.spec)
            Log("热键已注册：" . rec.spec . "（" . rec.name . "）")
        } catch as err {
            Log("热键注册失败：" . rec.name . " " . rec.spec . " —— " . Type(err) . ": " . err.Message)
        }
    }
    G_BoxBlocked := BoxBlockedMap(statuses)
    WriteHotkeyStatus(statuses)
    AnnounceHotkeyProblems(statuses)
}

; 原子写 data/hotkey-status.json：每条动作一行 JSON；临时文件 + FileMove（§9.2）。
WriteHotkeyStatus(statuses) {
    global G_HotkeyStatusPath
    probedAt := FormatTime(A_Now, "yyyy-MM-dd HH:mm:ss")
    txt := ""
    for rec in statuses
        txt .= HotkeyStatusLine(rec, probedAt) . "`n"
    tmp := G_HotkeyStatusPath . ".tmp"
    try {
        SplitPath(G_HotkeyStatusPath, , &dir)
        if (dir != "" && !DirExist(dir))
            DirCreate(dir)
        ; 先清掉上一轮的临时文件（否则 FileAppend 会追加、写出半旧半新）；文件不在时
        ; AHK v2 的 FileDelete **抛错**，所以这行必须包 try（本机 2026-10-13 实测：
        ; 不包会弹阻塞式错误框，热重载就此卡死）。
        try FileDelete(tmp)
        FileAppend(txt, tmp, "UTF-8-RAW")
        FileMove(tmp, G_HotkeyStatusPath, 1)       ; 1 = 覆盖
        Log("热键状态已写 " . G_HotkeyStatusPath . "（" . statuses.Length . " 条）")
    } catch as err {
        Log("写 hotkey-status.json 失败：" . err.Message)
    }
}

; 冲突点名（§9.2）：只汇总非 ok 的动作，一次气泡说清；日志留痕。
AnnounceHotkeyProblems(statuses) {
    bads := ""
    for rec in statuses {
        if (rec.state = "ok")
            continue
        tag := (rec.state = "dup") ? "重复" : (rec.state = "taken") ? "被占用" : "语法错误"
        bads .= (bads = "" ? "" : "；") . tag . "：" . rec.name . "=" . rec.spec
    }
    if (bads = "")
        return
    Log("热键冲突：" . bads)
    ShowBubble("热键冲突：" . bads . "（改 config.ini 后 2 秒内自动生效）", "英语取词", 3)
}

; v34 T3（#216）：开始/停止收句扳机（默认 ^!7，设置页可改，2 秒热生效）。
; 状态只在中枢内存里（armed）：外壳每次只发一条 toggle、按回话提示；
; **不缓存「已开」意图** —— 中枢不在位时只明说没打开，下次按仍是「从关到开」。
; 回话在 OnHubLine 的 capture_arm 槽里（OnCaptureArmLine）。
CaptureArmHotkey() {
    if (!HubSend('{"cmd":"capture_arm","op":"toggle"}', "capture_arm")) {
        ShowToast("数据中枢不在位，收句没打开", "err")
        Log("热键：开始/停止收句 —— 中枢不在位，未切换")
        return
    }
    Log("热键：开始/停止收句（toggle 已发出，等中枢回话）")
}

; #48（B2）一次性启动提示：呼出键是「裸 Alt 形态」（带 ! 且没写 ~ 放行前缀）时记
; **一行日志**（不弹气泡、不打断——用户没报错时不该被弹窗打断）。机制见
; docs/research/v3-b1-mechanism.md：AHK 注册 Alt 系热键时只吞触发键的 down，浏览器
; 收到一次「裸 Alt」→ Chromium 聚焦右上角「...」应用菜单 → 之后的空格命中它而不是
; 页面（B站 空格播放失效）。`~` 前缀让那次组合原样送达浏览器，从源头去掉裸 Alt。
; 只在启动调一次（热重载不重复刷日志）。
WarnBareAltHotkey() {
    global G_Cfg
    spec := Trim(G_Cfg.hotkey)
    if (!IsBareAltHotkey(spec))
        return
    Log("提示：呼出键 " . spec . " 是 Alt 系且未加 ~ 放行前缀，浏览器可能把焦点给应用菜单按钮"
      . "（之后的空格会失效）；建议改成 ~" . spec
      . "（机制见 docs/research/v3-b1-mechanism.md，#47）")
}

; hotkey_review（§9.1）：打开取词台。hub 的 HTTP 端（W1/W3）就绪后这就是正式入口；
; 票面要求先注册成打开 URL（越界风险已与主代理确认，见票 #28 评论）。
OpenReview() {
    global G_WebPort
    url := "http://127.0.0.1:" . G_WebPort . "/"
    Log("热键：打开取词台 " . url)
    try
        Run(url)
    catch as err
        Log("打开取词台失败：" . err.Message)
}

; hotkey_new_session（§9.1，默认空 = 不注册）：强制新会话 —— 等价于框内 chip 切到
; 「(新会话)」。先按常规呼出速记框，再置标志位（BeginCapture 会把它清零）。
ForceNewSession() {
    global G_SessNew, Bgui
    NewCapture()
    if (Bgui) {
        G_SessNew := true
        RenderChips()
        Log("热键：强制新会话（框内 chip 切到 (新会话)）")
    }
}

; ---------------------------------------------------------------------------
;  主题（§6.3：默认深色 —— 看视频场景；托盘「切换深色」可切，选择落 config）
;  取值就是 §8.1 的视觉令牌：
;    深色 卡片 #14161A / 字段 #1B1E24 / 边 #2A2F36 / chip #252A31 / 正文 #E7E9EC
;         次要 #8C939C / 强调 #7C9CFF
;    浅色 卡片 #FFFFFF / 字段 #F1F3F6 / 边 #E3E6EA / chip #E7EBF0 / 正文 #17191C
;         次要 #6B7280 / 强调 #2F6BFF
;  按钮一律用深字：原生按钮在 Windows 上恒为浅底（v1 的实测结论，§1.3）。
; ---------------------------------------------------------------------------
Theme() {
    global G_Dark
    if (G_Dark)
        return { border: "2A2F36", win: "14161A", field: "1B1E24", chip: "252A31"
               , chipOn: "3A4250", text: "E7E9EC", sub: "8C939C", err: "FF7A7A"
               , accent: "7C9CFF", rule: "252A31" }
    return { border: "E3E6EA", win: "FFFFFF", field: "F1F3F6", chip: "E7EBF0"
           , chipOn: "D5DCE6", text: "17191C", sub: "6B7280", err: "C42B1C"
           , accent: "2F6BFF", rule: "E3E6EA" }
}

; ---------------------------------------------------------------------------
;  出处采集（spec §6.2 —— 必须在弹出任何窗口之前调用）
; ---------------------------------------------------------------------------
CaptureProvenance() {
    hw := WinExist("A")
    if !hw
        return { auto: "", title: "", app: "", hwnd: 0 }
    proc := "", title := ""
    try proc := WinGetProcessName("ahk_id " hw)
    try title := WinGetTitle("ahk_id " hw)
    ; app 是友好应用名：spool 的 `source_app` 与 `session_peek` 的 key 都要它
    ; （标题本身按 §10.2 **不进日志**）。`hwnd` 给票 #47 的焦点还原用（关闭时交回）。
    return { auto: ProvenanceAuto(proc, title), title: title, app: FriendlyApp(proc), hwnd: hw }
}

; ---------------------------------------------------------------------------
;  v21 round2 (B)：长文面板语境的出处采集（Bug 5：session 页面标题 / 取词台联通）
;
;  面板一出现，前台就可能变成**面板自己**（面板内 Ctrl+V、点按钮、RichEdit 子控件）。
;  这时直接用 `CaptureProvenance()` 会把「长文面板 / 某个控件句柄」当来源，session
;  标题就成了我们的窗口名。两条纪律（口径与旧框 §6.2 完全一致，不另造一套）：
;    · 本进程自己的窗口（面板 / 子控件 / 遮罩 / Toast / 速记框）一律不算来源；
;    · 面板开着时用 700ms 低频定时器维护「最近一次非面板前台」，Ctrl+V 退回它。
;  E2E 钩子与真实入口调**同一个**助手（不复制第二套实现，见 LongformCaptureProvenance
;  / PanelFillLongProvenance 的调用点）。
; ---------------------------------------------------------------------------
CapturePanelProvenance() {
    global G_LfGui
    hw := WinExist("A")
    if (!hw)
        return { auto: "", title: "", app: "", hwnd: 0 }
    ; 面板主窗或它的子控件（RichEdit/按钮）→ GetAncestor(GA_ROOT) 回到面板主窗
    if (G_LfGui) {
        root := 0
        try root := DllCall("GetAncestor", "Ptr", hw, "UInt", 2, "Ptr")   ; GA_ROOT
        if (hw = G_LfGui.Hwnd || (root && root = G_LfGui.Hwnd))
            return { auto: "", title: "", app: "", hwnd: 0 }
    }
    ; 同进程的其他窗口（遮罩 / Toast / 速记框）也不是来源
    pid := 0
    try pid := WinGetPID("ahk_id " hw)
    if (pid && pid = DllCall("GetCurrentProcessId", "UInt"))
        return { auto: "", title: "", app: "", hwnd: 0 }
    proc := "", title := ""
    try proc := WinGetProcessName("ahk_id " hw)
    try title := WinGetTitle("ahk_id " hw)
    return { auto: ProvenanceAuto(proc, title), title: title, app: FriendlyApp(proc), hwnd: hw }
}

; 记住「最近一次非面板前台」（只认带标题的外部窗口）。缓存刷新与粘贴入口共用 ——
; 调用方先自己判空，这里只做写入，语义单点：「非空标题才进缓存」。
PanelRememberProv(prov) {
    global G_PanelProvLast
    G_PanelProvLast := { title: prov.title, app: prov.app, hwnd: prov.hwnd }
}

; 面板开着时的低频缓存刷新（LfBuild 挂 / LfDestroyWindow 摘）。面板自己在前台时
; CapturePanelProvenance 回空 → 缓存保持不变，不被自己的窗口覆盖。
PanelProvCacheTick() {
    prov := CapturePanelProvenance()
    if (NormalizeLine(prov.title) = "")
        return
    PanelRememberProv(prov)
}

; 粘贴入口（^!5 打开前 / 面板内 Ctrl+V）统一填 G_LongTitle/G_LongApp/G_LongUrl。
; 前台是外部窗口就直接用它；是面板自己就退回「最近非面板前台」缓存。
; URL 不在 provenance 采集口径里（旧框的 URL 来自 hub 的 browser context），
; 这里保持空 —— 不为它新增窗口/浏览器依赖（规格 §6.2 的边界）。
PanelFillLongProvenance() {
    global G_PanelProvLast, G_LongTitle, G_LongApp, G_LongUrl
    prov := CapturePanelProvenance()
    if (NormalizeLine(prov.title) != "")
        PanelRememberProv(prov)
    if (NormalizeLine(G_PanelProvLast.title) != "")
        G_LongTitle := G_PanelProvLast.title, G_LongApp := G_PanelProvLast.app
    else
        G_LongTitle := "", G_LongApp := ""
    G_LongUrl := ""
}

; ---------------------------------------------------------------------------
;  Toast（spec §4.4 / #10 定稿）：自定义 no-activate 胶囊，1.2s 自消、不出声、
;  不抢焦点。虚拟屏右下角定位，避开任务栏（票 #184（A6）前只量主屏）。
;  票 #185（A14）：1.2s 内连发不再互相覆盖 —— 活动 toast 进 G_Toasts 队列纵向
;  堆叠（新枚在最上层），自灭后其余枚下移补位；落点算术在 Logic.ToastStackY
;  （纯函数，harness 片 43 钉）。
; ---------------------------------------------------------------------------
ShowToast(text, kind := "ok") {
    global G_Toasts, LP
    ; 注意：AHK 变量名不分大小写，局部变量绝不能叫 t / T / d / D（会盖掉全局）
    tg := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000")
    tg.BackColor := (kind = "err") ? "8C2F2F" : "1F1F1F"
    tg.MarginX := P(14), tg.MarginY := P(8)
    tg.SetFont("s11 cFFFFFF", "Segoe UI")
    tg.AddText(, (kind = "err") ? "⚠ " . text : text)
    tg.Show("AutoSize NoActivate Hide")
    WinSetTransparent(LP.TOAST_A, tg)
    tg.GetPos(, , &w, &h)
    slot := { gui: tg, w: w, h: h }
    G_Toasts.Push(slot)
    ToastRelayout()
    tg.Show("NoActivate")
    RoundCorners(tg.Hwnd)
    SetTimer(() => ToastDismiss(slot), -1200)
}

; toast 的自灭收口（票 #185 / A14）：摘出活动队列 → 销毁 → 其余枚重排补位。
; 可能跑在 socket 的 Critical 回调里（KernelDied 直呼 ShowToast 的那一族），
; 全程只做数组操作与 Move，不阻塞。
ToastDismiss(slot) {
    global G_Toasts
    i := 0
    for s in G_Toasts {
        i++
        if (s = slot) {
            G_Toasts.RemoveAt(i)
            break
        }
    }
    try slot.gui.Destroy()
    ToastRelayout()
}

; 活动 toast 的落点（票 #185 / A14）：虚拟屏右下、自底向上纵向堆叠。新枚排到
; 队尾（最上层）；某枚出队后同一套落点重算，上面的枚下移补位。
ToastRelayout() {
    global G_Toasts, LP
    if (G_Toasts.Length = 0)
        return
    vr := VirtualScreenRect()
    heights := []
    for s in G_Toasts
        heights.Push(s.h)
    ys := ToastStackY(heights, P(LP.TOAST_GAP), vr.y + vr.h - P(LP.TOAST_B))
    i := 0
    for s in G_Toasts {
        i++
        s.gui.Move(vr.x + vr.w - s.w - P(LP.TOAST_R), ys[i])
    }
}

; ---------------------------------------------------------------------------
;  托盘气泡（TrayTip）。决策 D10：启动 / 自检 / 守护类消息用**托盘气泡**
;  （spec §4.1 / §11 的措辞就是「托盘气泡」）；toast 是捕获回执专属（#12 已定）。
;  气泡一律同时写日志 —— Windows 的气泡可能被通知设置吞掉，日志是保底。
; ---------------------------------------------------------------------------
ShowBubble(text, title := "英语取词", icon := 1) {
    Log("托盘气泡：" . text)
    try {
        TrayTip(text, title, icon)
    } catch as err {
        Log("TrayTip 失败：" . err.Message)
    }
}

; 托盘图标：正常 = #10 自绘蓝底横线；灰阶 = 内核或中枢不在位（spec §11 / v2 §6.5）；
; v34 T5 起多两档收句态（收句中 = 蓝底红点 / 异常 = 蓝底琥珀点）—— **灰的语义不变**：
; 决策 D9 的「灰只表达有组件不在位」仍由 `TrayApplyIcon` 单独一条轴管。
SetTrayIcon(kind) {
    global ICON_NORMAL, ICON_OFF, ICON_REC, ICON_ALERT
    icon := ICON_NORMAL
    if (kind = "off")
        icon := ICON_OFF
    else if (kind = "rec")
        icon := ICON_REC
    else if (kind = "alert")
        icon := ICON_ALERT
    try {
        TraySetIcon(icon)
    } catch as err {
        Log("切换托盘图标失败：" . err.Message)
    }
}

; 托盘图标的最终选择：先是「组件在不在位」（灰），再是收句三态。
; 灰优先 —— 中枢都不在了，收句状态没有意义。
TrayApplyIcon() {
    global G_TrayGrayed, G_TrayMode
    SetTrayIcon(G_TrayGrayed ? "off" : G_TrayMode)
}

; 置灰 / 复原的唯一入口：托盘图标是用户从界面上唯一能看出「内核/中枢在不在位」的信号
; （spec §11 / v2 §6.5），所以每次真的变化都记一条日志 —— 出问题时日志里得有痕迹。
SetTrayGray(gray) {
    global G_TrayGrayed
    if (gray = G_TrayGrayed)
        return
    G_TrayGrayed := gray
    TrayApplyIcon()
    Log("托盘图标" . (gray ? "置灰（内核或中枢不在位）" : "复原（内核与中枢都在位）"))
}

; 托盘图标的**唯一判据**（v2 §6.5：蓝 = 内核 + 中枢都在位；灰 = 任一不在位）。
; 内核侧原有的四个调用点（握手 pong / 判死 / 握手超时 / 拉不起来）都改走这里 ——
; 各自写死 false/true 会在中枢离线时被内核的「已就绪」误刷回蓝。
TraySync() {
    global G_Ready, G_HubUp
    SetTrayGray(!G_Ready || !G_HubUp)
}

; 收句托盘三态（v34 T5 / spec §C25）：收句中 / 待命 / 异常。纯函数，便于源码钉。
CaptureTrayMode(armed, ocr, ageMs) {
    if (!armed)
        return "normal"
    if (ocr = "redrag_needed" || ocr = "failed")
        return "alert"
    if (ageMs < 0 || ageMs > CAPTURE_HEARTBEAT_STALE_MS)
        return "alert"
    if (ocr = "active")
        return "rec"
    return "normal"
}

; ---------------------------------------------------------------------------
;  协议（spec §5.3）：新行分隔 JSON。AHK 没有内建 JSON，这里只解内核会发的
;  扁平对象，字段就五个：pong / ok / text / error / saved。不写通用解析器。
;  `UnescapeJson` 搬到 lib\Logic.ahk（`JsonField` 与 `ParseCorrections` 共用）。
; ---------------------------------------------------------------------------
JsonField(json, name) {
    ; v22 P1a（票 #170）：捕获组必须是 **possessive** `*+`。原写法 `(?:[^"\\]|\\.)*`
    ; 在 AHK v2.0.30 的 PCRE2 上对长字符串（实测 ≥~3KB，如文章行框编码、16KB 正文块）
    ; 抛 `PCRE execution error`（回溯撞 match limit）→ 整条长文链路解析失败。
    ; possessive 只收紧回溯：合法 JSON 的字符串在第一个未转义 `"` 处结束，语义不变。
    if (!RegExMatch(json, '"' . name . '"\s*:\s*"((?:[^"\\]|\\.)*+)"', &m))
        return ""
    return UnescapeJson(m[1])
}

; v21 T5 阶段2：长文入口要问「旧框是否正占着内核」（内核一次只处理一条请求）。
; 这个判定放主脚本里：面板模块**不碰** G_Pending，避免两条流程的标志被焊在一起。
KernelBusy() {
    global G_Pending
    return (G_Pending ? true : false)
}


; pong / ms 是数字而不是字符串，字符串那条正则抓不到
JsonNumber(json, name) {
    if (!RegExMatch(json, '"' . name . '"\s*:\s*(-?\d+)', &m))
        return 0
    return m[1] + 0
}

; 布尔字段。中枢的回话里 `"dedup":true` 这种，JsonField（只管字符串）与
; JsonNumber（只管数字）都抓不到 —— 少了它 toast 会永远说「已存 → 词」，
; 重复词的那句「本会话第 N 次」（§6.1）就永远不出现。
JsonBool(json, name) {
    return RegExMatch(json, '"' . name . '"\s*:\s*true') ? true : false
}

; ---------------------------------------------------------------------------
;  端口探测客户端（内核/中枢通用，票 #29）：只连一下、发一枚 ping、断开，**不处理
;  回话**。不能用 KernelClient/HubClient 干这事 —— 它们的 onRecv 会把任意回话当
;  成正式回话（拿 KernelClient 探中枢端口会误置「内核就绪」）。基类没定义
;  onRecv/onDisconnect，所以裸 SocketTCP 收到 FD_READ 会抛「方法不存在」，必须给
;  一个空的子类把这两个钩子接住。
; ---------------------------------------------------------------------------
class ProbeClient extends SocketTCP {
    onRecv() {
    }
    onDisconnect() {
    }
}

; ---------------------------------------------------------------------------
;  内核客户端：Socket.ahk 的事件回调。接收发生在 OnMessage 里，主线程不阻塞
;  —— 这是 ADR 0002 的硬要求（阻塞读取会连带冻死全局热键）。
;  非阻塞读法 + 自攒行缓冲：读到多少算多少，回调立刻返回（本机实测踩过坑）。
; ---------------------------------------------------------------------------
class KernelClient extends SocketTCP {
    Buf := ""

    onRecv() {
        chunk := this.RecvText()          ; 只取当前已到达的字节，永不等待
        if (chunk = "")
            return
        this.Buf .= chunk
        while (nl := InStr(this.Buf, "`n")) {
            line := RTrim(SubStr(this.Buf, 1, nl - 1), "`r")
            this.Buf := SubStr(this.Buf, nl + 1)
            OnKernelLine(line)
        }
    }
    onDisconnect() => OnKernelLost()
}

; socket 辅助（等价提取，收发语义不变）：ping 的发送形状（握手 Tick / 端口
; 探测共用）与断开的形状（重启 / 重连 / 退出共用）各收敛到一个函数，避免
; 同一个 try+清句柄在三处写歪。
SendPing(sock) {
    sock.SendText('{"cmd":"ping"}' . "`n")
}

; 断开并清空 G_Sock。自己主动断开**不触发** onDisconnect（Socket.ahk 先摘
; 回调再关），所以要走判死逻辑的地方必须显式调用 KernelDied。
DisconnectSock() {
    global G_Sock
    if (G_Sock) {
        try G_Sock.Disconnect()
        G_Sock := 0
    }
}

OnKernelLine(line) {
    ; 票 #185（A9）：G_KernelProbePending（存活 ping 的在途位）归零写在这里
    global G_Ready, G_Pending, G_PendingAt, G_WatchPhase, G_TrayGrayed, G_ToldNotReady
    global G_KernelProbePending
    if (InStr(line, '"pong"')) {
        if (!G_Ready) {
            G_Ready := true
            ; 握手成功：回存活、图标复原（spec §11）；死后续握与首次就绪的
            ; 文案不同，用 G_TrayGrayed 区分（WatchdogStep 的 recover 事件）
            step := WatchdogStep(G_WatchPhase, G_TrayGrayed ? "recover" : "ready")
            G_WatchPhase := step.phase
            TraySync()                      ; 内核在位 → 图标复原（spec §11 / §6.5）
            Log("内核就绪（握手 pong " . JsonNumber(line, "pong") . "ms）")
            ; UI 与日志都推迟一拍：这个回调跑在 Critical 线程里，尽快返回
            SetTimer(() => ShowToast(step.toast = "recovered" ? "OCR 内核已恢复" : "OCR 内核已就绪"), -1)
            SetTimer(Tick, 0)
            SetTimer(Watch, WATCH_MS)
            SetTimer(KernelProbe, KERNEL_PROBE_MS)   ; 票 #185（A9）：就绪后转低频存活探测
            G_ToldNotReady := false         ; 下一次判死后的超时气泡可以再报
        } else {
            G_KernelProbePending := 0       ; 票 #185（A9）：存活 ping 有着落了
        }
        return
    }
    ; v21 T5 阶段2：长文链路有自己的在途标志（独立 G_LongPending，**绝不共用 G_Pending**；
    ; RunShot 的守卫只认 G_Pending）。这条分流必须排在旧框那条之前。
    if (G_LongPending != "") {
        LfOnKernelLine(line)
        return
    }
    if (!G_Pending)
        return
    G_Pending := false
    waited := A_TickCount - G_PendingAt
    if (InStr(line, '"ok": true') || InStr(line, '"ok":true')) {
        text := JsonField(line, "text")
        corr := ParseCorrections(line)        ; §5.2 的后校正清单（状态行「已校正 N 处」）
        ; v17 W5（票 #142）：疑似粘连只读标记（旧内核无此字段 → 空数组 → 无标记）
        suspects := ParseSuspects(line)
        ; 只记长度与处数不记内容（spec §10.2）
        Log("捕获成功 外壳测得 " . waited . "ms、内核报 " . JsonNumber(line, "ms") . "ms，文本 "
          . StrLen(text) . " 字，校正 " . corr.Length . " 处，疑似粘连 " . suspects.Length . " 处")
        SetTimer(() => FillFromOcr(text, corr, suspects), -1)
    } else {
        errText := JsonField(line, "error")
        Log("捕获失败 " . waited . "ms：" . errText)
        saved := JsonField(line, "saved")
        SetTimer(() => OcrFailed(errText, saved), -1)
    }
}

; ---------------------------------------------------------------------------
;  判死 → 重启（spec §11：连接失败或请求超时即判内核已死 → 自动重启一次 +
;  toast「OCR 内核已重启，请再按一次」+ 托盘图标变灰；等待期间外壳始终可响应）
;  决策 D8：**每次**判死都重启一次（不是全程只一次）；重启后仍握手失败就回到
;  #12 的 300ms×10s 重试节奏，不追加重启（防重试风暴）。
;  重启/图标/提示的判定在 Logic.ahk 的 WatchdogStep / RestartPhase（有测试钉住）。
;
;  票 #69：这个函数可能跑在 Socket.ahk 的 OnMessage 回调里（第一行就是 `Critical`），
;  所以**只置位**：判死位 + 一枚一次性拍子，真正的重启在 RestartKernelTick 里跑
;  （taskkill / netstat 的 RunWait 在那儿才不会冻住热键与托盘）。
; ---------------------------------------------------------------------------
KernelDied(reason) {
    try {
        global G_WatchPhase, G_Pending, G_TrayGrayed, G_RestartN, G_RestartPending
        global G_LongBusy, G_LongPending
        step := WatchdogStep(G_WatchPhase, "death")
        G_WatchPhase := step.phase
        told := false
        if (G_Pending) {
            ; 在途请求作废：速记框关闭。「请再按一次」由这条 abort 的 toast 说，
            ; 本函数就不再发第二枚（两枚 toast 会叠在一起）
            G_Pending := false
            told := true
            SetTimer(() => AbortCapture("OCR 内核已重启，请再按一次"), -1)
        }
        ; 票 #184（A4）：内核死了，长文链路的在途标志一并收口 —— 不然 20s 内一切
        ; 截图入口被「长文识别进行中」拒绝（LongformInFlight 判 busy/pending），
        ; 最后还弹一枚与事实不符的「长文请求超时」。判据与三入口同源；没有长文
        ; 在途就不走 LfLongFail（免得每次判死都白弹一枚失败 toast）。
        ; OnKernelLost（连接断开）也汇到本函数，同一处覆盖。
        if (LongformInFlight(G_LongBusy, G_LongPending))
            LfLongFail("内核连接断开")
        g := RestartPhase(G_RestartPending, "death")
        G_RestartPending := g.pending
        if (g.dispatch) {
            G_RestartN++
            Log("判死并重启（第 " . G_RestartN . " 轮）：" . reason)
            SetTimer(RestartKernelTick, -1)     ; 一次性拍子：Critical 段结束后才跑
        } else
            Log("判死（重启已在队列里，不叠加）：" . reason)
        if (step.icon = "gray")
            TraySync()
        if (step.toast != "" && !told)
            ShowToast("OCR 内核已重启，请再按一次", "err")
    } catch as err {
        ; 判死路径自己不能崩：它可能跑在 socket 的 Critical 回调里
        Log("判死处理异常：" . err.Message)
    }
}

; 判死的那枚一次性拍子（票 #69）：真正重启在这一层，**别再往回调里搬回去**。
; 跑完（哪怕抛错）用 RestartPhase 的 "done" 复位重启位，下一轮判死才能再重启
; （D8）；不复位会让内核判死一次之后就永不再重启。
RestartKernelTick() {
    global G_RestartPending
    try {
        RestartKernel()
    } finally {
        g := RestartPhase(G_RestartPending, "done")
        G_RestartPending := g.pending
    }
}

; 重启内核：断开旧连接、结束自己拉起的那个（连进程树）、对端口做一次接管
; （决策 D12：万一还有外部内核占着 47800，先驱逐再拉起）、拉起新的、回到
; 300ms 重试节奏。
; 会阻塞：StopKernel 的 `taskkill`（RunWait）+ EvictPortSquatter 的 `netstat`
; （RunWait）+ 等端口释放的 Sleep 循环都在这儿。所以它**只能**从一次性拍子里
; （RestartKernelTick）或托盘点击里调，绝不能从 socket 的 Critical 回调直接调
; （票 #69）——热键与托盘的响应性由「回调只置位」保证，不是靠这个函数不阻塞。
; 返回 false = 新内核没拉起来（解释器不存在等，StartKernel 已记日志）。
RestartKernel() {
    global G_Ready, G_Pending, G_Deadline, G_ToldNotReady, G_WatchPhase
    global G_KernelProbeSent, G_KernelProbePending
    G_Ready := false
    DisconnectSock()
    SetTimer(Watch, 0)
    SetTimer(KernelProbe, 0)         ; 票 #185（A9）：重试期回 Tick 的握手节奏，存活探测收摊
    G_KernelProbeSent := 0
    G_KernelProbePending := 0
    StopKernel()                     ; 结束自己拉起的那个内核（单所有者）
    EvictPortSquatter(G_Port, "内核")   ; 端口上若还有外部内核，驱逐后由我们拉起（D12）
    ok := StartKernel()
    G_WatchPhase := "retrying"
    G_Deadline := A_TickCount + PING_TIMEOUT
    G_ToldNotReady := false          ; 新一轮握手，超时气泡可再报一次
    SetTimer(Tick, PING_INTERVAL)
    return ok
}

; ---------------------------------------------------------------------------
;  Tick：握手 / 重连，PING_INTERVAL 一拍。内核迟到也能自愈，不崩。
; ---------------------------------------------------------------------------
Tick() {
    ; G_Sock 必须在这里声明：AHK v2 里「赋值给没声明 global 的名字」= 造一个局部变量，
    ; 全局那个 G_Sock 会一直是 0 —— RunShot 的 `!G_Sock` 守卫于是永久拒绝截图，
    ; 而日志只说「内核未就绪」（2026-10-12 的 D12 重构掉过一次，见 tests/test_shell_source.py）
    global G_Ready, G_Sock, G_Port, G_Deadline, G_ToldNotReady, G_WatchPhase
    if (G_Ready)
        return
    if (A_TickCount > G_Deadline && !G_ToldNotReady) {
        G_ToldNotReady := true
        step := WatchdogStep(G_WatchPhase, "handshake-timeout")
        G_WatchPhase := step.phase
        ; 握手超时同样意味着「内核不在位」，图标要灰 —— 否则用户从托盘看不出
        ; 内核压根没起来（WatchdogStep 的 icon 有测试钉住，这里必须消费它）
        if (step.icon = "gray")
            TraySync()
        Log("握手超时（" . (PING_TIMEOUT // 1000) . "s）仍未就绪，继续按 " . PING_INTERVAL . "ms 重试")
        ; 决策 D10：启动 / 自检类消息用托盘气泡（#12 这里是 toast）
        ShowBubble("OCR 内核未就绪，仍在重试（详见 logs\app.log）", "英语取词", 3)
    }
    DisconnectSock()
    try {
        G_Sock := KernelClient()
        G_Sock.Connect(["127.0.0.1", String(G_Port)])
        SendPing(G_Sock)
    } catch {
        ; 内核还在冷启动（0.6–1.8s），下一拍再试
    }
}

; ---------------------------------------------------------------------------
;  Watch：就绪后在途请求的超时判死（spec §5.3 / §11）。外壳不阻塞，等内核的
;  时候热键始终可用。
; ---------------------------------------------------------------------------
Watch() {
    global G_Ready, G_Pending, G_PendingAt, G_OcrTimeout
    if (!G_Ready || !G_Pending)
        return
    waited := A_TickCount - G_PendingAt
    if (waited > G_OcrTimeout)
        KernelDied("请求 " . G_OcrTimeout . "ms 无响应")
}

; ---------------------------------------------------------------------------
;  票 #185（A9）：内核就绪后的低频存活探测（照 HubWatch 的模式：定时 ping + 在途
;  超时判死）。就绪前是 Tick（300ms 握手重连）；就绪后 Tick 收摊，这里接棒 ——
;  「挂而不死」（进程在、不回话也不断开）的内核只有靠探测才判得出来，否则托盘
;  一直蓝、直到下一次截图挂在 G_OcrTimeout 才有反应。频率从俭：KERNEL_PROBE_MS
;  一枚（hub 是 1s；内核侧远比 hub 重，没必要同频）。调度判定在
;  Logic.KernelProbeStep（纯函数，harness 片 39 钉）：OCR/长文在途期间既不发也
;  不判死 —— 内核忙起来 pong 会迟到（引擎调用占着事件循环），墙钟判死是
;  Watch / LfLongWatch 的事，探测不越权；等在途结束，若 pong 一直没来下一拍
;  照样判得出来。判死走既有 KernelDied 收口（#184 的长文收口原样生效）。
; ---------------------------------------------------------------------------
KernelProbe() {
    global G_Ready, G_Sock, G_KernelProbeSent, G_KernelProbePending, G_Pending, G_LongPending
    global KERNEL_PROBE_MS, KERNEL_PROBE_TIMEOUT
    if (!G_Ready)
        return
    now := A_TickCount
    inflight := (G_Pending || G_LongPending != "")
    eff := KernelProbeStep(G_KernelProbePending, G_KernelProbeSent, now, KERNEL_PROBE_MS, KERNEL_PROBE_TIMEOUT, inflight)
    if (eff = "dead") {
        G_KernelProbePending := 0
        KernelDied("存活探测 " . KERNEL_PROBE_TIMEOUT . "ms 无响应")
        return
    }
    if (eff != "ping")
        return
    try {
        SendPing(G_Sock)
    } catch {
        ; 发送失败 = 连接已坏：直接判死（FD_CLOSE 若迟迟不来，这里是兜底收口）
        KernelDied("存活探测发送失败")
        return
    }
    G_KernelProbeSent := now
    G_KernelProbePending := now
}

; 连接被对端断开（内核进程死了 / 崩了）：也是一次判死，走同一条重启路
OnKernelLost() => KernelDied("与内核的连接断了")

; ===========================================================================
;  中枢客户端（v2 §2 / §6.1）：RPC 47810，best-effort
;
;  中枢**永远不是保存路径的必要条件**：一条记录落 spool 就算成功。这里的全部
;  失败路径都只做三件事 —— chip 退化成「（离线）」、托盘置灰、按需重连，绝不
;  弹窗挡路、绝不重试风暴。发送在热键线程里、接收在 Socket.ahk 的 OnMessage
;  回调里（与内核客户端同一套非阻塞读法）。
; ===========================================================================
class HubClient extends SocketTCP {
    Buf := ""

    onRecv() {
        chunk := this.RecvText()          ; 只取当前已到达的字节，永不等待
        if (chunk = "")
            return
        this.Buf .= chunk
        while (nl := InStr(this.Buf, "`n")) {
            line := RTrim(SubStr(this.Buf, 1, nl - 1), "`r")
            this.Buf := SubStr(this.Buf, nl + 1)
            OnHubLine(line)
        }
    }
    onDisconnect() => HubDied("与中枢的连接断了")
}

; 取一条可用的连接（没有就新连一条）。连不上返回 0 —— 调用方按「不在位」处理。
; 自己主动断开不会触发 onDisconnect（Socket.ahk 先摘回调再关），所以 HubDown
; 是「连接没了」的唯一入口。
HubSock() {
    global G_HubSock, G_HubPort
    if (G_HubSock)
        return G_HubSock
    ; 2026-11-10：**这里不加「端口探活」**。试过加，结果是 HubWatch 永远连不上：
    ; 探活（TcpListenInfo）一旦抛异常会冒泡到定时器回调，定时器就此停摆，
    ; 外壳再也不会发现中枢就绪。连接路径只做连接。
    ; 启动慢的账另有去处（启动路径的 netstat / 待挑轮询的 WinHttp，见 PortListening）。
    try {
        sock := HubClient()
        sock.Connect(["127.0.0.1", String(G_HubPort)])
        G_HubSock := sock
        return sock
    } catch {
        return 0
    }
}

; 发一条 RPC（一行 JSON）。返回 true = 已发出（回话随后到 OnHubLine）；
; false = 中枢不在位（已置灰 + 日志）。`ask` 决定回话怎么处理，按发出顺序排队。
; 票 #70：队列元素是 `{ kind, gen }`（原来是裸字符串）——`gen` 只有 peek 用得上
; （回话归属门，见 OnPeekLine），其余 kind 恒 0。
HubSend(request, ask, gen := 0) {
    global G_HubSock, G_HubAsks, G_HubPort, G_HubSeq
    if (!HubSock()) {
        HubDown("RPC 连不上 127.0.0.1:" . G_HubPort)
        return false
    }
    stamp := HubStampRequest(request, G_HubSeq)
    G_HubSeq := stamp.seq
    try {
        G_HubSock.SendText(stamp.line . "`n")
    } catch as err {
        HubDown("发送失败：" . err.Message)
        return false
    }
    HubNoteShortWrites()
    G_HubAsks.Push({ id: stamp.seq, kind: ask, gen: gen })
    return true
}

; 中枢在位：**收到回话**才算（TCP 连上只说明端口有人在听，§6.5 的蓝要真的通）。
; 票 #29：这也是「重连恢复」的落点 —— 相位回 alive、托盘按 TraySync 复原（内核
; 也在位才真变蓝）；此前不在位过就给一枚 toast（首个就绪不弹，免得启动时刷屏）。
HubUp() {
    global G_HubUp, G_HubWatchPhase, G_HubProbePending, G_HubEverUp, G_Status
    global G_HubProbeStrikes
    G_HubProbePending := 0                      ; 在途 ping 有着落了
    G_HubProbeStrikes := 0                      ; 有回话：连续超时计数归零
    if (G_HubUp)
        return
    G_HubUp := true
    ; 票 #184（A5）：恢复即清「数据中枢未就绪(已暂存)」状态行 —— 它只在 hub 不
    ; 在位时由 DoSave 置上，恢复后与「已存 → 词」的回执 toast 自相矛盾（一次框
    ; 会话内原本永不清除）。判据在 Logic.HubUpClearsStatus（纯函数，harness 片 37
    ; 钉）；SetStatus 对同值早退，其它状态原样经过、不触发重排。
    SetStatus(HubUpClearsStatus(G_Status))
    step := WatchdogStep(G_HubWatchPhase, "ready")
    G_HubWatchPhase := step.phase
    recovered := G_HubEverUp
    G_HubEverUp := true
    TraySync()
    Log("中枢就绪（RPC 已回话）" . (recovered ? "（此前不在位，已自动恢复）" : ""))
    ; 2026-11-10：中枢重启会把它内存里的 armed 清零 —— 用户「同意收句」白点了，
    ; 而视频还在播。恢复后静默重放一次「开」（见 CaptureArmReplay）。
    if (recovered)
        CaptureArmReplay()
    if (recovered)
        SetTimer(() => ShowToast("数据中枢已恢复"), -1)
}

; 中枢不在位（连不上 / 发送失败 / 连接断了）：置灰 + 丢弃旧连接与在途命令；
; 在途的 peek 让 chip 退化成「本地标题（离线）」（§6.1），在途的 flush 不管 ——
; spool 已经落盘，hub 起来后会自己 ingest（§2）。
; 这是**状态清理**，不含重启决策（重启走 HubDied → RestartHub）。
HubDown(reason) {
    global G_HubSock, G_HubUp, G_HubAsks, G_SessOffline, Bgui, G_HubProbePending, G_HubPongLogged, G_CaptureArmed
    G_HubProbePending := 0                      ; 在途 ping 的连接没了，别再等它
    if (G_HubSock) {
        try G_HubSock.Disconnect()
        G_HubSock := 0
    }
    asks := G_HubAsks
    G_HubAsks := []
    for a in asks {
        kind := ObjProp(a, "kind")
        if (kind = "peek") {
            G_SessOffline := true
            if (Bgui)
                RenderChips()
        } else if (kind = "lookup") {
            ; 查询在途时中枢掉了：回话永远不来 → 撤掉「词典查询中…」，别让它一直挂着
            SetDictWait(false)
        } else if (kind = "article" || kind = "lf_locate" || kind = "lf_lookup" || kind = "lf_outline") {
            ; 长文在途回话不会来了：只让本次长文降级（词仍可手打/保存），不判死内核
            LfOnHubDown()
        } else if (kind = "lf_paragraphs" || kind = "lf_resegment") {
            ; v22 P2b：分段预览窗的在途回包不会来了 —— 只让预览窗降级，不动别的状态
            LfInspectorOnHubDown()
        }
    }
    G_HubUp := false
    G_HubPongLogged := false           ; 下一次在位期再记一条 pong（恢复的证据）
    ; v34 T5（票 #218）：中枢不在位 → 收句状态影子清空，tooltip 说「中枢不在位」，
    ; 不让上一拍的「收句中」冻在托盘上。
    CaptureStatusReset()
    ; v34 T3（#216）：中枢重启后内存态 armed 丢失 —— 本地缓存一并清零，
    ; 下次按键就是「从关到开」，不会让用户以为还在采。
    if (G_CaptureArmed) {
        G_CaptureArmed := false
        Log("中枢不在位：收句扳机已重置为关（中枢重启后需再按一次）")
    }
    TraySync()
    Log("中枢不在位：" . reason . "（保存照走 spool，词不丢）")
}

; ===========================================================================
;  中枢生命周期（票 #29；与内核同一套语义 —— 外壳是 kernel 与 hub 的唯一所有者，
;  spec §2 / 决策 D12）
;
;  · 启动/重启：端口接管（有外来者先驱逐）→ `pythonw hub.py`（工作目录 = 工具目录）
;    → 交给 HubWatch 定时探测握手；
;  · 判死：连接断 / 探测超时 → HubDown 清理 + **存活期判死重启一次**（HubDeathAction
;    的相位机；重试期再死不重启、不刷 toast，防风暴）；
;  · 重连：探测永远在跑 —— hub 回来后收到 pong 即 HubUp → 托盘复原，**不要求用户
;    重启外壳**（这是 #29 要修的缺口）；
;  · 退出：OnExitHandler 里连进程树 taskkill /F /T。
;  拉不起来（解释器缺失等）显式提示（气泡 + 日志），但**绝不影响 spool 保存**。
; ===========================================================================

; 探测用的静默发 ping：连不上就静默等下一拍（与 kernel 的 Tick 同款）。
; 不能走 HubSend —— 它在失败时会 HubDown + 记日志，hub 没起来的每秒都会刷屏。
HubProbeSend() {
    global G_HubSock, G_HubAsks, G_HubSeq
    s := HubSock()                              ; 静默：连不上返回 0（HubSock 自己吞了异常）
    if (!s)
        return false
    stamp := HubStampRequest(HubPingRequest(), G_HubSeq)
    G_HubSeq := stamp.seq
    try {
        s.SendText(stamp.line . "`n")
    } catch {
        try s.Disconnect()
        G_HubSock := 0
        return false
    }
    HubNoteShortWrites()
    G_HubAsks.Push({ id: stamp.seq, kind: "ping", gen: 0 })
    return true
}

; 票 #178：发生过 TCP 短写（已自动补发）就记一行 —— 补发本身无害，但它是
; 「中枢侧有积压」的信号，排障时能把「短写 → 粘行 → 队列错位」这条链留痕。
; Socket.ahk 只计数（不依赖本文件的 Log），由这里决定要不要说。
HubNoteShortWrites() {
    global G_HubSock, G_HubShortSeen
    if (!IsObject(G_HubSock))
        return
    if (G_HubSock.ShortWrites <= G_HubShortSeen)
        return
    G_HubShortSeen := G_HubSock.ShortWrites
    Log("中枢请求发生过 TCP 短写（累计 " . G_HubShortSeen . " 次，已自动补发）")
}

; 中枢存活探测（票 #29：与 kernel 的 Tick/Watch 同一套手法 —— 定时 ping + 在途
; 超时判死）。由 SetTimer(HubWatch, HUB_PROBE_MS) 驱动，启动时也立刻跑一次。
; 调度判定在 Logic 的 HubProbeStep（纯函数，harness 钉住）。
HubWatch() {
    global G_HubProbeSent, G_HubProbePending, G_HubWatchPhase, G_HubToldNotReady, G_HubDeadline
    global G_HubProbeStrikes, HUB_DEAD_STRIKES
    global HUB_PROBE_MS, HUB_PROBE_TIMEOUT, HUB_START_TIMEOUT
    now := A_TickCount
    eff := HubProbeStep(G_HubProbePending, G_HubProbeSent, now, HUB_PROBE_MS, HUB_PROBE_TIMEOUT)
    if (eff = "dead") {
        G_HubProbePending := 0
        G_HubProbeStrikes += 1
        if (G_HubProbeStrikes < HUB_DEAD_STRIKES) {
            ; 单次超时先不判死：hub 可能只是在做重活（GIL 被占），pong 迟到。
            Log("中枢探测超时（连续 " . G_HubProbeStrikes . "/" . HUB_DEAD_STRIKES
              . " 次，下一次还超时才判死）")
            return
        }
        HubDied("探测 " . HUB_PROBE_TIMEOUT . "ms 无回话（连续 " . HUB_DEAD_STRIKES . " 次）")
        return
    }
    ; 拉起后 HUB_START_TIMEOUT 仍不位：气泡一次（决策 D10：自检类消息走托盘气泡），
    ; 但**不**判死重启 —— 重试期只继续重连，免得对着一个起不来的 hub 反复重启
    if (!G_HubUp && now > G_HubDeadline && !G_HubToldNotReady) {
        G_HubToldNotReady := true
        step := WatchdogStep(G_HubWatchPhase, "handshake-timeout")
        G_HubWatchPhase := step.phase
        ; 同内核的 Tick：握手超时也意味着「中枢不在位」，图标必须灰（别只消费 phase）
        if (step.icon = "gray")
            TraySync()
        Log("中枢 " . (HUB_START_TIMEOUT // 1000) . "s 内仍未就绪，继续按 " . HUB_PROBE_MS . "ms 重连")
        ShowBubble("数据中枢未就绪（词已暂存，稍后自动入库）", "英语取词", 3)
    }
    if (eff != "ping")
        return
    G_HubProbeSent := now
    if (HubProbeSend())
        G_HubProbePending := now
}

; 中枢判死（连接断 / 探测超时）：相位机判动作（HubDeathAction）→ 清理 → 存活期
; 判死**重启一次** + 告知用户；重试期再死只记日志（不重启、不刷 toast）。
; 与 KernelDied 同款：可能跑在 socket 的 Critical 回调里，所以只置位 + 派一枚
; 一次性拍子（票 #69），真正重启在 RestartHubTick 里；自己不能崩。
HubDied(reason) {
    try {
        global G_HubWatchPhase, G_HubProbePending, G_HubDeadline, G_HubToldNotReady
        global G_HubProbeStrikes, G_HubRestartPending
        step := HubDeathAction(G_HubWatchPhase)
        G_HubWatchPhase := step.phase
        G_HubProbePending := 0
        G_HubProbeStrikes := 0
        ; 重启一次计时从这次判死重新起算 —— 否则「10s 仍不位」的气泡会在判死后
        ; 第一拍就误报（deadline 还是上一次拉起时那个，早就过了）
        G_HubDeadline := A_TickCount + HUB_START_TIMEOUT
        G_HubToldNotReady := false
        HubDown(reason)
        if (step.restart) {
            g := RestartPhase(G_HubRestartPending, "death")
            G_HubRestartPending := g.pending
            if (g.dispatch) {
                Log("中枢判死并重启（存活期）：" . reason)
                SetTimer(RestartHubTick, -1)     ; 一次性拍子：Critical 段结束后才跑
            } else
                Log("中枢判死（重启已在队列里，不叠加）：" . reason)
        } else
            Log("中枢重试期再死，不重启：" . reason)
        if (step.icon = "gray")
            TraySync()
        if (step.toast != "")
            ShowToast("数据中枢已重启，稍后自动恢复", "err")
    } catch as err {
        Log("中枢判死处理异常：" . err.Message)
    }
}

; 中枢判死的那枚一次性拍子（票 #69）：与 RestartKernelTick 同款 —— 真重启在这儿，
; 跑完（哪怕抛错）复位重启位。
RestartHubTick() {
    global G_HubRestartPending
    try {
        RestartHub()
    } finally {
        g := RestartPhase(G_HubRestartPending, "done")
        G_HubRestartPending := g.pending
    }
}

; 拉起中枢（spec §2：工作目录 = 工具目录，`pythonw hub.py`）。拿到的 PID 是 venv
; 的转发器，收尾要连树一起收。拉不起来（解释器缺失等）显式提示，返回 false ——
; **不**影响 spool 保存（词不丢是底线）。
StartHub() {
    global G_Cfg, G_HubPid
    exe := ResolveExe(G_Cfg.pythonExe)
    if (!FileExist(exe)) {
        Log("中枢解释器不存在：" . exe . "（config 的 python_exe；保存照走 spool，词不丢）")
        ShowBubble("找不到中枢解释器，词已暂存不会丢（请改 config.ini 的 python_exe）", "英语取词", 3)
        return false
    }
    try {
        Run('"' . exe . '" "' . TOOL_DIR . '\hub.py"', TOOL_DIR, "Hide", &pid)
        G_HubPid := pid
        Log("已拉起中枢 pid=" . (pid = "" ? "（没拿到 PID）" : pid) . "（" . exe . "）")
        return true
    } catch as err {
        Log("拉起中枢失败：" . err.Message . "（保存照走 spool，词不丢）")
        ShowBubble("拉起数据中枢失败，词已暂存不会丢（详见 logs\app.log）", "英语取词", 3)
        return false
    }
}

StopHub() {
    global G_HubPid
    if (!G_HubPid)
        return
    ; /T 连整棵进程树一起收（venv 的 pythonw.exe 只是转发器，同内核）
    try {
        RunWait('taskkill /F /T /PID ' . G_HubPid, , "Hide")
        Log("已结束自己拉起的那个中枢（taskkill /F /T /PID " . G_HubPid . "）")
    } catch as err {
        Log("结束中枢失败：" . err.Message)
    }
    G_HubPid := 0
}

; 重启中枢：断开旧连接 → 结束自己拉起的那个（连树）→ 端口接管（D12）→ 拉起新的
; → 回到探测/握手节奏。会阻塞（StopHub 的 taskkill RunWait + 驱逐的 netstat），
; 所以同 RestartKernel：从一次性拍子（RestartHubTick）或托盘点击里调，不从
; socket 的 Critical 回调里直接调（票 #69）。返回 false = 新中枢没拉起来
; （StartHub 已记日志 + 气泡）。
RestartHub() {
    global G_HubUp, G_HubWatchPhase, G_HubProbeSent, G_HubProbePending
    global G_HubDeadline, G_HubToldNotReady
    G_HubUp := false
    HubDown("重启中枢")
    StopHub()                                  ; 结束自己拉起的那个中枢（单所有者）
    EvictPortSquatter(G_HubPort, "中枢")       ; 端口上若还有外来者，驱逐后由我们拉起（D12）
    ok := StartHub()
    G_HubWatchPhase := "retrying"
    G_HubProbeSent := 0
    G_HubProbePending := 0
    G_HubDeadline := A_TickCount + HUB_START_TIMEOUT
    G_HubToldNotReady := false
    SetTimer(HubWatch, HUB_PROBE_MS)
    return ok
}

; 中枢回话：一行 JSON。票 #178 起回话自带 `_rpc_id`（中枢原样回显请求里的那个），
; 按它对号入座；回话不带编号（旧中枢 / 无编号的坏 json 回包）才退回位置 FIFO
; —— 后者只在两侧都老的时候成立，是向后兼容的退路，不是常态。
OnHubLine(line) {
    global G_HubAsks, G_HubFlushQ, G_HubPongLogged
    HubUp()
    replyId := JsonNumber(line, "_rpc_id")
    pick := HubPickAsk(G_HubAsks, replyId)
    if (pick.mode = "unknown") {
        ; 认不出编号 = 这条回话的请求已经作废（此前丢过一条回话），整条丢弃。
        ; 只记长度与在途条数：回话里可能有用户文本，内容一律不进日志（§2.7）。
        Log("丢弃无法对号的回话（编号 " . replyId . "，队首 "
          . ObjProp(G_HubAsks.Length ? G_HubAsks[1] : "", "kind")
          . "，在途 " . G_HubAsks.Length . " 条，回话 " . StrLen(line) . " 字）")
        return
    }
    if (pick.dropped > 0)
        Log("回话对号入座时作废 " . pick.dropped . " 条前序请求（它们的回话已丢）")
    ; 命中的那条 ask。票 #70：元素从裸字符串换成 `{ kind, gen }`，
    ; 所以匹配一律看 `kind`（`ObjProp` 容错：拿不到就什么都不认，与原来的空串落空同款）
    ask := pick.ask
    kind := ObjProp(ask, "kind")
    if (kind = "ping") {
        ; 票 #178：ping 槽必须真的看到 `pong`。以前这里不看内容 —— 任何错位/错误回话
        ; 落到 ping 槽都被静默吞掉，把「队列偏了一格」这类故障整个掩盖掉。
        if (!RegExMatch(line, '"pong"\s*:\s*-?\d+')) {
            Log("中枢回话异常：ping 槽收到的不是 pong（回话 " . StrLen(line) . " 字）")
            return
        }
        ; 探测是**每秒一次**的：只记「状态真的变了」那一拍（HubUp 已经记了「中枢就绪」，
        ; 这里补一条它的 pong 耗时即可）。每拍都记会在两天内把 2MB 日志滚掉、
        ; 把真正的事件淹掉（2026-10-13 实测：app.log 里 6,188 行都是这条 ping）。
        if (!G_HubPongLogged) {
            G_HubPongLogged := true
            Log("中枢 ping pong " . JsonNumber(line, "pong") . "ms")
        }
        ; v34 T5（票 #218 / spec §C22）：每秒 ping 顺带下接收句状态 —— 槽内**边沿
        ; 触发**（值变了才写 tooltip / 换图标 / 弹提示），1/s 不刷屏。
        CaptureStatusApply(line)
        return
    }
    if (kind = "capture_arm") {
        ; v34 T3（#216）：收句扳机的回话 —— 开/停/拒绝原因都在这一槽里，只弹一次
        OnCaptureArmLine(line)
        return
    }
    if (kind = "ext_command") {
        ; v36：下行指令的发放回话 —— hub 已存槽（等扩展心跳领取），不弹提示
        return
    }
    if (kind = "band_redrag") {
        ; v34 T4（#217）：字幕带重拖的回话 —— 区域入账成功/失败只弹一次
        OnBandRedragLine(line)
        return
    }
    if (kind = "peek") {
        ; 代次从队首那条 ask 取（票 #70）：回话据此判归属，迟到的一律丢弃
        OnPeekLine(line, ObjProp(ask, "gen"))
        return
    }
    if (kind = "lookup") {
        OnLookupLine(line)
        return
    }
    ; v21 T5 阶段2：长文三种回话（article_create / article_locate / 面板 lookup）。
    ; 票 #178 起按回话编号取 ask，kind 就是那条请求自己的种类（不再靠「发出顺序」猜）。
    if (kind = "article") {
        LfOnArticleLine(line)
        return
    }
    if (kind = "lf_locate") {
        LfOnLocateLine(line, ObjProp(ask, "gen"))
        return
    }
    if (kind = "lf_lookup") {
        LfOnLookupLine2(line)
        return
    }
    if (kind = "lf_outline") {
        LfOnOutlineLine(line)
        return
    }
    ; v22 P2b（spec §9）：分段预览窗的两条回包（article_paragraphs / article_resegment）
    if (kind = "lf_paragraphs") {
        LfOnInspectorParagraphs(line)
        return
    }
    if (kind = "lf_resegment") {
        LfOnInspectorResegment(line)
        return
    }
    if (kind = "media") {
        ; v5 §3：回包只记一行状态字（不弹提示、不改界面）
        OnMediaLine(line)
        return
    }
    if (kind = "flush") {
        ; §6.1 的重复词回执：hub 的 ingest 去重（同会话同 target 折叠大小写），
        ; `last` 里带 dedup/hits；`last` 为空（5s 定时器抢先 ingest 了）→ 只报已存。
        ; v4.1（#54 P2-5）：回执队列按发出顺序配 toast（连存两条也不串词）——
        ; 票 #178 只换了「回话配给哪条 ask」，这条队列的语义不变（D5）。
        ; v23 S4：`pending` 随这条 flush 一起排队 —— 关框保存没有状态行，把「后台补齐」
        ; 并进同一条保存回执 toast，避免再弹第二条把前一条压住。
        entry := (G_HubFlushQ.Length > 0) ? G_HubFlushQ.RemoveAt(1) : ""
        target := ObjProp(entry, "target")
        pending := ObjProp(entry, "pending")
        ; 回执种类（与上面 `kind` = ask 的种类不是一回事，换个名字免得看串）
        receipt := JsonBool(line, "dedup") ? "dedup" : "saved"
        hits := JsonNumber(line, "hits")
        ; 队列里还有在途的 → 兜底收据重排（回话全到就自然空转）
        if (G_HubFlushQ.Length > 0)
            SetTimer(HubFlushTimeout, -1500)
        ; toast 推迟一拍（同 OnKernelLine：回调跑在 Critical 线程里，尽早返回）
        SetTimer(() => ShowToast(SaveToastText(receipt, target, hits, pending), "ok"), -1)
        ; ==== v35 D1/D5 接线点 D（v35-ux design.md）：挑词保存成功 ====
        ; dedup（重复词回执）不算「保存成功」，hits 的语义也不同（第 N 次），
        ; 只在真保存时喂信息流行 + praise 庆祝。
        SetTimer(() => PetPraiseCelebrate(receipt = "saved" ? hits : 0), -1)
        return
    }
    ; 票 #178 的诊断：回话没落进任何槽（编号认不出的已在上面记过）。这里管两种：
    ; 「回话到了但在途队列是空的」与「ask 的 kind 不认识」。只记长度与计数，
    ; 内容不进日志（§2.7）。
    Log("回话没有对应的槽（编号 " . replyId . "，kind " . (kind = "" ? "缺失" : kind)
      . "，在途 " . G_HubAsks.Length . " 条，回话 " . StrLen(line) . " 字）")
}

; `capture_arm` 的回话（v34 T3 / 票 #216）：成功按 armed 报开/停，失败按 reason
; 说清「为什么没开」。请求-回话一一对应 → 一次按键只有一条回话，天然边沿触发
; （不随每秒 ping 反复弹）。本机只缓存**中枢回来的** armed（HubDown 清位），
; 不缓存用户「想开」的意图。
OnCaptureArmLine(line) {
    global G_CaptureArmed, G_ArmIntent, G_ArmReplay
    ; 2026-11-10：中枢意外重启后的「重放」回话 —— 用户没动手，别弹 toast
    ; （他看到的应该是收句自己还在，而不是突然跳一句「已开始收字幕」）。
    replay := G_ArmReplay
    G_ArmReplay := false
    if (!JsonBool(line, "ok")) {
        reason := JsonField(line, "reason")
        if (replay) {
            Log("收句扳机：中枢重启后未能恢复收句（reason=" . (reason = "" ? "unknown" : reason)
              . "），等用户再按一次")
            return
        }
        if (reason = "no_video")
            SetTimer(() => ShowToast("当前页面不是视频页；先打开视频再按", "err"), -1)
        else if (reason = "disabled")
            SetTimer(() => ShowToast("设置页的『视频收句』总开关是关的；先打开它", "err"), -1)
        else
            SetTimer(() => ShowToast("收句没切换（" . (reason = "" ? "未知原因" : reason) . "）", "err"), -1)
        Log("收句扳机未切换：reason=" . (reason = "" ? "unknown" : reason))
        return
    }
    armed := JsonBool(line, "armed")
    G_CaptureArmed := armed
    G_ArmIntent := armed ? "on" : "off"      ; 中枢内存态的本地影子（重启后重放用）
    if (!armed)
        CaptureHudHide()
    if (!replay)
        SetTimer(() => ShowToast(armed ? "已开始收字幕（本视频）" : "已停止收字幕"), -1)
    Log(armed ? "收句扳机：已开启（本视频）" : "收句扳机：已停止")
}

; 中枢意外重启后，把「用户本来开着收句」这件事重放一次（2026-11-10）。
;
; 为什么需要：armed 只活在中枢内存里，中枢一重启就归零。实测 15:03:42 中枢被判死
; 重启（4.2 秒），用户刚点过的「同意收句」直接消失，他得回去再点一次 —— 而这几秒
; 视频还在播，字幕照样在过。用户感受到的就是「服务起得慢 / 反应迟钝」。
;
; 纪律：
;   · 只在**中枢恢复**时重放，且只重放「开」（关是默认态，不需要恢复）；
;   · 静默：不弹 toast（用户没操作），失败只记日志，页面对不上就等他再按一次；
;   · 用 G_ArmReplay 标记，让 OnCaptureArmLine 知道这次回话不是用户的动作。
CaptureArmReplay() {
    global G_ArmIntent, G_ArmReplay
    if (G_ArmIntent != "on")
        return
    G_ArmReplay := true
    if (!HubSend('{"cmd":"capture_arm","op":"on"}', "capture_arm")) {
        G_ArmReplay := false
        Log("收句扳机：中枢恢复后重放失败（连不上中枢），下次恢复再试")
        return
    }
    Log("收句扳机：中枢重启后重放「已开启」（收句状态只在中枢内存里，重启会清零）")
}

; ---------------------------------------------------------------------------
;  v34 T5（票 #218 / spec §C22–C25）：ping 回包里的四个扁平 cap_* 字段 →
;  托盘 tooltip / 三态图标 / 需要动手的关键事件。
;  一律**边沿触发**：1s 一条 ping，只有值变了才写 tooltip / 弹提示（否则刷屏）。
; ---------------------------------------------------------------------------
CaptureStatusApply(line) {
    global G_Cfg, G_CapArmed, G_CapOcr, G_CapSession, G_CapAgeMs, G_CapTip, G_TrayMode
    global G_CapPhase, G_CapAdded, G_CapLast, G_CapHeartbeatBad, G_CapArmedSince, G_CapArmedMin
    global G_CapScene, G_CapVideo, G_CapCc, G_CapEnded, G_CapEndedStreak, G_CapAutoStopAt
    global G_CapNoSource, G_CapNoSourceStreak, G_CapBandRect, G_CapAsr, G_CapAsrAdded
    global G_CapAsrReason, G_BandPreviewAt
    armed := JsonBool(line, "cap_armed")
    ocr := JsonField(line, "cap_ocr")
    session := JsonNumber(line, "cap_session")
    ; 字段缺失（旧中枢）：按「从未收到心跳」处理，不谎报新鲜
    ageMs := RegExMatch(line, '"cap_age_ms"\s*:\s*-?\d+') ? JsonNumber(line, "cap_age_ms") : -1
    ; #220：状态浮窗的相位 / 本段句数 / 最近落库（仍是扁平字段）
    phase := RegExMatch(line, '"cap_phase"\s*:') ? JsonField(line, "cap_phase") : ""
    added := RegExMatch(line, '"cap_added"\s*:\s*-?\d+') ? JsonNumber(line, "cap_added") : 0
    last := RegExMatch(line, '"cap_last"\s*:') ? JsonField(line, "cap_last") : ""
    prevArmed := G_CapArmed
    prevOcr := G_CapOcr
    prevSession := G_CapSession
    prevPhase := G_CapPhase
    prevAdded := G_CapAdded
    prevLast := G_CapLast
    prevBad := G_CapHeartbeatBad
    prevArmedMin := G_CapArmedMin
    G_CapArmed := armed
    G_CapOcr := ocr
    G_CapSession := session
    G_CapAgeMs := ageMs
    G_TrayMode := CaptureTrayMode(armed, ocr, ageMs)
    TrayApplyIcon()
    tip := CaptureTipText()
    if (tip != G_CapTip) {
        G_CapTip := tip
        A_IconTip := tip
    }
    ; #220：看视频时的常驻状态浮窗（置顶/鼠标穿透；armed 关即收）
    ; v35 #232 接线点 A（D-L3）：pet 健康时老 HUD 让位；关桌宠 / 素材缺失时 HUD 原样工作。
    if (!PetHealthy())
        CaptureHudApply(armed, phase, added, ageMs, session, last)
    ; v35 #232 接线点 A：角色窗只消费 StatusVisual 的输出（face / motion），不判相位。
    PetApplyVisual(phase)
    ; ==== v35 #231/#233 接线点 B：玻璃卡 + 气泡 开始 ====
    ; 相位 → 视觉的唯一真值是 StatusVisual；这里只做「喂给两个窗口 + 锚点跟随」。
    ; pet 健康时卡/气泡都锚在角色头顶；pet 不可用（素材缺失/关闭）整块让位 → HUD 接管（D-L3）。
    visual := StatusVisual(phase, G_Cfg)
    healthy := PetHealthy()
    GlassCardSetEnabled(healthy)
    PetBubbleSetEnabled(healthy)
    if (healthy) {
        ; 画布 260×340 顶部有一段透明留白，锚点下移约 40lp 才贴着「她的头顶」
        ax := G_PetX + G_PetW // 2
        ay := G_PetY + P(40)
        GlassCardFollow(ax, ay)
        PetBubbleFollow(ax, ay)
        ; #244：相位提醒不再是独立气泡出口 —— 视觉快照随本拍喂给发言仲裁
        ; （PetCaptureFeedTick 把它做成「需动手相位」候选）。
    }
    GlassCardApply(visual, G_Cfg.petGlass)
    ; ==== v35 #231/#233 接线点 B 结束 ====
    CaptureEventTick(prevOcr, armed, ocr)
    ; ==== v35 D1/D5 接线点 C（v35-ux design.md）：信息流行 + 主动性引擎 ====
    ; 先记下本拍快照（下一拍的 prev），再把边沿事件喂信息流行、场景快照喂 chatter。
    G_CapPhase := phase
    G_CapAdded := added
    G_CapLast := last
    G_CapHeartbeatBad := (ageMs < 0 || ageMs > CAPTURE_HEARTBEAT_STALE_MS)
    nowTick := A_TickCount
    if (armed && !prevArmed) {
        G_CapArmedSince := nowTick               ; armed 边沿重记连续记录起点
        G_CapAutoStopAt := 0                     ; 新开一轮收句：播完自动停的冷却清零
        G_CapNoSourceStreak := 0                 ; 新开一轮：无字源连拍计数归零
        G_CapEndedStreak := 0                    ; 新开一轮：播完连拍计数归零（否则末尾重开会一拍就停）
    }
    if (!armed) {
        G_CapArmedSince := 0
        G_CapEnded := 0                          ; 收句已关：清播完标记
        G_CapEndedStreak := 0
        G_CapNoSource := 0                       ; 收句已关：清标记，别留到下一轮
        G_CapNoSourceStreak := 0
    }
    armedMin := (armed && G_CapArmedSince > 0) ? (nowTick - G_CapArmedSince) // 60000 : 0
    G_CapArmedMin := armedMin
    ; v36：场景字段（cap_scene / cap_video）——字段缺失（旧中枢）按「无场景」处理
    scene := RegExMatch(line, '"cap_scene"\s*:') ? JsonField(line, "cap_scene") : ""
    ; cap_video 是**扁平 0/1 标量**（外壳只有扁平正则，禁止嵌套，spec §C22）
    video := RegExMatch(line, '"cap_video"\s*:\s*-?\d+') ? JsonNumber(line, "cap_video") : 0
    prevScene := G_CapScene
    prevVideo := G_CapVideo
    G_CapScene := scene
    G_CapVideo := video
    ; v36 #246：来源字段（cap_cc）——与相位并行，字段缺失（旧中枢）按「未判明」。
    cc := RegExMatch(line, '"cap_cc"\s*:') ? JsonField(line, "cap_cc") : "off"
    prevCc := G_CapCc
    G_CapCc := cc
    ; 2026-11-10：cap_ended = 视频播到末尾停住了（hub 判，扁平 0/1；缺失 = 旧中枢）。
    ; 连续两拍才停收句（见 CaptureEndedShouldStop 的抖动理由）。
    ended := RegExMatch(line, '"cap_ended"\s*:\s*-?\d+') ? JsonNumber(line, "cap_ended") : 0
    prevEnded := G_CapEnded
    G_CapEnded := ended
    G_CapEndedStreak := ended ? G_CapEndedStreak + 1 : 0
    if (ended != prevEnded)
        Log("桌宠：播完标记 " . (prevEnded ? "播完" : "未播完") . " → " . (ended ? "播完" : "未播完"))
    ; 2026-11-10：cap_no_source = hub 的 10 秒帧签名窗口判出「无画面字源」
    ; （扁平 0/1，缺失 = 旧中枢）。同样连两拍才停：hub 侧已去抖，外壳再稳一手。
    noSource := RegExMatch(line, '"cap_no_source"\s*:\s*-?\d+') ? JsonNumber(line, "cap_no_source") : 0
    prevNoSource := G_CapNoSource
    G_CapNoSource := noSource
    G_CapNoSourceStreak := noSource ? G_CapNoSourceStreak + 1 : 0
    if (noSource != prevNoSource)
        Log("桌宠：无画面字源标记 " . prevNoSource . " → " . noSource)
    ; 2026-11-11：字幕带矩形（自动复用 / 用户重拖后）——本会话第一次拿到、或矩形变了，
    ; 用 1.6s 红框显示系统正在用的区域，用户一眼就知道框对不对（不必等 10 秒问框）。
    ; 区域校正后矩形会跟着视频框移动；同会话内加 2.5s 冷却，滚动时只偶尔提示、不闪屏。
    bandRect := RegExMatch(line, '"cap_band_rect"\s*:') ? JsonField(line, "cap_band_rect") : ""
    prevBandRect := G_CapBandRect
    G_CapBandRect := bandRect
    newSession := (session != prevSession)
    showPreview := (newSession || bandRect != prevBandRect)
    if (armed && bandRect != "" && showPreview && (newSession || A_TickCount - G_BandPreviewAt >= 2500)) {
        G_BandPreviewAt := A_TickCount
        BandPreviewShow(bandRect)
    }
    ; #240 可观测性：场景快照变化留痕（无场景 ↔ 邀请场景）。
    ; 只记判定名，不记 URL / 标题（§2.7 隐私纪律）。
    if (scene != prevScene)
        Log("桌宠：场景快照 " . (prevScene = "" ? "无场景" : prevScene)
          . " → " . (scene = "" ? "无场景" : scene))
    ; 2026-11-11 ASR P2：转写状态（off/queued/done/error）——同一拍喂信息流/仲裁
    asr := RegExMatch(line, '"cap_asr"\s*:') ? JsonField(line, "cap_asr") : "off"
    asrAdded := RegExMatch(line, '"cap_asr_added"\s*:\s*-?\d+') ? JsonNumber(line, "cap_asr_added") : 0
    asrReason := RegExMatch(line, '"cap_asr_reason"\s*:') ? JsonField(line, "cap_asr_reason") : ""
    prevAsr := G_CapAsr
    prevAsrAdded := G_CapAsrAdded
    prevAsrReason := G_CapAsrReason
    G_CapAsr := asr
    G_CapAsrAdded := asrAdded
    G_CapAsrReason := asrReason
    PetCaptureFeedTick({ armed: prevArmed, phase: prevPhase, added: prevAdded
                       , session: prevSession, last: prevLast, bad: prevBad
                       , armedMin: prevArmedMin, ageMs: -1, scene: prevScene
                       , video: prevVideo, cc: prevCc
                       , asr: prevAsr, asrAdded: prevAsrAdded, asrReason: prevAsrReason }
                     , { armed: armed, phase: phase, added: added, session: session
                       , last: last, bad: G_CapHeartbeatBad, armedMin: armedMin
                       , ageMs: ageMs, scene: scene, video: video, cc: cc
                       , asr: asr, asrAdded: asrAdded, asrReason: asrReason }
                     , visual)
    ; 本拍的信息流先写，再判「视频播完要不要停」—— 停了以后 armed 变 false，面板会
    ; 按设计自动隐藏，不先写这一行用户就永远看不到「为什么停了」。
    if (CaptureEndedShouldStop(armed, ended, G_CapEndedStreak))
        CaptureAutoStopOnEnded()
    ; 无画面字源：ASR 未启用/失败时的保底（与播完共用 10s 冷却，不会重复停机）。
    if (CaptureNoSourceShouldStop(armed, noSource, G_CapNoSourceStreak))
        CaptureAutoStopOnNoSource()
}

; 视频播完 → 自动停收句（2026-11-10 用户要求）。
; 三条纪律：
;  · 只发 capture_arm off，**不碰浏览器**（全屏那次的教训：任何自动改变用户环境
;    的动作都不做）；
;  · 先写一行信息流再停：armed 变 false 时面板会自动隐藏（PetFeedSetArmed），
;    不先写这一行，用户只会看到面板莫名消失；
;  · 10 秒冷却：off 没发出去（中枢不在位）时不要每拍重发刷日志；开收句的上升沿
;    会把冷却清零（见 CaptureStatusApply 的 armed 边沿）。
CaptureAutoStopOnEnded() {
    global G_CapAutoStopAt
    now := A_TickCount
    if (G_CapAutoStopAt > 0 && now - G_CapAutoStopAt < 10000)
        return
    G_CapAutoStopAt := now
    PetFeedSafeLine("视频播完了，收句已自动停（下条视频再开）", "idle", "auto_stop")
    if (!HubSend('{"cmd":"capture_arm","op":"off"}', "capture_arm")) {
        Log("桌宠：视频播完自动停收句 —— 中枢不在位，未发出")
        return
    }
    Log("桌宠：视频播完，自动停收句（capture_arm off 已发出）")
    PetSpeechMaybe([{ kind: "capture_end", text: CaptureEndedStopText(), action: ""
                    , source: "视频播完自动停"
                    , label: "去挑词", fn: (*) => PetPickOpen() }])
}

; 无画面字源 → 自动停收句（2026-11-10；ASR 未启用或转写失败时的保底）。
; 与 CaptureAutoStopOnEnded 同构：只发 capture_arm off（不碰浏览器）、先写一行
; 信息流再停（armed 变 false 面板会自动隐藏）、共用 10s 冷却。文案按 cap_asr 区分
; 「未启用」与「转写失败」，不叫用户换片 —— 无字幕听力练习是他要的片型。
CaptureAutoStopOnNoSource() {
    global G_CapAutoStopAt, G_CapAsr
    now := A_TickCount
    if (G_CapAutoStopAt > 0 && now - G_CapAutoStopAt < 10000)
        return
    G_CapAutoStopAt := now
    feedText := (G_CapAsr = "error")
        ? "这条没有可读字幕，音频转写也失败了，收句已自动停"
        : "这条没有可读字幕，收句已自动停（音频转写未启用）"
    PetFeedSafeLine(feedText, "warn", "no_source")
    if (!HubSend('{"cmd":"capture_arm","op":"off"}', "capture_arm")) {
        Log("桌宠：无画面字源自动停收句 —— 中枢不在位，未发出")
        return
    }
    Log("桌宠：无画面字源，自动停收句（capture_arm off 已发出）")
    PetSpeechMaybe([{ kind: "no_source", text: CaptureNoSourceStopText(G_CapAsr), action: ""
                    , source: "无画面字源自动停" }])
}

; 2026-11-11：字幕带红框预览（1.6s，不抢焦点、点击穿透）。
; 为什么：11-10 日志显示旧区域静默失效时系统没有任何可见确认，用户直到 10 秒后
; 被问框才知道要重拖。这里在开始采样/重拖后把系统正在用的区域画出来，
; 用户一眼就能判断框对不对；不对就按重拖热键（默认 ^!6）自己修。
; 只画 4 条细边，不铺全屏遮罩 —— 不改变视频窗口形态、不挡下层操作。
BandPreviewShow(rect) {
    global G_BandPreviewGuis
    parts := StrSplit(rect, ",")
    if (parts.Length != 4)
        return
    x := Integer(parts[1]), y := Integer(parts[2])
    w := Integer(parts[3]), h := Integer(parts[4])
    if (w <= 0 || h <= 0)
        return
    BandPreviewHide()
    th := P(2)
    edges := [[x, y, w, th], [x, y + h - th, w, th]
            , [x, y, th, h], [x + w - th, y, th, h]]
    for e in edges {
        g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
        g.BackColor := "FF3B30"
        g.Show(Format("x{} y{} w{} h{} NoActivate", e[1], e[2], e[3], e[4]))
        WinSetTransparent(230, g)
        G_BandPreviewGuis.Push(g)
    }
    SetTimer(BandPreviewHide, -1600)
}

BandPreviewHide() {
    global G_BandPreviewGuis
    for g in G_BandPreviewGuis
        try g.Hide()
    G_BandPreviewGuis := []
}

; 悬停提示（≤55 字符）：一眼看出「在不在收句 / 为什么没在收」。
CaptureTipText() {
    global G_Cfg, G_CapArmed, G_CapOcr, G_CapAgeMs
    if (!G_CapArmed)
        return "英语取词 — 收句未开启（按 " . G_Cfg.hotkeyCaptureArm . " 开始）"
    if (G_CapAgeMs < 0 || G_CapAgeMs > CAPTURE_HEARTBEAT_STALE_MS)
        return "英语取词 — 心跳中断（页面不在前台）"
    if (G_CapOcr = "active")
        return "英语取词 — 收句中 · 心跳 " . Round(G_CapAgeMs / 1000) . "s 前"
    if (G_CapOcr = "idle")
        return "英语取词 — 已暂停（回来自动续）"
    if (G_CapOcr = "dormant")
        return "英语取词 — 需要拖一次字幕带"
    if (G_CapOcr = "redrag_needed")
        return "英语取词 — 需要重拖字幕带"
    if (G_CapOcr = "failed")
        return "英语取词 — OCR 引擎不可用"
    return "英语取词 — 已开启，等待播放"
}

; 关键事件（spec §C24 的五类里归外壳的两类）：降级到画面识别 / 需要重拖。
; 开始 / 停止已有 T3 的 RPC 回话 toast（二选一，同一次按键只一条），这里不重复。
CaptureEventTick(prevOcr, armed, ocr) {
    global G_Cfg
    if (!armed)
        return
    ; 屏幕采样刚起来（off → active）= 这条没有英文 CC，改屏幕采
    if (ocr = "active" && prevOcr = "off")
        CaptureNotify("fallback", "没有英文 CC，改屏幕采")
    ; 无区域休眠 / 区域失效：都需要用户拖一次（同一类，冷却共用）
    if (ocr = "dormant" && prevOcr != "dormant")
        CaptureNotify("redrag", "需要拖一次字幕带（" . G_Cfg.hotkeyRedrag . "）")
    if (ocr = "redrag_needed" && prevOcr != "redrag_needed")
        CaptureNotify("redrag", "需要重拖字幕带（" . G_Cfg.hotkeyRedrag . "）")
}

; 同类事件 5 分钟冷却（spec §C24）：返回 true = 这次可以弹，并记下时刻。
CaptureEventAllowed(kind) {
    global G_CapEventAt
    now := A_TickCount
    last := G_CapEventAt.Has(kind) ? G_CapEventAt[kind] : -CAPTURE_EVENT_COOLDOWN_MS
    if (now - last < CAPTURE_EVENT_COOLDOWN_MS)
        return false
    G_CapEventAt[kind] := now
    return true
}

; 托盘气泡（无焦点抢夺、无声音；Windows 可能吞通知 → tooltip 同时已更新）。
CaptureNotify(kind, text) {
    if (!CaptureEventAllowed(kind))
        return
    ShowBubble(text, "英语取词", 3)
}

; ===========================================================================
;  v35 D1/D5（v35-ux design.md）：信息流行 + 主动性引擎的接线（A3 所有）。
;  纯逻辑全在 lib/Logic.ahk（PetFeed*Line / PetChatterGate / PetChatterPick /
;  PetSpeechArbitrate）；窗口侧 lib/PetFeed.ahk / lib/PetBubble.ahk 归 A2 ——
;  PetFeedLine 已随 include 就位，喂行仍走 PetFeedSafeLine 的存在守卫
;  （真机异常 / 模块被拿掉时安全降级，绝不崩外壳）。
; ===========================================================================

; 喂一行信息流（text 为空不喂）。
; v36 收尾复核（决策 12）：真记上一行就留日志——只记类别 tag，不记正文
; （「+1 句」行正文是原句预览，同发言日志的隐私纪律）。
;
; **2026-11-10 真机复核修复（信息流一直是空的）**：这里原来用
; `try f := Func("PetFeedLine")` 当「模块就位」守卫。AHK v2.0.30 里 `Func` 是
; **类**（不是取函数引用的函数），`Func("名字")` 直接抛 `Invalid base.`，异常被
; catch 一吞 → 每一行都被当成「lib/PetFeed.ahk 未就位」丢掉。后果：面板从
; #245 起只有启动那行「信息流就绪…」，写行日志 0 条，而纯函数单测全绿。
; 实测：`Func('Foo')` → `Invalid base.`；`Foo`（裸引用）→ Func。这里不需要
; 运行时探测——lib/PetFeed.ahk 是 #Include 静态引入，模块必然在位，直接调。
PetFeedSafeLine(text, tone, tag := "") {
    if (text = "")
        return false
    if (!PetFeedLine(text, tone))
        return false
    Log(PetFeedLogText(tag))
    return true
}

; 1Hz ping 一拍的 D1 行事件 + v36 #244 发言仲裁。
; v36 #245：「事件 → 行」的判定收成纯函数 PetFeedProject(prev, cur)（单点在
; lib/Logic.ahk），这里只喂快照、维护空闲计数、把行交给信息流面板。
; v36 #244：一拍只出一条 —— 本拍所有主动发言先收集成候选，交给纯函数仲裁，
; 再由唯一出口 PetSpeechSay 执行（气泡 + 动作 + 记冷却 + 日志）。
PetCaptureFeedTick(prev, cur, visual := "") {
    global G_Cfg, G_CapFeedIdleTicks, G_SceneInviteHold, G_CcReportSession
    ; 空闲计数 = 距上一条信息流行过了几拍；持续状态行按它低频补行（同状态不刷屏）。
    G_CapFeedIdleTicks += 1
    cur.idleTicks := G_CapFeedIdleTicks
    lines := PetFeedProject(prev, cur, G_Cfg)
    for l in lines
        PetFeedSafeLine(l.text, l.tone, ObjProp(l, "tag", ""))
    if (lines.Length > 0)
        G_CapFeedIdleTicks := 0
    ; v36 收尾复核（US11 补齐）：armed 中换会话 = 换了视频，也清「用户 × 关过」
    ; —— × 不等于永久失联（原来只有 armed 上升沿恢复，armed 期间换视频不恢复）。
    if (cur.armed && cur.session > 0 && cur.session != prev.session)
        PetFeedRestoreOnSession()
    ; armed 边沿的动作表情（D8：开 = open_lid、关 = drool 的即时反应）
    if (cur.armed != prev.armed)
        PetChatterEmitAction(cur.armed ? "open_lid" : "drool")
    ; ==== v36 #244：收集本拍全部候选发言 → 仲裁 → 唯一出口 ====
    cands := []
    ; ① 场景邀请（带「开始收句」按钮）：正边沿置待发位；这一拍被闸门/占位挡住，
    ;    待发位留着，下一拍接着排队（不再「边沿只此一次」）。
    for c in PetSceneMaybe(prev, cur)
        cands.Push(c)
    ; ② 需动手相位（玻璃卡被「知道了」收掉后的提醒）：redrag / error
    phaseKind := PetPhaseAskKind(visual)
    if (phaseKind != "")
        cands.Push({ kind: phaseKind, text: ObjProp(visual, "text", "")
                   , action: "", source: "相位提醒" })
    ; ③ 人设 chatter：里程碑 / 会话 / 关心 / 泛化邀请 / 闲聊
    for c in PetChatterCandidates(PetChatterCtx(prev, cur))
        cands.Push(c)
    ; ④ 4 步引导（首次教用法）
    tour := PetTourCandidate(G_Cfg)
    if (IsObject(tour))
        cands.Push(tour)
    ; ⑤ v36 #246：来源播报 —— armed + 会话确定 + cap_cc 判明 → 人话
    ;    （同一会话只说一次，去重键是 cap_session；换视频 = 新会话会重新说）。
    ccCand := PetCcReportCandidate(cur, G_CcReportSession)
    if (IsObject(ccCand))
        cands.Push(ccCand)
    ; ⑤b 2026-11-11 ASR P2：转写完成/失败各播报一次（done 带「去挑词」按钮）
    asrCand := PetAsrReportCandidate(ObjProp(cur, "asr", ""), ObjProp(prev, "asr", "")
                                   , ObjProp(cur, "asrAdded", 0)
                                   , ObjProp(cur, "asrReason", ""))
    if (IsObject(asrCand)) {
        if (asrCand.kind = "asr_done")
            asrCand.fn := (*) => PetPickOpen()
        cands.Push(asrCand)
    }
    ; ⑥ #247：这一拍情境变了吗（场景正边沿 / 换了视频会话）—— 变了就让
    ;    「重新打招呼」的两类不吃旧冷却（纯函数判定，数值仍在闸门里）。
    said := PetSpeechMaybe(cands, PetSpeechSituationChanged(prev, cur))
    ; 邀请真的出来了 → 清待发位；没出来就继续挂着（下一拍再抢那条唯一的气泡位）
    if (IsObject(said) && said.kind = "video_invite")
        G_SceneInviteHold := false
    ; 来源播报说出口才记「这个会话说过了」（被闸门/占位挡住时下一拍接着抢）
    if (IsObject(said) && said.kind = "cc_report")
        G_CcReportSession := cur.session
}

; chatter 侧的拍快照（#244：候选收集的输入；闸门状态另装，别混在一起）。
; sceneInvite = 这一拍场景邀请还成立（B站视频在播且未 armed）：泛化邀请让位。
PetChatterCtx(prev, cur) {
    global G_Cfg
    return { now: A_TickCount, rnd: Random(0, 1000000), cfg: G_Cfg
           , sceneInvite: (cur.scene = "video_invite" && cur.video = 1 && !cur.armed)
           , armed: cur.armed, pingAlive: (cur.ageMs < 0 || cur.ageMs <= CAPTURE_HEARTBEAT_STALE_MS)
           , hour: A_Hour
           , session: cur.session, prevSession: prev.session
           , added: cur.added, prevAdded: prev.added
           , armedMin: cur.armedMin, armedMinPrev: prev.armedMin
           , phase: cur.phase }
}

; ===========================================================================
;  v36 #244：主动发言的唯一出口（一拍只出一条）
;
;  所有主动发言（场景邀请 / 字幕带问询 / 需动手相位 / 里程碑·会话类 /
;  泛化邀请 / 闲聊 / 挑词庆祝 / 4 步引导）都只做一件事：把自己的意图做成
;  候选投给 PetSpeechMaybe。仲裁（纯函数 PetSpeechArbitrate，lib/Logic.ahk）
;  按固定优先级 + 唯一闸门（PetChatterGate）选一条；PetSpeechSay 执行：
;  气泡（可带一个按钮）+ 动作 + 记冷却 + 日志。
;
;  纪律：气泡调用只出现在 PetSpeechSay 里（源码钉子钉着）；未选中时只记
;  「为什么没说」，同一原因不刷屏；日志只记动作/判定，不记正文（§2.7）。
; ===========================================================================

; 仲裁入口：candidates 是本拍所有候选（{kind, text, action, source?}，
; 可选 label + fn 表示带一个按钮）。返回真的说出去的那条（"" = 都没说）。
; situationChanged = 这一拍情境变了（#247；由 PetSpeechSituationChanged 判定），
; 只用来豁免「重新打招呼」那两类的旧冷却，其余发言与勿扰照旧。
PetSpeechMaybe(candidates, situationChanged := false) {
    global G_BubEnabled, G_PetDnd, G_BubShown, G_GlassShown
    global G_ChatterLastAnyAt, G_ChatterLastAt, G_ChatterDay, G_ChatterCount
    global G_SpeechMissLogged
    if (!PetHealthy() || (IsSet(G_BubEnabled) && !G_BubEnabled))
        return ""
    if (!IsObject(candidates) || candidates.Length = 0)
        return ""
    now := A_TickCount
    today := FormatTime(A_Now, "yyyyMMdd")
    if (G_ChatterDay != today) {
        G_ChatterDay := today
        G_ChatterCount := Map()          ; 换天清每日计数（间隔冷却 Map 不动）
    }
    gate := PetSpeechGateState(now, G_ChatterLastAnyAt, G_ChatterLastAt
                             , G_ChatterCount, G_ChatterDay, today, situationChanged)
    ; 勿扰（spec §勿扰；G_PetDnd 归 pet-core）与「气泡显示中 / 玻璃卡展开中」：
    ; 两个守卫都交给纯函数，接线侧不再自己判「能不能说」。
    quiet := (IsSet(G_PetDnd) && G_PetDnd)
    bubbleBusy := ((IsSet(G_BubShown) && G_BubShown) || (IsSet(G_GlassShown) && G_GlassShown))
    chosen := PetSpeechArbitrate(candidates, gate, quiet, bubbleBusy)
    if (!IsObject(chosen)) {
        ; #240：留痕「为什么这一拍没说」；同一个原因只记一次（1Hz 不会刷满日志）。
        reason := PetSpeechMissReason(candidates, quiet, bubbleBusy)
        if (reason != G_SpeechMissLogged) {
            G_SpeechMissLogged := reason
            Log("桌宠：" . PetSpeechLogText("", reason))
        }
        return ""
    }
    G_SpeechMissLogged := ""
    ; #240：先留「选了谁 + 来源」，再产生副作用（弹气泡）。
    Log("桌宠：" . PetSpeechLogText(chosen.kind, ObjProp(chosen, "source", "")))
    if (PetSpeechSay(chosen))
        return chosen
    return ""
}

; 唯一执行点：气泡（带按钮的走 PetBubbleShowEx）+ 动作 + 记冷却与每日计数。
; 全外壳只有这里调用 PetBubbleShow / PetBubbleShowEx（源码钉子钉着）。
; 「带不带按钮 / 说什么动作」由纯函数 PetSpeechPlan 决定（片 61 用真对象跑它）——
; 这里只执行。**别在这层直接读 cand 的可选字段**：普通 Object 没有 Map 的 Has()，
; 2026-11-10 复核的「每次发言都抛异常」就是这么来的。
PetSpeechSay(cand) {
    global G_ChatterLastAnyAt, G_ChatterLastAt, G_ChatterCount
    plan := PetSpeechPlan(cand)
    if (!IsObject(plan))
        return false
    ok := plan.ex ? PetBubbleShowEx(cand.kind, cand.text, plan.label, plan.fn)
                  : PetBubbleShow(cand.kind, cand.text)
    if (!ok)
        return false
    PetChatterEmitAction(plan.action)
    if (IsObject(plan.onSaid))
        plan.onSaid.Call()
    now := A_TickCount
    G_ChatterLastAnyAt := now
    G_ChatterLastAt[cand.kind] := now
    G_ChatterCount[cand.kind] := PetMapGet(G_ChatterCount, cand.kind, 0) + 1
    return true
}

; ===========================================================================
;  v36：场景识别接线 —— B站视频页主动邀请 + 无 CC 字幕带问询
;
;  场景真值在 hub（scene_of → ping 的 cap_scene / cap_video 扁平字段）；外壳在
;  1Hz ping 的 CaptureStatusApply 里喂快照。这里只有接线（窗口与 RPC 调用），
;  纯逻辑（文案/边沿/待发位）在 lib/Logic.ahk 的 v36 段。
;
;  #244：两条都不再自己弹气泡，只投候选给统一的发言出口（PetSpeechMaybe）；
;  邀请的频率由唯一闸门 PetChatterGate 管（#247 重校准：同类 2 分钟、
;  每日 20 次只是跑飞保险 —— 同一视频只邀一次靠场景正边沿 + 待发位）。
; ===========================================================================

; 场景链的候选提供者（PetCaptureFeedTick 每拍调一次），返回候选数组：
;   · video_invite：正边沿置「待发位」，带「开始收句」按钮（CaptureArmAccept）；
;   · 字幕带问询：armed 中 ocr 边沿到 dormant（无区域休眠）→ 延迟 1.2s 再投递
;     （避开 armed-on 边沿的 toast/信息流行连环打扰），由 PetBandAskTick 消费。
PetSceneMaybe(prev, cur) {
    global G_Cfg, G_SceneInviteHold, G_CapArmedSince, G_CcReportSession, G_BandAskSession
    global G_CapNoSource
    out := []
    inv := PetSceneInviteCandidate(G_SceneInviteHold, cur, prev, G_Cfg, Random(0, 1000000))
    G_SceneInviteHold := inv.hold
    if (IsObject(inv.cand)) {
        inv.cand.label := "开始收句"
        inv.cand.fn := (*) => CaptureArmAccept()
        out.Push(inv.cand)
    }
    ; 字幕带问询：armed 中、这条没有英文 CC、屏幕采样还没有区域（dormant 边沿）。
    ; armed 刚开的前几拍不问（armed-on 的 toast/信息流行刚说过「已开启」）。
    ; #246：这一条会话的来源播报已经覆盖（待说或已说）→ 问询让位（「没有 CC」
    ; 不说两遍；要用户动手的部分由「需动手相位」候选继续提示）。
    if (cur.armed && !G_CapNoSource && cur.phase = "dormant" && prev.phase != "dormant") {
        armedJustNow := (G_CapArmedSince > 0 && A_TickCount - G_CapArmedSince < 5000)
        ccCovers := PetCcCoversBandAsk(cur.armed, cur.session, cur.cc, G_CcReportSession)
        if (PetBandAskEdge(cur.armed, cur.phase, prev.phase, armedJustNow, false, false
                         , ccCovers))
            SetTimer(PetBandAskTick, -1200)   ; 稍等一拍再弹（避开 armed-on 的 toast）
    }
    ; 2026-11-10 真机复核补：**没有边沿也要问**。最常见的路径是「先开收句 → 发现
    ; 没 CC」，此时 ocr 一上来就是 dormant，边沿早在 armed 之前就用掉了 → 一次都
    ; 不问（用户实测：她会说「没有 CC」，却从不问要不要框）。这里按持续条件补问
    ; 一次（每会话一次，等 armed 后 8s 避开 toast）；说过才记 session，被气泡占位
    ; 挡住时下一拍继续排队。
    ; 2026-11-11：delayMs 收到 1500（CC 判明后立刻问，给用户留足拖框时间；
    ; 10s 太晚——旧区域会先静默采废帧十几秒）；第 9 参 cap_no_source 守卫，
    ; 第 10 参 regionKnown：已有字幕带矩形就不再问，改由红框预览 + ^!6 兜底。
    if (PetBandOfferShould(cur.armed, cur.phase, cur.cc, cur.session, G_BandAskSession
                         , G_CapArmedSince, A_TickCount, 1500, G_CapNoSource
                         , G_CapBandRect != ""))
        SetTimer(PetBandAskTick, -1)
    return out
}

; 问询气泡（dormant 边沿后延迟 1.2s 的一次性拍子）：同样只投候选、不自己弹。
; 待弹位只消费一次（失败也不重弹——下一拍 dormant 边沿再来；绝不连弹骚扰）。
PetBandAskTick() {
    global G_Cfg, G_CapSession
    text := PetBandAskText(true, G_Cfg)
    if (text = "")
        return
    sess := G_CapSession
    PetSpeechMaybe([{ kind: "band_ask", text: text, action: ""
                    , source: "无 CC → 拖框"
                    , label: PetBandAskButtonLabel()
                    , fn: (*) => BandRedragStart()
                    , onSaid: (*) => PetBandAskMark(sess) }])
}

; 记「这个会话已经问过字幕带了」（每会话只问一次）。用命名函数而不是在闭包里
; 直接赋值：闭包里写全局不声明 global 会另起一个同名局部（AHK v2 没有超全局）。
PetBandAskMark(sess) {
    global G_BandAskSession
    if (sess > 0)
        G_BandAskSession := sess
}

; 气泡按钮 / 菜单共用的「同意开始收句」：armed 关着 → 发 capture_arm on；
; 同时按「当前是不是 B站视频页」决定要不要顺路下发全屏指令（hub 侧还会再验
; 一次 href 逐字相等；扩展侧也只认同一 href）。armed 已开着 → 只补拖框。
CaptureArmAccept() {
    global G_CapArmed
    if (G_CapArmed) {
        BandRedragStart()
        return
    }
    if (!HubSend('{"cmd":"capture_arm","op":"on"}', "capture_arm")) {
        ShowToast("数据中枢不在位，收句没打开", "err")
        return
    }
    Log("桌宠：同意收句（capture_arm on 已发出）")
    ; 2026-11-10 真机验收后删除：这里原来还会下发 `player_fullscreen`（扩展
    ; chrome.windows.update 切全屏）。用户实测的反馈是「浏览器标签栏和地址栏被
    ; 自动搞没了，只能 Ctrl+滚轮缩小找回」——强制全屏不是他要的动作，收句本身
    ; 也不需要它。**任何自动改变用户浏览器窗口形态的动作一律不做**；要全屏让
    ; 用户自己按 F11。hub 侧的 ext_command 槽保留（通用下行通道，当前无发放方）。
}

; 2026-11-10 真机验收后删除：下发全屏指令的实现（IssueFullscreen）与
; 「当前 href」查询（SceneCurrentHref）只服务那一件事，一并删掉，不留死代码。

; D5 动作通道：chatter / 事件动作 → 角色窗播一次。复用 PetApplyVisual 的同一
; 组动作状态位（G_PetMotion / G_PetMotionStart，PetWindow.ahk 归 A2）+ 戳一拍
; PetTick，不复制任何绘制逻辑；A2 的 PetIdleAmbientTick 走同一通道。
PetChatterEmitAction(action) {
    global G_PetManifest, G_PetMotion, G_PetMotionStart
    ; PetHealthy / PetClipResolve / PetTick 都在 PetWindow.ahk / PetFrames.ahk
    ; （主壳无条件 include），恒存在；pet 不健康时静默。
    if (action = "" || !PetHealthy())
        return
    if (!IsSet(G_PetManifest) || !IsObject(G_PetManifest))
        return
    mc := PetClipResolve(G_PetManifest, "motions", action)
    if (!IsObject(mc))
        return
    G_PetMotion := action
    G_PetMotionStart := A_TickCount
    PetTick()
}

; D5 接线点 D：挑词保存成功（OnHubLine 的 flush 槽）→ 信息流行 + praise 庆祝。
; #244：praise 也是「一条主动发言」——只投候选给唯一出口（勿扰 / 占位 / 闸门
; 都由仲裁与出口统一处理），不再自己弹气泡、自己记冷却。
PetPraiseCelebrate(hits) {
    global G_Cfg, G_CapAdded
    if (hits <= 0)
        return
    PetFeedSafeLine(PetFeedPickLine(hits, G_CapAdded), "ok", "praise")
    text := PetChatterPick("praise", {cfg: G_Cfg, n: hits, m: 0}, Random(0, 1000000))
    if (text = "")
        return
    PetSpeechMaybe([{ kind: "praise", text: text, action: "selfie_fast", source: "挑词" }])
}

; 中枢不在位：收句状态影子清空（tooltip 下一拍随 ping 恢复重建）。
CaptureStatusReset() {
    global G_CapArmed, G_CapOcr, G_CapSession, G_CapAgeMs, G_CapTip, G_TrayMode
    global G_CapPhase, G_CapAdded, G_CapLast, G_CapHeartbeatBad, G_CapArmedSince, G_CapArmedMin
    global G_CapScene, G_CapVideo, G_SceneInviteHold, G_SpeechMissLogged
    global G_CapCc, G_CcReportSession
    global G_CapEnded, G_CapEndedStreak, G_CapNoSource, G_CapNoSourceStreak
    global G_CapBandRect, G_CapAsr, G_CapAsrAdded, G_CapAsrReason, G_CapAutoStopAt
    G_CapArmed := false
    G_CapOcr := ""
    G_CapSession := 0
    G_CapAgeMs := -1
    ; v35 D1/D5：快照一并清零（hub 回来后的第一拍按「全新状态」发边沿行，
    ; 不拿掉线前的旧相位/旧句数对比）
    G_CapPhase := ""
    G_CapAdded := 0
    G_CapLast := ""
    G_CapHeartbeatBad := false
    G_CapArmedSince := 0
    G_CapArmedMin := 0
    ; v36：场景与问询待弹位一并清零（hub 回来后的第一拍按「无场景」重新判边沿）
    G_CapScene := ""
    G_CapVideo := 0
    ; v36 #246：来源影子位与「已播报会话」一并清零（回来第一拍按「未判明」重新判）
    G_CapCc := ""
    G_CcReportSession := 0
    ; 2026-11-11：v36 新增的播完/无字源/字幕带/ASR 快照一并清零，避免掉线前的旧值
    ; 在恢复第一拍产生虚假边沿（尤其 EndedStreak 会绕过「连两拍」去抖）。
    G_CapEnded := 0
    G_CapEndedStreak := 0
    G_CapNoSource := 0
    G_CapNoSourceStreak := 0
    G_CapBandRect := ""
    G_CapAsr := ""
    G_CapAsrAdded := 0
    G_CapAsrReason := ""
    G_CapAutoStopAt := 0
    G_SceneInviteHold := false
    G_SpeechMissLogged := ""
    G_TrayMode := "normal"
    TrayApplyIcon()
    CaptureHudHide()
    GlassCardReset()
    PetBubbleReset()
    if (G_CapTip != "英语取词 — 中枢不在位") {
        G_CapTip := "英语取词 — 中枢不在位"
        A_IconTip := G_CapTip
    }
}

; `session_peek` 的回话（§6.1）：chip 换成 hub 清洗后的标题 + 本会话词数。
; 直拍入口（无框）也会 peek —— 回话到的时候框可能还没建（OnHubLine 里只更状态，
; 建框时 BuildBox → ApplyVals → RenderChips 会带上它）。
;
; 票 #70 归属门：`askGen` = 发起这条 peek 时那次捕获的代次（由 OnHubLine 从队首的
; ask 里取）。peek 的回包要等 hub 侧本机探针（UIA 冷启动慢时最多 800ms）+ 排队，
; 而 BeginCapture 已经把标题/词数/站点/URL 清零给新一轮了 —— 代次不符就是上一轮
; 的回话，整包丢弃（只记「丢弃迟到回话」，不含标题/URL，§2.7）。
OnPeekLine(line, askGen := "") {
    global G_SessTitle, G_SessCount, G_SessOffline, Bgui, G_SessSite, G_BrowserUrl, G_CaptureGen
    if (!ReplyApplies(askGen, G_CaptureGen)) {
        Log("丢弃迟到的会话 peek 回话（捕获代次 "
          . (askGen = "" ? "缺失" : askGen) . " ≠ " . G_CaptureGen . "）")
        return
    }
    title := JsonField(line, "title")
    if (title = "") {
        Log("会话 peek 回话里没有标题，chip 退回离线口径")
        G_SessOffline := true
    } else {
        G_SessTitle := title
        G_SessCount := JsonNumber(line, "count")
        G_SessOffline := false
    }
    ; v3 §7：同一个回包里带浏览器上下文 —— 站点徽章、页面 URL、位置自动填
    G_SessSite := PeekSite(line)
    b := PeekBrowser(line)
    G_BrowserUrl := b.url
    ApplyAutoPosition(b.timeText)         ; 无框入口的框还没建时值先落 V，建框时带出来
    if (Bgui) {
        RenderChips()
        Relayout()                        ; 徽章出来了要重算第一行的宽度分配
    }
    ; 只记「新建还是复用 / 词数 / 分类 / 站点短名」——标题与 URL 一律不进日志（§2.7）
    cat := PeekCategory(line)
    Log("会话 peek：" . (JsonBool(line, "is_new") ? "将新建" : "复用")
      . "，本会话 " . G_SessCount . " 词"
      . (G_SessSite = "" ? "" : "，站点 " . G_SessSite)
      . (cat = "" ? "" : "，分类 " . cat))
}

; §7 位置自动填：peek 回包的 `browser.time_text` 非空、且「位置」**当时仍为空**才填。
; 回话是异步到的（用户可能已经在框里手打过位置了），所以判定在这里、不在发包时；
; 无框入口（直拍）回话先到时就落进 V，BuildBox → ApplyVals 会把它带进控件。
ApplyAutoPosition(timeText) {
    global V, C, Bgui
    if (timeText = "")
        return
    cur := (Bgui && C.HasOwnProp("pos")) ? C.pos.Value : V.pos
    if (!ShouldFillPosition(cur, timeText))
        return
    V.pos := timeText
    if (Bgui && C.HasOwnProp("pos"))
        C.pos.Value := timeText
    Log("位置已按浏览器上下文自动填入")
}

; 会话 chip 的数据来源（§6.1）。两条入口都在 BeginCapture 里调它 —— 直拍入口
; 一开始没有框，也得让 chip 一建出来就是真标题。hub 不在位 → 离线退化（§6.1）。
; v5 §2.4：带上呼出前抓到的 `hwnd`（0 = 不给）—— hub 侧据此对**这一只**窗口跑
; 本机探针（UIA 读地址栏），取回 session 链接；不给 / 非浏览器 → hub 静默不给链接。
; 票 #70：把**当前捕获代次**随 ask 入队，回话按它判归属（迟到的丢弃）。
HubPeek(app, title, hwnd := 0) {
    global G_SessOffline, Bgui, G_CaptureGen
    if (HubSend(HubPeekRequest(app, title, hwnd), "peek", G_CaptureGen))
        return
    G_SessOffline := true
    if (Bgui)
        RenderChips()
}

; ===========================================================================
;  媒体联动（v5 §3）：呼出停播 / 收工续播。机制在 hub 侧（Windows SMTC，
;  **不发任何按键** —— 项目红线）；外壳只负责在正确的时刻发 `media` RPC。
;  fire-and-forget：回包只记一行**状态字**（playing|paused|none|unavailable），
;  绝不弹提示、绝不改任何界面（§3「不许抢用户的控制」）。
; ===========================================================================
HubMedia(op, app, title) {
    return HubSend(HubMediaRequest(op, app, title), "media")
}

; `media` 的回包（§3 冻结的四个状态字）。**只记这一行日志** —— 不 toast、
; 不动 chip / 状态行；`op` 是我们自己刚发的那个（pause/resume），供日志对账。
; 只记枚举值，不记应用名/标题/内容（§10.2 不记用户内容）。
OnMediaLine(line) {
    Log("媒体联动回包：" . JsonField(line, "status")
      . (JsonBool(line, "paused_by_us") ? "（暂停由本工具压着）" : ""))
}

; 收工续播（§3 的触发点表）：**只在我们暂停过**（G_MediaPauseSent）时才发
; `resume`，发完立刻清标志 —— 重复调用（多条终止路径叠着走）不会重复发。
; 「保存并继续」（Enter）与「取消后又把框重现」都不走这里 → 视频保持暂停。
MediaResumeAfterCapture() {
    global G_MediaPauseSent, G_SessApp, G_SessRawTitle
    if (!G_MediaPauseSent)
        return
    G_MediaPauseSent := false
    if (!HubMedia("resume", G_SessApp, G_SessRawTitle))
        Log("媒体续播：中枢不在位，本次不发（视频不受影响）")
}

; ===========================================================================
;  释义自动填入（v2 §7 / 票 #27）：`lookup` 异步、不阻塞输入
;
;  目标词确定后（OCR 回填或手改）去抖 250ms 发一次 `lookup`；`key_lookup`
;  （默认 `Ctrl+L`）不等去抖立即重查。回话在 Socket 的 OnMessage 回调里到
;  （OnHubLine → OnLookupLine），主线程全程可打字。中枢不在位时静默（HubDown
;  已记日志 + 置灰），绝不刷屏；保存不受影响（spool 已落盘）。
; ===========================================================================

; 去抖（§7）：目标词变了就（重新）安排一次 `lookup`。force=true = 手动重查，
; 立即发（同一个词也重查）。状态判定在 Logic.DebounceStep / LookupDueWord。
ScheduleLookup(force := false) {
    global Bgui, V, G_LookupDeb, LOOKUP_DEBOUNCE_MS
    if (!Bgui)
        return
    word := NormalizeLine(V.target)
    if (word = "") {
        ; 目标词被清空：撤掉在途查询与「查询中」状态
        G_LookupDeb := 0
        SetDictWait(false)
        return
    }
    if (force) {
        G_LookupDeb := { word: word, at: A_TickCount }
        SetTimer(LookupFire, -1)
        return
    }
    st := DebounceStep(G_LookupDeb, word, A_TickCount, LOOKUP_DEBOUNCE_MS)
    G_LookupDeb := st.state
    if (st.schedule)
        SetTimer(LookupFire, -LOOKUP_DEBOUNCE_MS)
}

; v18 W4（#149）两段式回填的请求行：fast = 去抖首查（本地命中先回，可能带 provisional），
; full = provisional 之后的二跳（hub 跳过本地直查在线）。词的转义与 Logic.HubLookupRequest
; 同款（JsonString + NormalizeLine），帧格式仍是一行 JSON（N7）。
; v22 P5（#169 / spec §11）：多词目标额外带 `dict_only` —— hub 只做整条短语的词典查询
; （不查机翻）；单词目标不带这个键，请求行与 v18 逐字相同。
HubLookupRequestFast(word, dictOnly := false) => '{"cmd":"lookup","word":' . JsonString(NormalizeLine(word)) . ',"fast":true' . (dictOnly ? ',"dict_only":true' : '') . '}'
HubLookupRequestFull(word, dictOnly := false) => '{"cmd":"lookup","word":' . JsonString(NormalizeLine(word)) . ',"full":true' . (dictOnly ? ',"dict_only":true' : '') . '}'

; 去抖到点 / 手动重查：发一次 `lookup`，状态行先亮「词典查询中…」（§7）。
; v18 W4（#149）：首查带 fast —— 本地命中先到先显示（可能带 provisional，
; 回话到站后同词补发一次 full 二跳），本地全 miss 时 hub 同请求直接回最终结果。
LookupFire() {
    global V, G_LookupDeb, G_LookupDeadline, G_Status
    word := LookupDueWord(G_LookupDeb, V.target)
    if (word = "")
        return                                 ; 期间目标词又改了 → 交给新的一次去抖
    G_LookupDeb := { word: word, at: A_TickCount }
    ; v23 S2–S4：新一轮查询撤掉上一轮留下的失败/未收录/超时/后台补齐提示
    if (G_Status = "dict-failed" || G_Status = "dict-not-found"
        || G_Status = "dict-timeout" || G_Status = "meaning-pending")
        SetStatus("")
    if (!HubSend(HubLookupRequestFast(word, IsMultiwordTarget(word)), "lookup")) {
        ; 票 #185（A13）：发送失败 = 这次查询没出去，去抖状态必须归零 —— 否则残词
        ; 占着「同词在途」位，hub 恢复后重打/重框同一个词被 DebounceStep 拒绝，
        ; 永不自动补查。中枢不在位照旧静默（HubDown 已记日志，票 #27）。
        G_LookupDeb := LookupDebOnSendFailed(G_LookupDeb, word)
        return
    }
    SetDictWait(true)
    ; v23 S3（B5）：3.5s 无回话 → 撤掉「查询中」并给可读提示（Ctrl+L 重试）。
    ; 任何回话都会 SetDictWait(false) → 看门狗自然变 no-op；新查询会重置 deadline。
    G_LookupDeadline := A_TickCount + 3500
    SetTimer(LookupWatchdog, -3500)
    ; 只记词长，不记目标词本身（§10.2 不记用户内容）
    Log("lookup 已发出（" . StrLen(word) . " 字；去抖 " . LOOKUP_DEBOUNCE_MS . "ms）")
}

; v23 S3（B5）：lookup 看门狗。回话丢失 / 连接挂住时，状态行不再永远停在
; 「词典查询中…」：到 deadline 就撤掉查询中、亮「词典无响应（可 Ctrl+L 重试）」。
; 判定用 Logic.LookupWatchdogFired（纯函数，harness 片 34 钉）；只记一行，
; 不记目标词（§10.2）。迟到的回话照旧走 OnLookupLine 的归属门。
LookupWatchdog() {
    global G_DictWait, G_LookupDeadline
    if (!LookupWatchdogFired(G_DictWait, A_TickCount, G_LookupDeadline))
        return
    G_LookupDeadline := 0
    SetDictWait(false)
    SetStatus("dict-timeout")
    Log("lookup 3.5s 无回话，按超时收手（词典无响应）")
}

; `key_lookup`（默认 `Ctrl+L`，§9.1）：不等去抖，立即按当前目标词重查一次（§7）。
Relookup() {
    global Bgui, V
    if (!Bgui)
        return
    SyncFromCtl()
    if (IsTargetBlank(V.target)) {
        SetErrText("目标词不能为空")
        return
    }
    ScheduleLookup(true)
    Log("手动重查词典（key_lookup）")
}

; 从 `lookup` 回话里取 `seen` 子对象（§7；count=0 时 hub 不带这个键 → 0）。
JsonSeen(line) {
    if (!RegExMatch(line, '"seen"\s*:\s*\{'))
        return 0
    return { count: JsonNumber(line, "count")
           , first_at: JsonField(line, "first_at")
           , last_at: JsonField(line, "last_at")
           , last_session: JsonField(line, "last_session") }
}

; `lookup` 的回话（§7）：填音标 + 释义（按覆盖规则）+ `seen` 状态行。
; 覆盖规则：释义为空或仍等于上一次自动填入的值才覆盖；用户手打过就不覆盖、
; 只给一句状态行提示（票 #27）。音标无此限制（它没有手打的余地）。
OnLookupLine(line) {
    global V, C, Bgui, G_LastAutoMeaning, G_LookupFollowedUp, G_Status
    SetDictWait(false)
    if (!JsonBool(line, "ok")) {
        err := JsonField(line, "error")
        ; v23 S2（B4）：词典明确未收录 → 不再静默（告诉用户可手填 / 去取词台补查）；
        ; 超时/其它失败仍走「词典未返回，可手填」。
        if (err = "not found")
            SetStatus("dict-not-found")
        else
            SetStatus("dict-failed")
        Log("lookup 失败：" . (err = "" ? "未知" : err) . "（不影响保存）")
        return
    }
    if (!Bgui)
        return
    ; v4.1（#54 P1-1）：回话的 `word` 必须与**当前目标词**一致才写回——v4 保存并继续
    ; 不关框，词 A 的在途回话可能撞上已换到词 B 的槽位；不一致整条丢弃（只记一行日志，
    ; 不写 phonetic/meaning/dict_json/seen，也不记用户内容）。
    if (!LookupWordMatches(JsonField(line, "word"), V.target)) {
        Log("lookup 回话与当前目标词不符，丢弃（回话 " . StrLen(JsonField(line, "word"))
          . " 字，当前 " . StrLen(NormalizeLine(V.target)) . " 字）")
        return
    }
    ; v23 审核修正：迟到的成功回话证明词典其实有响应 —— 撤掉看门狗留下的
    ; 「词典无响应」提示。只在归属门通过之后清，换词后的迟到回话不许动状态行。
    if (G_Status = "dict-timeout")
        SetStatus("")
    phonetic := JsonField(line, "phonetic")
    ; v22 P5（#169 / spec §11）：多词目标只认 hub 的「整条短语词典命中」回包
    ; （dict_only:true）或**明确标注的机翻兜底**（machine:true，v22 修复 / #172）。
    ; 两者都没有 = 旧 hub / v18 的 web_trans·fanyi 机翻回包，一律不写释义（也不写
    ; 音标/dict_json）—— 宁愿释义留空，也不显示「whether the → 无论天气好不好」这种
    ; 错义。单词目标照旧放行（v18 行为零改动）。
    machine := JsonBool(line, "machine")
    if (!LookupMeaningApplies(V.target, JsonBool(line, "dict_only"), machine)) {
        Log("多词目标的回包不是词典命中/机翻兜底，释义/音标留空（"
          . StrLen(NormalizeLine(V.target)) . " 字）")
        return
    }
    if (phonetic != "")
        SetPhonetic(phonetic)                  ; 内部会归一化 + 只在变了时重排
    meaning := JsonField(line, "cn")
    if (meaning = "")
        meaning := JsonField(line, "en")
    if (meaning != "") {
        if (ShouldFillMeaning(C.meaning.Value, G_LastAutoMeaning, meaning)) {
            C.meaning.Value := meaning
            V.meaning := meaning
            ; v22 修复（#172）：机翻兜底要能存进 meaning_source（词典命中/手打留空）
            V.meaningSource := machine ? "machine" : ""
            G_LastAutoMeaning := NormalizeLine(meaning)
            SetNoteText("")
            if (machine)
                SetNoteText(StatusText("meaning-machine"))  ; 明确标注「机翻」
        } else {
            ; 用户手打过：不覆盖，只提示（票 #27）
            SetNoteText(StatusText("meaning-kept"))
        }
    }
    V.dictJson := line                        ; spool 的 dict_json 快照（§4.1）
    SetSeenText(SeenText(JsonSeen(line)))
    ; 只记来源/耗时/有无音标，不记释义（§10.2）
    Log("lookup 回话：" . JsonField(line, "source") . " " . JsonNumber(line, "ms") . "ms"
      . (JsonBool(line, "cached") ? "（缓存）" : "") . "，音标"
      . (phonetic = "" ? "无" : "有") . "，已记过 " . JsonNumber(line, "count") . " 次")
    ; v18 W4（#149）：provisional = 本地先到的预备释义 → 同词补发一次 full 二跳，
    ; 在线全量回话到站后走上面的同一条覆盖路径（LookupWordMatches / ShouldFillMeaning
    ; 照旧拦截换词与手改）。同词只补发一次：标记由 OnTargetChange / ResetForNextWord
    ; 清零；只记词长，不记词本身（§10.2）。
    ; v23 S6：判定抽成 Logic.LookupFollowUpWord（长文面板共用）——机翻兜底（machine）
    ; 永远不是 provisional：不补发 full 二跳（避免死循环）。
    followed := LookupFollowUpWord(line, G_LookupFollowedUp)
    if (followed != "") {
        G_LookupFollowedUp := followed
        if (HubSend(HubLookupRequestFull(followed, IsMultiwordTarget(followed)), "lookup"))
            Log("provisional 已显示，二跳 full 已发出（" . StrLen(followed) . " 字）")
    }
}

; 保存的后半程（§2）：通知中枢 ingest。返回 false = 中枢不在位 —— 不是保存失败
; （spool 已落盘即成功），调用方按 §10 报「已暂存」；true 时收据稍后由
; OnHubLine 的 flush 回话给出（dedup → 「本会话第 N 次」）。
HubFlush(target, meaningPending := false) {
    global G_HubFlushQ
    if (!HubSend(HubFlushRequest(), "flush"))
        return false
    ; v23 S4：`pending` 跟着这条保存走，回执 toast 才知道要不要并上「后台补齐」。
    G_HubFlushQ.Push({ target: target, pending: (meaningPending ? true : false) })
    ; 兜底收据：中枢接了命令但迟迟不回（端口被别的东西占着那种形态）时，
    ; 保存仍然必须有回执 —— 1.5s 没回话就按「已存 → 词」报，绝不静默。
    SetTimer(HubFlushTimeout, -1500)
    return true
}

HubFlushTimeout() {
    global G_HubFlushQ
    if (G_HubFlushQ.Length = 0)
        return
    ; v4.1（#54 P2-5）：兜底取**队首**（对号入座），队列里还有在途的再排空一拍
    entry := G_HubFlushQ.RemoveAt(1)
    target := ObjProp(entry, "target")
    pending := ObjProp(entry, "pending")
    Log("flush 1.5s 没有回话，按已存报收据（spool 已落盘）")
    ShowToast(SaveToastText("saved", target, 0, pending), "ok")
    if (G_HubFlushQ.Length > 0)
        SetTimer(HubFlushTimeout, -1)
}

; 捕获路径失败（OCR 失败 / 内核判死）：toast 报错、速记框关闭、本次不落盘
; （spec §5.5 / §11）
AbortCapture(msg) {
    global Bgui
    DestroyBox()
    ShowToast(msg, "err")
}

; OCR 成功 → 速记框出现并填原句 + 猜词目标词，焦点落目标词框（v4 §2；v2 §6.2 两入口共用）
; 票 #27：`corr` 是内核回的后校正清单（§5.2）→ 状态行「已校正 N 处：…」；
; v17 W5（票 #142）：`suspects` 是内核标的疑似粘连清单（只标记、不改原句）——
; 存进 G_OcrSuspects 供候选跳过（DropSuspectCands）、预填跳过（GuessTarget）与
; 「可手动改原句」提示（SuspectGlueHint）消费；旧内核没有这个字段 → 空 → 全部退化现状。
; 目标词定下后去抖发一次 `lookup`（异步，不阻塞输入）。
FillFromOcr(text, corr := [], suspects := 0) {
    global V, G_Common, Bgui, G_ShotBuildBox, G_CompIdx, G_TargetAuto, G_OcrSuspects
    if (!Bgui && !G_ShotBuildBox) {
        ; 无框入口会在下面把框建出来；这里挡住的是「框被别处收掉了」（§4.2 的保险）
        Log("OCR 回填时速记框已不在，丢弃结果")
        return
    }
    V.context := NormalizeLine(text)
    G_OcrSuspects := suspects
    if (IsObject(suspects) && suspects.Length > 0)
        Log("疑似粘连：收到 " . suspects.Length . " 处标记（候选/预填已跳过，可手动改原句）")
    V.target := GuessTarget(V.context, G_Common, G_OcrSuspects)
    ; v5 §4.1：记下「这个值是自动猜的」——首次聚焦整段全选（打一个字即替换），
    ; 用户一改（OnTargetChange）或换词重置（ResetForNextWord）就清空这个标记。
    G_TargetAuto := NormalizeLine(V.target)
    G_CompIdx := 0                    ; 回填换了原句/目标词 → 候选索引回第一项（§3.1）
    if (!Bgui)
        ShowBoxAtCursor()             ; 无框入口：OCR 回来才建框（§6.2）
    else {
        ApplyVals(V)                  ; 有框入口：回填那个被藏起来的框
        ShowBox()                     ; 落回鼠标位置（spec §4.2）
    }
    ; 后校正清单进状态行（票 #27：优先级 错误 > 已校正 > 已记过 > 查询中）
    SetCorrText(CorrectionsText(corr))
    ScheduleLookup()                  ; §7：目标词确定后去抖 250ms 发 lookup
    SetTimer(ApplyHintState, -1)      ; 提示行按新原句（O 提示 / 补全候选）重合成
}

; 无框入口的落点（§6.2 的字面名 `ShowBoxAtCursor`）：OCR 回来才建速记框。
; 出处早在遮罩之前就采集好、存在 V 里 —— 这里只把框造出来并按 §6.3 落在鼠标处；
; `Show` 会抢焦点 + `FocusDefault()`（v4 §2：一律落目标词框，即使已被猜词预填）。
ShowBoxAtCursor() {
    global Bgui, G_ShotBuildBox
    G_ShotBuildBox := false
    if (Bgui)
        return
    BuildBox()                        ; BuildBox 末尾自己 ApplyVals + RenderChips
    ShowBox()
    Log("OCR 回来：无框入口建框并显示（ShowBoxAtCursor）")
}

; spec §5.5：OCR 失败 / 空结果 → toast 报错（含留图路径）→ 本次不落盘
OcrFailed(errText, saved) {
    msg := "取词失败：" . errText
    if (saved != "")
        msg .= "（留图 " . saved . "）"
    AbortCapture(msg)
}

; 驱逐端口上的外部服务（内核或中枢）。返回被驱逐的 PID（0 = 端口本来空的 /
; 驱逐失败）。`label` 只用于日志点名（"内核" / "中枢"）。
; 端口是否在 LISTEN + 占用它的 PID（**实测 0ms**）：读 Windows 的 TCP 连接表。
;
; 为什么不用 netstat：`RunWait` 跑一次 `netstat -ano` 在本机实测 **2.2 秒**，
; 外壳启动时要查两个端口 = 3.4 秒白等 —— 用户说的「外壳内核中枢起得那么慢」
; 就是这两次 netstat（外壳自己 133ms 就绪，剩下的全耗在这儿）。
;
; 为什么不用 socket connect：本机实测同样是 **2 秒**（阻塞 connect 到未监听端口时
; Windows 不会立刻回 RST，而是按 SYN 重试走完）。三种写法都是 2 秒级，白折腾。
;
; TCP 表是**已经存在**的系统数据：读一遍 0ms，还能顺便拿到占用者的 PID，
; 于是「探活」和「找 PID」合并成一次调用（原来后者要跑 netstat）。
;
; 结构体 MIB_TCPROW_OWNER_PID（24 字节）：state@0 / localAddr@4 / localPort@8 /
; remoteAddr@12 / remotePort@16 / pid@20；表头是 DWORD 条数。端口在网络序的低 16 位。
TcpListenInfo(port) {
    size := 0
    DllCall("iphlpapi\GetExtendedTcpTable", "Ptr", 0, "UInt*", &size, "Int", 0
          , "Int", 2, "Int", 3, "UInt", 0)            ; AF_INET / TCP_TABLE_OWNER_PID_LISTENER
    if (size = 0)
        return { listening: false, pid: 0 }
    buf := Buffer(size, 0)
    rc := DllCall("iphlpapi\GetExtendedTcpTable", "Ptr", buf, "UInt*", &size, "Int", 0
                , "Int", 2, "Int", 3, "UInt", 0)
    if (rc != 0)
        return { listening: false, pid: 0 }
    n := NumGet(buf, 0, "UInt")
    off := 4
    loop n {
        localPort := NumGet(buf, off + 8, "UInt")
        state := NumGet(buf, off + 0, "UInt")
        owner := NumGet(buf, off + 20, "UInt")
        p := ((localPort & 0xFF) << 8) | ((localPort >> 8) & 0xFF)
        if (p = port && state = 2)                    ; MIB_TCP_STATE_LISTEN
            return { listening: true, pid: owner }
        off += 24
    }
    return { listening: false, pid: 0 }
}

; 端口上有没有人在听（薄封装：启动路径 / 待挑轮询这类只关心「有没有」的地方用）。
PortListening(port) {
    ; 异常一律当「有人在听」：这个函数只用来**省掉**一次 2 秒的等待，
    ; 它自己绝不能变成新的失败点（探活抛异常 → 调用方以为端口空 → 连都不连）。
    try
        return TcpListenInfo(port).listening
    catch
        return true
}
; 端口接管（决策 D12）：有外来者在听就驱逐，再让调用方拉起自己的。
;
; 2026-11-10：`snapshot` 是**一次 netstat 查多个端口**的结果（Map: port → pid）。
; 原来这里各自跑一次 `netstat -ano`（RunWait + 重定向临时文件，本机实测 1.3~2.2s），
; 启动时连跑两次（中枢 + 内核）= 3.4 秒白等 —— 这就是用户说的「外壳内核中枢起得那么慢」
; （外壳自己 133ms 就绪，剩下全耗在这两次 netstat 上）。合并成一次后省一半。
; 试过先用 socket 探活再查？不行：本机端口上若有残留监听，探活会连上并等 pong，
; 比 netstat 更慢（实测 4.1s）。
EvictPortSquatter(port, label, snapshot := "") {
    info := TcpListenInfo(port)
    if (!info.listening)
        return 0                          ; 端口空着：0ms 返回，一次 netstat 都不跑
    pid := info.pid
    if (pid = 0)
        return 0
    Log("端口 " . port . " 上有一个外部" . label . "在听（PID=" . pid . "），执行接管（决策 D12）")
    try {
        ; /T 连进程树一起收：venv 的 pythonw.exe 只是转发器（本机实测）
        RunWait('taskkill /F /T /PID ' . pid, , "Hide")
        Log("已驱逐外部" . label . "（taskkill /F /T /PID " . pid . "）")
    } catch as err {
        Log("驱逐外部" . label . "失败（PID=" . pid . "）：" . err.Message)
        return 0
    }
    ; 等端口释放，最多 3s（taskkill 返回后通常已释放，这里是兜底）
    waited := 0
    while (PortListening(port) && waited < 3000) {
        Sleep(100)
        waited += 100
    }
    if (PortListening(port))
        Log("端口 " . port . " 在 3s 后仍被占用，继续尝试拉起自己的" . label)
    else
        Log("端口 " . port . " 已释放（等待 " . waited . "ms）")
    return pid
}

StartKernel() {
    global G_Cfg, G_KernelPid
    exe := ResolveExe(G_Cfg.pythonExe)
    if (!FileExist(exe)) {
        Log("内核解释器不存在：" . exe . "（config 的 python_exe）")
        ShowBubble("找不到内核解释器，请改 config.ini 的 python_exe（详见 logs\app.log）", "英语取词", 3)
        return false
    }
    try {
        ; v2 的 Run 用输出参数回 PID（不是返回值）。拿到的是 venv 里的转发器
        ; pythonw.exe，真正绑端口的那个进程是它的子进程 —— 收尾要连树一起收。
        Run('"' . exe . '" "' . TOOL_DIR . '\kernel.py"', TOOL_DIR, "Hide", &pid)
        G_KernelPid := pid
        Log("已拉起内核 pid=" . (pid = "" ? "（没拿到 PID）" : pid) . "（" . exe . "）")
        return true
    } catch as err {
        Log("拉起内核失败：" . err.Message)
        ShowBubble("拉起 OCR 内核失败（详见 logs\app.log）", "英语取词", 3)
        return false
    }
}

StopKernel() {
    global G_KernelPid
    if (!G_KernelPid)
        return
    ; /T 连整棵进程树一起收：venv 的 pythonw.exe 只是转发器，
    ; 只杀它会留下真正绑着端口的子进程（本机实测踩到过）。
    try {
        RunWait('taskkill /F /T /PID ' . G_KernelPid, , "Hide")
        Log("已结束自己拉起的那个内核（taskkill /F /T /PID " . G_KernelPid . "）")
    } catch as err {
        Log("结束内核失败：" . err.Message)
    }
    G_KernelPid := 0
}

OnExitHandler(reason, code) {
    Log("---- 退出：" . reason . " ----")
    ; v35 #232 接线点 A：先收桌宠分层窗（不留无主窗口）
    PetShutdown()
    ; v35 #233 接线点 B：停待挑轮询 timer + 收气泡
    PetBubbleShutdown()
    ; v22 P2b：分段预览窗若还开着，退出前落盘几何并收窗（不留无主窗口）
    LfInspectorShutdown()
    ; v5 §3「不留暂停的残局」：退出前若还压着一次暂停就先续播。StopHub 用 taskkill /F
    ; （不是优雅退出），hub 侧那句「退出前续播」来不及跑，所以得由外壳先发这一条。
    MediaResumeAfterCapture()
    DisconnectSock()
    StopKernel()
    StopHub()                        ; 唯一所有者：exit 时把中枢进程树也收干净（§2）
}

; ---------------------------------------------------------------------------
;  速记框 v2 —— 构建（docs/specs/v2-workbench.md §6.3）
;
;  形态：宽 400 lp、折叠态高 ≤170 lp（v1 是 480×~400 的六字段长表单，挡死视频）；
;  三行常显（目标词 / 音标+释义 / 原句）+ 按钮行，`F2` 才展开元数据（出处/位置/标签）；
;  逐字段标签一律不画，改灰色占位提示（EM_SETCUEBANNER）；类型/会话 chip 用 ←→ 切；
;  `Ctrl+Enter` 存 / `Esc` 消 / `Ctrl+S` 截图 / `F1` 键位表 / `F3` 原句 3 行。
;
;  外形取舍：无标题栏（§6.3 的线框图没有标题栏，而标题栏白吃掉 ~37 lp 高度）+
;  自绘 1px 边（#2A2F36）+ 自绘圆角 12（DWM 的圆角偏好对无边框弹窗不生效）。
;  代价是拖不动框：v1 靠标题栏拖，v2 的定位是「鼠标右下、越界翻转」（§6.3 沿用 v1 §4.2）。
;
;  纯逻辑（焦点环 / chip 文案 / 类型推断 / 键位表文案）在 lib\Logic.ahk，
;  这里只做控件、事件与重排。
; ---------------------------------------------------------------------------
BlankV() {
    return { target: "", context: "", meaning: "", prov: "", provAuto: "", pos: "", tags: ""
           , phonetic: "", dictJson: "", meaningSource: "" }
}

hOf(ctl) {
    ControlGetPos(, , , &h, ctl.Hwnd)
    return h
}
wOf(ctl) {
    ControlGetPos(, , &w, , ctl.Hwnd)
    return w
}

; 控件里这段文字的**物理像素**宽度（站点徽章要让宽度跟着文字走，§7）：
; 取控件自己的字体（WM_GETFONT）在屏幕 DC 上量 —— 写死宽度要么截断
; 「Apple Podcasts」要么给「B站」留一片空洞。量出的是设备像素，正好是
; 这个 -DPIScale 的 Gui 要的坐标单位。
TextWidthPx(ctl, text) {
    hfont := DllCall("SendMessageW", "Ptr", ctl.Hwnd, "UInt", 0x0031, "Ptr", 0, "Ptr", 0, "Ptr")
    hdc := DllCall("GetDC", "Ptr", 0, "Ptr")
    old := DllCall("SelectObject", "Ptr", hdc, "Ptr", hfont, "Ptr")
    ext := Buffer(8, 0)
    DllCall("GetTextExtentPoint32W", "Ptr", hdc, "Str", text, "Int", StrLen(text), "Ptr", ext)
    DllCall("SelectObject", "Ptr", hdc, "Ptr", old, "Ptr")
    DllCall("ReleaseDC", "Ptr", 0, "Ptr", hdc)
    return NumGet(ext, 0, "Int")
}
Mv(ctl, x, y, w := -1) {
    if (w < 0)
        ctl.Move(x, y)
    else
        ctl.Move(x, y, w)
}

; 占位提示（§6.3：去掉逐字段标签，改灰色占位提示）：EM_SETCUEBANNER(0x1501)，
; 同进程直接把宽字符串指针给过去（票 #24 指定的做法）。失败就只记日志 ——
; 提示只是提示，缺了不影响任何行为。
SetCue(ctl, text) {
    try
        SendMessage(0x1501, 0, StrPtr(text), , ctl.Hwnd)
    catch as err
        Log("占位提示设置失败：" . err.Message)
}

; 焦点落到 Edit 上时 Windows 会全选文本（整块蓝底很抢眼）；另外程序写 `Ctl.Value := x`
; （WM_SETTEXT）会把光标**重置到 0**。这两处都要把光标收到末尾。
; ［2026-10-14 修复］原来发的是 `EM_SETSEL(-1,-1)`：它只**取消选择**，光标一步都不动 ——
; 只有当控件正处在「整块选中」时看着才像落在末尾（焦点回填那条路正是这样，所以一直没
; 暴露），而 `Value :=` 之后光标已经塌在 0，`(-1,-1)` 就把它留在词首了（Tab 接受补全
; 走的正是这条路）。改成 `EM_SETSEL(0x7FFFFFFF,0x7FFFFFFF)`：Edit 会把超界位置钳到
; 文本末尾，单行 / 多行都对（EM_GETSEL 实测见 tests/test_caret.py）。
CaretToEnd(ctl) {
    try
        SendMessage(0x00B1, 0x7FFFFFFF, 0x7FFFFFFF, , ctl.Hwnd)
}

; v5 §4.1：OCR 回填的**自动猜词**整段全选（EM_SETSEL(0,-1)），打第一个字即替换。
; 只在 FocusDefault 判出「值还是那个自动猜词」时才走它；其余一切聚焦仍是 CaretToEnd
; （v1 的刻意取舍：用户自己的文字不留蓝底）。
SelectAllText(ctl) {
    try
        SendMessage(0x00B1, 0, -1, , ctl.Hwnd)
}

; 圆角 12（§6.3）：无边框弹窗不吃 DWM 的圆角偏好，自己切一块圆角区域
RoundWin(hwnd, w, h) {
    radius := P(LP.RADIUS)
    r := DllCall("CreateRoundRectRgn", "Int", 0, "Int", 0, "Int", w + 1, "Int", h + 1
               , "Int", radius, "Int", radius, "Ptr")
    DllCall("SetWindowRgn", "Ptr", hwnd, "Ptr", r, "Int", 1)
}

BuildBox() {
    global Bgui, C, LP, Hint, G_Title, G_CtxRows, G_GripHwnd
    th := Theme()
    BoxKeys(false)                        ; 旧的框要销毁：先把框内键环摘掉
    if (Bgui) {
        deadBox := Bgui                   ; 热修：先摘引用再销毁 —— DestroyWindow 内部泵消息
        Bgui := 0                         ; 期间 WM_SETCURSOR 携框 hwnd 进监视器，Bgui 未清时
        try deadBox.Destroy()             ; 读 `.Hwnd` 抛「Gui has no window」（错误框带 Exit App）
    }
    KeymapClose()
    C := {}

    ; 窗体底色 = 1px 边色；内面板铺满客户区 = 卡片底色
    ; （面板**先**建，后建的控件都压在它上面）
    g := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow", G_Title)
    g.BackColor := th.border
    C.panel := g.AddText("x" . P(LP.BORDER) . " y" . P(LP.BORDER) . " w1 h1 Background" . th.win, "")

    ; ---- 第一行：目标词（主场，11pt 半粗）+ 类型 chip + 会话 chip ----
    g.SetFont("s11 c" . th.text, "Segoe UI Semibold")
    C.target := g.AddEdit("r1 -E0x200 +Background" . th.field)
    SetCue(C.target, "目标词")
    ; chip 是 ReadOnly Edit：能聚焦（进焦点环）、能收 ←→，但打不进字；
    ; `-E0x200` 去掉窄边框，`+Background` 上色 —— 「像 chip 不像按钮」
    ; （原生按钮要么胖要么恒为浅底，AddText 又不可聚焦；真机截图比对过，见票 #24 评论）
    g.SetFont("s9 c" . th.text, "Segoe UI")
    C.kind := g.AddEdit("r1 ReadOnly Center -E0x200 +Background" . th.chip)
    C.session := g.AddEdit("r1 ReadOnly -E0x200 +Background" . th.chip)
    ; 站点徽章（§7）：peek 回包的 `site` 非空才出现，显隐与宽度都归 LayoutBox 管。
    ; 用 Static 而不是像其它 chip 那样用 ReadOnly Edit —— Static 的 WM_NCHITTEST
    ; 是 HTTRANSPARENT（**不在**交互控件之列），按在徽章上照样能拖整个框；
    ; `+0x200` = SS_CENTERIMAGE，让文字在 chip 高度里垂直居中。
    C.site := g.AddText("h" . hOf(C.target) . " +0x200 Background" . th.chip, "")

    ; 拖拽把手（v5 §5）：第一行**最右**的 Static，唯一的「看起来能抓」的东西 ——
    ; 卡片几乎全是 Edit/chip/按钮，没有它用户找不到能抓的地方（用户报的「拖不动」）。
    ; 用 Static（与站点徽章同一手法）：它自己的 WM_NCHITTEST 是 HTTRANSPARENT，
    ; 按在把手上照样拖整个框（HitTestHandler 把父窗口那块判成 HTCAPTION）；
    ; Static 不进焦点环，Tab 不会停在它上面。悬停变四向箭头在 CursorHandler。
    ; 底色 = 卡片底色（不加 chips 底色：把手是「可抓的空白」，不是又一枚 chip）。
    g.SetFont("s9 c" . th.sub, "Segoe UI")
    C.grip := g.AddText("h" . hOf(C.target) . " +0x200 Background" . th.win, "⋮⋮")
    G_GripHwnd := C.grip.Hwnd                ; 只留整数（CursorHandler 用；见模块级注释）

    ; ---- 第二行：音标（词典填，空则不占位）+ 释义 ----
    ; 音标用等宽体（票 #27 的「等宽灰字」）：词典音标含 IPA 符号，比例字体下宽度
    ; 抖动会让它和左边距看起来忽宽忽窄；Consolas 在本机恒有。
    g.SetFont("s9 c" . th.sub, "Consolas")
    C.phon := g.AddText("Background" . th.win)
    g.SetFont("s10 c" . th.text, "Segoe UI")
    C.meaning := g.AddEdit("r1 -E0x200 +Background" . th.field)
    SetCue(C.meaning, "释义（自动填入，可改）")

    ; ---- 第三行：原句（`F3` 展成 3 行）----
    C.context := g.AddEdit("r" . G_CtxRows . " -VScroll -E0x200 +Background" . th.field)
    SetCue(C.context, "原句")

    ; ---- 元数据（`F2` 展开）：出处 / 位置+粘贴 / 标签 ----
    g.SetFont("s10 c" . th.text, "Segoe UI")
    C.prov := g.AddEdit("r1 -E0x200 +Background" . th.field)
    SetCue(C.prov, "出处")
    C.pos := g.AddEdit("r1 -E0x200 +Background" . th.field)
    SetCue(C.pos, "位置")
    g.SetFont("s9 c" . LP.BTN_TEXT, "Segoe UI")
    C.paste := g.AddButton("h" . P(LP.BTN_H), "粘贴")
    g.SetFont("s10 c" . th.text, "Segoe UI")
    C.tags := g.AddEdit("r1 -E0x200 +Background" . th.field)
    SetCue(C.tags, "标签")

    ; ---- 分隔线 / 状态行 / 提示行 ----
    C.rule := g.AddText("h" . P(1) . " Background" . th.rule, "")
    g.SetFont("s8 c" . th.sub, "Segoe UI")
    C.status := g.AddText("Background" . th.win, "")
    C.hint := g.AddText("Background" . th.win, "")

    ; ---- 按钮行（按钮文字自带键位，§6.3）+ 右下角常驻键位提示 ----
    g.SetFont("s9 c" . LP.BTN_TEXT, "Segoe UI")
    C.bSave := g.AddButton("h" . P(LP.BTN_H) . " default", "保存 ↵")
    C.bCancel := g.AddButton("h" . P(LP.BTN_H), "取消 Esc")
    C.bShot := g.AddButton("h" . P(LP.BTN_H), "截图 ⌃S")
    C.bMore := g.AddButton("h" . P(LP.BTN_H), "更多 F2")
    g.SetFont("s8 c" . th.sub, "Segoe UI")
    C.keyhint := g.AddText("Background" . th.win, "F1 全部快捷键")

    Bgui := g

    ; ---- 值 / chip / 状态先落一遍（控件刚建出来是空的）----
    ApplyVals(V)
    RenderChips()

    ; ---- 事件 ----
    C.bSave.OnEvent("Click", (*) => DoSave("next"))       ; 点按钮 = 「保存并继续」（v4 §4.1）
    C.bCancel.OnEvent("Click", (*) => DoCancel())
    C.bShot.OnEvent("Click", (*) => ShotFromBox())
    C.bMore.OnEvent("Click", (*) => ToggleMore())          ; 点「更多」等价按 F2（票 #24）
    C.paste.OnEvent("Click", (*) => DoPastePos())
    C.context.OnEvent("Change", OnContextChange)
    C.target.OnEvent("Change", OnTargetChange)
    ; chip 的 ←→ 只在 chip 聚焦时注册：不在 chip 上时箭头必须留给 Edit 的光标
    C.kind.OnEvent("Focus", (*) => ChipFocus("kind"))
    C.kind.OnEvent("LoseFocus", (*) => ChipBlur("kind"))
    C.kind.OnEvent("Change", (*) => ChipReset("kind"))
    C.session.OnEvent("Focus", (*) => ChipFocus("session"))
    C.session.OnEvent("LoseFocus", (*) => ChipBlur("session"))
    C.session.OnEvent("Change", (*) => ChipReset("session"))
    g.OnEvent("Escape", (*) => BoxEsc())   ; 与框内取消键同一条路（keydown 置位、抬起收工）
    g.OnEvent("Close", (*) => DoCancel())
}

; 一次算完全部位置与显隐，返回窗口应有的物理高度（含上下 1px 边）。
; 显隐规则（§6.3「无内容时不占位」）：音标 / 状态行 / 提示行 / 元数据都不留空位。
LayoutBox() {
    global C, LP, Hint, V, G_Status, G_StatusArg, G_Expanded
    global G_CorrText, G_SeenText, G_DictWait, G_NoteText, G_SessSite
    th := Theme()
    fw := P(LP.W) - 2 * P(LP.BORDER)                  ; 内面板宽（= 卡片宽）
    x0 := P(LP.BORDER) + P(LP.PAD)
    cw := fw - 2 * P(LP.PAD)
    kchipW := P(LP.CHIP_K), schipW := P(LP.CHIP_S)
    ; 站点徽章（§7）：`site` 为空就整块不占位（不许空徽章、不许抖布局）—— 宽度也不
    ; 预先留：有站点时目标词那格才让出徽章 + 一个间距。宽度按文字实测（TextWidthPx）。
    siteW := 0
    if (G_SessSite != "") {
        C.site.Text := G_SessSite
        siteW := Min(TextWidthPx(C.site, G_SessSite) + 2 * P(LP.CHIP_SITE_PAD)
                   , P(LP.CHIP_SITE))
    }
    ; v5 §5：第一行宽度按 `目标词 > chip > 徽章 > 把手` 分配；目标词被挤到
    ; `LP.TARGET_MIN` 以下时**先撤徽章**再算（算式是纯函数，harness 钉三种情形）。
    ; 徽章最终显不显示由 siteShown 说了算 —— 不是「有站点就显示」。
    gripW := P(LP.GRIP_W)
    w1 := RowOneWidths(cw, kchipW, schipW, gripW, siteW, P(LP.CHIP_GAP), P(LP.TARGET_MIN))
    tgtW := w1.tgtW
    C.site.Visible := (G_SessSite != "" && w1.siteShown)
    if (!w1.siteShown)
        siteW := 0                                    ; 撤掉就不占位（也不摆那块）
    lineH := hOf(C.keyhint)                           ; 单行小字的高度基准（s8）

    y := P(LP.BORDER) + P(LP.PAD)

    ; 第一行
    Mv(C.target, x0, y, tgtW)
    Mv(C.kind, x0 + tgtW + P(LP.CHIP_GAP), y, kchipW)
    Mv(C.session, x0 + tgtW + P(LP.CHIP_GAP) + kchipW + P(LP.CHIP_GAP), y, schipW)
    if (siteW > 0)
        Mv(C.site, x0 + tgtW + P(LP.CHIP_GAP) + kchipW + P(LP.CHIP_GAP) + schipW
                + P(LP.CHIP_GAP), y, siteW)
    ; 把手贴第一行最右 —— 与站点徽章显不显示无关（它占的是独立的一格）
    Mv(C.grip, x0 + cw - gripW, y, gripW)
    y += hOf(C.target) + P(LP.GAP)

    ; 第二行：音标有值才占位
    hasPhon := (NormalizeLine(V.phonetic) != "")
    C.phon.Visible := hasPhon
    phonW := hasPhon ? P(LP.PHON) : 0
    if (hasPhon) {
        C.phon.Text := V.phonetic
        Mv(C.phon, x0, y + P(3), phonW)
    }
    Mv(C.meaning, x0 + phonW, y, cw - phonW)
    y += hOf(C.meaning) + P(LP.GAP)

    ; 第三行
    Mv(C.context, x0, y, cw)
    y += hOf(C.context) + P(LP.GAP)

    ; 元数据（`F2`）
    C.prov.Visible := G_Expanded, C.pos.Visible := G_Expanded
    C.paste.Visible := G_Expanded, C.tags.Visible := G_Expanded
    if (G_Expanded) {
        Mv(C.prov, x0, y, cw)
        y += hOf(C.prov) + P(LP.GAP)
        tagX := x0 + P(LP.POS_W) + P(LP.PASTE_GAP) + wOf(C.paste) + P(LP.POS_TAG_GAP)
        Mv(C.pos, x0, y, P(LP.POS_W))
        Mv(C.paste, x0 + P(LP.POS_W) + P(LP.PASTE_GAP), y)
        Mv(C.tags, tagX, y, x0 + cw - tagX)
        y += Max(hOf(C.pos), hOf(C.paste)) + P(LP.GAP)
    }

    ; 分隔线
    Mv(C.rule, x0, y, cw)
    y += P(1) + P(LP.GAP)

    ; 状态行（§6.3：一行灰字；空就不占位）。
    ; 票 #27：多个候选（错误 / 已校正 / 已记过 / 查询中 / 已手打提示）同屏只显示
    ; 一条，优先级由 Logic.StatusPick 定（有测试钉住）。
    stText := StatusPick(StatusText(G_Status, G_StatusArg), G_CorrText, G_SeenText
                       , G_DictWait, G_NoteText)
    C.status.Visible := (stText != "")
    if (stText != "") {
        if (C.status.Text != stText)
            C.status.Text := stText
        C.status.Move(x0, y, cw, lineH)
        y += lineH + P(LP.GAP)
    }

    ; 提示行（v1 的两条：原句里的独立 O、保存失败红字）
    C.hint.Visible := (Hint != 0)
    if (Hint) {
        C.hint.SetFont("c" . (Hint.kind = "err" ? th.err : th.sub))
        if (C.hint.Text != Hint.text)
            C.hint.Text := Hint.text
        C.hint.Move(x0, y, cw, lineH)
        y += lineH + P(LP.GAP)
    }

    ; 按钮行 + 右下角常驻键位提示
    Mv(C.bSave, x0, y)
    bx := x0 + wOf(C.bSave) + P(LP.BTN_GAP)
    Mv(C.bCancel, bx, y)
    bx += wOf(C.bCancel) + P(LP.BTN_GAP2)
    Mv(C.bShot, bx, y)
    bx += wOf(C.bShot) + P(LP.BTN_GAP2)
    Mv(C.bMore, bx, y)
    Mv(C.keyhint, x0 + cw - wOf(C.keyhint), y + P(6))
    y += hOf(C.bSave) + P(LP.PAD)

    ; 内面板铺满 1px 边以内
    C.panel.Move(P(LP.BORDER), P(LP.BORDER), fw, y - P(LP.BORDER))
    return y + P(LP.BORDER)
}

; 值 → 控件（v1 的 ApplyVals + v2 的 chip 渲染）
ApplyVals(v) {
    global C, G_Kind, G_KindTouched, Bgui
    if (!Bgui)
        return                                    ; 框不在 → 没有控件可写（F1 类守卫）
    C.target.Value := v.target
    C.context.Value := v.context
    C.meaning.Value := v.meaning
    C.prov.Value := v.prov
    C.pos.Value := v.pos
    C.tags.Value := v.tags
    ; 类型默认按目标词推断（§6.3）；用户手动切过就不再覆盖（「默认」二字的含义）
    if (!G_KindTouched)
        G_Kind := InferKind(v.target)
    RenderChips()
    SyncSaveButton()
}

; 保存按钮文案（v4 §4.1，票 #51）：空目标词且已存过 ≥1 词 → 「收工 ↵」，否则「保存 ↵」。
; 两个文案像素宽一致，改文案不会顶到右下角键位提示，所以不重排（Relayout）以免打字时抖。
SyncSaveButton() {
    global C, V, G_SavedCount, Bgui
    if (!Bgui || !C.HasOwnProp("bSave"))
        return
    want := SaveButtonLabel(IsTargetBlank(V.target), G_SavedCount)
    if (C.bSave.Text != want)
        C.bSave.Text := want
}

SyncFromCtl() {
    global C, V, Bgui
    if (!Bgui)
        return
    for name in ["target", "context", "meaning", "prov", "pos", "tags"] {
        if (C.HasOwnProp(name))
            V.%name% := C.%name%.Value
    }
}

; chip 的文案与状态同源：Logic 出文案，控件只做显示
RenderChips() {
    global C, G_Kind, G_SessTitle, G_SessCount, G_SessNew, G_SessOffline, Bgui
    if (!Bgui)
        return                                    ; 异步 peek 回话可能晚于收工关框
    C.kind.Value := KindLabel(G_Kind)
    C.session.Value := SessionChipText(G_SessTitle, G_SessCount, G_SessNew, G_SessOffline)
}

; chip 被聚焦 / 失焦：只有聚焦时才注册 ←→（否则会抢 Edit 的光标键），
; 并给出聚焦视觉（强调色 + 提亮底色）
ChipFocus(name) {
    global C
    th := Theme()
    ChipKeys(true)
    ctl := C.%name%
    ctl.SetFont("c" . th.accent)
    try
        ctl.Opt("+Background" . th.chipOn)
}

ChipBlur(name) {
    global C
    th := Theme()
    ChipKeys(false)
    ctl := C.%name%
    ctl.SetFont("c" . th.text)
    try
        ctl.Opt("+Background" . th.chip)
}

; chip 是 ReadOnly：理论上改不动。兜一个 Change（万一将来换成可编辑的），
; 内容一律以状态为准，不让控件自己变成真源。
ChipReset(name) {
    global C, G_Kind, G_SessTitle, G_SessCount, G_SessNew, G_SessOffline
    want := (name = "kind") ? KindLabel(G_Kind)
                            : SessionChipText(G_SessTitle, G_SessCount, G_SessNew, G_SessOffline)
    if (C.%name%.Value != want)
        C.%name%.Value := want
}

; chip 上的 ←→（§6.3）：类型在 词/短语/句 之间环切；会话在 当前会话 / (新会话) 之间切
ChipLeft() => ChipStep(-1)
ChipRight() => ChipStep(1)

ChipStep(dir) {
    global C, G_Kind, G_KindTouched, G_SessNew
    if (C.kind.Focused) {
        G_Kind := CycleKind(G_Kind, dir)
        G_KindTouched := true
        RenderChips()
        Log("类型切到 " . G_Kind)              ; 只记枚举值，不记用户内容（§10.2）
    } else if (C.session.Focused) {
        G_SessNew := !G_SessNew
        RenderChips()
        Log("会话 chip 切到 " . (G_SessNew ? "新会话" : "当前会话"))
    }
}

; chip 的 ←→ 注册 / 摘除（作用域 = 速记框）
ChipKeys(on) {
    global G_BoxWin
    if (G_BoxWin = "")
        return
    HotIfWinActive(G_BoxWin)
    try {
        if (on) {
            Hotkey("Left", (*) => ChipLeft(), "On")
            Hotkey("Right", (*) => ChipRight(), "On")
        } else {
            Hotkey("Left", "Off")
            Hotkey("Right", "Off")
        }
    } catch as err {
        Log("chip 键注册失败：" . err.Message)
    }
    HotIf()
}

; 目标词边打边推类型（用户手动切过就不再覆盖）；票 #27：目标词确定后去抖查词典
OnTargetChange(*) {
    global C, V, G_Kind, G_KindTouched, G_CompIdx, G_TargetAuto, G_LastAutoMeaning
    global G_LookupFollowedUp
    V.target := C.target.Value
    ; v5 §4.1：用户真的动了目标词 → 这个值不再是「自动猜词」，下次聚焦不再整段全选
    G_TargetAuto := ""
    ; v5 §6：上一个词的自动释义还挂在释义框里（未手打）→ 立刻清掉释义 / 音标 /
    ; dict_json，免得它跟着新词一起被存进去。必须排在 ScheduleLookup **之前**：
    ; 去抖 250ms 后新词的 lookup 回话才有资格按 ShouldFillMeaning 把它填回来。
    ; 用户手打过的释义（与 G_LastAutoMeaning 不同）不受影响 —— 判定在 Logic 纯函数里。
    if (ShouldClearAutoMeaning(C.meaning.Value, G_LastAutoMeaning)) {
        C.meaning.Value := ""
        V.meaning := ""
        V.dictJson := ""
        V.meaningSource := ""                   ; 上一条词的机翻来源标记一起清掉
        SetPhonetic("")                        ; 音标也属于上一个词
        G_LastAutoMeaning := ""
    }
    ; v22 P5（票 #169 / spec §11）：目标变成多词 → 上一条词的单词词典结论不再适用。
    ; 音标只有「词典自动填入」一条来路（用户打不了），多词目标一律清掉；dict_json
    ; 是上一条词典快照，也不许跟着短语落库。手打过的释义属用户意图，照旧不动。
    if (IsMultiwordTarget(V.target)) {
        V.dictJson := ""
        SetPhonetic("")
    }
    if (!G_KindTouched) {
        G_Kind := InferKind(V.target)
        RenderChips()
    }
    ; 目标词变了：旧的「已记过 / 已手打」提示不再作数，按去抖重查（§7）
    SetSeenText("")
    SetNoteText("")
    G_LookupFollowedUp := ""                   ; v18 W4（#149）：新词的二跳标记归零
    ; 目标词从空变非空：撤掉「目标词不能为空」那条红字（空白时手动重查留下的）
    G_CompIdx := 0                             ; 前缀变了 → 候选索引回第一项（§3.1）
    SetErrText("")
    SyncSaveButton()                           ; 目标词空/非空切换 → 已存过时按钮在「收工/保存」间切
    ApplyHintState()                           ; 候选跟着新前缀重算（§3.3）
    ScheduleLookup()
}

; 「O」提示行 —— 只在「有没有这一行」真的变了时才重排（v1 是重建框）
OnContextChange(*) {
    global C, V, G_CompIdx
    V.context := C.context.Value
    G_CompIdx := 0                             ; 原句变了 → 候选集重算，索引回第一项
    SetTimer(ApplyHintState, -1)
}

; 提示行的合成（v4 §3.3 + v5 §4.2）：按优先级取第一个非空来源；全空则整行不占位。
; 来源各自状态化（G_ErrText / 补全（现算）/ HasLoneO / 已存 N 词），任一变化都重走这里。
; v5 §4.2 的优先级（冻结）：err ＞ 补全候选 ＞ O 提示 ＞ 已存 N 词 ＞ 空闲补全 ＞ 无匹配。
; 最后两条互斥（空闲要求目标词为空、无匹配要求 ≥2 字母前缀），但都排在「已存 N 词」
; 之后 —— 一句多词时用户先看到的是「已存 N 词」而不是泛泛的功能提示。
ApplyHintState() {
    global V, G_ErrText, G_CompIdx, G_SavedCount, G_Common, G_OcrSuspects
    ; #102：候选/来源一次算齐（提示行要标「句内 / 近似 / 常用词」）；优先级数组不动。
    ; v17 W5（票 #142）：疑似粘连 token 不作候选 —— 剔除后顺序与来源标签不变；
    ; 旧内核无标记（G_OcrSuspects 空）时原样返回，行为逐字不变。
    tiers := CompletionTiers(V.context, V.target, KnownWords(), G_Common)
    tiers := DropSuspectCands(tiers.words, G_OcrSuspects, tiers.sources)
    compText := CompletionHintText(tiers.words, G_CompIdx, tiers.sources)
    ; v17 W3（票 #139 / D4）：下一动作判定与 CompTab 共用 TabNextAction —— 尾随空白
    ; （顺句状态）时接词提示压过补全提示，提示说「接词」Tab 就真的接词（同源）。
    act := TabNextAction(V.context, V.target, tiers.words, G_CompIdx)
    if (act.action = "chain")
        compText := act.hint
    if (compText = "")
        compText := ChainHintText(V.context, V.target)   ; 顺句接词：补全候选档的补位来源
    ; v17 W4（票 #141）：`Ctrl+Tab` 接词提示 —— 与顺句提示同一槽位的兜底末位；
    ; 有补全候选时补全提示仍按现有优先级展示（Ctrl+Tab 键本身始终可用）。
    if (compText = "")
        compText := ChainNextHintText(V.context, V.target)
    oText := HasLoneO(V.context) ? "检测到 O，若原文是数字 0 请手动改正" : ""
    h := HintPick([{ kind: "err",  text: G_ErrText }
                 , { kind: "hint", text: compText }
                 ; v17 W5（票 #142）：疑似粘连提示 —— 补全候选之后、已存 N 词之前（不盖 err）；
                 ; 输入是疑似 token 前缀且无可用候选时，解释「Tab 为什么没反应」。
                 , { kind: "hint", text: SuspectGlueHint(V.target, G_OcrSuspects, tiers.words) }
                 , { kind: "hint", text: oText }
                 , { kind: "hint", text: SavedHintText(G_SavedCount, IsTargetBlank(V.target)) }
                 , { kind: "hint", text: CompletionIdleHint(V.context, V.target) }
                 , { kind: "hint", text: CompletionMissHint(V.context, V.target) }])
    SetHint(h ? h.kind : "", h ? h.text : "")
}

; #102：补全「词表校验」的已知词集 = 2000 常见词 ∪ data\words_10k.txt。
; 为什么惰性：票面红线是启动时间不得增加 >50ms —— 启动只读 2000 词表（现状），
; 10k 表等第一次真的要算补全/提示时才读一次并缓存（第一次调用发生在 OCR 回填
; 之后，不在启动路径上；本机实测 9894 词首次加载 ≈78ms，只付一次）。
; 为什么要并集：2000 表连 `reluctant`、`reinforce` 都不收，只拿它判 OOV 会把大量
; 真词误判成粘连串；`words_10k.txt` 正是内核 corrections 的 known_words 之一
; （corrections.py:370），两边口径一致。
KnownWords() {
    global G_Common, G_Known, WORDS_10K_PATH
    if (G_Known)
        return G_Known
    t0 := A_TickCount
    G_Known := Map()
    if (IsObject(G_Common)) {
        for w, rank in G_Common
            G_Known[w] := rank
    }
    if (FileExist(WORDS_10K_PATH)) {
        for w, rank in LoadCommonWords(WORDS_10K_PATH)
            if (!G_Known.Has(w))
                G_Known[w] := 0
    }
    Log("补全校验词表 " . G_Known.Count . " 词（common ∪ words_10k，惰性加载 "
        . (A_TickCount - t0) . "ms）")
    return G_Known
}

; 红字提示的来源（保存失败 / 目标词为空）：写状态再合成，不让调用方直接碰 Hint
SetErrText(text) {
    global G_ErrText
    if (text = G_ErrText)
        return
    G_ErrText := text
    ApplyHintState()
}

SetHint(kind, text) {
    global Hint
    Hint := (kind = "") ? 0 : { kind: kind, text: text }
    Relayout()
}

; 提示行的来源一次归零（新捕获 / 销毁框 / demo）：上一轮的 err 与补全不带过来；
; 本框累计保存数也一并清零（新捕获必须为 0，v4 §4.1）。
ResetHint() {
    global Hint, G_ErrText, G_CompIdx, G_SavedCount, G_OcrSuspects
    Hint := 0, G_ErrText := "", G_CompIdx := 0, G_SavedCount := 0
    G_OcrSuspects := 0                        ; v17 W5：上一轮的疑似标记不带过来（新捕获会重设）
}

; 状态行（§6.3）：代码进、文案出（Logic.StatusText）；空文案不占位
SetStatus(code, n := 0) {
    global G_Status, G_StatusArg
    if (code = G_Status && n = G_StatusArg)
        return
    G_Status := code, G_StatusArg := n
    Relayout()
}

; 「已校正 N 处：…」状态行（票 #27）：文案已在 Logic.CorrectionsText 组装好，这里只管落状态
SetCorrText(text) {
    global G_CorrText
    if (text = G_CorrText)
        return
    G_CorrText := text
    Relayout()
}

; 「已记过 N 次 · 上次…」状态行（§7 的 seen）；空串不显示
SetSeenText(text) {
    global G_SeenText
    if (text = G_SeenText)
        return
    G_SeenText := text
    Relayout()
}

; 「词典查询中…」（§7 异步查询期间）
SetDictWait(on) {
    global G_DictWait
    on := (on ? true : false)
    if (on = G_DictWait)
        return
    G_DictWait := on
    Relayout()
}

; 「释义已手打，词典结果未覆盖」一类的低优先级提示（票 #27）
SetNoteText(text) {
    global G_NoteText
    if (text = G_NoteText)
        return
    G_NoteText := text
    Relayout()
}

; 音标（词典填；C4 接 lookup）：有值才占位，所以变了要重排
SetPhonetic(text) {
    global V
    t := NormalizeLine(text)
    if (t = V.phonetic)
        return
    V.phonetic := t
    Relayout()
}

; 就地重排（F2/F3 的状态变化、状态行/提示行变化都走这条路）：
; 不重建控件 → 位置与焦点都不跳（§6.3 的「同一窗内展开收起」）
Relayout() {
    global Bgui, BoxPos, LP
    if (!Bgui)
        return
    h := LayoutBox()
    Bgui.Move(BoxPos.x, BoxPos.y, P(LP.W), h)
    RoundWin(Bgui.Hwnd, P(LP.W), h)
    LogBoxSize()
}

; 记一次框尺寸（lp）。§6.3 的「宽 400 lp、折叠 ≤170 lp」是硬指标，
; 写进日志才可复核（只记数字，不记用户内容）。
LogBoxSize() {
    global Bgui
    if (!Bgui)
        return
    Bgui.GetPos(, , &bw, &bh)
    Log("速记框 " . Round(bw / D) . "×" . Round(bh / D) . " lp"
      . "（元数据" . (G_Expanded ? "展开" : "收起") . "、原句 " . G_CtxRows . " 行）")
}

; `F2` / 点「更多」：元数据展开收起
ToggleMore() {
    global Bgui, C, G_Expanded
    if (!Bgui)
        return
    cur := FocusedField()
    G_Expanded := !G_Expanded
    ; 收起时焦点若在元数据字段上，拉回原句（否则焦点落在一个隐藏控件上）
    if (!G_Expanded && (cur = "prov" || cur = "pos" || cur = "tags")) {
        C.context.Focus()
        CaretToEnd(C.context)
    }
    Relayout()
    Log("元数据" . (G_Expanded ? "展开" : "收起"))
}

; `F3`：原句 1 行 ↔ 3 行（§6.3 线框图的 `⌄ 原句`）。
; 行数是创建期选项（r1/r3），改它必须重建控件 —— Rebuild 会保住位置、值与焦点。
ToggleCtxRows() {
    global G_CtxRows
    if (!Bgui)
        return
    G_CtxRows := (G_CtxRows = 1) ? 3 : 1
    Rebuild()
    Log("原句改为 " . G_CtxRows . " 行")
}

; 默认焦点规则（v4 §2）：速记框显示时**一律**落目标词框——无论它是否已被 OCR 猜词
; 预填。旧的「目标词非空 → 释义」规则（v1/v2 §6.3）在本轮被废除。
; v5 §4.1：若框里那个值还是 OCR 的**自动猜词**（`G_TargetAuto` 非空且相等），整段全选
; ——用户直接打第一个字母就把猜词替换掉（不这么做的话光标落末尾，打字变追加，
; 前缀永远构不成，Tab 补全永远不出现）。用户自己的文字（改过 / 重新聚焦）仍落末尾。
FocusDefault() {
    global C, V, G_TargetAuto, Bgui
    if (!Bgui)
        return                                    ; 框不在 → 没有焦点可落（F1 类守卫）
    C.target.Focus()
    if (G_TargetAuto != "" && NormalizeLine(V.target) = G_TargetAuto)
        SelectAllText(C.target)
    else
        CaretToEnd(C.target)
}

; 焦点在环里的哪个字段（不在环里 → ""）
FocusedField() {
    global C, G_Expanded, Bgui
    if (!Bgui)
        return ""
    for f in FieldRing(G_Expanded) {
        if (C.HasOwnProp(f) && C.%f%.Focused)
            return f
    }
    return ""
}

; `Tab` / `↓` = dir>0，`Shift+Tab` / `↑` = dir<0（§6.3：四个方向都走环）
RingMove(dir) {
    global C, G_Expanded
    cur := FocusedField()
    nxt := (dir < 0) ? PrevField(cur, G_Expanded) : NextField(cur, G_Expanded)
    if (!C.HasOwnProp(nxt))
        return
    C.%nxt%.Focus()
    if (nxt != "kind" && nxt != "session")
        CaretToEnd(C.%nxt%)
}

; 框内键的补全 dispatcher（v4 §3.2）。三条规则：
;   `Tab`  —— 目标词框聚焦**且有候选** → 接受当前候选；否则保持字段环（回归红线）；
;   `↑/↓`  —— 目标词框聚焦且有候选 → 只换候选（提示里的 (i/n) 跟着变）；否则走环；
;   `Shift+Tab` 永远走环（在 `BoxKeys` 里直接绑 `RingMove(-1)`，不进这里）。
; 「有候选」一律现算（`Completions(V.context, V.target)`），不缓存 —— 原句/目标词
; 一变候选就跟着变。
CompTab() {
    global C, V, G_Common, G_CompIdx, G_OcrSuspects
    if (FocusedField() != "target")
        return RingMove(1)
    SyncFromCtl()
    cands := Completions(V.context, V.target, KnownWords(), G_Common)
    ; v17 W5（票 #142）：疑似粘连 token 不作候选（旧内核无标记 → 原样返回）。
    cands := DropSuspectCands(cands, G_OcrSuspects).words
    if (cands.Length = 0) {
        ; 半截词没有候选 → 顺句接词（v4 §3.5）：框里已经有词时，Tab 接上原句里紧挨着的下一个词
        txt := ChainText(V.context, V.target)
        if (txt = "")
            return RingMove(1)                 ; 接不上 → 照旧走环（v4 §3.2 回归红线）
        AcceptTarget(txt)
        Log("顺句接词：接上原句的下一个词（框 " . StrLen(txt) . " 字）")
        return
    }
    w := cands[Mod(G_CompIdx, cands.Length) + 1]
    ; v17 W3（票 #139 / 决策 D4）：末尾有空白 = 顺句状态 → 先接词再回落补全（v4 §3.5 表序）。
    ; 下一动作判定与 ApplyHintState 共用 TabNextAction —— 提示说「接词」就真的接词。
    act := TabNextAction(V.context, V.target, cands, G_CompIdx)
    if (act.action = "chain") {
        AcceptTarget(act.word)
        Log("顺句接词：尾随空白优先，接上原句的下一个词（框 " . StrLen(act.word) . " 字）")
        return
    }
    ; 补完一个词就留一个尾随空格：它既是「该接下一个词了」的状态标记（Logic 的 ChainStep 认它），
    ; 也让用户接着手打下一个词时不用自己补空格。删掉这个空格 = 退出顺句，Tab 回到走环老语义。
    AcceptTarget(w . " ")
    Log("句内补全：接受候选（" . StrLen(w) . " 字）")
}

; v17 W4（票 #141 / 决策 D2）：`Ctrl+Tab` 接词 —— 专用键，不参与字段环。
;   目标词框聚焦时把框里已有的词串当锚点（允许单 token、不要求尾随空格），接上原句里
;   紧挨着的下一个词；写入与顺句一致（末尾留一个空格、光标到末尾、走 AcceptTarget
;   统一收口 → 去抖查词在 OnTargetChange 内）。
;   定位失败 / 焦点不在目标词框：**静默 no-op** —— 不改值、不移动焦点、不报错
;   （跳字段仍用 `Tab`；不抢字段环语义）。
CompCtrlTab() {
    global V
    if (FocusedField() != "target")
        return                                  ; 焦点不在目标词框 → no-op（不抢字段环）
    SyncFromCtl()
    txt := ChainNextText(V.context, V.target)
    if (txt = "")
        return                                  ; 定位失败 → 静默 no-op
    AcceptTarget(txt)
    Log("Ctrl+Tab 接词：接上原句的下一个词（框 " . StrLen(txt) . " 字）")
}

; 程序写目标词框的统一收口：写值 → 光标到末尾 → 手动推一次。
; 为什么手动推：值是本程序写进去的，Edit 的 Change 不会触发（类型推断 / 去抖查询同效）。
AcceptTarget(text) {
    global C
    C.target.Value := text
    CaretToEnd(C.target)
    OnTargetChange()
}

CompMove(delta) {
    global V, G_Common, G_CompIdx, G_OcrSuspects
    if (FocusedField() != "target")
        return RingMove(delta)
    SyncFromCtl()
    cands := Completions(V.context, V.target, KnownWords(), G_Common)
    cands := DropSuspectCands(cands, G_OcrSuspects).words   ; v17 W5：疑似 token 不作候选
    if (cands.Length = 0)
        return RingMove(delta)
    G_CompIdx := Mod(G_CompIdx + delta + cands.Length, cands.Length)
    ApplyHintState()                           ; 只换选中项与提示，不改写输入框
}

Rebuild() {
    global Bgui, BoxPos, V
    cur := FocusedField()
    if (Bgui) {
        Bgui.GetPos(&bx, &by)                  ; 保住位置，别跳回屏幕中央
        BoxPos.x := bx, BoxPos.y := by
    }
    SyncFromCtl()
    BuildBox()
    ApplyVals(V)                               ; 新控件是空的，必须把值写回去
    PresentBox(false)
    if (cur != "" && C.HasOwnProp(cur)) {      ; 焦点也不跳
        C.%cur%.Focus()
        CaretToEnd(C.%cur%)
    }
}

; 定位：鼠标**所在显示器**内、鼠标右下 16 lp，越界翻转，最后夹进那块屏（研究
; 文档 §2.1）。票 #184（A6）前只认主屏 —— 副屏鼠标会被夹回主屏。算术在
; Logic.PlaceRectNearMouse（纯函数，harness 片 38 钉）；显示器/虚拟屏每次现取，
; 拔插显示器不必重启外壳。
PlaceBox(w, h) {
    global LP
    MouseGetPos(&mx, &my)
    mon := MonitorRectOf(Screens(), mx, my, VirtualScreenRect())
    return PlaceRectNearMouse(mx, my, w, h, mon, P(LP.EDGE), P(LP.OFFSET))
}

; 各显示器的矩形（物理像素的虚拟屏幕坐标；左右副屏就是负坐标）。位置记忆的
; 可见性校正要它，所以每次现取 —— 拔插显示器 / 改分辨率后不必重启外壳。
Screens() {
    out := []
    Loop MonitorGetCount() {
        MonitorGet(A_Index, &l, &t, &r, &b)
        out.Push({ x: l, y: t, w: r - l, h: b - t })
    }
    return out
}

; 虚拟屏幕矩形（物理像素；SM_XVIRTUALSCREEN=76 / SM_YVIRTUALSCREEN=77 /
; SM_CXVIRTUALSCREEN=78 / SM_CYVIRTUALSCREEN=79）。左/上副屏布局下 x/y 是**负**
; 坐标 —— 票 #184（A6）起遮罩/toast/落点一律以它为界，不再用只反映主屏的
; A_ScreenWidth/Height。内核抓屏早已用同一组指标（kernel.py virtual_screen_bounds），
; 两边口径一致。
VirtualScreenRect() {
    return { x: DllCall("GetSystemMetrics", "Int", 76, "Int")
           , y: DllCall("GetSystemMetrics", "Int", 77, "Int")
           , w: DllCall("GetSystemMetrics", "Int", 78, "Int")
           , h: DllCall("GetSystemMetrics", "Int", 79, "Int") }
}

; 呼出的落点（§7）：`box_pos=custom` 就按记住的位置放，但先做可见性校正 ——
; 夹进当前虚拟屏幕、落在已拔掉的显示器上回落 `auto`（判定在 Logic.BoxPosResolve）。
; 译文单位：config 里存的是逻辑像素（96dpi），Show 要的是物理像素，故 ×D。
PickBoxPos(w, h) {
    global G_Cfg, LP
    r := BoxPosResolve(G_Cfg.boxPos, G_Cfg.boxX, G_Cfg.boxY, Screens(), LP.W, Round(h / D))
    if (r.mode = "custom")
        return { x: P(r.x), y: P(r.y) }
    return PlaceBox(w, h)
}

; 拖动结束（§7）：把当前位置记下来并**立即生效** —— 写 `box_pos=custom` +
; `box_x`/`box_y`（逻辑像素：config 里存逻辑像素，换缩放/换机器都还在同一处），
; 同时刷 G_Cfg.box*，免得同一次会话里下一次呼出又走 auto。
SaveBoxPos() {
    global Bgui, BoxPos, G_Cfg
    if (!Bgui)
        return
    Bgui.GetPos(&bx, &by)                        ; 物理像素（Show/Move 的同一口径）
    BoxPos.x := bx, BoxPos.y := by
    lx := Round(bx / D), ly := Round(by / D)
    SaveConfigKeys(Map("box_pos", "custom", "box_x", "" . lx, "box_y", "" . ly), "box_pos=custom")
    G_Cfg.boxPos := "custom", G_Cfg.boxX := "" . lx, G_Cfg.boxY := "" . ly
    Log("速记框位置已记住（box_pos=custom）")
}

; 显示 / 重新显示。尺寸显式给（不靠 AutoSize：内面板是我们自己 Move 的，
; AutoSize 会按内容算歪），再来一次圆角区域（区域只能跟着尺寸走）。
PresentBox(place) {
    global Bgui, BoxPos, LP, D
    Bgui.Show("Hide")                          ; 先隐藏着摆位，避免定位时闪一下
    h := LayoutBox()
    w := P(LP.W)
    pos := place ? PickBoxPos(w, h) : { x: BoxPos.x, y: BoxPos.y }
    ; 票 #184（A6）：框落在哪块屏就吃哪块屏的 DPI —— 长文面板 GetDpiForWindow 的
    ; 先例（A_ScreenDPI 只反映主屏，副屏上尺寸整体错）。先按旧标度**隐藏**摆到位，
    ; 读窗口自己的 DPI：换屏了就按新标度重建控件再来一遍（BuildBox 末尾自己
    ; ApplyVals + RenderChips；第二遍 D 已与新屏一致，不会再进这支）。同屏呼出
    ; （绝大多数）就一次 DllCall，零额外开销。
    Bgui.Show("x" . pos.x . " y" . pos.y . " w" . w . " h" . h . " Hide")
    dpi := DllCall("GetDpiForWindow", "Ptr", Bgui.Hwnd, "UInt")
    if (dpi > 0 && dpi != Round(D * 96)) {
        D := dpi / 96
        BuildBox()
        return PresentBox(place)
    }
    Bgui.Show("x" . pos.x . " y" . pos.y . " w" . w . " h" . h)   ; 无 NoActivate → 抢焦点
    RoundWin(Bgui.Hwnd, w, h)
    BoxPos.x := pos.x, BoxPos.y := pos.y
    BoxKeys(true)
    FocusDefault()
    LogBoxSize()
}

ShowBox() => PresentBox(true)

HideBox() {
    global Bgui
    BoxKeys(false)
    KeymapClose()
    if (Bgui)
        Bgui.Hide()
}

DestroyBox() {
    global Bgui, C, Hint, G_GripHwnd, G_CancelOnUp
    ; 取消键的「等抬起」状态与兜底定时器随框作废（框都关了，抬起无事可做）
    G_CancelOnUp := false
    SetTimer(BoxCancelFallback, 0)
    BoxKeys(false)
    KeymapClose()
    if (Bgui) {
        deadBox := Bgui                   ; 热修：先摘引用再销毁（同 BuildBox，见那条注释）
        Bgui := 0
        try deadBox.Destroy()
    }
    ; F1 修法（结构性）：控件对象随框一起作废 —— 清空 C 并复位把手 HWND，
    ; 掐掉「销毁后残留控件对象」整类崩溃（凡是用 `Bgui` 守卫 + `C.HasOwnProp`
    ; 判形状的读取点，从此在框销毁后一律走安全分支）。`C := {}` 与 BuildBox 同款。
    C := {}
    G_GripHwnd := 0
    ResetHint()
    ; v5 §3：**框销毁即续播** —— 这条是统一的收工漏斗（Esc / 取消 / 收工关框 /
    ; Ctrl+Enter / OCR 失败后的 AbortCapture 全走它）。只在「我们暂停过」时真发。
    ; 「保存并继续」（Enter）不走这里（框不关）→ 视频保持暂停，见 ResetForNextWord。
    MediaResumeAfterCapture()
    RestorePrevFocus()
}

; ---------------------------------------------------------------------------
;  焦点还原（票 #47 B1）：框关闭后把焦点交回**呼出前的那个窗口**。
;
;  为什么需要它：全局呼出热键是 Alt 系（用户的 `!1`/`!a`），AHK 把热键里的非修饰键
;  吞掉，浏览器因此看到一次「裸 Alt」（Alt down → Alt up 之间没有别的键）——Chromium
;  对裸 Alt 的行为是**聚焦应用菜单按钮（「...」）**。框关掉以后焦点落回浏览器时，
;  还停在那枚按钮上，空格就成了「激活按钮」而不是播放视频（用户 2026-10-14 的截图）。
;
;  这里只做 WinActivate 回到那只窗口（本票主修）；不向别的进程发任何按键（项目纪律
;  「AHK 不做键盘模拟」）。prevHwnd 已经消失（窗口关了）→ 什么都不做，只记日志。
; ---------------------------------------------------------------------------
RestorePrevFocus() {
    global G_PrevHwnd
    if (!G_PrevHwnd)
        return
    hw := G_PrevHwnd
    G_PrevHwnd := 0
    if (!WinExist("ahk_id " hw)) {
        Log("焦点还原：呼出前的窗口已不在，跳过")
        return
    }
    try {
        WinActivate("ahk_id " hw)
        Log("焦点还原：已把焦点交回呼出前的窗口")
    } catch as err {
        Log("焦点还原失败：" . err.Message)
    }
}

; 框内键环（§6.3 的键位表 + §9.1 的 config 键名）。
; 一律绑在速记框自己身上（HotIfWinActive）：框不在前台时这些键立刻让位给别的程序
; —— v1 的 `^Enter` 是全局注册的，切走窗口以后照样抢。
; 键位串取自 config，非法/重复的（G_BoxBlocked，来自 RegisterGlobalHotkeys）不注册
; 并记日志（§9.2；不是静默回落）。config 变了由 ReloadConfig 摘掉重建。
BoxKeys(on) {
    global G_BoxWin, G_Cfg, Bgui, G_BoxCancelUpReady, G_CancelOnUp
    static reg := []                           ; 已注册的键位串（关掉时按同一份名单摘）
    if (on) {
        if (!Bgui || reg.Length > 0)
            return
        G_BoxWin := "ahk_id " . Bgui.Hwnd
        HotIfWinActive(G_BoxWin)
        ; 环：Tab / Shift+Tab / ↑ / ↓（§6.3：全是单行字段，不与光标键冲突）。
        ; v4 §3.2：Tab/↑/↓ 先问补全（目标词框聚焦且有候选时服务候选），否则原样走环；
        ; Shift+Tab 永远走环 —— 留一条不参与补全的退路。
        for spec in [["Tab", (*) => CompTab()], ["+Tab", (*) => RingMove(-1)]
                   , ["Up", (*) => CompMove(-1)], ["Down", (*) => CompMove(1)]] {
            try {
                Hotkey(spec[1], spec[2], "On")
                reg.Push(spec[1])
            } catch as err {
                Log("框内环键注册失败 " . spec[1] . "：" . err.Message)
            }
        }
        ; v17 W4（票 #141）：`Ctrl+Tab` 接词（目标词框聚焦时接原句下一词；
        ; 定位失败/焦点不在目标词框 = 静默 no-op，不走字段环）
        try {
            Hotkey("^Tab", (*) => CompCtrlTab(), "On")
            reg.Push("^Tab")
        } catch as err {
            Log("框内键 ^Tab 注册失败：" . err.Message)
        }
        ; 动作键：保存 / 取消 / 截图 / 更多 / 原句行数 / 键位表
        kSave := Trim(G_Cfg.keySave)
        kCancel := Trim(G_Cfg.keyCancel)
        kShot := Trim(G_Cfg.keyShot)
        kMore := Trim(G_Cfg.keyMore)
        kCtx := Trim(G_Cfg.keyContext)
        kHelp := Trim(G_Cfg.keyHelp)
        kLookup := Trim(G_Cfg.keyLookup)
        reg.Push(RegBoxKey(kSave, "^Enter", (*) => DoSave("close")))   ; v4 §4.1：保存并关框
        cancelSpec := RegBoxKey(kCancel, "Escape", (*) => BoxEsc())
        reg.Push(cancelSpec)
        ; 同一组合的「抬起」也一起挂上：收工改在抬起时做（G_CancelOnUp 的注释）。
        ; 挂不上（罕见：非法单标点键 / AHK 拒绝）→ 退回「按下即收工」的老行为。
        G_BoxCancelUpReady := (cancelSpec != "")
            && RegBoxKeyUp(cancelSpec, (*) => BoxEscUp()) != ""
        ; 框内截图 = v2 §6.2 的入口一（票 #25）：与「截图 ⌃S」按钮同一条流水线
        reg.Push(RegBoxKey(kShot, "^s", (*) => ShotFromBox()))
        reg.Push(RegBoxKey(kMore, "F2", (*) => ToggleMore()))
        reg.Push(RegBoxKey(kCtx, "F3", (*) => ToggleCtxRows()))
        reg.Push(RegBoxKey(kHelp, "F1", (*) => ToggleKeymap()))
        ; `key_lookup`（`^l` 重新查词典）：票 #27 起真的注册（不等去抖立即重查，§7）
        reg.Push(RegBoxKey(kLookup, "^l", (*) => Relookup()))
        ; 回车（v4 §4.1）：`Enter` = 保存并继续（改掉了 v2 的「保存后关框」）；
        ; 自己拦下 Enter 还有个好处：原句展开成 3 行（多行编辑框）时，回车不会变成换行
        ; ——原句落盘前一律单行化，换行没有意义。
        reg.Push(RegBoxKey("Enter", "Enter", (*) => DoSave("next")))
        try {
            Hotkey("^NumpadEnter", (*) => DoSave("close"), "On")           ; v4 §4.1：保存并关框
            reg.Push("^NumpadEnter")
        } catch as err {
            Log("框内键 ^NumpadEnter 注册失败：" . err.Message)
        }
        HotIf()
        Log("框内键已就绪 " . reg.Length . " 个（保存=" . kSave . " 取消=" . kCancel
          . " 截图=" . kShot . " 更多=" . kMore . " 原句=" . kCtx . " 帮助=" . kHelp
          . " 重查=" . kLookup . "）")
    } else if (reg.Length > 0) {
        HotIfWinActive(G_BoxWin)
        for spec in reg {
            try Hotkey(spec, "Off")
        }
        HotIf()
        reg := []
        ; 框没了 → 取消键的「等抬起」状态与兜底定时器一并作废（框都关了，抬起无事可做）
        G_BoxCancelUpReady := false
        G_CancelOnUp := false
        SetTimer(BoxCancelFallback, 0)
    }
}

; 注册一个框内键。空 spec（用户显式清空）→ 退化为默认键并记日志；非空的非法/重复
; 键位由 G_BoxBlocked 拦下（§9.2 的 bad/dup 都不注册），**不再静默回落默认键**
; （v1 §4.1：静默回落就是静默失效）。
RegBoxKey(spec, defSpec, fn) {
    global G_BoxBlocked
    if (spec = "") {
        try {
            Hotkey(defSpec, fn, "On")
            Log("框内键为空，退化为默认 " . defSpec)
            return defSpec
        } catch as err {
            Log("框内键（默认 " . defSpec . "）注册失败：" . err.Message)
            return ""
        }
    }
    if (G_BoxBlocked.Has(spec)) {
        Log("框内键「" . spec . "」不注册：" . G_BoxBlocked[spec])
        return ""
    }
    try {
        Hotkey(spec, fn, "On")
        return spec
    } catch as err {
        Log("框内键「" . spec . "」挂不上，不注册：" . err.Message)
        return ""
    }
}

; 框内 `Esc`（按下）：键位表开着 → 先关表（§6.3「Esc 关」）；否则**只置位**，抬起才收工。
;
; 2026-10-23 真机 bug（「Esc 顺手把视频全屏退掉了」）：框内取消键是
; `HotIfWinActive(框)` 热键，keydown 被它吞掉；但老实现里 `DoCancel()` 在 keydown 当场
; 就 `DestroyBox()` + `RestorePrevFocus()`，等用户松手时前台已经回到浏览器 —— 那次
; **keyup** 就落在浏览器窗口上，而浏览器全屏播放时 Esc 抬起会退出全屏。
; 现在：keydown 只置 `G_CancelOnUp`，真正的 `DoCancel()` 放在同一组合的 `<key> Up`
; 热键里（抬起事件被本脚本消费，走不到浏览器）。兜底定时器防「抬起没等到」。
BoxEsc() {
    global G_KeymapGui, G_CancelOnUp, G_BoxCancelUpReady
    if (G_KeymapGui) {
        KeymapClose()
        return
    }
    if (!G_BoxCancelUpReady) {
        DoCancel()                     ; 抬起热键没挂上（极罕见）→ 退回「按下即收工」
        return
    }
    if (!G_CancelOnUp) {
        G_CancelOnUp := true
        SetTimer(BoxCancelFallback, -1500)   ; 兜底：抬起始终没来（焦点被抢）也收工
    }
}

; 取消键抬起：这才是真收工的时刻（抬起已被本热键消费，浏览器看不到）。
BoxEscUp() {
    global G_CancelOnUp
    if (!G_CancelOnUp)
        return
    G_CancelOnUp := false
    SetTimer(BoxCancelFallback, 0)
    DoCancel()
}

; 兜底：按下了取消键却始终没等到抬起（焦点被抢 / 抬起被别的钩子吞掉）。
BoxCancelFallback() {
    global G_CancelOnUp
    if (!G_CancelOnUp)
        return
    G_CancelOnUp := false
    DoCancel()
}

; 注册「取消键抬起」热键（2026-10-23）。
; 与 #47 否决的「事后补挂 up 热键吞尾巴」不同：它在**框存活期间**与 down 键一同注册，
; 抬起是取消动作的正常触发点，不靠事后补救。键位串用 Logic.KeySpecOf 规范化
; （否则 `Esc Up` / `Escape Up` 会被 AHK 当成两个不同的热键）。
RegBoxKeyUp(spec, fn) {
    r := ParseHotkeyV2(spec)
    if (!r.ok)
        return ""
    upSpec := KeySpecOf(r) . " Up"
    try {
        Hotkey(upSpec, fn, "On")
        return upSpec
    } catch as err {
        Log("框内键「" . upSpec . "」挂不上，不注册：" . err.Message)
        return ""
    }
}

; `F1` 键位表（§6.3：置顶小窗列出全局键与框内键，Esc 关）。
; NoActivate：不抢焦点 —— 框里继续打字不受影响，Esc 也仍归速记框处理。
; 文案由 Logic.KeymapText 按 config 的键位串生成（C5 改了键位它自己跟着变）。
ToggleKeymap() {
    global G_KeymapGui, Bgui, BoxPos, G_Cfg, LP
    if (G_KeymapGui) {
        KeymapClose()
        return
    }
    if (!Bgui)
        return
    th := Theme()
    kg := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000", "快捷键")
    kg.BackColor := th.border
    panel := kg.AddText("x" . P(LP.BORDER) . " y" . P(LP.BORDER) . " w1 h1 Background" . th.win, "")
    kg.SetFont("s9 c" . th.text, "Segoe UI")
    txt := kg.AddText("x" . (P(LP.BORDER) + P(LP.KEYMAP_PAD)) . " y" . (P(LP.BORDER) + P(LP.KEYMAP_PAD))
                    . " w" . P(LP.KEYMAP_W), KeymapText(G_Cfg))
    kg.Show("Hide")
    tw := P(LP.KEYMAP_W) + 2 * P(LP.BORDER) + 2 * P(LP.KEYMAP_PAD)
    thh := hOf(txt) + 2 * P(LP.BORDER) + 2 * P(LP.KEYMAP_PAD)
    panel.Move(P(LP.BORDER), P(LP.BORDER), tw - 2 * P(LP.BORDER), thh - 2 * P(LP.BORDER))
    ; 站框左边；左边放不下就放右边；再不行夹进**框所在的显示器**（票 #184（A6）：
    ; 框在副屏时键位表不再被夹回主屏；夹取算术在 Logic.ClampRectInMonitor）
    mon := MonitorRectOf(Screens(), BoxPos.x, BoxPos.y, VirtualScreenRect())
    kx := BoxPos.x - tw - P(LP.EDGE)
    if (kx < mon.x + P(LP.EDGE))
        kx := BoxPos.x + P(LP.W) + P(LP.EDGE)
    kr := ClampRectInMonitor(kx, BoxPos.y, tw, thh, mon, P(LP.EDGE))
    kg.Show("x" . kr.x . " y" . kr.y . " w" . tw . " h" . thh . " NoActivate")
    RoundWin(kg.Hwnd, tw, thh)
    G_KeymapGui := kg
    Log("键位表已打开")
}

KeymapClose() {
    global G_KeymapGui
    if (G_KeymapGui) {
        try G_KeymapGui.Destroy()
        G_KeymapGui := 0
    }
}

; ---------------------------------------------------------------------------
;  保存 / 取消 / 粘贴（v2 §2 / §4.1；一句多词见 v4 §4，票 #51）
;
;  mode（v4 §4.1）：
;    "next"  —— `Enter` / 「保存」按钮：非空 → **保存并继续**（落盘后不关框、清目标词、
;               保留原句/会话/位置/出处/标签、焦点回目标词）；空但已存过 ≥1 词 → 收工关框。
;    "close" —— `key_save`（`Ctrl+Enter`）/ `^NumpadEnter`：非空 → 保存并关框；空 → 直接收工关框。
;  两条路都共用一条落盘流水线：spool 先落盘、再通知中枢；离线不算失败（词已进 spool），
;  只是不关框（继续记下一个词照常可行）。
; ---------------------------------------------------------------------------
DoSave(mode := "next") {
    global Bgui, V, G_Demo, G_Kind, G_SessNew, G_SessApp, G_SessRawTitle, G_SpoolPath, G_SavedCount
    global G_LastAutoMeaning
    if (!Bgui)
        return
    SyncFromCtl()
    if (IsTargetBlank(V.target)) {
        ; v4 §4.1：空目标词 —— 本框已存过 ≥1 词 → 收工关框（不再红字）；否则维持现状
        if (G_SavedCount >= 1)
            return CloseBoxAfterSave()
        ; spec §8.5.3 / §11：目标词为空 → 拒绝保存，框内红字提示，不落盘
        SetErrText("目标词不能为空")
        return
    }
    if (G_Demo) {
        ; /demo 里按保存**绝不落盘**：示例值不该写进真捕获库（票 #24 的补充 AC 说
        ; 「不碰捕获库」）。只提示一声，框留着让人继续看形态。
        Log("/demo：保存被拦下（示例值不落盘）")
        ShowToast("（/demo）示例值不落盘")
        return
    }
    now := A_Now
    ; v22 修复（#172）：只有「释义仍是词典/机翻自动填入的原值」才记来源 —— 用户手改过
    ; 的释义属于用户（meaning_source 留空，与既有手打口径一致）。
    meaningSource := ""
    if (V.meaningSource = "machine" && NormalizeLine(V.meaning) = G_LastAutoMeaning)
        meaningSource := "machine"
    line := SpoolLine({ target: V.target, kind: G_Kind, context: V.context
                      , meaning: V.meaning, phonetic: V.phonetic, dictJson: V.dictJson
                      , meaningSource: meaningSource
                      , app: G_SessApp, title: G_SessRawTitle, url: G_BrowserUrl
                      , position: V.pos, tags: V.tags, newSession: G_SessNew }, now)
    ; 落盘失败（§2 / §11）：目录建不出来 / 磁盘满 / 无权限 → 框**不关闭** + 红字 +
    ; 整条内容进剪贴板（FailSave 沿用 v1 的语义）
    if ((err := SpoolAppend(G_SpoolPath, line)) != "") {
        FailSave(err, SpoolClipText(V))
        return
    }
    target := NormalizeLine(V.target)
    ; 只记「一条几字节」与会话归属方式，不记目标词/内容（§10.2）
    Log("已暂存一条（" . StrLen(line) . " 字节）到 spool，会话"
      . (G_SessNew ? "另起" : "自动归属"))
    G_SessNew := false                        ; 下一条捕获回到自动归属（§6.1）
    ; spool 落盘即成功（§2）；中枢只是「收到 flush 后立刻 ingest 一次」的加速器，
    ; 不在位时状态行/toast 按 §10 报「已暂存」，收据稍后由 hub 的回话给出。
    ; v23 S4（G2/G3）：释义为空 → 中枢会后台补齐，外壳给一句可读提示。
    ; next 走状态行；close 时框要关了，把提示并进这条保存的 flush 回执 toast
    ; （不在 DoSave 里另弹一条，否则会和回执叠在一起、被压住）。
    ; 离线时下面的 hub-offline 会顶掉状态行（离线没有后台补齐，别给假承诺）。
    meaningPending := (NormalizeLine(V.meaning) = "")
    if (meaningPending && mode = "next")
        SetStatus("meaning-pending")
    ; v4 §4.1：**离线也不关框**（next 模式继续记下一个词），离线 toast 照旧立刻发。
    if (!HubFlush(target, (meaningPending && mode = "close"))) {
        SetStatus("hub-offline")
        ShowToast(SaveToastText("offline", target), "err")
    }
    return FinishSave(mode)
}

; 落盘成功后的收尾（v4 §4.1）：`next` → 清目标词继续记（框不关）；`close` → 关框。
; toast/状态行由 hub 回话或兜底收据发（离线那条已在 DoSave 里发过），这里不复述。
FinishSave(mode) {
    Log("保存收尾：mode=" . mode)
    if (mode = "close")
        return DestroyBox()
    return ResetForNextWord()
}

; 空目标词时的「收工」（v4 §4.1：已存过 ≥1 词）：静默关框，不算失败、无 toast。
CloseBoxAfterSave() {
    global G_SavedCount
    Log("空目标词 → 收工关框（本框已存 " . G_SavedCount . " 词）")
    DestroyBox()
}

; 「保存并继续」（v4 §4.1）：清目标词/音标/释义/词典自动填入状态，保留原句/会话/位置/
; 出处/标签，焦点回目标词，类型 chip 复位为自动推断态，计数 +1，异步刷会话 chip。
ResetForNextWord() {
    global C, V, G_Kind, G_KindTouched, G_SavedCount, G_LastAutoMeaning, G_TargetAuto
    global G_SessApp, G_SessRawTitle, G_PrevHwnd, G_LookupFollowedUp, G_LookupDeb
    Log("保存并继续：清目标词（上一词已入 spool）")
    V.target := "", V.meaning := "", V.dictJson := "", V.meaningSource := ""
    ; v23 S1（B1）：去抖状态一起归零 —— 否则 Enter 后立刻重打**同一个词**时
    ; DebounceStep 判 state.word = w → 不再排程 → 查询永远不发、存空。
    G_LookupDeb := 0
    G_LastAutoMeaning := ""                    ; 词典「手打不覆盖」的基准随新词归零（§7）
    G_LookupFollowedUp := ""                   ; v18 W4（#149）：下一个词的二跳标记归零
    G_TargetAuto := ""                         ; v5 §4.1：下一个词的自动值标记也归零
    C.target.Value := ""
    C.meaning.Value := ""
    SetPhonetic("")                            ; 走它自己：归一化 + 变了才重排（音标行显隐跟着变）
    ; 上一个词的「已记过 / 已手打」提示与红字都不带到下一个词
    SetSeenText("")
    SetNoteText("")
    SetErrText("")
    G_KindTouched := false                     ; 下一个词重新推断类型（§4.1）
    G_Kind := InferKind(V.target)
    RenderChips()
    G_SavedCount += 1
    SyncSaveButton()                           ; 空目标词 + 已存过 → 「收工 ↵」
    FocusDefault()                             ; 焦点回目标词（§4.1）
    ; 会话 chip 异步刷新（§4.2：`×N` 跟手；hub 不在位时退化为离线口径）。
    ; v5 §2.4：同一个会话还是那只窗口 → 带上呼出时抓到的 hwnd（本机探针才读得到地址栏）。
    HubPeek(G_SessApp, G_SessRawTitle, G_PrevHwnd)
    ApplyHintState()                           ; 补全让位（空前缀无候选）→ 显示「已存 N 词」
}

; 保存失败的兜底（spec §11）：速记框不关、红字说明、**整条内容**（人可读形态）
; 自动进剪贴板，用户可自行粘到别处。只记失败原因，不记用户内容（§10.2）。
FailSave(err, clipText) {
    Log("保存失败：" . err)
    try {
        A_Clipboard := clipText
        SetErrText(err . "（整条内容已复制到剪贴板）")
    } catch as clipErr {
        Log("写剪贴板也失败：" . clipErr.Message)
        SetErrText(err . "（剪贴板写入失败，请手动复制框内内容）")
    }
}

DoCancel() {
    global Bgui, G_Demo
    if (!Bgui)
        return
    Log("取消本次捕获")
    DestroyBox()
    if (G_Demo)
        ExitApp()                     ; /demo 是「看一眼就收」的实例：Esc 即退出
}

DoPastePos() {
    global C
    txt := ""
    try txt := A_Clipboard
    C.pos.Value := PositionFromClipboard(txt)
}

; ---------------------------------------------------------------------------
;  全屏半透明遮罩 + 拖框（spec §4.2 定稿视觉）—— 两条入口共用的一条流水线（v2 §6.2）
;
;  ctx = { hadBox }：**开始之前有没有速记框**（v2 §6.2 的字面名）。它决定三件事：
;    · 开始前要不要 SyncFromCtl / HideBox（没有框就没什么可藏）；
;    · 取消时是「按原样重现、内容不丢」还是「什么都不留」（无框入口不许留空框）；
;    · OCR 回来时是「回填现成的框」还是「才建框」（ShowBoxAtCursor）。
;  入口一：框内 `key_shot`（`Ctrl+S`）与按钮「截图 ⌃S」→ hadBox=true；
;  入口二：全局 `hotkey_capture` → 先采集出处，再以 hadBox=false 进来。
;
;  三条不变量：
;    · 遮罩必须半透明（WinSetTransparent 90）—— 不透明会让 Chrome 判定窗口
;      被完全遮挡并立刻暂停视频（#5 八组实测）。
;    · Show("NoActivate") —— 不抢焦点。
;    · 截图前先销毁遮罩 —— 否则这层暗色会污染裁剪图。
; ---------------------------------------------------------------------------
RunShot(ctx) {
    ; DragHwnds 也必须在声明里：CursorHandler（拦 WM_SETCURSOR）读的是全局那份，
    ; 而这里是「赋值」—— 不声明 global 就只是往局部列表里塞，十字光标因此永远不出现
    global Bgui, V, G_Ready, G_Sock, G_Pending, G_PendingAt, G_ShotActive, G_ShotCancelled
    global MASK_ALPHA, MIN_SELECT_LP, SHOT_IDLE_MS, LP, DragHwnds, G_SessTitle, G_ShotBuildBox
    ; 端点坐标也必须在声明里 —— 这里是「赋值」，不声明 global 就只是往
    ; 局部变量里写，MaskLButtonDown 读的全局那份永远是空串（同 DragHwnds 的坑）
    global G_MaskOv, G_MaskOx, G_MaskOy, G_MaskDownX, G_MaskDownY, G_MaskUpX, G_MaskUpY
    global G_LongBusy, G_LongPending

    if (G_ShotActive)
        return
    ; 边界②：长文链路在途时旧框入口直接拒绝（明确 toast，不串词/不抢内核）。
    ; 票 #176（A3）：G_LongBusy 现在从长文遮罩拖框**进入前**就置位 —— 拖框窗口内
    ; 这里同样拦下（判据收口在 Logic.LongformInFlight，三入口共用，harness 片 36 钉）。
    if (LongformInFlight(G_LongBusy, G_LongPending)) {
        Log("长文在途：旧框截图入口已拒绝（避免两条链路抢内核）")
        ShowToast("长文识别进行中，请稍后再取词", "err")
        return
    }
    if (G_Pending) {
        Log("已有 OCR 请求在途，本次忽略")
        return
    }
    if (!G_Ready || !G_Sock) {
        Log("内核未就绪，拒绝截图")
        ShowToast("OCR 内核未就绪，稍后再试", "err")
        ; v5 §3：「失败且没有框」也是流程终止 —— 无框入口此刻屏幕上一个框都没有，
        ; 该把压着的暂停还回去（有框入口的框只是藏起来了，不算终止，继续压着）。
        if (!ctx.hadBox)
            MediaResumeAfterCapture()
        return
    }

    ; 有框入口：先把框里的当前值收进 V（取消 / 回填都靠它），再把框藏起来
    ; （`&& Bgui`：`ctx.hadBox` 是进入时拍的快照，显式守一下更稳 —— F1 同类）
    if (ctx.hadBox && Bgui) {
        SyncFromCtl()
        HideBox()
    }
    G_ShotActive := true
    G_ShotCancelled := false
    G_ShotBuildBox := !ctx.hadBox        ; 无框入口：OCR 回来时才建框（§6.2）
    ; 只记「有没有框」，不记窗口标题 / 内容（§10.2）
    Log("截图开始（" . (ctx.hadBox ? "有框入口：速记框已隐藏" : "无框入口：开始前没有框") . "）")

    ov := 0, hintG := 0, roG := 0, sessG := 0, bars := []
    rect := ""
    tooSmall := false
    try {
        ; 遮罩：纯黑 + 整窗 alpha 90（≈35% 不透明，绝不 255）+ NoActivate
        ; 票 #184（A6）：罩住**整个虚拟屏幕**（含负坐标副屏），不再只罩主屏 ——
        ; 此前副屏上点击根本落不进遮罩。遮罩原点经 GetPos 进 G_MaskOx/Oy，
        ; MaskLButtonDown 的客户区→屏幕换算自动跟上，无需特判。
        vr := VirtualScreenRect()
        ov := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000")
        ov.BackColor := LP.BLACK
        ov.Show("x" . vr.x . " y" . vr.y . " w" . vr.w . " h" . vr.h . " NoActivate")
        WinSetTransparent(LP.DIM, ov)
        DragHwnds := [ov.Hwnd]
        ; 端点坐标的唯一来源（§4.2）：记下遮罩主窗 + 它的屏幕原点，清掉上一轮的残留。
        ; 装饰窗稍后全部 +E0x20 鼠标穿透，所以左键消息只会落到这个窗口上。
        G_MaskOv := ov.Hwnd
        ov.GetPos(&G_MaskOx, &G_MaskOy)
        G_MaskDownX := "", G_MaskDownY := "", G_MaskUpX := "", G_MaskUpY := ""

        ; 提示文字：独立小窗（画在遮罩上会被 35% 透明一起吃成灰字，读不清）
        ; `+E0x20`（WS_EX_TRANSPARENT）= 鼠标穿透：装饰窗一颗都不许抢左键消息
        hintG := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
        hintG.BackColor := LP.BLACK
        hintG.MarginX := P(14), hintG.MarginY := P(8)
        hintG.SetFont("s12 cFFFFFF", "Segoe UI")
        hintG.AddText(, "拖框选一行英文 · Esc 取消 · 松手即取词")     ; §6.4 的字面量
        hintG.Show("AutoSize NoActivate Hide")
        WinSetTransparent(LP.TOAST_A, hintG)
        hintG.GetPos(, , &hw)
        hintG.Move(vr.x + Round((vr.w - hw) / 2), vr.y + P(LP.HINT_TOP))
        hintG.Show("NoActivate")
        DragHwnds.Push(hintG.Hwnd)

        ; 右上角小字：当前会话名（§6.4，帮用户确认「还在同一条视频里」）。
        ; 取值是 BeginCapture 里的**本地占位**（原始窗口标题截断）——peek 的回话是
        ; 异步的，遮罩这会儿可能还没拿到 hub 清洗后的标题；它只影响这 30s 里的一行小字，
        ; 速记框上的 chip 一定用真标题（OnPeekLine）。没有标题 → 整块不出现、不占位。
        sessLabel := SessionMaskLabel(G_SessTitle)
        if (sessLabel != "") {
            sessG := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
            sessG.BackColor := LP.BLACK
            sessG.MarginX := P(10), sessG.MarginY := P(4)
            sessG.SetFont("s9 cFFFFFF", "Segoe UI")
            sessG.AddText(, sessLabel)
            sessG.Show("AutoSize NoActivate Hide")
            WinSetTransparent(LP.TOAST_A, sessG)
            sessG.GetPos(, , &sw)
            sessG.Move(Max(vr.x + P(LP.EDGE), vr.x + vr.w - sw - P(LP.EDGE)), vr.y + P(LP.HINT_TOP))
            sessG.Show("NoActivate")
            DragHwnds.Push(sessG.Hwnd)
        }
        ; 只记「有没有显示」，不记会话名本身（§10.2）
        Log("遮罩会话名" . (sessLabel = "" ? "未显示（没有会话）" : "已显示"))

        ; 选区描边：外圈 2lp 黑 + 内圈 3lp 蓝（双层，任何底色上都看得见）
        bars := []
        Loop 2 {
            col := (A_Index = 1) ? LP.BLACK : LP.ACCENT
            Loop 4 {
                b := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
                b.BackColor := col
                b.Show("x0 y0 w0 h0 NoActivate")
                bars.Push(b)
                DragHwnds.Push(b.Hwnd)
            }
        }

        ; 尺寸读数：选区左上外侧，W × H 用 lp（定稿要求；物理像素是探针的做法）
        roG := Gui("-DPIScale -Caption +AlwaysOnTop +ToolWindow +E0x08000000 +E0x20")
        roG.BackColor := LP.BLACK
        roG.MarginX := P(8), roG.MarginY := P(3)
        roG.SetFont("s10 cFFFFFF", "Segoe UI")
        roCtl := roG.AddText("w" . P(92), "0 × 0")
        roG.Show("AutoSize NoActivate Hide")
        WinSetTransparent(LP.TOAST_A, roG)
        roG.GetPos(, , , &roH)
        roG.Show("NoActivate")
        DragHwnds.Push(roG.Hwnd)

        Hotkey("Esc", (*) => EscShot(), "On")
        ; 兜底：遮罩最多存在 30s 就自己收掉。全屏遮罩是把用户困住的形状，
        ; 宁可这次截图作废，也不能让它无限期挂着（误触发/走开/拖到一半都能自解）。
        SetTimer(EscShot, -SHOT_IDLE_MS)
        Log("遮罩已出现，等待拖框（Esc 取消，" . (SHOT_IDLE_MS // 1000) . "s 无操作自动取消）")

        while (!GetKeyState("LButton", "P") && !G_ShotCancelled)
            Sleep(15)

        if (!G_ShotCancelled) {
            ; 起点优先取**按下那一刻**消息里的坐标（零延迟）；消息没到（按下发生在遮罩
            ; 出现之前等）才退回轮询采样 —— 采样会晚 0~15ms，是这次真机错位的根因。
            if (G_MaskDownX != "")
                x1 := G_MaskDownX, y1 := G_MaskDownY
            else
                MouseGetPos(&x1, &y1)
            x2 := x1, y2 := y1
            lastX := -1, lastY := -1, lastW := -1, lastH := -1
            while (GetKeyState("LButton", "P") && !G_ShotCancelled) {
                MouseGetPos(&x2, &y2)
                rx := Min(x1, x2), ry := Min(y1, y2)
                rw := Abs(x2 - x1), rh := Abs(y2 - y1)
                ; 鼠标没动就不重绘：一拍要 Move 8 个描边窗 + 改读数（100 拍/秒），
                ; 按住不动时全是白干，还白让合成器忙
                if (rx != lastX || ry != lastY || rw != lastW || rh != lastH) {
                    lastX := rx, lastY := ry, lastW := rw, lastH := rh
                    DrawSel(bars, rx, ry, rw, rh)
                    roCtl.Text := Round(rw / D) . " × " . Round(rh / D)
                    roG.Move(rx, Max(vr.y + P(LP.EDGE), ry - roH - P(6)))
                }
                Sleep(10)
            }
            if (!G_ShotCancelled) {
                ; 终点同理：松开那一刻的坐标在消息里，别用上一拍采到的（甩手松开会差几像素）
                if (G_MaskUpX != "")
                    x2 := G_MaskUpX, y2 := G_MaskUpY
                r := DragRect(x1, y1, x2, y2)
                if (r.w < P(MIN_SELECT_LP) || r.h < P(MIN_SELECT_LP)) {   ; 阈值是 lp，比较的是物理像素
                    Log("选区过小 " . r.w . "×" . r.h . " px，按误操作取消")
                    tooSmall := true
                } else {
                    rect := { x: r.x, y: r.y, w: r.w, h: r.h }
                }
            }
        }
    } catch as err {
        Log("拖框流程异常：" . err.Message)
    } finally {
        ; 先销毁遮罩再做后续（半透明底色会污染裁剪图）
        SetTimer(EscShot, 0)
        Hotkey("Esc", "Off")
        DragHwnds := []
        G_MaskOv := 0                    ; 遮罩没了就不再认鼠标消息（装饰窗也一并销毁）
        for b in bars {
            try b.Destroy()
        }
        if (roG)
            try roG.Destroy()
        if (sessG)
            try sessG.Destroy()
        if (hintG)
            try hintG.Destroy()
        if (ov)
            try ov.Destroy()
        Sleep(30)                        ; 等窗口真的从屏幕上消失，再让内核去截图
        G_ShotActive := false
    }

    if (rect = "") {
        ; Esc / 误操作 / 异常
        Log("本次截图取消")
        if (tooSmall)
            ShowToast("选区过小，已取消", "err")
        if (ctx.hadBox) {
            ApplyVals(V)                 ; 有框入口：按原样重现，内容不丢（spec §4.2）
            ShowBox()
        } else {
            ; 无框入口取消：**不留一个空框**（v2 §6.2 / 票 #25；本来就没有框可重现）
            Log("无框入口取消：不建框（不留空框）")
            ; v5 §3：遮罩取消 / 选区太小 / 拖框异常 —— 无框入口的流程到此终止，
            ; 屏幕上**没有框** → 续播。有框入口上面刚把框重现（还在取词）→ 不续播。
            MediaResumeAfterCapture()
        }
        return
    }

    G_Pending := true
    G_PendingAt := A_TickCount
    try {
        G_Sock.SendText('{"cmd":"capture_ocr","rect":['
            . rect.x . "," . rect.y . "," . rect.w . "," . rect.h . ']}' . "`n")
        Log("capture_ocr 已发出 rect=" . rect.x . "," . rect.y . "," . rect.w . "," . rect.h
          . "（物理像素），等内核回话（外壳不阻塞）")
    } catch as err {
        G_Pending := false
        Log("发送失败：" . err.Message)
        AbortCapture("发送失败：" . err.Message)
    }
}

; ---------------------------------------------------------------------------
;  §6.2 的两条入口（都汇进上面那条 RunShot）：键位由 config 的
;  `key_shot`（框内）与 `hotkey_capture`（全局）给，注册在 BoxKeys / RegisterGlobalHotkeys。
; ---------------------------------------------------------------------------

; 入口一：框内截图（按钮「截图 ⌃S」/ `key_shot`）—— 有框，藏起来再截图（v1 语义）
ShotFromBox() {
    global Bgui
    RunShot({ hadBox: (Bgui ? true : false) })
}

; 入口二：全局 `hotkey_capture`（默认 `Ctrl+Alt+2`，§6.2）—— 不动框，直接遮罩拖框。
; **出处必须在任何窗口出现之前采集**（v1 §6.2 的硬约束）：遮罩是本流程第一个出现的
; 窗口，所以采集放在 RunShot 之前 —— 此刻本工具没有抢焦点的窗口，WinExist("A")
; 抓到的必然是用户正在看的那个（速记框 / 键位表都不在）。
; 已经有框时这条键的场景等同框内截图（§6.2 的 AC：不出现两个框），沿用框里
; 已经采集过的出处，**不重抓**（否则抓到的是速记框自己）。
ShotHotkey() {
    global Bgui, G_ShotActive, G_LongBusy, G_LongPending
    if (G_ShotActive) {
        Log("拖框进行中，忽略本次热键")
        return
    }
    ; 票 #176（A3）：长文遮罩拖框/识别期间这条入口一并拒绝 —— 拦在 BeginCapture
    ; 之前，不白采出处、不白 peek 一轮（判据与 RunShot 同源：Logic.LongformInFlight）。
    if (LongformInFlight(G_LongBusy, G_LongPending)) {
        Log("长文在途：直接截图入口已拒绝（避免两条链路抢内核）")
        ShowToast("长文识别进行中，请稍后再取词", "err")
        return
    }
    if (Bgui) {
        Log("直接截图取词：已有速记框 → 按框内截图处理（§6.2）")
        RunShot({ hadBox: true })
        return
    }
    prov := CaptureProvenance()               ; 遮罩（第一个窗口）之前
    BeginCapture(prov)
    ; 只记抓没抓到，不记标题本身（§10.2）
    Log("直接截图取词：出处已在任何窗口出现之前采集（自动出处"
      . (prov.auto = "" ? "没抓到" : "已抓到") . "）")
    RunShot({ hadBox: false })
}

EscShot() {
    global G_ShotCancelled
    G_ShotCancelled := true
}

DrawSel(bars, x, y, w, h) {
    if (w < 1 || h < 1)
        return
    t := P(2), u := P(3)
    bars[1].Move(x - t, y - t, w + 2 * t, t)
    bars[2].Move(x - t, y + h, w + 2 * t, t)
    bars[3].Move(x - t, y, t, h)
    bars[4].Move(x + w, y, t, h)
    bars[5].Move(x, y, w, u)
    bars[6].Move(x, y + h - u, w, u)
    bars[7].Move(x + u, y + u, u, Max(1, h - 2 * u))
    bars[8].Move(x + w - u, y + u, u, Max(1, h - 2 * u))
}

; 十字光标：必须拦 WM_SETCURSOR，否则鼠标一动就回箭头
; 验收后真机热修：框 GUI 存在「对象在、窗口已毁」的中间态（DestroyWindow 内部
; 泵消息 / Destroy 半途抛错），本监视器此刻读 `.Hwnd` 抛「Gui has no window.」
; （AHK 错误框带 Exit App，真机把外壳退掉过两次）。监视器里的 `.Hwnd` 一律走
; BoxHwndSafe：对象空 / 窗口已毁 → 0，永不抛（与上面 F1「绝不碰控件对象」同门）。
BoxHwndSafe(g) {
    if (!g)
        return 0
    try return g.Hwnd
    return 0
}
CursorHandler(wParam, lParam, msg, hwnd) {
    global DragHwnds, Bgui, G_GripHwnd
    bh := BoxHwndSafe(Bgui)
    for h in DragHwnds {
        if (h = hwnd) {
            DllCall("SetCursor", "Ptr", DllCall("LoadCursor", "Ptr", 0, "Ptr", 32515, "Ptr"))
            return 1
        }
    }
    ; v5 §5：鼠标悬在拖拽把手上 → 四向箭头（IDC_SIZEALL），把「这里能拖」说出来。
    ; 只比 **整数 HWND**（G_GripHwnd）—— 绝不碰控件对象：本处理是全局消息监视器，
    ; 框销毁后（遮罩拖框期间）照样会进来，读 `C.grip.Hwnd` 会抛「control is destroyed」
    ; （真机 F1）。把手是 Static（WM_NCHITTEST 回 HTTRANSPARENT），WM_SETCURSOR 多半
    ; 是带**速记框自己**的 hwnd 来的 —— 那条路用屏幕坐标兜底（Bgui 守）。
    if (G_GripHwnd && (hwnd = G_GripHwnd || (bh && hwnd = bh && CursorOnGrip()))) {
        DllCall("SetCursor", "Ptr", DllCall("LoadCursor", "Ptr", 0, "Ptr", 32646, "Ptr"))
        return 1
    }
}
OnMessage(0x0020, CursorHandler)

; ---------------------------------------------------------------------------
;  遮罩期间的两个端点坐标：只认遮罩主窗自己的鼠标消息（spec §4.2）
;
;  lParam 里就是按下/松开**那一刻**的客户区坐标 —— 零延迟，比「轮询发现按钮变了
;  再补一次 MouseGetPos」准得多（那次补采会晚 0~15ms，手一快就整块框选朝拖动方向
;  缩掉几十像素，见 G_MaskDownX 处的注释）。两个都只**记**不吞：不返回值（空返回），
;  消息照常走完（OnMessage 里返回整数才等于吞掉；遮罩上没有默认处理，吞消息纯属副作用）。
;
;  只认 `G_MaskOv` 一个 hwnd：装饰窗（提示 / 会话名 / 尺寸读数 / 四根描边）全部带
;  `+E0x20`（WS_EX_TRANSPARENT）**鼠标穿透**，所以命中测试落到的一律是遮罩主窗。
;  客户区→屏幕：遮罩是 -Caption 无边框窗，`G_MaskOx/Oy` 就是它 show 时的屏幕原点。
; ---------------------------------------------------------------------------
MaskLButtonDown(wParam, lParam, msg, hwnd) {
    global G_MaskOv, G_MaskOx, G_MaskOy, G_MaskDownX, G_MaskDownY
    if (!G_MaskOv || hwnd != G_MaskOv)
        return
    p := MouseLParamPoint(lParam)
    G_MaskDownX := p.x + G_MaskOx
    G_MaskDownY := p.y + G_MaskOy
}

MaskLButtonUp(wParam, lParam, msg, hwnd) {
    global G_MaskOv, G_MaskOx, G_MaskOy, G_MaskUpX, G_MaskUpY
    if (!G_MaskOv || hwnd != G_MaskOv)
        return
    p := MouseLParamPoint(lParam)
    G_MaskUpX := p.x + G_MaskOx
    G_MaskUpY := p.y + G_MaskOy
}
OnMessage(0x0201, MaskLButtonDown)
OnMessage(0x0202, MaskLButtonUp)

; v5 §5 的落点判定：光标是否在把手矩形里（物理像素）。HTTRANSPARENT 的 Static
; 让父窗口收 WM_SETCURSOR，所以只能自己拿 GetCursorPos 比 GetWindowRect。
; 用整数 HWND（G_GripHwnd）+ 取不到矩形就判否 —— 框没了不会抛错。
CursorOnGrip() {
    global G_GripHwnd
    if (!G_GripHwnd)
        return false
    pt := Buffer(8, 0)
    DllCall("GetCursorPos", "Ptr", pt)
    r := Buffer(16, 0)
    if (!DllCall("GetWindowRect", "Ptr", G_GripHwnd, "Ptr", r))
        return false
    cx := NumGet(pt, 0, "Int"), cy := NumGet(pt, 4, "Int")
    return (cx >= NumGet(r, 0, "Int") && cx < NumGet(r, 8, "Int")
         && cy >= NumGet(r, 4, "Int") && cy < NumGet(r, 12, "Int"))
}

; ---------------------------------------------------------------------------
;  v3 §7 速记框拖拽：`WM_NCHITTEST → HTCAPTION`
;
;  框没有标题栏（`+Caption` 已去）+ RoundWin 自切圆角区域，所以没有可拖的边框；
;  v1 靠标题栏拖、v2 干脆拖不动（代码注释里留着这条结论）—— 拦住 WM_NCHITTEST、
;  在**非交互**区域返回 HTCAPTION 是唯一的路子：Windows 会把这次按下当成标题栏
;  按下，自己进入拖动模态循环（松手即定，拖动期间不会触发别的交互）。
;
;  哪算「交互控件」交给 Windows 判：Edit/Button 对自己的 WM_NCHITTEST 回 HTCLIENT
;  —— 那条消息根本不会到我们这儿（子控件收点击），所以按钮照旧能按、输入框照旧
;  能打字；**父窗口**只在 Static（HTTRANSPARENT）与客户区上被问到，那才回 HTCAPTION。
;  这里再用 WindowFromPoint 复核一遍落点（落在我们自己的 Edit/Button 上就老实回
;  HTCLIENT），免得将来布局改动把交互控件挪进「无人认领」的区域。
;
;  注册只有一处（模块级、只挂一次）：BuildBox/Rebuild 会换 HWND，处理里经
;  BoxHwndSafe 现取比对 —— 每次建框都挂一次会累积旧 HWND 的处理，是漏水的路子。
; ---------------------------------------------------------------------------
HitTestHandler(wParam, lParam, msg, hwnd) {
    global Bgui
    if (hwnd != BoxHwndSafe(Bgui))
        return                                  ; 不是速记框 → 空返回（交回默认处理）
    ; lParam = 屏幕坐标（低 16 位 x、高 16 位 y，各是有符号 16 位；副屏在左/上就是负）
    x := (lParam & 0xFFFF), y := (lParam >> 16) & 0xFFFF
    if (x > 32767)
        x -= 65536
    if (y > 32767)
        y -= 65536
    pt := Buffer(8, 0)
    NumPut("Int", x, pt, 0), NumPut("Int", y, pt, 4)
    child := DllCall("WindowFromPoint", "Int64", NumGet(pt, 0, "Int64"), "Ptr")
    if (child) {
        buf := Buffer(256, 0)
        DllCall("GetClassNameW", "Ptr", child, "Ptr", buf, "Int", 128)
        cls := StrGet(buf, "UTF-16")
        if (cls = "Edit" || cls = "Button")
            return 1                            ; HTCLIENT：让它自己收这次点击
    }
    return 2                                    ; HTCAPTION：整框可拖
}

; 拖动开始/结束。程序化的 `Show("x… y…")` / `Move()` 不会产生这一对消息，所以
; 「是不是用户真的拖过」只靠它判：不设标志位就会把每次布局变动都当成拖动。
; v5 §5：起点也要记下来 —— WM_EXITSIZEMOVE 时会拿它比一比（见下）。
EnterSizeMoveHandler(wParam, lParam, msg, hwnd) {
    global Bgui, G_BoxDragging, G_BoxDragX, G_BoxDragY
    if (hwnd != BoxHwndSafe(Bgui))
        return
    G_BoxDragging := true
    Bgui.GetPos(&bx, &by)                        ; 物理像素（Move/GetPos 的同一口径）
    G_BoxDragX := bx, G_BoxDragY := by
}

ExitSizeMoveHandler(wParam, lParam, msg, hwnd) {
    global Bgui, G_BoxDragging, G_BoxDragX, G_BoxDragY
    if (hwnd != BoxHwndSafe(Bgui) || !G_BoxDragging)
        return
    G_BoxDragging := false
    ; v5 §5：**真的移动过**才落盘 —— 单击把手（按下→松手，没挪）走完同一对消息，
    ; 但位置没变，不该写 box_pos=custom（否则框被「钉」在原地，托盘复位形同虚设）。
    Bgui.GetPos(&bx, &by)
    if (bx = G_BoxDragX && by = G_BoxDragY) {
        Log("拖动结束但位置未变（单击把手）：不写 box_pos")
        return
    }
    Log("拖动结束：位置改变 " . (bx - G_BoxDragX) . "," . (by - G_BoxDragY) . " px → 落盘")
    SaveBoxPos()                                 ; 松手即定：位置立刻落 config
}

OnMessage(0x0084, HitTestHandler)               ; WM_NCHITTEST
OnMessage(0x0231, EnterSizeMoveHandler)         ; WM_ENTERSIZEMOVE
OnMessage(0x0232, ExitSizeMoveHandler)          ; WM_EXITSIZEMOVE

; ---------------------------------------------------------------------------
;  托盘（spec §10.1 + v2 §6.5 的项：一级平铺、无子菜单、无图标；
;  `暂停热键` 用勾选态；高频在前、`退出` 用分隔线隔开）
;  项与顺序**一处定义**在 Logic 的 TrayMenuItems（harness 钉住），这里只照着建
;  菜单、按 action 挂回调，并把最终菜单文案记一条日志。
; ---------------------------------------------------------------------------
SetupTray() {
    global G_Cfg, ICON_NORMAL
    if (FileExist(ICON_NORMAL))
        TraySetIcon(ICON_NORMAL)          ; #10 自绘五档 ico
    A_IconTip := "英语取词"
    m := A_TrayMenu
    m.Delete()
    labels := ""
    for it in TrayMenuItems(G_Cfg) {
        if (it.action = "") {
            m.Add()                        ; 分隔线
            continue
        }
        m.Add(it.label, TrayHandler(it.action))
        labels .= (labels = "" ? "" : " / ") . it.label
    }
    m.Default := TrayDefaultLabel(G_Cfg)
    ; 开机自启用勾选态表达（与「暂停热键」同一套）；状态以 config 为准
    if (AutostartEnabled(G_Cfg.autostart))
        m.Check("开机自启动")
    Log("托盘菜单：" . labels)
}

; 菜单项 action → 回调（§6.5 的每一项都落到一个已有函数；未知 action 给个空函数，
; 不静默崩）。每项点击的日志在各回调里（OpenReview/OpenDataDir/…都记）。
TrayHandler(action) {
    if (action = "pause")
        return (*) => TogglePause()
    if (action = "review")
        return (*) => OpenReview()
    if (action = "datadir")
        return (*) => OpenDataDir()
    if (action = "box-home")
        return (*) => BoxHome()
    if (action = "restart-kernel")
        return (*) => RestartKernelNow()
    if (action = "restart-hub")
        return (*) => RestartHubNow()
    if (action = "autostart")
        return (*) => ToggleAutostart()
    if (action = "test-hotkey")
        return (*) => TestHotkey()
    if (action = "dark")
        return (*) => ToggleDark()
    if (action = "exit")
        return (*) => TrayExit()
    return (*) => 0
}

; 托盘「退出」：先记一条点击日志再退（进程树收尾在 OnExitHandler：内核 + 中枢）。
TrayExit() {
    Log("托盘：退出")
    ExitApp()
}

; 托盘「速记框回到鼠标处」（§7）：复位 `box_pos=auto` 并清 `box_x`/`box_y`
; —— 下次呼出就回到现状（鼠标右下）。框若正开着，当场挪回鼠标处：拖偏了想找回
; 手感时不用等下一次呼出。写 config 走 SaveConfigKeys（与拖动结束同一条路）。
BoxHome() {
    global G_Cfg, Bgui
    Log("托盘：速记框回到鼠标处")
    SaveConfigKeys(Map("box_pos", "auto", "box_x", "", "box_y", ""), "box_pos=auto")
    G_Cfg.boxPos := "auto", G_Cfg.boxX := "", G_Cfg.boxY := ""
    if (Bgui && DllCall("IsWindowVisible", "Ptr", Bgui.Hwnd))
        PresentBox(true)
    ShowToast("速记框回到鼠标处")
}

; 暂停 / 恢复热键（勾选态表达状态）。决策 D9：暂停**不**置灰托盘图标 ——
; 灰 = 内核死（§11 的状态信号），暂停是用户主动行为，两个信号不混用。
; 名单来自 G_GlobalKeys（RegisterGlobalHotkeys 登记的那份），所以 C2 新增的
; 「直接截图取词」也跟着一起暂停 —— 只暂停一半等于没暂停。
TogglePause() {
    global G_GlobalKeys, G_HotkeyPaused
    G_HotkeyPaused := !G_HotkeyPaused
    n := 0, specs := ""
    for spec in G_GlobalKeys {
        try {
            ; §9.2 的暂停语义：暂停时把每个键 Off 掉，恢复时 On 回来
            Hotkey(spec, G_HotkeyPaused ? "Off" : "On")
        } catch as err {
            Log("热键" . (G_HotkeyPaused ? "暂停" : "恢复") . "失败 " . spec . "：" . err.Message)
        }
        n++
        specs .= (specs = "" ? "" : " / ") . spec
    }
    if (G_HotkeyPaused) {
        A_TrayMenu.Check("暂停热键")
        ShowToast("热键已暂停；再点一次托盘「暂停热键」恢复")
    } else {
        A_TrayMenu.Uncheck("暂停热键")
        ShowToast("热键已恢复")
    }
    Log("热键已" . (G_HotkeyPaused ? "暂停" : "恢复") . "（" . n . " 个：" . specs . "）")
}

; 托盘「开机自启动」（勾选态表达状态）：开 = Startup 里放快捷方式，关 = 删掉。
; 真实状态由 .lnk 的存在表达，config `autostart` 只记住用户意图 —— 否则用户
; 关掉后下次手动启动又被启动收敛建回来，选择被静默推翻。
ToggleAutostart() {
    global G_Cfg
    on := !AutostartEnabled(G_Cfg.autostart)
    link := StartupLinkPath(A_Startup)
    if (on) {
        EnsureStartupShortcut()
        if (!FileExist(link)) {
            ShowToast("开启开机自启失败（详见 logs\app.log）", "err")
            return
        }
        A_TrayMenu.Check("开机自启动")
    } else {
        try {
            if (FileExist(link))
                FileDelete(link)
        } catch as err {
            Log("自启：删除快捷方式失败 " . link . "：" . err.Message)
            ShowToast("关闭开机自启失败（详见 logs\app.log）", "err")
            return
        }
        A_TrayMenu.Uncheck("开机自启动")
    }
    G_Cfg.autostart := on ? "1" : "0"
    SaveConfigKeys(Map("autostart", G_Cfg.autostart), "autostart=" . G_Cfg.autostart)
    ShowToast(on ? "开机自启动已开启" : "开机自启动已关闭")
    Log("托盘：开机自启动 → " . (on ? "开" : "关"))
}

; 托盘「打开数据目录」（§6.5）：`<工具目录>\data`（= 数据库文件所在目录），
; 不存在则创建。创建失败只提示不拦路（日志 + toast）。
OpenDataDir() {
    global G_DataDir
    if (!DirExist(G_DataDir)) {
        try
            DirCreate(G_DataDir)
        catch as err {
            Log("托盘：创建数据目录失败 " . G_DataDir . "：" . err.Message)
            ShowToast("创建数据目录失败：" . G_DataDir, "err")
            return
        }
        Log("托盘：已创建数据目录 " . G_DataDir)
    }
    Run('explorer.exe "' . G_DataDir . '"')
    Log("托盘：打开数据目录 " . G_DataDir)
}

; 托盘「重启内核」：与守护的自动重启同一条路。决策 D11：该项常态可用、
; 不置灰 —— 内核死时正是最想点它的时候（#10 原型的置灰是因为原型无内核）。
RestartKernelNow() {
    Log("托盘：手动重启内核")
    RestartKernel()
    TraySync()                        ; 重启窗口期也是「内核不在位」
    ShowToast("OCR 内核正在重启")
}

; 托盘「重启中枢」（§6.5）：与守护的自动重启同一条路（RestartHub），完成后由
; 探测握手；图标语义同内核（TraySync）。
RestartHubNow() {
    Log("托盘：手动重启中枢")
    RestartHub()
    TraySync()
    ShowToast("数据中枢正在重启")
}

; 「测试热键」（spec §4.1）：点托盘项 → 气泡提示请按一次 → 用户按热键 →
; toast「热键正常收到」。pending 标志位区分「测试按下」与「正式捕获」。
TestHotkey() {
    global G_Cfg, G_HotkeyPaused, G_TestHotkey
    if (G_HotkeyPaused) {
        ShowBubble("热键已暂停（" . G_Cfg.hotkey . "），请先取消暂停再测试", "英语取词", 3)
        return
    }
    G_TestHotkey := true
    Log("托盘：测试热键，等待按一次 " . G_Cfg.hotkey)
    ShowBubble("请按一次 " . G_Cfg.hotkey . " 确认收得到", "英语取词")
}

; 决策 D6：主题的切换入口放托盘菜单（§6.5 保留「切换深色」）。§6.3 要求主题
; **落到 config**，所以这里顺手把 config.ini 的 theme 键原地改掉（保留其他键与
; 注释，§8.4 的同一条要求）。当前开着的速记框按新主题重建（Rebuild 机制）。
ToggleDark() {
    global G_Dark, Bgui
    G_Dark := !G_Dark
    Log("主题切换为 " . (G_Dark ? "深色" : "浅色"))
    SaveTheme(G_Dark ? "dark" : "light")
    ; 只重建**可见**的框：截图取词中途框是隐藏的，重建会把它弹出来
    if (Bgui && DllCall("IsWindowVisible", "Ptr", Bgui.Hwnd))
        Rebuild()
    ; 票 #185（A8）：已打开的长文面板同批按新主题重建；隐藏中的面板不弹出来
    ; （守卫在 LfRebuildForTheme 里，与旧框同一条）
    LfRebuildForTheme()
    ShowToast(G_Dark ? "已切换深色" : "已切换浅色")
}

; 主题写回 config.ini（§6.3）。改不动也不拦人：只记日志 —— 主题是偏好，不是数据。
SaveTheme(theme) {
    SaveConfigKeys(Map("theme", theme), "theme=" . theme)
}

; 一批 config 键写回（§6.3 主题 / §7 位置记忆共用一套「读全文 → 原地改 → 写回」）。
; 原地改靠 Logic.IniSetText：段内已有的键换值、没有的键插到段尾，段本身不丢。
; 返回 true = 文件已是最新（含「本来就是这个值」）。改不动只记日志，不弹框。
; 票 #185（A11）：读-改-写与 hub 设置页整文件写 config 之间有丢更新窗口 —— 旧实现
; 读取后不管文件动没动直接整文件写回。现在写前比对「读前拍的 mtime+size 指纹」
; （判据 Logic.ConfigWriteAllowed，harness 片 41 钉）：不一致 = 读取期间有外部写入，
; 重读重改再来一轮（最多 3 轮）。残余窗口只剩「拍指纹 → FileOpen」几毫秒，不再
; 引入文件锁/重命名那套复杂度。
SaveConfigKeys(pairs, what) {
    global CONFIG_PATH, SECTION, G_ConfigStamp
    loop 3 {
        stamp := ConfigStamp()
        txt := ""
        try
            txt := FileRead(CONFIG_PATH, "UTF-8")
        catch as err {
            Log("config 未写入（" . what . "）：" . err.Message)
            return false
        }
        newTxt := txt
        for key, value in pairs
            newTxt := IniSetText(newTxt, SECTION, key, value)
        if (newTxt = txt)
            return true
        if (!ConfigWriteAllowed(stamp, ConfigStamp())) {
            Log("config 在读取期间被外部修改（" . what . "），重读重写")
            continue
        }
        try {
            f := FileOpen(CONFIG_PATH, "w", "UTF-8-RAW")   ; 无 BOM：内核侧也读这个文件
            try {
                f.Write(newTxt)
            } finally {
                f.Close()
            }
            ; 自己写的盘自己认账：同步 stamp，免得 2s 后 WatchConfig 把这次写入误判为
            ; 外部改动 → ReloadConfig 重注册热键 + 重建可见框（主题/位置当场已各自生效；
            ; hub 设置页写 config 不走这里，外部写入的热重载路径不受影响）。
            G_ConfigStamp := ConfigStamp()
            Log("config 已写入（" . what . "）")
            return true
        } catch as err {
            Log("config 写入失败（" . what . "）：" . err.Message)
            return false
        }
    }
    Log("config 未写入（" . what . "）：外部写入频繁，3 轮重读后放弃（下次再试）")
    return false
}

; ---------------------------------------------------------------------------
;  config 热重载（v2 §9.2）：每 2s 比对 config.ini 的 mtime+size，变了就重读 →
;  重注册全局键（先 Off 旧再 On 新）→ 重建框内键环与 F1 键位表 → 重新探测 →
;  写状态文件 → 记日志。主题/log_enabled/ocr_timeout_ms 这类外壳自用的项当场生效。
;  由内核/中枢（Python）自读 config.ini 的项（session_idle_min / dict_online）外壳
;  不转发 —— 它们下一次启动才生效，见票 #28 说明。
; ---------------------------------------------------------------------------
ConfigStamp() {
    global CONFIG_PATH
    stamp := ""
    try
        stamp := FileGetTime(CONFIG_PATH, "M") . "|" . FileGetSize(CONFIG_PATH)  ; 文件不在 = ""
    return stamp
}

WatchConfig() {
    global G_ConfigStamp, G_Demo
    if (G_Demo)
        return
    s := ConfigStamp()
    if (s = "" || s = G_ConfigStamp)
        return
    G_ConfigStamp := s
    ReloadConfig()
}

ReloadConfig() {
    global G_Cfg, G_Dark, G_LogEnabled, G_OcrTimeout, G_HubPort, G_WebPort, G_SpoolPath, G_DataDir, Bgui, G_KeymapGui
    ; v21 T5（#160）：longform_x/y/w/h（长文面板几何）**故意不在这里 apply** ——
    ; 几何只在启动读一次（LongformStartup），拖动结束才写回（LfSaveGeom）。
    ; 把几何也放进热重载，2s 一次的全量重读会把用户刚拖到的位置按旧值摆回去。
    G_Cfg := ReadConfig()
    G_Dark := (G_Cfg.theme != "light")
    G_LogEnabled := (G_Cfg.logEnabled != "0")
    G_OcrTimeout := G_Cfg.ocrTimeout
    G_HubPort := G_Cfg.hubPort
    G_WebPort := G_Cfg.webPort
    G_SpoolPath := SpoolPathOf(ResolveExe(G_Cfg.dbPath))
    G_DataDir := DataDirOf(ResolveExe(G_Cfg.dbPath))
    Log("配置热重载：theme=" . G_Cfg.theme . " ocr_timeout_ms=" . G_OcrTimeout
      . " hub_port=" . G_HubPort . " web_port=" . G_WebPort . "（无需重启）")
    RegisterGlobalHotkeys()                        ; 重注册 + 重新探测 + 写状态文件
    ; 框可见 → 重建：BuildBox 内部 BoxKeys(false)、PresentBox 内部 BoxKeys(true)，
    ; 框内键环就按新键位重挂了（主题变了也一并刷新）。不可见就别动（免得凭空弹框）。
    if (Bgui && DllCall("IsWindowVisible", "Ptr", Bgui.Hwnd))
        Rebuild()
    if (G_KeymapGui)
        KeymapClose()                              ; 键位表开着就关掉，免得显示旧键位
}

; ---------------------------------------------------------------------------
;  启动自检（spec §4.1 / §11）
;
;  #26 起保存路径走 spool（`<data>\inbox.jsonl`），v1 的「捕获库可达性 + 补 README」
;  自检（决策 D7）随之删掉：`vault_dir` 现在只服务 v1 导入（`hub.py --import-vault`），
;  spool 目录由 SpoolAppend 首次写入时按需创建，建不出来走 FailSave（框不关 + 进剪贴板）。
; ---------------------------------------------------------------------------

; 开机自启单一入口（spec §10.1）：Startup 里只放一个指向**外壳**的快捷方式，
; 内核由外壳拉起（单所有者，§10.1）。幂等：已存在就不重建，结果写日志。
; 不提供卸载入口 —— README 写明删 Startup 里那个 英语取词.lnk 即可。
EnsureStartupShortcut() {
    link := StartupLinkPath(A_Startup)
    if (FileExist(link)) {
        Log("自启：快捷方式已存在 " . link)
        return
    }
    sc := StartupShortcut(A_AhkPath, A_ScriptFullPath)
    try {
        ; WScript.Shell COM 对象创建 .lnk（本机实测可用；目标/参数/工作目录
        ; 三个值由 Logic.ahk 的纯函数给出，测试钉住）
        wsh := ComObject("WScript.Shell")
        lnk := wsh.CreateShortcut(link)
        lnk.TargetPath := sc.target
        lnk.Arguments := sc.args
        lnk.WorkingDirectory := sc.workDir
        lnk.Description := "英语取词（AHK 外壳；OCR 内核由它拉起）"
        lnk.Save()
        Log("自启：已创建快捷方式 " . link . " → " . sc.target . " " . sc.args)
    } catch as err {
        Log("自启：创建快捷方式失败：" . err.Message)
        ShowBubble("创建开机自启快捷方式失败（详见 logs\app.log）", "英语取词", 3)
    }
}

; 启动时按 config 收敛自启状态：开 → 幂等创建；关 → 快捷方式还在就删掉
; （用户可能手改过 config，或上次删除没成功）。托盘菜单的勾选态也读同一个键。
ApplyStartupAutostart() {
    global G_Cfg
    if (AutostartEnabled(G_Cfg.autostart)) {
        EnsureStartupShortcut()
        return
    }
    link := StartupLinkPath(A_Startup)
    if (!FileExist(link)) {
        Log("自启：设置为关闭（无快捷方式）")
        return
    }
    try {
        FileDelete(link)
        Log("自启：设置为关闭，已删除快捷方式 " . link)
    } catch as err {
        Log("自启：设置为关闭，但删除快捷方式失败 " . link . "：" . err.Message)
    }
}

; ---------------------------------------------------------------------------
;  两条入口共用的「一次捕获的初始状态」：出处、会话 chip 的本地占位、折叠态。
;  **必须在任何窗口出现之前调用**（v1 §6.2 的硬约束）—— 出处是调用方传进来的，
;  因为它读的就是「此刻谁在前台」，晚一步就变成我们自己（`NewCapture` 的框、
;  `ShotHotkey` 的遮罩都会抢前台）。
; ---------------------------------------------------------------------------
BeginCapture(prov) {
    global V, Hint, G_PrevHwnd
    global G_Expanded, G_CtxRows, G_Kind, G_KindTouched
    global G_SessTitle, G_SessCount, G_SessNew, G_SessOffline, G_SessApp, G_SessRawTitle
    global G_SessSite, G_BrowserUrl, G_CaptureGen
    global G_Status, G_StatusArg
    global G_LookupDeb, G_LastAutoMeaning, G_CorrText, G_SeenText, G_DictWait, G_NoteText
    global G_MediaPauseSent, G_TargetAuto, G_LookupFollowedUp
    ; v5 §3：一次捕获开始 → 停播。放在**任何窗口出现之前**（与出处同一时刻）：
    ; 这一刻前台还是用户那只浏览器窗口，hub 的门（app token + 标题宽松匹配）才对得上。
    ; 两条入口（Ctrl+Alt+1 / Ctrl+Alt+2）都走这里，所以这是唯一的 pause 漏斗。
    ; 已有框时再按呼出键不会走到这（NewCapture / ShotHotkey 各自先挡），标志是第二道保险：
    ; 已经压着一次暂停就不再发（不重复 pause、也不把已清掉的标志重新点亮）。
    if (!G_MediaPauseSent && HubMedia("pause", prov.app, prov.title))
        G_MediaPauseSent := true
    V := BlankV()
    V.prov := prov.title, V.provAuto := prov.auto
    ; 焦点还原的目标（票 #47 B1）：就是在「任何窗口出现之前」抓到的那只前台窗口。
    ; 有框入口复用（ShotHotkey 的已有框分支不重抓）时会沿用上一次留下的值，正是想要的。
    G_PrevHwnd := prov.hwnd
    ResetHint()
    ; 每次捕获都回到折叠态（紧凑形态是默认；§6.3）
    G_Expanded := false, G_CtxRows := 1
    G_Kind := "word", G_KindTouched := false
    G_Status := "", G_StatusArg := 0
    ; 词典自动填入的状态一并归零（票 #27）：上一次捕获的查询/释义/校正/已记过不带过来
    G_LookupDeb := 0, G_LastAutoMeaning := ""
    G_LookupFollowedUp := ""                   ; v18 W4（#149）：二跳标记也归零（Esc 取消
                                               ; 不走 ResetForNextWord，别把下一个同词压死）
    G_TargetAuto := ""                          ; v5 §4.1：新捕获没有自动猜词
    G_CorrText := "", G_SeenText := "", G_DictWait := false, G_NoteText := ""
    ; 会话线索（spool 的 source_app/source_title + peek 的入参）：出处在这一刻采集，
    ; 晚一步读到的就是本工具自己的窗口（`NewCapture` 的框、`ShotHotkey` 的遮罩）
    G_SessApp := prov.app, G_SessRawTitle := prov.title
    ; 会话 chip 先立**本地占位**（原始标题截断），再异步问 hub 要清洗后的标题与词数
    ; （§6.1）；hub 不在位 → 占位改成「（离线）」。两条入口都在这里 peek ——
    ; 直拍入口一开始没有框，回话到的时候框才刚建（OnPeekLine 只更状态，不碰控件）。
    G_SessTitle := prov.title, G_SessCount := 0, G_SessNew := false, G_SessOffline := false
    ; 站点徽章 / 页面 URL 也归零：这一次的 peek 回话才知道它们（§7；不带上一次的过来）
    G_SessSite := "", G_BrowserUrl := ""
    ; 票 #70：新一代捕获 —— 上一轮（已取消 / 已收工）的 peek 回话据此被丢弃，
    ; 不许把上一条视频的标题、站点徽章、页面 URL、位置时间写进这一轮
    G_CaptureGen += 1
    ; v5 §2.4：把呼出前那只窗口的 hwnd 交给 hub —— 本机探针只读**这一只**（浏览器才读）
    HubPeek(prov.app, prov.title, prov.hwnd)
}

; ---------------------------------------------------------------------------
;  入口一（全局 `hotkey`，默认 `Ctrl+Alt+1`）：一次捕获的开始 —— 先抓出处
;  （任何窗口出现之前），再建速记框。
; ---------------------------------------------------------------------------
NewCapture() {
    global V, Bgui, G_ShotActive, G_TestHotkey, G_Cfg, G_LongBusy, G_LongPending
    if (G_TestHotkey) {
        ; 「测试热键」的 pending 标志位：这一次按下只确认通路，不开捕获框
        G_TestHotkey := false
        Log("测试热键：收到一次 " . G_Cfg.hotkey . "（热键通路正常）")
        ShowToast("热键 " . G_Cfg.hotkey . " 正常收到", "ok")
        return
    }
    if (G_ShotActive) {
        Log("拖框进行中，忽略本次热键")
        return
    }
    ; 票 #176（A3）：长文遮罩拖框/识别期间呼出速记框一并拒绝（框会叠在全屏遮罩上
    ; 互踩；判据与 RunShot 同源：Logic.LongformInFlight）。
    if (LongformInFlight(G_LongBusy, G_LongPending)) {
        Log("长文在途：呼出速记框入口已拒绝（避免两条链路互踩）")
        ShowToast("长文识别进行中，请稍后再取词", "err")
        return
    }
    if (Bgui) {
        Log("速记框已存在，忽略本次热键")
        return
    }
    prov := CaptureProvenance()        ; 必须在任何窗口出现之前（spec §6.2）
    BeginCapture(prov)
    ; 不记窗口标题本身（spec §10.2 不记用户内容：标题就是「用户在看什么」）
    Log("呼出速记框：出处已在任何窗口出现之前采集（自动出处"
      . (prov.auto = "" ? "没抓到" : "已抓到") . "）")
    BuildBox()
    ApplyVals(V)
    ShowBox()
}

; ---------------------------------------------------------------------------
;  `/demo`（票 #24 的补充 AC）：只看形态，不碰任何真实资源 —— 示例值取 §6.3 的
;  线框图与 §7 的词条样例；C2–C6 的截图验收都从这里出。
; ---------------------------------------------------------------------------
DemoBox() {
    global V, Hint, G_SessTitle, G_SessCount, G_SessNew, G_Expanded, G_CtxRows, G_TargetAuto
    SetupDemoTray()
    V := BlankV()
    V.target := "reluctant"
    V.context := "I was reluctant to understand."
    V.meaning := "不情愿的；勉强的"
    V.phonetic := "/rɪˈlʌktənt/"                  ; C4 之前由词典填；demo 直接给值
    V.prov := "Why do we procrastinate? - YouTube"
    V.provAuto := 'Edge · "Why do we procrastinate? - YouTube"'
    V.pos := "12:34"
    ; v5 §4.1：demo 复现的是「OCR 回填之后」的形态 —— 目标词是自动猜的，首次聚焦
    ; 整段全选（截图验收要看的就是这个状态；不设它只会落光标末尾）。
    G_TargetAuto := NormalizeLine(V.target)
    ResetHint()
    G_Expanded := false, G_CtxRows := 1
    G_SessTitle := "这样练英语听力", G_SessCount := 7, G_SessNew := false
    BuildBox()
    ApplyVals(V)
    ShowBox()
    SetStatus("session-new")                     ; 状态行（§6.3 的字面量）
    ; v5 §4.2：提示行也是框上可见形态的一部分 —— demo 复现的是「OCR 回填之后」
    ; （目标词 = 从原句里猜出来的整词），按收紧后的规则此时**不该有提示行**
    ; （整词已在句内 → CompletionMissHint 为空）；合成一次才看得出这个「正确地不出现」。
    ApplyHintState()
    Log("已显示速记框（/demo：示例值；不注册热键、不拉内核/中枢）")
}

; `/demo` 的托盘：只留「切换深色」（截深/浅两张图用）与「退出」
SetupDemoTray() {
    global ICON_NORMAL
    if (FileExist(ICON_NORMAL))
        TraySetIcon(ICON_NORMAL)
    A_IconTip := "英语取词（/demo）"
    m := A_TrayMenu
    m.Delete()
    m.Add("切换深色", (*) => ToggleDark())
    m.Add()
    m.Add("退出", (*) => ExitApp())
}

; ============================================================================
;  启动
; ============================================================================
OnExit(OnExitHandler)

; `/demo`（票 #24 的补充 AC）：只把速记框按示例值显示出来 —— 不注册热键、不拉
; 内核与中枢、不碰捕获库、不建自启快捷方式。用来截图与人工看形态（热键在进程外
; 没法可靠模拟），后续 C2–C6 的视觉验收都复用它。
; 注意 AHK 的 A_Args：带 `/` 的参数原样进来，用 A_Args.Length + A_Args[1] 读
; （v2 没有 A_Args.HasKey 这种 API）。
G_Demo := (A_Args.Length >= 1 && A_Args[1] = "/demo")
if (G_Demo)
    G_Title := "英语取词 速记（/demo）"
; v21 T5（#160）长文面板的 demo 开关：`--lf-demo=<state>`（hit/empty/multi/outline/
; low/nomatch/dark/geom-restore）。它走**独立入口**：不注册任何热键、不拉内核与中枢、
; 不碰 spool —— 关掉这个开关时对正常启动路径零影响（这里只多一次字符串判断）。
G_LfDemo := LongformDemoArg(A_Args)

G_Cfg := ReadConfig()
G_Dark := (G_Cfg.theme != "light")         ; 主题默认深色（§6.3）；选择落 config
G_Port := G_Cfg.port
G_VaultDir := G_Cfg.vaultDir
G_HubPort := G_Cfg.hubPort
G_WebPort := G_Cfg.webPort                 ; hotkey_review 打开的 HTTP 端口
G_SpoolPath := SpoolPathOf(ResolveExe(G_Cfg.dbPath))   ; <data>\inbox.jsonl（§2）
G_HotkeyStatusPath := HotkeyStatusPathOf(ResolveExe(G_Cfg.dbPath))  ; <data>\hotkey-status.json
G_DataDir := DataDirOf(ResolveExe(G_Cfg.dbPath))       ; <工具目录>\data（§6.5）
G_OcrTimeout := G_Cfg.ocrTimeout
G_LogEnabled := (G_Cfg.logEnabled != "0")
G_Common := LoadCommonWords(WORDS_PATH)

Log("==== 英语取词启动 ahk=" . A_AhkVersion . " dpi=" . A_ScreenDPI
  . " screen=" . A_ScreenWidth . "x" . A_ScreenHeight
  . (G_Demo ? " /demo" : "") . (G_LfDemo != "" ? " --lf-demo=" . G_LfDemo : "") . " ====")
Log("配置：hotkey=" . G_Cfg.hotkey . " hotkey_capture=" . G_Cfg.hotkeyCapture . " port=" . G_Port
  . " upsample_min_height=" . G_Cfg.minHeight . " ocr_timeout_ms=" . G_OcrTimeout
  . " log_enabled=" . G_LogEnabled)
Log("配置：theme=" . (G_Dark ? "dark" : "light")
  . " key_save=" . G_Cfg.keySave . " key_cancel=" . G_Cfg.keyCancel
  . " key_shot=" . G_Cfg.keyShot . " key_more=" . G_Cfg.keyMore
  . " key_help=" . G_Cfg.keyHelp)
Log("配置：hub_port=" . G_HubPort . " web_port=" . G_WebPort . " spool=" . G_SpoolPath
  . "（保存路径；中枢不在位也照写）")
Log("配置：data_dir=" . G_DataDir . "（托盘「打开数据目录」；不存在则创建）")
Log("配置：vault_dir=" . G_VaultDir . "（仅 v1 导入用：hub.py --import-vault）")
Log("猜词词表 " . G_Common.Count . " 词（data\common_words.txt）")

; v21 T5（#160）：长文面板 demo 走**独立入口** —— 只建面板窗口，走完自检/截图就退出。
; 不注册热键（避免抢用户键盘）、不拉内核/中枢、不碰 spool；正常启动路径在 else 分支。
if (G_LfDemo != "")
    LongformDemoRun(G_LfDemo, A_Args)
else if (G_Demo)
    DemoBox()
else
    StartNormal()

; 正常启动：托盘 → 自检 → 注册全局热键 → 拉内核并开始握手。
; （收成一个函数而不是摊在顶层：顶层语句块里的全局赋值会被
;   tests/test_shell_source.py 的静态扫描当成「没声明 global」，写了容易误伤。）
StartNormal() {
    global G_Deadline, G_ConfigStamp, G_HubWatchPhase, G_HubProbeSent, G_HubDeadline, G_HubToldNotReady
    ; --- 启动自检（spec §4.1 / §11；决策 D7 / D10）---------------------------
    ; 托盘先就位：后面的自检消息都是往托盘上挂的气泡
    SetupTray()
    ApplyStartupAutostart()

    ; v21 T5（#160）：长文面板几何**只在这里读一次**（LongformStartup）。
    ; config 热重载（ReloadConfig）故意不碰 longform_x/y/w/h —— 否则 2s 热重载会
    ; 把用户刚拖到的位置按旧值摆回去（研究 §7.2）。这是启动初始化 hook 的唯一内容。
    LongformStartup()
    ; v22 P2b（spec §9）：分段预览窗几何同一条纪律 —— 启动只读一次（LongformStartup
    ; 是面板的；预览窗自己的四键在这里读，ReloadConfig 同样不碰）。
    LfInspectorStartup()

    ; 全局热键（§6.2/§9.1 的全部全局入口）：注册 + 三条冲突检测 + 写
    ; data/hotkey-status.json + 失败气泡点名，都在 RegisterGlobalHotkeys() 里。
    ; 先把入口挂上再拉内核：热键是用户唯一入口，注册结果必须诚实记录。
    RegisterGlobalHotkeys()
    ; #48（B2）：呼出键是裸 Alt 形态（如 `!1`）时记一行提示（不弹气泡）；只在这里调，
    ; 热重载（ReloadConfig）不重复刷。
    ; v35 #232 接线点 A：拉起桌宠角色窗（素材缺失 / pet 关闭时自己干净退出，HUD 接管）。
    PetStartup()
    ; v35-ux D1：信息流播种一行（行全是边沿触发，不播种的话手动打开就是空面板）
    ; 2026-11-10 竖排改版：面板收到 ~196lp，这条播种文案跟着缩短——原来那句
    ; 「信息流就绪 · 开收句（Ctrl+Alt+7）后实时汇报录入情况」在窄条里要折三行。
    PetFeedLine("信息流就绪（" . StatusKeyText(G_Cfg, "hotkeyCaptureArm", "^!7") . " 开收句）", "idle")
    ; v35 #233 接线点 B：待挑计数 60 秒低频轮询（不进 1Hz ping）+ 先轮一次
    PetBubbleStartup()
    WarnBareAltHotkey()

    ; config 热重载（§9.2）：2s 轮一次 mtime+size，变了就重读 + 重注册 + 重探测
    G_ConfigStamp := ConfigStamp()
    SetTimer(WatchConfig, CONFIG_WATCH_MS)

    ; 中枢（票 #29）：先对端口做确定性接管（决策 D12：有外来者在听先驱逐），再
    ; 自己拉起并开始**存活探测**（与内核同一套：定时 ping + 在途超时判死）。
    ; hub 就绪 = 收到 pong（HubUp）→ 托盘才蓝；此后每秒探一枚，hub 被杀/重启后
    ; 外壳会自己发现并重连，不需要用户重启外壳。
    ; 2026-11-10：端口空着时**零 netstat**（`EvictPortSquatter` 内部先做毫秒级探活）。
    ; 原来无条件跑 netstat（每次 2.2s），启动时跑两次 = 3.4 秒白等 —— 就是「起得慢」。
    EvictPortSquatter(G_HubPort, "中枢")
    ; 拉不起来（解释器路径写错 / Run 失败）：hub 永远不会来，图标当场灰掉；
    ; 保存照走 spool（词不丢），StartHub 已气泡 + 记日志。
    if (!StartHub())
        TraySync()
    G_HubWatchPhase := "retrying"
    G_HubProbeSent := 0
    G_HubDeadline := A_TickCount + HUB_START_TIMEOUT
    G_HubToldNotReady := false
    SetTimer(HubWatch, HUB_PROBE_MS)

    ; 内核：先对端口做确定性接管（决策 D12：若有外部内核在听，找到 PID 驱逐、
    ; 等端口释放，再由我们拉起——spec §10.1 单所有者，#11 探针的「认领」路径
    ; 已删），再拉起并握手 300ms×10s（spec §5.3）；超时走托盘气泡并继续重试
    ; （决策 D10）。
    ;
    ; 2026-11-10：**内核与中枢并行拉起**。原来这里先 `HubWatch()` 立刻探一次，
    ; 而 hub 刚被拉起、还没 bind —— 那次 `SocketTCP.Connect` 会同步等满 2 秒，
    ; 内核因此晚了 2 秒才开始启动（用户：「内核和中枢起得那么慢」）。
    ; 现在先把两个进程都拉起来，探测交给 80ms 后的定时拍子（hub 也还在冷启动，
    ; 一拍就够，不必在启动路径上阻塞）。
    EvictPortSquatter(G_Port, "内核")
    ; 拉不起来（解释器路径写错 / Run 失败）：内核永远不会来，图标当场灰掉
    ; （spec §11 的灰 = 「内核不在位」；否则用户对着蓝图标等一个不会来的内核）
    if (!StartKernel())
        TraySync()
    G_Deadline := A_TickCount + PING_TIMEOUT
    SetTimer(Tick, PING_INTERVAL)
    ; 独立闭包槽：`SetTimer(HubWatch, -80)` 会**替换**上面那条周期 HubWatch，
    ; 让「挂而不死」的探测彻底停摆（AHK 同一函数只有一个定时器）。
    SetTimer(() => HubWatch(), -80)  ; 两个进程都起来了再探（不阻塞启动）
}


