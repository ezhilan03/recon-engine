"""Start one approved demo, invoke its fixed SSM document, stop in every exit path."""
import json
import os
import re
import subprocess
import time


def aws(*args):
    return json.loads(subprocess.check_output(['aws', *args, '--output', 'json'], text=True))


def main():
    revision = os.environ['REVISION']
    instance = os.environ['INSTANCE_ID']
    if not re.fullmatch(r'[0-9a-f]{40}', revision) or not re.fullmatch(r'i-[0-9a-f]+', instance):
        raise ValueError('Invalid demo revision or instance')
    try:
        aws('ec2', 'start-instances', '--instance-ids', instance)
        subprocess.run(['aws', 'ec2', 'wait', 'instance-running', '--instance-ids', instance], check=True, timeout=300)
        for _ in range(60):
            items = aws('ssm', 'describe-instance-information', '--filters', f'Key=InstanceIds,Values={instance}')['InstanceInformationList']
            if items and items[0]['PingStatus'] == 'Online':
                break
            time.sleep(5)
        else:
            raise RuntimeError('Demo did not connect to SSM')
        command = aws('ssm', 'send-command', '--document-name', os.environ['DEMO_DOCUMENT'],
                      '--instance-ids', instance, '--parameters', json.dumps({'Revision': [revision]}),
                      '--timeout-seconds', '60', '--cloud-watch-output-config',
                      json.dumps({'CloudWatchOutputEnabled': True, 'CloudWatchLogGroupName': os.environ['LOG_GROUP']}))['Command']['CommandId']
        # Invocation may not be visible immediately after send-command.
        for _ in range(100):
            result = subprocess.run(['aws', 'ssm', 'get-command-invocation', '--command-id', command,
                                     '--instance-id', instance, '--output', 'json'], capture_output=True, text=True)
            if result.returncode == 0:
                status = json.loads(result.stdout)['Status']
                if status == 'Success':
                    print(json.dumps({'revision': revision, 'status': 'succeeded', 'command_id': command}))
                    return
                if status not in ('Pending', 'InProgress', 'Delayed'):
                    raise RuntimeError('Remote demo failed: ' + status)
            elif 'InvocationDoesNotExist' not in result.stderr:
                raise RuntimeError('Cannot inspect remote command')
            time.sleep(10)
        raise TimeoutError('Remote demo exceeded the session deadline')
    finally:
        aws('ec2', 'stop-instances', '--instance-ids', instance)


if __name__ == '__main__':
    main()
