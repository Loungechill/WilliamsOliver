import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from segment_feed import build_feed, exclusions


class SegmentFeedTests(unittest.TestCase):
    def test_threshold_missing_products_brands_and_preserved_data(self):
        source = b'''<yml_catalog date="2026-10-09 17:00"><shop><categories><category id="cat" parentId="root">Example</category><category id="root">Root</category></categories><offers>
        <offer id="00000000001"><vendor>Keep</vendor><categoryId>cat</categoryId><price>12</price></offer>
        <offer id="00000000002"><vendor>Keep</vendor><categoryId>cat</categoryId><price>34</price></offer>
        <offer id="00000000003"><vendor>Keep</vendor><categoryId>cat</categoryId></offer>
        <offer id="00000000004"><vendor>Keep</vendor><categoryId>cat</categoryId></offer>
        <offer id="00000000005"><vendor> BLOCKED  BRAND </vendor><categoryId>cat</categoryId></offer>
        </offers></shop></yml_catalog>'''
        csv = 'ID товара,Название товара или каталога,Показы\n00000000001,"Name, quoted",1001\n00000000002,Equal,1000\n00000000003,Less,999\n00000000001,Repeated,1\nbrands/test,Catalog,99999\n'
        with tempfile.TemporaryDirectory() as folder:
            blacklist, output = Path(folder)/'blocked.txt', Path(folder)/'feed.xml'
            blacklist.write_text('Blocked Brand\n')
            stats = build_feed(source, csv, blacklist, output, 1000)
            result = ET.parse(output)
            kept = result.findall('./shop/offers/offer')
            self.assertEqual([o.get('id') for o in kept], ['00000000002','00000000003','00000000004'])
            original = ET.fromstring(source)
            self.assertEqual([ET.tostring(o) for o in kept], [ET.tostring(o) for o in original.findall('./shop/offers/offer')[1:4]])
            self.assertEqual(ET.tostring(result.find('./shop/categories')), ET.tostring(original.find('./shop/categories')))
            self.assertEqual(stats['removed_brands'], 1)
            self.assertEqual(stats['removed_impressions'], 1)

    def test_errors_do_not_replace_previous_feed(self):
        with tempfile.TemporaryDirectory() as folder:
            blacklist, output = Path(folder)/'blocked.txt', Path(folder)/'feed.xml'
            blacklist.write_text('Blocked\n')
            output.write_text('previous good feed')
            for csv in ['ID товара,Показы\n00000000001,garbage\n', 'Wrong,Header\n', 'ID товара,Показы\n']:
                with self.assertRaises(ValueError):
                    build_feed(b'<invalid/>', csv, blacklist, output, 1000)
                self.assertEqual(output.read_text(), 'previous good feed')

    def test_csv_quotes_newlines_bom_and_spaced_impressions(self):
        blocked, _, ignored = exclusions('\ufeffID товара,Название товара или каталога,Показы\r\n00000000001,"Name with ""quote""\nand newline",1\u00a0001\r\n', 1000)
        self.assertEqual(blocked, {'00000000001'})
        self.assertEqual(ignored, 0)


if __name__ == '__main__':
    unittest.main()
