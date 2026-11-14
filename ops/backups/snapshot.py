#!/usr/bin/env python3
"""Append a repository's Git history to independent snapshot refs.

This covers Git objects only. LFS payloads, submodule repositories, live site
uploads, configuration, databases and hosting-only changes are not covered.
"""
import argparse
import json
import pathlib
import re
import subprocess
import tempfile
from datetime import datetime, timezone


def git(*args, cwd=None):
    try:
        result = subprocess.run(['git', *args], cwd=cwd, capture_output=True,
                                text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise RuntimeError('git operation timed out: ' + args[0]) from None
    if result.returncode:
        # Never expose credential-bearing remote errors or configuration.
        raise RuntimeError('git operation failed: ' + args[0])
    return result.stdout


def refs(remote):
    return {line.split('\t')[1]: line.split('\t')[0]
            for line in git('ls-remote', '--refs', remote).splitlines()}


def source_refs():
    result = {}
    for line in git('for-each-ref', '--format=%(refname) %(objectname)',
                    'refs/remotes/origin/', 'refs/tags/').splitlines():
        ref, oid = line.split()
        if ref.startswith('refs/remotes/origin/'):
            tail = ref.removeprefix('refs/remotes/origin/')
            if tail == 'HEAD' or tail.startswith('backups/'):
                continue
            result['heads/' + tail] = oid
        elif not ref.startswith('refs/tags/backups/'):
            result['tags/' + ref.removeprefix('refs/tags/')] = oid
    if not result:
        raise RuntimeError('No source refs found')
    return result


def destination_refs(source, slug, snapshot):
    result = {}
    for name, oid in source.items():
        kind, tail = name.split('/', 1)
        prefix = 'refs/heads' if kind == 'heads' else 'refs/tags'
        result[f'{prefix}/backups/{slug}/{snapshot}/{tail}'] = oid
    return result


def copy_and_restore(remote, wanted):
    before = refs(remote)
    for ref, oid in wanted.items():
        if ref in before and before[ref] != oid:
            raise RuntimeError('Snapshot ID already exists with different bytes')
    missing = [f'{oid}:{ref}' for ref, oid in wanted.items() if ref not in before]
    if missing:
        # No --force, --mirror, deletions, or rewrites of old snapshots.
        git('push', '--atomic', remote, *missing)
    after = refs(remote)
    if any(after.get(ref) != oid for ref, oid in wanted.items()):
        raise RuntimeError('Remote snapshot refs do not match')
    with tempfile.TemporaryDirectory(prefix='restore-check-') as folder:
        git('init', '--bare', folder)
        fetchspec = [f'{ref}:refs/restore/{i}' for i, ref in enumerate(wanted)]
        # An empty repository fetches only from this backup provider. It cannot
        # silently borrow objects from the working checkout or GitHub.
        git('fetch', '--no-tags', remote, *fetchspec, cwd=folder)
        git('fsck', '--full', '--no-reflogs', cwd=folder)
        for i, expected in enumerate(wanted.values()):
            if git('rev-parse', f'refs/restore/{i}', cwd=folder).strip() != expected:
                raise RuntimeError('Independent restore does not match')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--slug', required=True)
    p.add_argument('--snapshot', required=True)
    p.add_argument('--github', default='origin')
    p.add_argument('--gitlab')
    p.add_argument('--receipt', type=pathlib.Path, required=True)
    a = p.parse_args()
    if not all(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', v)
               for v in (a.slug, a.snapshot)):
        p.error('Unsafe slug or snapshot ID')
    if any(v.startswith('-') for v in [a.github, a.gitlab or '']):
        p.error('Unsafe remote')
    # A fresh Actions checkout has only origin/main; fetch all live source refs.
    git('fetch', '--prune', '--tags', 'origin',
        '+refs/heads/*:refs/remotes/origin/*')
    source = source_refs()
    wanted = destination_refs(source, a.slug, a.snapshot)
    record = {
        'schema': 1, 'slug': a.slug, 'snapshot': a.snapshot,
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'git_history_only', 'full_live_site_backup': False,
        'source_refs': source, 'snapshot_refs': wanted, 'copies': {},
        'all_configured_providers_verified': False,
        'exclusions': ['LFS payloads', 'submodule repositories', 'working databases',
                       'uploads', 'hosting configuration', 'uncommitted hosting files'],
    }
    # Detect pointers; a successful Git backup must still say these are excluded.
    record['lfs_or_submodules_possible'] = True
    github_url = git('remote', 'get-url', a.github).strip()
    for name, remote in [('github', github_url), ('gitlab', a.gitlab)]:
        if not remote:
            record['copies'][name] = {'verified': False, 'reason': 'not_configured'}
            continue
        try:
            copy_and_restore(remote, wanted)
            record['copies'][name] = {'verified': True, 'independent_restore': True}
        except RuntimeError as error:
            record['copies'][name] = {'verified': False, 'reason': str(error)}
    record['all_configured_providers_verified'] = all(
        entry['verified'] for entry in record['copies'].values())
    a.receipt.parent.mkdir(parents=True, exist_ok=True)
    a.receipt.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'scope': record['scope'], 'copies': record['copies']}))
    return 0 if record['all_configured_providers_verified'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
