export interface NotificationActionSource {
  requestId?: string
  sessionId?: string
}

export interface NotificationActionPayload {
  actionId: string
  requestId?: string
  sessionId?: string
}

export function notificationActionPayload(
  source: NotificationActionSource | null | undefined,
  actionId: string
): NotificationActionPayload {
  return {
    actionId,
    requestId: source?.requestId,
    sessionId: source?.sessionId
  }
}
