"""Check that deployment cannot silently use a different account or billing link."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
args = sys.argv[2:]
if args[:3] == ['billing', 'projects', 'describe']:
    if os.environ.get('API_ERROR'):
        sys.exit(1)
    print(os.environ.get('BILLING', 'billingAccounts/01D42C-5BB8A5-0CD6DA') + '\tTrue')
elif args[:2] == ['projects', 'describe']:
    print(os.environ.get('ORGANIZATION', '580417351522'))
elif args[:3] == ['compute', 'instances', 'describe']:
    print(os.environ.get('VM_STATE', 'RUNNING'))
elif args[:2] == ['compute', 'ssh']:
    print('ok')
else:
    sys.exit('Unexpected mutation or command: ' + repr(args))
'''


class DeploymentAccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        folder = Path(self.tmp.name)
        self.gcloud = folder / 'gcloud'
        self.gcloud.write_text(FAKE_GCLOUD)
        self.gcloud.chmod(0o755)
        self.log = folder / 'calls.jsonl'
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('GCP_') and k not in
                    {'BILLING', 'ORGANIZATION', 'API_ERROR', 'VM_STATE'}}
        self.env.update(GCLOUD_BIN=str(self.gcloud), CALL_LOG=str(self.log))

    def run_check(self, script='deploy/deploy.sh', **overrides):
        self.env.update(overrides)
        args = ['bash', script]
        if script.endswith('/deploy.sh'):
            args.append('--check')
        return subprocess.run(args, cwd=ROOT, env=self.env, text=True,
                              capture_output=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_all_commands_use_new_account(self):
        result = self.run_check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls()), 3)
        self.assertTrue(all(c[0] == '--account=jk5391775@gmail.com'
                            for c in self.calls()))

    def test_explicit_account_override(self):
        result = self.run_check(GCP_ACCOUNT='operator@example.com')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(all(c[0] == '--account=operator@example.com'
                            for c in self.calls()))

    def test_wrong_billing_blocks_vm_access(self):
        result = self.run_check(BILLING='billingAccounts/OLD')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_wrong_organization_blocks_vm_access(self):
        result = self.run_check(ORGANIZATION='237163885821')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 2)

    def test_billing_api_failure_stops_deployment(self):
        result = self.run_check(API_ERROR='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_check_never_starts_stopped_vm(self):
        result = self.run_check(VM_STATE='TERMINATED')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('TERMINATED', result.stdout)
        self.assertEqual(len(self.calls()), 3)

    def test_ship_checks_account_before_git_changes(self):
        result = self.run_check(script='deploy/ship.sh', BILLING='billingAccounts/OLD')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertNotIn('[1/3] commit', result.stdout)

    def test_ship_selects_repository_login_for_push_only(self):
        folder = Path(self.tmp.name)
        gh = folder / 'gh'
        gh.write_text('#!/usr/bin/env python3\nimport sys\n'
                      'assert sys.argv[1:] == ["auth", "token", "--hostname", '
                      '"github.com", "--user", "vijaykavuri43-sudo"]\n'
                      'print("test-token")\n')
        gh.chmod(0o755)
        git = folder / 'git'
        git.write_text('#!/usr/bin/env python3\nimport os, sys\n'
                       'if sys.argv[1:2] == ["rev-list"]: print("1")\n'
                       'if "push" in sys.argv:\n'
                       ' assert os.environ.get("GH_TOKEN") == "test-token"\n'
                       ' open(os.environ["PUSH_MARKER"], "w").write("passed")\n')
        git.chmod(0o755)
        marker = folder / 'push'
        result = self.run_check(script='deploy/ship.sh',
                                PATH=str(folder) + os.pathsep + self.env['PATH'],
                                PUSH_MARKER=str(marker), GH_TOKEN='other-login')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(), 'passed')
        self.assertNotIn('test-token', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
