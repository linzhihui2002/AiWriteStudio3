import { createContext, createElement, useContext, useLayoutEffect, useRef, type ReactNode, type RefObject } from 'react'

type Overlay = { panel: HTMLElement; root: HTMLElement; priority: number; close: () => void }
const OverlayDepth = createContext(0)
export function OverlayDepthProvider({ children }: { children: ReactNode }) {
  const depth = useContext(OverlayDepth)
  return createElement(OverlayDepth.Provider, { value: depth + 1 }, children)
}
const overlays: Overlay[] = []
const previousInert = new Map<HTMLElement, boolean>()
const focusable = 'button:not(:disabled), a[href], input:not(:disabled), textarea:not(:disabled), select:not(:disabled), [tabindex]:not([tabindex="-1"])'

function controls(panel: HTMLElement) {
  return Array.from(panel.querySelectorAll<HTMLElement>(focusable)).filter(el => !el.closest('[inert]') && !el.hidden && el.getClientRects().length > 0)
}
function syncInert() {
  previousInert.forEach((value, element) => { element.inert = value })
  previousInert.clear()
  const top = overlays[overlays.length - 1]
  if (!top) return
  let current = top.root
  while (current.parentElement) {
    for (const sibling of current.parentElement.children) {
      if (sibling !== current && sibling instanceof HTMLElement) {
        previousInert.set(sibling, sibling.inert)
        sibling.inert = true
      }
    }
    if (current.parentElement === document.body) break
    current = current.parentElement
  }
}
function keyboard(event: KeyboardEvent) {
  const top = overlays[overlays.length - 1]
  if (!top) return
  if (event.key === 'Escape' && !event.isComposing) {
    const disclosure = event.target instanceof HTMLElement ? event.target.closest<HTMLDetailsElement>('details.workspace-more[open], details.chat__session-menu[open]') : null
    if (disclosure) {
      event.preventDefault(); event.stopImmediatePropagation(); disclosure.open = false
      disclosure.querySelector<HTMLElement>('summary')?.focus()
      return
    }
    event.preventDefault(); event.stopImmediatePropagation(); top.close()
  } else if (event.key === 'Tab') {
    const items = controls(top.panel)
    const first = items[0] ?? top.panel; const last = items[items.length - 1] ?? top.panel
    if (!top.panel.contains(document.activeElement) || (event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault(); (event.shiftKey ? last : first).focus()
    }
  }
}
function containFocus(event: FocusEvent) {
  const top = overlays[overlays.length - 1]
  if (top && event.target instanceof Node && !top.panel.contains(event.target)) (controls(top.panel)[0] ?? top.panel).focus()
}

/** Shared stack: only the top dialog receives keys; background and underlying dialogs are inert. */
export function useOverlayFocus(panelRef: RefObject<HTMLElement>, open: boolean, onClose: () => void) {
  const depth = useContext(OverlayDepth)
  const closeRef = useRef(onClose)
  closeRef.current = onClose
  useLayoutEffect(() => {
    const panel = panelRef.current
    if (!open || !panel) return
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const root = panel.closest<HTMLElement>('[data-workbench-overlay]') ?? panel
    // A hidden navigation becomes active before its later passive effect runs.
    panel.inert = false; root.inert = false
    const entry = { panel, root, priority: (Number.parseInt(getComputedStyle(root).zIndex) || 0) + depth, close: () => closeRef.current() }
    overlays.push(entry)
    overlays.sort((a, b) => a.priority - b.priority)
    syncInert()
    document.addEventListener('keydown', keyboard, true)
    document.addEventListener('focusin', containFocus, true)
    const top = overlays[overlays.length - 1]
    if (top === entry) (panel.querySelector<HTMLElement>('[data-autofocus]') ?? panel).focus()
    return () => {
      const index = overlays.indexOf(entry)
      if (index >= 0) overlays.splice(index, 1)
      syncInert()
      if (!overlays.length) {
        document.removeEventListener('keydown', keyboard, true)
        document.removeEventListener('focusin', containFocus, true)
      }
      if (previousFocus?.isConnected && !previousFocus.closest('[inert]')) previousFocus.focus()
      else overlays[overlays.length - 1]?.panel.focus()
    }
  }, [depth, open, panelRef])
}
