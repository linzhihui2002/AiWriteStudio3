/**
 * 应用内单行输入弹窗：替代 window.prompt（原生对话框阻塞、样式脱离应用）。
 *
 * 用法（在根组件调用一次，根 JSX 末尾渲染 node）：
 *   const [prompt, promptNode] = usePrompt()
 *   const name = await prompt({ title: '另存为模板', label: '模板名称' })
 * 确认为 trim 后的文本，取消返回 null；allowEmpty 为真时空值也可确认。
 */

import { useEffect, useRef, useState, type ReactNode } from 'react'
import Modal from './Modal'
import { NO_AUTOFILL } from '../lib/autofill'

export interface PromptOptions {
  title: string
  label?: string
  defaultValue?: string
  placeholder?: string
  confirmText?: string
  cancelText?: string
  allowEmpty?: boolean
}

export interface PromptDialogProps extends PromptOptions {
  open: boolean
  onConfirm: (value: string) => void
  onCancel: () => void
}

export function PromptDialog({
  open,
  title,
  label,
  defaultValue = '',
  placeholder,
  confirmText,
  cancelText,
  allowEmpty = false,
  onConfirm,
  onCancel,
}: PromptDialogProps) {
  const [value, setValue] = useState(defaultValue)
  const inputRef = useRef<HTMLInputElement>(null)
  const canConfirm = allowEmpty || value.trim().length > 0

  // 打开时重置默认值并把焦点落到输入框；延后一拍，避免被 Modal 的面板焦点抢走
  useEffect(() => {
    if (!open) return
    setValue(defaultValue)
    const timer = window.setTimeout(() => {
      inputRef.current?.focus()
      inputRef.current?.select()
    }, 0)
    return () => window.clearTimeout(timer)
  }, [open, defaultValue])

  return (
    <Modal title={title} open={open} onClose={onCancel} width={480}>
      <div className="stack">
        {label ? <span className="field__label">{label}</span> : null}
        <input
          ref={inputRef}
          className="input"
          autoComplete={NO_AUTOFILL}
          value={value}
          placeholder={placeholder}
          onChange={(event) => setValue(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && canConfirm) onConfirm(value.trim())
          }}
        />
        <div className="btn-row btn-row--end">
          <button className="btn btn--ghost btn--sm" type="button" onClick={onCancel}>
            {cancelText ?? '取消'}
          </button>
          <button
            className="btn btn--primary btn--sm"
            type="button"
            disabled={!canConfirm}
            onClick={() => onConfirm(value.trim())}
          >
            {confirmText ?? '确定'}
          </button>
        </div>
      </div>
    </Modal>
  )
}

interface PendingPrompt extends PromptOptions {
  resolve: (value: string | null) => void
}

export function usePrompt(): [(options: PromptOptions) => Promise<string | null>, ReactNode] {
  const [pending, setPending] = useState<PendingPrompt | null>(null)

  const prompt = (options: PromptOptions) =>
    new Promise<string | null>((resolve) => setPending({ ...options, resolve }))

  const settle = (value: string | null) => {
    pending?.resolve(value)
    setPending(null)
  }

  const node = (
    <PromptDialog
      open={Boolean(pending)}
      title={pending?.title ?? ''}
      label={pending?.label}
      defaultValue={pending?.defaultValue}
      placeholder={pending?.placeholder}
      confirmText={pending?.confirmText}
      cancelText={pending?.cancelText}
      allowEmpty={pending?.allowEmpty}
      onConfirm={(value) => settle(value)}
      onCancel={() => settle(null)}
    />
  )

  return [prompt, node]
}