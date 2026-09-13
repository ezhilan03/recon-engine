import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import os


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / (name + '.py'))
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class ReleaseContractTests(unittest.TestCase):
    def test_reject_foreign_bucket_path_unpinned_image_and_unversioned_input(self):
        demo = module('demo_run')
        config = {'bucket': 'test', 'repository': 'test.ecr/recon'}
        revision = 'a' * 40
        manifest = {'revision': revision, 'image': 'test.ecr/recon@sha256:' + 'b'*64,
                    'inputs': {name: {'bucket': 'test', 'key': f'inputs/{revision}/{name}',
                                     'version_id': 'v1', 'sha256': 'c'*64}
                               for name in ['internal_ledger.csv', 'network_settlement.csv']}}
        demo.validate_manifest(manifest, revision, config)
        for key, value in [('bucket', 'foreign'), ('key', '../../secret'), ('version_id', 'null')]:
            changed = copy.deepcopy(manifest)
            changed['inputs']['internal_ledger.csv'][key] = value
            with self.assertRaises(ValueError):
                demo.validate_manifest(changed, revision, config)
        manifest['image'] = 'test.ecr/recon:latest'
        with self.assertRaises(ValueError):
            demo.validate_manifest(manifest, revision, config)

    def test_remote_start_failure_still_attempts_stop(self):
        remote = module('run_remote_demo')
        calls = []
        def aws(*args):
            calls.append(args)
            if args[1] == 'start-instances':
                raise RuntimeError('simulated start failure')
            return {}
        with patch.dict(os.environ, {'REVISION': 'a'*40, 'INSTANCE_ID': 'i-0123456789abcdef0'}), patch.object(remote, 'aws', aws):
            with self.assertRaises(RuntimeError):
                remote.main()
        self.assertEqual([c[1] for c in calls], ['start-instances', 'stop-instances'])

    def test_invalid_revision_never_starts_compute(self):
        remote = module('run_remote_demo')
        with patch.dict(os.environ, {'REVISION': 'x; command', 'INSTANCE_ID': 'i-0123'}), patch.object(remote, 'aws') as aws:
            with self.assertRaises(ValueError):
                remote.main()
            aws.assert_not_called()
