-- table: authority_curation_attempts
CREATE TABLE authority_curation_attempts (
	curation_row_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	mutation_event_id INTEGER,
	curation_attempt_id VARCHAR NOT NULL,
	source_authority_id INTEGER NOT NULL,
	source_authority_fingerprint VARCHAR NOT NULL,
	spec_version_id INTEGER NOT NULL,
	feedback_attempt_id VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'running' NOT NULL,
	max_iterations INTEGER DEFAULT 2 NOT NULL,
	iteration_count INTEGER DEFAULT 0 NOT NULL,
	compiler_model VARCHAR,
	candidate_authority_id INTEGER,
	candidate_authority_fingerprint VARCHAR,
	request_json TEXT DEFAULT '{}' NOT NULL,
	candidate_lineage_json TEXT DEFAULT '{}' NOT NULL,
	diff_summary_json TEXT DEFAULT '{}' NOT NULL,
	lineage_json TEXT DEFAULT '{}' NOT NULL,
	quality_report_json TEXT DEFAULT '{}' NOT NULL,
	contract_version VARCHAR DEFAULT 'authority_curation.v1' NOT NULL,
	menu_fingerprint VARCHAR,
	selection_fingerprint VARCHAR,
	rejected_selection_json TEXT DEFAULT '{}' NOT NULL,
	overlay_json TEXT DEFAULT '{}' NOT NULL,
	failure_artifact_id VARCHAR,
	request_hash VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	changed_by VARCHAR DEFAULT 'cli-agent' NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (curation_row_id),
	CONSTRAINT uq_authority_curation_project_attempt UNIQUE (project_id, curation_attempt_id),
	CONSTRAINT uq_authority_curation_project_idempotency UNIQUE (project_id, idempotency_key),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: authority_feedback_attempts
CREATE TABLE authority_feedback_attempts (
	feedback_row_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	feedback_attempt_id VARCHAR NOT NULL,
	source_authority_id INTEGER NOT NULL,
	source_authority_fingerprint VARCHAR NOT NULL,
	feedback_fingerprint VARCHAR NOT NULL,
	status VARCHAR DEFAULT 'recorded' NOT NULL,
	has_blocking_feedback BOOLEAN DEFAULT 0 NOT NULL,
	feedback_json TEXT NOT NULL,
	request_hash VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	changed_by VARCHAR DEFAULT 'cli-agent' NOT NULL,
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (feedback_row_id),
	CONSTRAINT uq_authority_feedback_project_attempt UNIQUE (project_id, feedback_attempt_id),
	CONSTRAINT uq_authority_feedback_project_idempotency UNIQUE (project_id, idempotency_key),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: backlog_artifact_decisions
CREATE TABLE backlog_artifact_decisions (
	backlog_artifact_decision_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	backlog_artifact_id INTEGER NOT NULL,
	artifact_fingerprint VARCHAR NOT NULL,
	decision VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	reviewer VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (backlog_artifact_decision_id),
	CONSTRAINT uq_backlog_artifact_decision UNIQUE (project_id, backlog_artifact_id),
	CONSTRAINT ck_backlog_artifact_decision CHECK (decision IN ('accepted', 'rejected', 'feedback')),
	CONSTRAINT fk_backlog_artifact_decision_parent FOREIGN KEY(project_id, backlog_artifact_id, artifact_fingerprint) REFERENCES backlog_artifacts (project_id, backlog_artifact_id, content_fingerprint)
);

-- table: backlog_artifacts
CREATE TABLE backlog_artifacts (
	backlog_artifact_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	authority_id INTEGER NOT NULL,
	authority_fingerprint VARCHAR NOT NULL,
	product_goal_artifact_id INTEGER NOT NULL,
	product_goal_fingerprint VARCHAR NOT NULL,
	version_number INTEGER NOT NULL,
	canonical_content_json TEXT NOT NULL,
	content_fingerprint VARCHAR NOT NULL,
	supersedes_backlog_artifact_id INTEGER,
	created_by VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (backlog_artifact_id),
	CONSTRAINT uq_backlog_artifact_project_id UNIQUE (project_id, backlog_artifact_id),
	CONSTRAINT uq_backlog_artifact_review_parent UNIQUE (project_id, backlog_artifact_id, content_fingerprint),
	CONSTRAINT uq_backlog_artifact_version UNIQUE (project_id, version_number),
	CONSTRAINT uq_backlog_artifact_fingerprint UNIQUE (project_id, content_fingerprint),
	CONSTRAINT fk_backlog_artifact_supersedes FOREIGN KEY(project_id, supersedes_backlog_artifact_id) REFERENCES backlog_artifacts (project_id, backlog_artifact_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(authority_id) REFERENCES compiled_spec_authority (authority_id)
);

-- table: compiled_spec_authority
CREATE TABLE compiled_spec_authority (
	authority_id INTEGER NOT NULL,
	spec_version_id INTEGER NOT NULL,
	compiler_version VARCHAR NOT NULL,
	prompt_hash VARCHAR NOT NULL,
	compiled_at DATETIME NOT NULL,
	compiled_artifact_json TEXT,
	scope_themes TEXT NOT NULL,
	invariants TEXT NOT NULL,
	eligible_feature_ids TEXT NOT NULL,
	rejected_features TEXT,
	spec_gaps TEXT,
	PRIMARY KEY (authority_id),
	FOREIGN KEY(spec_version_id) REFERENCES spec_registry (spec_version_id)
);

-- table: epics
CREATE TABLE epics (
	epic_id INTEGER NOT NULL,
	title VARCHAR NOT NULL,
	summary TEXT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	theme_id INTEGER NOT NULL,
	PRIMARY KEY (epic_id),
	FOREIGN KEY(theme_id) REFERENCES themes (theme_id)
);

-- table: features
CREATE TABLE features (
	feature_id INTEGER NOT NULL,
	title VARCHAR NOT NULL,
	description TEXT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	epic_id INTEGER NOT NULL,
	PRIMARY KEY (feature_id),
	FOREIGN KEY(epic_id) REFERENCES epics (epic_id)
);

-- table: post_sprint_triage
CREATE TABLE post_sprint_triage (
	triage_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	impact VARCHAR NOT NULL,
	canonical_payload_json TEXT NOT NULL,
	payload_fingerprint VARCHAR NOT NULL,
	supersedes_triage_id INTEGER,
	recorded_by VARCHAR NOT NULL,
	recorded_at DATETIME NOT NULL,
	PRIMARY KEY (triage_id),
	CONSTRAINT ck_post_sprint_triage_impact CHECK (impact IN ('none', 'backlog', 'specification')),
	CONSTRAINT uq_post_sprint_triage_correction UNIQUE (project_id, sprint_id, supersedes_triage_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id),
	FOREIGN KEY(supersedes_triage_id) REFERENCES post_sprint_triage (triage_id)
);

-- table: product_goal_artifact_decisions
CREATE TABLE product_goal_artifact_decisions (
	product_goal_artifact_decision_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	product_goal_artifact_id INTEGER NOT NULL,
	artifact_fingerprint VARCHAR NOT NULL,
	decision VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	reviewer VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (product_goal_artifact_decision_id),
	CONSTRAINT ck_product_goal_artifact_decision CHECK (decision IN ('accepted', 'rejected', 'feedback')),
	CONSTRAINT uq_product_goal_artifact_decision_idempotency UNIQUE (project_id, idempotency_key),
	CONSTRAINT fk_product_goal_artifact_decision_parent FOREIGN KEY(project_id, product_goal_artifact_id, artifact_fingerprint) REFERENCES product_goal_artifacts (project_id, product_goal_artifact_id, content_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: product_goal_artifacts
CREATE TABLE product_goal_artifacts (
	product_goal_artifact_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	vision_artifact_id INTEGER NOT NULL,
	vision_fingerprint VARCHAR NOT NULL,
	goal_number INTEGER NOT NULL,
	revision_number INTEGER NOT NULL,
	statement TEXT NOT NULL,
	content_fingerprint VARCHAR NOT NULL,
	supersedes_product_goal_artifact_id INTEGER,
	source_interview_turn_id INTEGER NOT NULL,
	created_by VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (product_goal_artifact_id),
	CONSTRAINT uq_product_goal_artifact_project_id UNIQUE (project_id, product_goal_artifact_id),
	CONSTRAINT uq_product_goal_artifact_parent UNIQUE (project_id, product_goal_artifact_id, content_fingerprint),
	CONSTRAINT uq_product_goal_artifact_version UNIQUE (project_id, goal_number, revision_number),
	CONSTRAINT fk_product_goal_artifact_vision FOREIGN KEY(project_id, vision_artifact_id, vision_fingerprint) REFERENCES vision_artifacts (project_id, vision_artifact_id, content_fingerprint),
	CONSTRAINT fk_product_goal_artifact_supersedes FOREIGN KEY(project_id, supersedes_product_goal_artifact_id) REFERENCES product_goal_artifacts (project_id, product_goal_artifact_id),
	CONSTRAINT fk_product_goal_artifact_source_turn FOREIGN KEY(project_id, source_interview_turn_id) REFERENCES product_goal_interview_turns (project_id, product_goal_interview_turn_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: product_goal_interview_turns
CREATE TABLE product_goal_interview_turns (
	product_goal_interview_turn_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	vision_artifact_id INTEGER NOT NULL,
	vision_fingerprint VARCHAR NOT NULL,
	goal_number INTEGER NOT NULL,
	revision_number INTEGER NOT NULL,
	prior_turn_id INTEGER,
	user_text TEXT NOT NULL,
	components_json TEXT NOT NULL,
	goal_statement TEXT NOT NULL,
	is_complete BOOLEAN NOT NULL,
	clarifying_questions_json TEXT NOT NULL,
	output_fingerprint VARCHAR NOT NULL,
	workflow_node_attempt_id INTEGER NOT NULL,
	attempt_fingerprint VARCHAR NOT NULL,
	recorded_at DATETIME NOT NULL,
	PRIMARY KEY (product_goal_interview_turn_id),
	CONSTRAINT uq_product_goal_interview_turn_project_id UNIQUE (project_id, product_goal_interview_turn_id),
	CONSTRAINT uq_product_goal_interview_turn_identity UNIQUE (project_id, goal_number, revision_number, product_goal_interview_turn_id),
	CONSTRAINT fk_product_goal_interview_turn_vision FOREIGN KEY(project_id, vision_artifact_id, vision_fingerprint) REFERENCES vision_artifacts (project_id, vision_artifact_id, content_fingerprint),
	CONSTRAINT fk_product_goal_interview_turn_prior_turn FOREIGN KEY(project_id, prior_turn_id) REFERENCES product_goal_interview_turns (project_id, product_goal_interview_turn_id),
	CONSTRAINT fk_product_goal_interview_turn_attempt FOREIGN KEY(project_id, workflow_node_attempt_id) REFERENCES workflow_node_attempts (project_id, workflow_node_attempt_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: product_goal_outcomes
CREATE TABLE product_goal_outcomes (
	product_goal_outcome_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	product_goal_artifact_id INTEGER NOT NULL,
	artifact_fingerprint VARCHAR NOT NULL,
	outcome VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	decided_by VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (product_goal_outcome_id),
	CONSTRAINT ck_product_goal_outcome CHECK (outcome IN ('fulfilled', 'abandoned')),
	CONSTRAINT uq_product_goal_outcome_artifact UNIQUE (project_id, product_goal_artifact_id),
	CONSTRAINT uq_product_goal_outcome_idempotency UNIQUE (project_id, idempotency_key),
	CONSTRAINT fk_product_goal_outcome_parent FOREIGN KEY(project_id, product_goal_artifact_id, artifact_fingerprint) REFERENCES product_goal_artifacts (project_id, product_goal_artifact_id, content_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: project_personas
CREATE TABLE project_personas (
	persona_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	persona_name VARCHAR(100) NOT NULL,
	is_default BOOLEAN NOT NULL,
	category VARCHAR(50) NOT NULL,
	description TEXT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (persona_id),
	CONSTRAINT unique_project_persona UNIQUE (project_id, persona_name),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: project_teams
CREATE TABLE project_teams (
	project_id INTEGER NOT NULL,
	team_id INTEGER NOT NULL,
	PRIMARY KEY (project_id, team_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(team_id) REFERENCES teams (team_id)
);

-- table: projects
CREATE TABLE projects (
	project_id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	description TEXT,
	active_repository_binding_id INTEGER,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (project_id),
	CONSTRAINT fk_project_active_repository_binding FOREIGN KEY(active_repository_binding_id) REFERENCES repository_bindings (repository_binding_id) ON DELETE SET NULL
);

-- table: repository_bindings
CREATE TABLE repository_bindings (
	repository_binding_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	worktree_path TEXT NOT NULL,
	common_git_dir TEXT NOT NULL,
	head_sha VARCHAR(40) NOT NULL,
	branch_name VARCHAR,
	detached_head BOOLEAN NOT NULL,
	dirty BOOLEAN NOT NULL,
	status_fingerprint VARCHAR NOT NULL,
	status_entries_json TEXT NOT NULL,
	remotes_json TEXT NOT NULL,
	warnings_json TEXT NOT NULL,
	probe_version VARCHAR NOT NULL,
	inspected_at DATETIME NOT NULL,
	supersedes_repository_binding_id INTEGER,
	recorded_by VARCHAR NOT NULL,
	PRIMARY KEY (repository_binding_id),
	CONSTRAINT uq_repository_binding_project_id UNIQUE (project_id, repository_binding_id),
	CONSTRAINT uq_repository_binding_project_fingerprint_inspected_at UNIQUE (project_id, status_fingerprint, inspected_at),
	CONSTRAINT fk_repository_binding_supersedes FOREIGN KEY(project_id, supersedes_repository_binding_id) REFERENCES repository_bindings (project_id, repository_binding_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: roadmap_artifact_decisions
CREATE TABLE roadmap_artifact_decisions (
	roadmap_artifact_decision_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	roadmap_artifact_id INTEGER NOT NULL,
	artifact_fingerprint VARCHAR NOT NULL,
	decision VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	reviewer VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (roadmap_artifact_decision_id),
	CONSTRAINT uq_roadmap_decision UNIQUE (project_id, roadmap_artifact_id),
	CONSTRAINT ck_roadmap_decision CHECK (decision IN ('accepted', 'rejected', 'feedback')),
	CONSTRAINT fk_roadmap_decision_parent FOREIGN KEY(project_id, roadmap_artifact_id, artifact_fingerprint) REFERENCES roadmap_artifacts (project_id, roadmap_artifact_id, content_fingerprint)
);

-- table: roadmap_artifacts
CREATE TABLE roadmap_artifacts (
	roadmap_artifact_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	backlog_artifact_id INTEGER NOT NULL,
	backlog_artifact_fingerprint VARCHAR NOT NULL,
	version_number INTEGER NOT NULL,
	canonical_content_json TEXT NOT NULL,
	content_fingerprint VARCHAR NOT NULL,
	supersedes_roadmap_artifact_id INTEGER,
	created_by VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (roadmap_artifact_id),
	CONSTRAINT uq_roadmap_project UNIQUE (project_id, roadmap_artifact_id),
	CONSTRAINT uq_roadmap_review_parent UNIQUE (project_id, roadmap_artifact_id, content_fingerprint),
	CONSTRAINT uq_roadmap_version UNIQUE (project_id, version_number),
	CONSTRAINT uq_roadmap_fingerprint UNIQUE (project_id, content_fingerprint),
	CONSTRAINT fk_roadmap_backlog FOREIGN KEY(project_id, backlog_artifact_id, backlog_artifact_fingerprint) REFERENCES backlog_artifacts (project_id, backlog_artifact_id, content_fingerprint),
	CONSTRAINT fk_roadmap_supersedes FOREIGN KEY(project_id, supersedes_roadmap_artifact_id) REFERENCES roadmap_artifacts (project_id, roadmap_artifact_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: spec_authority_acceptance
CREATE TABLE spec_authority_acceptance (
	id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	spec_version_id INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	policy VARCHAR NOT NULL,
	decided_by VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	rationale TEXT,
	compiler_version VARCHAR NOT NULL,
	prompt_hash VARCHAR NOT NULL,
	spec_hash VARCHAR NOT NULL,
	pending_authority_id INTEGER,
	authority_fingerprint VARCHAR,
	review_token VARCHAR,
	review_fingerprint VARCHAR,
	disk_spec_hash VARCHAR,
	resolved_spec_path VARCHAR,
	actor_mode VARCHAR,
	review_completeness VARCHAR,
	incomplete_review_override BOOLEAN NOT NULL,
	incomplete_review_rationale VARCHAR,
	incomplete_review_overrides_json TEXT,
	terminal_decision_key VARCHAR,
	provenance_source VARCHAR NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(spec_version_id) REFERENCES spec_registry (spec_version_id)
);

-- table: spec_registry
CREATE TABLE spec_registry (
	spec_version_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	spec_hash VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	approved_at DATETIME,
	approved_by VARCHAR,
	approval_notes TEXT,
	source_specification_candidate_id INTEGER NOT NULL,
	source_specification_candidate_fingerprint VARCHAR NOT NULL,
	source_vision_artifact_id INTEGER NOT NULL,
	source_vision_fingerprint VARCHAR NOT NULL,
	source_product_goal_artifact_id INTEGER NOT NULL,
	source_product_goal_fingerprint VARCHAR NOT NULL,
	supersedes_spec_version_id INTEGER,
	PRIMARY KEY (spec_version_id),
	CONSTRAINT uq_spec_registry_project_id UNIQUE (project_id, spec_version_id),
	CONSTRAINT ck_spec_registry_status CHECK (status IN ('approved', 'superseded')),
	CONSTRAINT uq_spec_registry_project_id_hash UNIQUE (project_id, spec_version_id, spec_hash),
	CONSTRAINT uq_spec_registry_candidate UNIQUE (project_id, source_specification_candidate_id),
	CONSTRAINT uq_spec_registry_source_candidate UNIQUE (project_id, source_specification_candidate_id, source_specification_candidate_fingerprint, spec_hash),
	CONSTRAINT fk_spec_registry_source_candidate FOREIGN KEY(project_id, source_specification_candidate_id, source_specification_candidate_fingerprint, spec_hash) REFERENCES specification_candidates (project_id, specification_candidate_id, candidate_fingerprint, payload_fingerprint) DEFERRABLE INITIALLY DEFERRED,
	CONSTRAINT fk_spec_registry_source_vision FOREIGN KEY(project_id, source_vision_artifact_id, source_vision_fingerprint) REFERENCES vision_artifacts (project_id, vision_artifact_id, content_fingerprint),
	CONSTRAINT fk_spec_registry_source_goal FOREIGN KEY(project_id, source_product_goal_artifact_id, source_product_goal_fingerprint) REFERENCES product_goal_artifacts (project_id, product_goal_artifact_id, content_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(supersedes_spec_version_id) REFERENCES spec_registry (spec_version_id)
);

-- table: specification_candidates
CREATE TABLE specification_candidates (
	specification_candidate_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	candidate_kind VARCHAR NOT NULL,
	specification_source_id INTEGER NOT NULL,
	specification_source_fingerprint VARCHAR NOT NULL,
	vision_artifact_id INTEGER NOT NULL,
	vision_fingerprint VARCHAR NOT NULL,
	product_goal_artifact_id INTEGER NOT NULL,
	product_goal_fingerprint VARCHAR NOT NULL,
	base_spec_version_id INTEGER,
	base_spec_hash VARCHAR,
	canonical_envelope_json TEXT NOT NULL,
	payload_fingerprint VARCHAR NOT NULL,
	source_manifest_fingerprint VARCHAR NOT NULL,
	producer_input_fingerprint VARCHAR NOT NULL,
	rendered_view_fingerprint VARCHAR NOT NULL,
	candidate_fingerprint VARCHAR NOT NULL,
	workflow_node_attempt_id INTEGER NOT NULL,
	attempt_fingerprint VARCHAR NOT NULL,
	supersedes_specification_candidate_id INTEGER,
	supersedes_candidate_fingerprint VARCHAR,
	recorded_by VARCHAR NOT NULL,
	recorded_at DATETIME NOT NULL,
	PRIMARY KEY (specification_candidate_id),
	CONSTRAINT uq_specification_candidate_identity UNIQUE (project_id, specification_candidate_id, candidate_fingerprint),
	CONSTRAINT uq_specification_candidate_payload_identity UNIQUE (project_id, specification_candidate_id, candidate_fingerprint, payload_fingerprint),
	CONSTRAINT uq_specification_candidate_attempt UNIQUE (project_id, workflow_node_attempt_id),
	CONSTRAINT uq_specification_candidate_successor UNIQUE (project_id, supersedes_specification_candidate_id),
	CONSTRAINT ck_specification_candidate_kind CHECK (candidate_kind IN ('initial', 'amendment')),
	CONSTRAINT ck_specification_candidate_base_spec CHECK ((candidate_kind = 'initial' AND base_spec_version_id IS NULL AND base_spec_hash IS NULL) OR (candidate_kind = 'amendment' AND base_spec_version_id IS NOT NULL AND base_spec_hash IS NOT NULL)),
	CONSTRAINT ck_specification_candidate_supersedes CHECK ((supersedes_specification_candidate_id IS NULL AND supersedes_candidate_fingerprint IS NULL) OR (supersedes_specification_candidate_id IS NOT NULL AND supersedes_candidate_fingerprint IS NOT NULL)),
	CONSTRAINT fk_specification_candidate_source FOREIGN KEY(project_id, specification_source_id, specification_source_fingerprint) REFERENCES specification_sources (project_id, specification_source_id, source_fingerprint),
	CONSTRAINT fk_specification_candidate_vision FOREIGN KEY(project_id, vision_artifact_id, vision_fingerprint) REFERENCES vision_artifacts (project_id, vision_artifact_id, content_fingerprint),
	CONSTRAINT fk_specification_candidate_goal FOREIGN KEY(project_id, product_goal_artifact_id, product_goal_fingerprint) REFERENCES product_goal_artifacts (project_id, product_goal_artifact_id, content_fingerprint),
	CONSTRAINT fk_specification_candidate_base_spec FOREIGN KEY(project_id, base_spec_version_id, base_spec_hash) REFERENCES spec_registry (project_id, spec_version_id, spec_hash) DEFERRABLE INITIALLY DEFERRED,
	CONSTRAINT fk_specification_candidate_attempt FOREIGN KEY(project_id, workflow_node_attempt_id, attempt_fingerprint) REFERENCES workflow_node_attempts (project_id, workflow_node_attempt_id, attempt_fingerprint),
	CONSTRAINT fk_specification_candidate_supersedes FOREIGN KEY(project_id, supersedes_specification_candidate_id, supersedes_candidate_fingerprint) REFERENCES specification_candidates (project_id, specification_candidate_id, candidate_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: specification_decisions
CREATE TABLE specification_decisions (
	specification_decision_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	specification_candidate_id INTEGER NOT NULL,
	candidate_fingerprint VARCHAR NOT NULL,
	decision VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	reviewer VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (specification_decision_id),
	CONSTRAINT ck_specification_decision CHECK (decision IN ('accepted', 'rejected', 'feedback')),
	CONSTRAINT uq_specification_decision_idempotency UNIQUE (project_id, idempotency_key),
	CONSTRAINT uq_specification_decision_candidate UNIQUE (project_id, specification_candidate_id),
	CONSTRAINT fk_specification_decision_parent FOREIGN KEY(project_id, specification_candidate_id, candidate_fingerprint) REFERENCES specification_candidates (project_id, specification_candidate_id, candidate_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: specification_sources
CREATE TABLE specification_sources (
	specification_source_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	source_bundle_json TEXT NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	repository_binding_id INTEGER NOT NULL,
	repository_head_sha VARCHAR(40) NOT NULL,
	repository_dirty BOOLEAN NOT NULL,
	repository_status_fingerprint VARCHAR NOT NULL,
	vision_artifact_id INTEGER NOT NULL,
	vision_fingerprint VARCHAR NOT NULL,
	product_goal_artifact_id INTEGER NOT NULL,
	product_goal_fingerprint VARCHAR NOT NULL,
	supersedes_specification_source_id INTEGER,
	supersedes_source_fingerprint VARCHAR,
	registered_by VARCHAR NOT NULL,
	registered_at DATETIME NOT NULL,
	PRIMARY KEY (specification_source_id),
	CONSTRAINT uq_specification_source_identity UNIQUE (project_id, specification_source_id, source_fingerprint),
	CONSTRAINT uq_specification_source_successor UNIQUE (project_id, supersedes_specification_source_id),
	CONSTRAINT ck_specification_source_supersedes CHECK ((supersedes_specification_source_id IS NULL AND supersedes_source_fingerprint IS NULL) OR (supersedes_specification_source_id IS NOT NULL AND supersedes_source_fingerprint IS NOT NULL)),
	CONSTRAINT fk_specification_source_repository_binding FOREIGN KEY(project_id, repository_binding_id) REFERENCES repository_bindings (project_id, repository_binding_id),
	CONSTRAINT fk_specification_source_vision FOREIGN KEY(project_id, vision_artifact_id, vision_fingerprint) REFERENCES vision_artifacts (project_id, vision_artifact_id, content_fingerprint),
	CONSTRAINT fk_specification_source_goal FOREIGN KEY(project_id, product_goal_artifact_id, product_goal_fingerprint) REFERENCES product_goal_artifacts (project_id, product_goal_artifact_id, content_fingerprint),
	CONSTRAINT fk_specification_source_supersedes FOREIGN KEY(project_id, supersedes_specification_source_id, supersedes_source_fingerprint) REFERENCES specification_sources (project_id, specification_source_id, source_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: sprint_closures
CREATE TABLE sprint_closures (
	sprint_closure_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	review_fingerprint VARCHAR NOT NULL,
	close_fingerprint VARCHAR NOT NULL,
	closed_by VARCHAR NOT NULL,
	closed_at DATETIME NOT NULL,
	PRIMARY KEY (sprint_closure_id),
	CONSTRAINT uq_sprint_closure UNIQUE (sprint_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

-- table: sprint_plan_artifact_decisions
CREATE TABLE sprint_plan_artifact_decisions (
	sprint_plan_artifact_decision_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_plan_artifact_id INTEGER NOT NULL,
	plan_fingerprint VARCHAR NOT NULL,
	decision VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	reviewer VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (sprint_plan_artifact_decision_id),
	CONSTRAINT uq_sprint_plan_decision UNIQUE (project_id, sprint_plan_artifact_id),
	CONSTRAINT uq_sprint_plan_decision_lineage UNIQUE (project_id, sprint_plan_artifact_id, sprint_plan_artifact_decision_id),
	CONSTRAINT ck_sprint_plan_decision CHECK (decision IN ('accepted', 'rejected', 'feedback')),
	CONSTRAINT fk_sprint_plan_decision_parent FOREIGN KEY(project_id, sprint_plan_artifact_id, plan_fingerprint) REFERENCES sprint_plan_artifacts (project_id, sprint_plan_artifact_id, plan_fingerprint)
);

-- table: sprint_plan_artifacts
CREATE TABLE sprint_plan_artifacts (
	sprint_plan_artifact_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	version_number INTEGER NOT NULL,
	selected_story_ids_json TEXT NOT NULL,
	canonical_task_plan_json TEXT NOT NULL,
	plan_fingerprint VARCHAR NOT NULL,
	candidate_set_fingerprint VARCHAR NOT NULL,
	supersedes_sprint_plan_artifact_id INTEGER,
	created_by VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (sprint_plan_artifact_id),
	CONSTRAINT uq_sprint_plan_project UNIQUE (project_id, sprint_plan_artifact_id),
	CONSTRAINT uq_sprint_plan_review_parent UNIQUE (project_id, sprint_plan_artifact_id, plan_fingerprint),
	CONSTRAINT uq_sprint_plan_version UNIQUE (project_id, version_number),
	CONSTRAINT uq_sprint_plan_fingerprint UNIQUE (project_id, plan_fingerprint),
	CONSTRAINT fk_sprint_plan_supersedes FOREIGN KEY(project_id, supersedes_sprint_plan_artifact_id) REFERENCES sprint_plan_artifacts (project_id, sprint_plan_artifact_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

-- table: sprint_reviews
CREATE TABLE sprint_reviews (
	sprint_review_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	review_fingerprint VARCHAR NOT NULL,
	reviewed_by VARCHAR NOT NULL,
	reviewed_at DATETIME NOT NULL,
	PRIMARY KEY (sprint_review_id),
	CONSTRAINT uq_sprint_review UNIQUE (sprint_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

-- table: sprint_starts
CREATE TABLE sprint_starts (
	sprint_start_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	sprint_plan_artifact_id INTEGER NOT NULL,
	sprint_plan_artifact_decision_id INTEGER NOT NULL,
	story_dependency_review_id INTEGER NOT NULL,
	plan_fingerprint VARCHAR NOT NULL,
	candidate_set_fingerprint VARCHAR NOT NULL,
	selected_story_ids_json TEXT NOT NULL,
	task_content_fingerprint VARCHAR NOT NULL,
	dependency_source_fingerprint VARCHAR NOT NULL,
	dependency_fingerprint VARCHAR NOT NULL,
	dependency_rows_fingerprint VARCHAR NOT NULL,
	decision_fingerprint VARCHAR NOT NULL,
	audit_event_id INTEGER NOT NULL,
	started_by VARCHAR NOT NULL,
	started_at DATETIME NOT NULL,
	PRIMARY KEY (sprint_start_id),
	CONSTRAINT uq_sprint_start UNIQUE (sprint_id),
	CONSTRAINT uq_sprint_start_audit_event UNIQUE (audit_event_id),
	CONSTRAINT fk_sprint_start_accepted_plan FOREIGN KEY(project_id, sprint_plan_artifact_id, sprint_plan_artifact_decision_id) REFERENCES sprint_plan_artifact_decisions (project_id, sprint_plan_artifact_id, sprint_plan_artifact_decision_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id),
	FOREIGN KEY(story_dependency_review_id) REFERENCES story_dependency_reviews (story_dependency_review_id),
	FOREIGN KEY(audit_event_id) REFERENCES workflow_events (event_id)
);

-- table: sprint_stories
CREATE TABLE sprint_stories (
	sprint_id INTEGER NOT NULL,
	story_id INTEGER NOT NULL,
	added_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (sprint_id, story_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id),
	FOREIGN KEY(story_id) REFERENCES user_stories (story_id)
);

-- table: sprints
CREATE TABLE sprints (
	sprint_id INTEGER NOT NULL,
	goal TEXT,
	start_date DATE,
	end_date DATE,
	status VARCHAR(9) NOT NULL,
	started_at DATETIME,
	completed_at DATETIME,
	close_snapshot_json TEXT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	project_id INTEGER NOT NULL,
	team_id INTEGER NOT NULL,
	PRIMARY KEY (sprint_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(team_id) REFERENCES teams (team_id)
);

-- table: story_artifact_decisions
CREATE TABLE story_artifact_decisions (
	story_artifact_decision_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	story_artifact_id INTEGER NOT NULL,
	artifact_fingerprint VARCHAR NOT NULL,
	decision VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	reviewer VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (story_artifact_decision_id),
	CONSTRAINT uq_story_artifact_decision UNIQUE (project_id, story_artifact_id),
	CONSTRAINT ck_story_artifact_decision CHECK (decision IN ('accepted', 'rejected', 'feedback')),
	CONSTRAINT fk_story_artifact_decision_parent FOREIGN KEY(project_id, story_artifact_id, artifact_fingerprint) REFERENCES story_artifacts (project_id, story_artifact_id, content_fingerprint)
);

-- table: story_artifacts
CREATE TABLE story_artifacts (
	story_artifact_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	requirement_id VARCHAR NOT NULL,
	roadmap_artifact_id INTEGER NOT NULL,
	roadmap_artifact_fingerprint VARCHAR NOT NULL,
	version_number INTEGER NOT NULL,
	canonical_content_json TEXT NOT NULL,
	content_fingerprint VARCHAR NOT NULL,
	story_ids_json TEXT NOT NULL,
	supersedes_story_artifact_id INTEGER,
	created_by VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (story_artifact_id),
	CONSTRAINT uq_story_artifact_project UNIQUE (project_id, story_artifact_id),
	CONSTRAINT uq_story_artifact_review_parent UNIQUE (project_id, story_artifact_id, content_fingerprint),
	CONSTRAINT uq_story_artifact_version UNIQUE (project_id, requirement_id, version_number),
	CONSTRAINT uq_story_artifact_fingerprint UNIQUE (project_id, requirement_id, content_fingerprint),
	CONSTRAINT fk_story_artifact_roadmap FOREIGN KEY(project_id, roadmap_artifact_id, roadmap_artifact_fingerprint) REFERENCES roadmap_artifacts (project_id, roadmap_artifact_id, content_fingerprint),
	CONSTRAINT fk_story_artifact_supersedes FOREIGN KEY(project_id, supersedes_story_artifact_id) REFERENCES story_artifacts (project_id, story_artifact_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: story_closures
CREATE TABLE story_closures (
	story_closure_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	story_id INTEGER NOT NULL,
	completion_fingerprint VARCHAR NOT NULL,
	resolution VARCHAR NOT NULL,
	delivered TEXT NOT NULL,
	evidence TEXT NOT NULL,
	known_gaps TEXT NOT NULL,
	closed_by VARCHAR NOT NULL,
	closed_at DATETIME NOT NULL,
	PRIMARY KEY (story_closure_id),
	CONSTRAINT uq_story_closure UNIQUE (story_id, sprint_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id),
	FOREIGN KEY(story_id) REFERENCES user_stories (story_id)
);

-- table: story_completion_logs
CREATE TABLE story_completion_logs (
	log_id INTEGER NOT NULL,
	story_id INTEGER NOT NULL,
	old_status VARCHAR(11) NOT NULL,
	new_status VARCHAR(11) NOT NULL,
	resolution VARCHAR(22),
	delivered TEXT,
	evidence TEXT,
	known_gaps TEXT,
	follow_ups_created TEXT,
	changed_by VARCHAR,
	changed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (log_id),
	FOREIGN KEY(story_id) REFERENCES user_stories (story_id)
);

-- table: story_dependency_reviews
CREATE TABLE story_dependency_reviews (
	story_dependency_review_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	selected_story_ids_json TEXT NOT NULL,
	reviewed_edges_json TEXT NOT NULL,
	source_fingerprint VARCHAR NOT NULL,
	dependency_fingerprint VARCHAR NOT NULL,
	reviewed_by VARCHAR NOT NULL,
	reviewed_at DATETIME NOT NULL,
	PRIMARY KEY (story_dependency_review_id),
	CONSTRAINT uq_story_dependency_review_source UNIQUE (project_id, source_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: task_completion_evidence
CREATE TABLE task_completion_evidence (
	task_completion_evidence_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	task_id INTEGER NOT NULL,
	outcome_summary TEXT NOT NULL,
	artifact_refs_json TEXT NOT NULL,
	acceptance_result VARCHAR NOT NULL,
	checklist_result_json TEXT NOT NULL,
	evidence_fingerprint VARCHAR NOT NULL,
	completed_by VARCHAR NOT NULL,
	completed_at DATETIME NOT NULL,
	PRIMARY KEY (task_completion_evidence_id),
	CONSTRAINT uq_task_completion_evidence UNIQUE (task_id, sprint_id),
	CONSTRAINT ck_task_completion_acceptance CHECK (acceptance_result IN ('partially_met', 'fully_met')),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id),
	FOREIGN KEY(task_id) REFERENCES tasks (task_id)
);

-- table: task_execution_logs
CREATE TABLE task_execution_logs (
	log_id INTEGER NOT NULL,
	old_status VARCHAR(11),
	new_status VARCHAR(11) NOT NULL,
	outcome_summary TEXT,
	artifact_refs_json TEXT,
	acceptance_result VARCHAR(13) NOT NULL,
	notes TEXT,
	changed_by VARCHAR(100) NOT NULL,
	changed_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	task_id INTEGER NOT NULL,
	sprint_id INTEGER NOT NULL,
	PRIMARY KEY (log_id),
	FOREIGN KEY(task_id) REFERENCES tasks (task_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

-- table: tasks
CREATE TABLE tasks (
	task_id INTEGER NOT NULL,
	description TEXT NOT NULL,
	metadata_json TEXT,
	status VARCHAR(11) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	story_id INTEGER NOT NULL,
	assigned_to_member_id INTEGER,
	PRIMARY KEY (task_id),
	FOREIGN KEY(story_id) REFERENCES user_stories (story_id),
	FOREIGN KEY(assigned_to_member_id) REFERENCES team_members (member_id)
);

-- table: team_members
CREATE TABLE team_members (
	member_id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	email VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (member_id)
);

-- table: team_memberships
CREATE TABLE team_memberships (
	team_id INTEGER NOT NULL,
	member_id INTEGER NOT NULL,
	role VARCHAR(13) NOT NULL,
	PRIMARY KEY (team_id, member_id),
	FOREIGN KEY(team_id) REFERENCES teams (team_id),
	FOREIGN KEY(member_id) REFERENCES team_members (member_id)
);

-- table: teams
CREATE TABLE teams (
	team_id INTEGER NOT NULL,
	name VARCHAR NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (team_id)
);

-- table: themes
CREATE TABLE themes (
	theme_id INTEGER NOT NULL,
	title VARCHAR NOT NULL,
	description TEXT,
	time_frame VARCHAR(5),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	project_id INTEGER NOT NULL,
	PRIMARY KEY (theme_id),
	UNIQUE (project_id, title),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: user_stories
CREATE TABLE user_stories (
	story_id INTEGER NOT NULL,
	title VARCHAR NOT NULL,
	story_description TEXT,
	acceptance_criteria TEXT,
	status VARCHAR(11) NOT NULL,
	story_points INTEGER,
	rank VARCHAR,
	source_requirement VARCHAR,
	refinement_slot INTEGER,
	story_origin VARCHAR,
	is_refined BOOLEAN NOT NULL,
	is_superseded BOOLEAN NOT NULL,
	superseded_by_story_id INTEGER,
	archived_reason VARCHAR,
	archived_at DATETIME,
	archived_by VARCHAR(100),
	archive_reset_attempt_id VARCHAR,
	archive_previous_status VARCHAR,
	persona VARCHAR(100),
	resolution VARCHAR(22),
	completion_notes TEXT,
	evidence_links TEXT,
	completed_at DATETIME,
	original_acceptance_criteria TEXT,
	ac_updated_at DATETIME,
	ac_update_reason TEXT,
	accepted_spec_version_id INTEGER,
	validation_evidence TEXT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	project_id INTEGER NOT NULL,
	feature_id INTEGER,
	PRIMARY KEY (story_id),
	FOREIGN KEY(superseded_by_story_id) REFERENCES user_stories (story_id),
	FOREIGN KEY(accepted_spec_version_id) REFERENCES spec_registry (spec_version_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(feature_id) REFERENCES features (feature_id)
);

-- table: user_story_dependencies
CREATE TABLE user_story_dependencies (
	dependency_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	dependent_story_id INTEGER NOT NULL,
	prerequisite_story_id INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	source VARCHAR NOT NULL,
	confidence VARCHAR NOT NULL,
	reason TEXT,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	PRIMARY KEY (dependency_id),
	CONSTRAINT unique_user_story_dependency_edge UNIQUE (project_id, dependent_story_id, prerequisite_story_id),
	CONSTRAINT ck_user_story_dependencies_not_self CHECK (dependent_story_id <> prerequisite_story_id),
	CONSTRAINT ck_user_story_dependencies_status CHECK (status IN ('proposed', 'active', 'rejected')),
	CONSTRAINT ck_user_story_dependencies_source CHECK (source IN ('story_writer', 'dependency_repair', 'manual_review')),
	CONSTRAINT ck_user_story_dependencies_confidence CHECK (confidence IN ('explicit', 'inferred', 'reviewed')),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(dependent_story_id) REFERENCES user_stories (story_id),
	FOREIGN KEY(prerequisite_story_id) REFERENCES user_stories (story_id)
);

-- table: vision_artifact_decisions
CREATE TABLE vision_artifact_decisions (
	vision_artifact_decision_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	vision_artifact_id INTEGER NOT NULL,
	artifact_fingerprint VARCHAR NOT NULL,
	decision VARCHAR NOT NULL,
	rationale TEXT NOT NULL,
	reviewer VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	decided_at DATETIME NOT NULL,
	PRIMARY KEY (vision_artifact_decision_id),
	CONSTRAINT uq_vision_artifact_decision UNIQUE (project_id, vision_artifact_id),
	CONSTRAINT uq_vision_artifact_decision_idempotency UNIQUE (project_id, idempotency_key),
	CONSTRAINT ck_vision_artifact_decision CHECK (decision IN ('accepted', 'rejected', 'feedback')),
	CONSTRAINT fk_vision_artifact_decision_parent FOREIGN KEY(project_id, vision_artifact_id, artifact_fingerprint) REFERENCES vision_artifacts (project_id, vision_artifact_id, content_fingerprint)
);

-- table: vision_artifacts
CREATE TABLE vision_artifacts (
	vision_artifact_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	version_number INTEGER NOT NULL,
	components_json TEXT NOT NULL,
	statement TEXT NOT NULL,
	content_fingerprint VARCHAR NOT NULL,
	vision_evidence_snapshot_id INTEGER NOT NULL,
	component_basis_json TEXT NOT NULL,
	assumptions_json TEXT NOT NULL,
	conflicts_json TEXT NOT NULL,
	supersedes_vision_artifact_id INTEGER,
	source_interview_turn_id INTEGER NOT NULL,
	created_by VARCHAR NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (vision_artifact_id),
	CONSTRAINT uq_vision_artifact_project_id UNIQUE (project_id, vision_artifact_id),
	CONSTRAINT uq_vision_artifact_decision_parent UNIQUE (project_id, vision_artifact_id, content_fingerprint),
	CONSTRAINT uq_vision_artifact_version UNIQUE (project_id, version_number),
	CONSTRAINT uq_vision_artifact_fingerprint UNIQUE (project_id, content_fingerprint),
	CONSTRAINT fk_vision_artifact_supersedes FOREIGN KEY(project_id, supersedes_vision_artifact_id) REFERENCES vision_artifacts (project_id, vision_artifact_id),
	CONSTRAINT fk_vision_artifact_source_turn FOREIGN KEY(project_id, source_interview_turn_id) REFERENCES vision_interview_turns (project_id, vision_interview_turn_id),
	CONSTRAINT fk_vision_artifact_evidence_snapshot FOREIGN KEY(project_id, vision_evidence_snapshot_id) REFERENCES vision_evidence_snapshots (project_id, vision_evidence_snapshot_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: vision_evidence_snapshots
CREATE TABLE vision_evidence_snapshots (
	vision_evidence_snapshot_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	repository_binding_id INTEGER,
	supersedes_vision_evidence_snapshot_id INTEGER,
	workflow_node_attempt_id INTEGER NOT NULL,
	evidence_json TEXT NOT NULL,
	evidence_fingerprint VARCHAR NOT NULL,
	warnings_json TEXT NOT NULL,
	created_at DATETIME NOT NULL,
	PRIMARY KEY (vision_evidence_snapshot_id),
	CONSTRAINT uq_vision_evidence_snapshot_project_id UNIQUE (project_id, vision_evidence_snapshot_id),
	CONSTRAINT fk_vision_evidence_snapshot_attempt FOREIGN KEY(project_id, workflow_node_attempt_id) REFERENCES workflow_node_attempts (project_id, workflow_node_attempt_id),
	CONSTRAINT fk_vision_evidence_snapshot_repository_binding FOREIGN KEY(project_id, repository_binding_id) REFERENCES repository_bindings (project_id, repository_binding_id),
	CONSTRAINT fk_vision_evidence_snapshot_supersedes FOREIGN KEY(project_id, supersedes_vision_evidence_snapshot_id) REFERENCES vision_evidence_snapshots (project_id, vision_evidence_snapshot_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: vision_interview_turns
CREATE TABLE vision_interview_turns (
	vision_interview_turn_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	operation VARCHAR NOT NULL,
	turn_number INTEGER NOT NULL,
	revision_intent_id INTEGER,
	vision_evidence_snapshot_id INTEGER NOT NULL,
	prior_turn_id INTEGER,
	user_text TEXT,
	components_json TEXT NOT NULL,
	vision_statement TEXT NOT NULL,
	is_complete BOOLEAN NOT NULL,
	clarifying_questions_json TEXT NOT NULL,
	component_basis_json TEXT NOT NULL,
	assumptions_json TEXT NOT NULL,
	conflicts_json TEXT NOT NULL,
	output_fingerprint VARCHAR NOT NULL,
	workflow_node_attempt_id INTEGER NOT NULL,
	attempt_fingerprint VARCHAR NOT NULL,
	recorded_at DATETIME NOT NULL,
	PRIMARY KEY (vision_interview_turn_id),
	CONSTRAINT uq_vision_interview_turn_project_id UNIQUE (project_id, vision_interview_turn_id),
	CONSTRAINT uq_vision_interview_snapshot_turn_number UNIQUE (project_id, vision_evidence_snapshot_id, turn_number),
	CONSTRAINT ck_vision_interview_turn_operation CHECK (operation IN ('bootstrap', 'clarification', 'revision')),
	CONSTRAINT ck_vision_interview_turn_user_text_operation CHECK (((operation = 'bootstrap' AND user_text IS NULL) OR (operation IN ('clarification', 'revision') AND user_text IS NOT NULL))),
	CONSTRAINT fk_vision_interview_turn_revision_intent FOREIGN KEY(project_id, revision_intent_id) REFERENCES vision_revision_intents (project_id, vision_revision_intent_id),
	CONSTRAINT fk_vision_interview_turn_evidence_snapshot FOREIGN KEY(project_id, vision_evidence_snapshot_id) REFERENCES vision_evidence_snapshots (project_id, vision_evidence_snapshot_id),
	CONSTRAINT fk_vision_interview_turn_prior_turn FOREIGN KEY(project_id, prior_turn_id) REFERENCES vision_interview_turns (project_id, vision_interview_turn_id),
	CONSTRAINT fk_vision_interview_turn_attempt FOREIGN KEY(project_id, workflow_node_attempt_id) REFERENCES workflow_node_attempts (project_id, workflow_node_attempt_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: vision_revision_intents
CREATE TABLE vision_revision_intents (
	vision_revision_intent_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	source_vision_artifact_id INTEGER NOT NULL,
	source_vision_fingerprint VARCHAR NOT NULL,
	reason TEXT NOT NULL,
	initiated_by VARCHAR NOT NULL,
	initiated_at DATETIME NOT NULL,
	PRIMARY KEY (vision_revision_intent_id),
	CONSTRAINT uq_vision_revision_intent_project_id UNIQUE (project_id, vision_revision_intent_id),
	CONSTRAINT fk_vision_revision_intent_source_vision FOREIGN KEY(project_id, source_vision_artifact_id, source_vision_fingerprint) REFERENCES vision_artifacts (project_id, vision_artifact_id, content_fingerprint),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: workflow_events
CREATE TABLE workflow_events (
	event_id INTEGER NOT NULL,
	event_type VARCHAR(27) NOT NULL,
	timestamp DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	duration_seconds FLOAT,
	turn_count INTEGER,
	project_id INTEGER,
	sprint_id INTEGER,
	event_metadata TEXT,
	PRIMARY KEY (event_id),
	FOREIGN KEY(project_id) REFERENCES projects (project_id),
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

-- table: workflow_node_attempt_outcomes
CREATE TABLE workflow_node_attempt_outcomes (
	workflow_node_attempt_outcome_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	workflow_node_attempt_id INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	output_fingerprint VARCHAR,
	output_json TEXT,
	failure_code VARCHAR,
	failure_message TEXT,
	recorded_at DATETIME NOT NULL,
	PRIMARY KEY (workflow_node_attempt_outcome_id),
	CONSTRAINT uq_workflow_attempt_outcome UNIQUE (workflow_node_attempt_id),
	CONSTRAINT ck_workflow_attempt_outcome_status CHECK (status IN ('success', 'failure', 'obsolete')),
	CONSTRAINT ck_workflow_attempt_outcome_shape CHECK ((status = 'success' AND output_fingerprint IS NOT NULL AND output_json IS NOT NULL AND failure_code IS NULL AND failure_message IS NULL) OR (status = 'failure' AND output_fingerprint IS NULL AND output_json IS NULL AND failure_code IS NOT NULL AND failure_message IS NOT NULL) OR (status = 'obsolete' AND output_fingerprint IS NULL AND output_json IS NULL AND failure_code IS NULL AND failure_message IS NULL)),
	CONSTRAINT fk_workflow_attempt_outcome_attempt FOREIGN KEY(project_id, workflow_node_attempt_id) REFERENCES workflow_node_attempts (project_id, workflow_node_attempt_id)
);

-- table: workflow_node_attempts
CREATE TABLE workflow_node_attempts (
	workflow_node_attempt_id INTEGER NOT NULL,
	project_id INTEGER NOT NULL,
	node_id VARCHAR NOT NULL,
	instance_key VARCHAR,
	graph_version VARCHAR NOT NULL,
	fact_fingerprint VARCHAR NOT NULL,
	business_fact_fingerprint VARCHAR NOT NULL,
	decision_fingerprint VARCHAR NOT NULL,
	normalized_input_json TEXT NOT NULL,
	input_fingerprint VARCHAR NOT NULL,
	model_id VARCHAR NOT NULL,
	execution_settings_json TEXT NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	actor VARCHAR NOT NULL,
	correlation_id VARCHAR,
	started_at DATETIME NOT NULL,
	lease_expires_at DATETIME NOT NULL,
	attempt_fingerprint VARCHAR NOT NULL,
	PRIMARY KEY (workflow_node_attempt_id),
	CONSTRAINT uq_workflow_attempt_project_id UNIQUE (project_id, workflow_node_attempt_id),
	CONSTRAINT uq_workflow_attempt_identity UNIQUE (project_id, workflow_node_attempt_id, attempt_fingerprint),
	CONSTRAINT ck_workflow_attempt_lease CHECK (lease_expires_at > started_at),
	FOREIGN KEY(project_id) REFERENCES projects (project_id)
);

-- table: workflow_transition_receipts
CREATE TABLE workflow_transition_receipts (
	workflow_transition_receipt_id INTEGER NOT NULL,
	request_kind VARCHAR NOT NULL,
	idempotency_key VARCHAR NOT NULL,
	request_fingerprint VARCHAR NOT NULL,
	request_json TEXT NOT NULL,
	result_json TEXT,
	started_at DATETIME NOT NULL,
	completed_at DATETIME,
	PRIMARY KEY (workflow_transition_receipt_id),
	CONSTRAINT uq_workflow_transition_receipt UNIQUE (request_kind, idempotency_key)
);

-- index: ix_authority_curation_mutation_event_id
CREATE INDEX ix_authority_curation_mutation_event_id ON authority_curation_attempts (mutation_event_id);

-- index: ix_authority_curation_project_status
CREATE INDEX ix_authority_curation_project_status ON authority_curation_attempts (project_id, status);

-- index: ix_authority_curation_source_authority
CREATE INDEX ix_authority_curation_source_authority ON authority_curation_attempts (source_authority_id);

-- index: ix_authority_feedback_project_status
CREATE INDEX ix_authority_feedback_project_status ON authority_feedback_attempts (project_id, status);

-- index: ix_authority_feedback_source_authority
CREATE INDEX ix_authority_feedback_source_authority ON authority_feedback_attempts (source_authority_id);

-- index: ix_backlog_artifact_decisions_artifact_fingerprint
CREATE INDEX ix_backlog_artifact_decisions_artifact_fingerprint ON backlog_artifact_decisions (artifact_fingerprint);

-- index: ix_backlog_artifact_decisions_backlog_artifact_id
CREATE INDEX ix_backlog_artifact_decisions_backlog_artifact_id ON backlog_artifact_decisions (backlog_artifact_id);

-- index: ix_backlog_artifact_decisions_decision
CREATE INDEX ix_backlog_artifact_decisions_decision ON backlog_artifact_decisions (decision);

-- index: ix_backlog_artifact_decisions_idempotency_key
CREATE INDEX ix_backlog_artifact_decisions_idempotency_key ON backlog_artifact_decisions (idempotency_key);

-- index: ix_backlog_artifact_decisions_project_id
CREATE INDEX ix_backlog_artifact_decisions_project_id ON backlog_artifact_decisions (project_id);

-- index: ix_backlog_artifact_decisions_reviewer
CREATE INDEX ix_backlog_artifact_decisions_reviewer ON backlog_artifact_decisions (reviewer);

-- index: ix_backlog_artifacts_authority_fingerprint
CREATE INDEX ix_backlog_artifacts_authority_fingerprint ON backlog_artifacts (authority_fingerprint);

-- index: ix_backlog_artifacts_authority_id
CREATE INDEX ix_backlog_artifacts_authority_id ON backlog_artifacts (authority_id);

-- index: ix_backlog_artifacts_content_fingerprint
CREATE INDEX ix_backlog_artifacts_content_fingerprint ON backlog_artifacts (content_fingerprint);

-- index: ix_backlog_artifacts_created_by
CREATE INDEX ix_backlog_artifacts_created_by ON backlog_artifacts (created_by);

-- index: ix_backlog_artifacts_product_goal_artifact_id
CREATE INDEX ix_backlog_artifacts_product_goal_artifact_id ON backlog_artifacts (product_goal_artifact_id);

-- index: ix_backlog_artifacts_product_goal_fingerprint
CREATE INDEX ix_backlog_artifacts_product_goal_fingerprint ON backlog_artifacts (product_goal_fingerprint);

-- index: ix_backlog_artifacts_project_id
CREATE INDEX ix_backlog_artifacts_project_id ON backlog_artifacts (project_id);

-- index: ix_backlog_artifacts_supersedes_backlog_artifact_id
CREATE INDEX ix_backlog_artifacts_supersedes_backlog_artifact_id ON backlog_artifacts (supersedes_backlog_artifact_id);

-- index: ix_compiled_spec_authority_spec_version_id
CREATE INDEX ix_compiled_spec_authority_spec_version_id ON compiled_spec_authority (spec_version_id);

-- index: ix_post_sprint_triage_impact
CREATE INDEX ix_post_sprint_triage_impact ON post_sprint_triage (impact);

-- index: ix_post_sprint_triage_payload_fingerprint
CREATE INDEX ix_post_sprint_triage_payload_fingerprint ON post_sprint_triage (payload_fingerprint);

-- index: ix_post_sprint_triage_project_id
CREATE INDEX ix_post_sprint_triage_project_id ON post_sprint_triage (project_id);

-- index: ix_post_sprint_triage_recorded_by
CREATE INDEX ix_post_sprint_triage_recorded_by ON post_sprint_triage (recorded_by);

-- index: ix_post_sprint_triage_sprint_id
CREATE INDEX ix_post_sprint_triage_sprint_id ON post_sprint_triage (sprint_id);

-- index: ix_post_sprint_triage_supersedes_triage_id
CREATE INDEX ix_post_sprint_triage_supersedes_triage_id ON post_sprint_triage (supersedes_triage_id);

-- index: ix_product_goal_artifact_decisions_artifact_fingerprint
CREATE INDEX ix_product_goal_artifact_decisions_artifact_fingerprint ON product_goal_artifact_decisions (artifact_fingerprint);

-- index: ix_product_goal_artifact_decisions_decision
CREATE INDEX ix_product_goal_artifact_decisions_decision ON product_goal_artifact_decisions (decision);

-- index: ix_product_goal_artifact_decisions_idempotency_key
CREATE INDEX ix_product_goal_artifact_decisions_idempotency_key ON product_goal_artifact_decisions (idempotency_key);

-- index: ix_product_goal_artifact_decisions_product_goal_artifact_id
CREATE INDEX ix_product_goal_artifact_decisions_product_goal_artifact_id ON product_goal_artifact_decisions (product_goal_artifact_id);

-- index: ix_product_goal_artifact_decisions_project_id
CREATE INDEX ix_product_goal_artifact_decisions_project_id ON product_goal_artifact_decisions (project_id);

-- index: ix_product_goal_artifact_decisions_reviewer
CREATE INDEX ix_product_goal_artifact_decisions_reviewer ON product_goal_artifact_decisions (reviewer);

-- index: ix_product_goal_artifacts_content_fingerprint
CREATE INDEX ix_product_goal_artifacts_content_fingerprint ON product_goal_artifacts (content_fingerprint);

-- index: ix_product_goal_artifacts_created_by
CREATE INDEX ix_product_goal_artifacts_created_by ON product_goal_artifacts (created_by);

-- index: ix_product_goal_artifacts_project_id
CREATE INDEX ix_product_goal_artifacts_project_id ON product_goal_artifacts (project_id);

-- index: ix_product_goal_artifacts_source_interview_turn_id
CREATE INDEX ix_product_goal_artifacts_source_interview_turn_id ON product_goal_artifacts (source_interview_turn_id);

-- index: ix_product_goal_artifacts_supersedes_product_goal_artifact_id
CREATE INDEX ix_product_goal_artifacts_supersedes_product_goal_artifact_id ON product_goal_artifacts (supersedes_product_goal_artifact_id);

-- index: ix_product_goal_artifacts_vision_artifact_id
CREATE INDEX ix_product_goal_artifacts_vision_artifact_id ON product_goal_artifacts (vision_artifact_id);

-- index: ix_product_goal_artifacts_vision_fingerprint
CREATE INDEX ix_product_goal_artifacts_vision_fingerprint ON product_goal_artifacts (vision_fingerprint);

-- index: ix_product_goal_interview_turns_attempt_fingerprint
CREATE INDEX ix_product_goal_interview_turns_attempt_fingerprint ON product_goal_interview_turns (attempt_fingerprint);

-- index: ix_product_goal_interview_turns_output_fingerprint
CREATE INDEX ix_product_goal_interview_turns_output_fingerprint ON product_goal_interview_turns (output_fingerprint);

-- index: ix_product_goal_interview_turns_prior_turn_id
CREATE INDEX ix_product_goal_interview_turns_prior_turn_id ON product_goal_interview_turns (prior_turn_id);

-- index: ix_product_goal_interview_turns_project_id
CREATE INDEX ix_product_goal_interview_turns_project_id ON product_goal_interview_turns (project_id);

-- index: ix_product_goal_interview_turns_vision_artifact_id
CREATE INDEX ix_product_goal_interview_turns_vision_artifact_id ON product_goal_interview_turns (vision_artifact_id);

-- index: ix_product_goal_interview_turns_vision_fingerprint
CREATE INDEX ix_product_goal_interview_turns_vision_fingerprint ON product_goal_interview_turns (vision_fingerprint);

-- index: ix_product_goal_interview_turns_workflow_node_attempt_id
CREATE INDEX ix_product_goal_interview_turns_workflow_node_attempt_id ON product_goal_interview_turns (workflow_node_attempt_id);

-- index: ix_product_goal_outcomes_artifact_fingerprint
CREATE INDEX ix_product_goal_outcomes_artifact_fingerprint ON product_goal_outcomes (artifact_fingerprint);

-- index: ix_product_goal_outcomes_decided_by
CREATE INDEX ix_product_goal_outcomes_decided_by ON product_goal_outcomes (decided_by);

-- index: ix_product_goal_outcomes_idempotency_key
CREATE INDEX ix_product_goal_outcomes_idempotency_key ON product_goal_outcomes (idempotency_key);

-- index: ix_product_goal_outcomes_outcome
CREATE INDEX ix_product_goal_outcomes_outcome ON product_goal_outcomes (outcome);

-- index: ix_product_goal_outcomes_product_goal_artifact_id
CREATE INDEX ix_product_goal_outcomes_product_goal_artifact_id ON product_goal_outcomes (product_goal_artifact_id);

-- index: ix_product_goal_outcomes_project_id
CREATE INDEX ix_product_goal_outcomes_project_id ON product_goal_outcomes (project_id);

-- index: ix_projects_active_repository_binding_id
CREATE INDEX ix_projects_active_repository_binding_id ON projects (active_repository_binding_id);

-- index: ix_projects_name
CREATE UNIQUE INDEX ix_projects_name ON projects (name);

-- index: ix_repository_bindings_head_sha
CREATE INDEX ix_repository_bindings_head_sha ON repository_bindings (head_sha);

-- index: ix_repository_bindings_project_id
CREATE INDEX ix_repository_bindings_project_id ON repository_bindings (project_id);

-- index: ix_repository_bindings_recorded_by
CREATE INDEX ix_repository_bindings_recorded_by ON repository_bindings (recorded_by);

-- index: ix_repository_bindings_status_fingerprint
CREATE INDEX ix_repository_bindings_status_fingerprint ON repository_bindings (status_fingerprint);

-- index: ix_repository_bindings_supersedes_repository_binding_id
CREATE INDEX ix_repository_bindings_supersedes_repository_binding_id ON repository_bindings (supersedes_repository_binding_id);

-- index: ix_roadmap_artifact_decisions_artifact_fingerprint
CREATE INDEX ix_roadmap_artifact_decisions_artifact_fingerprint ON roadmap_artifact_decisions (artifact_fingerprint);

-- index: ix_roadmap_artifact_decisions_decision
CREATE INDEX ix_roadmap_artifact_decisions_decision ON roadmap_artifact_decisions (decision);

-- index: ix_roadmap_artifact_decisions_idempotency_key
CREATE INDEX ix_roadmap_artifact_decisions_idempotency_key ON roadmap_artifact_decisions (idempotency_key);

-- index: ix_roadmap_artifact_decisions_project_id
CREATE INDEX ix_roadmap_artifact_decisions_project_id ON roadmap_artifact_decisions (project_id);

-- index: ix_roadmap_artifact_decisions_reviewer
CREATE INDEX ix_roadmap_artifact_decisions_reviewer ON roadmap_artifact_decisions (reviewer);

-- index: ix_roadmap_artifact_decisions_roadmap_artifact_id
CREATE INDEX ix_roadmap_artifact_decisions_roadmap_artifact_id ON roadmap_artifact_decisions (roadmap_artifact_id);

-- index: ix_roadmap_artifacts_backlog_artifact_fingerprint
CREATE INDEX ix_roadmap_artifacts_backlog_artifact_fingerprint ON roadmap_artifacts (backlog_artifact_fingerprint);

-- index: ix_roadmap_artifacts_backlog_artifact_id
CREATE INDEX ix_roadmap_artifacts_backlog_artifact_id ON roadmap_artifacts (backlog_artifact_id);

-- index: ix_roadmap_artifacts_content_fingerprint
CREATE INDEX ix_roadmap_artifacts_content_fingerprint ON roadmap_artifacts (content_fingerprint);

-- index: ix_roadmap_artifacts_created_by
CREATE INDEX ix_roadmap_artifacts_created_by ON roadmap_artifacts (created_by);

-- index: ix_roadmap_artifacts_project_id
CREATE INDEX ix_roadmap_artifacts_project_id ON roadmap_artifacts (project_id);

-- index: ix_roadmap_artifacts_supersedes_roadmap_artifact_id
CREATE INDEX ix_roadmap_artifacts_supersedes_roadmap_artifact_id ON roadmap_artifacts (supersedes_roadmap_artifact_id);

-- index: ix_spec_authority_acceptance_authority_fingerprint
CREATE INDEX ix_spec_authority_acceptance_authority_fingerprint ON spec_authority_acceptance (authority_fingerprint);

-- index: ix_spec_authority_acceptance_pending_authority_id
CREATE INDEX ix_spec_authority_acceptance_pending_authority_id ON spec_authority_acceptance (pending_authority_id);

-- index: ix_spec_authority_acceptance_project_id
CREATE INDEX ix_spec_authority_acceptance_project_id ON spec_authority_acceptance (project_id);

-- index: ix_spec_authority_acceptance_review_token
CREATE INDEX ix_spec_authority_acceptance_review_token ON spec_authority_acceptance (review_token);

-- index: ix_spec_authority_acceptance_spec_version_id
CREATE INDEX ix_spec_authority_acceptance_spec_version_id ON spec_authority_acceptance (spec_version_id);

-- index: ix_spec_authority_acceptance_terminal_decision_key
CREATE INDEX ix_spec_authority_acceptance_terminal_decision_key ON spec_authority_acceptance (terminal_decision_key);

-- index: ix_spec_registry_project_id
CREATE INDEX ix_spec_registry_project_id ON spec_registry (project_id);

-- index: ix_spec_registry_source_product_goal_artifact_id
CREATE INDEX ix_spec_registry_source_product_goal_artifact_id ON spec_registry (source_product_goal_artifact_id);

-- index: ix_spec_registry_source_product_goal_fingerprint
CREATE INDEX ix_spec_registry_source_product_goal_fingerprint ON spec_registry (source_product_goal_fingerprint);

-- index: ix_spec_registry_source_specification_candidate_fingerprint
CREATE INDEX ix_spec_registry_source_specification_candidate_fingerprint ON spec_registry (source_specification_candidate_fingerprint);

-- index: ix_spec_registry_source_specification_candidate_id
CREATE INDEX ix_spec_registry_source_specification_candidate_id ON spec_registry (source_specification_candidate_id);

-- index: ix_spec_registry_source_vision_artifact_id
CREATE INDEX ix_spec_registry_source_vision_artifact_id ON spec_registry (source_vision_artifact_id);

-- index: ix_spec_registry_source_vision_fingerprint
CREATE INDEX ix_spec_registry_source_vision_fingerprint ON spec_registry (source_vision_fingerprint);

-- index: ix_spec_registry_supersedes_spec_version_id
CREATE INDEX ix_spec_registry_supersedes_spec_version_id ON spec_registry (supersedes_spec_version_id);

-- index: ix_specification_candidates_attempt_fingerprint
CREATE INDEX ix_specification_candidates_attempt_fingerprint ON specification_candidates (attempt_fingerprint);

-- index: ix_specification_candidates_base_spec_hash
CREATE INDEX ix_specification_candidates_base_spec_hash ON specification_candidates (base_spec_hash);

-- index: ix_specification_candidates_base_spec_version_id
CREATE INDEX ix_specification_candidates_base_spec_version_id ON specification_candidates (base_spec_version_id);

-- index: ix_specification_candidates_candidate_fingerprint
CREATE INDEX ix_specification_candidates_candidate_fingerprint ON specification_candidates (candidate_fingerprint);

-- index: ix_specification_candidates_candidate_kind
CREATE INDEX ix_specification_candidates_candidate_kind ON specification_candidates (candidate_kind);

-- index: ix_specification_candidates_payload_fingerprint
CREATE INDEX ix_specification_candidates_payload_fingerprint ON specification_candidates (payload_fingerprint);

-- index: ix_specification_candidates_producer_input_fingerprint
CREATE INDEX ix_specification_candidates_producer_input_fingerprint ON specification_candidates (producer_input_fingerprint);

-- index: ix_specification_candidates_product_goal_artifact_id
CREATE INDEX ix_specification_candidates_product_goal_artifact_id ON specification_candidates (product_goal_artifact_id);

-- index: ix_specification_candidates_product_goal_fingerprint
CREATE INDEX ix_specification_candidates_product_goal_fingerprint ON specification_candidates (product_goal_fingerprint);

-- index: ix_specification_candidates_project_id
CREATE INDEX ix_specification_candidates_project_id ON specification_candidates (project_id);

-- index: ix_specification_candidates_recorded_by
CREATE INDEX ix_specification_candidates_recorded_by ON specification_candidates (recorded_by);

-- index: ix_specification_candidates_rendered_view_fingerprint
CREATE INDEX ix_specification_candidates_rendered_view_fingerprint ON specification_candidates (rendered_view_fingerprint);

-- index: ix_specification_candidates_source_manifest_fingerprint
CREATE INDEX ix_specification_candidates_source_manifest_fingerprint ON specification_candidates (source_manifest_fingerprint);

-- index: ix_specification_candidates_specification_source_fingerprint
CREATE INDEX ix_specification_candidates_specification_source_fingerprint ON specification_candidates (specification_source_fingerprint);

-- index: ix_specification_candidates_specification_source_id
CREATE INDEX ix_specification_candidates_specification_source_id ON specification_candidates (specification_source_id);

-- index: ix_specification_candidates_supersedes_candidate_fingerprint
CREATE INDEX ix_specification_candidates_supersedes_candidate_fingerprint ON specification_candidates (supersedes_candidate_fingerprint);

-- index: ix_specification_candidates_supersedes_specification_candidate_id
CREATE INDEX ix_specification_candidates_supersedes_specification_candidate_id ON specification_candidates (supersedes_specification_candidate_id);

-- index: ix_specification_candidates_vision_artifact_id
CREATE INDEX ix_specification_candidates_vision_artifact_id ON specification_candidates (vision_artifact_id);

-- index: ix_specification_candidates_vision_fingerprint
CREATE INDEX ix_specification_candidates_vision_fingerprint ON specification_candidates (vision_fingerprint);

-- index: ix_specification_candidates_workflow_node_attempt_id
CREATE INDEX ix_specification_candidates_workflow_node_attempt_id ON specification_candidates (workflow_node_attempt_id);

-- index: ix_specification_decisions_candidate_fingerprint
CREATE INDEX ix_specification_decisions_candidate_fingerprint ON specification_decisions (candidate_fingerprint);

-- index: ix_specification_decisions_decision
CREATE INDEX ix_specification_decisions_decision ON specification_decisions (decision);

-- index: ix_specification_decisions_idempotency_key
CREATE INDEX ix_specification_decisions_idempotency_key ON specification_decisions (idempotency_key);

-- index: ix_specification_decisions_project_id
CREATE INDEX ix_specification_decisions_project_id ON specification_decisions (project_id);

-- index: ix_specification_decisions_reviewer
CREATE INDEX ix_specification_decisions_reviewer ON specification_decisions (reviewer);

-- index: ix_specification_decisions_specification_candidate_id
CREATE INDEX ix_specification_decisions_specification_candidate_id ON specification_decisions (specification_candidate_id);

-- index: ix_specification_sources_product_goal_artifact_id
CREATE INDEX ix_specification_sources_product_goal_artifact_id ON specification_sources (product_goal_artifact_id);

-- index: ix_specification_sources_product_goal_fingerprint
CREATE INDEX ix_specification_sources_product_goal_fingerprint ON specification_sources (product_goal_fingerprint);

-- index: ix_specification_sources_project_id
CREATE INDEX ix_specification_sources_project_id ON specification_sources (project_id);

-- index: ix_specification_sources_registered_by
CREATE INDEX ix_specification_sources_registered_by ON specification_sources (registered_by);

-- index: ix_specification_sources_repository_binding_id
CREATE INDEX ix_specification_sources_repository_binding_id ON specification_sources (repository_binding_id);

-- index: ix_specification_sources_repository_head_sha
CREATE INDEX ix_specification_sources_repository_head_sha ON specification_sources (repository_head_sha);

-- index: ix_specification_sources_repository_status_fingerprint
CREATE INDEX ix_specification_sources_repository_status_fingerprint ON specification_sources (repository_status_fingerprint);

-- index: ix_specification_sources_source_fingerprint
CREATE INDEX ix_specification_sources_source_fingerprint ON specification_sources (source_fingerprint);

-- index: ix_specification_sources_supersedes_source_fingerprint
CREATE INDEX ix_specification_sources_supersedes_source_fingerprint ON specification_sources (supersedes_source_fingerprint);

-- index: ix_specification_sources_supersedes_specification_source_id
CREATE INDEX ix_specification_sources_supersedes_specification_source_id ON specification_sources (supersedes_specification_source_id);

-- index: ix_specification_sources_vision_artifact_id
CREATE INDEX ix_specification_sources_vision_artifact_id ON specification_sources (vision_artifact_id);

-- index: ix_specification_sources_vision_fingerprint
CREATE INDEX ix_specification_sources_vision_fingerprint ON specification_sources (vision_fingerprint);

-- index: ix_sprint_closures_close_fingerprint
CREATE INDEX ix_sprint_closures_close_fingerprint ON sprint_closures (close_fingerprint);

-- index: ix_sprint_closures_closed_by
CREATE INDEX ix_sprint_closures_closed_by ON sprint_closures (closed_by);

-- index: ix_sprint_closures_project_id
CREATE INDEX ix_sprint_closures_project_id ON sprint_closures (project_id);

-- index: ix_sprint_closures_review_fingerprint
CREATE INDEX ix_sprint_closures_review_fingerprint ON sprint_closures (review_fingerprint);

-- index: ix_sprint_closures_sprint_id
CREATE INDEX ix_sprint_closures_sprint_id ON sprint_closures (sprint_id);

-- index: ix_sprint_plan_artifact_decisions_decision
CREATE INDEX ix_sprint_plan_artifact_decisions_decision ON sprint_plan_artifact_decisions (decision);

-- index: ix_sprint_plan_artifact_decisions_idempotency_key
CREATE INDEX ix_sprint_plan_artifact_decisions_idempotency_key ON sprint_plan_artifact_decisions (idempotency_key);

-- index: ix_sprint_plan_artifact_decisions_plan_fingerprint
CREATE INDEX ix_sprint_plan_artifact_decisions_plan_fingerprint ON sprint_plan_artifact_decisions (plan_fingerprint);

-- index: ix_sprint_plan_artifact_decisions_project_id
CREATE INDEX ix_sprint_plan_artifact_decisions_project_id ON sprint_plan_artifact_decisions (project_id);

-- index: ix_sprint_plan_artifact_decisions_reviewer
CREATE INDEX ix_sprint_plan_artifact_decisions_reviewer ON sprint_plan_artifact_decisions (reviewer);

-- index: ix_sprint_plan_artifact_decisions_sprint_plan_artifact_id
CREATE INDEX ix_sprint_plan_artifact_decisions_sprint_plan_artifact_id ON sprint_plan_artifact_decisions (sprint_plan_artifact_id);

-- index: ix_sprint_plan_artifacts_candidate_set_fingerprint
CREATE INDEX ix_sprint_plan_artifacts_candidate_set_fingerprint ON sprint_plan_artifacts (candidate_set_fingerprint);

-- index: ix_sprint_plan_artifacts_created_by
CREATE INDEX ix_sprint_plan_artifacts_created_by ON sprint_plan_artifacts (created_by);

-- index: ix_sprint_plan_artifacts_plan_fingerprint
CREATE INDEX ix_sprint_plan_artifacts_plan_fingerprint ON sprint_plan_artifacts (plan_fingerprint);

-- index: ix_sprint_plan_artifacts_project_id
CREATE INDEX ix_sprint_plan_artifacts_project_id ON sprint_plan_artifacts (project_id);

-- index: ix_sprint_plan_artifacts_sprint_id
CREATE INDEX ix_sprint_plan_artifacts_sprint_id ON sprint_plan_artifacts (sprint_id);

-- index: ix_sprint_plan_artifacts_supersedes_sprint_plan_artifact_id
CREATE INDEX ix_sprint_plan_artifacts_supersedes_sprint_plan_artifact_id ON sprint_plan_artifacts (supersedes_sprint_plan_artifact_id);

-- index: ix_sprint_reviews_project_id
CREATE INDEX ix_sprint_reviews_project_id ON sprint_reviews (project_id);

-- index: ix_sprint_reviews_review_fingerprint
CREATE INDEX ix_sprint_reviews_review_fingerprint ON sprint_reviews (review_fingerprint);

-- index: ix_sprint_reviews_reviewed_by
CREATE INDEX ix_sprint_reviews_reviewed_by ON sprint_reviews (reviewed_by);

-- index: ix_sprint_reviews_sprint_id
CREATE INDEX ix_sprint_reviews_sprint_id ON sprint_reviews (sprint_id);

-- index: ix_sprint_starts_audit_event_id
CREATE INDEX ix_sprint_starts_audit_event_id ON sprint_starts (audit_event_id);

-- index: ix_sprint_starts_candidate_set_fingerprint
CREATE INDEX ix_sprint_starts_candidate_set_fingerprint ON sprint_starts (candidate_set_fingerprint);

-- index: ix_sprint_starts_decision_fingerprint
CREATE INDEX ix_sprint_starts_decision_fingerprint ON sprint_starts (decision_fingerprint);

-- index: ix_sprint_starts_dependency_fingerprint
CREATE INDEX ix_sprint_starts_dependency_fingerprint ON sprint_starts (dependency_fingerprint);

-- index: ix_sprint_starts_dependency_rows_fingerprint
CREATE INDEX ix_sprint_starts_dependency_rows_fingerprint ON sprint_starts (dependency_rows_fingerprint);

-- index: ix_sprint_starts_dependency_source_fingerprint
CREATE INDEX ix_sprint_starts_dependency_source_fingerprint ON sprint_starts (dependency_source_fingerprint);

-- index: ix_sprint_starts_plan_fingerprint
CREATE INDEX ix_sprint_starts_plan_fingerprint ON sprint_starts (plan_fingerprint);

-- index: ix_sprint_starts_project_id
CREATE INDEX ix_sprint_starts_project_id ON sprint_starts (project_id);

-- index: ix_sprint_starts_sprint_id
CREATE INDEX ix_sprint_starts_sprint_id ON sprint_starts (sprint_id);

-- index: ix_sprint_starts_sprint_plan_artifact_decision_id
CREATE INDEX ix_sprint_starts_sprint_plan_artifact_decision_id ON sprint_starts (sprint_plan_artifact_decision_id);

-- index: ix_sprint_starts_sprint_plan_artifact_id
CREATE INDEX ix_sprint_starts_sprint_plan_artifact_id ON sprint_starts (sprint_plan_artifact_id);

-- index: ix_sprint_starts_started_by
CREATE INDEX ix_sprint_starts_started_by ON sprint_starts (started_by);

-- index: ix_sprint_starts_story_dependency_review_id
CREATE INDEX ix_sprint_starts_story_dependency_review_id ON sprint_starts (story_dependency_review_id);

-- index: ix_sprint_starts_task_content_fingerprint
CREATE INDEX ix_sprint_starts_task_content_fingerprint ON sprint_starts (task_content_fingerprint);

-- index: ix_story_artifact_decisions_artifact_fingerprint
CREATE INDEX ix_story_artifact_decisions_artifact_fingerprint ON story_artifact_decisions (artifact_fingerprint);

-- index: ix_story_artifact_decisions_decision
CREATE INDEX ix_story_artifact_decisions_decision ON story_artifact_decisions (decision);

-- index: ix_story_artifact_decisions_idempotency_key
CREATE INDEX ix_story_artifact_decisions_idempotency_key ON story_artifact_decisions (idempotency_key);

-- index: ix_story_artifact_decisions_project_id
CREATE INDEX ix_story_artifact_decisions_project_id ON story_artifact_decisions (project_id);

-- index: ix_story_artifact_decisions_reviewer
CREATE INDEX ix_story_artifact_decisions_reviewer ON story_artifact_decisions (reviewer);

-- index: ix_story_artifact_decisions_story_artifact_id
CREATE INDEX ix_story_artifact_decisions_story_artifact_id ON story_artifact_decisions (story_artifact_id);

-- index: ix_story_artifacts_content_fingerprint
CREATE INDEX ix_story_artifacts_content_fingerprint ON story_artifacts (content_fingerprint);

-- index: ix_story_artifacts_created_by
CREATE INDEX ix_story_artifacts_created_by ON story_artifacts (created_by);

-- index: ix_story_artifacts_project_id
CREATE INDEX ix_story_artifacts_project_id ON story_artifacts (project_id);

-- index: ix_story_artifacts_requirement_id
CREATE INDEX ix_story_artifacts_requirement_id ON story_artifacts (requirement_id);

-- index: ix_story_artifacts_roadmap_artifact_fingerprint
CREATE INDEX ix_story_artifacts_roadmap_artifact_fingerprint ON story_artifacts (roadmap_artifact_fingerprint);

-- index: ix_story_artifacts_roadmap_artifact_id
CREATE INDEX ix_story_artifacts_roadmap_artifact_id ON story_artifacts (roadmap_artifact_id);

-- index: ix_story_artifacts_supersedes_story_artifact_id
CREATE INDEX ix_story_artifacts_supersedes_story_artifact_id ON story_artifacts (supersedes_story_artifact_id);

-- index: ix_story_closures_closed_by
CREATE INDEX ix_story_closures_closed_by ON story_closures (closed_by);

-- index: ix_story_closures_completion_fingerprint
CREATE INDEX ix_story_closures_completion_fingerprint ON story_closures (completion_fingerprint);

-- index: ix_story_closures_project_id
CREATE INDEX ix_story_closures_project_id ON story_closures (project_id);

-- index: ix_story_closures_sprint_id
CREATE INDEX ix_story_closures_sprint_id ON story_closures (sprint_id);

-- index: ix_story_closures_story_id
CREATE INDEX ix_story_closures_story_id ON story_closures (story_id);

-- index: ix_story_completion_logs_story_id
CREATE INDEX ix_story_completion_logs_story_id ON story_completion_logs (story_id);

-- index: ix_story_dependency_reviews_dependency_fingerprint
CREATE INDEX ix_story_dependency_reviews_dependency_fingerprint ON story_dependency_reviews (dependency_fingerprint);

-- index: ix_story_dependency_reviews_project_id
CREATE INDEX ix_story_dependency_reviews_project_id ON story_dependency_reviews (project_id);

-- index: ix_story_dependency_reviews_reviewed_by
CREATE INDEX ix_story_dependency_reviews_reviewed_by ON story_dependency_reviews (reviewed_by);

-- index: ix_story_dependency_reviews_source_fingerprint
CREATE INDEX ix_story_dependency_reviews_source_fingerprint ON story_dependency_reviews (source_fingerprint);

-- index: ix_task_completion_evidence_acceptance_result
CREATE INDEX ix_task_completion_evidence_acceptance_result ON task_completion_evidence (acceptance_result);

-- index: ix_task_completion_evidence_completed_by
CREATE INDEX ix_task_completion_evidence_completed_by ON task_completion_evidence (completed_by);

-- index: ix_task_completion_evidence_evidence_fingerprint
CREATE INDEX ix_task_completion_evidence_evidence_fingerprint ON task_completion_evidence (evidence_fingerprint);

-- index: ix_task_completion_evidence_project_id
CREATE INDEX ix_task_completion_evidence_project_id ON task_completion_evidence (project_id);

-- index: ix_task_completion_evidence_sprint_id
CREATE INDEX ix_task_completion_evidence_sprint_id ON task_completion_evidence (sprint_id);

-- index: ix_task_completion_evidence_task_id
CREATE INDEX ix_task_completion_evidence_task_id ON task_completion_evidence (task_id);

-- index: ix_task_execution_logs_sprint_id
CREATE INDEX ix_task_execution_logs_sprint_id ON task_execution_logs (sprint_id);

-- index: ix_task_execution_logs_task_id
CREATE INDEX ix_task_execution_logs_task_id ON task_execution_logs (task_id);

-- index: ix_team_members_email
CREATE UNIQUE INDEX ix_team_members_email ON team_members (email);

-- index: ix_teams_name
CREATE UNIQUE INDEX ix_teams_name ON teams (name);

-- index: ix_user_stories_archive_reset_attempt_id
CREATE INDEX ix_user_stories_archive_reset_attempt_id ON user_stories (archive_reset_attempt_id);

-- index: ix_user_stories_archived_reason
CREATE INDEX ix_user_stories_archived_reason ON user_stories (archived_reason);

-- index: ix_user_stories_persona
CREATE INDEX ix_user_stories_persona ON user_stories (persona);

-- index: ix_user_stories_project_id
CREATE INDEX ix_user_stories_project_id ON user_stories (project_id);

-- index: ix_user_stories_rank
CREATE INDEX ix_user_stories_rank ON user_stories (rank);

-- index: ix_user_stories_refinement_slot
CREATE INDEX ix_user_stories_refinement_slot ON user_stories (refinement_slot);

-- index: ix_user_stories_source_requirement
CREATE INDEX ix_user_stories_source_requirement ON user_stories (source_requirement);

-- index: ix_user_story_dependencies_confidence
CREATE INDEX ix_user_story_dependencies_confidence ON user_story_dependencies (confidence);

-- index: ix_user_story_dependencies_dependent_story_id
CREATE INDEX ix_user_story_dependencies_dependent_story_id ON user_story_dependencies (dependent_story_id);

-- index: ix_user_story_dependencies_prerequisite_story_id
CREATE INDEX ix_user_story_dependencies_prerequisite_story_id ON user_story_dependencies (prerequisite_story_id);

-- index: ix_user_story_dependencies_project_id
CREATE INDEX ix_user_story_dependencies_project_id ON user_story_dependencies (project_id);

-- index: ix_user_story_dependencies_source
CREATE INDEX ix_user_story_dependencies_source ON user_story_dependencies (source);

-- index: ix_user_story_dependencies_status
CREATE INDEX ix_user_story_dependencies_status ON user_story_dependencies (status);

-- index: ix_vision_artifact_decisions_artifact_fingerprint
CREATE INDEX ix_vision_artifact_decisions_artifact_fingerprint ON vision_artifact_decisions (artifact_fingerprint);

-- index: ix_vision_artifact_decisions_decision
CREATE INDEX ix_vision_artifact_decisions_decision ON vision_artifact_decisions (decision);

-- index: ix_vision_artifact_decisions_idempotency_key
CREATE INDEX ix_vision_artifact_decisions_idempotency_key ON vision_artifact_decisions (idempotency_key);

-- index: ix_vision_artifact_decisions_project_id
CREATE INDEX ix_vision_artifact_decisions_project_id ON vision_artifact_decisions (project_id);

-- index: ix_vision_artifact_decisions_reviewer
CREATE INDEX ix_vision_artifact_decisions_reviewer ON vision_artifact_decisions (reviewer);

-- index: ix_vision_artifact_decisions_vision_artifact_id
CREATE INDEX ix_vision_artifact_decisions_vision_artifact_id ON vision_artifact_decisions (vision_artifact_id);

-- index: ix_vision_artifacts_content_fingerprint
CREATE INDEX ix_vision_artifacts_content_fingerprint ON vision_artifacts (content_fingerprint);

-- index: ix_vision_artifacts_created_by
CREATE INDEX ix_vision_artifacts_created_by ON vision_artifacts (created_by);

-- index: ix_vision_artifacts_project_id
CREATE INDEX ix_vision_artifacts_project_id ON vision_artifacts (project_id);

-- index: ix_vision_artifacts_source_interview_turn_id
CREATE INDEX ix_vision_artifacts_source_interview_turn_id ON vision_artifacts (source_interview_turn_id);

-- index: ix_vision_artifacts_supersedes_vision_artifact_id
CREATE INDEX ix_vision_artifacts_supersedes_vision_artifact_id ON vision_artifacts (supersedes_vision_artifact_id);

-- index: ix_vision_artifacts_vision_evidence_snapshot_id
CREATE INDEX ix_vision_artifacts_vision_evidence_snapshot_id ON vision_artifacts (vision_evidence_snapshot_id);

-- index: ix_vision_evidence_snapshots_evidence_fingerprint
CREATE INDEX ix_vision_evidence_snapshots_evidence_fingerprint ON vision_evidence_snapshots (evidence_fingerprint);

-- index: ix_vision_evidence_snapshots_project_id
CREATE INDEX ix_vision_evidence_snapshots_project_id ON vision_evidence_snapshots (project_id);

-- index: ix_vision_evidence_snapshots_repository_binding_id
CREATE INDEX ix_vision_evidence_snapshots_repository_binding_id ON vision_evidence_snapshots (repository_binding_id);

-- index: ix_vision_evidence_snapshots_supersedes_vision_evidence_snapshot_id
CREATE INDEX ix_vision_evidence_snapshots_supersedes_vision_evidence_snapshot_id ON vision_evidence_snapshots (supersedes_vision_evidence_snapshot_id);

-- index: ix_vision_evidence_snapshots_workflow_node_attempt_id
CREATE INDEX ix_vision_evidence_snapshots_workflow_node_attempt_id ON vision_evidence_snapshots (workflow_node_attempt_id);

-- index: ix_vision_interview_turns_attempt_fingerprint
CREATE INDEX ix_vision_interview_turns_attempt_fingerprint ON vision_interview_turns (attempt_fingerprint);

-- index: ix_vision_interview_turns_operation
CREATE INDEX ix_vision_interview_turns_operation ON vision_interview_turns (operation);

-- index: ix_vision_interview_turns_output_fingerprint
CREATE INDEX ix_vision_interview_turns_output_fingerprint ON vision_interview_turns (output_fingerprint);

-- index: ix_vision_interview_turns_prior_turn_id
CREATE INDEX ix_vision_interview_turns_prior_turn_id ON vision_interview_turns (prior_turn_id);

-- index: ix_vision_interview_turns_project_id
CREATE INDEX ix_vision_interview_turns_project_id ON vision_interview_turns (project_id);

-- index: ix_vision_interview_turns_revision_intent_id
CREATE INDEX ix_vision_interview_turns_revision_intent_id ON vision_interview_turns (revision_intent_id);

-- index: ix_vision_interview_turns_vision_evidence_snapshot_id
CREATE INDEX ix_vision_interview_turns_vision_evidence_snapshot_id ON vision_interview_turns (vision_evidence_snapshot_id);

-- index: ix_vision_interview_turns_workflow_node_attempt_id
CREATE INDEX ix_vision_interview_turns_workflow_node_attempt_id ON vision_interview_turns (workflow_node_attempt_id);

-- index: ix_vision_revision_intents_initiated_by
CREATE INDEX ix_vision_revision_intents_initiated_by ON vision_revision_intents (initiated_by);

-- index: ix_vision_revision_intents_project_id
CREATE INDEX ix_vision_revision_intents_project_id ON vision_revision_intents (project_id);

-- index: ix_vision_revision_intents_source_vision_artifact_id
CREATE INDEX ix_vision_revision_intents_source_vision_artifact_id ON vision_revision_intents (source_vision_artifact_id);

-- index: ix_vision_revision_intents_source_vision_fingerprint
CREATE INDEX ix_vision_revision_intents_source_vision_fingerprint ON vision_revision_intents (source_vision_fingerprint);

-- index: ix_workflow_events_event_type
CREATE INDEX ix_workflow_events_event_type ON workflow_events (event_type);

-- index: ix_workflow_node_attempt_outcomes_project_id
CREATE INDEX ix_workflow_node_attempt_outcomes_project_id ON workflow_node_attempt_outcomes (project_id);

-- index: ix_workflow_node_attempt_outcomes_status
CREATE INDEX ix_workflow_node_attempt_outcomes_status ON workflow_node_attempt_outcomes (status);

-- index: ix_workflow_node_attempt_outcomes_workflow_node_attempt_id
CREATE INDEX ix_workflow_node_attempt_outcomes_workflow_node_attempt_id ON workflow_node_attempt_outcomes (workflow_node_attempt_id);

-- index: ix_workflow_node_attempts_attempt_fingerprint
CREATE INDEX ix_workflow_node_attempts_attempt_fingerprint ON workflow_node_attempts (attempt_fingerprint);

-- index: ix_workflow_node_attempts_idempotency_key
CREATE INDEX ix_workflow_node_attempts_idempotency_key ON workflow_node_attempts (idempotency_key);

-- index: ix_workflow_node_attempts_instance_key
CREATE INDEX ix_workflow_node_attempts_instance_key ON workflow_node_attempts (instance_key);

-- index: ix_workflow_node_attempts_node_id
CREATE INDEX ix_workflow_node_attempts_node_id ON workflow_node_attempts (node_id);

-- index: ix_workflow_node_attempts_project_id
CREATE INDEX ix_workflow_node_attempts_project_id ON workflow_node_attempts (project_id);

-- index: ix_workflow_transition_receipts_idempotency_key
CREATE INDEX ix_workflow_transition_receipts_idempotency_key ON workflow_transition_receipts (idempotency_key);

-- index: ix_workflow_transition_receipts_request_kind
CREATE INDEX ix_workflow_transition_receipts_request_kind ON workflow_transition_receipts (request_kind);

-- index: uq_authority_curation_running_authority
CREATE UNIQUE INDEX uq_authority_curation_running_authority ON authority_curation_attempts (project_id, source_authority_id) WHERE status = 'running';
