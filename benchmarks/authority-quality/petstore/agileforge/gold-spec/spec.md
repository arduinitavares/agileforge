# Petstore API Technical Spec

- Schema: agileforge.spec.v1
- Artifact id: SPEC.petstore-api
- Status: accepted
- Version: 1.0
- Created: 2026-05-21
- Updated: 2026-05-21
- Markdown profile: agileforge.spec_markdown.v1

## Summary

Defines the compact Petstore API contract used by the authority-quality benchmark.

## Problem Statement

A compact Petstore API contract must compile into authority that preserves endpoint, parameter, response, and schema semantics without depending on prose extraction.

## Controlled Terms

### operation

- Scope: artifact
- Definition: An HTTP method and path pair in the Petstore API contract.

### schema

- Scope: artifact
- Definition: The reusable Petstore data shape referenced by operations or responses.

## Items

### CONSTRAINT.limit-maximum - Limit query maximum

- Type: CONSTRAINT
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, limit, parameter

Statement:

The GET /pets limit query parameter must have a maximum value of 100.

Acceptance:

- The GET /pets operation defines a limit query parameter.
- The limit query parameter maximum value is 100.

### DATA.error-schema - Error schema required fields

- Type: DATA
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, error, schema

Statement:

The Error schema must require the code and message fields.

Acceptance:

- The Error schema defines a code field.
- The Error schema defines a message field.
- The Error schema marks code and message as required.

### DATA.pet-schema - Pet schema required fields

- Type: DATA
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, pet, schema

Statement:

The Pet schema must require the id and name fields.

Acceptance:

- The Pet schema defines an id field.
- The Pet schema defines a name field.
- The Pet schema marks id and name as required.

### DATA.pets-array - Pets array schema

- Type: DATA
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, pets, schema

Statement:

The Pets schema must be an array of Pet objects with maxItems equal to 100.

Acceptance:

- The Pets schema is an array.
- The Pets schema items are Pet objects.
- The Pets schema maxItems value is 100.

### GOAL.api-contract-fidelity - API contract fidelity

- Type: GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The compiled authority should preserve the Petstore API's endpoint, parameter, response, and schema contracts closely enough for downstream API planning agents.

Acceptance:

- None

### INTERFACE.create-pet - Create pet operation

- Type: INTERFACE
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, operation, pets

Statement:

The API must expose POST /pets to create a pet from a Pet request body and must define both the expected created-pet success response and the default error response returning Error.

Acceptance:

- The API contract includes a POST operation at /pets.
- The POST /pets operation accepts a Pet request body.
- The POST /pets success response is 201 and returns the created Pet schema.
- The POST /pets default response returns the Error schema.

### INTERFACE.get-pet - Get pet operation

- Type: INTERFACE
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, operation, pets

Statement:

The API must expose GET /pets/{petId} to fetch a single pet and must define both the expected success response returning Pet and the default error response returning Error.

Acceptance:

- The API contract includes a GET operation at /pets/{petId}.
- The GET /pets/{petId} success response is 200 and returns the Pet schema.
- The GET /pets/{petId} default response returns the Error schema.

### INTERFACE.list-pets - List pets operation

- Type: INTERFACE
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, operation, pets

Statement:

The API must expose GET /pets to list pets and must define both the expected success response returning Pets and the default error response returning Error.

Acceptance:

- The API contract includes a GET operation at /pets.
- The GET /pets success response is 200 and returns the Pets schema.
- The GET /pets default response returns the Error schema.

### INTERFACE.pet-id-path-parameter - Required petId path parameter

- Type: INTERFACE
- Status: accepted
- Level: MUST
- Verification: integration-test
- Tags: api, parameter, petId

Statement:

The GET /pets/{petId} operation must define petId as a required path parameter.

Acceptance:

- The GET /pets/{petId} operation defines petId as a path parameter.
- The petId path parameter is required.

### NON_GOAL.full-openapi-surface - Full OpenAPI surface

- Type: NON_GOAL
- Status: accepted
- Level: -
- Verification: -
- Tags: -

Statement:

The benchmark fixture is not intended to model the full Swagger Petstore surface beyond the listed paths, parameters, responses, and schemas.

Acceptance:

- None

## Relations

- CONSTRAINT.limit-maximum constrains INTERFACE.list-pets
  - Rationale: The maximum constrains the list operation's limit parameter.
- INTERFACE.create-pet depends_on DATA.error-schema
  - Rationale: The default error response depends on the Error schema.
- INTERFACE.create-pet depends_on DATA.pet-schema
  - Rationale: The create operation request and success response depend on the Pet schema.
- INTERFACE.create-pet satisfies GOAL.api-contract-fidelity
  - Rationale: The create operation advances the API contract fidelity goal.
- INTERFACE.get-pet depends_on DATA.error-schema
  - Rationale: The default error response depends on the Error schema.
- INTERFACE.get-pet depends_on DATA.pet-schema
  - Rationale: The get success response depends on the Pet schema.
- INTERFACE.get-pet satisfies GOAL.api-contract-fidelity
  - Rationale: The get operation advances the API contract fidelity goal.
- INTERFACE.list-pets depends_on DATA.error-schema
  - Rationale: The default error response depends on the Error schema.
- INTERFACE.list-pets depends_on DATA.pets-array
  - Rationale: The list success response depends on the Pets schema.
- INTERFACE.list-pets satisfies GOAL.api-contract-fidelity
  - Rationale: The list operation advances the API contract fidelity goal.
- INTERFACE.pet-id-path-parameter constrains INTERFACE.get-pet
  - Rationale: The required path parameter constrains the get-pet operation.

<!-- agileforge-review-notes:start -->
<!-- agileforge-review-notes:end -->
