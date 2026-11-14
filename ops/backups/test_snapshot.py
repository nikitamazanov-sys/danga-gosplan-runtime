import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).with_name('snapshot.py').resolve()


class SnapshotTest(unittest.TestCase):
    def test_restore_deleted_history_and_collision(self):
        with tempfile.TemporaryDirectory() as folder:
            base = pathlib.Path(folder)

            def git(*args, cwd=base):
                return subprocess.check_output(['git', *args], cwd=cwd,
                                               stderr=subprocess.DEVNULL, text=True).strip()

            source, target, work = (base / x for x in ['github.git', 'gitlab.git', 'work'])
            git('init', '--bare', str(source))
            git('init', '--bare', str(target))
            git('init', '-b', 'main', str(work))
            git('config', 'user.name', 'Backup test', cwd=work)
            git('config', 'user.email', 'backup@example.invalid', cwd=work)
            git('config', 'commit.gpgsign', 'false', cwd=work)
            (work / 'source.txt').write_text('first source\n')
            git('add', '.', cwd=work)
            git('commit', '-m', 'first', cwd=work)
            first = git('rev-parse', 'HEAD', cwd=work)
            git('branch', 'feature', cwd=work)
            git('tag', '-a', 'v1', '-m', 'annotated release', cwd=work)
            tag = git('rev-parse', 'v1', cwd=work)
            git('remote', 'add', 'origin', str(source), cwd=work)
            git('push', '--all', 'origin', cwd=work)
            git('push', '--tags', 'origin', cwd=work)

            def run(snapshot):
                return subprocess.run(['python3', str(SCRIPT), '--slug', 'site',
                                       '--snapshot', snapshot, '--gitlab', str(target),
                                       '--receipt', str(base / 'receipt.json')],
                                      cwd=work, capture_output=True, text=True)

            result = run('v001')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = json.loads((base / 'receipt.json').read_text())
            self.assertTrue(receipt['all_configured_providers_verified'])
            self.assertEqual(receipt['source_refs']['tags/v1'], tag)
            self.assertEqual(len(receipt['source_refs']), 3)
            (work / 'source.txt').write_text('new source\n')
            git('commit', '-am', 'second', cwd=work)
            git('push', 'origin', 'main', cwd=work)
            collision = run('v001')
            self.assertNotEqual(collision.returncode, 0)
            result = run('v002')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            second = json.loads((base / 'receipt.json').read_text())
            self.assertEqual(len(second['source_refs']), 3)  # no nested backups
            git('push', 'origin', '--delete', 'feature', cwd=work)
            result = run('v003')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            third = json.loads((base / 'receipt.json').read_text())
            self.assertNotIn('heads/feature', third['source_refs'])
            # Restore old deleted history with only the target repository available.
            source.rename(base / 'github-no-longer-available.git')
            restore = base / 'offline-restore'
            git('init', str(restore))
            git('fetch', str(target), 'refs/heads/backups/site/v001/feature', cwd=restore)
            self.assertEqual(git('rev-parse', 'FETCH_HEAD', cwd=restore), first)
            git('fsck', '--full', cwd=restore)
            old = git('--git-dir=' + str(target), 'rev-parse',
                      'refs/heads/backups/site/v001/main')
            self.assertEqual(old, first)


if __name__ == '__main__':
    unittest.main()
