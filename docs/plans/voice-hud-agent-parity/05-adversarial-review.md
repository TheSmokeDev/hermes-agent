# PRP-001 Adversarial Review Manifest

**Reviewed revision:** uncommitted planning draft on base `155f05266d6b67ff008966397875ac356bcf9890`
**Review date:** 2026-08-08
**Verdict received:** fix blockers
**Review policy:** one adversarial planning review maximum; no second prose review

## Blocker dispositions

1. **Nonexistent `QuickEntryTarget` / inconsistent payload — fixed.**
   - Contract now uses `{ target: typeof QUICK_TARGET_CURRENT }` consistently.
   - IPC requires runtime literal validation.

2. **Gateway-open blank draft could create a session — fixed in contract.**
   - Primary pushes `currentVoiceTargetAvailable` from gateway-open plus non-null active runtime.
   - Mic remains disabled when no runtime is bound.
   - RED coverage requires no start/session creation in the gateway-open/no-runtime case.

3. **Global latch could drift from session/profile — fixed in contract.**
   - Start request carries a host-owned active profile/runtime/durable snapshot.
   - Linearization moved to successful main-composer consumption after exact validation.
   - Drift before consumption and while active/before submit fails closed and ends capture.

4. **HUD error projection impossible in prior scope — fixed.**
   - Scope now includes composer voice hooks and store.
   - Quick Entry receives minimal primary-owned availability/activity/status/terminal-error projection.
   - No second controller or wake-indicator inference is allowed.

5. **IPC sender was not validated — fixed.**
   - Main accepts start only from `quickEntryWindow.webContents`.
   - Main/secondary/unknown senders and malformed/new/stored targets fail closed.
   - Negative Electron tests are required.

6. **Wrong global declaration path — fixed.**
   - Correct path is `apps/desktop/src/global.d.ts`.

## Baseline evidence added after review

Dependencies were installed from the lockfile in the isolated worktree. The exact PRP command shapes were then executed:

- UI voice/Quick Entry baseline: 5 files, 42 tests passed.
- Electron baseline: 3 files, 34 tests passed.
- Desktop typecheck: passed.
- Desktop lint: passed with 88 pre-existing warnings and 0 errors.
- One pre-existing React `act(...)` warning remains non-fatal.

## Promotion

With the blocker fixes above, PRP-001 is promoted to **implementation-ready**. Further risk discovery moves to RED/GREEN tests, packaged Windows smoke, and final-diff review rather than another planning review.
