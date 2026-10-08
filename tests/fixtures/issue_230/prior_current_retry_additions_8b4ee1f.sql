-- Prior-current retry-table additions captured from HEAD 8b4ee1fac488c9c93e1733a57ce7ce1b32af856f.
-- Combine with the unchanged issue_260 pre_retry_business_schema_da3dbf63.sql.

CREATE TABLE sprint_retry_attempts (
	retry_attempt_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	ordinal INTEGER NOT NULL, 
	predecessor_retry_attempt_id INTEGER, 
	contract_fingerprint VARCHAR NOT NULL, 
	created_by VARCHAR NOT NULL, 
	rationale TEXT NOT NULL, 
	creation_fingerprint VARCHAR NOT NULL, 
	creation_receipt_key VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	status VARCHAR NOT NULL, 
	started_at DATETIME, 
	completed_at DATETIME, 
	PRIMARY KEY (retry_attempt_id), 
	CONSTRAINT uq_retry_attempt_project UNIQUE (project_id, retry_attempt_id), 
	CONSTRAINT uq_retry_attempt_scope UNIQUE (project_id, sprint_id, retry_attempt_id), 
	CONSTRAINT uq_retry_attempt_ordinal UNIQUE (project_id, sprint_id, ordinal), 
	CONSTRAINT uq_retry_attempt_predecessor UNIQUE (project_id, sprint_id, predecessor_retry_attempt_id), 
	CONSTRAINT fk_retry_attempt_predecessor FOREIGN KEY(project_id, sprint_id, predecessor_retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	CONSTRAINT ck_retry_attempt_ordinal CHECK (ordinal >= 2), 
	CONSTRAINT ck_retry_attempt_status CHECK (status IN ('Planned', 'Active', 'Completed')), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

CREATE TABLE sprint_retry_closures (
	sprint_retry_closure_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	review_fingerprint VARCHAR NOT NULL, 
	close_fingerprint VARCHAR NOT NULL, 
	closed_by VARCHAR NOT NULL, 
	closed_at DATETIME NOT NULL, 
	PRIMARY KEY (sprint_retry_closure_id), 
	CONSTRAINT uq_retry_closure_attempt UNIQUE (retry_attempt_id), 
	CONSTRAINT fk_retry_closure_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

CREATE TABLE sprint_retry_reviews (
	sprint_retry_review_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	review_fingerprint VARCHAR NOT NULL, 
	reviewed_by VARCHAR NOT NULL, 
	reviewed_at DATETIME NOT NULL, 
	PRIMARY KEY (sprint_retry_review_id), 
	CONSTRAINT uq_retry_review_attempt UNIQUE (retry_attempt_id), 
	CONSTRAINT fk_retry_review_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

CREATE TABLE sprint_retry_starts (
	sprint_retry_start_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	contract_fingerprint VARCHAR NOT NULL, 
	decision_fingerprint VARCHAR NOT NULL, 
	started_by VARCHAR NOT NULL, 
	started_at DATETIME NOT NULL, 
	PRIMARY KEY (sprint_retry_start_id), 
	CONSTRAINT uq_retry_start_attempt UNIQUE (retry_attempt_id), 
	CONSTRAINT fk_retry_start_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

CREATE TABLE sprint_retry_story_closures (
	sprint_retry_story_closure_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	story_id INTEGER NOT NULL, 
	completion_fingerprint VARCHAR NOT NULL, 
	resolution VARCHAR NOT NULL, 
	delivered TEXT NOT NULL, 
	evidence TEXT NOT NULL, 
	known_gaps TEXT NOT NULL, 
	closed_by VARCHAR NOT NULL, 
	closed_at DATETIME NOT NULL, 
	PRIMARY KEY (sprint_retry_story_closure_id), 
	CONSTRAINT uq_retry_story_closure UNIQUE (retry_attempt_id, story_id), 
	CONSTRAINT fk_retry_story_closure_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	CONSTRAINT fk_retry_story_closure_state FOREIGN KEY(project_id, sprint_id, retry_attempt_id, story_id) REFERENCES sprint_retry_story_states (project_id, sprint_id, retry_attempt_id, story_id), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id), 
	FOREIGN KEY(story_id) REFERENCES user_stories (story_id)
);

CREATE TABLE sprint_retry_story_states (
	sprint_retry_story_state_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	story_id INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	PRIMARY KEY (sprint_retry_story_state_id), 
	CONSTRAINT uq_retry_story_state UNIQUE (retry_attempt_id, story_id), 
	CONSTRAINT uq_retry_story_state_scope UNIQUE (project_id, sprint_id, retry_attempt_id, story_id), 
	CONSTRAINT fk_retry_story_state_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id), 
	FOREIGN KEY(story_id) REFERENCES user_stories (story_id)
);

CREATE TABLE sprint_retry_task_evidence (
	sprint_retry_task_evidence_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	task_id INTEGER NOT NULL, 
	outcome_summary TEXT NOT NULL, 
	artifact_refs_json TEXT NOT NULL, 
	acceptance_result VARCHAR NOT NULL, 
	checklist_result_json TEXT NOT NULL, 
	evidence_fingerprint VARCHAR NOT NULL, 
	completed_by VARCHAR NOT NULL, 
	completed_at DATETIME NOT NULL, 
	PRIMARY KEY (sprint_retry_task_evidence_id), 
	CONSTRAINT uq_retry_task_evidence UNIQUE (retry_attempt_id, task_id), 
	CONSTRAINT fk_retry_task_evidence_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	CONSTRAINT fk_retry_task_evidence_state FOREIGN KEY(project_id, sprint_id, retry_attempt_id, task_id) REFERENCES sprint_retry_task_states (project_id, sprint_id, retry_attempt_id, task_id), 
	CONSTRAINT ck_retry_task_evidence_acceptance CHECK (acceptance_result IN ('partially_met', 'fully_met')), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id), 
	FOREIGN KEY(task_id) REFERENCES tasks (task_id)
);

CREATE TABLE sprint_retry_task_states (
	sprint_retry_task_state_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	task_id INTEGER NOT NULL, 
	status VARCHAR NOT NULL, 
	PRIMARY KEY (sprint_retry_task_state_id), 
	CONSTRAINT uq_retry_task_state UNIQUE (retry_attempt_id, task_id), 
	CONSTRAINT uq_retry_task_state_scope UNIQUE (project_id, sprint_id, retry_attempt_id, task_id), 
	CONSTRAINT fk_retry_task_state_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id), 
	FOREIGN KEY(task_id) REFERENCES tasks (task_id)
);

CREATE TABLE sprint_retry_triage (
	sprint_retry_triage_id INTEGER NOT NULL, 
	project_id INTEGER NOT NULL, 
	sprint_id INTEGER NOT NULL, 
	retry_attempt_id INTEGER NOT NULL, 
	impact VARCHAR NOT NULL, 
	canonical_payload_json TEXT NOT NULL, 
	payload_fingerprint VARCHAR NOT NULL, 
	supersedes_sprint_retry_triage_id INTEGER, 
	recorded_by VARCHAR NOT NULL, 
	recorded_at DATETIME NOT NULL, 
	PRIMARY KEY (sprint_retry_triage_id), 
	CONSTRAINT uq_retry_triage_identity UNIQUE (retry_attempt_id, sprint_retry_triage_id), 
	CONSTRAINT uq_retry_triage_correction UNIQUE (retry_attempt_id, supersedes_sprint_retry_triage_id), 
	CONSTRAINT fk_retry_triage_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	CONSTRAINT fk_retry_triage_supersedes FOREIGN KEY(retry_attempt_id, supersedes_sprint_retry_triage_id) REFERENCES sprint_retry_triage (retry_attempt_id, sprint_retry_triage_id), 
	CONSTRAINT ck_retry_triage_impact CHECK (impact IN ('none', 'backlog', 'specification')), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id)
);

CREATE INDEX ix_sprint_retry_attempts_contract_fingerprint ON sprint_retry_attempts (contract_fingerprint);

CREATE INDEX ix_sprint_retry_attempts_created_by ON sprint_retry_attempts (created_by);

CREATE INDEX ix_sprint_retry_attempts_creation_fingerprint ON sprint_retry_attempts (creation_fingerprint);

CREATE INDEX ix_sprint_retry_attempts_creation_receipt_key ON sprint_retry_attempts (creation_receipt_key);

CREATE INDEX ix_sprint_retry_attempts_predecessor_retry_attempt_id ON sprint_retry_attempts (predecessor_retry_attempt_id);

CREATE INDEX ix_sprint_retry_attempts_project_id ON sprint_retry_attempts (project_id);

CREATE INDEX ix_sprint_retry_attempts_sprint_id ON sprint_retry_attempts (sprint_id);

CREATE INDEX ix_sprint_retry_attempts_status ON sprint_retry_attempts (status);

CREATE INDEX ix_sprint_retry_closures_close_fingerprint ON sprint_retry_closures (close_fingerprint);

CREATE INDEX ix_sprint_retry_closures_closed_by ON sprint_retry_closures (closed_by);

CREATE INDEX ix_sprint_retry_closures_project_id ON sprint_retry_closures (project_id);

CREATE INDEX ix_sprint_retry_closures_retry_attempt_id ON sprint_retry_closures (retry_attempt_id);

CREATE INDEX ix_sprint_retry_closures_review_fingerprint ON sprint_retry_closures (review_fingerprint);

CREATE INDEX ix_sprint_retry_closures_sprint_id ON sprint_retry_closures (sprint_id);

CREATE INDEX ix_sprint_retry_reviews_project_id ON sprint_retry_reviews (project_id);

CREATE INDEX ix_sprint_retry_reviews_retry_attempt_id ON sprint_retry_reviews (retry_attempt_id);

CREATE INDEX ix_sprint_retry_reviews_review_fingerprint ON sprint_retry_reviews (review_fingerprint);

CREATE INDEX ix_sprint_retry_reviews_reviewed_by ON sprint_retry_reviews (reviewed_by);

CREATE INDEX ix_sprint_retry_reviews_sprint_id ON sprint_retry_reviews (sprint_id);

CREATE INDEX ix_sprint_retry_starts_contract_fingerprint ON sprint_retry_starts (contract_fingerprint);

CREATE INDEX ix_sprint_retry_starts_decision_fingerprint ON sprint_retry_starts (decision_fingerprint);

CREATE INDEX ix_sprint_retry_starts_project_id ON sprint_retry_starts (project_id);

CREATE INDEX ix_sprint_retry_starts_retry_attempt_id ON sprint_retry_starts (retry_attempt_id);

CREATE INDEX ix_sprint_retry_starts_sprint_id ON sprint_retry_starts (sprint_id);

CREATE INDEX ix_sprint_retry_starts_started_by ON sprint_retry_starts (started_by);

CREATE INDEX ix_sprint_retry_story_closures_closed_by ON sprint_retry_story_closures (closed_by);

CREATE INDEX ix_sprint_retry_story_closures_completion_fingerprint ON sprint_retry_story_closures (completion_fingerprint);

CREATE INDEX ix_sprint_retry_story_closures_project_id ON sprint_retry_story_closures (project_id);

CREATE INDEX ix_sprint_retry_story_closures_retry_attempt_id ON sprint_retry_story_closures (retry_attempt_id);

CREATE INDEX ix_sprint_retry_story_closures_sprint_id ON sprint_retry_story_closures (sprint_id);

CREATE INDEX ix_sprint_retry_story_closures_story_id ON sprint_retry_story_closures (story_id);

CREATE INDEX ix_sprint_retry_story_states_project_id ON sprint_retry_story_states (project_id);

CREATE INDEX ix_sprint_retry_story_states_retry_attempt_id ON sprint_retry_story_states (retry_attempt_id);

CREATE INDEX ix_sprint_retry_story_states_sprint_id ON sprint_retry_story_states (sprint_id);

CREATE INDEX ix_sprint_retry_story_states_status ON sprint_retry_story_states (status);

CREATE INDEX ix_sprint_retry_story_states_story_id ON sprint_retry_story_states (story_id);

CREATE INDEX ix_sprint_retry_task_evidence_acceptance_result ON sprint_retry_task_evidence (acceptance_result);

CREATE INDEX ix_sprint_retry_task_evidence_completed_by ON sprint_retry_task_evidence (completed_by);

CREATE INDEX ix_sprint_retry_task_evidence_evidence_fingerprint ON sprint_retry_task_evidence (evidence_fingerprint);

CREATE INDEX ix_sprint_retry_task_evidence_project_id ON sprint_retry_task_evidence (project_id);

CREATE INDEX ix_sprint_retry_task_evidence_retry_attempt_id ON sprint_retry_task_evidence (retry_attempt_id);

CREATE INDEX ix_sprint_retry_task_evidence_sprint_id ON sprint_retry_task_evidence (sprint_id);

CREATE INDEX ix_sprint_retry_task_evidence_task_id ON sprint_retry_task_evidence (task_id);

CREATE INDEX ix_sprint_retry_task_states_project_id ON sprint_retry_task_states (project_id);

CREATE INDEX ix_sprint_retry_task_states_retry_attempt_id ON sprint_retry_task_states (retry_attempt_id);

CREATE INDEX ix_sprint_retry_task_states_sprint_id ON sprint_retry_task_states (sprint_id);

CREATE INDEX ix_sprint_retry_task_states_status ON sprint_retry_task_states (status);

CREATE INDEX ix_sprint_retry_task_states_task_id ON sprint_retry_task_states (task_id);

CREATE INDEX ix_sprint_retry_triage_impact ON sprint_retry_triage (impact);

CREATE INDEX ix_sprint_retry_triage_payload_fingerprint ON sprint_retry_triage (payload_fingerprint);

CREATE INDEX ix_sprint_retry_triage_project_id ON sprint_retry_triage (project_id);

CREATE INDEX ix_sprint_retry_triage_recorded_by ON sprint_retry_triage (recorded_by);

CREATE INDEX ix_sprint_retry_triage_retry_attempt_id ON sprint_retry_triage (retry_attempt_id);

CREATE INDEX ix_sprint_retry_triage_sprint_id ON sprint_retry_triage (sprint_id);

CREATE INDEX ix_sprint_retry_triage_supersedes_sprint_retry_triage_id ON sprint_retry_triage (supersedes_sprint_retry_triage_id);

CREATE UNIQUE INDEX uq_retry_attempt_live_project ON sprint_retry_attempts (project_id) WHERE status IN ('Planned', 'Active');

CREATE UNIQUE INDEX uq_retry_attempt_original_predecessor ON sprint_retry_attempts (project_id, sprint_id) WHERE predecessor_retry_attempt_id IS NULL;

CREATE UNIQUE INDEX uq_retry_triage_original ON sprint_retry_triage (retry_attempt_id) WHERE supersedes_sprint_retry_triage_id IS NULL;
