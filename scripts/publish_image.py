"""Publish once per immutable commit tag; retry safely after later-stage failure."""
import json
import os
from pathlib import Path
import re
import subprocess


def main():
    revision = os.environ['GITHUB_SHA']
    repository = os.environ['ECR_REPOSITORY']
    registry = os.environ['REGISTRY']
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Invalid commit identity')
    describe = ['aws', 'ecr', 'describe-images', '--repository-name', repository,
                '--image-ids', 'imageTag=' + revision, '--output', 'json']
    result = subprocess.run(describe, capture_output=True, text=True)
    if result.returncode:
        if 'ImageNotFoundException' not in result.stderr:
            raise RuntimeError('Cannot inspect release repository')
        image = f'{registry}/{repository}:{revision}'
        subprocess.run(['docker', 'buildx', 'build', '--platform', 'linux/arm64', '--load', '-t', image, '.'], check=True)
        subprocess.run(['docker', 'run', '--rm', '-v', str(Path('tests').resolve()) + ':/app/tests:ro', image,
                        'python', '-m', 'unittest', 'tests.test_deterministic_matcher', 'tests.test_approval_rules'], check=True)
        subprocess.run(['docker', 'push', image], check=True)
        result = subprocess.run(describe, capture_output=True, text=True, check=True)
    digest = json.loads(result.stdout)['imageDetails'][0]['imageDigest']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Invalid image digest')
    Path('image-digest.txt').write_text(digest + '\n')


if __name__ == '__main__':
    main()
