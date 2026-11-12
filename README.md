# 英语取词 · 框架版本

一个本地优先的英语取词与速记工具，由 Python 数据中枢、AutoHotkey v2 桌面外壳、浏览器扩展和本地 Web 界面组成。

本仓库为公开框架版本，只包含源码与运行配置。词库数据库、个人笔记、测试数据、过程文档、日志与截图均不包含在内，使用前需自行准备数据目录。

## 结构

- `tools/英语取词/*.py` — 数据中枢、采集、ASR、OCR、存储等模块
- `tools/英语取词/lib/` — AutoHotkey 桌面外壳组件
- `tools/英语取词/web/` — 本地 Web 界面
- `tools/英语取词/extension/` — 浏览器扩展
- `tools/英语取词/assets/` — 托盘图标与生成脚本

## 运行

```bash
cd tools/英语取词
pip install -r requirements.txt
python hub.py
```

`config.ini` 中的路径与热键已改为通用默认值，请按本机环境调整。需要 AutoHotkey v2 才能启动桌面外壳。
