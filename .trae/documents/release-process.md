# 已迁移：发版流程 → `RELEASE.md`

本文件内容已整理并迁移到仓库根目录的 **[RELEASE.md](../../RELEASE.md)**（工作流结构：六阶段 → 通过条件 → 可勾选清单 → 回滚策略）。

为避免同一份发版流程出现两处、日后各自漂移，这里不再保留正文。

相关历史决策（已并入 `RELEASE.md`）：
- 资产目录 `realease/` → `release/`（拼写修正），三个 `1.iss` 的 `OutputDir` 已同步；
- `.gitignore` 只忽略 `release/**/*.exe`、`release/**/*.zip`，`1.iss` 与 `*-release.json` 纳入版本管理；
- `AppId` 统一为 `{5544B66A-CA07-4C55-9406-40B217CCA383}`（以 v2.0 已发布安装为准）。
