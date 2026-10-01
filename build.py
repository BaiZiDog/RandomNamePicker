# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本：编译 helper.exe 和 RandomNamePicker.exe

用法:
  python build.py          编译全部（helper + 主程序）
  python build.py --helper 仅编译 helper.exe
  python build.py --main   仅编译 RandomNamePicker.exe
"""
import os
import sys

import PyInstaller.__main__

# 输出目录（两个 exe 共用）
OUT_DIR = os.path.join('dist', 'RandomNamePicker')


def build_helper():
    """编译 helper.exe（纯 stdlib，单文件，体积小）"""
    print('[2/2] 编译 helper.exe ...')
    PyInstaller.__main__.run([
        'helper.py',
        '--windowed',
        '--name', 'helper',
        '--onefile',              # 单文件：运行时解压到临时目录，不依赖程序目录
        '--distpath', OUT_DIR,
        '--workpath', os.path.join('build', 'helper'),
        '--specpath', 'build',
        '--noconfirm',
    ])
    exe = os.path.join(OUT_DIR, 'helper.exe')
    if os.path.exists(exe):
        print(f'  OK helper.exe ({os.path.getsize(exe) / 1024 / 1024:.1f} MB)')
    else:
        print('  FAIL helper.exe 编译失败')


def build_main():
    """编译 RandomNamePicker.exe（含 pywebview 等依赖）"""
    print('[1/2] 编译 RandomNamePicker.exe ...')
    # 用绝对路径，避免 --specpath 改变工作目录后找不到 data
    data_src = os.path.abspath('data')
    # distpath 用 dist，PyInstaller 会生成 dist/RandomNamePicker/（即 OUT_DIR）
    PyInstaller.__main__.run([
        'app.py',
        '--windowed',
        '--name', 'RandomNamePicker',
        '--add-data', f'{data_src}{os.pathsep}data',
        '--contents-directory', '.',
        '--distpath', 'dist',
        '--workpath', os.path.join('build', 'main'),
        '--specpath', 'build',
        '--collect-all', 'webview',
        '--collect-all', 'clr_loader',
        '--collect-all', 'pythonnet',
        '--collect-all', 'proxy_tools',
        '--collect-all', 'bottle',
        '--noconfirm',
    ])
    exe = os.path.join(OUT_DIR, 'RandomNamePicker.exe')
    if os.path.exists(exe):
        print(f'  OK RandomNamePicker.exe ({os.path.getsize(exe) / 1024 / 1024:.1f} MB)')
    else:
        print('  FAIL RandomNamePicker.exe 编译失败')


if __name__ == '__main__':
    args = sys.argv[1:]
    # --all 与具体目标互斥：混用时"到底编译哪个"不可预期，直接拒绝而不是静默取其一
    if '--all' in args and ('--helper' in args or '--main' in args):
        print('参数冲突：--all 不能与 --helper / --main 同时使用')
        sys.exit(2)

    build_all = not args or '--all' in args
    build_h = build_all or '--helper' in args
    build_m = build_all or '--main' in args

    if build_m:
        build_main()
    if build_h:
        build_helper()

    # 输出目录不存在时（首次打包失败/参数写错）直接给出可读提示，避免 listdir 抛栈
    if not os.path.isdir(OUT_DIR):
        print(f'输出目录不存在（打包可能失败）：{OUT_DIR}')
        sys.exit(1)

    print(f'\n打包完成：{OUT_DIR}/')
    for f in sorted(os.listdir(OUT_DIR)):
        fp = os.path.join(OUT_DIR, f)
        if os.path.isfile(fp):
            size = os.path.getsize(fp)
            if size > 1024 * 1024:
                print(f'  {f} ({size / 1024 / 1024:.1f} MB)')
            else:
                print(f'  {f} ({size / 1024:.1f} KB)')
        elif os.path.isdir(fp):
            print(f'  {f}/')
