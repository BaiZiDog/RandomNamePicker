# 程序运行逻辑说明

> 适用代码：`app.py`（主程序）、`helper.py`（更新器/入口）、`build.py`（打包）
> 说明：本文件描述**当前代码的实际行为**，不是设计愿景。改动代码时请同步更新本文与
> `helper.py` 顶部的模块 docstring。

---

## 1. 进程与文件拓扑

整个产品只有两个可执行文件，通过「文件 + 命名 Mutex」耦合：

| 角色 | 进程 | 职责 |
|---|---|---|
| 入口 / 更新器 | `helper.exe` | 检查更新 → 下载 → 应用更新 → 启动主程序。是**用户唯一需要双击的文件** |
| 主程序 | `RandomNamePicker.exe` | pywebview 窗口 + 点名逻辑；不感知更新 |

运行期间会出现的目录与文件：

| 路径 | 归属 | 生命周期 |
|---|---|---|
| `data/file.txt` | 用户数据 | 永久（名单索引，一行一个文件名） |
| `data/*.txt` | 用户数据 | 永久（名单正文） |
| `helper.log` | 诊断 | 永久，UTF-8（helper 侧日志） |
| `helper_batch.log` | 诊断 | 永久，GBK（兜底批处理日志，与上一行分开以保持单一编码） |
| `app.log` / `app.log.1` | 诊断 | 永久，UTF-8；超过 1MB 轮转为 `.1` |
| `_backup/` | 更新中间态 | 备份成功创建 → 更新成功即删除 / 更新失败保留供手动还原 |
| `_staging/` | 更新中间态 | 解压后创建 → 替换成功 / 回滚 / 下次启动清理 |
| `*.old` | 更新中间态 | 标记阶段生成 → **下次启动** helper 时删除 |
| `_replace_manifest.txt` | 状态 | `safe_extract()` 写 → `check()` 读后立即删 |
| `_replace_rollback.flag` | 状态 | 兜底批处理回滚时写 → 下次启动清理 |
| `_replace_report.txt` | 报告 | 兜底批处理写 → 保留（不删） |
| `%TEMP%\RandomNamePickerReplace\_replace.bat` | 兜底脚本 | 仅在「内联替换失败」时生成；放在程序目录之外，避免回滚时被自己删掉 |

**保留白名单 `KEEP_ITEMS` —— 所有「改名 / 删除 / 覆盖」操作的唯一来源**：
`data`、`app.log`、`app.log.1`、`helper.log`、`helper_batch.log`、`_replace_report.txt`、
`_backup`、`app.zip`（= `ZIP_ASSET_NAME`，下载名与保留项共用同一常量）。
其余顶层内容一律视为包内容物 —— 标记阶段改名 `*.old`，下次启动按后缀扫描删除
（**不依赖任何状态文件**）。

条目的两种形态互不干扰：**不含分隔符**（如 `app.log`）匹配顶层同名项及其下全部内容；
**含分隔符**（如 `data/notes.txt`）只匹配该路径自身及其子树。
新增需要保护的东西时**只改这一处** —— 历史教训：保留判断曾分散在 `clean_dir`、
`rollback`、兜底批处理、替换规则四处，导致同一文件在不同路径下保留语义不一致。

> 取舍说明：早期版本用 `_marked_manifest.txt` 记录"本程序标记过哪些项"来做精确清理，
> 以免误删用户自有的 `.old`。实测表明该状态文件带来的失败模式（清单缺失）后果可自愈
> （残留会被下一轮 `clean_dir` 的 `os.remove(同名 .old)` 顺带清掉、且不会阻塞标记），
> 收益不足以抵消"多一个状态文件"的成本，故简化为按后缀扫描。约定：**用户数据放 `data/`**。

---

## 2. 链路一：入口 / 更新器（helper.exe）

`main()` 的执行顺序（顺序有依赖，不可调换）：

```
1. _mark_mei()                    在自己的 _MEIxxxx 临时目录写标记文件
2. log_env()                      记录环境（版本/PID/BASE_DIR/Mutex 名）
3. _instance_lock.acquire()       抢 Helper Mutex —— 失败即退出（防重复更新）
4. cleanup_stale_mei()            删除上次残留的 _MEI 临时目录（校验标记 + 年龄门槛）
5. cleanup_stale_old_files()      删除顶层全部 *.old（按后缀扫描，不依赖状态文件）
6. cleanup_stale_replace_files()  清理残留状态文件与 _staging（批处理存在则跳过）
7. atexit.register(release)       兜底释放锁
8. tk.Tk() → HelperApp → mainloop 进入 GUI
9. finally: release + _flush_pending_replace() + ensure_launch()
```

第 4–6 步必须在拿到 Mutex **之后**执行：那时不存在第二个 helper 实例，文件不再被占用。

`HelperApp.__init__` 在 200ms 后拉起后台线程 `check()`；GUI 期间所有界面更新都通过
`_ui()` 投递到主线程，并接管 `WM_DELETE_WINDOW`（见 §4.3）。

---

## 3. 链路二：主程序（RandomNamePicker.exe）

```
Mutex 获取失败 → MessageBoxW("程序已在运行") → sys.exit(0)
Mutex 获取成功 ↓
webview.create_window(html=HTML, js_api=Api()) → webview.start()
finally: 释放 Mutex
```

前后端约定：

| 前端动作 | 后端方法 | 备注 |
|---|---|---|
| 页面就绪 | `get_file_list()` / `get_names()` | 索引 + 正文两次读取 |
| 切换名单 | `set_current_file(path)` | 把选中项移到 `file.txt` 首行 |
| 浏览添加 | `browse_file()` → `add_file(path)` | 复制进 `data/`，同名自动加序号 |
| 新名单 | `ask_new_file_name()` → `create_new_file(name)` | 用系统「另存为」对话框取文件名 |
| 编辑内容 | `edit_file(path)` | `os.startfile` 调系统默认编辑器 |
| 删除 | `remove_file(path)` | **先删文件成功才移除索引**，前端有二次确认 |
| 点名 | `choose_name()` | 结果由后端产生，前端动画只负责表现 |
| 多人 / 分组 | `choose_multi(n)` / `create_groups(sizes)` | 返回 `{ok, ...}` 结构 |
| Esc / 全屏按钮 | `toggle_fullscreen()` | 后端做 350ms 去抖 |

前端所有后端调用统一走 `callApi(name, ...args)`：它会给 Promise 加 `.catch` 并弹提示，
避免后端抛异常时界面「点了没反应」。

**数据一致性约定**：`file.txt` 里**只存文件名**（不存绝对路径），路径统一由
`_resolve_path()` 基于 `DATA_DIR` 解析。读取名单时按 `utf-8-sig` → `gbk` 顺序回退，
兼容用户在记事本里以 ANSI 另存的文件。

---

## 4. 链路三：更新（核心）

### 4.1 状态机

```
[检查版本] ──无新版本──> [直接启动主程序]
     │有新版本
     ▼
[确保主程序退出] ──失败──> [提示用户，直接启动主程序]
     ▼
[下载 app.zip] ──失败──> 直接启动主程序
     ▼
[备份 _backup] ──失败──> 删 _backup，直接启动主程序
     ▼
[标记 *.old] ──失败──> rollback()，直接启动主程序
     ▼
[解压 _staging + 写清单] ──失败──> rollback()，直接启动主程序
     ▼
[内联替换 replace_staged]（每文件重试 REPLACE_MAX_RETRY 次）
     ├─ 全成功 ──> 删 _backup / _staging ──> [更新完成] ──> 启动主程序
     ├─ 部分失败 + 兜底 bat 生成成功 ──> launch_after=False ──> 退出，由 bat 继续
     └─ 部分失败 + 兜底生成失败 ──> rollback() ──> 直接启动主程序
```

### 4.2 四条硬约束（改动时必须保持）

1. **失败必须收敛**：每个失败分支要么 `rollback()`，要么给出明确的失败态；
   绝不允许带着「一半新、一半 `.old`」的目录去启动主程序。
2. **成功必须可验证**：`check()` 里若替换清单为空，判定失败并回滚
   （否则会删掉 `_backup` 与 `_staging`，目录被清空）。
3. **回滚必须还原自身**：`rollback()` 的还原段**不得**跳过 `helper.exe` ——
   标记阶段已把它改名成 `.old`，原路径是空的，写入完全合法（运行中的镜像由
   已映射句柄持有，与路径无关）。跳过它 = 更新器永久消失。
4. **保留只有一份定义**：`KEEP_ITEMS` 是「改名 / 删除 / 覆盖」的唯一白名单 ——
   `clean_dir()`、`rollback()` 的清空段与还原段、兜底批处理的守卫、
   `classify_files()` 全部查它。约定用户数据只放 `data/`；顶层 `*.old` 一律删除
   （按后缀扫描，不依赖状态文件）。代价：用户自行放在程序目录里的 `*.old` 会被清掉。

### 4.3 UI 线程安全

`check()` 跑在后台守护线程，tkinter 的 `after` 只能在主线程调用：
用户在流程中关闭窗口后 `root` 已销毁，再调用会抛
`RuntimeError: main thread is not in main loop`。

因此：
- 所有界面更新都经 `_ui(fn, *args)` 投递，内部吞掉该异常；
- `check()` 只做「异常兜底 + `finally` 收敛到 `enable_start()`」，
  流程本体在 `_check_impl()` 中；
- **关键动作（启动主程序）不依赖 UI**，即使窗口被关闭也会执行：
  `auto_launch()` 会置位 `launched`，而 `main()` 的 `finally` 会调用
  `ensure_launch()` 兜底 —— 窗口被关掉、事件循环结束、`_ui()` 静默跳过时，
  仍保证主程序被拉起（且不会重复拉起）。
- **关键阶段不可中断**：`_check_impl()` 在调用 `apply_update()` 前把 `_busy`
  置为 `True`，`check()` 的 `finally` 复位。`_busy` 期间 `_on_close()` 忽略
  窗口关闭请求 —— 否则目录会停在「一半新、一半 `.old`」，而残留的 `.old`
  会在下次启动被清理，等于永久丢掉这些文件。

---

## 5. 链路四：发布

```
python build.py [--all|--helper|--main]
      ↓ PyInstaller（helper: --onefile；主程序: --add-data data;data + --collect-all）
dist/RandomNamePicker/  (helper.exe, RandomNamePicker.exe, 依赖目录, data/)
      ↓ 打 zip（顶层不能再套一层目录，否则解压会错位）
app.zip
      ↓ 上传到 GitHub Release（tag 形如 v{M}.{N}[.{X}]，X 为 a–z）
      ↓ 更新 helper.py 的 LOCAL_VERSION 并重新打包
用户端 helper.exe 读 releases.atom → 取版本号最大的 tag → 与新版本比较
```

`_parse_version` / `_version_gt` 的语义：比较 `(主版本, 次版本, 补丁字母)`，
补丁用完 `z` 后进位到次版本。`releases.atom` 按**时间**排序，因此必须收集全部
tag 取最大者，不能只看第一条。

---

## 6. 状态文件契约（谁写 / 谁读 / 谁删 / 何时删）

| 文件 | 写 | 读 | 删 | 时机 |
|---|---|---|---|---|
| `_replace_manifest.txt` | `safe_extract()` | `check()` | `check()` | 读入内存后立即 |
| `_replace_rollback.flag` | 兜底批处理 | 人工排查 | `cleanup_stale_replace_files()` | 下次启动 |
| `_replace_report.txt` | 兜底批处理 | 人工排查 | 不删 | — |
| `_staging/` | `safe_extract()` | `replace_staged()` | 成功 / 回滚 / 兜底 / 下次启动 | — |
| `_backup/` | `apply_update()` | `rollback()` | 更新成功时；失败保留 | — |

`*.old` 不是状态文件：它由 `clean_dir()` 按后缀产生、由 `cleanup_stale_old_files()`
按后缀扫描清除，**无需记录"谁标记过"**。

「某个文件是否保留（不改名 / 不删除 / 不覆盖）」也不在本表 —— 统一由 `KEEP_ITEMS`
决定（见 §1）。

> 已知差异（本次未改）：兜底批处理的**还原**段仍用 `xcopy /e /i /y` 整体回拷
> （只有清空段查了白名单），因此该路径会用 `_backup` 里的旧日志覆盖当前日志。
> 影响仅限日志证据，且只在「内联替换失败 → 兜底批处理也回滚」的罕见路径触发。

「漏删」是历史上出过事故的地方（残留清单会被下次启动误读为有替换任务），
因此上表各项统一在 `cleanup_stale_replace_files()` 与 `rollback()` 两处收口。

---

## 7. 单实例与进程互斥

| Mutex 名 | 持有者 | 用途 |
|---|---|---|
| `RandomNamePicker.Helper.SingleInstance` | helper.exe | 防止并发更新 |
| `RandomNamePicker.Main.SingleInstance` | RandomNamePicker.exe | 防止重复开窗 |

`is_main_running()` 用「尝试获取主程序 Mutex」来探测主程序是否在运行（拿到即释放）。
两个关键交互：

- **无更新且主程序在运行** → helper 不启动第二个实例，直接退出（避免弹窗打扰）。
- **有更新且主程序在运行** → `kill_main()`（`taskkill /F`）后再更新。

> 说明：`kill_main()` 目前只有强杀一种手段（关闭前无提示、无优雅关闭），
> 这属于已知残留项，见 §9。

---

## 8. 故障矩阵

| 故障 | 现行为 | 用户可见结果 |
|---|---|---|
| 无网络 / atom 不可达 | 跳过更新 | 「无法连接 GitHub」，随后启动主程序 |
| 下载失败（含所有代理） | 清理半成品 zip | 「下载失败」，随后启动主程序 |
| 更新包损坏（CRC 校验失败） | 回滚 | 「解压失败，已回滚」 |
| 更新包路径非法（Zip Slip） | 拒绝解压并回滚 | 同上 |
| 磁盘满 / 目录不可写 | 写清单失败 → 上抛 → 回滚 | 「更新失败，已回滚」 |
| 目标文件被占用 | 重试 8 次 → 兜底 bat（退出后再试） | 「部分文件被占用，即将退出后完成更新...」 |
| 兜底 bat 也无法生成 | 回滚 | 「更新失败，已回滚（N 项未替换）」 |
| 替换清单为空 | 判定失败并回滚 | 「更新失败，已回滚」 |
| 更新途中用户关窗 | 关键阶段（备份/标记/解压/替换）忽略关闭请求；非关键阶段关闭后由 `ensure_launch()` 兜底启动主程序 | 关键阶段会看到「更新进行中，请稍候…」；其余情况主程序照常出现 |
| 回滚后关键文件缺失 | 写 ERROR 日志（不静默） | 日志中可见 `[ERROR] 回滚后关键文件缺失` |

---

## 9. 本次未修复的已知项（按决定保留）

| 项 | 内容 | 影响 |
|---|---|---|
| `H6` | 更新前 `taskkill /F` 强杀主程序，无提示、无优雅关闭 | 主程序里未保存的输入会丢失；同名 exe 会被一起杀掉 |
| `B1` | `build.py` 编译失败不设置退出码（恒 0） | 自动化无法感知打包失败 |
| `B2` | `build.py` 未知参数静默忽略 | 写错参数会「什么都不编译却报打包完成」 |
| `V1` | `versiontest.py` 版本比较自检失败时退出码仍为 0 | `V2`（取不到 tag 返回 1）已修，此项与之一致性未修 |
| — | 无 SHA256 校验（仅有总大小 + CRC） | 无法防御「完整但被替换」的包 |
| — | `dist/` 中的 `helper.exe` 为修复前版本 | 需重新 `build.py --helper` 才能分发 |

---

## 10. 改代码时的检查清单

1. 新增失败分支时：它是否收敛到「回滚」或「明确失败态」？
2. 新增/修改状态文件时：§6 的四问（谁写、谁读、谁删、何时删）是否补齐？
3. 涉及进程内改名/替换时：目标路径此刻是否已腾空？是否需要跳过「正在运行的自己」？
4. 涉及 UI 更新时：是否经 `_ui()`？（后台线程不得直接调 `root.after`）
5. 改动后至少执行：
   ```
   python -m py_compile app.py helper.py build.py versiontest.py
   python versiontest.py
   ```
   涉及更新流程的改动，另需沙盒端到端验证（备份/标记/替换/回滚）。
6. 发版时：`LOCAL_VERSION` 是否已同步？`app.zip` 顶层是否**没有**多余包装目录？
