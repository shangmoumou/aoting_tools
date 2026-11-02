"""固定七份 SQLite 数据库的只读快照与浏览，不接受任意路径或 SQL。"""
import base64
import csv
import datetime
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
import atexit
from pathlib import Path

DATABASES = {name: '/home/aoting/user_config/db/' + name for name in (
    'cloudsync.db', 'diagnose_db.db', 'mqtt.db', 'tasks_db.db',
    'tasks_monitor_db.db', 'tasks_record_db.db')}
DATABASES['maps.db'] = '/home/aoting/user_config/map_database/maps.db'
MAX_BYTES = 100 * 1024 * 1024
SNAPSHOTS = {}
LOCK = threading.Lock()


def cleanup(expired_only=True):
    """删除过期快照；进程正常退出时删除全部快照临时文件。"""
    with LOCK:
        for token, item in list(SNAPSHOTS.items()):
            if not expired_only or time.time() - item['created'] > 3600:
                try:
                    os.unlink(item['path'])
                    del SNAPSHOTS[token]
                except OSError:
                    pass


atexit.register(cleanup, False)

# 备份源以只读方式打开，sqlite backup 包含已提交 WAL，避免直接复制漏数据。
REMOTE_BACKUP = r'''
import sqlite3, tempfile, json, os, sys, time
from pathlib import Path
source = Path(sys.argv[1])
if not source.is_file():
    raise RuntimeError("数据库文件不存在")
if source.stat().st_size > 104857600:
    raise RuntimeError("数据库超过 100 MiB 限制")
fd, target = tempfile.mkstemp(prefix="aoting_db_", suffix=".db", dir="/tmp")
os.close(fd)
started = time.monotonic()
def progress(status, remaining, total):
    if time.monotonic() - started > 45:
        raise RuntimeError("数据库繁忙，快照超时，请稍后重试")
    if os.path.getsize(target) > 104857600:
        raise RuntimeError("快照超过 100 MiB 限制")
try:
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
    print(json.dumps({"path": target, "size": os.path.getsize(target)}))
except Exception:
    os.unlink(target)
    raise
'''


def quote_identifier(value):
    """SQLite 标识符转义；名称还必须经过实际表和字段清单校验。"""
    return '"' + value.replace('"', '""') + '"'


def readonly(path):
    """返回只读本地连接，限制单次查询耗时，避免大表查询挂住服务。"""
    conn = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
    deadline = time.monotonic() + 8
    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    return conn


def listing(ssh, shell_quote):
    """仅远程 stat 固定清单，不打开数据库，返回文件大小及修改时间。"""
    script = '''import os,json,sys
out=[]
for name,path in json.loads(sys.argv[1]).items():
    try:
        st=os.stat(path)
        out.append({"name":name,"path":path,"size":st.st_size,"mtime":st.st_mtime})
    except OSError as e:
        out.append({"name":name,"path":path,"err":str(e)})
print(json.dumps(out))
'''
    _, stdout, stderr = ssh.exec_command('python3 -c %s %s' % (
        shell_quote(script), shell_quote(json.dumps(DATABASES))), timeout=15)
    stdout.channel.settimeout(15)
    data = stdout.read().decode('utf-8', 'replace')
    if stdout.channel.recv_exit_status() != 0:
        raise ValueError('读取文件状态失败：' + stderr.read().decode('utf-8', 'replace')[-300:])
    return {'ok': True, 'files': json.loads(data)}


def capture(ssh, name, shell_quote):
    """车端生成一致性快照，通过 SFTP 拉回，始终清理远程临时文件。"""
    if name not in DATABASES:
        raise ValueError('数据库不在允许清单中')
    remote = None
    local = None
    sftp = None
    try:
        command = 'python3 -u -c %s %s' % (shell_quote(REMOTE_BACKUP), shell_quote(DATABASES[name]))
        _, stdout, stderr = ssh.exec_command(command, timeout=60)
        stdout.channel.settimeout(60)
        raw = stdout.read().decode('utf-8', 'replace')
        errors = stderr.read().decode('utf-8', 'replace')
        if stdout.channel.recv_exit_status() != 0:
            raise ValueError('车端快照失败：' + errors[-600:])
        info = json.loads(raw.strip().splitlines()[-1])
        remote = info['path']
        if not remote.startswith('/tmp/aoting_db_') or '/' in remote[len('/tmp/'):]:
            raise ValueError('车端临时路径无效')
        if info['size'] > MAX_BYTES:
            raise ValueError('快照超过 100 MiB 限制')
        fd, local = tempfile.mkstemp(prefix='aoting_snapshot_', suffix='.db')
        os.close(fd)
        sftp = ssh.open_sftp()
        sftp.get_channel().settimeout(60)
        sftp.get(remote, local)
        conn = readonly(local)
        try:
            # 读取表清单验证下载内容为 SQLite，查询不在车机上执行。
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        finally:
            conn.close()
        token = uuid.uuid4().hex
        with LOCK:
            # 清理过期快照并限制最多保留 14 份，避免长期使用占满磁盘。
            for key, item in list(SNAPSHOTS.items()):
                if time.time() - item['created'] > 3600 or len(SNAPSHOTS) >= 14:
                    os.unlink(item['path'])
                    del SNAPSHOTS[key]
            SNAPSHOTS[token] = {'path': local, 'created': time.time(), 'name': name}
        local = None
        return {'ok': True, 'token': token, 'tables': tables, 'name': name,
                'size': info['size'], 'at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
    finally:
        if sftp:
            if remote:
                try:
                    sftp.remove(remote)
                except OSError:
                    pass
            sftp.close()
        elif remote:
            # 下载通道创建失败时仍尝试删除车端备份。
            try:
                cleanup = ssh.open_sftp()
                cleanup.remove(remote)
                cleanup.close()
            except Exception:
                pass
        if local:
            os.unlink(local)


def browse(body, export=False):
    """对本地快照做分页、单字段包含筛选及排序，返回数据或 CSV 字节。"""
    with LOCK:
        item = SNAPSHOTS.get(body.get('token'))
        if not item or time.time() - item['created'] > 3600:
            raise ValueError('快照已过期，请重新获取')
        conn = readonly(item['path'])
    try:
        table = body.get('table', '')
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        if table not in tables:
            raise ValueError('请选择有效的数据表')
        # table_info 读取真实字段，用于校验筛选和排序参数。
        schema = [dict(zip(('id', 'name', 'type', 'notnull', 'default', 'pk'), r))
                  for r in conn.execute('PRAGMA table_info(%s)' % quote_identifier(table))]
        columns = [r['name'] for r in schema]
        field, value = body.get('field', ''), str(body.get('value', ''))
        sort = body.get('sort', '')
        if (field and field not in columns) or (sort and sort not in columns):
            raise ValueError('字段无效')
        sql = 'SELECT * FROM ' + quote_identifier(table)
        params = []
        if value:
            if not field:
                raise ValueError('请先选择筛选字段')
            sql += ' WHERE instr(lower(CAST(%s AS TEXT)), lower(?)) > 0' % quote_identifier(field)
            params.append(value)
        # 范围值保持字段原生单位：ISO 时间文本或 Unix 秒/毫秒不自动猜测转换。
        for key, operator in (('start', '>='), ('end', '<=')):
            bound = body.get(key, '')
            if bound != '':
                if not field:
                    raise ValueError('范围查询需要选择字段')
                sql += (' AND ' if ' WHERE ' in sql else ' WHERE ') + quote_identifier(field) + ' ' + operator + ' ?'
                params.append(bound)
        if sort:
            sql += ' ORDER BY ' + quote_identifier(sort) + (' DESC' if body.get('desc') else ' ASC')
        page = max(0, min(100000, int(body.get('page', 0))))
        limit = 10000 if export else 100
        rows = conn.execute(sql + ' LIMIT ? OFFSET ?', params + [limit + 1, 0 if export else page * limit]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        if export:
            if more:
                raise ValueError('结果超过 10000 行，请缩小筛选范围或下载完整快照')
            out = io.StringIO()
            writer = csv.writer(out)
            writer.writerow(columns)
            # 避免文本字段被表格软件当成公式执行。
            writer.writerows([csv_value(v) for v in row] for row in rows)
            return out.getvalue().encode('utf-8-sig')
        return {'ok': True, 'columns': columns, 'schema': schema, 'page': page, 'more': more,
                'rows': [[display_value(v) for v in row] for row in rows]}
    finally:
        conn.close()


def display_value(value):
    """限制单元格预览长度，二进制只展示大小，完整数据保留在快照中。"""
    if isinstance(value, bytes):
        return '[BLOB %d bytes]' % len(value)
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + '…（预览截断）'
    return value


def csv_value(value):
    """CSV 二进制以 base64 输出，保护可能被识别为公式的文本。"""
    if isinstance(value, bytes):
        return 'base64:' + base64.b64encode(value).decode('ascii')
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def download(token):
    """读取已登记的本地快照，客户端不能指定任意本地路径。"""
    with LOCK:
        item = SNAPSHOTS.get(token)
        if not item or time.time() - item['created'] > 3600:
            raise ValueError('快照已过期，请重新获取')
        return Path(item['path']).read_bytes()


def archive(tokens):
    """打包已生成的快照与清单，保留来源目录结构，限制批量总大小。"""
    if not isinstance(tokens, list) or not 1 <= len(tokens) <= 7:
        raise ValueError('请选择 1 至 7 份快照')
    output = io.BytesIO()
    with LOCK, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as bundle:
        items = [SNAPSHOTS.get(token) for token in tokens]
        if any(not item or time.time() - item['created'] > 3600 for item in items):
            raise ValueError('快照已过期，请重新获取')
        if sum(os.path.getsize(item['path']) for item in items) > 200 * 1024 * 1024:
            raise ValueError('批量总大小超过 200 MiB，请逐份下载')
        manifest = []
        for item in items:
            folder = 'map_database' if item['name'] == 'maps.db' else 'db'
            bundle.write(item['path'], folder + '/' + item['name'])
            manifest.append({'name': item['name'], 'source': DATABASES[item['name']],
                             'captured_at': item['created']})
        bundle.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    return output.getvalue()
