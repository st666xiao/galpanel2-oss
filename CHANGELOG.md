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
