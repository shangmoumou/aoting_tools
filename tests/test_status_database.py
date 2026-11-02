"""验证真实 SQLite WAL 快照、分页筛选、导出及参数边界。"""
import csv
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

MODULE = Path(__file__).resolve().parents[1] / 'apps/statusweb/database_tools.py'
spec = importlib.util.spec_from_file_location('database_tools', MODULE)
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'data.db'
        self.conn = sqlite3.connect(self.path)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('CREATE TABLE events(id INTEGER, timestamp INTEGER, message TEXT, payload BLOB)')
        self.conn.executemany('INSERT INTO events VALUES(?,?,?,?)',
                              [(i, 1700000000+i, 'item %d' % i, b'abc') for i in range(205)])
        self.conn.commit()
        db.SNAPSHOTS.clear()
        db.SNAPSHOTS['test'] = {'path': str(self.path), 'created': time.time(), 'name': 'tasks_db.db'}

    def tearDown(self):
        db.SNAPSHOTS.clear()
        self.conn.close()
        self.temp.cleanup()

    def test_online_backup_includes_committed_wal(self):
        """直接运行实际下发脚本，只替换测试机的临时目录。"""
        script = db.REMOTE_BACKUP.replace('dir="/tmp"', 'dir=' + repr(self.temp.name))
        result = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        with sqlite3.connect(snapshot['path']) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM events').fetchone()[0], 205)
            self.assertEqual(connection.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        connection.close()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM events').fetchone()[0], 205)

    def test_paging_range_and_sort(self):
        result = db.browse({'token':'test','table':'events'})
        self.assertEqual(len(result['rows']), 100)
        self.assertTrue(result['more'])
        self.assertEqual(result['rows'][0][3], '[BLOB 3 bytes]')
        result = db.browse({'token':'test','table':'events','page':2})
        self.assertEqual(len(result['rows']), 5)
        self.assertFalse(result['more'])
        result = db.browse({'token':'test','table':'events','field':'timestamp',
                            'start':'1700000010','end':'1700000012','sort':'id','desc':True})
        self.assertEqual([r[0] for r in result['rows']], [12,11,10])
        result = db.browse({'token':'test','table':'events','field':'message','value':'item 204'})
        self.assertEqual(result['rows'][0][0], 204)

    def test_untrusted_names_and_expired_token(self):
        for args in ({'table':'events; DROP TABLE events'}, {'table':'events','sort':'id; DELETE'},
                     {'table':'events','field':'missing','value':'x'}):
            with self.assertRaises(ValueError):
                db.browse({'token':'test',**args})
        db.SNAPSHOTS['test']['created'] -= 3601
        with self.assertRaises(ValueError):
            db.download('test')
        with self.assertRaises(ValueError):
            db.capture(None, '../../outside.db', None)

    def test_csv_and_zip(self):
        self.conn.execute("UPDATE events SET message='=1+1' WHERE id=0")
        self.conn.commit()
        content=db.browse({'token':'test','table':'events'}, export=True).decode('utf-8-sig')
        rows=list(csv.reader(io.StringIO(content)))
        self.assertEqual(rows[1][2], "'=1+1")
        self.assertEqual(rows[1][3], 'base64:YWJj')
        with zipfile.ZipFile(io.BytesIO(db.archive(['test']))) as bundle:
            self.assertIn('db/tasks_db.db',bundle.namelist())
            self.assertIn('manifest.json',bundle.namelist())
        self.conn.executemany('INSERT INTO events(id) VALUES(?)', [(i,) for i in range(10001)])
        self.conn.commit()
        with self.assertRaisesRegex(ValueError,'10000'):
            db.browse({'token':'test','table':'events'}, export=True)


if __name__ == '__main__':
    unittest.main()
