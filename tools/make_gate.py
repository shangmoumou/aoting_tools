#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
make_gate.py —— gate.json 账号库维护工具  v5

【用途】
    生成/维护远程相机的解锁账号库。账号密码由管理员（你）统一创建，
    使用者拿到的是什么就是什么，他们无法自行修改密码。

【工作流】
    1) 你输入账号（拼音，如 zhangsan）
    2) 密码二选一：
         · 自动生成 8 位随机密码（含大写+小写+数字）
         · 手动指定（你自己输入，比如沿用 aoting321 这类内部口令）
    3) 脚本自动把密码的哈希写进 gate.json
    4) 你把「账号 + 密码」发给对应的人

    两种来源共用同一套哈希流程（PBKDF2 + 每账号独立 salt），
    所以手动设的和自动生成的账号可以混用，校验端无需改动。

    gate.json 里【只有哈希，没有明文】。所以：
      · 文件可以丢，别人拿到也反推不出密码
      · 你也看不到已创建账号的明文密码，只能重置

【哈希算法】PBKDF2-HMAC-SHA256，200000 次迭代
    为什么不用 sha256：sha256 太快，普通电脑每秒能试上千万个密码，
    gate.json 一旦外流，弱密码几分钟就被撞出来。
    PBKDF2 把每次尝试压到约 40ms，每秒只能试 25 个 —— 撞库成本
    高到不值得。代价是每次解锁多花 40ms，用户完全无感。

【每个账号独立 salt】
    不用全局 salt。这样两个人即使密码碰巧相同，哈希也不一样，
    看不出谁和谁用了同一个密码。

【v1 → v2 格式】
    v1（全局 salt + sha256）已废弃。检测到旧格式会提示重建，
    不会静默按错误方式处理。
"""
from __future__ import print_function
import os
import sys
import json
import time
import hashlib
import secrets
import string
import getpass

ALGO = 'pbkdf2-sha256'
ITER = 200000
VERSION = 2
SALT_BYTES = 16
PWD_LEN = 8

# 8 位：大写 + 小写 + 数字。三类各至少 1 个，剩下 5 位从全集里随机。
UPPER = string.ascii_uppercase
LOWER = string.ascii_lowercase
DIGIT = string.digits
ALLCH = UPPER + LOWER + DIGIT

# 容易看错的字符：0/O、1/l/I。屏幕上分不清，电话里也念不清。
CONFUSING = set('0O1lI')


def _out(s=''):
    try:
        sys.stdout.write(s + '\n')
        sys.stdout.flush()
    except Exception:
        pass


def gen_password(length=PWD_LEN):
    """生成 8 位密码：大写+小写+数字各至少 1 个，不含易混淆字符。

    去掉 0/O/1/l/I 之后每类还剩 8 个字母 + 8 个数字，
    8 位密码的组合数约 2e14，配 PBKDF2 足够。
    """
    up = ''.join(c for c in UPPER if c not in CONFUSING)
    lo = ''.join(c for c in LOWER if c not in CONFUSING)
    di = ''.join(c for c in DIGIT if c not in CONFUSING)
    pool = up + lo + di

    while True:
        chars = [secrets.choice(up), secrets.choice(lo), secrets.choice(di)]
        chars += [secrets.choice(pool) for _ in range(length - 3)]
        # 打乱，否则前三位固定是「大写、小写、数字」的顺序
        for i in range(len(chars) - 1, 0, -1):
            j = secrets.randbelow(i + 1)
            chars[i], chars[j] = chars[j], chars[i]
        pwd = ''.join(chars)
        if _strong_enough(pwd, length):
            return pwd


def _strong_enough(pwd, length=PWD_LEN):
    """只做最基本的自校验（自动生成的密码理论上必过）。

    用户要求不要复杂校验，所以这里只挡住「三类字符没凑齐」
    这类生成 bug，不限制长度、不查字典。
    """
    if len(pwd) != length:
        return False
    if not any(c in UPPER for c in pwd):
        return False
    if not any(c in LOWER for c in pwd):
        return False
    if not any(c in DIGIT for c in pwd):
        return False
    if any(c in CONFUSING for c in pwd):
        return False
    return True


def hash_password(pwd, salt_hex=None):
    """返回 (salt_hex, hash_hex)。salt 不传则随机生成。"""
    if salt_hex is None:
        salt_hex = secrets.token_hex(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac('sha256', pwd.encode('utf-8'),
                             bytes.fromhex(salt_hex), ITER)
    return salt_hex, dk.hex()


def verify_password(pwd, salt_hex, hash_hex):
    """校验密码。用 hmac.compare_digest 防计时侧信道。"""
    try:
        import hmac
        _, h = hash_password(pwd, salt_hex)
        return hmac.compare_digest(h, hash_hex)
    except Exception:
        return False


def warn_weak(pwd):
    """弱密码只提示，不拦。返回警告条数。

    用户明确要求不要复杂校验，所以这里只把风险摆出来，
    最终用不用由管理员自己定。
    """
    n = 0
    if len(pwd) < 10:
        _out('    [!] 警告: 密码只有 %d 位，建议 >= 10 位' % len(pwd))
        n += 1
    if pwd.isdigit():
        _out('    [!] 警告: 密码是纯数字，建议混入字母')
        n += 1
    elif not any(c in DIGIT for c in pwd):
        _out('    [!] 警告: 密码没有数字，建议混入数字')
        n += 1
    low = pwd.lower()
    for w in ('aoting', '123456', 'password', 'passwd', 'qwerty', 'admin'):
        if w in low:
            _out('    [!] 警告: 密码包含常见词 "%s"' % w)
            n += 1
            break
    return n


def _read_pwd(prompt):
    """读密码。有终端就隐藏输入，没有终端就退回普通 input。

    为什么要判断：getpass 会去读 /dev/tty 而不是 stdin。在没有真实
    终端的环境里（IDE 的运行按钮、管道、某些远程会话）它会一直等
    那个永远不会有输入的 tty —— 表现是脚本直接挂住，用户以为死了。
    """
    try:
        if hasattr(sys.stdin, 'isatty') and sys.stdin.isatty():
            return getpass.getpass(prompt)
    except Exception:
        pass
    try:
        # 非交互终端：回显就回显，至少能用，总比卡住强
        return input(prompt)
    except Exception:
        return ''


def input_password():
    """手动输入密码。返回密码，或 None（取消/放弃）。

    输两次防手滑；弱密码只提示不拦，最终由管理员决定。
    """
    while True:
        pwd = _read_pwd('    密码（输入时不显示）: ')
        if not pwd:
            _out('    [!] 密码不能为空。')
            return None
        pwd2 = _read_pwd('    再输一次: ')
        if pwd != pwd2:
            _out('    [!] 两次不一致，请重新输入。')
            continue
        if warn_weak(pwd):
            try:
                ans = input('    仍要使用？(y/N): ').strip().lower()
            except Exception:
                ans = 'n'
            if ans != 'y':
                _out('    已放弃，未做任何改动。')
                return None
        return pwd


def default_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gate.json')


def load(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save(path, data):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def is_v2(data):
    return isinstance(data, dict) and data.get('version') == VERSION \
        and isinstance(data.get('codes'), dict)


def new_data():
    return {
        'version': VERSION,
        'algo': ALGO,
        'iterations': ITER,
        'updated': time.strftime('%Y-%m-%d'),
        'note': '内部口令库。codes 里只存哈希与 salt，不存明文。',
        'codes': {},
    }


def show_accounts(data):
    codes = data.get('codes') or {}
    if not codes:
        return '（暂无账号）'
    return ', '.join(sorted(codes.keys()))


def need_v2(data, path):
    """旧格式统一拦下，避免按错误方式继续操作。"""
    if data is None:
        _out('  [!] 还没有 gate.json，请先选 1 创建。')
        return False
    if not is_v2(data):
        _out('  [!] 这个 gate.json 是旧格式（v%s）。' % data.get('version'))
        _out('      v4 使用 PBKDF2，格式不兼容，需要重建。')
        _out('      请先把 %s 改名备份，然后选 1 重新创建。' % os.path.basename(path))
        return False
    return True


def act_create(path):
    if os.path.exists(path):
        ans = input('  gate.json 已存在，重建会清空所有账号！输入 yes 确认: ').strip()
        if ans.lower() != 'yes':
            _out('  已取消。')
            return
    data = new_data()
    save(path, data)
    _out('  [OK] 已创建: %s' % path)
    _out('       算法=%s  迭代=%d' % (ALGO, ITER))
    _out('       接下来选 2 添加账号。')


def act_add(path, data, manual=False):
    _out('  当前账号: %s' % show_accounts(data))
    _out('  （直接回车结束）')
    while True:
        name = input('  账号: ').strip()
        if not name:
            break
        if name in data['codes']:
            _out('    [!] %s 已存在。要重设密码请选 4 或 5。' % name)
            continue
        if manual:
            pwd = input_password()
            if pwd is None:
                continue
        else:
            pwd = gen_password()
        salt, h = hash_password(pwd)
        data['codes'][name] = {'salt': salt, 'hash': h}
        data['updated'] = time.strftime('%Y-%m-%d')
        save(path, data)
        _out('    [OK] 已添加 %s' % name)
        if manual:
            _out('       密码: 你指定的那个（已写入哈希）')
        else:
            _out('       密码: %s      ← 复制发给本人，只显示这一次' % pwd)
        _out('')


def act_reset(path, data, manual=False):
    _out('  当前账号: %s' % show_accounts(data))
    name = input('  要重置谁的密码: ').strip()
    if not name:
        return
    if name not in data['codes']:
        _out('    [!] 没有这个账号: %s' % name)
        return
    if manual:
        pwd = input_password()
        if pwd is None:
            return
    else:
        pwd = gen_password()
    salt, h = hash_password(pwd)
    data['codes'][name] = {'salt': salt, 'hash': h}
    data['updated'] = time.strftime('%Y-%m-%d')
    save(path, data)
    _out('    [OK] 已重置 %s' % name)
    if manual:
        _out('       新密码: 你指定的那个（已写入哈希）')
    else:
        _out('       新密码: %s      ← 复制发给本人，只显示这一次' % pwd)
    _out('       其他 %d 个账号不受影响。' % (len(data['codes']) - 1))


def act_del(path, data):
    _out('  当前账号: %s' % show_accounts(data))
    name = input('  要删除谁: ').strip()
    if not name:
        return
    if name not in data['codes']:
        _out('    [!] 没有这个账号: %s' % name)
        return
    ans = input('  确认删除 %s？(yes/no): ' % name).strip().lower()
    if ans != 'yes':
        _out('  已取消。')
        return
    del data['codes'][name]
    data['updated'] = time.strftime('%Y-%m-%d')
    save(path, data)
    _out('    [OK] 已删除 %s，剩余 %d 个账号。' % (name, len(data['codes'])))


def act_list(path, data):
    codes = data.get('codes') or {}
    _out('  文件: %s' % path)
    _out('  算法: %s  迭代: %s  版本: v%s' % (
        data.get('algo'), data.get('iterations'), data.get('version')))
    _out('  更新: %s' % data.get('updated'))
    _out('  账号数: %d' % len(codes))
    for n in sorted(codes):
        v = codes[n]
        _out('    - %-16s salt=%s...  hash=%s...' % (
            n, str(v.get('salt', ''))[:8], str(v.get('hash', ''))[:12]))
    if not codes:
        _out('    （暂无账号，选 2 添加）')


MENU = """
==============================================
 gate.json 账号库维护工具  v5   (PBKDF2)
==============================================
 1) 创建新的 gate.json        (第一次用)
 2) 添加账号（自动生成密码）
 3) 添加账号（手动指定密码）
 4) 重置密码（自动生成）
 5) 重置密码（手动指定）
 6) 删除账号
 7) 查看账号列表
 0) 退出

 文件: %s
 账号: %s
"""


def main():
    path = default_path()
    while True:
        data = load(path)
        _out(MENU % (path, show_accounts(data) if data else '（无文件）'))
        c = input('请选择: ').strip()
        if c == '0':
            _out('  再见。')
            return 0
        if c == '7':
            if not need_v2(data, path):
                continue
            act_list(path, data)
            continue
        if c == '1':
            act_create(path)
            continue
        if c in ('2', '3', '4', '5', '6'):
            if not need_v2(data, path):
                continue
            if c == '2':
                act_add(path, data, manual=False)
            elif c == '3':
                act_add(path, data, manual=True)
            elif c == '4':
                act_reset(path, data, manual=False)
            elif c == '5':
                act_reset(path, data, manual=True)
            else:
                act_del(path, data)
            continue
        _out('  无效选项。')


if __name__ == '__main__':
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        _out('\n  已中断。')
        sys.exit(130)
    except EOFError:
        _out('\n  [!] 没有交互输入（可能是在 IDE 里运行的）。')
        _out('      请在命令行执行: python make_gate.py')
        sys.exit(1)
