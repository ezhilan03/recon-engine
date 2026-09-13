#!/usr/bin/env python3
"""Bounded, manually invoked EC2 demo. Uses the instance role through AWS CLI.

Only synthetic versioned input manifests and digest-pinned app images are accepted.
The one-hour systemd stop timer remains independent of this job's success.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

ROOT = Path('/var/lib/recon')


def execute(args, **kwargs):
    return subprocess.run(args, check=True, timeout=kwargs.pop('timeout', 120), **kwargs)


def aws(config, *args):
    return execute(['aws', '--region', config['region'], *args, '--output', 'json'], capture_output=True, text=True).stdout


def validate_manifest(manifest, revision, config):
    if manifest.get('revision') != revision:
        raise ValueError('Release revision mismatch')
    if not re.fullmatch(re.escape(config['repository']) + r'@sha256:[0-9a-f]{64}', manifest.get('image', '')):
        raise ValueError('Image must be a digest in the approved repository')
    names = {'internal_ledger.csv', 'network_settlement.csv'}
    if set(manifest.get('inputs', {})) != names:
        raise ValueError('Expected exactly two synthetic source files')
    for name, item in manifest['inputs'].items():
        if (item.get('bucket') != config['bucket'] or item.get('key') != f'inputs/{revision}/{name}'
                or not item.get('version_id') or item['version_id'] == 'null'
                or not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', ''))):
            raise ValueError('Invalid versioned source contract')


def database():
    password_path = ROOT / 'passwords.json'
    if not password_path.exists():
        fd = os.open(password_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump({'admin': secrets.token_hex(32), 'app': secrets.token_hex(32)}, f)
    passwords = json.loads(password_path.read_text())
    # Locally generated hexadecimal values only; never print credentials.
    if any(not re.fullmatch(r'[0-9a-f]{64}', p) for p in passwords.values()):
        raise ValueError('Invalid local credential file')
    admin_env = ROOT / 'postgres.env'
    admin_env.write_text('POSTGRES_PASSWORD=' + passwords['admin'] + '\n')
    admin_env.chmod(0o600)
    if subprocess.run(['docker', 'network', 'inspect', 'recon'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        execute(['docker', 'network', 'create', 'recon'], capture_output=True)
    exists = subprocess.run(['docker', 'inspect', 'recon-db'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if exists:
        execute(['docker', 'start', 'recon-db'], capture_output=True)
    else:
        execute(['docker', 'run', '-d', '--name', 'recon-db', '--network', 'recon', '--restart', 'unless-stopped',
                 '--env-file', str(admin_env), '-v', 'recon-postgres:/var/lib/postgresql/data', 'postgres:16'], capture_output=True)
    for _ in range(60):
        if subprocess.run(['docker', 'exec', 'recon-db', 'pg_isready', '-U', 'postgres'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('PostgreSQL did not become ready')
    base = ['docker', 'exec', '-i', 'recon-db', 'psql', '-U', 'postgres', '-v', 'ON_ERROR_STOP=1', '-At']
    found = execute(base + ['-c', "SELECT 1 FROM pg_roles WHERE rolname='recon_app'"], capture_output=True, text=True).stdout.strip()
    if not found:
        execute(base, input="CREATE ROLE recon_app LOGIN PASSWORD '" + passwords['app'] + "';\n", capture_output=True, text=True)
    db_exists = execute(base + ['-c', "SELECT 1 FROM pg_database WHERE datname='recon'"], capture_output=True, text=True).stdout.strip()
    if not db_exists:
        execute(base + ['-c', 'CREATE DATABASE recon OWNER recon_app'], capture_output=True)
    app_env = ROOT / 'app.env'
    app_env.write_text('DATABASE_URL=postgresql://recon_app:' + passwords['app'] + '@recon-db:5432/recon\n')
    app_env.chmod(0o600)
    return app_env


def run(revision):
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('Expected the full Git commit SHA')
    config = json.loads((ROOT / 'config.json').read_text())
    with (ROOT / 'job.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            work = Path(directory)
            manifest_path = work / 'candidate.json'
            aws(config, 's3api', 'get-object', '--bucket', config['bucket'], '--key', f'releases/{revision}/candidate.json', str(manifest_path))
            manifest = json.loads(manifest_path.read_text())
            validate_manifest(manifest, revision, config)
            inputs, reports = work / 'input', work / 'reports'
            inputs.mkdir(mode=0o755)
            reports.mkdir(mode=0o755)
            os.chown(reports, 1000, 1000)
            for name, item in manifest['inputs'].items():
                target = inputs / name
                aws(config, 's3api', 'get-object', '--bucket', config['bucket'], '--key', item['key'], '--version-id', item['version_id'], str(target))
                if hashlib.sha256(target.read_bytes()).hexdigest() != item['sha256']:
                    raise ValueError('Input hash mismatch')
                target.chmod(0o644)
            token = execute(['aws', '--region', config['region'], 'ecr', 'get-login-password'], capture_output=True, text=True).stdout
            execute(['docker', 'login', '--username', 'AWS', '--password-stdin', config['repository'].split('/')[0]], input=token, capture_output=True, text=True)
            execute(['docker', 'pull', manifest['image']], timeout=300, capture_output=True)
            app_env = database()
            try:
                execute(['docker', 'run', '--rm', '--name', 'recon-batch', '--network', 'recon', '--env-file', str(app_env),
                         '-v', f'{inputs}:/input:ro', '-v', f'{reports}:/app/reports', manifest['image'],
                         'python', '-m', 'recon_engine.batch', '--data-dir', '/input', '--output-dir', '/app/reports',
                         '--run-id', 'demo-' + revision], timeout=600)
            finally:
                subprocess.run(['docker', 'rm', '-f', 'recon-batch'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                for report in reports.iterdir():
                    aws(config, 's3api', 'put-object', '--bucket', config['bucket'], '--key', f'reports/{revision}/{report.name}', '--body', str(report))
                dump = work / 'recon.dump'
                with dump.open('wb') as stream:
                    execute(['docker', 'exec', 'recon-db', 'pg_dump', '-U', 'postgres', '-d', 'recon', '-Fc'], stdout=stream, stderr=subprocess.PIPE)
                aws(config, 's3api', 'put-object', '--bucket', config['bucket'], '--key', f'backups/{revision}/recon.dump', '--body', str(dump))
                # Completed sessions stop early; the boot timer bounds failed setup too.
                execute(['shutdown', '-h', '+1'], capture_output=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('revision')
    args = parser.parse_args()
    try:
        run(args.revision)
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1)
