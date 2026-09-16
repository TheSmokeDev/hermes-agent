import { randomBytes } from 'node:crypto'

const TOKEN_ENV = 'HERMES_DESKTOP_TALK_TOKEN'
const TOKEN_HEADER = 'X-Hermes-Desktop-Talk-Token'

interface BackendDescriptor {
  baseUrl: string
  token?: null | string
  mode?: string
  source?: string
  authMode?: string
}

interface NativeGrant {
  token: string
  gatewayToken: string
  isCurrent: () => boolean
}

function localTalkRoute(descriptor: BackendDescriptor, path: string): boolean {
  if (descriptor.mode !== 'local' || descriptor.source !== 'local' || descriptor.authMode !== 'token') {
    return false
  }

  const pathname = path.split(/[?#]/, 1)[0]

  if (
    !/^\/api\/plugins\/hermes-talk(?:\/|$)/.test(pathname) ||
    pathname.includes('%') ||
    pathname.includes('\\') ||
    pathname.split('/').some(segment => segment === '.' || segment === '..')
  ) {
    return false
  }

  try {
    const url = new URL(descriptor.baseUrl)

    return (
      url.protocol === 'http:' &&
      ['127.0.0.1', '[::1]'].includes(url.hostname) &&
      url.pathname === '/' &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash
    )
  } catch {
    return false
  }
}

/** A local child receives the grant through its environment; IPC descriptors never carry it. */
export function createDesktopTalkAuth() {
  const grants = new Map<string, NativeGrant>()

  return {
    issue() {
      const token = randomBytes(32).toString('base64url')
      let binding: null | { baseUrl: string; grant: NativeGrant } = null
      let revoked = false

      return {
        env: { [TOKEN_ENV]: token },
        bind(baseUrl: string, gatewayToken: string, isCurrent: () => boolean) {
          if (revoked) {
            return
          }

          const grant = { token, gatewayToken, isCurrent }
          binding = { baseUrl, grant }
          grants.set(baseUrl, grant)
        },
        revoke() {
          revoked = true

          if (binding && grants.get(binding.baseUrl) === binding.grant) {
            grants.delete(binding.baseUrl)
          }
        }
      }
    },

    async dispatch<T>(
      descriptor: BackendDescriptor,
      path: string,
      send: (headers: Record<string, string>) => Promise<T>
    ): Promise<T> {
      const grant = grants.get(descriptor.baseUrl)

      if (!grant || !localTalkRoute(descriptor, path) || descriptor.token !== grant.gatewayToken) {
        return send({})
      }

      if (!grant.isCurrent()) {
        grants.delete(descriptor.baseUrl)

        return send({})
      }

      try {
        return await send({ [TOKEN_HEADER]: grant.token })
      } catch (error) {
        const safe = new Error(
          String(error instanceof Error ? error.message : error)
            .split(grant.token)
            .join('[redacted]')
        )

        if (error instanceof Error) {
          safe.name = error.name
        }

        throw safe
      }
    }
  }
}
