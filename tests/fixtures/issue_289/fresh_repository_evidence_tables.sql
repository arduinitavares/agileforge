-- Frozen empty-fixture DDL only; product upgrades use additive ALTER TABLE.

DROP TABLE task_completion_evidence;

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
	repository_evidence_json TEXT, 
	PRIMARY KEY (task_completion_evidence_id), 
	CONSTRAINT uq_task_completion_evidence UNIQUE (task_id, sprint_id), 
	CONSTRAINT ck_task_completion_acceptance CHECK (acceptance_result IN ('partially_met', 'fully_met')), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id), 
	FOREIGN KEY(task_id) REFERENCES tasks (task_id)
);

CREATE INDEX ix_task_completion_evidence_acceptance_result ON task_completion_evidence (acceptance_result);

CREATE INDEX ix_task_completion_evidence_completed_by ON task_completion_evidence (completed_by);

CREATE INDEX ix_task_completion_evidence_evidence_fingerprint ON task_completion_evidence (evidence_fingerprint);

CREATE INDEX ix_task_completion_evidence_project_id ON task_completion_evidence (project_id);

CREATE INDEX ix_task_completion_evidence_sprint_id ON task_completion_evidence (sprint_id);

CREATE INDEX ix_task_completion_evidence_task_id ON task_completion_evidence (task_id);

DROP TABLE sprint_retry_task_evidence;

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
	repository_evidence_json TEXT, 
	PRIMARY KEY (sprint_retry_task_evidence_id), 
	CONSTRAINT uq_retry_task_evidence UNIQUE (retry_attempt_id, task_id), 
	CONSTRAINT fk_retry_task_evidence_attempt FOREIGN KEY(project_id, sprint_id, retry_attempt_id) REFERENCES sprint_retry_attempts (project_id, sprint_id, retry_attempt_id), 
	CONSTRAINT fk_retry_task_evidence_state FOREIGN KEY(project_id, sprint_id, retry_attempt_id, task_id) REFERENCES sprint_retry_task_states (project_id, sprint_id, retry_attempt_id, task_id), 
	CONSTRAINT ck_retry_task_evidence_acceptance CHECK (acceptance_result IN ('partially_met', 'fully_met')), 
	FOREIGN KEY(project_id) REFERENCES projects (project_id), 
	FOREIGN KEY(sprint_id) REFERENCES sprints (sprint_id), 
	FOREIGN KEY(task_id) REFERENCES tasks (task_id)
);

CREATE INDEX ix_sprint_retry_task_evidence_acceptance_result ON sprint_retry_task_evidence (acceptance_result);

CREATE INDEX ix_sprint_retry_task_evidence_completed_by ON sprint_retry_task_evidence (completed_by);

CREATE INDEX ix_sprint_retry_task_evidence_evidence_fingerprint ON sprint_retry_task_evidence (evidence_fingerprint);

CREATE INDEX ix_sprint_retry_task_evidence_project_id ON sprint_retry_task_evidence (project_id);

CREATE INDEX ix_sprint_retry_task_evidence_retry_attempt_id ON sprint_retry_task_evidence (retry_attempt_id);

CREATE INDEX ix_sprint_retry_task_evidence_sprint_id ON sprint_retry_task_evidence (sprint_id);

CREATE INDEX ix_sprint_retry_task_evidence_task_id ON sprint_retry_task_evidence (task_id);
