# 参与开发

使用 Windows 和 Python 3.13。创建虚拟环境后执行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pip install -e . --no-deps
$env:QT_QPA_PLATFORM="offscreen"
python -m pytest -q
```

类 Unix shell 对应为 `QT_QPA_PLATFORM=offscreen python -m pytest -q`，但 Windows 路径和系统功能的测试应在 Windows 验证。

写库必须调用 `galpanel2/core` API，在事务中处理 revision，不在 GUI、agent 或 MCP 层写 SQL。agent 的每次 apply 会话先备份，写库时持有 OS 写锁；不要绕过锁或自动升级真实库。

测试只使用临时目录、虚构数据和可控站点回放，不得联网，不得读写真实库或用户游戏目录。新增行为应有覆盖拒绝路径和数据保持不变的测试。提交前运行相关测试，PR 中说明变更和实际验证结果。

代码遵循 MIT；请保留字体和素材各自的许可说明，不将素材的许可范围当作 MIT。
