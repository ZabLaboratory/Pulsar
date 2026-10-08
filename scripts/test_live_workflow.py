"""Owner-disabled jobs must not block or fabricate release proof."""
from pathlib import Path
import yaml

WORKFLOW = yaml.safe_load((Path(__file__).resolve().parents[1] /
                           '.github/workflows/pipeline.yml').read_text(encoding='utf-8'))
JOBS = WORKFLOW['jobs']

def test_live_and_pages_are_unconditionally_disabled():
    for name in ('live-broadcast', 'publish-gh-pages'):
        assert JOBS[name]['if'] is False

def test_tag_release_no_longer_depends_on_live_proof():
    job = JOBS['release-attach']
    assert job['needs'] == 'package'
    assert "startsWith(github.ref, 'refs/tags/v') &&" in job['if']
    assert 'pulsar-live-broadcast-proof' not in str(job['steps'])

def test_existing_draft_publication_waits_for_all_non_live_gates():
    job = JOBS['release-publish']
    assert set(job['needs']) == {'lint', 'contract-tests', 'build', 'binary-gate',
                                'offline-probes', 'capture-pgm-compat'}
    assert "github.ref == 'refs/heads/main'" in job['if']
    assert "github.event_name == 'workflow_dispatch'" in job['if']
    assert "github.event.inputs.enable_release_attach == 'true'" in job['if']
    assert "github.event.inputs.release_tag != ''" in job['if']
    assert not job.get('continue-on-error')

def test_existing_release_tag_is_passed_as_data_not_shell_source():
    step = JOBS['release-publish']['steps'][-1]
    assert step['env']['RELEASE_TAG'] == '${{ github.event.inputs.release_tag }}'
    assert '--tag "$RELEASE_TAG"' in step['run']
    assert '${{' not in step['run']
