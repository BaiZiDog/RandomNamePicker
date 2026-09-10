# -*- coding: utf-8 -*-
"""PyInstaller 打包脚本：生成单文件 exe，名单文件保留在 exe 旁可编辑"""
import os
import shutil

import PyInstaller.__main__

if __name__ == '__main__':
    # 清理上次产物
    for p in ('build', 'dist'):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)

    PyInstaller.__main__.run([
        'app.py',
        '--windowed',            # 无控制台窗口
        '--name', 'RandomNamePicker',
        '--add-data', 'data;.\data',
        '--contents-directory', '.',
        '--collect-all', 'webview',   # pywebview 及其动态加载的平台后端
        '--collect-all', 'clr_loader',
        '--collect-all', 'pythonnet',
        '--collect-all', 'proxy_tools',
        '--collect-all', 'bottle',
        '--noconfirm',
    ])

    # 将名单文件复制到 dist/data 目录，方便用户编辑
    dist_dir = os.path.join('dist', 'RandomNamePicker')
    if not os.path.isdir(dist_dir):
        dist_dir = 'dist'
    data_out = os.path.join(dist_dir, 'data')
    os.makedirs(data_out, exist_ok=True)
    for f in ('example.txt', 'file.txt'):
        src = os.path.join('data', f)
        if os.path.exists(src):
            shutil.copy2(src, data_out)

    print(f'打包完成：{dist_dir}/RandomNamePicker.exe + data/名单文件')
