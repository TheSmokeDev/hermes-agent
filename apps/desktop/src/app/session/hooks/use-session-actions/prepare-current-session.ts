import type { SessionOwnerRoute } from '@/store/session-request-router'

import { type ResolveTargetSessionDeps, resolveTargetSessionId } from '../use-prompt-actions/resolve-target-session'

export interface PrepareCurrentSessionDeps extends ResolveTargetSessionDeps {
  owner: SessionOwnerRoute
  routeToken: string
  isCurrent: () => boolean
  current: () => { routeToken: string; selectedStoredSessionId: null | string; activeRuntimeId: null | string }
  publish: (runtimeId: string, storedSessionId: string, owner: SessionOwnerRoute) => void
}

/** Prepare the displayed conversation without submitting text or starting a turn. */
export async function prepareCurrentSession(deps: PrepareCurrentSessionDeps): Promise<string> {
  const storedTarget = deps.routedStoredSessionId ?? deps.selectedStoredSessionId

  if (
    !deps.isCurrent() ||
    (deps.routedStoredSessionId && deps.routedStoredSessionId !== deps.selectedStoredSessionId)
  ) {
    throw new Error('The conversation is still switching. Try Talk again when it finishes.')
  }

  const runtimeId = await resolveTargetSessionId(deps)

  if (!runtimeId) {
    throw new Error('This conversation could not be prepared for voice. Reconnect and try again.')
  }

  const storedSessionId = storedTarget ?? deps.current().selectedStoredSessionId

  const assertCurrent = () => {
    const current = deps.current()

    const changed = storedTarget
      ? current.routeToken !== deps.routeToken || current.selectedStoredSessionId !== storedTarget
      : current.activeRuntimeId !== runtimeId || current.selectedStoredSessionId !== storedSessionId

    if (!deps.isCurrent() || changed || !storedSessionId) {
      throw new Error('The selected conversation changed while voice was preparing.')
    }
  }

  assertCurrent()

  const prepared = await deps.requestGateway<{ session_id: string; stored_session_id: string }>('session.prepare', {
    session_id: runtimeId,
    stored_session_id: storedSessionId
  })

  assertCurrent()

  if (prepared.session_id !== runtimeId || prepared.stored_session_id !== storedSessionId) {
    throw new Error('The prepared conversation does not match the selected conversation.')
  }

  deps.publish(runtimeId, prepared.stored_session_id, deps.owner)

  return runtimeId
}
