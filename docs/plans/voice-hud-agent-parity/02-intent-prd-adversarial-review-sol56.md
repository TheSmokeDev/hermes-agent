# Adversarial product/authority review — Hermes Native Realtime HUD Intent PRD

## Review posture

The PRD has the correct central decision: canonical Hermes is the only reasoning, tool-selection, approval, session, memory, and durable-history authority; realtime is intended to be untrusted audio/transcription/rendering transport. That intent is repeated consistently in §§1–3, 6, 8.3, 9, and 11 and aligns with `AGENTS.md`’s narrow-waist, session-scoped capability, prompt-cache, role-alternation, and E2E-invariant rules (`AGENTS.md` lines 19–27, 71–91, 213–250, 1172–1184).

The draft is not yet a safe acceptance authority. Several lifecycle and security requirements are contradictory or undefined at exactly the points where a transport could acquire authority, leak data, deadlock a turn, or accidentally cancel durable Hermes work.

## BLOCKERS

### B1. Voice-turn authorization is asserted but has no acceptance contract

Sections 6.4, 8.2, and 9 require foreign-speaker and replay rejection and correctly say provider metadata is not authority (§8.2 lines 211–213; §9 lines 276–290). Yet neither MVP scope nor the acceptance demo says what host-verifiable evidence authorizes a speaker to create a canonical user turn. In a multi-user Discord voice channel, channel attachment alone cannot establish that each audio stream is Smoke, and a provider transcript/correlation ID is explicitly untrusted. This is more serious than spoken approval: an admitted voice turn can ask canonical Hermes to invoke mutating tools.

The MVP must fail closed unless the host can bind each admitted utterance to an authenticated platform principal that is authorized for the exact profile/session/surface/generation. Voiceprint inference and provider attribution must not count. The acceptance demo must include an unauthorized platform principal and a stale/replayed authorized event and prove neither creates a canonical row or starts a Hermes turn.

### B2. Barge-in requires an impossible playback boundary

Section 8.5 says the replacement is admitted after the previous turn reaches its “required persistence and playback boundary” (lines 243–245). Sections 8.4 and 8.7 say speaking/completion depends on physical playback completion/drain (lines 231–234, 267–270), while §§5, 6.7, and 12.8 require barge-in to cancel that playback. An interrupted stream cannot also drain normally. As written, an implementation can deadlock the correction, falsely claim drain/completion, or weaken the completion rule ad hoc.

The PRD must define two mutually exclusive terminal delivery outcomes: normal playback drain, or host-observed interruption/cancellation acknowledgment for the exact response and playback lease. The prior canonical assistant message must already be durably terminal before the correction becomes the next serialized user turn; `COMPLETED` must never be emitted for interrupted playback. This also preserves `AGENTS.md`’s strict role alternation and no-synthetic-user-message invariant (lines 88–91).

### B3. Close/disconnect cleanup contradicts durable background work

Sections 6.9, 12.9, and the north-star scenario require delegated/background work to survive HUD close, disconnect, and barge-in. Sections 12.11 and 14.3 instead require close/cancellation to leave “no orphan tasks” and “zero retained tasks,” while §17’s mitigation explicitly calls for retained task ownership. These statements cannot all be used as acceptance gates.

The PRD must distinguish transport-owned ephemeral work (capture, provider response, audio queue, playback lease, reconnect loop), which must terminate on close, from canonical Hermes-owned work (tools, delegation, cron, background processes), which follows existing Hermes cancellation/durability semantics and must not be cancelled merely because the surface closes. Note that `AGENTS.md` lines 1051–1053 state background `delegate_task` is process-local, not restart-durable; the PRD must not imply survival across a Hermes host restart unless an existing durable mechanism is used.

### B4. Spoken progress creates a second, undefined response authority

Sections 5 and 8.3 promise spoken progress, including progress that is not a new authoritative assistant message (lines 111–113, 222–223). Section 8.4 says all spoken content originates from the exact accepted canonical assistant result (lines 227–230), and §18.1 defers the progress policy. These cannot all be true. Unpersisted provider speech is precisely the parallel-answer channel the PRD prohibits.

Spoken progress must be removed from MVP promises and acceptance unless it is canonical Hermes-authored, explicitly typed as non-final lifecycle output, causally bound to a canonical turn, auditable, and forbidden from claiming tool outcomes or approvals. For the minimal MVP, only the persisted canonical final assistant text should be rendered.

### B5. Security covers authority but omits the audio/data threat boundary

The PRD discusses approvals, replay, and provider metadata but has no coherent security/privacy requirement for microphone audio, transcripts, canonical assistant text sent for rendering, Discord participants, provider retention, logs/receipts, credentials, or output that contains sensitive data. “Same safety” (§6.4) is not sufficient because typed Hermes does not continuously send microphone audio or speak results into a shared audio surface.

Before canary authorization, the PRD must require explicit operator activation and visible capture/playback state; authenticated and authorized surface membership; least-data provider disclosure; no provider access to Hermes credentials/tool schemas/session history beyond the bounded audio/transcript/render request; defined retention and redaction policy for audio, transcripts, logs, and receipts; no secret values in telemetry/receipts; and an immediate host-enforced mute/detach path. The canary must use non-sensitive data and prove an unauthorized participant cannot admit a turn or receive a privileged response.

## IMPORTANT improvements

### I1. “Full parity” overclaims beyond the exact session’s configured capability

Sections 1, 6.3, 10.1, 12, and 20 repeatedly say “same capability,” “full capability parity,” or “full dynamic Hermes tool parity.” Hermes tool availability is a property of the exact session/platform/configuration, not a universal registry (`AGENTS.md` lines 213–250 and 1003–1018). A voice attachment must neither expand nor shrink the attached session’s resolved tools, approvals, or prompt.

Replace universal parity language with an invariant: for the same canonical session and unchanged session configuration, a voice-admitted turn uses the identical already-resolved model loop, tool schemas, policy, context, and prompt prefix as a typed turn. Explicitly prohibit mid-conversation toolset/system-prompt mutation, consistent with `AGENTS.md` lines 1172–1184.

### I2. Discord MVP, Desktop HUD scope, and rollout gates disagree

Section 10.1 defines MVP as Discord voice. Section 10.2 makes Desktop Quick Entry/HUD the next surface. Gate D nevertheless requires Desktop/Quick Entry before Gate E, while the title, executive decision, and definition of done describe a native HUD product. This leaves “MVP passed” ambiguous: either Discord is the bounded transport/authority proof, or Desktop HUD is part of MVP.

Name the Discord slice “MVP authority/transport proof” and make Desktop consumption a later gate, or move Desktop into MVP and its acceptance demo. Do not require a next-surface deliverable in a prerequisite gate for the Discord canary.

### I3. Experience success has no pass/fail latency threshold

Section 14.2 uses “feels immediate” and “quickly enough” while deferring all numbers until canary evidence (lines 409–417). That is suitable for exploration, not a success gate. Functional cancellation receipts do not prove conversational interruption.

Define provisional bounded thresholds or explicitly classify latency as observational during the first canary and require thresholds to be approved before declaring the realtime experience/MVP successful. Record at least finalized-transcript-to-admission, canonical-final-to-first-audio, speech-start-to-audible-stop, and reconnect distributions with sample count and failure treatment.

### I4. “Physical playback” exceeds observable evidence

Sections 8.4, 8.7, 12.11, and 14.1 require physical playback/drain. Software can prove that the owning adapter/device API accepted and drained bounded PCM; it generally cannot prove that sound was physically audible. The current wording invites false certainty.

Define the authoritative observable as an adapter/device playback-drain acknowledgment for the exact lease, plus an operator observation only for the bounded canary. Reserve “audible” for human-confirmed canary evidence, not deterministic tests.

### I5. Rollback is named but not accepted

Sections 6.10, 10.1, 14.3, 15.9, and Gate E require rollback, but no success criterion or demo step defines what a successful rollback proves.

Add an acceptance step that disables/detaches the native route, restores the exact prior reviewed runtime/source mapping, starts a fresh process, verifies ordinary typed/session behavior and canonical history remain intact, and proves no realtime capture/provider/playback task remains. Rollback evidence must not itself authorize merge or production activation.

### I6. Current-evidence claims are not independently falsifiable

Section 19 says a provider-neutral contract/controller is “in progress” and Discord attachment “has proven the direction,” but supplies no immutable commit, test/receipt, or scoped limitation. Section 15.6 itself requires cross-repository evidence at exact immutable commits. The prose also correctly admits the full output/playback/interruption/canary path is not proven.

Either attach immutable evidence references for each current-state claim or relabel them as unverified starting hypotheses. State explicitly that input-only admission proves neither output authority, replay resistance, interruption safety, nor production readiness.

### I7. The canary combines too many consequential variables

Section 13’s default demo opens email through computer control and delegates a summarizer, then interrupts with a wrong-account correction. It is nondeterministic, potentially privacy-sensitive, and may execute the wrong action before the correction. The read-only substitute is currently optional.

Make a deterministic, harmless, read-only dynamic tool the mandatory authority/playback/barge-in canary. Prove delegation separately with a bounded non-mutating task whose continuation can be observed. Email/account switching may be an operator-approved follow-up demonstration, not the minimum pass gate.

## OPTIONAL notes

- The fixed `gpt-realtime-2.1 / cedar` pair is coherent as the first canary fixture, but should be labeled a validated fixture rather than part of the provider-neutral authority contract; provider/model availability is a Gate E prerequisite, not assumed evidence.
- Add stable turn/response/playback identifiers to receipts only as correlation fields; §9 already correctly prevents those IDs from becoming identity or authorization.
- Replace “user row” in §14.1 with the repository’s actual canonical persistence concept before implementation review; otherwise a schema-specific metric can become a change-detector rather than a behavior invariant.

## Numbered concrete edit list

1. **Change §8.2 “Listening and transcript admission” and §13 “Minimum acceptance demonstration.”** Replace the current authorization implication with: “A finalized utterance is admissible only when the Hermes host binds its adapter-authenticated platform principal to the authorized operator, exact profile/session/surface, and current transport generation. Provider attribution, transcript content, correlation IDs, and voiceprints confer no authority. MVP tests and canary prove unauthorized, stale, replayed, and duplicated events create no canonical turn.”
2. **Change §8.5 “Barge-in” and §8.7 “Truthful HUD state.”** Replace “required persistence and playback boundary” with: “The previous canonical assistant turn must be durably terminal. Its delivery then terminates by exactly one outcome: normal adapter/device drain, or exact-lease interruption acknowledgment. `INTERRUPTED` is terminal delivery evidence and can never emit `COMPLETED`; after that barrier the pinned correction is admitted once as the next user turn.”
3. **Change §§6.9, 12.9–12.11, and §14.3 “Reliability.”** Replace undifferentiated task cleanup with: “Close/disconnect terminates all surface/provider/playback work and leases. Canonical Hermes tools and background/delegated work are not surface-owned and follow existing Hermes cancellation and durability rules. No claim is made that process-local delegation survives host restart.”
4. **Change §5, §8.3, §8.4, and §18.1.** Replace MVP spoken-progress promises with: “MVP renders only the exact persisted canonical final assistant text. Spoken progress is out of MVP until Hermes-authored, typed non-final lifecycle output has an auditable authority contract and cannot claim approval, tool success, or finality.”
5. **Add a security/privacy subsection before §10 “Scope” and extend §13.** Insert intent covering explicit capture consent, authenticated membership, least-data provider disclosure, provider isolation from tools/credentials/history, retention/redaction, shared-surface output privacy, mute/detach, non-sensitive canary data, and negative unauthorized-speaker/output tests.
6. **Change §§1, 6.3, 10.1, 12, and 20 parity wording.** Replace “full capability parity” with: “For the exact attached canonical session with unchanged configuration, voice and typed turns use the same already-resolved prompt/context, model loop, tools, policies, approvals, and persistence path; voice neither adds nor removes capability and does not mutate toolsets or the system prompt mid-conversation.”
7. **Change §10 and Gates D/E.** Replace the mixed scope with either: “Discord is the MVP authority/transport proof; Desktop/HUD is a later surface gate and is not required for its canary,” or explicitly add Desktop to MVP and demonstrate it. The document must choose one.
8. **Change §14.2 “Experience.”** Replace subjective success terms with provisional measurable latency budgets, or mark first-canary latency as observational and prohibit declaring realtime experience success until budgets, sample count, percentile, and failure handling are approved.
9. **Change §§8.4, 8.7, 12.11, and 14.1.** Replace deterministic “physical/audible playback” proof with “exact-lease adapter/device drain acknowledgment”; require human audible confirmation only in the bounded canary.
10. **Change §12 and §13 to add rollback acceptance.** Insert a post-canary rollback demonstration proving exact prior runtime restoration from a fresh process, preservation of typed session/history behavior, disabled native routing, and zero remaining realtime transport resources.
11. **Change §19 “Current evidence and starting point.”** Add immutable commit/test/receipt anchors for each evidence claim or label it unverified; explicitly bound input-only evidence so it cannot imply output, replay, interruption, or deployment proof.
12. **Change §13 steps 2–8.** Replace email as the default with a deterministic harmless read-only dynamic tool; demonstrate bounded non-mutating delegation separately; retain email/account switching only as an optional operator-approved follow-up.

BLOCK
