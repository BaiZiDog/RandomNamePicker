# -*- coding: utf-8 -*-
"""版本检测测试脚本 —— 只测试版本比较逻辑与数据源，不执行下载和更新

用法:
  python versiontest.py              使用 helper.py 中的 LOCAL_VERSION
  python versiontest.py v2.0.a       指定本地版本进行测试
"""
import sys

from helper import (
    LOCAL_VERSION,
    ZIP_ASSET_NAME,
    _asset_url,
    _version_gt,
    get_latest_tag,
)

# 版本号比较自检用例：(较新, 较旧, 期望结果)
CASES = [
    ('v1.0', 'v1.0', False),
    ('v1.0.a', 'v1.0', True),      # 补丁 > 无补丁
    ('v1.0', 'v1.0.a', False),
    ('v1.0.b', 'v1.0.a', True),    # 字母顺序
    ('v1.0.a', 'v1.0.b', False),
    ('v1.1', 'v1.0.z', True),      # 次版本优先于补丁
    ('v2.0', 'v1.9.z', True),      # 主版本优先
    ('v1.9', 'v2.0', False),
    ('v2.0', 'v2.0', False),
]


def run_version_cases():
    print('\n[1] 版本号比较自检 ...')
    failed = 0
    for a, b, expect in CASES:
        got = _version_gt(a, b)
        mark = 'OK  ' if got == expect else 'FAIL'
        if got != expect:
            failed += 1
        print(f'  [{mark}] _version_gt({a}, {b}) = {got}（期望 {expect}）')
    print(f'  {len(CASES) - failed}/{len(CASES)} 通过')
    return failed == 0


def main():
    local_tag = sys.argv[1] if len(sys.argv) > 1 else LOCAL_VERSION

    print('=' * 50)
    print('版本检测测试')
    print('=' * 50)
    print(f'本地版本：{local_tag}')

    ok = run_version_cases()

    print('\n[2] 获取 GitHub 最新 tag（releases.atom，不走 API 配额）...')
    remote_tag = get_latest_tag()
    if not remote_tag:
        print('  FAIL 无法获取最新 tag')
        sys.exit(1)
    print(f'  OK 最新版本：{remote_tag}')

    print('\n[3] 版本比较结果 ...')
    if _version_gt(remote_tag, local_tag):
        print(f'  发现新版本（本地 {local_tag} < 远程 {remote_tag}）')
        print('  → 正式运行时会下载并应用更新')
    else:
        print(f'  已是最新版本（本地 {local_tag} >= 远程 {remote_tag}）')

    print('\n[4] 更新包地址（按约定名拼接）...')
    print(f'  {_asset_url(remote_tag, ZIP_ASSET_NAME)}')

    print('\n' + '=' * 50)
    print('测试完成' if ok else '测试完成（版本比较自检存在失败项）')


if __name__ == '__main__':
    main()