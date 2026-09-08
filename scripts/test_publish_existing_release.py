import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

spec = importlib.util.spec_from_file_location('publisher', Path(__file__).with_name('publish-existing-release.py'))
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
TAG = 'v3.0.0'


def fixture_release(directory):
    directory.mkdir(parents=True, exist_ok=True)
    assets = []
    def add(name, content):
        (directory / name).write_bytes(content)
        asset = {'id': len(assets) + 1, 'name': name, 'size': len(content),
                 'digest': 'sha256:' + hashlib.sha256(content).hexdigest(), 'state': 'uploaded',
                 'browser_download_url': f'https://github.com/owner/repo/releases/download/{TAG}/{name}'}
        assets.append(asset)
        return asset
    full = add('pulsar-windows-x64-full-v3.0.0.zip', b'full immutable distro')
    add('pulsar-windows-x64-v3.0.0.zip', b'light immutable distro')
    manifest = {'schema_version': 'prism.component.release.v1', 'component': 'pulsar',
                'version': '3.0.0', 'release_tag': TAG, 'artifact_name': full['name'],
                'artifact_sha256': full['digest'], 'artifact_url': full['browser_download_url']}
    add('prism-pulsar-runtime-manifest.json', json.dumps(manifest).encode())
    return {'id': 99, 'tag_name': TAG, 'body': 'Complete existing release notes',
            'assets': assets, 'draft': True, 'html_url': 'https://github.com/owner/repo/releases/tag/v3.0.0'}


def test_validates_all_existing_assets_and_manifest(tmp_path):
    release = fixture_release(tmp_path)
    publisher.validate_assets(release, TAG, tmp_path)


def test_draft_temporary_urls_do_not_replace_the_public_manifest_url(tmp_path):
    release = fixture_release(tmp_path)
    for asset in release['assets']:
        asset['browser_download_url'] = asset['browser_download_url'].replace('/v3.0.0/', '/untagged-temporary/')
    publisher.validate_assets(release, TAG, tmp_path)


@pytest.mark.parametrize('mutation', ['missing', 'size', 'digest', 'state', 'tag', 'notes', 'path'])
def test_rejects_incomplete_or_changed_assets(tmp_path, mutation):
    release = fixture_release(tmp_path)
    if mutation == 'missing':
        release['assets'].pop(0)
    elif mutation == 'size':
        release['assets'][0]['size'] += 1
    elif mutation == 'digest':
        release['assets'][0]['digest'] = 'sha256:' + '0' * 64
    elif mutation == 'state':
        release['assets'][0]['state'] = 'new'
    elif mutation == 'tag':
        release['tag_name'] = 'v2.0.0'
    elif mutation == 'notes':
        release['body'] = ''
    elif mutation == 'path':
        release['assets'].append({'name': '../escape'})
    with pytest.raises(ValueError):
        publisher.validate_assets(release, TAG, tmp_path)


def test_rejects_manifest_for_another_tag_even_with_valid_server_hash(tmp_path):
    release = fixture_release(tmp_path)
    asset = release['assets'][-1]
    data = json.loads((tmp_path / asset['name']).read_text())
    data['release_tag'] = 'v2.0.0'
    content = json.dumps(data).encode()
    (tmp_path / asset['name']).write_bytes(content)
    asset.update(size=len(content), digest='sha256:' + hashlib.sha256(content).hexdigest())
    with pytest.raises(ValueError, match='manifest'):
        publisher.validate_assets(release, TAG, tmp_path)


@pytest.mark.parametrize('publish', [False, True])
def test_only_explicit_publish_changes_draft_and_never_uploads(tmp_path, publish):
    release = fixture_release(tmp_path / 'fixture')
    calls = []
    def fake_gh(*args):
        calls.append(args)
        if args[:3] == ('api', '--paginate', '--slurp'):
            return json.dumps([[release]])
        if args[0] == 'api':
            return json.dumps({'object': {'sha': 'immutable-tag', 'type': 'tag'}}
                              if '/git/ref/' in args[1] else release)
        if args[:2] == ('release', 'edit'):
            release['draft'] = False
        return ''
    original = copy.deepcopy(release)
    with patch.object(publisher, 'gh', side_effect=fake_gh), patch.object(publisher, 'validate_assets'):
        result = publisher.publish_existing('owner/repo', TAG, tmp_path / 'download', publish)
    assert result['draft'] is (not publish)
    assert publisher.release_identity(result) == publisher.release_identity(original)
    edits = [call for call in calls if call[:2] == ('release', 'edit')]
    assert len(edits) == int(publish)
    if edits:
        assert edits[0] == ('release', 'edit', TAG, '--repo', 'owner/repo', '--draft=false', '--latest')
    assert not any(call[:2] in [('release', 'upload'), ('release', 'create')] for call in calls)


def test_rejects_tag_drift_before_publication(tmp_path):
    release = fixture_release(tmp_path / 'fixture')
    calls = []
    refs = iter(['old', 'new'])
    def fake_gh(*args):
        calls.append(args)
        if args[:3] == ('api', '--paginate', '--slurp'):
            return json.dumps([[release]])
        if args[0] == 'api':
            return json.dumps({'object': {'sha': next(refs)}} if '/git/ref/' in args[1] else release)
        return ''
    with patch.object(publisher, 'gh', side_effect=fake_gh), patch.object(publisher, 'validate_assets'):
        with pytest.raises(ValueError, match='Tag changed'):
            publisher.publish_existing('owner/repo', TAG, tmp_path / 'download', True)
    assert not any(call[:2] == ('release', 'edit') for call in calls)


def test_rejects_implicit_tag_before_network(tmp_path):
    with patch.object(publisher, 'gh') as gh:
        with pytest.raises(ValueError, match='explicit existing'):
            publisher.publish_existing('owner/repo', 'main', tmp_path)
        gh.assert_not_called()
