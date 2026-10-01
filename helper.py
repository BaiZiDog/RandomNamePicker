# -*- coding: utf-8 -*-
"""Helper 更新程序 —— 图形界面版启动器

检查 GitHub Release 更新，应用后启动主程序。
版本判定：从 releases.atom（公开 XML，不走 API 配额）读取最新 tag，
与本地版本号做版本号比较（v{M}.{N} 或 v{M}.{N}.{字母补丁}）。

更新策略（单进程完成，不再需要独立的替换进程/进度窗口）：
  1. 版本检查：releases.atom 取最新 tag，与 LOCAL_VERSION 做版本号比较
  2. 下载 app.zip（代理兜底 + 分块多线程）
  3. 全量备份 BASE_DIR 到 _backup（仅用于失败回滚）
  4. 把待替换项统一改名 *.old 以腾出原路径 —— Windows 允许重命名运行中的
     可执行文件/动态库，因此这一步连 helper.exe 自身也能在运行中完成；
     新包解压到 _staging
  5. 内联把 _staging 的新文件搬到原路径（replace_staged，带重试）：
       成功 -> 删除 _backup，随后由 Helper 启动主程序
       失败 -> 交给退出后的 _replace.bat 兜底：它会再试一次，必要时从
               _backup 回滚，最后启动主程序
  6. 残留的 *.old 由下次启动 helper 时清理（cleanup_stale_old_files）——
     那时旧进程已退出、文件不再被占用，删除必然成功
  7. 硬约束（失败路径必须收敛）：任何失败分支都要落到"回滚"或"明确的失败态"，
     绝不允许带着"一半新、一半 *.old"的状态启动主程序；回滚结束时自检
     helper.exe 与主程序是否仍在位，把静默损坏转成日志里可见的 ERROR
"""
import os
import sys
import re
import time
import socket
import atexit
import zipfile
import tempfile
import subprocess
import shutil
import threading
import urllib.request
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk

GITHUB_REPO = 'BaiZiDog/RandomNamePicker'
# releases.atom：公开 XML 源，不占用 API 配额。
# API 未认证请求按 IP 限速 60 次/小时，反复调试或多次启动就会耗尽（HTTP 403）。
RELEASES_ATOM_URL = f'https://github.com/{GITHUB_REPO}/releases.atom'
# 更新包文件名的固定约定（atom 不含资产列表，按 tag 直接拼下载地址）
ZIP_ASSET_NAME = 'app.zip'
# 获取最新 tag 的单源超时（秒）：元数据请求很小，超时设短些，
# 某个源不可用时不至于让启动卡上十几秒
ATOM_TIMEOUT = 8
BASE_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, 'frozen', False) else __file__
))
MAIN_EXE = os.path.join(BASE_DIR, 'RandomNamePicker.exe')

# 当前版本号（硬编码，每次发版时同步更新）
LOCAL_VERSION = 'v1.5.a'

# 更新时【原样保留】的内容（不改名、不删除、不覆盖）。
# 注意：helper.exe 不在此列 —— 它需要在运行中被改名成 *.old 以腾出文件名
# （Windows 允许重命名运行中的可执行文件，镜像仍由已映射的句柄持有），
# 新版本随后直接落位到原路径，无需等待进程退出。
KEEP_ITEMS = {'data'}

# 备份目录名（仅失败回滚用，更新成功后由批处理删除）
BACKUP_DIR_NAME = '_backup'
# 本 Helper 自身的可执行文件名
SELF_NAME = os.path.basename(
    sys.executable if getattr(sys, 'frozen', False) else __file__
).lower()

# 命名 Mutex 名称（系统范围内唯一，用于单实例互斥）
MUTEX_NAME = 'RandomNamePicker.Helper.SingleInstance'
# 主程序的 Mutex 名，必须与 app.py 里的 MUTEX_NAME 完全一致
MAIN_MUTEX_NAME = 'RandomNamePicker.Main.SingleInstance'

# ---------------------------------------------------------------------------
# 下载相关配置
# ---------------------------------------------------------------------------

# GitHub 加速代理前缀（下载时优先尝试代理，全部失败后再回退直连）
# 用法：代理前缀 + 原始 GitHub URL
GITHUB_PROXIES = [
    'https://gh-proxy.com/',
    'https://ghfast.top/',
    'https://gh-proxy.org/',
]

# 超过该大小才启用多线程分块下载
MULTI_THREAD_THRESHOLD = 5 * 1024 * 1024      # 5 MB
# 每个线程负责的分片大小（线程数 = 文件大小 / 分片大小）
CHUNK_SIZE = 2 * 1024 * 1024                  # 2 MB
# 线程数上下限
MIN_THREADS = 2
MAX_THREADS = 16
# 常规请求超时（秒）：API 请求、探测等通用
DOWNLOAD_TIMEOUT = 60
# 分片下载失败重试次数
CHUNK_RETRY = 3
# 读超时（秒）：作为 urlopen 的 timeout 参数传给 _download_chunk/_download_single，
# 每次 resp.read() 超过该时长无数据即抛 socket.timeout（覆盖连接与单次读取）
STALL_TIMEOUT = 20
# 龟速判定：低于该速度（字节/秒）持续 STALL_TIMEOUT 秒则判定为"过慢"
MIN_SPEED = 30 * 1024

# ---------------------------------------------------------------------------
# 延时批处理替换配置
# ---------------------------------------------------------------------------

# 替换清单文件名：safe_extract() 写 -> check() 读 -> 读后立即删
REPLACE_MANIFEST = '_replace_manifest.txt'
# 已标记 *.old 的文件清单：clean_dir() 写 -> 下次启动 cleanup_stale_old_files() 读并删。
# 内容为磁盘上的完整名称（含 .old 后缀）。用于【精确清理】：
# 只删除本清单列出的 *.old，绝不碰用户自己放在程序目录的 .old 文件。
MARKED_MANIFEST = '_marked_manifest.txt'
# 被占用文件的临时后缀（“标记”阶段统一改名为此后缀，“清理”阶段据此识别删除）
OLD_SUFFIX = '.old'
# 延时替换报告文件名
REPLACE_REPORT = '_replace_report.txt'
# 兜底批处理日志文件名：批处理以 GBK（chcp 936）输出，单独成文件，
# 避免与 Python 侧 UTF-8 的 helper.log 追加到同一文件形成混合编码
REPLACE_BATCH_LOG = 'helper_batch.log'
# 延时替换批处理文件名
REPLACE_BAT = '_replace.bat'
# 批处理实际存放目录（放在 BASE_DIR 之外，避免回滚时的 for 循环把它自己删掉）
REPLACE_BAT_DIR = os.path.join(tempfile.gettempdir(), 'RandomNamePickerReplace')
# 新版本暂存目录（解压到这里，随后由 replace_staged 搬到目标位置）
STAGING_DIR = '_staging'

# 每批处理的文件数（批次大小）—— 仅用于兜底批处理
REPLACE_BATCH_SIZE = 32
# 批次之间的间隔秒数（给系统释放句柄留时间）
REPLACE_BATCH_INTERVAL = 0
# 单个文件替换的最大重试次数（内联替换与兜底批处理共用）
REPLACE_MAX_RETRY = 8
# 单次重试的等待秒数
REPLACE_RETRY_WAIT = 1
# 兜底批处理由 main() 的退出路径启动，先等 Helper 完全退出再开始搬运
REPLACE_INITIAL_WAIT = 1

# 自身 _MEI 残留目录标记文件 + 清理年龄门槛（秒）
# PyInstaller onefile 退出时若临时目录被占用（杀软扫描、子进程句柄等），
# bootloader 会弹 "Failed to remove temporary directory" 警告并留下 _MEI 垃圾目录。
# 每实例启动时在自己的 _MEI 里写标记，下次启动据此识别并清理本程序残留。
MEI_MARKER = '._randomnamepicker_mei'
MEI_CLEAN_MIN_AGE = 300

# 回滚标记文件名（兜底批处理回滚时写入，便于排查与下次启动时清理）
REPLACE_ROLLBACK_FLAG = '_replace_rollback.flag'

# ---------------------------------------------------------------------------
# 跨阶段状态文件契约（谁写、谁读、谁删、何时删）—— 集中记录，避免散落各处漏删
# ---------------------------------------------------------------------------
#   _replace_manifest.txt   写：safe_extract() | 读：check()            | 删：check() 读后立即删
#   _marked_manifest.txt    写：clean_dir()    | 读：cleanup_stale_old_*  | 删：cleanup 用后即删
#   _replace_rollback.flag  写：兜底批处理      | 读：仅人工排查          | 删：下次启动 cleanup_stale_replace_files()
#   _replace_report.txt     写：兜底批处理      | 读：仅人工排查          | 删：不删（保留证据）
#   _staging/               写：safe_extract() | 读：replace_staged()    | 删：成功/回滚/兜底/下次启动 cleanup
#   以上任一文件"漏删"都可能让下次启动误判流程状态，因此全部集中在
#   cleanup_stale_replace_files() 与 rollback() 两处收口。

# 回滚清空阶段的【额外】保留项（KEEP_ITEMS 之外）。
# 备份目录本身、日志、更新包与替换报告都要留下：备份用于手动还原，其余用于排查。
ROLLBACK_KEEP = {
    BACKUP_DIR_NAME, 'helper.log', 'app.log', 'app.zip',
    REPLACE_REPORT, REPLACE_BATCH_LOG, REPLACE_ROLLBACK_FLAG,
}

# 替换规则：按顺序匹配，第一条命中的规则决定该文件的处理方式
#   pattern : 正则表达式（匹配相对路径，大小写不敏感）
#   mode    : 'replace' 纳入替换清单 / 'skip' 跳过不处理 / 'delete' 删除
#   desc    : 规则说明（写入日志）
# 说明：'replace' 现在指"由 replace_staged() 内联替换" —— 标记阶段已把原文件
# 改名 *.old 腾出路径，因此运行中的 exe/dll 也能直接落位，不再需要"退出后替换"。
REPLACE_RULES = [
    # 自身可执行文件：纳入替换清单（原路径已腾空，可运行中落位）
    {'pattern': r'^helper\.exe$', 'mode': 'replace',
     'desc': 'Helper 自身，内联替换'},
    # 主程序：内联替换
    {'pattern': r'^RandomNamePicker\.exe$', 'mode': 'replace',
     'desc': '主程序，内联替换'},
    # 动态库：最容易被进程锁定，替换失败会重试并交兜底批处理
    {'pattern': r'\.(dll|pyd|so|dylib)$', 'mode': 'replace',
     'desc': '动态库，易被占用'},
    # 用户数据：绝不覆盖
    {'pattern': r'^data([\\/].*)?$', 'mode': 'skip',
     'desc': '用户数据目录，保留'},
    # 日志与备份：不处理
    {'pattern': r'^(helper\.log|app\.log|_backup([\\/].*)?)$', 'mode': 'skip',
     'desc': '日志/备份，保留'},
    # 临时文件：清理掉（match_replace_rule 用 re.search，无需 .* 前缀）
    {'pattern': r'.*\.(tmp|temp|bak|old)$', 'mode': 'delete',
     'desc': '临时文件，删除'},
    # 其余文件：默认延时替换
    {'pattern': r'.*', 'mode': 'replace',
     'desc': '默认规则'},
]

# 配色
BG = '#2b2b3d'
FG = '#ffffff'
ACCENT = '#667eea'
MUTED = '#9a9ab0'


def log(msg, level='INFO'):
    """写入日志文件便于排查。

    格式：[时间] [级别] [线程] 消息
    level: INFO / WARN / ERROR / DEBUG

    本函数统一以 UTF-8 写入 helper.log；兜底批处理侧以 GBK 写入
    helper_batch.log（REPLACE_BATCH_LOG），两个文件各自保持单一编码，
    排查时不必再做分段解码。
    """
    try:
        import time as _t
        ts = _t.strftime('%Y-%m-%d %H:%M:%S')
        ms = int((_t.time() % 1) * 1000)
        thread_name = threading.current_thread().name
        line = f'[{ts}.{ms:03d}] [{level:<5}] [{thread_name}] {msg}\n'
        with open(os.path.join(BASE_DIR, 'helper.log'), 'a', encoding='utf-8') as f:
            f.write(line)
    except OSError:
        pass


def log_exc(msg):
    """记录异常及其完整堆栈，便于定位问题。"""
    import traceback
    log(f'{msg}\n{traceback.format_exc()}', level='ERROR')


def _mark_mei():
    """在自身临时解压目录（sys._MEIPASS）写标记文件。

    供下次启动的 cleanup_stale_mei() 识别本程序残留的 _MEI 目录，
    避免误删其他 PyInstaller 程序（或正在运行实例）的临时目录。
    开发环境（未打包）下 sys._MEIPASS 不存在，直接返回。
    """
    base = getattr(sys, '_MEIPASS', None)
    if not base:
        return
    try:
        with open(os.path.join(base, MEI_MARKER), 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


def cleanup_stale_mei():
    """清理本程序上次运行残留的 _MEI 临时目录。

    触发背景：PyInstaller onefile 退出瞬间若临时目录被占用（杀软实时扫描、
    子进程句柄未释放等），bootloader 删除失败 → 弹 "Failed to remove
    temporary directory" 警告并留下垃圾目录。下次启动时据此回收。

    三重保护，避免误删：
      1) 只清理目录中含本程序标记文件 MEI_MARKER 的；
      2) 跳过创建不超过 MEI_CLEAN_MIN_AGE 秒的目录（可能仍在运行中）；
      3) 必须在获取到单实例 Mutex 之后再调用 —— 同一时刻只有唯一一个实例
         会执行清理，不会出现两个实例互相删除对方临时目录的情况。
    """
    tmp = tempfile.gettempdir()
    own = os.path.abspath(getattr(sys, '_MEIPASS', '') or '')
    now = time.time()
    try:
        names = os.listdir(tmp)
    except OSError:
        return
    for name in names:
        if not name.startswith('_MEI'):
            continue
        path = os.path.join(tmp, name)
        try:
            if not os.path.isdir(path):
                continue
            if os.path.abspath(path) == own:       # 正在运行的自己
                continue
            if now - os.path.getmtime(path) < MEI_CLEAN_MIN_AGE:
                continue                           # 可能仍在运行
            if not os.path.isfile(os.path.join(path, MEI_MARKER)):
                continue                           # 其他程序的目录，不动
        except OSError:
            continue
        try:
            shutil.rmtree(path)
            log(f'已清理残留临时目录：{name}', level='WARN')
        except OSError as e:
            log(f'清理残留临时目录失败（下次再试）：{name}：{e}', level='DEBUG')


def cleanup_stale_replace_files():
    """清理上次异常中断残留的替换状态文件与暂存目录。

    背景：_replace_manifest.txt 曾"只写不删"，残留清单会让下次启动
    误判为有替换任务 —— 即使本次"已是最新版本"，也会跑延时替换批处理，
    把 helper.exe 改名 .old 后当垃圾清掉，且无 _backup 可回滚。

    安全前提：只要兜底批处理存在就跳过 —— 它可能正在搬运文件
    （此时删 _staging 会让替换因"暂存缺失"失败并触发回滚）。
    批处理正常结束会自删，故"下次启动时它已不存在"是常态；
    若它因异常残留，也只会让本次残留清理跳过一轮，不会造成损坏。
    批处理在但 _staging 不在的那些瞬间（搬运中会清空 _staging），
    同样按"仍在进行"处理，避免误删。

    跨进程/跨阶段的状态文件必须明确"谁写、谁读、谁删、何时删"：
      写：safe_extract()  →  读：check()（内联替换前）  →  删：读取后立即删
      本函数是"异常中断导致漏删"时的兜底。
    """
    bat_path = os.path.join(REPLACE_BAT_DIR, REPLACE_BAT)
    if os.path.exists(bat_path):
        log('疑似兜底替换仍在进行（批处理存在），跳过残留清理', level='WARN')
        return

    for path, desc in (
        (os.path.join(BASE_DIR, REPLACE_MANIFEST), '替换清单'),
        (os.path.join(BASE_DIR, REPLACE_ROLLBACK_FLAG), '回滚标志'),
    ):
        try:
            if os.path.exists(path):
                os.remove(path)
                log(f'已清理残留{desc}：{path}', level='WARN')
        except OSError as e:
            log(f'清理残留{desc}失败：{e}', level='WARN')

    try:
        staging = os.path.join(BASE_DIR, STAGING_DIR)
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
            log(f'已清理残留暂存目录：{staging}', level='WARN')
    except OSError as e:
        log(f'清理残留暂存目录失败：{e}', level='WARN')


# 注意：helper.py 的【标记】阶段只把待替换/待清理项改名 *.old，不删除；
# 删除由下次启动时的 cleanup_stale_old_files() 完成（见该函数说明）。
# 之所以不在退出后的批处理里删：进程退出时机不可控（系统可能仍持有映像句柄），
# helper.exe.old / VCRUNTIME140*.dll.old 常因仍被占用而删除失败。


def cleanup_stale_old_files():
    """清理上次更新残留的 *.old 文件（【清理】阶段）。

    背景：更新时 clean_dir() 把待替换/待清理项统一改名 *.old（规避占用），
    真正的删除推迟到"下次启动 helper"时执行 —— 那时旧进程早已退出，
    文件不再被占用，删除必然成功。残留的 helper.exe.old 会让下次更新的
    标记逻辑"放弃标记"，导致新 helper.exe 无法就位、更新静默失败。

    为什么不在退出后的批处理里删：批处理只能固定等待若干秒，而进程退出
    时机不可控，helper.exe.old / VCRUNTIME140*.dll.old 常因仍被占用而失败。

    清理范围【只限 MARKED_MANIFEST 记录的条目】（由 clean_dir 写入）：
    用户自己放在程序目录里的 .old 文件因此不会被误删；若清单不存在
    （本次没更新过，或回滚已把 .old 清空），本函数不做任何删除。

    时机：必须在获取单实例 Mutex 之后调用 —— 此时无其他 helper 实例。
    _backup 目录内的 *.old 是回滚依据，绝不触碰。
    """
    manifest_path = os.path.join(BASE_DIR, MARKED_MANIFEST)
    if not os.path.exists(manifest_path):
        log('无待清理清单，跳过 .old 清理', level='DEBUG')
        return
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            names = [line.strip() for line in f if line.strip()]
    except OSError as e:
        log(f'读取待清理清单失败：{e}，跳过 .old 清理', level='WARN')
        return

    remaining = []
    for name in names:
        # 双保险：只处理带 .old 后缀的顶层项
        if not name.endswith(OLD_SUFFIX):
            continue
        path = os.path.join(BASE_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            log(f'已清理残留：{name}', level='WARN')
        except OSError as e:
            remaining.append(name)
            log(f'清理残留 {name} 失败（下次再试）：{e}', level='WARN')

    # 全部成功才删除清单；仍有残留则回写剩余项，下次启动继续尝试
    try:
        if remaining:
            with open(manifest_path, 'w', encoding='utf-8') as f:
                for name in remaining:
                    f.write(f'{name}\n')
        else:
            os.remove(manifest_path)
            log(f'已删除待清理清单：{manifest_path}', level='DEBUG')
    except OSError as e:
        log(f'更新待清理清单失败：{e}', level='WARN')


# ---------------------------------------------------------------------------
# 替换规则匹配引擎
# ---------------------------------------------------------------------------

def match_replace_rule(rel_path):
    """按 REPLACE_RULES 顺序匹配相对路径，返回命中的规则字典。

    支持正则表达式匹配（大小写不敏感）。未命中任何规则时返回默认 replace。
    使用 re.search：与 re.match（仅从开头匹配）不同，
    像 ".dll" / ".pyd" 这类无 .* 前缀的模式也能命中路径中任意位置的扩展名。
    """
    normalized = rel_path.replace('/', '\\')
    for rule in REPLACE_RULES:
        try:
            if re.search(rule['pattern'], normalized, re.IGNORECASE):
                return rule
        except re.error as e:
            log(f'规则正则无效，跳过：{rule.get("pattern")}（{e}）', level='WARN')
    return {'pattern': '.*', 'mode': 'replace', 'desc': '兜底默认'}


def classify_files(rel_paths):
    """把文件列表按规则分类。

    返回 (to_replace, to_skip, to_delete)，均为 [(相对路径, 规则说明)] 列表。
    """
    to_replace, to_skip, to_delete = [], [], []
    for rel in rel_paths:
        rule = match_replace_rule(rel)
        mode = rule.get('mode', 'replace')
        item = (rel, rule.get('desc', ''))
        if mode == 'skip':
            to_skip.append(item)
        elif mode == 'delete':
            to_delete.append(item)
        else:
            to_replace.append(item)
    return to_replace, to_skip, to_delete


def log_env():
    """记录运行环境信息，便于排查平台相关问题。"""
    log('-' * 60)
    log(f'Helper 启动 | 版本={LOCAL_VERSION} | PID={os.getpid()}')
    log(f'Python={sys.version.split()[0]} | 平台={sys.platform} | frozen={getattr(sys, "frozen", False)}')
    log(f'BASE_DIR={BASE_DIR}')
    log(f'可执行文件={sys.executable}')
    log(f'自身文件名={SELF_NAME}')
    log(f'主程序路径={MAIN_EXE}（存在={os.path.exists(MAIN_EXE)}）')
    log(f'工作目录={os.getcwd()}')
    log(f'Mutex 名：helper={MUTEX_NAME} | main={MAIN_MUTEX_NAME}')
    log('-' * 60)


# ---------------------------------------------------------------------------
# 单实例互斥：命名 Mutex
# ---------------------------------------------------------------------------

class SingleInstance:
    """基于 Windows 命名 Mutex 的跨进程单实例锁。

    CreateMutexW + GetLastError 判定。获取失败即表示已有实例在运行。
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


def _candidate_urls(url, direct_first=False):
    """候选请求地址：默认代理优先、直连兜底。

    direct_first=True 时改为直连优先 —— 适用于 GitHub 加速代理不支持的
    路径（实测 releases.atom 在代理上返回 404，白等超时），此时直连最快。
    返回 [(源名, 地址), ...]。
    """
    candidates = [(p, p + url) for p in GITHUB_PROXIES]
    direct = ('直连', url)
    return [direct] + candidates if direct_first else candidates + [direct]


def _asset_url(tag, name):
    """按 tag 直接拼资产下载地址。

    atom 不含资产列表，因此下载地址由约定名拼出：
      https://github.com/{repo}/releases/download/{tag}/{name}
    """
    return f'https://github.com/{GITHUB_REPO}/releases/download/{tag}/{name}'


def _parse_atom_tags(xml_bytes):
    """从 releases.atom 的 XML 中提取所有 entry 的 title（即 tag 列表）。

    用命名空间无关的后缀匹配（endswith），避免 GitHub 改命名空间前缀时失效。
    """
    root = ET.fromstring(xml_bytes)
    tags = []
    for entry in root.iter():
        if not entry.tag.endswith('entry'):
            continue
        for child in entry:
            if child.tag.endswith('title'):
                tag = (child.text or '').strip()
                if tag:
                    tags.append(tag)
                break
    return tags


def get_latest_tag():
    """从 releases.atom 获取最新 tag（取版本号最大者）。

    为什么不用 API：未认证的 GitHub API 按 IP 限速 60 次/小时，
    反复调试或用户多次启动就会耗尽，之后所有请求返回 403，
    更新流程直接瘫痪。releases.atom 是公开 XML，无速率限制、无需 Token。

    为什么不是"取第一条"：atom 条目按【创建/更新时间】倒序，而不是按版本。
    实测把 v1.5-fix 重命名为 v1.5.a 后，旧版本立刻排到了第一条。
    若据此判断"最新版本"，重新打过旧版本 tag 就会让助手看不到真正的新版本。
    因此收集全部 tag、按版本号取最大者（无法解析的 tag 直接忽略）。

    直连优先：实测 releases.atom 路径在加速代理上返回 404，代理优先会白等
    超时（十几秒）；直连不可用时再回退代理。
    """
    for name, src in _candidate_urls(RELEASES_ATOM_URL, direct_first=True):
        try:
            log(f'获取最新版本（{name}）：{src}', level='DEBUG')
            with _open_url(src, timeout=ATOM_TIMEOUT) as resp:
                tags = _parse_atom_tags(resp.read())
        except Exception as e:
            log(f'获取最新 tag 失败（来源={name}）：{e}', level='WARN')
            continue

        if not tags:
            log(f'atom 中没有 entry（来源={name}）', level='WARN')
            continue

        candidates = [(p, t) for t in tags for p in [_parse_version(t)] if p]
        if not candidates:
            log(f'atom 中的 tag 均不符合命名规则：{tags}', level='WARN')
            continue

        latest = max(candidates)[1]
        log(f'atom 共 {len(tags)} 个 tag，版本号最大者为 {latest}'
            f'（来源={name}）', level='DEBUG')
        return latest

    log('所有来源均无法获取最新 tag', level='ERROR')
    return None


def _parse_version(s):
    """解析版本号：v{M}.{N}[.{X}] -> (M, N, X)，失败返回 None。

    X 是单字母补丁（a-z）；无补丁返回空串，空串在字符串比较中小于任何字母，
    因此天然满足 '' < 'a' < 'b' < ... 的语义。
    """
    m = re.match(r'^v?(\d+)\.(\d+)(?:\.([A-Za-z]))?$', (s or '').strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), (m.group(3) or '').lower()


def _version_gt(a, b):
    """判断版本 a 是否比 b 新：先比主版本，再比次版本，最后比字母补丁。

    约定：v{M}.{N} 或 v{M}.{N}.{X}，X 为单字母。补丁用满 z 后进位到
    v{M}.{N+1}。任一版本号解析失败时返回 False（保守：不触发更新）。
    """
    pa, pb = _parse_version(a), _parse_version(b)
    if not pa or not pb:
        log(f'版本号无法解析，跳过比较：{a} / {b}', level='WARN')
        return False
    if pa[0] != pb[0]:
        return pa[0] > pb[0]
    if pa[1] != pb[1]:
        return pa[1] > pb[1]
    return pa[2] > pb[2]


def _open_url(url, headers=None, timeout=DOWNLOAD_TIMEOUT):
    """发起请求并返回响应对象。

    超时统一通过 urlopen 的 timeout 参数设置：
      - 覆盖 TCP 连接阶段（connect timeout）；
      - 也覆盖 socket 级超时——每次 resp.read() 阻塞超过 timeout 秒
        无数据时会抛 socket.timeout，服务端"连上但不发数据"也能及时失败。

    注意：不能用 resp.fp.raw._sock.settimeout() 这种写法，它依赖 CPython
    http.client 的内部实现细节，在 Python 3.10/3.11/3.12+ 中层次路径不同，
    且某些 SSL 实现下路径也存在差异，无法跨版本稳定工作。
    """
    import urllib.error
    hdrs = {'User-Agent': 'RandomNamePicker-Helper'}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    return urllib.request.urlopen(req, timeout=timeout)


def _is_timeout(e):
    """判断异常是否为超时。

    urllib 会把 socket.timeout 包装进 URLError（e.reason 才是真正的异常），
    所以需要层层拆开判断，否则超时会被当成普通失败误分类。
    """
    import urllib.error
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    if isinstance(e, urllib.error.URLError):
        return _is_timeout(e.reason)
    return False


def _probe(url, timeout=10):
    """探测 URL 是否可用，返回 (可用, 文件总大小, 是否支持 Range)。

    优先用 HEAD 请求——不传输正文，省流量且更快；
    服务端不支持 HEAD（405/501 等）或 HEAD 拿不到有效大小时，
    回退到 GET + Range（bytes=0-0）探测。

    Range 支持度判断：HEAD 响应带 Accept-Ranges: bytes 即认为支持；
    看不到该头则保守按不支持处理（走单线程下载，慢但正确）。
    """
    # 1) HEAD 优先，减少不必要的数据传输
    try:
        req = urllib.request.Request(
            url, headers={'User-Agent': 'RandomNamePicker-Helper'}, method='HEAD')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get('Content-Length')
            total = int(length) if length else 0
            accept_ranges = (resp.headers.get('Accept-Ranges') or '').lower()
            support_range = accept_ranges == 'bytes'
            if total > 0:
                return True, total, support_range
            # total 为 0（部分 CDN/代理对 HEAD 不返回长度）时继续 GET 探测
    except Exception as e:
        log(f'HEAD 探测失败 {url}：{e}', level='DEBUG')
    # 2) 回退：GET + Range 探测
    try:
        with _open_url(url, headers={'Range': 'bytes=0-0'}, timeout=timeout) as resp:
            support_range = resp.status == 206
            if support_range:
                cr = resp.headers.get('Content-Range', '')
                total = int(cr.split('/')[-1]) if '/' in cr else 0
            else:
                length = resp.headers.get('Content-Length')
                total = int(length) if length else 0
            return True, total, support_range
    except Exception as e:
        log(f'探测失败 {url}：{e}', level='DEBUG')
        return False, 0, False


def _calc_threads(total_size):
    """按文件大小自适应计算线程数（参考 PCL2：分片驱动）。"""
    if total_size <= 0:
        return MIN_THREADS
    n = total_size // CHUNK_SIZE
    n = max(MIN_THREADS, min(MAX_THREADS, int(n)))
    return n


class _SpeedWatchdog:
    """速度看门狗：监控下载进度，长时间无进展或龟速时判定为卡死。

    覆盖两种"连接还活着但没有进展"的场景：
      1) 完全无数据：time.time() 之后长时间没有新增字节（服务器挂起）；
      2) 龟速：有数据但长期低于 MIN_SPEED（服务器带宽被限死）。
    这两种情况 socket 层都可能不抛异常，只能靠速度判定。
    """

    def __init__(self, name):
        self.name = name
        self._last_bytes = 0
        self._last_time = time.time()
        self._slow_since = None          # 开始持续龟速的时间戳（None=当前不慢）
        self._lock = threading.Lock()
        self.stalled = False

    def update(self, total_bytes):
        """由下载线程汇报当前累计字节数，顺带计算本次间隔的速度。"""
        with self._lock:
            now = time.time()
            if total_bytes > self._last_bytes:
                elapsed = now - self._last_time
                speed = ((total_bytes - self._last_bytes) / elapsed
                         if elapsed > 0 else float('inf'))
                self._last_bytes = total_bytes
                self._last_time = now
                if speed < MIN_SPEED:
                    # 进入/维持龟速状态（记录起点时间戳）
                    if self._slow_since is None:
                        self._slow_since = now
                else:
                    self._slow_since = None

    def check(self):
        """检查是否卡死。返回 True 表示应中止当前下载源。"""
        with self._lock:
            now = time.time()
            # 场景 1：长时间完全没有新数据
            if now - self._last_time >= STALL_TIMEOUT:
                self.stalled = True
                return True
            # 场景 2：持续龟速（有数据但速度长期低于 MIN_SPEED）
            if (self._slow_since is not None
                    and now - self._slow_since >= STALL_TIMEOUT):
                self.stalled = True
                return True
            return False

    def start(self):
        """启动后台监控线程。"""
        def loop():
            while not self.stalled:
                time.sleep(1)
                if self.check():
                    log(f'下载源 {self.name} 连续 {STALL_TIMEOUT}s 无进展'
                        f'或速度低于 {MIN_SPEED / 1024:.0f} KB/s，'
                        f'判定为卡死，切换下一个源', level='WARN')
                    return
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t


def _download_chunk(url, dest, start, end, progress_cb, lock, counter, watchdog):
    """下载 [start, end] 区间的分片并写入文件对应位置。

    返回实际写入字节数；失败抛异常。看门狗判定卡死时立即放弃，不重试。
    通过 timeout=STALL_TIMEOUT 实现 socket 级读超时：
      每次 resp.read() 阻塞超过 STALL_TIMEOUT 秒无数据即抛 socket.timeout，
      与上层看门狗构成双层保险（看门狗兜底"socket 没抛异常但连接已失效"）。
    """
    headers = {'Range': f'bytes={start}-{end}'}
    last_err = None
    for attempt in range(CHUNK_RETRY):
        if watchdog.stalled:
            raise Exception('下载源卡死，中止')
        try:
            with _open_url(url, headers=headers, timeout=STALL_TIMEOUT) as resp:
                if resp.status not in (200, 206):
                    raise Exception(f'HTTP {resp.status}')
                dest.seek(start)
                written = 0
                while True:
                    if watchdog.stalled:
                        raise Exception('下载源卡死，中止')
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    dest.write(chunk)
                    written += len(chunk)
                    with lock:
                        counter[0] += len(chunk)
                        if progress_cb:
                            progress_cb(counter[0])
                    watchdog.update(counter[0])
                return written
        except Exception as e:
            last_err = e
            # 卡死或读超时：不再重试，直接放弃该源
            if watchdog.stalled or _is_timeout(e):
                watchdog.stalled = True
                raise Exception(f'下载源卡死或读超时：{last_err}')
            log(f'分片 {start}-{end} 第 {attempt + 1} 次失败：{e}', level='WARN')
    raise Exception(f'分片 {start}-{end} 下载失败：{last_err}')


def _download_multi(url, dest, total, progress_cb, watchdog):
    """多线程分块下载（分片并发，任一失败即整体中止）。"""
    threads_n = _calc_threads(total)
    log(f'启用多线程下载：{threads_n} 线程，分片 {CHUNK_SIZE // 1024 // 1024} MB，'
        f'总大小 {total / 1024 / 1024:.2f} MB')

    # 预分配文件空间，避免多线程写入时反复扩展
    with open(dest, 'wb') as f:
        f.truncate(total)

    lock = threading.Lock()
    counter = [0]
    sem = threading.Semaphore(threads_n)
    errors = []

    # 切分任务
    tasks = []
    pos = 0
    while pos < total:
        end = min(pos + CHUNK_SIZE - 1, total - 1)
        tasks.append((pos, end))
        pos = end + 1

    def worker(start, end):
        if errors:
            return
        with sem:
            if errors:
                return
            try:
                with open(dest, 'r+b') as f:
                    _download_chunk(url, f, start, end, progress_cb,
                                    lock, counter, watchdog)
            except Exception as e:
                with lock:
                    # 分片内部已自含重试与备用源切换，走到这里即彻底失败，
                    # 记录后中止其余任务（半成品文件由调用方删除）
                    errors.append(e)

    ts = []
    for start, end in tasks:
        if errors:
            break
        t = threading.Thread(target=worker, args=(start, end), daemon=True)
        t.start()
        ts.append(t)
        # 已启动线程数到达并发上限时等待，避免一次性创建全部线程
        while sum(1 for x in ts if x.is_alive()) >= threads_n:
            if errors:
                break
            time.sleep(0.05)

    for t in ts:
        t.join()

    if errors:
        raise errors[0]

    log(f'多线程下载完成：{counter[0]} 字节')


def _download_single(url, dest, total, progress_cb, watchdog):
    """单线程流式下载（小文件或服务端不支持 Range 时使用）。

    同样使用 timeout=STALL_TIMEOUT 做 socket 级读超时，
    每次 resp.read() 无数据超过该时长即抛 socket.timeout 中止。
    """
    log('使用单线程下载')
    done = 0
    with _open_url(url, timeout=STALL_TIMEOUT) as resp:
        with open(dest, 'wb') as f:
            while True:
                if watchdog.stalled:
                    raise Exception('下载源卡死，中止')
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done)
                watchdog.update(done)
    log(f'单线程下载完成：{done} 字节')


def download_file(url, dest, progress_cb=None, size_cb=None):
    """下载文件：代理优先，全部失败回退直连；大文件多线程分块。

    progress_cb 接收"已下载字节数"（不是比例），由调用方换算。
    size_cb 在探到文件总大小时被调用（atom 渠道拿不到文件大小，
    进度条需要它来换算比例，否则分母恒为 0、进度条不动）。
    每个源都有速度看门狗（无进展/龟速判定），会自动切换下一个源。
    """
    log(f'开始下载：{url}')
    log(f'保存到：{dest}')

    last_err = None
    for name, src in _candidate_urls(url):
        log(f'尝试下载源：{name} -> {src}')
        ok, total, support_range = _probe(src)
        if not ok:
            log(f'下载源不可用：{name}', level='WARN')
            continue

        if size_cb and total > 0:
            size_cb(total)
        log(f'下载源可用：{name} | 大小={total} 字节 '
            f'({total / 1024 / 1024:.2f} MB) | 支持 Range={support_range}')

        watchdog = _SpeedWatchdog(name)
        watchdog.start()
        t0 = time.time()
        try:
            if total >= MULTI_THREAD_THRESHOLD and support_range:
                _download_multi(src, dest, total, progress_cb, watchdog)
            else:
                if total >= MULTI_THREAD_THRESHOLD:
                    log('服务端不支持 Range，降级为单线程', level='WARN')
                _download_single(src, dest, total, progress_cb, watchdog)

            size = os.path.getsize(dest)
            if total and size != total:
                raise Exception(f'大小不符：期望 {total}，实际 {size}')

            elapsed = time.time() - t0
            speed = size / elapsed / 1024 if elapsed > 0 else 0
            log(f'下载成功（源={name}，{size} 字节，耗时 {elapsed:.2f}s，'
                f'平均 {speed:.1f} KB/s）')
            return
        except Exception as e:
            last_err = e
            log_exc(f'下载源 {name} 失败：{e}')
            if os.path.exists(dest):
                try:
                    os.remove(dest)
                except OSError:
                    pass

    raise Exception(f'所有下载源均失败：{last_err}')


# ---------------------------------------------------------------------------
# 更新核心：备份 -> 清理 -> 解压
# ---------------------------------------------------------------------------

def _abs(p):
    return os.path.abspath(p)


def _is_running_self(path):
    return _abs(path).lower() == _abs(os.path.join(BASE_DIR, SELF_NAME)).lower()


def backup_current(backup_root, exclude=None):
    """把 BASE_DIR 下所有内容备份到 backup_root（排除 _backup 自身）。

    exclude: 额外排除的文件/目录名集合（如正在下载的 app.zip）。
    被占用的文件跳过并记录，不中止整体流程。
    返回被跳过的条目列表。
    """
    os.makedirs(backup_root, exist_ok=True)
    exclude = exclude or set()
    items = [i for i in os.listdir(BASE_DIR)
             if i != BACKUP_DIR_NAME and i not in exclude]
    log(f'开始备份 {len(items)} 项到 {backup_root}', level='DEBUG')
    skipped = []
    for item in items:
        src = os.path.join(BASE_DIR, item)
        dst = os.path.join(backup_root, item)
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                shutil.copytree(src, dst, symlinks=True)
                log(f'  备份目录：{item}', level='DEBUG')
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
                log(f'  备份文件：{item}（{os.path.getsize(src)} 字节）', level='DEBUG')
        except OSError as e:
            # 被占用（WinError 32/33）等：跳过，不中止
            skipped.append(item)
            log(f'备份 {item} 失败（跳过）：{e}', level='WARN')

    if skipped:
        log(f'备份完成，{len(skipped)} 项被占用跳过：{skipped}', level='WARN')
    return skipped


def clean_dir(keep=None):
    """【标记】阶段：把 BASE_DIR 下待替换/待清理的项统一改名为 *.old，不直接删除。

    keep: 额外保留的文件/目录名集合（如正在使用的更新包 app.zip）。

    "标记-清理"两阶段设计：
      - 标记（本函数）：只改名加 .old 后缀，绝不删除。被占用（WinError 5/32/33）
        的文件一般也能改名成功，因此本阶段几乎总是全部成功；改名后原路径即空出，
        新文件可直接落位（Windows 允许重命名运行中的可执行文件/动态库）。
      - 清理：推迟到【下次启动 helper】由 cleanup_stale_old_files() 执行 ——
        那时旧进程早已退出、文件不再被占用，删除必然成功。
        本函数把已标记项写入 MARKED_MANIFEST，清理阶段只删清单内的条目，
        不会误删用户自己放在程序目录里的 .old 文件。

    返回 (marked, kept)：
      marked - 已改名标记为 .old 的项列表
      kept   - 本次保留的项列表
    """
    keep = keep or set()
    marked = []
    kept = []
    failed = []
    for item in os.listdir(BASE_DIR):
        if item.lower() in KEEP_ITEMS:      # 大小写不敏感匹配（data）
            kept.append(item)
            continue
        if item in (BACKUP_DIR_NAME, 'helper.log') or item in keep:
            kept.append(item)
            continue
        if item.endswith(OLD_SUFFIX):
            # 已有 .old 残留（上次清理失败的），不重复标记，留给批处理清理
            kept.append(item)
            continue
        path = os.path.join(BASE_DIR, item)
        new_path = path + OLD_SUFFIX
        # 目标 .old 已存在（可能被占用删不掉）则本次改名失败，跳过
        try:
            if os.path.exists(new_path):
                try:
                    os.remove(new_path)
                except OSError:
                    pass
            os.replace(path, new_path)
            marked.append(item)
            log(f'  已标记：{item} -> {item}{OLD_SUFFIX}', level='DEBUG')
        except OSError as e:
            failed.append(item)
            log(f'标记 {item} 失败：{e}（由批处理兜底）', level='WARN')

    log(f'清理结果：标记 {len(marked)} 项，保留 {len(kept)} 项，'
        f'标记失败 {len(failed)} 项')
    if kept:
        log(f'  保留：{kept}', level='DEBUG')

    # 记录本次标记的项（磁盘上的 *.old 名称），供下次启动【精确清理】。
    # 必须写改名后的全名：清理端按 ".old" 后缀过滤，写原名会被整体跳过。
    try:
        with open(os.path.join(BASE_DIR, MARKED_MANIFEST), 'w',
                  encoding='utf-8') as f:
            for name in marked:
                f.write(f'{name}{OLD_SUFFIX}\n')
        log(f'已记录待清理清单：{MARKED_MANIFEST}（{len(marked)} 项）',
            level='DEBUG')
    except OSError as e:
        log(f'写入待清理清单失败：{e}（这些 .old 将由下次标记时顺带删除）',
            level='WARN')

    return marked, kept


def safe_extract(zip_path, target_dir):
    """安全解压，防 Zip Slip。

    所有文件先解压到暂存目录 _staging，再按替换规则分类：
      - replace：写入替换清单，由 replace_staged() 内联搬到目标路径
      - skip   ：不处理（用户数据等）
      - delete ：直接删除暂存文件

    这样即使目标文件被占用，也不影响解压本身（替换阶段带重试，失败再交兜底批处理）。

    返回 (替换清单, 跳过清单, 删除清单)，均为 [(相对路径, 规则说明)]。
    解压失败或清单写入失败都会抛异常 —— 调用方（apply_update）据此回滚，
    绝不能"假装成功"，否则原文件已被改成 *.old，安装目录会被清空。
    """
    abs_target = _abs(target_dir)
    staging = os.path.join(BASE_DIR, STAGING_DIR)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    with zipfile.ZipFile(zip_path, 'r') as zf:
        # 完整性校验：对全部条目做 CRC 校验，可发现"下载被截断/内容损坏"的包。
        # （比 SHA256 轻量，且不依赖外部摘要，避免引入无数据源的空校验。）
        bad = zf.testzip()
        if bad is not None:
            raise Exception(f'更新包损坏（CRC 校验失败）：{bad}')
        members = zf.namelist()
        log(f'开始解压 {len(members)} 个条目到暂存目录（CRC 校验通过）',
            level='DEBUG')

        # 防 Zip Slip
        for member in members:
            member_path = _abs(os.path.join(abs_target, member))
            if member_path != abs_target and not member_path.startswith(abs_target + os.sep):
                log(f'检测到非法压缩路径：{member}', level='ERROR')
                raise Exception(f'非法压缩路径：{member}')

        # 解压到暂存目录（不碰目标文件，避免占用问题）
        extracted = []
        failed = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            try:
                zf.extract(info, staging)
                extracted.append(info.filename.replace('/', os.sep))
            except OSError as e:
                # 解压失败的文件不会进入清单，也就不会被批处理计为 FAIL。
                # 必须单独记录：否则主程序会因漏装文件而损坏却"更新成功"。
                failed.append(info.filename)
                log(f'解压 {info.filename} 到暂存失败：{e}', level='WARN')

    log(f'暂存解压完成：{len(extracted)} 个文件'
        + (f'，失败 {len(failed)} 个：{failed}' if failed else ''))
    if failed:
        raise Exception(f'解压失败 {len(failed)} 个文件：{failed}')

    # 按规则分类
    to_replace, to_skip, to_delete = classify_files(extracted)
    log(f'规则分类：替换 {len(to_replace)} | 跳过 {len(to_skip)} | 删除 {len(to_delete)}')

    # 删除类：直接删掉暂存文件
    for rel, desc in to_delete:
        try:
            os.remove(os.path.join(staging, rel))
            log(f'  删除暂存文件：{rel}（{desc}）', level='DEBUG')
        except OSError as e:
            log(f'  删除暂存文件 {rel} 失败：{e}', level='WARN')

    # 写入替换清单（供 check() 的内联替换阶段读取）
    # 写失败必须上抛：此时原文件已被改成 *.old，若继续按"更新成功"处理，
    # 调用方会删掉 _backup 与 _staging，安装目录将被清空且无法回滚。
    manifest_path = os.path.join(BASE_DIR, REPLACE_MANIFEST)
    try:
        with open(manifest_path, 'w', encoding='utf-8') as f:
            for rel, desc in to_replace:
                f.write(f'{rel}\t{desc}\n')
        log(f'已写入替换清单：{manifest_path}（{len(to_replace)} 项）')
    except OSError as e:
        log_exc(f'写入替换清单失败：{e}')
        raise Exception(f'写入替换清单失败：{e}')

    return to_replace, to_skip, to_delete


def rollback(backup_path):
    """从备份回滚：先清空（保留白名单项）再从 _backup 还原。

    被占用的文件跳过并记录，不中止；结束时做一次"关键文件不变量自检"，
    把静默损坏转成日志里显式可见的 ERROR。
    """
    log(f'开始回滚，来源：{backup_path}', level='WARN')
    if not os.path.isdir(backup_path):
        log(f'备份目录不存在，无法回滚：{backup_path}', level='ERROR')
        return

    # 1. 清空（尽力而为）
    # 保留项 = KEEP_ITEMS（用户数据 data）∪ ROLLBACK_KEEP（备份/日志/更新包/报告）。
    # 用显式白名单而不是"只保护 data"，让"删什么、留什么"一目了然。
    cleared = 0
    for item in os.listdir(BASE_DIR):
        if item.lower() in KEEP_ITEMS or item in ROLLBACK_KEEP:
            log(f'  跳过保留项：{item}', level='DEBUG')
            continue
        path = os.path.join(BASE_DIR, item)
        if _is_running_self(path):
            # 仅当标记阶段没能腾出自身路径时才走到这里（那时它仍是运行中的镜像）
            log(f'  跳过正在运行的自己：{item}', level='DEBUG')
            continue
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            cleared += 1
        except OSError as e:
            log(f'回滚清理 {item} 失败（跳过）：{e}', level='WARN')
    log(f'  回滚清空完成：{cleared} 项', level='DEBUG')

    # 2. 还原（尽力而为）
    # 注意：这里【不能】再用 _is_running_self(dst) 跳过 ——
    # 标记阶段（clean_dir）已把 BASE_DIR 下的 helper.exe 改名成 helper.exe.old，
    # 目标路径此刻是空的，写入完全合法（运行中的镜像由已映射句柄持有，与路径无关）。
    # 旧实现把该目标当成"正在运行的自己"跳过，而唯一被跳过的恰好就是 helper.exe，
    # 于是回滚后更新器永久消失，用户再也打不开更新入口。
    restored = 0
    failed = []
    for item in os.listdir(backup_path):
        src = os.path.join(backup_path, item)
        dst = os.path.join(BASE_DIR, item)
        try:
            if os.path.isdir(src) and not os.path.islink(src):
                # dirs_exist_ok=True：目标可能已存在（清空时被占用的没删掉）
                shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
            restored += 1
        except OSError as e:
            failed.append(item)
            log(f'回滚还原 {item} 失败（跳过）：{e}', level='WARN')

    # 3. 不变量自检：回滚后必须仍有"更新器 + 主程序"，否则用户会失去入口
    missing = [name for name in (SELF_NAME, os.path.basename(MAIN_EXE))
               if not os.path.exists(os.path.join(BASE_DIR, name))]
    if missing:
        log(f'[ERROR] 回滚后关键文件缺失：{missing}；'
            f'备份仍保留在 {backup_path}，可手动还原', level='ERROR')
    if failed:
        log(f'回滚完成：还原 {restored} 项，失败 {len(failed)} 项：{failed}',
            level='WARN')
    else:
        log(f'回滚完成：还原 {restored} 项')


def apply_update(zip_path):
    """备份 -> 清理 -> 解压。返回 (ok: bool, message: str)"""
    import time as _t
    backup_root = os.path.join(BASE_DIR, BACKUP_DIR_NAME)
    t0 = _t.time()
    log(f'===== 开始应用更新 =====')
    log(f'更新包：{zip_path}（{os.path.getsize(zip_path)} 字节）')
    log(f'备份目录：{backup_root}')

    # 清掉可能残留的旧备份（例如上次失败后遗留）
    if os.path.isdir(backup_root):
        log('发现残留旧备份，先清除', level='WARN')
    shutil.rmtree(backup_root, ignore_errors=True)

    # 1. 备份（排除正在使用的更新包，避免把 26MB 的 zip 也备份一份）
    t = _t.time()
    try:
        backup_current(backup_root, exclude={os.path.basename(zip_path)})
        log(f'[1/3] 备份完成，耗时 {_t.time() - t:.2f}s')
    except Exception as e:
        log_exc(f'[1/3] 备份失败：{e}')
        shutil.rmtree(backup_root, ignore_errors=True)
        return False, '备份失败'

    # 2. 标记（除 KEEP_ITEMS 外全部改名为 *.old，不直接删除；
    #    必须保留更新包本身，否则第 3 步无包可解）
    t = _t.time()
    zip_name = os.path.basename(zip_path)
    try:
        marked, kept = clean_dir(keep={zip_name})
        log(f'[2/3] 标记完成，耗时 {_t.time() - t:.2f}s，'
            f'{len(marked)} 项待批处理清理')
    except Exception as e:
        log_exc(f'[2/3] 清理失败：{e}，回滚中')
        rollback(backup_root)
        return False, '清理失败，已回滚'

    # 3. 解压到暂存目录并按规则分类（不直接覆盖目标，避免占用问题）
    t = _t.time()
    if not os.path.exists(zip_path):
        log(f'更新包丢失，无法解压：{zip_path}', level='ERROR')
        rollback(backup_root)
        return False, '更新包丢失，已回滚'
    try:
        to_replace, to_skip, to_delete = safe_extract(zip_path, BASE_DIR)
        log(f'[3/3] 暂存解压完成，耗时 {_t.time() - t:.2f}s | '
            f'待替换 {len(to_replace)} | 跳过 {len(to_skip)} | 删除 {len(to_delete)}')
    except Exception as e:
        log_exc(f'[3/3] 解压失败：{e}，回滚中')
        rollback(backup_root)
        return False, '解压失败，已回滚'

    log(f'===== 更新应用成功，总耗时 {_t.time() - t0:.2f}s =====')
    log(f'待替换 {len(to_replace)} 项，由 Helper 内联完成')
    return True, f'成功（{len(to_replace)} 项待替换）'


def replace_staged(items, progress_cb=None, retries=REPLACE_MAX_RETRY):
    """把 _staging 里的新文件搬到目标路径（Helper 内联完成的"替换"阶段）。

    前提：clean_dir() 已把目标位置的原文件统一改名 *.old，原路径已空出，
    因此无需等待任何进程退出即可直接落位 —— Windows 允许重命名运行中的
    可执行文件/动态库（镜像由已映射的句柄持有，与路径无关）。

    items:       [(相对路径, 规则说明)]
    progress_cb: fn(done, total, rel)，每项开始时回调一次，供 GUI 显示进度
    retries:     单个文件的最大重试次数

    返回 (ok, failed)：failed 为 [(相对路径, 失败原因)]；ok 为 True 表示全部成功。
    """
    staging = os.path.join(BASE_DIR, STAGING_DIR)
    total = len(items)
    failed = []
    for idx, (rel, _desc) in enumerate(items, 1):
        if progress_cb:
            try:
                progress_cb(idx, total, rel)
            except Exception:
                pass
        src = os.path.join(staging, rel)
        dst = os.path.join(BASE_DIR, rel)
        last_err = ''
        for attempt in range(1, retries + 1):
            try:
                parent = os.path.dirname(dst)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                if os.path.exists(dst):
                    os.remove(dst)          # 目标应已空出；有残留则先清掉
                shutil.move(src, dst)
                last_err = ''
                break
            except OSError as e:
                last_err = str(e)
                if attempt < retries:
                    time.sleep(REPLACE_RETRY_WAIT)
        if last_err:
            failed.append((rel, last_err))
            log(f'[FAIL] {rel} 替换失败（已重试 {retries} 次）：{last_err}',
                level='WARN')
        else:
            log(f'[OK] {rel}', level='DEBUG')
    log(f'内联替换完成：成功 {total - len(failed)} 项，失败 {len(failed)} 项')
    return (not failed), failed


# ---------------------------------------------------------------------------
# 退出前调度批处理：分批延时替换 + 清理备份
# ---------------------------------------------------------------------------

def _read_manifest():
    """读取替换清单，返回 [(相对路径, 规则说明)]。"""
    manifest_path = os.path.join(BASE_DIR, REPLACE_MANIFEST)
    if not os.path.exists(manifest_path):
        return []
    items = []
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                if not line:
                    continue
                parts = line.split('\t', 1)
                rel = parts[0]
                desc = parts[1] if len(parts) > 1 else ''
                items.append((rel, desc))
    except OSError as e:
        log(f'读取替换清单失败：{e}', level='WARN')
    return items


def _remove_manifest(manifest_path):
    """删除替换清单，可重复调用。

    清单只是"生成批处理的输入"，不是"运行批处理的状态"：
    内容已编译进 bat，生成后必须立刻删除，否则残留清单会在下次
    启动时被 _read_manifest() 读到，误判为有替换任务。
    """
    try:
        if os.path.exists(manifest_path):
            os.remove(manifest_path)
            log(f'已删除替换清单：{manifest_path}', level='DEBUG')
    except OSError as e:
        log(f'删除替换清单失败：{e}', level='WARN')


def _bat_escape(s):
    """转义批处理中的特殊字符。"""
    return s.replace('^', '^^').replace('%', '%%').replace('&', '^&') \
            .replace('|', '^|').replace('<', '^<').replace('>', '^>')


def _gen_replace_batch(items, delete_backup, backup_root, launch_main=False):
    """生成分批替换批处理脚本。

    设计要点：
      - 每批处理 REPLACE_BATCH_SIZE 个文件，批间等待 REPLACE_BATCH_INTERVAL 秒
      - 每个文件独立重试 REPLACE_MAX_RETRY 次，失败只记录不中断
      - 全程写入日志文件，最后生成汇总报告

    重要：所有路径统一使用反斜杠。实测发现 cmd 的 del/move 会把 "/"
    当作参数开关处理（如 del "dir/a.py" 返回 rc=3、move 返回 rc=1，
    文件不会被操作），因此任何来源的相对路径都必须归一化为 "\\"。
    """
    # 归一化相对路径分隔符（防御：无论源头是 / 还是 \\，生成的都是 \\ 路径）
    items = [(rel.replace('/', '\\'), desc) for rel, desc in items]
    staging = os.path.join(BASE_DIR, STAGING_DIR)
    # 批处理以 chcp 936（GBK）输出，单独写一个日志文件：
    # 与 Python 侧 UTF-8 的 helper.log 混写会形成混合编码，排查时需分段解码。
    log_path = os.path.join(BASE_DIR, REPLACE_BATCH_LOG)
    report_path = os.path.join(BASE_DIR, REPLACE_REPORT)

    L = [
        '@echo off',
        # 批处理以 GBK 写入，日志也用 GBK 输出（实测 chcp 65001 会让 echo 中文变乱码）。
        'chcp 936 >nul',
        'setlocal enabledelayedexpansion',
        f'set "BASE={_bat_escape(BASE_DIR)}"',
        f'set "STAGING={_bat_escape(staging)}"',
        f'set "LOG={_bat_escape(log_path)}"',
        f'set "REPORT={_bat_escape(report_path)}"',
        'set /a OK=0',
        'set /a FAIL=0',
        'set /a BATCHNO=0',
        '',
        # 追加写入（>>）到批处理专用日志（GBK 编码，与 helper.log 分开）
        'echo [%date% %time%] ===== 延时替换开始 ===== >> "%LOG%"',
        f'echo 批次大小={REPLACE_BATCH_SIZE} 批间隔={REPLACE_BATCH_INTERVAL}s '
        f'重试={REPLACE_MAX_RETRY} >> "%LOG%"',
        '',
        # ---- 等待 Helper 进程完全退出 ----
        # 本批处理由 Helper 的退出路径启动，且仅在内联替换失败时兜底使用。
        # 先等它彻底退出（含 PyInstaller bootloader 收尾），再开始搬运，
        # 避免与正在退出的进程争抢文件句柄。
        f'ping 127.0.0.1 -n {REPLACE_INITIAL_WAIT + 1} >nul',
        f'echo [%date% %time%] 开始兜底替换 >> "%LOG%"',
        '',
    ]

    # ---- 说明：此处不再有"标记 helper.exe"段 ----
    # helper.exe 已由 clean_dir() 在运行中改名成 *.old（Windows 允许重命名
    # 运行中的可执行文件），原路径已空出，本脚本按普通文件搬运即可。
    # 同样地，也没有"待删清单"段：所有 *.old 的删除由下次启动 Helper 时的
    # cleanup_stale_old_files() 完成（那时旧进程已退出，不再有占用）。

    # 按批次切分
    batches = [items[i:i + REPLACE_BATCH_SIZE]
               for i in range(0, len(items), REPLACE_BATCH_SIZE)]

    for bi, batch in enumerate(batches, 1):
        L.append(f':: ---------- 批次 {bi}/{len(batches)} ----------')
        L.append(f'set /a BATCHNO={bi}')
        L.append(f'echo [%date% %time%] --- 批次 {bi}/{len(batches)} '
                 f'（{len(batch)} 个文件）--- >> "%LOG%"')

        for rel, desc in batch:
            src = os.path.join(staging, rel)
            dst = os.path.join(BASE_DIR, rel)
            safe_rel = _bat_escape(rel)
            safe_desc = _bat_escape(desc)
            L += [
                f':: 替换 {safe_rel}（{safe_desc}）',
                f'if not exist "{_bat_escape(src)}" (',
                # 暂存缺失说明解压不完整：按失败处理（计 FAIL 触发回滚），
                # 避免主程序因漏装文件而损坏；语义与计数保持一致
                f'  echo [%date% %time%] [FAIL] {safe_rel} 暂存文件缺失 >> "%LOG%"',
                f'  set /a FAIL+=1',
                f'  goto :next_{bi}_{len(L)}',
                ')',
                f'set /a TRY=0',
                f':retry_{bi}_{len(L)}',
                f'set /a TRY+=1',
                # 确保目标目录存在
                f'for %%D in ("{_bat_escape(dst)}") do if not exist "%%~dpD" '
                f'mkdir "%%~dpD" >nul 2>&1',
                # 先删旧文件，再搬新文件
                f'del /f /q "{_bat_escape(dst)}" >nul 2>&1',
                f'move /y "{_bat_escape(src)}" "{_bat_escape(dst)}" >nul 2>&1',
                f'if exist "{_bat_escape(dst)}" (',
                f'  echo [%date% %time%] [OK] {safe_rel} >> "%LOG%"',
                f'  set /a OK+=1',
                f'  goto :next_{bi}_{len(L)}',
                ')',
                f'if !TRY! GEQ {REPLACE_MAX_RETRY} (',
                f'  echo [%date% %time%] [FAIL] {safe_rel} 重试 !TRY! 次仍失败 >> "%LOG%"',
                f'  set /a FAIL+=1',
                f'  goto :next_{bi}_{len(L)}',
                ')',
                f'ping 127.0.0.1 -n {REPLACE_RETRY_WAIT + 1} >nul',
                f'goto :retry_{bi}_{len(L)}',
                f':next_{bi}_{len(L)}',
            ]

        # 批次间隔
        if bi < len(batches):
            L += [
                f'echo [%date% %time%] 批次 {bi} 完成，等待 '
                f'{REPLACE_BATCH_INTERVAL}s >> "%LOG%"',
                f'ping 127.0.0.1 -n {REPLACE_BATCH_INTERVAL + 1} >nul',
            ]
        L.append('')

    # ---- 失败判定与回滚 ----
    # 只要有任一文件替换失败，就从 _backup 全量回滚到更新前状态
    L += [
        ':: ---------- 失败判定与回滚 ----------',
        f'if !FAIL! EQU 0 goto :no_rollback',
        f'echo [%date% %time%] 检测到 !FAIL! 个文件替换失败，开始回滚 >> "%LOG%"',
        f'echo [%date% %time%] 回滚来源：{_bat_escape(backup_root)} >> "%LOG%"',
        '',
        f'if not exist "{_bat_escape(backup_root)}" (',
        f'  echo [%date% %time%] [ERROR] 备份目录不存在，无法回滚 >> "%LOG%"',
        f'  goto :no_rollback',
        ')',
        '',
        # 1. 清空当前目录（保留 _backup、data、helper.log、回滚标记）
        #    data 是用户名单数据目录，必须与 KEEP_ITEMS 语义一致地保护：
        #    若备份阶段 data 被占用跳过，清空后 xcopy 无法还原 -> 用户数据永久丢失。
        f'echo [%date% %time%] 回滚步骤 1/2：清空当前文件 >> "%LOG%"',
        f'for /d %%D in ("%BASE%\\*") do (',
        f'  if /i not "%%~nxD"=="{BACKUP_DIR_NAME}" '
        f'if /i not "%%~nxD"=="data" rmdir /s /q "%%D" >nul 2>&1',
        ')',
        f'for %%F in ("%BASE%\\*") do (',
        f'  if /i not "%%~nxF"=="helper.log" '
        f'if /i not "%%~nxF"=="{REPLACE_REPORT}" '
        f'if /i not "%%~nxF"=="app.zip" del /f /q "%%F" >nul 2>&1',
        ')',
        '',
        # 2. 从备份还原
        f'echo [%date% %time%] 回滚步骤 2/2：从备份还原 >> "%LOG%"',
        f'xcopy /e /i /y /q "{_bat_escape(backup_root)}\\*" "%BASE%\\" >nul 2>&1',
        # xcopy 失败检测：返回码被 >nul 丢弃，若不检测会"假回滚"
        # （日志写"回滚完成"但文件未还原，用户看到已回滚却处于半损坏状态）
        'if errorlevel 1 (',
        f'  echo [%date% %time%] [WARN] 回滚还原不完整（xcopy 失败），请检查文件 >> "%LOG%"',
        '  set /a ROLLBACK_INCOMPLETE=1',
        ') else (',
        f'  echo [%date% %time%] 回滚还原完成 >> "%LOG%"',
        ')',
        '',
        # 写入回滚标记（进度窗口据此识别）
        f'echo rollback > "%BASE%\\{REPLACE_ROLLBACK_FLAG}"',
        f'echo [%date% %time%] 回滚完成 >> "%LOG%"',
        '',
        ':no_rollback',
        '',
    ]

    # 清理暂存目录
    L += [
        ':: ---------- 清理暂存 ----------',
        f'if exist "%STAGING%" rmdir /s /q "%STAGING%" >nul 2>&1',
        f'if exist "%STAGING%" (',
        f'  echo [%date% %time%] [WARN] 暂存目录未能删除 >> "%LOG%"',
        ') else (',
        f'  echo [%date% %time%] 暂存目录已清理 >> "%LOG%"',
        ')',
        '',
    ]

    # ---- 清理 .old 残留：不在此处做 ----
    # 原设计由本段扫删 *.old，但实测进度窗口（第二个 helper.exe 实例）
    # 退出时机不可控，helper.exe.old / VCRUNTIME140*.dll.old 常因仍被占用
    # 而删除失败；残留的 helper.exe.old 会让下次更新的标记逻辑"放弃标记"，
    # 导致新 helper.exe 无法就位、更新静默失败。
    # 现改为：由下次启动 helper 时（cleanup_stale_old_files）删除 ——
    # 那时旧进程早已退出，文件不再被占用，删除必然成功。

    # 删除备份（仅在无失败时；回滚后需保留备份供用户排查）
    if delete_backup:
        L += [
            ':: ---------- 删除备份（仅成功时） ----------',
            f'if !FAIL! NEQ 0 (',
            f'  echo [%date% %time%] 存在失败，保留备份目录 >> "%LOG%"',
            f'  goto :skip_backup_del',
            ')',
            f'if exist "{_bat_escape(backup_root)}" (',
            f'  rmdir /s /q "{_bat_escape(backup_root)}" >nul 2>&1',
            ')',
            f'if exist "{_bat_escape(backup_root)}" (',
            f'  echo [%date% %time%] [WARN] 备份目录未能删除 >> "%LOG%"',
            ') else (',
            f'  echo [%date% %time%] 备份目录已清理 >> "%LOG%"',
            ')',
            ':skip_backup_del',
            '',
        ]

    # 生成报告
    L += [
        ':: ---------- 生成报告 ----------',
        f'echo [%date% %time%] ===== 延时替换结束 ===== >> "%LOG%"',
        f'echo 成功=!OK! 失败=!FAIL! 批次=!BATCHNO! >> "%LOG%"',
        '',
        f'echo ============================================ > "%REPORT%"',
        f'echo 延时替换报告 >> "%REPORT%"',
        f'echo 时间: %date% %time% >> "%REPORT%"',
        f'echo ============================================ >> "%REPORT%"',
        f'echo 总文件数: {len(items)} >> "%REPORT%"',
        f'echo 成功替换: !OK! >> "%REPORT%"',
        f'echo 失败: !FAIL! >> "%REPORT%"',
        f'echo 批次数: !BATCHNO! >> "%REPORT%"',
        f'echo. >> "%REPORT%"',
        f'if !FAIL! EQU 0 (',
        f'  echo 结果: 更新成功 >> "%REPORT%"',
        ') else (',
        f'  echo 结果: 更新失败，已回滚到更新前状态 >> "%REPORT%"',
        f'  echo 备份保留在: {_bat_escape(BACKUP_DIR_NAME)} >> "%REPORT%"',
        ')',
        # 回滚还原不完整时明确标注，避免用户误以为已完全恢复
        'if defined ROLLBACK_INCOMPLETE (',
        f'  echo [警告] 回滚还原不完整，部分文件可能未恢复，请检查 >> "%REPORT%"',
        ')',
        f'echo. >> "%REPORT%"',
        f'echo 详细日志见: helper.log（Helper 侧）与 {REPLACE_BATCH_LOG}（批处理侧） >> "%REPORT%"',
    ]

    # ---- 启动主程序 ----
    # 兜底批处理是"最后一道工序"：Helper 已退出，替换完成（或已回滚）后
    # 由本脚本启动主程序，保证用户最终一定能看到程序界面。
    if launch_main:
        L += [
            ':: ---------- 启动主程序 ----------',
            f'echo [%date% %time%] 启动主程序 >> "%LOG%"',
            f'if exist "{_bat_escape(MAIN_EXE)}" start "" "{_bat_escape(MAIN_EXE)}"',
            '',
        ]

    return L


# 模块级：待启动的兜底替换任务（helper 退出后才真正启动批处理）
_pending_replace = False
_pending_replace_total = 0


def schedule_fallback_replace(items, delete_backup):
    """把"内联替换失败"的项交给退出后的批处理兜底完成，【不在此启动】。

    正常路径下 replace_staged() 已把全部文件落位，不会走到这里；只有当
    某些文件在 Helper 运行期间被占用、重试后仍失败时才调用本函数：
    Helper 退出后这些占用通常随之释放，独立 cmd 进程可以再试一次；
    若批处理仍失败，则从 _backup 回滚。

    items:         [(相对路径, 规则说明)]，仅含内联阶段失败的项
    delete_backup: 是否在成功后删除 _backup（仅更新成功路径为 True）

    返回：是否已成功生成兜底批处理。
    """
    backup_root = os.path.join(BASE_DIR, BACKUP_DIR_NAME)
    manifest_path = os.path.join(BASE_DIR, REPLACE_MANIFEST)
    has_backup = delete_backup and os.path.isdir(backup_root)

    if not items:
        _remove_manifest(manifest_path)
        log('无待兜底项，不调度批处理', level='DEBUG')
        return False

    # 批处理放在 BASE_DIR 之外：回滚时会用 for 循环清空 BASE_DIR，
    # 若批处理在里面会被自己删掉，导致后续 xcopy 还原无法执行。
    os.makedirs(REPLACE_BAT_DIR, exist_ok=True)
    bat_path = os.path.join(REPLACE_BAT_DIR, REPLACE_BAT)
    lines = _gen_replace_batch(items, has_backup, backup_root, launch_main=True)

    try:
        with open(bat_path, 'w', encoding='gbk') as f:
            f.write('\r\n'.join(lines) + '\r\n')
        log(f'已写入兜底替换批处理：{bat_path}（{len(lines)} 行）')
        # 仅记录待启动，真正 Popen 交给 helper 关闭时的 _flush_pending_replace()
        _set_pending_replace(True, len(items))
        return True
    except Exception as e:
        log_exc(f'写入兜底替换批处理失败：{e}')
        return False


def _set_pending_replace(ready, total):
    """记录待启动的替换任务（供 helper 关闭路径读取）。"""
    global _pending_replace, _pending_replace_total
    _pending_replace = ready
    _pending_replace_total = total


def _flush_pending_replace():
    """在 helper 关闭后启动兜底批处理（供 main() 的 finally 调用）。

    只会在"内联替换失败"时走到这里：Helper 退出后文件占用通常随之释放，
    独立的 cmd 进程可再试一次；批处理末尾会启动主程序，保证用户最终
    一定能看到程序界面（正常路径不经过这里，主程序由 Helper 直接启动）。
    """
    global _pending_replace
    if not _pending_replace:
        return
    _pending_replace = False   # 只启动一次
    total = _pending_replace_total
    if total <= 0:
        return

    try:
        bat_path = os.path.join(REPLACE_BAT_DIR, REPLACE_BAT)
        proc = subprocess.Popen(
            ['cmd', '/c', bat_path],
            cwd=BASE_DIR,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        log(f'已启动兜底替换批处理（PID={proc.pid}，{total} 个文件）')
    except Exception as e:
        log_exc(f'启动兜底替换批处理失败：{e}')


# ---------------------------------------------------------------------------
# 主程序管理：探测 / 关闭 / 启动
# ---------------------------------------------------------------------------

def is_main_running():
    """探测主程序是否在运行：尝试拿主程序的 Mutex。

    - 拿不到  -> 主程序持有锁 -> 在运行
    - 拿到了  -> 主程序没跑   -> 立即释放
    """
    probe = SingleInstance(MAIN_MUTEX_NAME)
    if probe.acquire():
        probe.release()
        log('探测结果：主程序未运行', level='DEBUG')
        return False
    log('探测结果：主程序正在运行', level='DEBUG')
    return True


def kill_main(wait=5):
    """taskkill 主程序并轮询确认退出。"""
    import time as _t
    exe_name = os.path.basename(MAIN_EXE)
    log(f'尝试终止主程序：{exe_name}（最长等待 {wait}s）')
    try:
        result = subprocess.run(
            ['taskkill', '/F', '/IM', exe_name],
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=wait,
        )
        output = result.stdout.decode('gbk', errors='replace').strip()
        log(f'taskkill 返回码={result.returncode} | 输出：{output}', level='DEBUG')
    except subprocess.TimeoutExpired:
        # taskkill 自身超时（subprocess 已终止该命令）：不能直接判定失败 ——
        # 主程序可能正在退出中，转入下面的轮询确认真实状态
        log(f'taskkill 超时（{wait}s），转入轮询确认主程序状态', level='WARN')
    except Exception as e:
        log_exc(f'终止主程序失败：{e}')
        return False

    for i in range(int(wait * 4)):
        if not is_main_running():
            log(f'主程序已确认退出（轮询 {i + 1} 次，'
                f'耗时约 {(i + 1) * 0.25:.2f}s）')
            return True
        _t.sleep(0.25)

    still = is_main_running()
    if still:
        log(f'等待 {wait}s 后主程序仍在运行', level='ERROR')
    else:
        log('主程序已退出')
    return not still


def launch_main():
    if os.path.exists(MAIN_EXE):
        log(f'启动主程序：{MAIN_EXE}')
        proc = subprocess.Popen([MAIN_EXE], cwd=BASE_DIR)
        log(f'主程序已启动（PID={proc.pid}）')
        return True
    log(f'主程序不存在：{MAIN_EXE}', level='ERROR')
    return False


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class HelperApp:

    def __init__(self, root):
        self.root = root
        # 退出时是否需要由 Helper 启动主程序：
        #   True  -> 由 Helper 启动（正常路径 / 出错回落）
        #   False -> 不启动（主程序已在运行，或已交给兜底批处理启动）
        self.launch_after = True

        self.root.title('随机点名工具 - 启动器')
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        w, h = 420, 260
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f'{w}x{h}+{x}+{y}')

        tk.Label(root, text='随 机 点 名', font=('Microsoft YaHei', 18, 'bold'),
                 bg=BG, fg=FG).pack(pady=(28, 4))

        self.ver_label = tk.Label(root, text=f'当前版本：{LOCAL_VERSION}',
                                  font=('Microsoft YaHei', 10), bg=BG, fg=MUTED)
        self.ver_label.pack()

        self.status = tk.Label(root, text='正在检查更新...',
                               font=('Microsoft YaHei', 11), bg=BG, fg=FG)
        self.status.pack(pady=(22, 8))

        style = ttk.Style()
        style.theme_use('default')
        style.configure('Helper.Horizontal.TProgressbar',
                        troughcolor='#3d3d52', background=ACCENT,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)
        self.progress = ttk.Progressbar(root, style='Helper.Horizontal.TProgressbar',
                                        length=320, mode='determinate')
        self.progress.pack()

        self.btn = tk.Button(root, text='检查中...', font=('Microsoft YaHei', 11),
                             bg=ACCENT, fg=FG, activebackground='#5568d3',
                             activeforeground=FG, relief='flat',
                             width=14, state='disabled')
        self.btn.pack(pady=(22, 0))

        # 是否已启动主程序（防止"退出路径兜底"重复拉起第二个实例）
        self.launched = False
        # 是否处于不可中断的更新关键阶段（标记 *.old -> 替换 之间）
        self._busy = False
        # 接管窗口关闭：关键阶段不允许中途退出
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)

        self.root.after(200, lambda: threading.Thread(
            target=self.check, daemon=True).start())

    def _on_close(self):
        """窗口关闭请求处理。

        更新关键阶段（标记 *.old -> 替换）中途退出，会让目录停在
        "一半新、一半 *.old"：残留的 *.old 会在下次启动被清理，
        等于把这些文件永久丢掉。因此该阶段内忽略关闭请求。
        """
        if self._busy:
            log('更新进行中，忽略窗口关闭请求', level='WARN')
            self.set_status('更新进行中，请稍候…', '#ffb86c')
            return
        self.root.destroy()

    def set_status(self, text, color=FG):
        self.status.config(text=text, fg=color)

    def set_progress(self, value):
        self.progress['value'] = value * 100

    def _ui(self, fn, *args):
        """线程安全地更新界面。

        check() 跑在后台线程，而 tkinter 的 after 只能从主线程调用；
        用户在流程进行中关闭窗口后 root 已销毁，再调用会抛
        RuntimeError: main thread is not in main loop。
        这里统一吞掉该异常：UI 更新失败不应中断更新流程本身
        （关键动作如"启动主程序"并不依赖 UI）。
        """
        try:
            self.root.after(0, lambda: fn(*args))
        except (RuntimeError, tk.TclError):
            pass

    def check(self):
        """后台线程入口：保证任何异常都不会吃掉"最终启动主程序"这一步。

        _check_impl() 只负责流程；所有出口统一收敛到 finally 的
        enable_start()（800ms 后 auto_launch），避免中途异常导致
        窗口永久停在半途、主程序不被启动。
        """
        try:
            self._check_impl()
        except Exception as e:
            log_exc(f'检查更新流程异常：{e}')
            self._ui(self.set_status, '更新失败，将直接启动程序', '#ffb86c')
        finally:
            self._busy = False      # 无论从哪个分支退出，都解除"不可关闭"保护
            self._ui(self.enable_start)

    def _check_impl(self):
        """检查更新并应用更新。

        硬约束：每个失败分支都必须落到"回滚"或"明确的失败态"，
        不允许带着"一半新、一半 *.old"的目录状态去启动主程序。
        """
        import time as _t
        t0 = _t.time()
        log('=' * 60)
        log(f'开始检查更新 | 本地版本={LOCAL_VERSION}')

        remote_tag = get_latest_tag()
        if not remote_tag:
            log('无法获取最新版本（atom 获取失败），跳过更新', level='WARN')
            self._ui(self.set_status, '无法连接 GitHub', '#ffb86c')
            return

        log(f'远端最新版本：{remote_tag}')

        # 版本号比较（不走 API，无速率限制）
        if not _version_gt(remote_tag, LOCAL_VERSION):
            log(f'已是最新版本（本地 {LOCAL_VERSION} >= 远端 {remote_tag}）')
            if is_main_running():
                # 主程序已在运行：本次属于"误触启动器"，直接退出即可，
                # 不再拉起第二个实例（否则会被主程序单实例锁拦下并弹窗打扰）
                log('主程序已在运行，本次不再重复启动', level='WARN')
                self.launch_after = False
                self._ui(self.set_status, '程序已在运行', '#7ee787')
            else:
                self._ui(self.set_status, '已是最新版本', '#7ee787')
            return

        log(f'发现新版本：{LOCAL_VERSION} -> {remote_tag}')
        zip_url = _asset_url(remote_tag, ZIP_ASSET_NAME)
        log(f'更新包地址：{zip_url}')

        # 更新前先确保主程序已退出：主程序占用的 DLL 会阻碍改名与替换
        if is_main_running():
            self._ui(self.set_status, '正在关闭主程序...')
            log('主程序在运行，尝试终止', level='WARN')
            if not kill_main():
                log('无法关闭主程序，放弃更新', level='ERROR')
                self._ui(self.set_status, '请先关闭主程序后重试', '#ffb86c')
                return
            log('主程序已退出')
        else:
            log('主程序未运行，直接更新')

        self._ui(self.set_status, f'发现新版本 {remote_tag}，下载中...')
        zip_path = os.path.join(BASE_DIR, 'app.zip')
        t = _t.time()
        # 进度回调收到的是"已下载字节数"，这里换算成比例。
        # 分母由 size_cb 在探测到总大小时回填（atom 渠道拿不到文件大小）
        total_size = [0]

        def on_progress(done):
            if total_size[0]:
                self._ui(self.set_progress, done / total_size[0])

        def on_size(total):
            total_size[0] = total
            log(f'更新包大小：{total} 字节（{total / 1024 / 1024:.2f} MB）')

        try:
            download_file(zip_url, zip_path, on_progress, size_cb=on_size)
            log(f'下载完成，耗时 {_t.time() - t:.2f}s')
        except Exception as e:
            log_exc(f'下载失败：{e}')
            self._ui(self.set_status, '下载失败', '#ff7b72')
            return

        # ---- 应用更新：备份 -> 全部改名 *.old（含 helper.exe 自身）-> 解压到 _staging
        # 说明：不需要等待任何进程退出。Windows 允许重命名运行中的可执行文件，
        # 因此 Helper 在自己运行期间就能腾出全部目标文件名（含自身），
        # 随后内联把新文件搬到原路径，无需独立进程与"退出后批处理"。
        self._ui(self.set_status, '正在应用更新...')
        self._ui(self.set_progress, 0)
        # 进入关键阶段：从此刻直到替换结束，忽略窗口关闭请求（见 _on_close）
        self._busy = True
        ok, msg = apply_update(zip_path)

        if os.path.exists(zip_path):
            try:
                os.remove(zip_path)
                log(f'已删除更新包：{zip_path}', level='DEBUG')
            except OSError as e:
                log(f'删除更新包失败：{e}', level='WARN')

        if not ok:
            log(f'更新失败：{msg} | 总耗时 {_t.time() - t0:.2f}s', level='ERROR')
            self._ui(self.set_status, msg, '#ffb86c')
            return

        # ---- 内联替换：原路径已空出，直接把 _staging 里的新文件搬过去
        items = _read_manifest()
        # 清单已读入内存，立即删除，避免残留清单在下次启动时被误读为有任务
        _remove_manifest(os.path.join(BASE_DIR, REPLACE_MANIFEST))
        backup_root = os.path.join(BASE_DIR, BACKUP_DIR_NAME)

        if not items:
            # 此时原文件已被改成 *.old；若没有任何待替换项却按"成功"继续，
            # 会删掉 _backup 与 _staging，目录被清空且无法恢复 —— 必须回滚。
            log('替换清单为空（无待替换项），判定更新失败并回滚', level='ERROR')
            rollback(backup_root)
            self._ui(self.set_status, '更新失败，已回滚', '#ffb86c')
            return

        log(f'内联替换开始：{len(items)} 项')

        def on_replace(done, total, rel):
            self._ui(self.set_status, f'正在替换 {done}/{total}：{rel}')
            if total:
                self._ui(self.set_progress, done / total)

        ok2, failed = replace_staged(items, progress_cb=on_replace)
        if ok2:
            # 全部落位成功：清掉备份与暂存目录（暂存里可能残留空目录），
            # 退出时由 Helper 启动主程序
            shutil.rmtree(backup_root, ignore_errors=True)
            shutil.rmtree(os.path.join(BASE_DIR, STAGING_DIR),
                          ignore_errors=True)
            self._ui(self.set_status, f'更新完成（{remote_tag}）', '#7ee787')
            self._ui(self.set_progress, 1)
            log(f'更新成功！版本：{remote_tag} | 总耗时 {_t.time() - t0:.2f}s')
            return

        # 重试后仍失败：交给退出后的批处理兜底（由它负责启动主程序）
        log(f'内联替换仍有 {len(failed)} 项失败，交由批处理兜底', level='WARN')
        if schedule_fallback_replace(failed, delete_backup=True):
            self.launch_after = False
            self._ui(self.set_status, '部分文件被占用，即将退出后完成更新...',
                     '#ffb86c')
            return

        # 无兜底可用：绝不能带着"一半新、一半 *.old"的状态启动主程序，必须回滚
        log('兜底批处理生成失败，执行回滚', level='ERROR')
        rollback(backup_root)
        self._ui(self.set_status, f'更新失败，已回滚（{len(failed)} 项未替换）',
                 '#ffb86c')

    def enable_start(self):
        self.btn.config(text='启动中...')
        self.root.after(800, self.auto_launch)

    def auto_launch(self):
        if self.launch_after and not self.launched:
            log('准备启动主程序')
            self.launched = launch_main()
        elif not self.launch_after:
            log('无需启动主程序（已在运行或已交给兜底批处理）', level='DEBUG')
        log('Helper 即将退出')
        self.root.destroy()

    def ensure_launch(self):
        """退出路径兜底：窗口被关闭或 GUI 异常时，也必须把主程序拉起来。

        auto_launch 依赖 tkinter 事件循环（root.after(800)）：窗口一旦销毁，
        事件循环结束、_ui() 也会静默跳过，就没有人再启动主程序了。
        因此由 main() 的 finally 直接调用本方法兜底（唯一调用点）。
        """
        if not self.launch_after or self.launched:
            return
        log('界面退出路径未能启动主程序，由退出兜底启动', level='WARN')
        self.launched = launch_main()


def main():
    # 在自身 _MEI 里写标记，供下次启动识别并清理本程序残留的临时目录
    _mark_mei()
    log_env()
    # 单实例检查：已有实例在运行则立即退出
    if not _instance_lock.acquire():
        log('已有 Helper 实例在运行，本次启动退出', level='WARN')
        sys.exit(0)

    # 拿到唯一实例资格后，清理上次运行残留的 _MEI 垃圾目录
    cleanup_stale_mei()
    # 清理上次更新残留的 *.old（【清理】阶段）—— 此时旧进程已退出，
    # 文件不再被占用，删除必然成功；残留的 helper.exe.old 会导致下次更新失败
    cleanup_stale_old_files()
    # 兜底清理上次异常中断残留的替换状态文件与暂存目录，
    # 避免残留清单在"已是最新版本"时误触发替换流程
    cleanup_stale_replace_files()

    # 正常退出 / 异常终止时释放 Mutex，避免残留死锁
    atexit.register(_instance_lock.release)

    app = None
    try:
        root = tk.Tk()
        app = HelperApp(root)
        root.mainloop()
    except Exception as e:
        log_exc(f'GUI 异常退出：{e}')
        raise
    finally:
        _instance_lock.release()
        # 仅在内联替换失败时才会启动兜底批处理（独立 cmd 进程）
        _flush_pending_replace()
        # 窗口被关闭 / GUI 异常时，事件循环不会再驱动 auto_launch，
        # 这里兜底保证"用户最终一定看得到主程序"
        if app is not None:
            app.ensure_launch()
        log('Helper 已退出')


if __name__ == '__main__':
    main()