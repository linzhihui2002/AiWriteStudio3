/** 通用弹窗：遮罩 + 面板 + Esc 关闭 + 焦点落位（键盘可达）。 */

import { useEffect, useRef, type ReactNode } from 'react'

export interface ModalProps {
  title: string
  open: boolean
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
}

export default function Modal({ title, open, onClose, children, footer, width = 720 }: ModalProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  // 用 ref 持有最新 onClose：调用方常传内联函数，引用每次渲染都变，
  // 若直接放进 effect 依赖会导致 effect 反复重跑、焦点被抢回面板（输入框只能敲一个字符）。
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        onCloseRef.current()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [open])

  // 打开时把焦点移进面板（键盘可达）；仅在 open 变化时执行一次，避免抢占输入框焦点
  useEffect(() => {
    if (open) panelRef.current?.focus()
  }, [open])

  if (!open) return null

  return (
    <div className="modal-overlay" onMouseDown={onClose} role="presentation">
      <div
        className="modal"
        style={{ maxWidth: width }}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panelRef}
      >
        <header className="modal__header">
          <h2 className="modal__title">{title}</h2>
          <button className="btn btn--ghost btn--sm" type="button" onClick={onClose}>
            关闭
          </button>
        </header>
        <div className="modal__body">{children}</div>
        {footer ? <footer className="modal__footer">{footer}</footer> : null}
      </div>
    </div>
  )
}