/**
 * 面板分隔条：按住左右拖动改变相邻面板宽度（TraeCode 式交互）。
 *
 * 设计要点
 * - 拖拽期间只回调 ``onPreview``（父组件直接写 CSS 变量），**不触发 React 重渲染**，
 *   长会话 / 大文件树下依然顺滑；松手时才 ``onChange`` 落到状态（并写 localStorage）；
 * - 双击 = 收起该面板（``onToggle``）；展开入口由各栏既有按钮负责；
 * - 键盘可达：聚焦后 ←/→ 每次 16px，``role="separator"`` 暴露当前宽度。
 */

import { useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'

const STEP = 16

export interface PanelResizerProps {
  /** 当前宽度（px） */
  value: number
  min: number
  max: number
  /** 面板在分隔条的哪一侧：left = 面板在左（拖右变宽） */
  side: 'left' | 'right'
  label: string
  onChange: (next: number) => void
  /** 拖拽过程中的实时预览（写 CSS 变量，不走状态） */
  onPreview?: (next: number) => void
  /** 双击分隔条 */
  onToggle?: () => void
  className?: string
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, Math.round(value)))
}

export default function PanelResizer({
  value,
  min,
  max,
  side,
  label,
  onChange,
  onPreview,
  onToggle,
  className,
}: PanelResizerProps) {
  const [dragging, setDragging] = useState(false)
  const dragRef = useRef<{ startX: number; startValue: number; next: number } | null>(null)

  const resolve = (clientX: number): number => {
    const state = dragRef.current
    if (!state) return value
    const delta = clientX - state.startX
    return clamp(side === 'left' ? state.startValue + delta : state.startValue - delta, min, max)
  }

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    dragRef.current = { startX: event.clientX, startValue: value, next: value }
    setDragging(true)
    document.body.classList.add('panel-resizing')
  }

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return
    const next = resolve(event.clientX)
    dragRef.current.next = next
    onPreview?.(next)
  }

  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    const state = dragRef.current
    if (!state) return
    dragRef.current = null
    setDragging(false)
    document.body.classList.remove('panel-resizing')
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    if (state.next !== value) onChange(state.next)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    // 视觉语义：← 等于把分隔条往左推 —— 左侧面板变窄、右侧面板变宽
    const direction = event.key === 'ArrowLeft' ? -1 : 1
    const next = clamp(side === 'left' ? value + direction * STEP : value - direction * STEP, min, max)
    if (next !== value) onChange(next)
  }

  return (
    // ui-style-ok: 分隔条本身无内联色值，样式全部走 .panel-resizer 类与 token
    <div
      className={[
        'panel-resizer',
        dragging ? 'is-dragging' : '',
        className ?? '',
      ]
        .filter(Boolean)
        .join(' ')}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      title={`${label}（拖动调整，双击收起）`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onDoubleClick={() => onToggle?.()}
      onKeyDown={onKeyDown}
    />
  )
}