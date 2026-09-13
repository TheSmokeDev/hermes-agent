interface PluginRequest {
  path?: unknown
  pluginToken?: unknown
}

export function pluginRequestHeaders(request: PluginRequest): Record<string, string> {
  const token = request?.pluginToken

  if (token === undefined) {
    return {}
  }

  const path = typeof request.path === 'string' ? request.path : ''
  const pathname = path.split(/[?#]/, 1)[0]
  let decoded: string

  try {
    decoded = decodeURIComponent(pathname)
  } catch {
    throw new Error('Invalid plugin token route')
  }

  if (
    !/^\/api\/plugins\/[a-zA-Z0-9][a-zA-Z0-9_-]*(?:\/|$)/.test(pathname) ||
    decoded !== pathname ||
    pathname.includes('\\') ||
    pathname.split('/').some(segment => segment === '..' || segment === '.')
  ) {
    throw new Error('Plugin token is restricted to a plugin API namespace')
  }

  if (typeof token !== 'string' || !token || token.length > 4096 || !/^[\x21-\x7e]+$/.test(token)) {
    throw new Error('Invalid plugin token')
  }

  return { 'X-Hermes-Plugin-Token': token }
}

export async function withPluginRequestAuth<T>(request: PluginRequest, dispatch: () => Promise<T>): Promise<T> {
  pluginRequestHeaders(request)

  try {
    return await dispatch()
  } catch (error) {
    if (request.pluginToken && error instanceof Error) {
      const token = String(request.pluginToken)
      const message = error.message.split(token).join('[redacted]')
      const safe = new Error(message)
      safe.name = error.name
      throw safe
    }

    throw error
  }
}
