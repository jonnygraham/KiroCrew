import { createElement, useCallback, useEffect, useRef, type ReactNode, type RefObject } from 'react'

import { useConfirm } from '../components/ConfirmDialog'
import { i18nT } from '../i18n/t'
import { hasGesture, OPEN_MESSAGE_TYPE, openExternal, useFrameOpenLink } from './useFrameOpenLink'

/**
 * The web URL an open message asks for, or null when it is not one the link
 * bridge honours.
 *
 * Only an absolute `http:` or `https:` URL passes. `javascript:`, `data:`,
 * `blob:`, `file:`, `mailto:` and every custom scheme are refused, and so is a
 * URL carrying a user name or password, whose `user@host` form makes the host
 * part hard to read in the confirm. The returned value is the PARSED form, so
 * the URL the person reads in the confirm is exactly the one that opens.
 */
export function webUrlOf(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null
  const msg = data as { type?: unknown; url?: unknown }
  if (msg.type !== OPEN_MESSAGE_TYPE || typeof msg.url !== 'string') return null
  if (!msg.url) return null
  let parsed: URL
  try {
    parsed = new URL(msg.url)
  } catch {
    return null
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null
  if (parsed.username || parsed.password) return null
  return parsed.href
}

/**
 * The host half of the link bridge for frames that hold agent-written pages
 * (saved artifacts, a teammate's shared artifact, chat widgets).
 *
 * Those frames are `sandbox="allow-scripts"` with no popup grant, so a page cannot
 * open a window itself. `buildSrcdoc({ linkBridge: true })` turns a `_blank` or
 * bare web link click, and a `window.open` call, into the same
 * `kirocrew-dashboard:open` message the crew pages use. This hook answers it:
 *
 * - only from one of the given frames (`event.source`);
 * - only for a web URL (`webUrlOf`);
 * - only while the person has just clicked or pressed a key in that frame
 *   (`hasGesture`), so a page posting on its own opens nothing;
 * - and only after the person confirms a dialog that shows the full URL, so a
 *   page that ties an open to an unrelated click still cannot open anything the
 *   person did not see and accept.
 *
 * Render the returned node once, next to the frames.
 */
export function useFrameLinkBridge(
  frames: ReadonlyArray<RefObject<HTMLIFrameElement | null>>,
  open: (url: string) => void = openExternal,
  gesture: (frame: HTMLIFrameElement) => boolean = hasGesture,
): ReactNode {
  const { confirm, confirmDialog, confirmOpen, cancelConfirm } = useConfirm()
  const openRef = useRef(open)
  openRef.current = open
  // The frame, URL and gesture checks are useFrameOpenLink's own listener; this
  // hook adds the web-URL rule and puts the confirm in front of the open.
  const ask = useCallback((url: string) => {
    void confirm({
      title: i18nT('hooks.frameLinkBridge.title'),
      body: createElement(
        'code',
        { className: 'block break-all font-mono text-xs text-[var(--text)]', translate: 'no' },
        url,
      ),
      confirmLabel: i18nT('hooks.frameLinkBridge.open'),
      danger: false,
      primary: true,
      // The artifact frame also renders inside ArtifactPanel's full-screen shell
      // (an opaque portal at z-[9999]); the prompt must paint above it.
      layer: 'top',
      // Focus must not go back to the frame: the click on Cancel is itself a
      // live activation, and with the frame focused again a page posting on a
      // loop would pass the gesture test and reopen the prompt at once. On the
      // body, the next prompt needs a new click in the frame.
      restoreFocus: false,
    }).then((ok) => {
      if (ok) openRef.current(url)
    })
  }, [confirm])
  useFrameOpenLink(frames, ask, gesture, webUrlOf)
  // Escape on this prompt is consumed here, at window capture, before any
  // document listener underneath sees it: ArtifactPanel's full-screen shell
  // exits full screen on Escape, which would unmount the frame and drop what
  // the person typed into the page just to cancel a link.
  useEffect(() => {
    if (!confirmOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing) return
      e.stopPropagation()
      e.preventDefault()
      cancelConfirm()
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [confirmOpen, cancelConfirm])
  return confirmDialog
}
