# Tolerate a leading prose preamble before an agent's JSON object

## Status

Accepted

## Context

Every role is instructed to "return exactly one JSON object and no
other text." `_parse_json_object()` already tolerated one departure
from that instruction: a markdown code fence (` ```json ... ``` `)
wrapping the whole output, since models occasionally add one despite
being told not to.

Running the loop against a real project, `argo/Claude Opus 4.8` (in
the planner role) reproducibly prefixed its JSON with a short
confirmation sentence, across two independent fresh sessions with no
shared context:

> "Confirmed: no parser exists yet. The next coherent unit of work
> is...\n\n{...}"

Both times the JSON itself was well-formed and the plan it carried was
substantively correct; only the leading sentence caused
`json.loads()` to fail at the very first character. Because the
failure was deterministic for this model, it exhausted
`malformed_output_retries` and turned a good plan into a durable
`operational_failure` (see ADR 0009), costing a full retry round trip
for a formatting habit rather than a reasoning error.

This is the same category of problem the fence tolerance already
solves: a specific, observed model habit that plain instruction text
does not reliably suppress, and that can be recognized narrowly enough
not to weaken the contract's actual guarantees.

## Decision

Extend `_parse_json_object()` to tolerate a leading prose preamble:
when parsing the whole (fence-stripped) text fails, find the first
`{` in the text and decode a single object from there with
`json.JSONDecoder().raw_decode()`. Accept the result only if nothing
but whitespace (or a bare leftover markdown fence closer, for the
preamble-then-fenced-object case) remains after the decoded object.

Deliberately reject, exactly as before:

- **Trailing content after the object.** A model that keeps talking
  after its answer is a different, more concerning failure mode than
  one that briefly narrates before it, and is not tolerated.
- **A second JSON object anywhere in the text.** The first-`{`
  decode fails to consume the whole remaining text when a second
  object follows, so this falls out of the trailing-content check
  automatically rather than needing separate handling.
- **A preamble that itself contains an unrelated `{`.** Decoding
  always starts at the *first* `{` in the text. If that brace does
  not open a valid, complete JSON object, parsing fails loudly. An
  alternative design would scan every `{` in the text until one
  decodes cleanly with nothing trailing, which would also handle a
  brace inside the preamble — but it would silently accept whichever
  candidate object happens to parse, including picking one arbitrarily
  out of several. A validation layer should reject ambiguous or
  multi-object output outright, not guess which object was intended.

No change to the agent prompts: the existing instruction ("return
exactly one JSON object and no other text") remains correct and is
left in place. This decision only widens what the parser tolerates
when that instruction is not perfectly followed; it does not change
what any prompt asks for.

## Consequences

- A model's leading confirmation/narration sentence no longer costs a
  retry round trip or, if it recurs on retry, a full operational
  failure and resume cycle.
- The "return exactly one object" guarantee is unchanged: two objects,
  or an object followed by other content, are still rejected exactly
  as before.
- The one deliberate gap — a preamble containing its own `{` — remains
  unhandled by design, not by oversight; it fails with the same "output
  is not valid JSON" error as any other malformed output, which is the
  safe default until a real occurrence justifies further tolerance.
- `check_decision_answered()` and `check_task_identity()` are
  unaffected: they validate already-parsed field values, not the raw
  text, so this decision only touches the parsing boundary in
  `_parse_json_object()`.
