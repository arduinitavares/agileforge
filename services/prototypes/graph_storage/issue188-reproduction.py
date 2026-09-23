"""Provider-free issue 188 probe against the archived current checkout."""
import json
import os
from pathlib import Path
import sys
import tarfile

source = Path('/tmp/issue188-source')
source.mkdir()
with tarfile.open('/input/source.tar') as archive:
    archive.extractall(source, filter='data')
os.chdir(source)
sys.path.insert(0, str(source))

from tests.conftest import fresh_test_engine
from tests.workflow.planning_fixtures import (
    _seed_accepted_backlog, _record_and_accept_roadmap,
    _record_and_accept_story, _select_for_sprint, _guards, _decision, _story_content,
)
from tests.workflow.test_planning_transitions import (
    _domain, ApplyStoryDependencies, story_dependency_source_fingerprint,
)
from tests.services.test_durable_product_definition_projections import _record_and_accept_story_set
from workflow.requests.planning import ReviewedDependencyEdge
from repositories.workflow import WorkflowFactRepository
from sqlmodel import Session
from models.core import UserStory

def snapshot(engine, project_id):
    with Session(engine) as session:
        return WorkflowFactRepository(session).load(project_id)

with fresh_test_engine('sqlite:///:memory:') as engine:
    requirements = ('Provide prerequisite A', 'Deliver dependent B')
    project_id = _seed_accepted_backlog(engine, requirements=requirements)
    domain = _domain(engine)
    _record_and_accept_roadmap(domain, project_id, requirements=requirements)
    artifact_a, story_a = _record_and_accept_story(
        engine, domain, project_id, requirement=requirements[0],
        spec_item_id='REQ.planning-1', backlog_item_id='PBI-000001',
        idempotency_suffix='-A',
    )
    _, story_b = _record_and_accept_story(
        engine, domain, project_id, requirement=requirements[1],
        spec_item_id='REQ.planning-2', backlog_item_id='PBI-000002',
        idempotency_suffix='-B',
    )
    _select_for_sprint(engine, story_b)
    before = snapshot(engine, project_id)
    fingerprint_before = story_dependency_source_fingerprint(before.stories)
    result = domain.transition(ApplyStoryDependencies(
        **_guards(domain.position(project_id), 'planning.story_dependencies'),
        idempotency_key='issue188-review-before-replacement',
        selected_story_ids=(story_b,),
        reviewed_edges=(ReviewedDependencyEdge(
            dependent_story_id=story_b, prerequisite_story_id=story_a,
            reason='B needs the capability delivered by A.',
        ),),
        source_fingerprint=fingerprint_before,
    ))
    assert result.ok, result
    reviewed = snapshot(engine, project_id)
    _, replacements = _record_and_accept_story_set(
        domain, project_id, backlog_item_id='PBI-000001',
        content=_story_content('Corrected prerequisite A', spec_item_id='REQ.planning-1'),
        idempotency_suffix='issue188-C', supersedes_story_artifact_id=artifact_a,
    )
    after = snapshot(engine, project_id)
    fingerprint_after = story_dependency_source_fingerprint(after.stories)
    decision = _decision(domain.position(project_id), 'planning.story_dependencies')
    with Session(engine) as session:
        prior = session.get(UserStory, story_a)
        assert prior is not None
        superseded = prior.is_superseded
    report = {
        'story_a': story_a, 'story_b': story_b, 'replacement_c': replacements,
        'a_is_superseded': superseded,
        'source_fingerprint_before': fingerprint_before,
        'source_fingerprint_after': fingerprint_after,
        'review_unchanged': reviewed.story_dependency_reviews == after.story_dependency_reviews,
        'edges': [edge.model_dump(mode='json') for edge in after.story_dependencies],
        'decision_category': decision.category.value,
        'decision_reason': decision.reason_code,
    }
    print(json.dumps(report, indent=2), flush=True)
    assert superseded
    assert fingerprint_before == fingerprint_after
    assert reviewed.story_dependency_reviews == after.story_dependency_reviews
    assert any(edge.dependent_story_id == story_b and edge.prerequisite_story_id == story_a for edge in after.story_dependencies)
    assert decision.reason_code == 'STORY_DEPENDENCY_EXTERNAL_INCOMPLETE'
    print('CONFIRMED: real review-before-replacement sequence reproduces issue 188.', flush=True)
