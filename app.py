# -*- coding: utf-8 -*-
"""随机点名工具 —— 使用 pywebview 渲染内嵌 HTML 页面"""
import os
import sys
import random

import webview

# 编译后（Nuitka）用 exe 所在目录定位配置文件；否则用脚本目录
if getattr(sys, "frozen", False) or "__compiled__" in dir():
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FILE_LIST = os.path.join(BASE_DIR, 'file.txt')


class Api:
    """暴露给前端 JS 的接口，通过 window.pywebview.api 调用"""

    def __init__(self):
        self.avoid_choice = []      # 去重模式：本轮已抽中过的名字
        self.avoid_name = ''        # 平衡模式：上一位被抽中者
        self.mode_status = 'normal'  # 当前模式：normal / balance / dedup
        self._window = None         # 主窗口引用，用于 Esc 切换全屏

    def _resolve_path(self, path):
        """相对路径基于 BASE_DIR 解析"""
        if os.path.isabs(path):
            return path
        return os.path.join(BASE_DIR, path)

    def _get_current_path(self):
        """当前选中文件（file.txt 第一行）的绝对路径"""
        files = self.get_file_list()
        if files:
            return self._resolve_path(files[0])
        return os.path.join(BASE_DIR, 'person.txt')

    def _save_file_list(self, paths):
        """保存名单路径列表到 file.txt"""
        with open(FILE_LIST, 'w', encoding='utf-8') as f:
            for p in paths:
                f.write(p + '\n')

    def get_file_list(self):
        """读取 file.txt 中的名单路径列表"""
        try:
            with open(FILE_LIST, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except OSError:
            return []

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
        """添加名单文件路径"""
        paths = self.get_file_list()
        if path not in paths:
            paths.append(path)
            self._save_file_list(paths)
        return paths

    def remove_file(self, path):
        """删除名单文件路径，同时删除文件本身"""
        paths = self.get_file_list()
        if path in paths:
            paths.remove(path)
            self._save_file_list(paths)
        abs_path = self._resolve_path(path)
        try:
            os.remove(abs_path)
        except OSError:
            pass
        return paths

    def create_new_file(self, name):
        """创建空白名单文件"""
        if not name.endswith('.txt'):
            name += '.txt'
        path = os.path.join(BASE_DIR, name)
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as f:
                pass
        paths = self.get_file_list()
        if path not in paths:
            paths.append(path)
            self._save_file_list(paths)
        return paths

    def edit_file(self, path):
        """用默认编辑器打开名单文件"""
        abs_path = self._resolve_path(path)
        try:
            os.startfile(abs_path)
            return True
        except OSError:
            return False

    def browse_file(self):
        """打开文件浏览器选择名单文件"""
        if self._window is None:
            return ''
        results = self._window.create_file_dialog(
            webview.OPEN_DIALOG,
            file_types=("Text files (*.txt)",),
        )
        if results and len(results) > 0:
            return results[0]
        return ''

    def set_window(self, w):
        self._window = w

    def toggle_fullscreen(self):
        """前端按 Esc 时切换全屏/窗口化"""
        if self._window is not None:
            self._window.toggle_fullscreen()
        return True

    def get_names(self):
        """读取当前选中名单文件"""
        path = self._get_current_path()
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except OSError:
            return []

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
</style>
</head>
<body>
  <div class="wrap">
    <h1>随 机 点 名</h1>
    <div id="display" class="display"></div>
    <div class="controls">
      <button id="btn" disabled>名单加载中...</button>
      <select id="mode">
        <option value="normal" selected>普通模式</option>
        <option value="balance">平衡模式</option>
        <option value="dedup">去重模式</option>
      </select>
      <button id="fsbtn" class="fs-btn">全屏 / 窗口</button>
    </div>
    <div id="mode-desc" class="mode-desc"></div>
    <div id="count" class="count"></div>
    <div class="file-mgr">
      <select id="file-select" title="选择名单文件"></select>
      <button id="new-btn">新名单</button>
      <button id="edit-btn">编辑内容</button>
      <button id="add-btn">浏览添加</button>
      <button id="del-btn" class="del-btn">删除</button>
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
  var firstPickDone = false;  // 第一次点名后标题缩小、名字框放大
  var currentNameSize = { fs: 16.5, lh: 30 };  // 当前名字字号（vh），初始 30vh×0.55

  var DESCRIPTIONS = {
    normal:  '完全随机抽取，可能连续抽到同一人',
    balance: '避开上一位被抽中者，不会连续两次点到同一人',
    dedup:   '同一轮内抽中过的人不再出现，全员抽完后自动开始新一轮'
  };

  modeSelect.addEventListener('change', function () {
    window.pywebview.api.set_mode(modeSelect.value).then(function () {
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
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      window.pywebview.api.toggle_fullscreen();
    }
  });
  document.getElementById('fsbtn').addEventListener('click', function () {
    window.pywebview.api.toggle_fullscreen();
  });

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
    window.pywebview.api.get_file_list().then(function (files) {
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
    window.pywebview.api.get_names().then(function (list) {
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
    });
  }

  fileSelect.addEventListener('change', function () {
    var path = fileSelect.value;
    if (path) {
      window.pywebview.api.set_current_file(path).then(function () {
        reloadNames();
      });
    }
  });

  addBtn.addEventListener('click', function () {
    window.pywebview.api.browse_file().then(function (path) {
      if (path) {
        window.pywebview.api.add_file(path).then(function () {
          loadFileList();
          reloadNames();
        });
      }
    });
  });

  delBtn.addEventListener('click', function () {
    var path = fileSelect.value;
    if (path) {
      window.pywebview.api.remove_file(path).then(function () {
        loadFileList();
        reloadNames();
      });
    }
  });

  newBtn.addEventListener('click', function () {
    var name = prompt('请输入新名单文件名（不含扩展名）：');
    if (name) {
      window.pywebview.api.create_new_file(name).then(function () {
        loadFileList();
        reloadNames();
      });
    }
  });

  editBtn.addEventListener('click', function () {
    var path = fileSelect.value;
    if (path) {
      window.pywebview.api.edit_file(path);
    }
  });

  function init() {
    loadFileList();
    reloadNames();
  }

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
    window.pywebview.api.choose_name().then(function (name) {
      clearTimers();
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
      display.classList.add('result');   // 定格为橙色渐变高亮
      btn.disabled = false;
      btn.textContent = '再 来 一 次';
      rolling = false;
      // 第一次点名后：标题缩小、名字框放大（仅执行一次）
      if (!firstPickDone) {
        firstPickDone = true;
        document.querySelector('h1').classList.add('shrink');
        display.classList.add('expand');
        // 更新 currentNameSize 供后续 tick 使用
        currentNameSize.fs = 24.2;
        currentNameSize.lh = 44;
        // 给所有现存名字元素设置新字号（CSS transition 平滑过渡）
        var els = display.querySelectorAll('.name');
        for (var i = 0; i < els.length; i++) {
          els[i].style.fontSize = '24.2vh';
          els[i].style.lineHeight = '44vh';
        }
      }
    });
  }

  window.addEventListener('pywebviewready', init);
  btn.addEventListener('click', start);
</script>
</body>
</html>"""


if __name__ == '__main__':
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
    webview.start()
