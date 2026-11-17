# 更新记录

## 2.1.0

首个公开版本。

- Windows PySide6 海报墙、多目录扫描、SQLite `.galdb` 库与封面管理。
- VNDB、DLsite、Bangumi、Steam、F95 资料检索与绑定。
- 分类、游玩状态、目录整理、冷藏搬动和 2DFan 存档导入。
- AI 识别流水线支持显式配置的 OpenAI 兼容 HTTP 与外部命令。
- 新增 JSON CLI 和标准库 MCP stdio 调控接口，写入默认预览，提供锁检查、revision 检测与备份。

### 破坏性变化

- 默认外部命令已移除，必须显式配置 AI 后端（环境变量或 `--ai-config`）。
- 显式指定的配置文件不存在时会报错，不再回退到默认配置。
- command 配置中也禁止出现 `api_key`、`key`、`token` 字段；密钥通过环境变量传递。

### 发布与演示

- Windows 下载包采用完整目录形式，提供 GUI、JSON agent、MCP 三个入口；解压后无需安装 Python。
- 提供完全虚构的 24 部作品演示库、程序化封面和离线资料回放，以及可手动运行的截图 workflow。
- 中英文说明涵盖首次使用、备份迁移、联网行为与 AI 配置。

### 已知限制

- 运行目标为 Windows；源码要求 Python 3.13。界面主要为中文。
- 资料站可用性、限频、权限与元数据完整性受站点影响；候选需要人工核验，agent 不读取 GUI Bangumi token。
- GUI 打开库时持有写锁，agent 只能读取，需关闭 GUI 后应用修改。
- AI 识别需要显式配置后端，通过源码 CLI 使用；下载包未提供独立 identify 入口。
- 库备份不包含游戏或存档文件；跨盘符迁移后需要重新添加来源、扫描并核对副本。
- MCP 当前提供 stdio tools，不提供 HTTP transport、resources、prompts 或订阅。
