# Petstore API Contract Source

This fixture is a compact OpenAPI-style description of a Petstore API used for
authority-quality benchmarking.

## Paths

### GET /pets

Lists pets.

Parameters:

- `limit`: optional query parameter limiting the number of returned pets. The
  maximum value is `100`.

Responses:

- `200`: returns `Pets`.
- `default`: returns `Error`.

### POST /pets

Creates a pet from a `Pet` request body.

Responses:

- `201`: returns the created `Pet`.
- `default`: returns `Error`.

### GET /pets/{petId}

Returns a single pet by identifier.

Parameters:

- `petId`: required path parameter.

Responses:

- `200`: returns `Pet`.
- `default`: returns `Error`.

## Schemas

### Pet

Object schema with required fields `id` and `name`.

### Pets

Array of `Pet` objects with `maxItems` of `100`.

### Error

Object schema with required fields `code` and `message`.
