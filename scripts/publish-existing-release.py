#!/usr/bin/env python3
"""Publish an existing draft without retagging, rebuilding or replacing assets."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import quote


def gh(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True, encoding='utf-8', check=True)
    return result.stdout


def gh_json(*args):
    return json.loads(gh(*args))


def validate_assets(release, tag, directory):
    if release['tag_name'] != tag or not release.get('body', '').strip():
        raise ValueError('Release tag or existing release notes are missing/mismatched')
    version = tag.removeprefix('v')
    names = {asset['name'] for asset in release['assets']}
    required = {f'pulsar-windows-x64-full-v{version}.zip',
                f'pulsar-windows-x64-v{version}.zip', 'prism-pulsar-runtime-manifest.json'}
    if not required <= names:
        raise ValueError('Existing immutable distro assets or runtime manifest are missing')
    for asset in release['assets']:
        name = asset['name']
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('Unsafe asset name')
        path = directory / name
        digest = asset.get('digest', '')
        if asset.get('state') != 'uploaded' or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError(f'Unverified server asset: {name}')
        if not path.is_file() or path.stat().st_size != asset['size']:
            raise ValueError(f'Asset size mismatch: {name}')
        with path.open('rb') as stream:
            actual = 'sha256:' + hashlib.file_digest(stream, 'sha256').hexdigest()
        if actual != digest:
            raise ValueError(f'Asset hash mismatch: {name}')
    manifest = json.loads((directory / 'prism-pulsar-runtime-manifest.json').read_text())
    full = next(asset for asset in release['assets']
                if asset['name'] == f'pulsar-windows-x64-full-v{version}.zip')
    # Draft asset URLs contain GitHub's temporary untagged-* slug. The manifest
    # intentionally names the eventual immutable public tag URL instead.
    download_root = full['browser_download_url'].split('/releases/download/', 1)[0]
    public_url = f'{download_root}/releases/download/{quote(tag, safe="")}/{quote(full["name"], safe="")}'
    expected = {'schema_version': 'prism.component.release.v1', 'component': 'pulsar',
                'version': version, 'release_tag': tag, 'artifact_name': full['name'],
                'artifact_sha256': full['digest'], 'artifact_url': public_url}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError('Runtime manifest does not match the selected immutable full asset')


def release_identity(release):
    return (release['id'], release['tag_name'], release['body'],
            sorted((asset['id'], asset['name'], asset['size'], asset.get('digest'))
                   for asset in release['assets']))


def publish_existing(repo, tag, directory, publish=False):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?', tag):
        raise ValueError('An explicit existing semantic release tag is required')
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Asset verification requires an empty destination')
    ref_endpoint = f'repos/{repo}/git/ref/tags/{tag}'
    tag_object = gh_json('api', ref_endpoint)['object']
    # The /releases/tags endpoint omits drafts. Resolve through the authenticated
    # release list, then use the immutable release id for every subsequent read.
    pages = gh_json('api', '--paginate', '--slurp', f'repos/{repo}/releases?per_page=100')
    matches = [release for page in pages for release in page if release['tag_name'] == tag]
    if len(matches) != 1:
        raise ValueError('Expected exactly one existing release for the selected tag')
    release = matches[0]
    endpoint = f'repos/{repo}/releases/{release["id"]}'
    gh('release', 'download', tag, '--repo', repo, '--dir', str(directory))
    validate_assets(release, tag, directory)
    latest = gh_json('api', endpoint)
    if release_identity(latest) != release_identity(release):
        raise ValueError('Release changed during asset verification')
    if gh_json('api', ref_endpoint)['object'] != tag_object:
        raise ValueError('Tag changed during asset verification')
    print(f'Verified {tag}: {len(release["assets"])} existing assets; notes and tag preserved.')
    if publish and latest['draft']:
        gh('release', 'edit', tag, '--repo', repo, '--draft=false', '--latest')
    result = gh_json('api', endpoint)
    if release_identity(result) != release_identity(release):
        raise ValueError('Release content changed during publication')
    if gh_json('api', ref_endpoint)['object'] != tag_object:
        raise ValueError('Tag changed during publication')
    if publish and result['draft']:
        raise ValueError('Release remains a draft after publication')
    print(f'Release draft={result["draft"]}: {result["html_url"]}')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--assets-dir', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    publish_existing(args.repo, args.tag, args.assets_dir, args.publish)
