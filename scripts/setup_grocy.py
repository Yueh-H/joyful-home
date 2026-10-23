#!/usr/bin/env python3
"""Create only this compose project's Grocy; secrets never enter command arguments/logs."""
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / '.env'
ACCESS = ROOT / '.local-access.txt'


def read_env():
    if not ENV.exists():
        return {}
    return dict(line.split('=', 1) for line in ENV.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)


def private_write(path, content):
    # No shell interpolation, and no temporary file with permissive mode.
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)


def api(path, env, method='GET', data=None):
    request = urllib.request.Request(env['GROCY_URL'].rstrip('/') + '/api/' + path,
        data=None if data is None else json.dumps(data).encode(), method=method,
        headers={'GROCY-API-KEY': env['GROCY_API_KEY'], 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        content = response.read()
        return json.loads(content) if content else None


def main():
    os.chdir(ROOT)
    for path in ('.env', '.local-access.txt', 'data/grocy'):
        if subprocess.run(['git', 'check-ignore', '-q', path]).returncode:
            raise RuntimeError(f'{path} must be gitignored before setup')
    env = read_env()
    for key in ('GROCY_API_KEY', 'HOME_PASSWORD', 'SESSION_SECRET'):
        env.setdefault(key, '')
        if not env[key]:
            env[key] = secrets.token_urlsafe(32)
    env.setdefault('GROCY_URL', 'http://127.0.0.1:9283')
    env.setdefault('GEMINI_MODEL', 'gemini-3-flash-preview')
    env.setdefault('HOME_BIND', '0.0.0.0')
    # Preserve explicit existing .env configuration; never copy a key from the shell.
    if env['GROCY_URL'] != 'http://127.0.0.1:9283':
        raise RuntimeError('Bootstrap only supports the isolated loopback Grocy in this project')
    private_write(ENV, ''.join(f'{key}={value}\n' for key, value in env.items()))
    subprocess.run(['docker', 'compose', 'up', '-d', 'grocy'], check=True)
    for _ in range(90):
        try:
            with urllib.request.urlopen(env['GROCY_URL'], timeout=2) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError('Grocy did not become ready')
    # Database may only be touched by PHP inside this explicitly named compose service.
    # A small bootstrap bypasses interactive default admin login; auth stays enabled.
    password = env.get('GROCY_ADMIN_PASSWORD') or secrets.token_urlsafe(24)
    payload = json.dumps({'key': env['GROCY_API_KEY'], 'password': password})
    php = r'''
$p = json_decode(stream_get_contents(STDIN), true);
$db = new PDO('sqlite:/config/data/grocy.db');
$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
$u = $db->query("SELECT id FROM users WHERE username='admin'")->fetchColumn();
if (!$u) { throw new Exception('Fresh Grocy admin not found'); }
$db->beginTransaction();
$q = $db->prepare('UPDATE users SET password=? WHERE id=?');
$q->execute([password_hash($p['password'], PASSWORD_ARGON2ID), $u]);
$q = $db->prepare("SELECT id FROM api_keys WHERE api_key=?");
$q->execute([$p['key']]);
if (!$q->fetchColumn()) {
 $q = $db->prepare("INSERT INTO api_keys(api_key,user_id,expires,key_type,description) VALUES(?,?,?,'default','Joyful Home local backend')");
 $q->execute([$p['key'], $u, '2999-12-31 23:59:59']);
}
$db->commit();
'''
    subprocess.run(['docker', 'compose', 'exec', '-T', 'grocy', 'php', '-r', php],
                   input=payload, text=True, capture_output=True, check=True)
    env['GROCY_ADMIN_PASSWORD'] = password
    private_write(ENV, ''.join(f'{key}={value}\n' for key, value in env.items()))
    private_write(ACCESS, 'Joyful Home local access (private; do not share or commit)\n'
        f'Household UI: http://127.0.0.1:8765\nHousehold password: {env["HOME_PASSWORD"]}\n'
        f'Grocy: {env["GROCY_URL"]}\nGrocy username: admin\nGrocy password: {password}\n')
    for endpoint in ('system/info', 'stock', 'objects/products'):
        result = api(endpoint, env)
        print(f'GET /api/{endpoint}: OK' + (f' ({len(result)} entries)' if isinstance(result, list) else ''))
    print('Setup ready. Credentials are only in ignored .env and .local-access.txt (0600).')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # CalledProcessError can include stdin-derived data if blindly formatted.
        print(f'Setup failed ({type(error).__name__}); inspect local service readiness without printing secrets.', file=sys.stderr)
        sys.exit(1)
