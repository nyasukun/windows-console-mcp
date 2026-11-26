"""Check targeted VM management regressions by mutating a disposable source tree."""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

PROJECT = Path(__file__).resolve().parents[2]
TESTS = 'console/tests'


class Mutation(NamedTuple):
    name: str
    file: str
    original: str
    replacement: str
    test: str
    real_vm: bool = False


MUTATIONS = (
    Mutation('apply a stale inventory response', 'console/web/manager.js',
             'if (request !== latestRefresh) return;', 'if (false) return;',
             'ManagementE2E.test_old_inventory_response_cannot_overwrite_a_newer_save'),
    Mutation('surface a stale inventory error', 'console/web/manager.js',
             "if (request === latestRefresh) message($('message'), error.message);",
             "message($('message'), error.message);",
             'ManagementE2E.test_old_inventory_failure_cannot_replace_a_newer_success'),
    Mutation('delete VMs with their template', 'console/vm_manager.py',
             "            del inv['templates'][template_id]",
             "            del inv['templates'][template_id]\n"
             "            inv['vms'] = {ident: vm for ident, vm in inv['vms'].items()\n"
             "                          if vm['template_id'] != template_id}",
             'ManagementE2E.test_web_edits_inventory_created_by_mcp'),
    Mutation('accept overlapping IPv4 segments', 'console/vm_manager.py',
             'if any(network.overlaps(n) for n in networks):', 'if False:',
             'ManagedVMTest.test_web_rejects_cross_origin_mutations_and_invalid_topologies'),
    Mutation('reuse the old console session after restart', 'console/server.py',
             "shared = self.sessions.get(vm_id)\n        if shared is not None and shared['monitor'].identity != identity:",
             "shared = self.sessions.get(vm_id)\n        if False:",
             'ManagementE2E.test_real_vm_lifecycle', real_vm=True),
    Mutation('accept commands from an old QMP socket session', 'console/qmp.py',
             'if self.identity is not None and socket_identity(self.path) != self.identity:', 'if False:',
             'ManagementE2E.test_real_vm_lifecycle', real_vm=True),
)


def run_test(root, env, test):
    if test.startswith('ManagementE2E.'):
        args = [f'{TESTS}/vm_management_e2e.py', test]
    else:
        args = ['-m', 'unittest', 'discover', '-s', TESTS, '-p', 'test_vm_manager.py', '-k', test]
    return subprocess.run([sys.executable, *args], cwd=root, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=90)


def main():
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    iso = env.get('WINDOWS_E2E_ISO')
    if iso:
        env['WINDOWS_E2E_ISO'] = str(Path(iso).resolve(strict=True))
    active = [mutation for mutation in MUTATIONS if iso or not mutation.real_vm]

    with tempfile.TemporaryDirectory(prefix='windows-mutations-') as temp:
        root = Path(temp)
        shutil.copytree(PROJECT / 'console', root / 'console',
                        ignore=shutil.ignore_patterns('.venv', '__pycache__'))
        shutil.copy2(PROJECT / 'run-vm-clean.sh', root)
        for test in dict.fromkeys(mutation.test for mutation in active):
            baseline = run_test(root, env, test)
            if baseline.returncode or 'Ran 1 test in' not in baseline.stdout or 'skipped=' in baseline.stdout:
                sys.exit(f'Baseline failed or was skipped for {test}\n{baseline.stdout}')

        killed = 0
        for mutation in active:
            path = root / mutation.file
            source = path.read_text()
            if source.count(mutation.original) != 1:
                sys.exit(f'Mutation target must occur exactly once: {mutation.name}')
            path.write_text(source.replace(mutation.original, mutation.replacement))
            try:
                result = run_test(root, env, mutation.test)
            finally:
                path.write_text(source)
            method = mutation.test.split('.')[-1]
            if result.returncode == 0:
                print(f'SURVIVED {mutation.name}', flush=True)
            elif result.returncode == 1 and f'FAIL: {method} (' in result.stdout and 'FAILED (failures=1)' in result.stdout:
                print(f'KILLED   {mutation.name}', flush=True)
                killed += 1
            else:
                sys.exit(f'Unexpected test error for {mutation.name}\n{result.stdout}')

    print(f'{killed}/{len(active)} mutations killed')
    if not iso:
        print('Set WINDOWS_E2E_ISO to include the two Docker/QEMU console mutations.')
    return 0 if killed == len(active) else 1


if __name__ == '__main__':
    sys.exit(main())
