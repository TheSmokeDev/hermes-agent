import { createElement, type ReactNode, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'
import { X } from '@/lib/icons'
import { $pluginVoiceRenderers, type PluginVoiceSurfaceProps } from '@/store/plugin-voice'

import { useHudClickThrough } from './click-through'
import { useHudComposerDrag } from './composer-drag'
import { createPluginVoiceController } from './plugin-voice-controller'
import { watchPluginVoiceLifetime } from './plugin-voice-lifetime'

interface MountedVoice extends PluginVoiceSurfaceProps {
  render: (props: PluginVoiceSurfaceProps) => ReactNode
}

export function PluginVoiceHud() {
  const { t } = useI18n()
  const [mounted, setMounted] = useState<MountedVoice | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const windowing = window.hermesDesktop?.hud?.windowing
  const { onPointerDown } = useHudComposerDrag(windowing?.nativeDrag !== true, windowing)

  // The surface paints only what it needs; the rest of the window is empty and hands the mouse through.
  useHudClickThrough(rootRef)

  useEffect(() => {
    let cancelled = false
    let dispose: (() => void) | undefined
    const api = window.hermesDesktop?.hud?.voice

    void api?.get().then(async descriptor => {
      if (cancelled || !descriptor) {
        return
      }

      const resource = createPluginVoiceController(descriptor)
      const stopWatching = watchPluginVoiceLifetime(descriptor, resource)
      let previous: MountedVoice['render'] | undefined
      let prepared = false

      const publish = () => {
        const render = $pluginVoiceRenderers.get().get(descriptor.pluginId)

        if (previous && render !== previous) {
          resource.controller.stop()
        } else if (prepared && render && !resource.controller.signal.aborted && !previous) {
          previous = render
          setMounted({ controller: resource.controller, render })
        }
      }

      const offRenderers = $pluginVoiceRenderers.listen(publish)

      // A missing/disabled plugin cannot leave a blank, immortal HUD after disk discovery settles.
      const registrationDeadline = window.setTimeout(() => {
        if (!previous) {
          resource.controller.stop()
        }
      }, 30_000)

      dispose = () => {
        window.clearTimeout(registrationDeadline)
        offRenderers()
        stopWatching()
      }

      try {
        await resource.controller.prepareSession()
        prepared = true
        publish()
      } catch {
        resource.controller.stop()
      }
    }).catch(() => void window.hermesDesktop?.hud?.close())

    return () => {
      cancelled = true
      dispose?.()
    }
  }, [])

  return (
    <div className="h-screen w-screen overflow-hidden text-foreground" ref={rootRef}>
      <div className="inline-flex max-h-screen max-w-full flex-col overflow-hidden rounded-lg bg-background">
        <div className={`flex shrink-0 items-center justify-end ${windowing?.nativeDrag ? '[-webkit-app-region:drag]' : ''}`} onPointerDown={onPointerDown}>
          <Button aria-label={t.common.close} onClick={() => mounted ? mounted.controller.stop() : void window.hermesDesktop?.hud?.close()} size="icon-sm"
            variant="ghost">
            <X />
          </Button>
        </div>
        {mounted ? createElement(mounted.render, { controller: mounted.controller }) : <Loader label={t.common.loading} />}
      </div>
    </div>
  )
}
