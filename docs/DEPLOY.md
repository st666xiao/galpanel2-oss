# GalPanel2 部署指南

推荐普通用户下载 **Setup 安装包**。安装包和便携包都自带运行环境，不用安装 Python。系统要求：Windows 10/11 64 位（x64）；源码运行需要 Python 3.13。界面以中文为主。

## 方式 A：安装包（推荐）

1. 打开 [GitHub Releases](https://github.com/st666xiao/galpanel2-oss/releases/latest)，在 Assets 下载 `GalPanel2-Setup-<版本>.exe`（例如 `GalPanel2-Setup-2.1.0.exe`；对应版本发布后可用）。
2. 双击安装，默认位置是 `%LOCALAPPDATA%\Programs\GalPanel2`，当前用户安装，无需管理员权限。按向导继续，可勾选桌面快捷方式；开始菜单快捷方式会自动创建。
3. 完成时可勾选 **Launch GalPanel2** 立即启动，以后从开始菜单或桌面打开。

若 Windows SmartScreen 提示“Windows 已保护你的电脑”，先确认文件来自上述官方仓库的 Release，再按需要选择“更多信息”→“仍要运行”。单位策略可能禁止继续；请联系设备管理员，不必关闭系统防护。Release 附带 `SHA256SUMS.txt`，可用 `Get-FileHash .\GalPanel2-Setup-2.1.0.exe -Algorithm SHA256` 对照同名条目；一行安装方式会自动校验。

**升级**：先按下文备份库，关闭 GUI、agent/MCP，再下载新版 Setup 覆盖安装到原位置。安装程序只管理程序文件，升级和卸载不会删除程序目录外的用户库或 `%APPDATA%\GalPanel2`。请始终把库放在程序目录外。

**卸载**：关闭程序，在 Windows“设置 → 应用”中卸载 GalPanel2，或运行安装目录的 `unins000.exe`。库、封面、游戏与设置保留；如需删除设置，请先确认其中没有默认库和凭据，再自行处理 `%APPDATA%\GalPanel2`。不要把这个目录当成纯缓存。

## 方式 B：一行 PowerShell 命令

打开普通 **Windows PowerShell**（5.1 或更新版本，不必管理员），执行：

```powershell
irm https://raw.githubusercontent.com/st666xiao/galpanel2-oss/main/install.ps1 | iex
```

脚本查询 GitHub 最新正式 Release，下载对应 Setup 与 `SHA256SUMS.txt`，校验成功后静默安装到 `%LOCALAPPDATA%\Programs\GalPanel2`。不会自动启动 GUI，结束后打印程序、agent 和 MCP 的完整路径。重复执行可升级到最新正式版；升级前同样先退出程序并备份。

需要联网访问 GitHub API、raw.githubusercontent.com 及 Release 下载服务。没有正式发行版、附件不全、API 限频、校验失败或安装失败都会给出错误并停止，不回退到未校验安装。校验值与安装包来自同一 Release，用于检查下载完整性。

可先下载脚本检查再执行；如本机执行策略限制脚本文件，按设备策略处理，或选双击安装方式。

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/st666xiao/galpanel2-oss/main/install.ps1 -OutFile install.ps1 -UseBasicParsing
.\install.ps1
```

## 方式 C：便携版

在 [Releases](https://github.com/st666xiao/galpanel2-oss/releases/latest) 下载 `GalPanel2-<版本>-win64.zip`，对照同一发行版的 SHA256SUMS，**完整解压**到可写目录（如 `D:\Apps`）。打开解压得到的 `GalPanel2` 文件夹，双击 `GalPanel2.exe`。保留 `_internal` 和两个控制台 EXE，不要在压缩包内运行，不要只复制主 EXE。避免 Program Files 等需要特殊权限的位置。

也可以用脚本自动下载、校验、解压到指定的空目录：

```powershell
# 使用上节已下载的脚本；带参数时不要直接在 iex 后面加 -Portable。
.\install.ps1 -Portable -Directory D:\Apps\GalPanel2
```

省略 `-Directory` 时，默认是 `%LOCALAPPDATA%\Programs\GalPanel2-Portable`。脚本拒绝向非空目录解压；便携版升级请先备份库和原程序目录，再手动将新 zip 的程序文件覆盖到原位置。不要把用户库放进程序目录。便携版同样使用 `%APPDATA%\GalPanel2` 存设置，并非设置随身版。

需要快捷方式时，右键 `GalPanel2.exe` →“发送到 → 桌面快捷方式”（Windows 11 可能先点“显示更多选项”）。卸载便携版只需删除程序目录；设置与库另行保留。

## 方式 D：源码部署

适合开发者或想跟随最新代码的用户。在 Windows 安装 [Python 3.13](https://www.python.org/downloads/windows/)（勾选 **Add Python to PATH**）与 [Git](https://git-scm.com/downloads/win)，然后在 PowerShell 中执行：

```powershell
git clone https://github.com/st666xiao/galpanel2-oss.git
cd galpanel2-oss
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m galpanel2
```

不需要激活 venv。项目当前要求 Python `>=3.13,<3.14`，不要直接换成最新版 Python。更新前退出程序并备份库，然后：

```powershell
git pull --ff-only
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e . --no-deps
.\.venv\Scripts\python.exe -m galpanel2
```

有本地代码修改时先自行保存、处理冲突，不要强制覆盖。源码卸载是删除源码和 venv；外部数据仍需单独保留。

## 第一次启动与备份

1. 标题页点「开始使用」会在默认位置创建库：存在 D 盘时为 `D:\GalPanel2\library.galdb`，否则在 `%APPDATA%\GalPanel2`（环境变量可覆盖）。已有库点「打开已有的库」。如要自己指定新库位置，进入界面后在 ⚙ 菜单选「新建库…」，建议放数据盘，例如 `D:\Library\library.galdb`。
2. 库不要放程序目录；避免把正在使用的库放在云同步目录并在多台电脑同时打开。写锁是本机写者保护，不是跨电脑协作同步机制。
3. 在「出所」点「添加目录」，选游戏目录，再点「扫描这个目录」。审阅候选后点「入库 N 部」。打开作品点「找资料」，检查候选版本后再绑定。
4. **关闭 GUI 和所有写入工具后**备份 `library.galdb`、旁边的 `library.galdb.covers` 整个文件夹及自动生成的 `.bak` 文件。自动备份由升级库、整理、agent 实际写入等操作触发，并不是每次退出都会生成。需要保留批量候选时也备份 `<库>.m2queue.json`。
5. 设置是 `%APPDATA%\GalPanel2\settings.json`；同目录的 `bangumi_token.txt` 含凭据，不要公开。`GALPANEL2_SETTINGS_DIR` 可覆盖设置目录，`GALPANEL2_DEFAULT_LIBRARY` 可覆盖默认库路径。游戏及存档不在库文件中，需要另外备份。

## 方式 E：给 AI 接入

先在 GUI 创建或打开库，再将示例 `D:\Library\library.galdb` 换成实际路径。安装后的固定默认入口为：

```text
%LOCALAPPDATA%\Programs\GalPanel2\galpanel2-agent.exe
%LOCALAPPDATA%\Programs\GalPanel2\galpanel2-mcp.exe
```

PowerShell 用 `$env:LOCALAPPDATA` 展开环境变量：

```powershell
& "$env:LOCALAPPDATA\Programs\GalPanel2\galpanel2-agent.exe" --library D:\Library\library.galdb status
```

便携版替换成解压后的实际路径（例如 `D:\Apps\GalPanel2\galpanel2-agent.exe` / `galpanel2-mcp.exe`）。源码版使用虚拟环境的绝对路径：

```powershell
& 'D:\Tools\galpanel2-oss\.venv\Scripts\python.exe' -m galpanel2.agent --library D:\Library\library.galdb status
& 'D:\Tools\galpanel2-oss\.venv\Scripts\python.exe' -m galpanel2.mcp --library D:\Library\library.galdb --lock-library
```

### Claude Code

在 PowerShell 中注册安装版 stdio MCP，之后重启客户端并检查连接：

```powershell
claude mcp add --transport stdio galpanel2 -- "$env:LOCALAPPDATA\Programs\GalPanel2\galpanel2-mcp.exe" --library D:\Library\library.galdb --lock-library
```

便携版改 EXE 路径即可；源码版为：

```powershell
claude mcp add --transport stdio galpanel2 -- 'D:\Tools\galpanel2-oss\.venv\Scripts\python.exe' -m galpanel2.mcp --library D:\Library\library.galdb --lock-library
```

参考 [Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp)。

### Codex

在 `%USERPROFILE%\.codex\config.toml` 中加入以下配置。**command 要用完整绝对路径**，将 `YOUR_NAME` 换成实际 Windows 用户目录；TOML 单引号字符串保留反斜杠，不能把 `%LOCALAPPDATA%` 原样当作可展开的路径。

```toml
[mcp_servers.galpanel2]
command = 'C:\Users\YOUR_NAME\AppData\Local\Programs\GalPanel2\galpanel2-mcp.exe'
args = ["--library", 'D:\Library\library.galdb', "--lock-library"]
```

便携版将 command 改成解压目录的 `galpanel2-mcp.exe`。源码版用以下配置替换同名段落：

```toml
[mcp_servers.galpanel2]
command = 'D:\Tools\galpanel2-oss\.venv\Scripts\python.exe'
args = ["-m", "galpanel2.mcp", "--library", 'D:\Library\library.galdb', "--lock-library"]
```

参考 [Codex 官方 MCP 文档](https://developers.openai.com/codex/mcp)。GUI 开着时 agent/MCP 可以读和预览；实际写入前关闭 GUI。不要删除锁文件。完整命令、revision、dry-run、应用和备份规则见 [AI 调控指南](AI-CONTROL.md)。

## 常见问题

- **缺 DLL / VC 运行库**：先确认完整解压或重新运行安装包，未漏掉 `_internal`。如错误明确指向 Microsoft Visual C++ 运行库，可尝试安装微软官方 [最新 x64 VC++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist)。不要从不明网站下载单个 DLL。
- **字体不正常**：发行包已包含字体；先尝试重新安装完整包，并在设置中切换主题确认。源码版需完整保留 `galpanel2/resources/fonts`；无法据此保证所有系统字体回退都一致。
- **杀软提示**：先核实下载来源及 SHA256。如文件被隔离，查看安全软件的检测详情，判断后按其申诉流程处理；不要直接关闭防护。哈希匹配不代表一定不会被报毒。
- **库被锁定 / library_locked**：关闭使用同一库的 GUI 或其他写入工具后重试。写锁由进程持有，单独存在 `.lock` 文件不代表锁未释放；不要靠删文件绕过锁。
- **API 限频、下载失败或尚无 Release**：稍后重试，或去 Releases 下载对应完整附件。首发上传完成前一行安装不能成功，不应跳过校验继续安装。
