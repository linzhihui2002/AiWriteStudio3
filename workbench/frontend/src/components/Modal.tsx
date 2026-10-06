/** 通用弹窗：遮罩 + 面板 + Esc 关闭 + 焦点落位（键盘可达）。 */

import { useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { OverlayDepthProvider, useOverlayFocus } from '../state/useOverlayFocus'

export interface ModalProps {
  title: string
  open: boolean
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
  className?: string
}

export default function Modal({ title, open, onClose, children, footer, width = 720, className = '' }: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  useOverlayFocus(panelRef, open, onClose)

  if (!open) return null

  return createPortal(
    <div data-workbench-overlay className="modal-overlay" onMouseDown={onClose} role="presentation">
      <div
        className={`modal ${className}`.trim()}
        style={{ maxWidth: width }}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panelRef}
      >
        <OverlayDepthProvider>
        <header className="modal__header">
          <h2 className="modal__title">{title}</h2>
          <button className="btn btn--ghost btn--sm" type="button" onClick={onClose}>
            关闭
          </button>
        </header>
        <div className="modal__body">{children}</div>
        {footer ? <footer className="modal__footer">{footer}</footer> : null}
        </OverlayDepthProvider>
      </div>
    </div>, document.body
  )
}
