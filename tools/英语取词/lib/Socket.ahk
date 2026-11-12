; ============================================================================
;  Socket.ahk —— AHK 侧唯一的第三方依赖（spec §2.2、§3）
;
;  上游：https://github.com/G33kDude/Socket.ahk （master，commit 68d66c9）
;  上游仓库没有 LICENSE 文件；spec §2.2 把它记为 MIT。
;
;  【不是原样 vendor：上游那份是 v1 语法，票 #11 把它转成 v2 才跑得起来】
;  改动只有语法层，没有任何行为/协议改动，逐条列在下面：
;    1. Listen(backlog=32)              → backlog := 32（v2 默认值语法）
;    2. static SocketType/ProtocolId    → 子类的「类体实例变量」
;                                          （v2 里 this.X 读不到 static 成员）
;    3. 常量改用类名引用：this.WM_SOCKET → Socket.WM_SOCKET，其余同理
;    4. ByRef Buffer → &Bytes，接收缓冲改用 Buffer 对象
;       （原写法 VarSetCapacity + NumGet(var) 在 v2 里不通）
;    5. DllCall 输出参数补 &：`"UInt*", argp` → `"UInt*", &argp`
;    6. `Sleep, x` → `Sleep(x)`
;    7. `Exception(...)` → `Error(...)`：v2 的异常基类叫 Error，没有 Exception。
;       留着 Exception 会让脚本每次启动都弹一个**阻塞**的 VarUnset 警告框。
;       顺带把局部变量 `Error` 改名 `WsaErr` —— 它会遮住 Error 这个类名。
;    8. `static Init` → `static Init := False`：v2 里读一个从未赋值的 static 会抛
;       UnsetError（v1 只是当空值）；`Accept()` 里的局部 `s` 改名 `Accepted`。
;
;  【上面 8 条是「只改语法」；下面这 1 条是**有意的行为修正**，不是语法转换】
;    9. 票 #156：`RecvText()` 原样对每个 recv chunk 单独 `StrGet(UTF-8)`，chunk 末尾
;       落在多字节字符（中文 3 字节 / emoji 4 字节）中间时残缺字节被解成 U+FFFD，
;       静默、无日志、无异常。现在把末尾那段不完整序列扣留到下一拍再一起解码。
;       详见 Utf8SafeLength 与 RecvText 里的注释。签名与调用方零改动。
;
;  【10. 票 #178：`Send()` 循环补发短写】
;       上游只发一次就返回 `send` 的返回值 —— 发送缓冲满时 `send` 合法地只写一部分，
;       尾部 `}` / `\n` 丢掉会让下一条请求粘成一行（中枢只回一条 bad json，外壳的
;       位置 FIFO 从此错位）。现在补发到全量，且**每次从已写字节之后续写**；
;       循环抽成纯函数 `SocketSendAll`（假 sender 可测），短写次数记在 `this.ShortWrites`。
;
;  【11. 票 #185：`OnMessage` 的事件位独立判定】
;       上游的 else-if 链在 WSAAsyncSelect 把 FD_READ|FD_CLOSE **合并**成一条投递时
;       只处理头一个命中的位 —— 数据读到了、FD_CLOSE 被吞，断连迟迟不报（外壳的
;       判死/托盘全被拖住）。现在事件位独立消费，次序 recv → accept → close：先读
;       数据（断开前最后几行回话不能丢），再走断连清理。判据抽成本文件底部的
;       SocketEventRoute（纯函数，harness 片 40 钉；必须随文件自带 —— 探针单独
;       #Include 本文件，见该函数的注释）。
;
;  异步收发靠 WSAAsyncSelect + OnMessage 事件回调：接收在消息回调里完成，
;  不会阻塞主线程 —— 这是 ADR 0002 的硬要求（阻塞读取会冻死全局热键）。
;
;  用法：sock := SocketTCP()
;        sock.Connect(["127.0.0.1", "47800"])
;        sock.SendText(json . "`n")
;  子类覆写 onRecv() / onDisconnect()，其余由基类的事件回调驱动。
; ============================================================================
class Socket
{
	static WM_SOCKET := 0x9987, MSG_PEEK := 2
	static FD_READ := 1, FD_ACCEPT := 8, FD_CLOSE := 32
	static Blocking := True, BlockSleep := 50

	__New(Socket := -1)
	{
		static Init := False
		if (!Init)
		{
			DllCall("LoadLibrary", "Str", "Ws2_32", "Ptr")
			WSAData := Buffer(394 + A_PtrSize, 0)
			if (WsaErr := DllCall("Ws2_32\WSAStartup", "UShort", 0x0202, "Ptr", WSAData))
				throw Error("Error starting Winsock",, WsaErr)
			if (NumGet(WSAData, 2, "UShort") != 0x0202)
				throw Error("Winsock version 2.2 not available")
			Init := True
		}
		this.Socket := Socket
		this.Bound := False ; v2 读一个不存在的属性会抛 PropertyError，先占位
		this.ShortWrites := 0                  ; 票 #178：TCP 短写累计（Send 补发后计数）
		; 票 #156：跨拍被切断的 UTF-8 尾部序列（最多 3 字节）扣留在这里，等下一拍
		this._Pend := Buffer(4, 0)
		this._PendLen := 0
	}

	__Delete()
	{
		if (this.Socket != -1)
			this.Disconnect()
	}

	Connect(Address)
	{
		if (this.Socket != -1)
			throw Error("Socket already connected")
		Next := pAddrInfo := this.GetAddrInfo(Address)
		while Next
		{
			ai_addrlen := NumGet(Next + 0, 16, "UPtr")
			ai_addr := NumGet(Next + 0, 16 + (2 * A_PtrSize), "Ptr")
			if ((this.Socket := DllCall("Ws2_32\socket", "Int", NumGet(Next + 0, 4, "Int")
				, "Int", this.SocketType, "Int", this.ProtocolId, "UInt")) != -1)
			{
				if (DllCall("Ws2_32\WSAConnect", "UInt", this.Socket, "Ptr", ai_addr
					, "UInt", ai_addrlen, "Ptr", 0, "Ptr", 0, "Ptr", 0, "Ptr", 0, "Int") == 0)
				{
					DllCall("Ws2_32\freeaddrinfo", "Ptr", pAddrInfo)
					return this.EventProcRegister(Socket.FD_READ | Socket.FD_CLOSE)
				}
				this.Disconnect()
			}
			Next := NumGet(Next + 0, 16 + (3 * A_PtrSize), "Ptr")
		}
		throw Error("Error connecting")
	}

	Bind(Address)
	{
		if (this.Socket != -1)
			throw Error("Socket already connected")
		Next := pAddrInfo := this.GetAddrInfo(Address)
		while Next
		{
			ai_addrlen := NumGet(Next + 0, 16, "UPtr")
			ai_addr := NumGet(Next + 0, 16 + (2 * A_PtrSize), "Ptr")
			if ((this.Socket := DllCall("Ws2_32\socket", "Int", NumGet(Next + 0, 4, "Int")
				, "Int", this.SocketType, "Int", this.ProtocolId, "UInt")) != -1)
			{
				if (DllCall("Ws2_32\bind", "UInt", this.Socket, "Ptr", ai_addr
					, "UInt", ai_addrlen, "Int") == 0)
				{
					DllCall("Ws2_32\freeaddrinfo", "Ptr", pAddrInfo)
					return this.EventProcRegister(Socket.FD_READ | Socket.FD_ACCEPT | Socket.FD_CLOSE)
				}
				this.Disconnect()
			}
			Next := NumGet(Next + 0, 16 + (3 * A_PtrSize), "Ptr")
		}
		throw Error("Error binding")
	}

	Listen(backlog := 32)
	{
		return DllCall("Ws2_32\listen", "UInt", this.Socket, "Int", backlog) == 0
	}

	Accept()
	{
		if ((Accepted := DllCall("Ws2_32\accept", "UInt", this.Socket, "Ptr", 0, "Ptr", 0, "Ptr")) == -1)
			throw Error("Error calling accept",, this.GetLastError())
		Sock := Socket(Accepted)
		Sock.ProtocolId := this.ProtocolId
		Sock.SocketType := this.SocketType
		Sock.EventProcRegister(Socket.FD_READ | Socket.FD_CLOSE)
		return Sock
	}

	Disconnect()
	{
		; Return 0 if not connected
		if (this.Socket == -1)
			return 0

		; Unregister the socket event handler and close the socket
		this.EventProcUnregister()
		this._PendLen := 0 ; 票 #156：上一连接扣留的残尾绝不带进下一连接
		if (DllCall("Ws2_32\closesocket", "UInt", this.Socket, "Int") == -1)
			throw Error("Error closing socket",, this.GetLastError())
		this.Socket := -1
		return 1
	}

	MsgSize()
	{
		static FIONREAD := 0x4004667F
		argp := 0 ; v2：对未赋值的变量取 & 会抛 UnsetError，输出参数得先初始化
		if (DllCall("Ws2_32\ioctlsocket", "UInt", this.Socket, "UInt", FIONREAD, "UInt*", &argp) == -1)
			throw Error("Error calling ioctlsocket",, this.GetLastError())
		return argp
	}

	Send(pBuffer, BufSize, Flags := 0)
	{
		; 票 #178：TCP send 合法短写（发送缓冲满等）→ 循环补发到齐才返回累计字节。
		; 纯逻辑在 SocketSendAll（harness 用假 sender 钉），这里只接真 socket。
		base := IsObject(pBuffer) ? pBuffer.Ptr : pBuffer
		sock := this.Socket
		res := SocketSendAll(BufSize, (offset, len) => DllCall("Ws2_32\send"
			, "UInt", sock, "Ptr", base + offset, "Int", len, "Int", Flags), &shortWrites)
		this.ShortWrites += shortWrites
		if (res.failed)
			throw Error("Error calling send",, this.GetLastError())
		return res.sent
	}

	SendText(Text, Flags := 0, Encoding := "UTF-8")
	{
		Unit := (Encoding = "UTF-16" || Encoding = "cp1200") ? 2 : 1
		Buf := Buffer(StrPut(Text, Encoding) * Unit, 0)
		Length := StrPut(Text, Buf, Encoding)
		return this.Send(Buf, Length - 1)
	}

	Recv(&Bytes, BufSize := 0, Flags := 0)
	{
		while (!(Length := this.MsgSize()) && Socket.Blocking)
			Sleep(Socket.BlockSleep)
		if !Length
			return 0
		if !BufSize
			BufSize := Length
		Bytes := Buffer(BufSize, 0)
		if ((r := DllCall("Ws2_32\recv", "UInt", this.Socket, "Ptr", Bytes, "Int", BufSize, "Int", Flags)) == -1)
			throw Error("Error calling recv",, this.GetLastError())
		return r
	}

	; ---------------------------------------------------------------------
	;  票 #156：一次 recv 的 chunk 边界可能落在多字节 UTF-8 字符中间。
	;
	;  修复前：每个 chunk 独立 `StrGet(UTF-8)`，残缺的 1–2 个字节被解成 U+FFFD，
	;  静默无日志（120KB 中文实测 3 处损坏）。修复后：把**末尾不完整的那段
	;  序列（1–3 字节）扣留到下一拍**，与下一拍的新字节拼起来再解码 —— 一个字符
	;  永远只在「字节完整」的时候被解码一次。
	;
	;  语义不变（这是硬约束，两个调用方 English取词.ahk:742 / :950 零改动）：
	;    · 仍然只取「当前已到达的字节」，永不等待、永不阻塞；
	;    · 仍然返回「已到达的部分」，不攒整行（攒行是调用方自己干的）；
	;    · 唯一可见差别：末尾那个被切开的字符不再以 U+FFFD 提前出现，而是完整地
	;      出现在下一拍（正文内容因此变正确，总字符数可能比修复前少 1–2 个 FFFD）。
	;  非 UTF-8 编码与 MSG_PEEK 路径原样解码，行为与修复前逐字相同。
	;
	;  `BufSize` 非 0 时**不扣留**：那是 `RecvLine()` 的用法（它先用 MSG_PEEK 量出
	;  「到换行有多少字节」，再按这个精确字节数取走）。在那里扣留尾部会让扣下的
	;  字节从下一次 MSG_PEEK 的视野里消失，行就永远凑不齐。生产代码不走这条路
	;  （`RecvLine` 只被测试用），但仍须不破坏它 —— 所以这条路径保持修复前的解码。
	; ---------------------------------------------------------------------
	RecvText(BufSize := 0, Flags := 0, Encoding := "UTF-8")
	{
		if !(Length := this.Recv(&Bytes, BufSize, Flags))
			return ""

		; MSG_PEEK 不消费字节（RecvLine 用它量到换行的距离）。既然没消费，
		; 就没有「扣留」这回事：原样解码，也不碰 _Pend。
		if (Flags & Socket.MSG_PEEK)
			return StrGet(Bytes, Length, Encoding)

		; 只有 UTF-8 才会「一个字符跨多个字节」；其它编码与修复前一致。
		; BufSize 非 0 = 调用方按精确字节数取（RecvLine），也不扣留，见上。
		if !(Encoding = "UTF-8" || Encoding = "CP_UTF8") || BufSize
			return StrGet(Bytes, Length, Encoding)

		; 上一拍扣留的残尾 + 这一拍，先拼成一整段再判边界 —— 只判新字节是不够的，
		; 那个字符的两个碎片分别在两拍里。
		if (this._PendLen)
		{
			Joined := Buffer(this._PendLen + Length, 0)
			Socket.CopyBytes(Joined, 0, this._Pend, 0, this._PendLen)
			Socket.CopyBytes(Joined, this._PendLen, Bytes, 0, Length)
			Buf := Joined
			Length += this._PendLen
			this._PendLen := 0
		}
		else
			Buf := Bytes

		Safe := Socket.Utf8SafeLength(Buf, Length)
		if (Safe < Length)
		{
			Socket.CopyBytes(this._Pend, 0, Buf, Safe, Length - Safe)
			this._PendLen := Length - Safe
		}
		if !Safe
			return ""            ; 整段就是一个不完整字符：扣下，等下一拍
		return StrGet(Buf, Safe, Encoding)
	}

	; ---------------------------------------------------------------------
	;  票 #156：UTF-8 边界判定 —— 「从哪个下标起都不完整」。
	;
	;  只看字节高位模式，不解析字符（RFC 3629 §3）：
	;    0xxxxxxx  1 字节，ASCII
	;    110xxxxx  2 字节首字节
	;    1110xxxx  3 字节首字节（中文/日文/韩文常用区在这里）
	;    11110xxx  4 字节首字节（emoji、增补平面）
	;    10xxxxxx  续字节 —— 永远不是字符起点
	;
	;  做法：末字节若是续字节就往前退（最多 3 步）找首字节，再看这个首字节
	;  「声明的长度」比「它到末尾实际还剩的字节数」大多少：大 = 尾部不完整。
	;  返回安全解码长度（= 末尾不完整序列的起点）；整段都完整时返回 Length。
	; ---------------------------------------------------------------------
	static Utf8SafeLength(Buf, Length)
	{
		if (Length <= 0)
			return 0
		i := Length - 1                      ; 末字节下标
		j := i
		while (j >= 0 && (NumGet(Buf, j, "UChar") & 0xC0) = 0x80)
		{
			j--
			if (i - j > 3)                   ; 连续续字节 > 3 个 = 数据本身不合法
				return Length
		}
		if (j < 0)
			return Length                    ; 整段都是续字节：无从判断，交给 StrGet
		Lead := NumGet(Buf, j, "UChar")
		if (Lead < 0x80)
			return Length                    ; 末尾是 ASCII（它后面的续字节本就是脏数据）
		if ((Lead & 0xE0) = 0xC0)
			Need := 2
		else if ((Lead & 0xF0) = 0xE0)
			Need := 3
		else if ((Lead & 0xF8) = 0xF0)
			Need := 4
		else
			return Length                    ; 非法首字节：不猜，交给 StrGet
		return (i - j + 1 < Need) ? j : Length
	}

	; 字节搬运（只在扣留/拼接残尾时用，1–3 字节 + 一拍的数据）
	static CopyBytes(Dst, DstOff, Src, SrcOff, N)
	{
		if (N > 0)
			DllCall("RtlMoveMemory", "Ptr", Dst.Ptr + DstOff
				, "Ptr", Src.Ptr + SrcOff, "UPtr", N)
	}

	RecvLine(BufSize := 0, Flags := 0, Encoding := "UTF-8", KeepEnd := False)
	{
		while !(i := InStr(this.RecvText(BufSize, Flags | Socket.MSG_PEEK, Encoding), "`n"))
		{
			if !Socket.Blocking
				return ""
			Sleep(Socket.BlockSleep)
		}
		if KeepEnd
			return this.RecvText(i, Flags, Encoding)
		else
			return RTrim(this.RecvText(i, Flags, Encoding), "`r`n")
	}

	GetAddrInfo(Address)
	{
		; TODO: Use GetAddrInfoW
		Host := Address[1], Port := Address[2]
		Hints := Buffer(16 + (4 * A_PtrSize), 0)
		NumPut("Int", this.SocketType, Hints, 8)
		NumPut("Int", this.ProtocolId, Hints, 12)
		Result := 0 ; v2：对未赋值的变量取 & 会抛 UnsetError，输出参数得先初始化
		if (WsaErr := DllCall("Ws2_32\getaddrinfo", "AStr", Host, "AStr", Port, "Ptr", Hints, "Ptr*", &Result))
			throw Error("Error calling GetAddrInfo",, WsaErr)
		return Result
	}

	OnMessage(wParam, lParam, Msg, hWnd)
	{
		Critical
		if (Msg != Socket.WM_SOCKET || wParam != this.Socket)
			return
		; 票 #185（A10）：事件位**独立**判定（判据 = 本文件底部的 SocketEventRoute，
		; harness 片 40 钉）。旧 else-if 链在 FD_READ|FD_CLOSE 合并投递时只处理头一个位，
		; FD_CLOSE 被吞 —— 数据读到了、断连却迟迟不报（可能直到下一次发送失败或
		; 探测超时才有机会）。现在按 recv → accept → close 的次序独立消费：
		; 先把数据读干净（断开前最后几行回话不能丢），再走断连清理。
		ev := SocketEventRoute(lParam, Socket.FD_READ, Socket.FD_ACCEPT, Socket.FD_CLOSE)
		if (ev.recv)
			this.onRecv()
		if (ev.accept)
			this.onAccept()
		if (ev.close)
			this.EventProcUnregister(), this.OnDisconnect()
	}

	EventProcRegister(lEvent)
	{
		this.AsyncSelect(lEvent)
		if !this.Bound
		{
			this.Bound := this.OnMessage.Bind(this)
			OnMessage(Socket.WM_SOCKET, this.Bound)
		}
	}

	EventProcUnregister()
	{
		this.AsyncSelect(0)
		if this.Bound
		{
			OnMessage(Socket.WM_SOCKET, this.Bound, 0) ; v2 的注销形式：第三个参数传 0
			this.Bound := False
		}
	}

	AsyncSelect(lEvent)
	{
		if (DllCall("Ws2_32\WSAAsyncSelect"
			, "UInt", this.Socket      ; s
			, "Ptr", A_ScriptHwnd      ; hWnd
			, "UInt", Socket.WM_SOCKET ; wMsg
			, "UInt", lEvent) == -1)   ; lEvent
			throw Error("Error calling WSAAsyncSelect",, this.GetLastError())
	}

	GetLastError()
	{
		return DllCall("Ws2_32\WSAGetLastError")
	}
}

class SocketTCP extends Socket
{
	SocketType := 1 ; SOCK_STREAM
	ProtocolId := 6 ; IPPROTO_TCP
}

class SocketUDP extends Socket
{
	SocketType := 2  ; SOCK_DGRAM
	ProtocolId := 17 ; IPPROTO_UDP

	SetBroadcast(Enable)
	{
		static SOL_SOCKET := 0xFFFF, SO_BROADCAST := 0x20
		if (DllCall("Ws2_32\setsockopt"
			, "UInt", this.Socket ; SOCKET s
			, "Int", SOL_SOCKET   ; int    level
			, "Int", SO_BROADCAST ; int    optname
			, "UInt*", !!Enable   ; *char  optval
			, "Int", 4) == -1)    ; int    optlen
			throw Error("Error calling setsockopt",, this.GetLastError())
	}
}

; ----------------------------------------------------------------------------
;  票 #178：TCP 短写的补发循环（纯逻辑 —— 真 socket 调用由 Sender 注入）。
;
;  为什么需要：`Ws2_32\send` 在发送缓冲满时**合法地**只写一部分。短写丢掉尾部
;  的 `}` / `\n`，下一条请求就会和它粘成一行；hub 按「一行一条」只回一条
;  `bad json` → 请求数比回话数多一条 → 外壳的位置 FIFO 永久错一格。
;
;  Sender(offset, len) → 本次写出的字节数（<= 0 = 出错，由调用方转成异常）。
;  返回 { sent, failed }；&ShortWrites 累计「一次没写完」的次数。
; ----------------------------------------------------------------------------
SocketSendAll(BufSize, Sender, &ShortWrites) {
	ShortWrites := 0
	sent := 0
	while (sent < BufSize)
	{
		want := BufSize - sent
		got := Sender.Call(sent, want)
		if (got <= 0)
			return { sent: sent, failed: true }
		if (got < want)
			ShortWrites += 1
		sent += got
	}
	return { sent: sent, failed: false }
}

; ----------------------------------------------------------------------------
;  票 #185（A10）：OnMessage 的事件位分发判据（纯逻辑，harness 片 40 钉）。
;  WSAAsyncSelect 会把「数据到达 + 对端断开」**合并**成一条 FD_READ|FD_CLOSE 投递；
;  旧的 else-if 链只处理头一个命中的位，FD_CLOSE 被吞 —— 数据读到了、断连却迟迟
;  不报（直到下一次发送失败或探测超时才有机会，外壳的判死/托盘全被拖住）。
;  返回三位的独立布尔；调用方按 recv → accept → close 的次序消费：先把数据读干净，
;  再走断连清理（断开前最后几行回话不能丢）。
;  **必须住在 Socket.ahk**：本文件被 tests/recv_utf8_probe.ahk 等探针单独
;  #Include（不带 lib\Logic.ahk），OnMessage 引用的每个名字都得随文件自带。
; ----------------------------------------------------------------------------
SocketEventRoute(lParam, fdRead, fdAccept, fdClose) {
	return { recv: (lParam & fdRead) != 0
	       , accept: (lParam & fdAccept) != 0
	       , close: (lParam & fdClose) != 0 }
}
