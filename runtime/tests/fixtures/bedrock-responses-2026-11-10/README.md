# Bedrock Responses streams, recorded 2026-11-10

Replies of Amazon Bedrock Runtime's `/openai/v1/responses` to three requests, with a Grok 4.6
model. The prompts were made up for the recording.

- `text.sse`: a developer message and a user message, no tools. One text reply.
- `tool-call.sse`: the same, with the assistant's tools offered as plain function tools. A
  short text, then a call of `factorial_projects_list`.
- `tool-answer.sse`: the request that followed, carrying that reply's items and the call's
  output as text. The answer.

Each file is the stream as Bedrock sent it, but for three edits. The `tools` list that the
`response.created`, `response.in_progress` and `response.completed` events repeat is empty: it
was the request's own, about 30 KB each time. Each reasoning item's `encrypted_content` is
`rsn_trimmed`: the adapter never reads it. The model id reads `us.xai.grok-4.6` throughout.
