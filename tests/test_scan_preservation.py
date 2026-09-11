"""Changing source roots never deletes model annotations or legacy path records."""
import json
import os
import unittest

from aihub import scan
from test_aihub import Fixture


class ScanPreservation(Fixture):
    def test_removed_source_preserves_personal_record_and_can_return(self):
        path=self.ai/'models'/'example.pt'
        path.parent.mkdir()
        path.write_bytes(b'never deserialize this synthetic model')
        self.db.upsert_model({'path':str(path),'filename':path.name,'mtype':'LoRA',
                              'rating':5,'notes':'keep this note','source_url':'https://example.invalid/model'})
        self.db.conn.execute('INSERT INTO model_labels(model_path,domain,purposes) VALUES(?,?,?)',
                             (str(path),'image',json.dumps(['style'])))
        self.db.conn.commit()
        result=scan.build_models(self.db,self.cfg,{'model_groups':{}})
        row=self.db.model_by_path(str(path))
        self.assertEqual((row['rating'],row['notes'],row['source_url'],row['missing']),
                         (5,'keep this note','https://example.invalid/model',1))
        self.assertEqual(result['pruned'],0)
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM model_labels')['n'],1)
        self.db.conn.execute("INSERT INTO files(path,category) VALUES(?,'model')",(str(path),))
        self.db.conn.commit()
        scan.build_models(self.db,self.cfg,{'model_groups':{}})
        self.assertEqual(self.db.model_by_path(str(path))['missing'],0)

    def test_missing_file_keeps_annotations(self):
        path=self.ai/'missing.pt'
        self.db.upsert_model({'path':str(path),'filename':path.name,'rating':3,'notes':'offline backup'})
        scan.build_models(self.db,self.cfg,{'model_groups':{}})
        row=self.db.model_by_path(str(path))
        self.assertEqual((row['notes'],row['rating'],row['missing']),('offline backup',3,1))

    def test_canonical_legacy_alias_stays_available(self):
        if os.name!='nt':self.skipTest('Windows junction behavior')
        import _winapi
        actual=self.ai/'models'/'actual.pt';actual.parent.mkdir();actual.write_bytes(b'fixture')
        link=self.ai/'legacy';_winapi.CreateJunction(str(actual.parent),str(link))
        try:
            alias=link/actual.name
            self.assertTrue(os.path.samefile(actual,alias))
            self.db.upsert_model({'path':str(alias),'filename':alias.name,'rating':4,'notes':'legacy'})
            self.db.conn.execute("INSERT INTO files(path,category) VALUES(?,'model')",(str(actual),))
            self.db.conn.commit()
            scan.build_models(self.db,self.cfg,{'model_groups':{}})
            row=self.db.model_by_path(str(alias))
            self.assertEqual((row['missing'],row['rating'],row['notes']),(0,4,'legacy'))
        finally:
            self.assertEqual(os.path.realpath(link),str(actual.parent))
            self.assertTrue(os.lstat(link).st_file_attributes & 0x400)
            os.rmdir(link)


if __name__=='__main__':unittest.main()
