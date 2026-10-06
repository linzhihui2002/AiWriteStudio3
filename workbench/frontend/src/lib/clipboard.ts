/** Copy during the user's click without requesting browser clipboard permission. */
export function copyTextToClipboard(text: string): void {
  if (typeof document === 'undefined' || typeof document.execCommand !== 'function') {
    throw new Error('当前浏览器无法直接复制，请选择文字后手动复制。')
  }

  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null
  const input = active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement ? active : null
  const inputSelection = input && input.selectionStart !== null && input.selectionEnd !== null
    ? { start: input.selectionStart, end: input.selectionEnd, direction: input.selectionDirection ?? 'none' }
    : null
  const selection = document.getSelection()
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange()) : []
  const direction = selection?.anchorNode && selection.focusNode
    ? { anchor: selection.anchorNode, anchorOffset: selection.anchorOffset, focus: selection.focusNode, focusOffset: selection.focusOffset }
    : null
  const buffer = document.createElement('textarea')
  // Keep the temporary field inside the active overlay's focus boundary.
  // A body-level field would be redirected by the dialog focus trap before copy.
  const host = active?.closest<HTMLElement>('[role="dialog"], [role="alertdialog"]') ?? document.body
  buffer.value = text
  buffer.readOnly = true
  buffer.tabIndex = -1
  buffer.setAttribute('aria-hidden', 'true')
  buffer.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;pointer-events:none;'

  try {
    host.appendChild(buffer)
    buffer.focus({ preventScroll: true })
    buffer.select()
    buffer.setSelectionRange(0, text.length)
    if (!document.execCommand('copy')) throw new Error('复制失败，请选择文字后手动复制。')
  } finally {
    buffer.remove()
    if (selection) {
      selection.removeAllRanges()
      for (const range of ranges) selection.addRange(range)
      if (ranges.length === 1 && direction?.anchor.isConnected && direction.focus.isConnected && typeof selection.setBaseAndExtent === 'function') {
        selection.setBaseAndExtent(direction.anchor, direction.anchorOffset, direction.focus, direction.focusOffset)
      }
    }
    if (active?.isConnected) active.focus({ preventScroll: true })
    if (input?.isConnected && inputSelection) {
      input.setSelectionRange(inputSelection.start, inputSelection.end, inputSelection.direction)
    }
  }
}
