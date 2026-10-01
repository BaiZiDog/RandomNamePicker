# -*- coding: utf-8 -*-
"""随机点名工具 —— 使用 pywebview 渲染内嵌 HTML 页面"""
import os
import sys
import time
import shutil
import random

import webview

# 打包后（PyInstaller onefile）用 exe 所在目录定位配置文件；开发时用脚本目录
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
os.makedirs(DATA_DIR, exist_ok=True)
FILE_LIST = os.path.join(DATA_DIR, 'file.txt')

# 日志文件与轮转阈值（超过后轮转为 app.log.1，只保留一代）
LOG_FILE = os.path.join(BASE_DIR, 'app.log')
LOG_MAX_BYTES = 1024 * 1024

# 命名 Mutex 名称（系统范围内唯一，用于单实例互斥）
MUTEX_NAME = 'RandomNamePicker.Main.SingleInstance'

# 另存为对话框类型：pywebview 6.x 推荐 FileDialog 枚举，旧常量作为兜底
_FileDialog = getattr(webview, 'FileDialog', None)
_SAVE_DIALOG = getattr(_FileDialog, 'SAVE', None) or getattr(
    webview, 'SAVE_DIALOG', 30)


def log(msg, level='INFO'):
    """写入日志文件便于排查。

    格式：[时间] [级别] [线程] 消息
    level: INFO / WARN / ERROR / DEBUG

    文件超过 LOG_MAX_BYTES 时轮转为 app.log.1（覆盖上一代），
    避免长期使用后日志无限增长。
    """
    try:
        import time as _t
        import threading
        ts = _t.strftime('%Y-%m-%d %H:%M:%S')
        ms = int((_t.time() % 1) * 1000)
        thread_name = threading.current_thread().name
        line = f'[{ts}.{ms:03d}] [{level:<5}] [{thread_name}] {msg}\n'
        try:
            if os.path.getsize(LOG_FILE) >= LOG_MAX_BYTES:
                os.replace(LOG_FILE, LOG_FILE + '.1')
        except OSError:
            pass
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError:
        pass


def log_exc(msg):
    """记录异常及其完整堆栈，便于定位问题。"""
    import traceback
    log(f'{msg}\n{traceback.format_exc()}', level='ERROR')


def log_env():
    """记录运行环境信息，便于排查平台相关问题。"""
    log('-' * 60)
    log(f'主程序启动 | PID={os.getpid()}')
    log(f'Python={sys.version.split()[0]} | 平台={sys.platform} | frozen={getattr(sys, "frozen", False)}')
    log(f'BASE_DIR={BASE_DIR}')
    log(f'可执行文件={sys.executable}')
    log(f'数据目录={DATA_DIR}（存在={os.path.isdir(DATA_DIR)}）')
    log(f'名单文件={FILE_LIST}（存在={os.path.exists(FILE_LIST)}）')
    log(f'工作目录={os.getcwd()}')
    log(f'Mutex 名：{MUTEX_NAME}')
    log('-' * 60)


# ---------------------------------------------------------------------------
# 单实例互斥：命名 Mutex
# ---------------------------------------------------------------------------

class SingleInstance:
    """基于 Windows 命名 Mutex 的跨进程单实例锁。

    CreateMutexW + GetLastError 判定：获取失败即表示已有实例在运行。
    本程序只在 Windows 上发布（依赖 MessageBoxW / os.startfile），
    因此不再保留其他平台的文件锁分支（原分支在本产品中不可达）。
    """

    def __init__(self, name):
        self.name = name
        self._handle = None
        self.acquired = False

    def acquire(self):
        """尝试获取所有权。成功返回 True，已有实例返回 False。"""
        log(f'尝试获取 Mutex：{self.name}', level='DEBUG')
        try:
            return self._acquire_windows()
        except Exception as e:
            # 出错时保守放行，避免因锁机制本身故障导致程序无法启动
            log_exc(f'Mutex 获取异常，放行启动：{e}')
            self.acquired = False
            return True

    def _acquire_windows(self):
        import ctypes
        from ctypes import wintypes

        ERROR_ALREADY_EXISTS = 183
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE

        handle = kernel32.CreateMutexW(None, False, self.name)
        err = ctypes.get_last_error()
        if not handle:
            log(f'CreateMutexW 失败，错误码 {err}，放行启动', level='WARN')
            return True
        if err == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            log(f'检测到已有实例运行（Mutex={self.name}），退出', level='WARN')
            return False
        self._handle = handle
        self.acquired = True
        log(f'Mutex 获取成功：{self.name}（句柄={handle}）')
        return True

    def release(self):
        """释放 Mutex 资源，可重复调用。"""
        if self._handle is not None:
            try:
                import ctypes
                ctypes.WinDLL('kernel32', use_last_error=True).CloseHandle(
                    self._handle)
                log(f'Mutex 已释放：{self.name}（句柄={self._handle}）')
            except Exception as e:
                log_exc(f'Mutex 释放失败：{e}')
            finally:
                self._handle = None
        self.acquired = False


_instance_lock = SingleInstance(MUTEX_NAME)


class Api:
    """暴露给前端 JS 的接口，通过 window.pywebview.api 调用"""

    def __init__(self):
        self.avoid_choice = []      # 去重模式：本轮已抽中过的名字
        self.avoid_name = ''        # 平衡模式：上一位被抽中者
        self.mode_status = 'normal'  # 当前模式：normal / balance / dedup
        self._window = None         # 主窗口引用，用于全屏切换 / 文件对话框
        self._last_fs_toggle = 0.0  # 上次全屏切换时间戳（Esc 去抖用）

    def _resolve_path(self, path):
        """相对路径基于 DATA_DIR 解析"""
        if os.path.isabs(path):
            return path
        return os.path.join(DATA_DIR, path)

    @staticmethod
    def _read_lines(path):
        """读取文本行，兼容 UTF-8（含 BOM）与 GBK（记事本 ANSI 另存）。

        名单文件由用户手工维护，编码不受我们控制：用 UTF-8 读 GBK 文件会抛
        UnicodeDecodeError（属 ValueError 而非 OSError），异常会穿透 js_api
        导致前端静默失败，因此这里显式做编码回退。
        """
        for enc in ('utf-8-sig', 'gbk'):
            try:
                with open(path, 'r', encoding=enc) as f:
                    return [line.strip() for line in f if line.strip()]
            except UnicodeDecodeError:
                continue
            except OSError:
                return []
        log(f'名单文件编码无法识别（已尝试 utf-8-sig / gbk）：{path}', level='WARN')
        return []

    def _get_current_path(self):
        """当前选中文件（file.txt 第一行）的绝对路径；未选择名单时返回 ''"""
        files = self.get_file_list()
        if files:
            return self._resolve_path(files[0])
        return ''

    def _save_file_list(self, paths):
        """保存名单列表到 file.txt（先写临时文件再原子替换）。

        直接以 'w' 截断写入时，写到一半崩溃会丢掉整份名单索引；
        改为 "写 .tmp -> os.replace" 的原子替换。
        """
        tmp = FILE_LIST + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                for p in paths:
                    f.write(p + '\n')
            os.replace(tmp, FILE_LIST)
        except OSError as e:
            log(f'保存名单列表失败：{e}', level='WARN')

    def get_file_list(self):
        """读取 file.txt 中的名单路径列表"""
        return self._read_lines(FILE_LIST)

    def set_current_file(self, path):
        """将指定文件设为当前（移到列表首位）"""
        paths = self.get_file_list()
        if path in paths:
            paths.remove(path)
            paths.insert(0, path)
            self._save_file_list(paths)
        self.avoid_choice.clear()
        self.avoid_name = ''
        return True

    def add_file(self, path):
        """添加名单文件，复制到 data 目录下；返回列表（统一为文件名形式）"""
        filename = os.path.basename(path)
        dest = os.path.join(DATA_DIR, filename)
        # 如果 data 下已有同名文件，自动加序号避免覆盖
        if os.path.exists(dest) and os.path.abspath(path) != os.path.abspath(dest):
            base, ext = os.path.splitext(filename)
            i = 1
            while os.path.exists(os.path.join(DATA_DIR, f'{base}_{i}{ext}')):
                i += 1
            filename = f'{base}_{i}{ext}'
            dest = os.path.join(DATA_DIR, filename)
        # 复制文件到 data 目录
        if os.path.abspath(path) != os.path.abspath(dest):
            try:
                shutil.copy2(path, dest)
            except OSError as e:
                log(f'复制名单文件失败：{path} -> {dest}：{e}', level='WARN')
                return self.get_file_list()
        paths = self.get_file_list()
        # 统一保存【文件名】（与 create_new_file 一致），避免同一文件因
        # "绝对路径 / 文件名"两种写法重复入库、下拉框出现两个同名项
        if filename not in paths:
            paths.append(filename)
            self._save_file_list(paths)
        return paths

    def remove_file(self, path):
        """删除名单文件，同时移除索引条目。

        返回 {'ok': bool, 'paths': [...], 'msg': str}。
        只有文件确实删除成功才移除条目 —— 否则会出现"条目没了、文件还在"
        的幽灵状态（下次启动又冒出来）。
        """
        paths = self.get_file_list()
        abs_path = self._resolve_path(path)
        try:
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except OSError as e:
            log(f'删除名单文件失败：{abs_path}：{e}', level='WARN')
            return {'ok': False, 'paths': paths,
                    'msg': f'删除失败：{e.strerror or e}'}
        if path in paths:
            paths.remove(path)
            self._save_file_list(paths)
        return {'ok': True, 'paths': paths, 'msg': ''}

    def create_new_file(self, name):
        """创建空白名单文件（索引统一保存文件名，与 add_file 保持一致）"""
        name = os.path.basename((name or '').strip())
        if not name:
            return self.get_file_list()
        if not name.endswith('.txt'):
            name += '.txt'
        path = os.path.join(DATA_DIR, name)
        if not os.path.exists(path):
            try:
                with open(path, 'w', encoding='utf-8'):
                    pass
            except OSError as e:
                log(f'创建名单文件失败：{path}：{e}', level='WARN')
                return self.get_file_list()
        paths = self.get_file_list()
        if name not in paths:
            paths.append(name)
            self._save_file_list(paths)
        return paths

    def edit_file(self, path):
        """用系统默认编辑器打开名单文件。返回是否成功。"""
        abs_path = self._resolve_path(path)
        if not os.path.exists(abs_path):
            log(f'待编辑的名单文件不存在：{abs_path}', level='WARN')
            return False
        try:
            os.startfile(abs_path)
            return True
        except OSError as e:
            log(f'打开编辑器失败：{abs_path}：{e}', level='WARN')
            return False

    def browse_file(self):
        """打开文件对话框选择名单文件。

        返回 {'path': 绝对路径或 '', 'msg': 提示信息}：
        取消选择时 msg 为空（静默返回，符合系统对话框习惯）；
        选到非 .txt 时给出提示，避免"点了没反应"的困惑。
        """
        if self._window is None:
            return {'path': '', 'msg': '窗口尚未就绪，请稍后重试'}
        results = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Text files (*.txt)",),
        )
        if not results:
            return {'path': '', 'msg': ''}
        path = str(results[0])
        if not path.lower().endswith('.txt'):
            log(f'已忽略非 .txt 文件：{path}', level='WARN')
            return {'path': '', 'msg': '仅支持 .txt 名单文件'}
        return {'path': path, 'msg': ''}

    def ask_new_file_name(self):
        """弹出系统"另存为"对话框让用户输入新名单文件名，返回文件名（取消返回 ''）。

        不再依赖 window.prompt：该 API 在部分 WebView 后端不可用，
        而 create_file_dialog 是已验证可用的窗口实例方法。
        只取文件名部分（名单统一存放在 data/ 下）。
        """
        if self._window is None:
            return ''
        result = self._window.create_file_dialog(
            _SAVE_DIALOG,
            save_filename='新名单.txt',
            file_types=("Text files (*.txt)",),
        )
        if not result:
            return ''
        picked = result[0] if isinstance(result, (list, tuple)) else result
        return os.path.basename(str(picked))

    def set_window(self, w):
        self._window = w

    def toggle_fullscreen(self):
        """切换全屏/窗口化（Esc 与"全屏 / 窗口"按钮共用）。

        去抖：WebView 自身对 Esc 也可能退出全屏，若两个触发源在极短时间内
        都调用本方法会切换两次（等于没切），因此忽略 350ms 内的重复请求。
        """
        now = time.time()
        if now - self._last_fs_toggle < 0.35:
            return True
        self._last_fs_toggle = now
        if self._window is not None:
            self._window.toggle_fullscreen()
        return True

    def get_names(self):
        """读取当前选中名单文件；未选择名单或读取失败返回 []"""
        path = self._get_current_path()
        if not path:
            return []
        return self._read_lines(path)

    def set_mode(self, mode):
        """前端切换抽取模式；切换时清空避让状态，从干净状态开始"""
        if mode in ('normal', 'balance', 'dedup'):
            self.mode_status = mode
            self.avoid_choice.clear()
            self.avoid_name = ''
        return self.mode_status

    def choose_name(self):
        """最终随机抽取：结果由 Python 端产生，前端动画只负责表现。
        按 mode_status 分三种模式：
        - normal：完全随机，不做避让
        - balance：避开上一位抽中者 avoid_name，抽后替换为本轮结果
        - dedup：本轮已抽中的不再抽，全员抽完自动重置"""
        names = self.get_names()
        if not names:
            return ''
        if self.mode_status == 'normal':
            return random.choice(names)
        if self.mode_status == 'balance':
            pool = [n for n in names if n != self.avoid_name]
            name = random.choice(pool if pool else names)
            self.avoid_name = name
            return name
        # dedup 模式
        pool = [n for n in names if n not in self.avoid_choice]
        if not pool:  # 所有人都被抽过，清空记录开始新一轮
            self.avoid_choice.clear()
            pool = names
        name = random.choice(pool)
        self.avoid_choice.append(name)
        return name

    def choose_multi(self, count):
        """多人点名：随机抽取 count 个不重复名字"""
        names = self.get_names()
        if not names:
            return {'ok': False, 'msg': '名单为空'}
        try:
            count = int(count)
        except (TypeError, ValueError):
            return {'ok': False, 'msg': '人数格式错误'}
        if count < 1:
            return {'ok': False, 'msg': '人数必须大于 0'}
        if count > len(names):
            return {'ok': False, 'msg': f'抽取人数超过名单人数（{len(names)} 人）'}
        picked = random.sample(names, count)
        return {'ok': True, 'names': picked}

    def create_groups(self, sizes):
        """随机分组：sizes 为每组人数列表（每组可自定义），无重复且符合参数"""
        names = self.get_names()
        if not names:
            return {'ok': False, 'msg': '名单为空'}
        if not sizes:
            return {'ok': False, 'msg': '请至少添加一个分组'}
        try:
            sizes = [int(s) for s in sizes]
        except (TypeError, ValueError):
            return {'ok': False, 'msg': '分组参数格式错误'}
        if any(s < 1 for s in sizes):
            return {'ok': False, 'msg': '每组人数必须大于 0'}
        total = sum(sizes)
        if total > len(names):
            return {'ok': False, 'msg': f'共需 {total} 人，名单只有 {len(names)} 人'}
        pool = random.sample(names, total)
        result = []
        offset = 0
        for s in sizes:
            result.append(pool[offset:offset + s])
            offset += s
        return {'ok': True, 'groups': result}


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>随机点名</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: "Microsoft YaHei", sans-serif;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    user-select: none;
  }
  .wrap { text-align: center; }
  /* 尺寸全部用 vh/vw 相对单位，窗口/分辨率变化时文字与布局自动缩放 */
  h1 {
    color: #fff;
    margin-bottom: 2vh;
    letter-spacing: 1vh;
    font-size: 10vh;  /* 初始放大 */
    font-weight: bold;
    transition: font-size 0.6s ease, margin-bottom 0.6s ease;
  }
  h1.shrink {
    font-size: 5.5vh;  /* 第一次点名后缩小 */
    margin-bottom: 2vh;
  }
  .display {
    position: relative;
    width: min(80vw, 170vh);
    height: 30vh;  /* 初始较小 */
    line-height: 30vh;
    margin: 0 auto 2vh;
    background: rgba(255, 255, 255, 0.95);
    border-radius: 2vh;
    font-weight: bold;
    color: #4a4a6a;
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.25);
    overflow: hidden;
    transition: height 0.6s ease, line-height 0.6s ease, margin 0.6s ease;
  }
  .display.expand {
    height: 44vh;  /* 第一次点名后放大 */
    line-height: 44vh;
  }
  .display.rolling { color: #8a8ab0; }
  .display.result {
    color: #fff;
    background: linear-gradient(135deg, #ff9a44 0%, #fc6076 100%);
  }
  /* 名字层：绝对定位，仅用 transform/filter/opacity 做过渡，避免重排抖动 */
  .name {
    position: absolute;
    left: 0;
    width: 100%;
    font-size: 16.5vh;   /* 初始：30vh × 0.55 */
    line-height: 30vh;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    padding: 0 16px;
    will-change: transform, filter, opacity;
    transition: font-size 0.6s ease, line-height 0.6s ease;
  }
  /* 失焦切换：模糊度由 JS 按滚动速度写入 --b（快时约10px，慢时趋近0，像镜头对焦） */
  .name-in  { animation: focusIn 0.22s ease-out both; }
  .name-out { animation: focusOut 0.22s ease-in both; }
  .name-reveal { animation: reveal 0.6s cubic-bezier(0.2, 0.8, 0.3, 1) both; }
  @keyframes focusIn {
    from { filter: blur(var(--b, 10px)); transform: scale(1.3);  opacity: 0.1; }
    to   { filter: blur(0);             transform: scale(1);    opacity: 1; }
  }
  @keyframes focusOut {
    from { filter: blur(0);             transform: scale(1);    opacity: 1; }
    to   { filter: blur(var(--b, 10px)); transform: scale(0.75); opacity: 0; }
  }
  @keyframes reveal {
    0%   { transform: scale(0.5);  opacity: 0; filter: blur(8px); }
    60%  { transform: scale(1.12); opacity: 1; filter: blur(0); }
    100% { transform: scale(1);    opacity: 1; filter: blur(0); }
  }
  button {
    padding: 1.4vh 4.5vw;
    font-size: 3.6vh;
    letter-spacing: 0.7vh;
    color: #5b4a9a;
    background: #fff;
    border: none;
    border-radius: 4vh;
    cursor: pointer;
    box-shadow: 0 8px 20px rgba(0, 0, 0, 0.22);
    transition: transform 0.15s, opacity 0.15s;
  }
  button:hover:not(:disabled) { transform: translateY(-2px); }
  button:active:not(:disabled) { transform: scale(0.96); }
  button:disabled { opacity: 0.6; cursor: not-allowed; }
  /* 控件行：按钮 + 模式选择横向排列，紧凑收在一行 */
  .controls {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 2.5vw;
    margin: 2vh auto 0;
  }
  /* 全局工具栏：全屏/语音键在所有模式下可见 */
  .global-bar {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 1vw;
    margin: 0 auto 1.2vh;
  }
  select {
    padding: 1.2vh 2.5vw;
    font-size: 2.2vh;
    font-family: inherit;
    color: #5b4a9a;
    background: #fff;
    border: none;
    border-radius: 3vh;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
    outline: none;
  }
  .mode-desc {
    margin-top: 1.5vh;
    min-height: 2.2vh;
    color: rgba(255, 255, 255, 0.92);
    font-size: 1.8vh;
    letter-spacing: 0.12vh;
  }
  .count { margin-top: 0.8vh; color: rgba(255, 255, 255, 0.85); font-size: 1.8vh; }
  /* 全屏/窗口切换小按钮 */
  .fs-btn {
    padding: 1.2vh 2vw;
    font-size: 2vh;
    letter-spacing: 0.3vh;
    color: #5b4a9a;
    background: rgba(255, 255, 255, 0.85);
    border: none;
    border-radius: 3vh;
    cursor: pointer;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.18);
    transition: transform 0.15s, opacity 0.15s;
  }
  .fs-btn:hover { transform: translateY(-2px); }
  .fs-btn:active { transform: scale(0.96); }
  /* 名单管理区域 */
  .file-mgr {
    margin-top: 1.2vh;
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 0.6vw;
    flex-wrap: wrap;
    opacity: 0.55;
    transition: opacity 0.3s;
  }
  .file-mgr:hover { opacity: 0.85; }
  .file-mgr select {
    max-width: 28vw;
    min-width: 10vw;
    padding: 0.5vh 1.2vw;
    font-size: 1.4vh;
    box-shadow: none;
    background: rgba(255, 255, 255, 0.25);
    color: rgba(255, 255, 255, 0.8);
    border: 1px solid rgba(255, 255, 255, 0.2);
  }
  .file-mgr select option { color: #333; background: #fff; }
  .file-mgr button {
    padding: 0.4vh 1vw;
    font-size: 1.4vh;
    letter-spacing: 0.1vh;
    color: rgba(255, 255, 255, 0.7);
    background: transparent;
    border: 1px solid rgba(255, 255, 255, 0.2);
    border-radius: 1.5vh;
    cursor: pointer;
    box-shadow: none;
    transition: opacity 0.15s;
  }
  .file-mgr button:hover { opacity: 0.9; }
  .file-mgr button:active { opacity: 0.7; }
  .file-mgr .del-btn { color: rgba(255, 150, 150, 0.7); }
  /* 选项卡栏：标题与显示区之间 */
  .tabs {
    display: flex;
    justify-content: center;
    gap: 1.2vw;
    margin: 0 auto 1.8vh;
  }
  .tab {
    padding: 0.9vh 3vw;
    font-size: 2.6vh;
    letter-spacing: 0.5vh;
    color: rgba(255, 255, 255, 0.8);
    background: rgba(255, 255, 255, 0.15);
    border: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 3vh;
    cursor: pointer;
    box-shadow: none;
    transition: background 0.2s, color 0.2s, transform 0.15s;
  }
  .tab:hover { color: #fff; background: rgba(255, 255, 255, 0.28); }
  .tab.active {
    color: #5b4a9a;
    background: #fff;
    font-weight: bold;
    box-shadow: 0 6px 18px rgba(0, 0, 0, 0.25);
  }
  /* 参数输入行（多人/分组） */
  .param-row {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 1vw;
    margin: 2vh auto 1.5vh;
    color: rgba(255, 255, 255, 0.92);
    font-size: 2.2vh;
    letter-spacing: 0.15vh;
  }
  .param-row input {
    position: relative;
    z-index: 2;
    width: 7vw;
    padding: 0.9vh 1vw;
    font-size: 2.4vh;
    text-align: center;
    color: #5b4a9a;
    background: rgba(255, 255, 255, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.85);
    border-radius: 2vh;
    outline: none;
    box-shadow: 0 5px 12px rgba(0, 0, 0, 0.18);
    transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
  }
  .param-row input:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 18px rgba(0, 0, 0, 0.24);
  }
  .param-row input:focus {
    border-color: #667eea;
    box-shadow: 0 8px 18px rgba(0, 0, 0, 0.22),
                0 0 0 3px rgba(102, 126, 234, 0.35);
  }
  .param-row .go-btn {
    padding: 1.1vh 2.5vw;
    font-size: 2.4vh;
    letter-spacing: 0.4vh;
    color: #fff;
    background: linear-gradient(135deg, #ff9a44 0%, #fc6076 100%);
    border: 1px solid rgba(255, 255, 255, 0.55);
    border-radius: 3vh;
    cursor: pointer;
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.22);
    transition: transform 0.15s, opacity 0.15s, box-shadow 0.15s;
  }
  .param-row .go-btn:hover {
    transform: translateY(-2px);
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.28);
  }
  .param-row .go-btn:active { transform: scale(0.96); }
  /* 分组行：整行横跨窗口，左组号 / 中人数输入 + 名单 / 右删除 */
  #group-panel { width: 90vw; max-width: 1700px; }
  #group-rows {
    margin: 2vh auto 0;
    display: flex;
    flex-direction: column;
    gap: 1.2vh;
  }
  #group-rows .group-row {
    display: flex;
    align-items: center;
    gap: 1.2vw;
    width: 100%;
    padding: 1.2vh 1.8vw;
    letter-spacing: 0.1vh;
    /* 半透明玻璃底板：内容可见性与美观度平衡 */
    background: rgba(255, 255, 255, 0.16);
    backdrop-filter: blur(10px) saturate(1.15);
    -webkit-backdrop-filter: blur(10px) saturate(1.15);
    border: 1px solid rgba(255, 255, 255, 0.32);
    border-radius: 2.4vh;
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.16),
                inset 0 1px 0 rgba(255, 255, 255, 0.45);
    animation: reveal 0.35s cubic-bezier(0.2, 0.8, 0.3, 1) both;
  }
  /* 组号徽章：彩色悬浮块，悬浮于半透明底板之上 */
  #group-rows .group-row .g-label {
    position: relative;
    z-index: 2;
    flex-shrink: 0;
    padding: 0.6vh 1.4vw;
    font-size: 2.1vh;
    font-weight: bold;
    color: #fff;
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
    border: 1px solid rgba(255, 255, 255, 0.55);
    border-radius: 1.8vh;
    letter-spacing: 0.15vh;
    box-shadow: 0 5px 12px rgba(70, 60, 150, 0.35);
    transition: transform 0.15s, box-shadow 0.15s;
  }
  #group-rows .group-row .g-label:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 18px rgba(70, 60, 150, 0.45);
  }
  /* 人数输入框：白色悬浮块，悬停上浮、聚焦高亮 */
  #group-rows .group-row input {
    position: relative;
    z-index: 2;
    flex-shrink: 0;
    width: 7vw;
    padding: 0.8vh 0.8vw;
    font-size: 2.3vh;
    text-align: center;
    color: #5b4a9a;
    background: rgba(255, 255, 255, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.85);
    border-radius: 1.6vh;
    outline: none;
    box-shadow: 0 5px 12px rgba(0, 0, 0, 0.18);
    transition: transform 0.15s, border-color 0.15s, box-shadow 0.15s;
  }
  #group-rows .group-row input:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 18px rgba(0, 0, 0, 0.24);
  }
  #group-rows .group-row input:focus {
    border-color: #667eea;
    box-shadow: 0 8px 18px rgba(0, 0, 0, 0.22),
                0 0 0 3px rgba(102, 126, 234, 0.35);
  }
  /* 行内名单：占满剩余宽度，姓名文本框自动换行 */
  #group-rows .group-row .g-names {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.8vh 0.6vw;
  }
  /* 姓名：独立悬浮小文本框，逐个弹出（延迟由 JS 按顺序写入） */
  #group-rows .group-row .g-names .g-name {
    position: relative;
    z-index: 2;
    display: inline-block;
    padding: 0.5vh 1.1vw;
    font-size: 2.1vh;
    color: #5b4a9a;
    background: rgba(255, 255, 255, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.85);
    border-radius: 1.6vh;
    box-shadow: 0 5px 12px rgba(0, 0, 0, 0.18);
    animation: reveal 0.45s cubic-bezier(0.2, 0.8, 0.3, 1) both;
    transition: transform 0.15s, box-shadow 0.15s;
  }
  #group-rows .group-row .g-names .g-name:hover {
    transform: translateY(-2px) scale(1.05);
    box-shadow: 0 9px 18px rgba(0, 0, 0, 0.26);
  }
  /* 删除按钮：圆柱悬浮块，悬停上浮、按下回弹 */
  #group-rows .group-row .rm-btn {
    position: relative;
    z-index: 2;
    flex-shrink: 0;
    padding: 0.8vh 1.3vw;
    font-size: 1.9vh;
    font-weight: bold;
    color: #fff;
    background: #ff8a80;
    border: 1px solid rgba(255, 255, 255, 0.6);
    border-radius: 1.8vh;
    cursor: pointer;
    box-shadow: 0 5px 12px rgba(200, 60, 60, 0.32);
    transition: background 0.15s, transform 0.15s, box-shadow 0.15s;
  }
  #group-rows .group-row .rm-btn:hover {
    background: #ff6b5e;
    transform: translateY(-2px);
    box-shadow: 0 8px 18px rgba(200, 60, 60, 0.42);
  }
  #group-rows .group-row .rm-btn:active { transform: scale(0.94); }
  /* 添加组：次级描边按钮，与主渐变按钮区分 */
  .param-row .add-btn {
    padding: 1.1vh 2.5vw;
    font-size: 2.4vh;
    letter-spacing: 0.4vh;
    color: #fff;
    background: rgba(255, 255, 255, 0.22);
    border: 1.5px solid rgba(255, 255, 255, 0.65);
    border-radius: 3vh;
    cursor: pointer;
    box-shadow: 0 6px 16px rgba(0, 0, 0, 0.18);
    transition: background 0.15s, transform 0.15s;
  }
  .param-row .add-btn:hover {
    background: rgba(255, 255, 255, 0.32);
    transform: translateY(-2px);
  }
  .param-row .add-btn:active { transform: scale(0.96); }
  /* 结果展示区 */
  .result {
    margin: 1vh auto 0;
    min-height: 10vh;
    display: flex;
    flex-wrap: wrap;
    justify-content: center;
    align-items: center;
    gap: 1.2vh;
  }
  /* 多人点名结果：与分组姓名文本框统一为白色悬浮块 */
  .result .name-chip {
    position: relative;
    z-index: 2;
    padding: 1.2vh 2.2vw;
    font-size: 3.4vh;
    color: #5b4a9a;
    background: rgba(255, 255, 255, 0.96);
    border: 1px solid rgba(255, 255, 255, 0.85);
    border-radius: 2vh;
    box-shadow: 0 6px 14px rgba(0, 0, 0, 0.22);
    animation: reveal 0.45s cubic-bezier(0.2, 0.8, 0.3, 1) both;
    transition: transform 0.15s, box-shadow 0.15s;
  }
  .result .name-chip:hover {
    transform: translateY(-2px) scale(1.04);
    box-shadow: 0 10px 22px rgba(0, 0, 0, 0.28);
  }
  /* 分组结果区仅用于错误提示，名单显示在各分组行内 */
  #group-result { flex-direction: column; align-items: center; }
  .result .err-msg {
    font-size: 2.2vh;
    letter-spacing: 0.15vh;
    color: rgba(255, 215, 160, 0.95);
  }
</style>
</head>
<body>
  <div class="wrap">
    <h1>随 机 点 名</h1>
    <div class="tabs">
      <button class="tab active" data-tab="single">单人</button>
      <button class="tab" data-tab="multi">多人</button>
      <button class="tab" data-tab="group">分组</button>
    </div>
    <div class="global-bar">
      <button id="fsbtn" class="fs-btn">全屏 / 窗口</button>
      <button id="voice-btn" class="fs-btn" title="朗读点名结果"> 语音</button>
    </div>
    <div id="single-panel">
      <div id="display" class="display"></div>
      <div class="controls">
        <button id="btn" disabled>名单加载中...</button>
        <select id="mode">
          <option value="normal" selected>普通模式</option>
          <option value="balance">平衡模式</option>
          <option value="dedup">去重模式</option>
        </select>
      </div>
      <div id="mode-desc" class="mode-desc"></div>
      <div id="count" class="count"></div>
    </div>
    <div id="multi-panel" class="panel" style="display:none;">
      <div class="param-row">
        <label>抽取人数</label>
        <input type="number" id="multi-count" min="1" value="1">
        <button class="go-btn" id="multi-btn">开始抽取</button>
      </div>
      <div id="multi-result" class="result"></div>
    </div>
    <div id="group-panel" class="panel" style="display:none;">
      <div id="group-rows"></div>
      <div class="param-row">
        <button class="add-btn" id="group-add-btn">添加组</button>
        <button class="go-btn" id="group-btn">生成</button>
      </div>
      <div id="group-result" class="result"></div>
    </div>
    <div class="file-mgr">
      <select id="file-select" title="选择名单文件"></select>
      <button id="new-btn">新名单</button>
      <button id="edit-btn">编辑内容</button>
      <button id="add-btn">浏览添加</button>
      <button id="del-btn" class="del-btn">删除</button>
      <button id="jp-btn" style="display:none;">整活</button>
    </div>
  </div>
<script>
  var names = [];
  var rolling = false;
  var timers = [];
  var currentEl = null;
  var display = document.getElementById('display');
  var btn = document.getElementById('btn');
  var modeSelect = document.getElementById('mode');
  var modeDesc = document.getElementById('mode-desc');
  var fileSelect = document.getElementById('file-select');
  var newBtn = document.getElementById('new-btn');
  var editBtn = document.getElementById('edit-btn');
  var addBtn = document.getElementById('add-btn');
  var delBtn = document.getElementById('del-btn');
  var currentNameSize = { fs: 16.5, lh: 30 };  // 当前名字字号（vh），初始 30vh×0.55
  var voiceEnabled = true;  // 语音朗读开关
  var voiceBtn = document.getElementById('voice-btn');
  var jpEnabled = false;  // 整活模式（日语朗读）
  var jpBtn = document.getElementById('jp-btn');
  var voiceTapCount = 0;      // 连点"语音"计数
  var voiceTapTimer = null;   // 连点判定窗口
  // 选项卡：单人 / 多人 / 分组
  var tabs = document.querySelectorAll('.tabs .tab');
  var singlePanel = document.getElementById('single-panel');
  var multiPanel = document.getElementById('multi-panel');
  var groupPanel = document.getElementById('group-panel');

  // 统一的后端调用封装：后端抛异常时给出可见提示。
  // 直接写 window.pywebview.api.x().then(...) 在异常时 Promise 被拒，
  // 前端不会有任何反应（表现为"点了没反应"），因此统一在这里兜底。
  function callApi(name) {
    var args = Array.prototype.slice.call(arguments, 1);
    return window.pywebview.api[name].apply(null, args).catch(function (err) {
      var msg = (err && err.message) ? err.message : String(err);
      alert('操作失败：' + msg);
      throw err;
    });
  }

  var DESCRIPTIONS = {
    normal:  '完全随机抽取，可能连续抽到同一人',
    balance: '避开上一位被抽中者，不会连续两次点到同一人',
    dedup:   '同一轮内抽中过的人不再出现，全员抽完后自动开始新一轮'
  };

  modeSelect.addEventListener('change', function () {
    callApi('set_mode', modeSelect.value).then(function () {
      modeDesc.textContent = DESCRIPTIONS[modeSelect.value];
    });
  });

  function clearTimers() {
    timers.forEach(clearTimeout);
    timers = [];
  }

  // 名字超出显示区宽度时自动缩小字号（长名单/小窗口自适应）
  // 注意：用元素自身 scrollWidth > clientWidth 判断文字是否溢出，
  // 不能与 display.clientWidth 比较（width:100% 的元素 scrollWidth 恒大于它，会误缩到最小）
  function fitName(el) {
    if (!el) return;
    el.style.fontSize = '';
    var fs = parseFloat(getComputedStyle(el).fontSize);
    while (el.scrollWidth > el.clientWidth && fs > 12) {
      fs -= 2;
      el.style.fontSize = fs + 'px';
    }
  }
  window.addEventListener('resize', function () { fitName(currentEl); });

  // Esc 切换全屏/窗口化；全屏按钮同样生效（教室大屏触控友好）
  // 注：WebView 自身对 Esc 也可能退出全屏，重复触发由后端 toggle_fullscreen 去抖
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      window.pywebview.api.toggle_fullscreen();
    }
  });
  document.getElementById('fsbtn').addEventListener('click', function () {
    window.pywebview.api.toggle_fullscreen();
  });

  // 语音朗读开关（整活开启时仍保留完整功能）
  voiceBtn.addEventListener('click', function () {
    voiceEnabled = !voiceEnabled;
    voiceBtn.textContent = voiceEnabled ? ' 语音' : '静音';
    voiceBtn.style.opacity = voiceEnabled ? '1' : '0.5';
    if (!voiceEnabled) {
      if (window.speechSynthesis) window.speechSynthesis.cancel();  // 静音立即中断朗读
    }
    // 连点三下"语音"：在名单选择界面显示隐藏的"整活"选项
    voiceTapCount++;
    clearTimeout(voiceTapTimer);
    voiceTapTimer = setTimeout(function () { voiceTapCount = 0; }, 600);
    if (voiceTapCount >= 3) {
      voiceTapCount = 0;
      clearTimeout(voiceTapTimer);
      jpBtn.style.display = '';
    }
  });

  // 整活模式（日语朗读中文名字）
  function setJpEnabled(on) {
    jpEnabled = on;
    if (on) {
      jpBtn.textContent = '关闭';
      jpBtn.style.display = '';
    } else {
      jpBtn.textContent = '整活';
      jpBtn.style.display = 'none';
    }
  }
  jpBtn.addEventListener('click', function () {
    setJpEnabled(!jpEnabled);
  });

  // 预加载语音列表（异步）
  var cachedVoices = [];
  if (window.speechSynthesis) {
    cachedVoices = window.speechSynthesis.getVoices();
    window.speechSynthesis.onvoiceschanged = function () {
      cachedVoices = window.speechSynthesis.getVoices();
    };
  }

  function makeUtterance(text) {
    var u = new SpeechSynthesisUtterance(text);
    if (jpEnabled) {
      // 尝试找日语语音
      var jpVoice = cachedVoices.find(function (v) { return v.lang.indexOf('ja') === 0; });
      if (jpVoice) {
        u.voice = jpVoice;
      }
      u.lang = 'ja-JP';
    } else if (/[\u4e00-\u9fff]/.test(text)) {
      u.lang = 'zh-CN';
    } else {
      u.lang = 'en-US';
    }
    u.rate = 0.9;
    u.pitch = 1;
    return u;
  }

  function speak(text) {
    if (!voiceEnabled || !window.speechSynthesis) return;
    window.speechSynthesis.cancel();
    window.speechSynthesis.speak(makeUtterance(text));
  }

  // 依次朗读多段文本：一次性入队，段间停顿最小（用于多人点名）
  function speakSeq(parts) {
    if (!voiceEnabled || !window.speechSynthesis || parts.length === 0) return;
    window.speechSynthesis.cancel();
    for (var i = 0; i < parts.length; i++) {
      window.speechSynthesis.speak(makeUtterance(parts[i]));
    }
  }

  function showStatic(text) {
    display.innerHTML = '';
    var el = document.createElement('span');
    el.className = 'name';
    el.textContent = text;
    display.appendChild(el);
    currentEl = el;
    fitName(el);
  }

  // 切换一格：旧名字虚化缩小淡出，新名字由虚到实落定
  function tick(text, blur) {
    var old = currentEl;
    if (old) {
      old.classList.remove('name-in');
      old.classList.add('name-out');
      setTimeout(function () { old.remove(); }, 260);
    }
    var el = document.createElement('span');
    el.className = 'name name-in';
    el.textContent = text;
    el.style.setProperty('--b', blur + 'px');
    el.style.fontSize = currentNameSize.fs + 'vh';
    el.style.lineHeight = currentNameSize.lh + 'vh';
    display.appendChild(el);
    currentEl = el;
    fitName(el);
  }

  function loadFileList() {
    callApi('get_file_list').then(function (files) {
      fileSelect.innerHTML = '';
      if (!files || files.length === 0) {
        fileSelect.innerHTML = '<option value="">无名单文件</option>';
        return;
      }
      files.forEach(function (f, i) {
        var opt = document.createElement('option');
        opt.value = f;
        opt.textContent = f.split(/[\\/]/).pop();
        if (i === 0) opt.selected = true;
        fileSelect.appendChild(opt);
      });
    });
  }

  function reloadNames() {
    callApi('get_names').then(function (list) {
      names = list || [];
      if (names.length === 0) {
        showStatic('名单为空');
        btn.textContent = '请检查名单文件';
        document.getElementById('count').textContent = '';
        return;
      }
      if (!rolling) showStatic('准备就绪');
      btn.disabled = false;
      btn.textContent = '开 始 点 名';
      modeDesc.textContent = DESCRIPTIONS[modeSelect.value];
      document.getElementById('count').textContent = '名单共 ' + names.length + ' 人 · Esc 切换全屏/窗口';
      // 名单变更后，若仍处于自动平均模式则重新分配分组人数
      if (typeof distributeEvenly === 'function' && groupAutoEven) distributeEvenly();
    });
  }

  fileSelect.addEventListener('change', function () {
    var path = fileSelect.value;
    if (path) {
      callApi('set_current_file', path).then(function () {
        reloadNames();
      });
    }
  });

  addBtn.addEventListener('click', function () {
    callApi('browse_file').then(function (res) {
      if (!res || !res.path) {
        // 取消选择时静默返回；选了非 .txt 文件则给出提示
        if (res && res.msg) alert(res.msg);
        return;
      }
      callApi('add_file', res.path).then(function () {
        loadFileList();
        reloadNames();
      });
    });
  });

  delBtn.addEventListener('click', function () {
    var path = fileSelect.value;
    if (!path) return;
    var label = path.split(/[\\/]/).pop();
    // 删除会同时删掉文件本体且不可恢复，必须二次确认
    if (!confirm('确定删除名单「' + label + '」？\n该文件会被永久删除，无法恢复。')) {
      return;
    }
    callApi('remove_file', path).then(function (res) {
      if (res && !res.ok) { alert(res.msg || '删除失败'); }
      loadFileList();
      reloadNames();
    });
  });

  newBtn.addEventListener('click', function () {
    callApi('ask_new_file_name').then(function (name) {
      if (!name) return;
      callApi('create_new_file', name).then(function () {
        loadFileList();
        reloadNames();
      });
    });
  });

  editBtn.addEventListener('click', function () {
    var path = fileSelect.value;
    if (!path) return;
    callApi('edit_file', path).then(function (ok) {
      if (!ok) alert('打开名单文件失败，请确认文件是否仍然存在');
    });
  });

  function init() {
    loadFileList();
    reloadNames();
  }

  // ---- 选项卡切换：单人 / 多人 / 分组 ----
  function switchTab(name) {
    tabs.forEach(function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === name);
    });
    singlePanel.style.display = (name === 'single') ? '' : 'none';
    multiPanel.style.display = (name === 'multi') ? '' : 'none';
    groupPanel.style.display = (name === 'group') ? '' : 'none';
  }
  tabs.forEach(function (t) {
    t.addEventListener('click', function () {
      switchTab(t.getAttribute('data-tab'));
    });
  });

  // 多人点名
  document.getElementById('multi-btn').addEventListener('click', function () {
    var n = parseInt(document.getElementById('multi-count').value, 10);
    if (!n || n < 1) { n = 1; }
    callApi('choose_multi', n).then(function (res) {
      var box = document.getElementById('multi-result');
      if (!res || !res.ok) {
        box.innerHTML = '<div class="err-msg">' + (res.msg || '抽取失败') + '</div>';
        return;
      }
      box.innerHTML = '';
      res.names.forEach(function (nm, i) {
        var el = document.createElement('span');
        el.className = 'name-chip';
        el.style.animationDelay = (i * 0.12) + 's';
        el.textContent = nm;
        box.appendChild(el);
      });
      // 姓名连读（逗号停顿短），全部读完后以"被选中"作为流程结束标识
      speakSeq([res.names.join('，') + '。', '被选中']);
    });
  });

  // 随机分组：每组自定义人数
  var groupRows = document.getElementById('group-rows');
  var groupAutoEven = true;  // 初次添加分组起，编辑前自动按名单平均分配

  function renumberRows() {
    var rows = groupRows.children;
    for (var i = 0; i < rows.length; i++) {
      rows[i].querySelector('.g-label').textContent = '组' + (i + 1);
    }
  }

  // 按当前组数将名单人数平均分配到各组（余数给前面几组）
  function distributeEvenly() {
    var rows = groupRows.children;
    var count = rows.length;
    if (count === 0 || names.length === 0) return;
    var base = Math.floor(names.length / count);
    var extra = names.length % count;
    // 名单人数少于组数时 base 为 0：至少填 1，交由后端按"人数不足"提示，
    // 避免自动填入 0 触发"每组人数必须大于 0"这种误导性报错
    for (var i = 0; i < count; i++) {
      rows[i].querySelector('input').value = Math.max(1, base + (i < extra ? 1 : 0));
      rows[i].querySelector('.g-names').textContent = '';
    }
  }

  function addGroupRow() {
    var row = document.createElement('div');
    row.className = 'group-row';
    var label = document.createElement('span');
    label.className = 'g-label';
    var input = document.createElement('input');
    input.type = 'number';
    input.min = '1';
    input.value = '1';
    var namesBox = document.createElement('span');
    namesBox.className = 'g-names';
    // 用户手动编辑任一输入框后，转入自定义模式，不再自动平均；旧名单失效清空
    input.addEventListener('input', function () {
      groupAutoEven = false;
      namesBox.textContent = '';
    });
    var rm = document.createElement('button');
    rm.className = 'rm-btn';
    rm.textContent = '删除';
    rm.addEventListener('click', function () {
      row.remove();
      renumberRows();
      if (groupAutoEven) distributeEvenly();
    });
    row.appendChild(label);
    row.appendChild(input);
    row.appendChild(namesBox);
    row.appendChild(rm);
    groupRows.appendChild(row);
    renumberRows();
    if (groupAutoEven) distributeEvenly();
  }

  // 默认两个分组，按名单人数平均
  addGroupRow();
  addGroupRow();
  document.getElementById('group-add-btn').addEventListener('click', addGroupRow);

  document.getElementById('group-btn').addEventListener('click', function () {
    var rows = groupRows.children;
    if (rows.length === 0) {
      document.getElementById('group-result').innerHTML =
        '<div class="err-msg">请先添加分组</div>';
      return;
    }
    var sizes = [];
    var invalid = false;
    for (var i = 0; i < rows.length; i++) {
      var v = parseInt(rows[i].querySelector('input').value, 10);
      if (!v || v < 1) { invalid = true; break; }
      sizes.push(v);
    }
    if (invalid) {
      document.getElementById('group-result').innerHTML =
        '<div class="err-msg">每组人数必须大于 0</div>';
      return;
    }
    callApi('create_groups', sizes).then(function (res) {
      var box = document.getElementById('group-result');
      if (!res || !res.ok) {
        box.innerHTML = '<div class="err-msg">' + (res.msg || '分组失败') + '</div>';
        return;
      }
      box.innerHTML = '';
      var spoken = [];
      var rows = groupRows.children;
      var idx = 0;  // 全局序号：各分组的人名依次弹出
      res.groups.forEach(function (members, i) {
        if (rows[i]) {
          var namesBox = rows[i].querySelector('.g-names');
          namesBox.textContent = '';
          members.forEach(function (nm) {
            var s = document.createElement('span');
            s.className = 'g-name';
            s.style.animationDelay = (idx * 0.05) + 's';
            s.textContent = nm;
            namesBox.appendChild(s);
            idx++;
          });
        }
        spoken.push('第' + (i + 1) + '组：' + members.join('、'));
      });
      speak(spoken.join('。'));
    });
  });

  function randomName() {
    return names[Math.floor(Math.random() * names.length)];
  }

  function start() {
    if (rolling || names.length === 0) return;
    rolling = true;
    btn.disabled = true;
    btn.textContent = '抽取中...';
    display.classList.remove('result');
    display.classList.add('rolling');
    clearTimers();

    // 总时长 2.4s：前段匀速快滚且高度虚化，最后 0.9s 逐渐减速并越来越清晰
    var elapsed = 0;
    var TOTAL = 2400;
    function step() {
      var remain = TOTAL - elapsed;
      var interval;
      if (remain > 900) {
        interval = 90;                              // 快速滚动
      } else {
        interval = 90 + (1 - remain / 900) * 210;   // 90ms 渐增至 300ms
      }
      var blur = (300 - interval) / 210 * 10;       // 同步：越快越糊，停下时清晰
      tick(randomName(), blur);
      elapsed += interval;
      if (elapsed >= TOTAL) { finish(); return; }
      timers.push(setTimeout(step, interval));
    }
    step();
  }

  function finish() {
    // 最终结果由后端给出，保证随机公平；动画只负责表现
    callApi('choose_name').then(function (name) {
      clearTimers();
      if (!name) {
        // 名单被清空等异常情况：不能把空字符串定格成"结果"
        display.classList.remove('rolling');
        btn.disabled = false;
        btn.textContent = '开 始 点 名';
        rolling = false;
        return;
      }
      var old = currentEl;
      if (old) {
        old.classList.remove('name-in');
        old.classList.add('name-out');
        setTimeout(function () { old.remove(); }, 300);
      }
      var el = document.createElement('span');
      el.className = 'name name-reveal';
      el.textContent = name;
      display.appendChild(el);
      currentEl = el;
      fitName(el);
      display.classList.remove('rolling');
      display.classList.add('result', 'expand');   // 定格为橙色渐变高亮，放大显示
      document.querySelector('h1').classList.add('shrink');
      // 更新 currentNameSize 供后续 tick 使用
      currentNameSize.fs = 24.2;
      currentNameSize.lh = 44;
      // 给所有现存名字元素设置新字号（CSS transition 平滑过渡）
      var els = display.querySelectorAll('.name');
      for (var i = 0; i < els.length; i++) {
        els[i].style.fontSize = '24.2vh';
        els[i].style.lineHeight = '44vh';
      }
      btn.disabled = false;
      btn.textContent = '再 来 一 次';
      rolling = false;
      speak(name + '被选中');
    });
  }

  window.addEventListener('pywebviewready', init);
  btn.addEventListener('click', start);
</script>
</body>
</html>"""


if __name__ == '__main__':
    log_env()
    # 单实例检查：已有实例在运行则立即退出
    if not _instance_lock.acquire():
        log('已有主程序实例在运行，本次启动退出', level='WARN')
        # 已有实例在运行，提示一下再退出
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, '程序已在运行', '随机点名工具', 0x40)
        except Exception as e:
            log(f'弹出提示框失败：{e}', level='WARN')
        sys.exit(0)

    try:
        api = Api()
        window = webview.create_window(
            '随机点名工具',
            html=HTML,
            js_api=api,
            width=1000,
            height=750,
            fullscreen=False,  # 默认窗口化，Esc 或"全屏 / 窗口"按钮切全屏
        )
        api.set_window(window)
        log('窗口已创建，进入主循环')
        webview.start()
    except Exception as e:
        log_exc(f'主程序异常退出：{e}')
        raise
    finally:
        _instance_lock.release()
        log('主程序已退出')
