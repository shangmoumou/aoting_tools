"""运行实际车端脚本，验证并行日志文件与时间窗筛选。"""
import ast
import contextlib
import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


class GlogOverlapTest(unittest.TestCase):
    """不同级别和进程的文件不能被当成同一条轮转序列。"""

    def test_overlapping_files(self):
        """旧文件继续写入时保留窗口内日志，排除窗口外行及未来文件。"""
        source = Path(__file__).resolve().parents[1] / 'apps/statusweb/server.py'
        # 仅解析脚本常量，避免导入服务端触发连接或依赖初始化。
        tree = ast.parse(source.read_text(encoding='utf-8'))
        script = next(ast.literal_eval(node.value) for node in tree.body
                      if isinstance(node, ast.Assign) and any(
                          isinstance(target, ast.Name) and target.id == 'GLOG_PY'
                          for target in node.targets))
        with tempfile.TemporaryDirectory() as root:
            fixtures = {
                'diagnose_info_20261028-100000.100': (
                    'I1028 10:28:59.999999  100 a.cpp:1] BEFORE\n'
                    'I1028 10:29:00.000000  100 a.cpp:1] START\n'
                    'I1028 10:30:00.000000  100 a.cpp:1] INFO\n'
                    '    stack detail\n'
                    'I1028 10:31:00.000000  100 a.cpp:1] END\n'
                    'I1028 10:31:00.000001  100 a.cpp:1] AFTER\n'),
                'diagnose_error_20261028-102000.100':
                    'E1028 10:30:01.000000  100 a.cpp:2] ERROR\n',
                'diagnose_info_20261028-102000.200':
                    'I1028 10:30:02.000000  200 a.cpp:3] OTHER_PROCESS\n',
                'diagnose_info_20261028-110000.300':
                    'I1028 11:00:00.000000  300 a.cpp:4] FUTURE\n',
            }
            for name, content in fixtures.items():
                Path(root, name).write_text(content, encoding='utf-8')
            # 与车端 mktime 使用同一时区，将测试窗口转换为秒时间戳。
            start = time.mktime(datetime.datetime(2026, 10, 28, 10, 29).timetuple())
            end = start + 120
            for levels, expected in (('', 6), ('E', 1)):
                with self.subTest(levels=levels):
                    args = ['agent', json.dumps([{'key': 'diagnose', 'dir': root}]),
                            str(start), str(end), '6000', levels]
                    output = io.StringIO()
                    # 执行原始下发脚本并捕获 JSON，验证实际筛选而非源码字符串。
                    with patch.object(sys, 'argv', args), contextlib.redirect_stdout(output):
                        exec(compile(script, 'glog_agent', 'exec'), {})
                    result = json.loads(output.getvalue())
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(result['nline'], expected)
                    self.assertEqual(len(result['dirs'][0]['picked']), 3)
                    self.assertTrue(all(start <= line['t'] <= end
                                        for line in result['lines']))
                    if not levels:
                        messages = '\n'.join(line['x'] for line in result['lines'])
                        for marker in ('START', 'INFO', 'stack detail', 'END',
                                       'ERROR', 'OTHER_PROCESS'):
                            self.assertIn(marker, messages)
                        for marker in ('BEFORE', 'AFTER', 'FUTURE'):
                            self.assertNotIn(marker, messages)


if __name__ == '__main__':
    unittest.main()
