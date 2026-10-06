/** 轻量 Toast：全局提示（成功/失败/提示），默认 2.6s 自动消失。
 *
 * 带 action（如「撤销」）时延长到 8s，点击 action 先关闭该条再执行回调，
 * 用于「可逆操作直接执行 + 撤销」这种非阻塞交互。
 */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

export type ToastKind = 'info' | 'success' | 'error'

export interface ToastAction {
  label: string
  onAction: () => void
}

export interface ToastItem {
  id: number
  kind: ToastKind
  message: string
  action?: ToastAction
}

interface ToastContextValue {
  toasts: ToastItem[]
  push: (message: string, kind?: ToastKind, action?: ToastAction) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

const DEFAULT_DURATION_MS = 2600
const ACTION_DURATION_MS = 8000

let counter = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  const push = useCallback((message: string, kind: ToastKind = 'info', action?: ToastAction) => {
    counter += 1
    const item: ToastItem = { id: counter, kind, message, action }
    setToasts((current) => [...current, item])
    window.setTimeout(() => {
      setToasts((current) => current.filter((entry) => entry.id !== item.id))
    }, action ? ACTION_DURATION_MS : DEFAULT_DURATION_MS)
  }, [])

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((entry) => entry.id !== id))
  }, [])

  const value = useMemo(() => ({ toasts, push }), [toasts, push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((item) => (
          <div key={item.id} className={`toast toast--${item.kind}`}>
            <span className="toast__message">{item.message}</span>
            {item.action ? (
              <button
                className="toast__action"
                type="button"
                onClick={() => {
                  dismiss(item.id)
                  item.action?.onAction()
                }}
              >
                {item.action.label}
              </button>
            ) : null}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast 必须在 ToastProvider 内使用')
  return context
}

/** 统一错误消息提取（ApiError / Error / 字符串）。 */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message
  if (typeof error === 'string') return error
  return '未知错误'
}
