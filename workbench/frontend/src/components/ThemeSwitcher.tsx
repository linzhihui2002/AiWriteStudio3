import type { Theme } from '../theme/useTheme'
import { useSettings } from '../state/useSettings'

const OPTIONS: ReadonlyArray<{ value: Theme; label: string; hint: string }> = [
  { value: 'light', label: '亮', hint: '亮色主题' },
  { value: 'dark', label: '暗', hint: '暗色主题' },
  { value: 'paper', label: '纸', hint: '纸感阅读主题（长时间写作）' },
]

/** 顶栏极简主题切换（亮 / 暗 / 纸）：走全局设置，跨会话持久化。 */
export default function ThemeSwitcher() {
  const { settings, setTheme } = useSettings()
  const current = (settings?.appearance.theme ?? 'light') as Theme

  return (
    <div className="theme-switch" role="group" aria-label="主题切换">
      {OPTIONS.map((option) => (
        <button
          key={option.value}
          type="button"
          className="theme-switch__btn"
          aria-pressed={current === option.value}
          title={option.hint}
          onClick={() => void setTheme(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}