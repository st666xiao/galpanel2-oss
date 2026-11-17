# 蔵書帖 · GalPanel2

GalPanel2 is a Windows desktop library manager for locally owned visual novels and games, with metadata binding and safe JSON CLI / MCP control for AI agents.

蔵書帖用海报墙管理本地 galgame 和其他游戏：扫描多个目录、整理作品资料，记录分类和游玩状态。**仅支持 Windows + Python 3.13**。

本软件不包含任何游戏内容，只管理用户本地已有的文件。涉及成人内容的元数据来自公开站点。

截图待补：`docs/screenshot.png`（当前未提供图片）。

## 功能

- PySide6 海报墙、作品详情、多目录扫描、文件可用性检查。
- VNDB / DLsite / Bangumi / Steam / F95 搜索、资料绑定、品牌与标签管理。
- 分类、游玩状态、自定义简介、目录整理与冷藏搬动。
- 2DFan 存档导入与备份；AI 识别流水线。
- JSON CLI、MCP stdio：读取库、检索候选、预览和应用资料绑定。

## 安装与运行

在 Windows PowerShell 中，从源码目录执行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python -m pip install -e . --no-deps
.\.venv\Scripts\python -m galpanel2
```

也可激活环境后运行 `galpanel2` 或双击 `run_galpanel2.bat`。

## 数据位置

- 库是用户选择的 `.galdb` SQLite 文件；默认新建位置为 `D:\GalPanel2\library.galdb`（存在 D 盘时），否则为设置目录下的 `library.galdb`。
- 封面在库旁的 `<库名>.galdb.covers/`；写锁文件为 `<库名>.galdb.lock`。
- 设置在 `%APPDATA%\GalPanel2\settings.json`，Bangumi token 在同目录 `bangumi_token.txt`；可用 `GALPANEL2_SETTINGS_DIR` 覆盖目录，`GALPANEL2_DEFAULT_LIBRARY` 覆盖默认库路径。
- agent 写入前在库旁创建 `<库>.agent-<UTC>.bak`；请与库及封面一起妥善备份。

## 联网行为

仅在用户显式操作时联网，例如资料搜索、详情获取、绑定封面下载、存档查找和 AI 识别。普通本地浏览不需要网络。元数据请求发给对应公开资料站点；AI 识别会把提示中的标题和本地识别线索发给用户配置的服务或命令，请先检查配置。站点可能限频或拒绝访问。

## 配置 AI 识别

没有默认外部工具或默认模型，未配置会明确报错。可设置环境变量：

```powershell
$env:GALPANEL2_AI_BASE_URL="https://api.example.com/v1"
$env:GALPANEL2_AI_MODEL="your-model"
$env:GALPANEL2_AI_API_KEY="你的密钥"
python -m galpanel2.identify --help
```

远程 base_url 必须使用 HTTPS；HTTP 仅允许 localhost、127.0.0.1、[::1]。HTTP 后端使用 `POST {base_url}/chat/completions`。也可将下面 JSON 保存到 `ai.json`，在识别命令中指定 `--ai-config ai.json`：

```json
{"backend":"openai","base_url":"https://api.example.com/v1","model":"your-model","api_key_env":"GALPANEL2_AI_API_KEY"}
```

配置文件只能放密钥环境变量名，不能直接放 key。HTTP 密钥不写日志和识别产物，不跟随重定向。

保留可信外部命令后端，旧配置只有 `argv` 时同样有效：

```json
{"backend":"command","argv":["your-ai-tool","--prompt","{prompt}","--output","{out}"],"mode":"file","timeout":300}
```

`file` 模式读取 `{prompt}` 文件并把 JSON 写到 `{out}`（空文件时可从 stdout 读）；`stdin` 模式从标准输入读提示、向 stdout 输出 JSON。占位符还有 `{dir}`、`{timeout}`。外部命令以当前用户权限运行，日志和凭据处理由该命令负责。

## AI 调控接口

```powershell
python -m galpanel2.agent --library D:\Library\library.galdb list --unbound
python -m galpanel2.mcp --library D:\Library\library.galdb
```

参见 [AI 调控与资料绑定指南](docs/AI-CONTROL.md)：接入 Claude Code、Codex 和通用 MCP 客户端，以及完整的先预览后写入流程。

## 开发与测试

```powershell
python -m pip install -r requirements-dev.txt
$env:QT_QPA_PLATFORM="offscreen"
python -m pytest -q
```

测试不得联网或触碰真实库。详见 [贡献指南](CONTRIBUTING.md) 和 [更新记录](CHANGELOG.md)。

## 许可

- 项目代码：[MIT](LICENSE)，版权人 GalPanel2 contributors。
- 界面素材：由项目作者用 AI 自行生成，随项目以 MIT 许可发布，生成记录见 [assets/SOURCES.md](assets/SOURCES.md)。
- 内置字体：各自采用 SIL Open Font License（OFL），原许可保存在 [字体目录](galpanel2/resources/fonts)。
- 站点元数据、封面及用户游戏文件不因本项目而获得额外许可。
