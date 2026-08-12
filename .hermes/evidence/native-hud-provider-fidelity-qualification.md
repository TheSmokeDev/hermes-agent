# Native HUD Provider-Fidelity Qualification Receipt

**Date:** 2026-08-11
**Verdict:** `PASS_FIDELITY_IDENTITY_PARTIAL`
**Scope:** disposable production-read-only OpenAI Realtime wire proof
**Gateway/runtime impact:** none

## Qualified claim

One explicit host request supplied exact canonical text and opaque correlation
metadata to `gpt-realtime-2.1` / `cedar`. Automatic response and tools remained
disabled. The provider emitted native PCM and an output-audio transcript only
after that request. The final transcript matched the supplied UTF-8 bytes
exactly.

This is a bounded observed fidelity result, not a claim that model inference is
a deterministic TTS primitive.

## Authoritative final proof

```text
Model:                         gpt-realtime-2.1
Voice:                         cedar
Auth lane:                     codex-oauth
Automatic response:            disabled
Tools/tool choice:              [] / none
Quiet pre-request output:       zero
Correlation at created/done:    exact
Response status:                completed
Response output type:           message
Nested content type:            output_audio
Final transcript exact UTF-8:   yes
Function/tool calls:            zero
Native PCM bytes:               153600
PCM16 mono frames:              76800
Audio deltas:                   8
Response output items:          1
Response/item continuity:       exact
Client close:                   clean
```

Every executed gate in `receipt.json` is `true`.

## Identity result

The provider exposed:

- one nonempty provider response ID at `response.created`;
- one stable output item ID across audio/transcript/completion;
- exact response and item continuity in `response.done`.

That is sufficient for exact render/playback correlation.

Response-local cancellation remains unqualified and blocks barge-in work. The
current pinned Talk contract encodes global cancellation only:

```json
{"type": "response.cancel"}
```

The required response-local wire shape is:

```json
{
  "type": "response.cancel",
  "event_id": "<opaque unique token>",
  "response_id": "<exact active provider response>"
}
```

No cancellation was sent during the final fidelity proof because cancelling the
only response would invalidate the fidelity terminal receipt.

## Probe correction history

The first live run is preserved as
`receipt.invalid-audio-discriminator.json`. It rendered exact transcript and PCM
but the harness incorrectly expected nested content discriminator `audio`
instead of the frozen/provider shape `output_audio`. A read-only adjudication
classified this as `PROBE_INVALID_REQUIRES_FINAL_RERUN`, not a provider failure.

Before the one final rerun:

- the predicate was corrected to exact `message → output_audio`;
- safe response output/content types and counts were added to the receipt;
- local asymmetric tests proved canonical shape acceptance and rejection of the
  old discriminator and tool-bearing shapes;
- `2 passed`, Ruff passed, and Python compilation passed.

There was no third live attempt.

## Artifact hashes

```text
README.md
9ad84cab68b8fa3f16f254dc35e7fc823ad1332d06b86ed4e0a691eb4279f412

probe.py
3d7391fed671d517799d3135bec2dd1cb461ab839b662afa0f7606601fb37bce

test_probe.py
e0c45ff7260178f9827676ff70d7a967cf3f3255f5c375ae756580ef78a5adf8

invalid first receipt
5732650744d806ec42e14810f20f60498e3d007c44e100d3fbe0d8480e8858ce

authoritative final receipt
a6dcad31a6a01dcab96712f76937123427d1c5f632723342b9f0b833989e009b
```

## Source integrity

```text
Hermes Agent sealed branch tip:
de6e08de236f617722034aebb228bce1875112ac
Status: clean

Pinned Talk provider source:
99d8f2a3971db074bd2649c56d9afc91b63c57e2
Tracked status: unchanged
```

The disposable proof persisted no audio, raw/base64 event payload, credential,
ephemeral secret, auth header, or credential-bearing URL.

## Authorized next boundary

Do not begin Discord playback, audible-drain completion, or barge-in.

The next narrow step is response-local cancellation qualification and contract
work:

1. define exact bounded `cancel_response(response_id)` capability;
2. encode exact response ID plus unpredictable event ID;
3. prove one cancel send, correlated cancelled/completed terminal race, replay
   safety, and cleanup with deterministic local tests;
4. run a separate bounded live cancellation proof only after the local contract
   passes review;
5. only then plan the canonical-response request → native PCM sink slice, while
   continuing to defer physical playback/drain and replacement-turn barge-in.

No install, restart, activation, live voice canary, push, PR, or merge is
authorized by this receipt.

## Final review

Independent read-only review verdict: `APPROVED`.

The reviewer verified every authoritative JSON gate, exact transcript and
`message → output_audio` shape, PCM counts, zero function/tool activity,
correlation and response/item continuity, clean close, artifact hashes, source
SHAs, and unchanged pinned worktrees. It found no critical or important issue
and confirmed that `PASS_FIDELITY_IDENTITY_PARTIAL` does not authorize output
integration, playback, audible-drain completion, or barge-in.
