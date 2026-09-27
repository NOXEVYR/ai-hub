import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from aihub import api, capabilities, config


class CapabilitySourcesAPI(unittest.TestCase):
    def test_sources_save_uses_current_workspace_and_revision_preserves_other_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            skills=root/'skills';skills.mkdir()
            cfg={'ai_root':str(root),'custom':{'keep':True}}
            with mock.patch.object(capabilities,'execute',return_value={'suggestions':[]}):
                code,_,raw=api.capabilities_request(None,cfg,{'action':'discover'},None)
            self.assertEqual(code,200)
            revision=json.loads(raw)['sources_revision']
            body={'sources':[{'tool':'new-worker','kind':'skills_root','path':str(skills)}], 'revision':revision,'_workspace_root':str(root)}
            with mock.patch.object(config,'save_config') as save:
                code,_,raw=api.capabilities_request(None,cfg,{'action':'sources'},body)
                self.assertEqual(code,200,json.loads(raw))
                save.assert_called_once()
                self.assertEqual(cfg['custom'],{'keep':True})
                self.assertEqual(cfg['capability_sources'][0]['tool'],'new-worker')
                self.assertEqual(api.capabilities_request(None,cfg,{'action':'sources'},body)[0],409)
                save.reset_mock()
                body.update(revision=json.loads(raw)['sources_revision'],_workspace_root=str(root/'other'))
                self.assertEqual(api.capabilities_request(None,cfg,{'action':'sources'},body)[0],409)
                save.assert_not_called()

    def test_failed_save_does_not_change_live_configuration(self):
        cfg={'ai_root':'','capability_sources':[],'custom':1}
        with mock.patch.object(capabilities,'execute',return_value={'suggestions':[]}):
            _,_,raw=api.capabilities_request(None,cfg,{'action':'discover'},None)
        body={'sources':[],'revision':json.loads(raw)['sources_revision'],'_workspace_root':''}
        with mock.patch.object(config,'save_config',side_effect=OSError('read only')):
            self.assertEqual(api.capabilities_request(None,cfg,{'action':'sources'},body)[0],409)
        self.assertEqual(cfg,{'ai_root':'','capability_sources':[],'custom':1})
