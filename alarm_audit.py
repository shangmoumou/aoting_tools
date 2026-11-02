import zipfile
import xml.etree.ElementTree as E
import json
from pathlib import Path

# Read cell values directly to tolerate nonstandard source workbook styles.
ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
books = []
for p in [Path(r'C:\Users\79940\Desktop\（新）翻译汇总表20261021.xlsx'), Path(r'C:\Users\79940\Desktop\健康监控.xlsx')]:
    z = zipfile.ZipFile(p)
    ss = []
    if 'xl/sharedStrings.xml' in z.namelist():
        ss = [''.join(x.itertext()) for x in E.fromstring(z.read('xl/sharedStrings.xml')).findall('m:si', ns)]
    rel = {r.attrib['Id']: r.attrib['Target'] for r in E.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
    sheets = []
    for s in E.fromstring(z.read('xl/workbook.xml')).find('m:sheets', ns):
        target = rel[s.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']]
        target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
        rows = []
        for row in E.fromstring(z.read(target)).findall('.//m:sheetData/m:row', ns):
            cells = {}
            for c in row:
                v = c.find('m:v', ns)
                val = v.text if v is not None and v.text else ''
                if c.attrib.get('t') == 's' and val:
                    val = ss[int(val)]
                elif c.attrib.get('t') == 'inlineStr':
                    val = ''.join(c.find('m:is', ns).itertext())
                if val:
                    cells[c.attrib['r']] = val
            if cells:
                rows.append(cells)
        sheets.append({'name': s.attrib['name'], 'rows': rows})
        print(p.name, s.attrib['name'], len(rows))
        print(json.dumps(rows[:5], ensure_ascii=False))
        print('EXAMPLE', json.dumps([r for r in rows if any('回桩成功' in v for v in r.values())], ensure_ascii=False))
    books.append({'path': str(p), 'sheets': sheets})
Path('alarm_audit_data.json').write_text(json.dumps(books, ensure_ascii=False), encoding='utf-8')
