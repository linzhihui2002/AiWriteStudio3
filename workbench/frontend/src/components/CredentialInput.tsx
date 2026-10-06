import { useEffect, useId, useRef, useState } from 'react'
import type { InputHTMLAttributes } from 'react'
import { CREDENTIAL_FIELD_EXTRA, NO_AUTOFILL } from '../lib/autofill'
import '../styles/credentialInput.css'

export interface CredentialInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'autoComplete' | 'value' | 'defaultValue'> {
  /** 只接收本次输入的凭据；已保存的凭据使用 placeholder 展示掩码，不作为 value 回填。 */
  value: string
  /** 显示 / 隐藏按钮的可访问名称，默认使用 API Key。 */
  credentialLabel?: string
}

/**
 * 本机配置凭据输入框：使用普通文本输入与视觉掩码，避免触发浏览器登录密码保存流程。
 * 支持原生 input 的编辑、粘贴、标签和事件；只有用户主动显示时才呈现明文，失焦自动隐藏。
 */
export default function CredentialInput({
  value,
  credentialLabel = 'API Key',
  className = '',
  id,
  disabled,
  ...inputProps
}: CredentialInputProps) {
  const generatedId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const inputId = id || `credential-${generatedId}`
  const [revealed, setRevealed] = useState(false)
  const actionLabel = `${revealed ? '隐藏' : '显示'} ${credentialLabel}`

  useEffect(() => {
    if (!value || disabled) setRevealed(false)
  }, [value, disabled])

  useEffect(() => {
    const hide = () => setRevealed(false)
    window.addEventListener('blur', hide)
    document.addEventListener('visibilitychange', hide)
    return () => {
      window.removeEventListener('blur', hide)
      document.removeEventListener('visibilitychange', hide)
    }
  }, [])

  return (
    <div
      className={`credential-input${revealed ? ' is-visible' : ''}`}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setRevealed(false)
      }}
    >
      <input
        {...inputProps}
        {...CREDENTIAL_FIELD_EXTRA}
        ref={inputRef}
        id={inputId}
        className={`input credential-input__field ${className}`.trim()}
        type="text"
        autoComplete={NO_AUTOFILL}
        autoCapitalize="none"
        spellCheck={false}
        disabled={disabled}
        value={value}
      />
      {!revealed && value ? (
        <span className="credential-input__fallback-mask" aria-hidden="true">••••••••</span>
      ) : null}
      <button
        className="credential-input__toggle"
        type="button"
        disabled={disabled}
        aria-label={actionLabel}
        aria-pressed={revealed}
        aria-controls={inputId}
        title={actionLabel}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => {
          inputRef.current?.focus()
          setRevealed((current) => !current)
        }}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
          <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" />
          <circle cx="12" cy="12" r="3" />
          {revealed ? <path d="m3 3 18 18" /> : null}
        </svg>
      </button>
    </div>
  )
}
