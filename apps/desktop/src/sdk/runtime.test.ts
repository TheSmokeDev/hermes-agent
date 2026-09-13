import { Blob } from 'node:buffer'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { runInNewContext } from 'node:vm'

import { rolldown } from 'rolldown'
import { expect, it } from 'vitest'

it('resolves the SDK after its bundled inventory cycle initializes, retaining singleton namespaces', async () => {
  const modules: Record<string, string> = {
    'entry.ts': `import * as sdk from './index'; export {sdk}; export {installPluginSdk,sdkImportMap} from './runtime';`,
    'index.ts': `export {SkillsView} from './skills'; export const Button = 'button'; export function useComposerVoiceController() {return 'lease';}`,
    'skills.ts': `import {sdkImportMap} from './runtime'; export function SkillsView() {return sdkImportMap();}`,
    'react.ts': `export function createElement() {}; export function useState() {}; export const version = 'fixture';`,
    'runtime.ts': readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'runtime.ts'), 'utf8')
  }

  const bundle = await rolldown({
    input: 'entry.ts',
    plugins: [
      {
        name: 'sdk-inventory-cycle',
        resolveId(id) {
          return id.startsWith('react')
            ? 'react.ts'
            : id.replace(/^\.\//, '').replace(/^(index|skills|runtime)$/, '$1.ts')
        },
        load: id => modules[id]
      }
    ]
  })

  try {
    const { output } = await bundle.generate({ format: 'iife', name: 'SDKFixture', minify: true })
    const chunk = output.find(entry => entry.type === 'chunk')

    if (!chunk) {
      throw new Error('SDK fixture bundle did not contain JavaScript')
    }

    const sources = new Map<string, Blob>()

    const context: Record<string, any> = {
      Blob,
      URL: {
        createObjectURL(blob: Blob) {
          const url = `blob:fixture-${sources.size}`
          sources.set(url, blob)

          return url
        }
      }
    }

    runInNewContext(chunk.code, context)
    context.SDKFixture.installPluginSdk()
    expect(context.__HERMES_PLUGIN_SDK__).toBe(context.SDKFixture.sdk)
    expect(context.__HERMES_PLUGIN_SDK__.useComposerVoiceController()).toBe('lease')
    const react = context.__HERMES_REACT__
    const imports = context.SDKFixture.sdkImportMap()
    expect(Object.keys(imports)).toEqual(['@hermes/plugin-sdk', 'react/jsx-dev-runtime', 'react/jsx-runtime', 'react'])
    expect(await sources.get(imports['@hermes/plugin-sdk'])!.text()).toContain('useComposerVoiceController')
    context.SDKFixture.installPluginSdk()
    expect(context.__HERMES_REACT__).toBe(react)
    expect(context.SDKFixture.sdkImportMap()).toBe(imports)
  } finally {
    await bundle.close()
  }
})
