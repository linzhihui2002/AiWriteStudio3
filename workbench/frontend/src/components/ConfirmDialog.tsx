/**
 * 应用内确认弹窗：替代 window.confirm（原生对话框阻塞、样式脱离应用）。
 *
 * 用法（在根组件调用一次，根 JSX 末尾渲染 node）：
 *   const [confirm, confirmNode] = useConfirm()
 *   if (!(await confirm({ title: '删除供应商', message: `确定删除「${id}」吗？`, danger: true }))) return
 */

import { useState, type ReactNode } from 'react'
import Modal from './Modal'

export interface ConfirmOptions {
  title: string
  message: string
  confirmText?: string
  cancelText?: string
  danger?: boolean
}

export interface ConfirmDialogProps extends ConfirmOptions {
  open: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmText,
  cancelText,
  danger,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal title={title} open={open} onClose={onCancel} width={480}>
      <div className="stack">
        <p className="dialog__message">{message}</p>
        <div className="btn-row btn-row--end">
          <button className="btn btn--ghost btn--sm" type="button" onClick={onCancel}>
            {cancelText ?? '取消'}
          </button>
          <button
            className={`btn btn--sm ${danger ? 'btn--danger' : 'btn--primary'}`}
            type="button"
            onClick={onConfirm}
          >
            {confirmText ?? '确定'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

interface PendingConfirm extends ConfirmOptions {
  resolve: (ok: boolean) => void
}

export function useConfirm(): [(options: ConfirmOptions) => Promise<boolean>, ReactNode] {
  const [pending, setPending] = useState<PendingConfirm | null>(null)

  const confirm = (options: ConfirmOptions) =>
    new Promise<boolean>((resolve) => setPending({ ...options, resolve }))

  const settle = (ok: boolean) => {
    pending?.resolve(ok)
    setPending(null)
  }

  const node = (
    <ConfirmDialog
      open={Boolean(pending)}
      title={pending?.title ?? ''}
      message={pending?.message ?? ''}
      confirmText={pending?.confirmText}
      cancelText={pending?.cancelText}
      danger={pending?.danger}
      onConfirm={() => settle(true)}
      onCancel={() => settle(false)}
    />
  )

  return [confirm, node]
}