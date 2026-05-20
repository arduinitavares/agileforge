# Petstore Oracle Notes

Do not provide these notes to the spec generator, authority compiler, or
external LLM reviewers. They are benchmark oracle notes for human synthesis
after independent reviews are complete.

- `GET /pets`
- `POST /pets`
- `GET /pets/{petId}`
- `limit` query parameter with maximum `100`
- `petId` required path parameter
- `Pet` schema with required `id` and `name`
- `Pets` array with `maxItems=100`
- `Error` schema with required `code` and `message`
- expected success and default error responses
