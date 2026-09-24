/** 轻量 Toast：全局提示（成功/失败/提示），2.6s 自动消失。 */

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

export type ToastKind = 'info' | 'success' | 'error'

export interface ToastItem {
  id: number
  kind: ToastKind
  message: string
}

interface ToastContextValue {
  toasts: ToastItem[]
  push: (message: string, kind?: ToastKind) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

let counter = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  const push = useCallback((message: string, kind: ToastKind = 'info') => {
    counter += 1
    const item: ToastItem = { id: counter, kind, message }
    setToasts((current) => [...current, item])
    window.setTimeout(() => {
      setToasts((current) => current.filter((entry) => entry.id !== item.id))
    }, 2600)
  }, [])

  const value = useMemo(() => ({ toasts, push }), [toasts, push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack" role="status" aria-live="polite">
        {toasts.map((item) => (
          <div key={item.id} className={`toast toast--${item.kind}`}>
            {item.message}
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