import unittest
import test_organization_http as fixtures


class FileLifecycleHTTP(unittest.TestCase):
    setUp=fixtures.OrganizationHTTP.setUp
    tearDown=fixtures.OrganizationHTTP.tearDown
    patch=fixtures.OrganizationHTTP.patch
    request=fixtures.OrganizationHTTP.request
    ok=fixtures.OrganizationHTTP.ok

    def test_real_http_scope_registration_and_recursive_preview(self):
        self.cfg['ai_root']=str(self.root)
        path=self.root/'project/work/deep/cache.tmp';path.parent.mkdir(parents=True);path.write_bytes(b'test')
        self.assertEqual(self.ok('GET','/api/file-lifecycle')['records'],[])
        result=self.ok('POST','/api/file-lifecycle/inventory',{'_workspace_root':str(self.root)})
        self.assertEqual(result['summary']['files'],1);self.assertEqual(result['items'][0]['decision'],'skip')
        record={'id':'project','name':'Project','kind':'project','path':str(self.root/'project'),'purpose':'unknown','state':'active'}
        preview=self.ok('POST','/api/file-lifecycle/registration-preview',{'_workspace_root':str(self.root),'record':record})
        self.ok('POST','/api/file-lifecycle/registration-save',{'_workspace_root':str(self.root),'token':preview['token']})
        result=self.ok('POST','/api/file-lifecycle/inventory',{'_workspace_root':str(self.root)})
        self.assertEqual(result['items'][0]['decision'],'protected')
        self.assertEqual(self.request('POST','/api/file-lifecycle/inventory',{'_workspace_root':'wrong'})[0],409)
        self.assertEqual(self.request('POST','/api/file-lifecycle/delete',{'_workspace_root':str(self.root)})[0],404)
        self.assertEqual(path.read_bytes(),b'test')

    def test_actual_frontend_files_and_route_are_served(self):
        self.assertIn('data-page="lifecycle"',self.ok('GET','/'))
        self.assertIn('递归预览文件',self.ok('GET','/file-lifecycle.js'))
        self.assertIn('.fl-metrics',self.ok('GET','/file-lifecycle.css'))


if __name__=='__main__':unittest.main()
