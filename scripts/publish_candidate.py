"""Publish only the two synthetic input files with exact S3 version IDs and hashes.

The workflow authenticates through OIDC. This script never launches compute.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def aws(*args):
    return json.loads(subprocess.check_output(['aws', *args, '--output', 'json'], text=True))


def main():
    revision = os.environ['GITHUB_SHA']
    bucket = os.environ['ARTIFACT_BUCKET']
    registry = os.environ['REGISTRY']
    repository = os.environ['ECR_REPOSITORY']
    digest = Path('image-digest.txt').read_text().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Invalid immutable release identity')
    manifest = {'revision': revision, 'image': f'{registry}/{repository}@{digest}', 'inputs': {}}
    for name in ('internal_ledger.csv', 'network_settlement.csv'):
        path = Path('data/output') / name
        content = path.read_bytes()
        key = f'inputs/{revision}/{name}'
        response = aws('s3api', 'put-object', '--bucket', bucket, '--key', key, '--body', str(path))
        version = response.get('VersionId')
        if not version or version == 'null':
            raise ValueError('Versioned input storage is required')
        manifest['inputs'][name] = {'bucket': bucket, 'key': key, 'version_id': version,
                                   'sha256': hashlib.sha256(content).hexdigest()}
    Path('candidate.json').write_text(json.dumps(manifest, indent=2) + '\n')
    aws('s3api', 'put-object', '--bucket', bucket, '--key', f'releases/{revision}/candidate.json',
        '--body', 'candidate.json')
    print(f'Published candidate manifest for {revision}; no runtime was deployed')


if __name__ == '__main__':
    main()
