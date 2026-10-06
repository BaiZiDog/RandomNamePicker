# RandomNamePicker 随机点名工具

一个用于课堂随机点名的 Windows 桌面小工具：pywebview 渲染内嵌页面，Python 负责抽取逻辑，
支持**单人点名 / 多人抽取 / 随机分组**，带中文语音播报、名单管理与自动更新。

## 功能

- **单人点名**：三种模式 —— 普通（完全随机）、平衡（不连续抽到同一人）、去重（本轮不重复）
- **多人抽取**：指定人数，随机抽 N 个不重复的名字
- **随机分组**：每组人数可自定义，无重复分配
- **中文朗读**：通过 Web Speech API 播报结果，可静音
- **名单管理**：新建 / 浏览添加 / 编辑 / 删除，多份名单可切换
- **自适应界面**：全屏与窗口化一键切换，字号随窗口与名单长度自动缩放
- **自动更新**：启动器检查 GitHub Release，下载并应用新版本

## 下载与安装

从 [Releases](https://github.com/BaiZiDog/RandomNamePicker/releases) 获取：

| 资产 | 用途 |
|---|---|
| `NamePicker.exe` | 安装程序（Inno Setup），安装后从开始菜单启动 |
| `app.zip` | 免安装包，解压后双击 `helper.exe` 即用 |

> 安装后请从 **`helper.exe`（启动器）** 进入程序：它负责检查更新并在更新后拉起主程序。

## 从源码运行

```powershell
python -m venv .venv
.venv\Scripts\pip install pywebview     # 唯一的第三方运行时依赖
.venv\Scripts\python app.py
```

启动时若提示"数据目录不可用"，请确认程序目录下不存在名为 `data` 的**文件**且当前用户可写。

## 数据与文件

| 路径 | 说明 |
|---|---|
| `data/file.txt` | 名单索引，每行一个名单**文件名** |
| `data/*.txt` | 名单正文，每行一个名字（UTF-8 或 GBK 均可，自动识别） |
| `app.log` / `helper.log` | 运行日志（排查问题时提供） |

- 用户数据只放在 `data/`；更新流程会**保留**该目录，不会覆盖或删除名单。
- 名单文件的编码不受限制：记事本另存为 ANSI（GBK）也能正常读取。

## 构建

```powershell
python -m py_compile app.py helper.py build.py versiontest.py
python versiontest.py        # 版本比较自检 + 检查 GitHub 最新版本
python build.py              # 打包全部；或用 --helper / --main 只打其中一个
```

产物在 `dist/RandomNamePicker/`：`helper.exe`（更新器/入口）、`RandomNamePicker.exe`（主程序）、
运行时依赖与 `data/`。

## 项目结构

```
app.py             主程序：pywebview 窗口 + 内嵌 HTML/JS + 点名逻辑
helper.py          更新器/入口：检查更新、下载、备份、替换、启动主程序
build.py           PyInstaller 打包脚本
versiontest.py     版本比较与数据源自检
data/              用户名单数据（不随更新包分发）
LICENSE            许可证
```

## 更新机制（简介）

`helper.exe` 启动后读取 GitHub 的 `releases.atom` 取版本号最大的 tag，与自身
`LOCAL_VERSION` 比较；有新版本则下载 `app.zip`，**先备份再替换**，失败自动回滚。
版本号格式为 `v{M}.{N}` 或 `v{M}.{N}.{X}`（`X` 为字母 `a`–`z`）。

> 发版步骤属于内部流程，记录在本地 `RELEASE.md`（该文件不纳入版本管理）。

## 许可证

见 [LICENSE](LICENSE)。
