# 发版流程（Release Process）

> 适用范围：RandomNamePicker 的正式发版（构建 → 打包 → 发布 → 验证 → 归档）。
> 资产目录为 `release/`，每版一个子目录。

---

## 0. 名词与目录约定

| 名称 | 路径 / 形态 | 说明 |
|---|---|---|
| 版本目录 | `release/v{M}.{N}[.{X}]/` | 每版一个：放该版 `1.iss` 与打包出的安装器 |
| 安装器 | `release/v{ver}/NamePicker.exe` | Inno Setup 编译产物（`OutputBaseFilename=NamePicker`） |
| 更新包 | `app.zip` | 用户端 `helper.exe` 自动更新用的包，**顶层直放** |
| 资产备份 | `release/_assets_backup/v{ver}/` | 该版 Release 的资产留档（`app.zip` + `NamePicker.exe`） |
| 构建输出 | `dist/RandomNamePicker/` | `build.py` 的产物（`dist/`、`release/` 均已被 `.gitignore` 忽略） |

**版本号规则**：`v{M}.{N}` 或 `v{M}.{N}.{X}`，`X` 为单字母 `a`–`z`；
`z` 用尽后进位到 `{N+1}`。比较逻辑见 `helper.py` 的 `_parse_version` / `_version_gt`
（先比 M，再比 N，最后比字母；空补丁 < `a` < … < `z`）。

> 历史说明：该目录原名 `realease/`（拼写笔误），已改名为 `release/`，并同步修正了
> 三个 `1.iss` 的 `OutputDir`。`.trae/documents/` 下的历史计划文档仍保留旧拼写，
> 那是当时执行记录，不必回改。

---

## 1. 发版前：定版本号

| 改动性质 | 版本号 |
|---|---|
| 主程序（`app.py`）功能 / 界面 / 数据格式变化，或需强制用户升级 | `N+1`（如 `v2.0` → `v2.1`） |
| 仅修 `helper.py` 更新链路、打包脚本、文案等小补丁 | `X` 顺延（如 `v2.1` → `v2.1.a`） |
| `z` 已用尽 | 进位：`v2.1.z` → `v2.2` |

**版本号只增不减**，且**不要移动已推送的 tag**（`releases.atom` 按时间排序，`helper`
取版本号最大者；乱序 tag 虽能被正确识别，但会让排查变困难）。

---

## 2. 六处一致性清单（发版最容易出错的地方）

同一个版本号必须在以下 **6 处**完全一致：

| # | 位置 | 值示例 |
|---|---|---|
| 1 | `helper.py` 的 `LOCAL_VERSION` | `v2.1` |
| 2 | git tag | `v2.1` |
| 3 | GitHub Release 的 tag | `v2.1` |
| 4 | 资产目录名 | `release/v2.1/` |
| 5 | `1.iss` 的 `MyAppVersion` | `v2.1` |
| 6 | `1.iss` 的 `OutputDir` | `...\release\v2.1` |

核对命令：

```powershell
# 1) 代码里的本地版本
Select-String -Path helper.py -Pattern "^LOCAL_VERSION"
# 2) 资产目录
Get-ChildItem release -Directory | Select-Object Name
# 3) 本版 1.iss 的两处
Select-String -Path release\v2.1\1.iss -Pattern "MyAppVersion|OutputDir"
# 4) 远端已有 tag（确认没重名）
git ls-remote --tags origin
```

> 第 1 项最关键：`LOCAL_VERSION` 不推进，老用户永远收不到更新；
> 推进过度则用户会被"降级式"更新误导。

---

## 3. 操作步骤

### Step 0 · 前置检查
- 工作区干净（`git status`），或确认待提交内容就是本次要发的改动；
- `python -m py_compile app.py helper.py build.py versiontest.py` 通过；
- `python versiontest.py` 通过（版本比较 9/9、能取到远端最新 tag）。

### Step 1 · 改 `LOCAL_VERSION`
`helper.py` 顶部（当前 `LOCAL_VERSION = 'v1.5.a'`）改为本版版本号。

### Step 2 · 构建
```powershell
python build.py                 # 编译 helper.exe + RandomNamePicker.exe（退出码应为 0）
```
验收：`dist/RandomNamePicker/` 下同时存在 `helper.exe`、`RandomNamePicker.exe`、
依赖目录（`webview/`、`clr_loader/`、`pythonnet/` …）与 `data/`。

> `build.py` 三个模式：默认全部 / `--helper` 只编更新器 / `--main` 只编主程序；
> `--all` 不能与 `--helper`/`--main` 同用。

### Step 3 · 组装资产目录
```powershell
New-Item -ItemType Directory -Force release\v2.1
Copy-Item release\v2.0\1.iss release\v2.1\1.iss     # 以上一版为模板
```
然后按 §5 修改 `1.iss`（**每版固定改 3 处**）。

### Step 4 · 打 `app.zip`（用户更新包）
要求（与 `helper.py` 的 `safe_extract` 强相关）：

- **顶层直放**：zip 根下直接是 `helper.exe`、`RandomNamePicker.exe`、各依赖目录，
  **不能有包装目录**（否则内容会被平铺到安装目录，造成嵌套错位）；
- 必须包含 `helper.exe` 与 `RandomNamePicker.exe`；
- **不要包含 `data/`**：`KEEP_ITEMS` 会跳过它，但仍不应把用户数据放进包；
- 与安装器**使用同一份 `dist/RandomNamePicker/`**，避免两者内容不一致。

```powershell
# 在 dist\RandomNamePicker 目录内部打包，保证条目位于根层
Compress-Archive -Path dist\RandomNamePicker\* -DestinationPath release\v2.1\app.zip -Force
```

实测参考（v2.0 资产）：**334 个条目**，根层是 `helper.exe` / `RandomNamePicker.exe` /
各依赖目录，**不含 `data/`**。用 §6 的命令体检。

### Step 5 · 用 `1.iss` 打包安装器
用 Inno Setup 打开 `release/v2.1/1.iss` → **Compile**；或命令行：

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" release\v2.1\1.iss
```
产物：`release/v2.1/NamePicker.exe`。

> 现状：本机未检测到 `ISCC.exe`（Inno Setup 未安装或不在 PATH）。需先安装
> Inno Setup 6，或改用 IDE 手动编译。

### Step 6 · 本地冒烟（发布前最后一关）
1. 把 `app.zip` 解压到空目录 → 双击 `helper.exe`，确认能拉起主程序；
2. 或运行安装器装一次，确认桌面 / 开始菜单快捷方式指向 `helper.exe`，点名功能正常。

### Step 7 · 提交并打 tag
```powershell
git add app.py helper.py build.py versiontest.py   # 按实际改动挑，不要 git add -A
git commit -m "release: v2.1"
git tag -a v2.1 -m "v2.1"
git push origin main
git push origin v2.1
```

### Step 8 · 建 Release 并上传资产
以 tag `v2.1` 建 Release（标题写版本号，说明写变更点），上传两个资产：

| 资产 | 来源 |
|---|---|
| `app.zip` | `release/v2.1/app.zip`（**名称必须精确为 `app.zip`** —— `helper.py` 按约定名拼直链） |
| `NamePicker.exe` | `release/v2.1/NamePicker.exe` |

> GitHub 自动生成的 `Source code (zip)` 与我们上传的 `app.zip` 是两个不同文件，
> `helper` 只认 `app.zip`。

### Step 9 · 发布后验证（4 项，缺一不可）
1. `releases.atom` 已含新 tag，且 `python versiontest.py` 显示的"最新版本"= 本版；
2. 直链可下载且大小正确：
   `curl.exe -sIL https://github.com/BaiZiDog/RandomNamePicker/releases/download/v2.1/app.zip`（末尾 200）；
3. 用**旧版**（把 `LOCAL_VERSION` 临时调低）跑 `helper.exe`，确认能检测并完成更新；
4. 用**已是最新**的本地版本跑 `helper.exe`，确认提示"已是最新版本"并直接启动主程序。

### Step 10 · 归档
把 Release 的两个资产复制回 `release/_assets_backup/v2.1/`，并保存 Release 说明文本，
便于日后回退与排查。

---

## 4. `app.zip` 的构成（与更新链路强耦合）

| 项 | 要求 | 原因 |
|---|---|---|
| 根层结构 | 无包装目录 | `safe_extract` 按相对路径落位，包装目录会被平铺 |
| `helper.exe` | 必须有 | 更新器本体，缺失则下次无法更新 |
| `RandomNamePicker.exe` | 必须有 | 主程序 |
| 依赖目录（`webview/` 等） | 必须有 | 运行时依赖 |
| `data/` | **不要有** | 用户数据；`KEEP_ITEMS` 会跳过，但仍不应入包 |
| 日志 `*.log` | 不要有 | 同上（保留白名单会跳过） |

---

## 5. `1.iss` 每版固定要改的 3 处

| 位置 | 说明 | 当前各版实际写法 |
|---|---|---|
| `#define MyAppVersion` | 本版版本号 | `"v2.0"` → 改成新版号 |
| `OutputDir=` | 输出到本版资产目录 | `D:\PythonProject\RandomNamePicker\release\v2.0` → 改成 `v2.1` |
| `[Files] Source=` | 指向 `dist` 产物 | 见下方表格，**必须与 `dist\RandomNamePicker\` 对齐** |

`[Files] Source` 的现状（只有 v1.5 与当前 `build.py` 的输出一致）：

| 版本 | Source 路径 | 状态 |
|---|---|---|
| v1.5 | `dist\RandomNamePicker\{#MyAppExeName}` + `\example.txt` / `\file.txt` / `\_internal\*` | 布局为旧的 onedir（含 `_internal`），与当前构建不符 |
| v1.5-fix | `dist\RandomNamePicker-1.5---fix\dist\RandomNamePicker\*` | 双层嵌套的历史路径 |
| v2.0 | `dist\RandomNamePicker-202709282155\*` | **该目录已不存在** |

**当前构建的正确写法**（`build.py` 用 `--contents-directory .`，依赖与 exe 平铺）：

```
[Files]
Source: "D:\PythonProject\RandomNamePicker\dist\RandomNamePicker\helper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "D:\PythonProject\RandomNamePicker\dist\RandomNamePicker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
```

**另外两个待修正项（强烈建议尽早处理）**：

1. **`AppId` 三个版本各不相同** → Inno 视为三个不同程序，安装新版**不会覆盖旧版**，
   会并存多份安装（且卸载/升级路径混乱）：
   - v1.5 `{9A25ACD3-B42A-4020-9A19-5D9027381AC3}`
   - v1.5-fix `{8679EAB0-EF54-4112-B0EC-9F0176E1E718}`
   - v2.0 `{5544B66A-CA07-4C55-9406-40B217CCA383}`
   　→ 应固定为同一个 `AppId`（首次正式发布时生成的那个），此后各版沿用。
2. **`DefaultDirName` 被写死盘符**：v1.5-fix 与 v2.0 为 `D:\Program Files\{#MyAppName}`，
   无 D 盘的机器会失败；应改用 `{autopf}\{#MyAppName}`（v1.5 已是这种写法）。

> `MyAppExeName` 三版均为 `helper.exe` ✅ —— 必须保持：安装器的启动项要是更新器，
> 否则用户绕过更新链路直接进主程序。

---

## 6. 常用校验命令

```powershell
# app.zip 结构体检（顶层直放 + 关键文件 + 是否含 data/）
.venv\Scripts\python.exe -c "
import zipfile
z = zipfile.ZipFile(r'release\v2.1\app.zip'); n = z.namelist()
print('条目数:', len(n))
print('根层条目(前 8):', n[:8])
print('含 helper.exe:', 'helper.exe' in n, '| 含主程序:', 'RandomNamePicker.exe' in n)
print('含 data/:', any(x.startswith('data/') for x in n), '（应为 False）')
print('是否疑似有包装目录:', all(x.startswith(n[0].split('/')[0] + '/') for x in n if '/' in x))
"
```

---

## 7. 回滚与撤回

| 情形 | 处理 |
|---|---|
| 发布后发现严重问题，**尚无用户拉到新包** | 可删 Release 与 tag（`gh release delete <tag> --yes`；`git push origin :refs/tags/<tag>`），修好后**用同一版本号**重发 |
| 已有用户拉到新包 | **不要删 tag/Release**（用户可能正在更新，删除会让直链 404、`atom` 取到旧版导致判断异常）；正确做法是**立即发 `X+1` 修复版** |
| 只想撤回安装器 | 删除 Release 上的 `NamePicker.exe` 资产，保留 `app.zip`（更新链路不依赖安装器） |

> 已发布过的版本号不要复用。

---

## 8. 其他注意事项

- `release/` 已加入 `.gitignore`，其中的安装器与 `app.zip`（几十 MB）不会被提交。
  **副作用**：`1.iss` 也一并变成不被跟踪 —— 打包脚本是发版配方，丢了就得重写。
  若要版本化，建议二选一：
  ① 把忽略规则收窄为只忽略大文件（`/release/**/*.exe`、`/release/**/*.zip`），
  ② 或在仓库内另存一份 `1.iss` 模板（例如 `.trae/documents/` 下）。
- `dist/`、`build/` 已被 `.gitignore` 忽略，构建产物不会误入仓库。
- 每次发版必须同步推进 `helper.py` 的 `LOCAL_VERSION`，否则用户端检测不到新版本。
- 只更新 `app.zip` 而忘记重新编译安装器 → 新装用户拿到旧代码（两者必须同源同版）。
- `data/` 不进 `app.zip`，新装用户的 `data/` 由主程序启动时自动创建（`_ensure_data_dir()`）。
