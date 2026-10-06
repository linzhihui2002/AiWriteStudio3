/** 右侧抽屉：非阻塞的详情 / 长内容 / 大表单容器（遮罩点击或 Esc 关闭，键盘可达）。
 *
 * 与 Modal 的分工：需要用户「决策后才能继续」或短表单用 Modal；
 * 以阅读、对照、长内容浏览为主的场景用 Drawer，避免居中遮罩遮挡上下文。
 */

import { useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { OverlayDepthProvider, useOverlayFocus } from '../state/useOverlayFocus'

export interface DrawerProps {
  title: string
  open: boolean
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  width?: number
}

export default function Drawer({ title, open, onClose, children, footer, width = 720 }: DrawerProps) {
  const panelRef = useRef<HTMLDivElement>(null)
  useOverlayFocus(panelRef, open, onClose)

  if (!open) return null

  return createPortal(
    <div data-workbench-overlay className="drawer-overlay" onMouseDown={onClose} role="presentation">
      <div
        className="drawer"
        style={{ maxWidth: width }}
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        ref={panelRef}
      >
        <OverlayDepthProvider>
        <header className="drawer__header">
          <h2 className="drawer__title">{title}</h2>
          <button className="btn btn--ghost btn--sm" type="button" onClick={onClose}>
            关闭
          </button>
        </header>
        <div className="drawer__body">{children}</div>
        {footer ? <footer className="drawer__footer">{footer}</footer> : null}
        </OverlayDepthProvider>
      </div>
    </div>, document.body
  )
}
