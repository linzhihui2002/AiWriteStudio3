/** 就地行内编辑：把「只要一个名字」的交互从弹窗改为原位输入。
 *
 * 交互纪律：Enter 提交、Esc 取消；不做 blur 提交（避免误提交）。
 * 用法：
 *   <InlineEdit defaultValue={name} ariaLabel="新名称" onCommit={(value) => rename(value)} onCancel={() => setEditing(null)} />
 */

import { useState, type KeyboardEvent } from 'react'
import { NO_AUTOFILL } from '../lib/autofill'

export interface InlineEditProps {
  defaultValue?: string
  placeholder?: string
  ariaLabel?: string
  maxLength?: number
  className?: string
  allowEmpty?: boolean
  onCommit: (value: string) => void
  onCancel: () => void
}

export default function InlineEdit({
  defaultValue = '',
  placeholder,
  ariaLabel,
  maxLength,
  className = '',
  allowEmpty = false,
  onCommit,
  onCancel,
}: InlineEditProps) {
  const [value, setValue] = useState(defaultValue)

  const commit = () => {
    const next = value.trim()
    if (!next && !allowEmpty) {
      onCancel()
      return
    }
    if (next === defaultValue.trim()) {
      onCancel()
      return
    }
    onCommit(next)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      commit()
    }
    if (event.key === 'Escape') {
      event.preventDefault()
      event.stopPropagation()
      onCancel()
    }
  }

  return (
    <input
      className={`input inline-edit ${className}`.trim()}
      autoFocus
      autoComplete={NO_AUTOFILL}
      value={value}
      placeholder={placeholder}
      aria-label={ariaLabel}
      maxLength={maxLength}
      onChange={(event) => setValue(event.target.value)}
      onFocus={(event) => event.currentTarget.select()}
      onKeyDown={onKeyDown}
    />
  )
}
