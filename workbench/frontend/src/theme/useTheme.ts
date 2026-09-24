import { useCallback, useEffect, useState } from 'react'

/** 可用主题：亮色 / 暗色 / 纸感阅读（M9 将在此扩展护眼参数）。 */
export type Theme = 'light' | 'dark' | 'paper'

export const THEMES: readonly Theme[] = ['light', 'dark', 'paper']

/** localStorage 持久化键；index.html 首屏内联脚本使用同一键名。 */
const STORAGE_KEY = 'aiw.theme'

function isTheme(value: unknown): value is Theme {
  return value === 'light' || value === 'dark' || value === 'paper'
}

function readStoredTheme(): Theme | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return isTheme(value) ? value : null
  } catch {
    return null
  }
}

function prefersDark(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  )
}

/** 将主题写入 <html data-theme="...">，CSS 变量据此切换。 */
export function applyTheme(theme: Theme): void {
  document.documentElement.setAttribute('data-theme', theme)
}

/**
 * 主题状态：读取 localStorage（无值时跟随系统偏好），
 * 变更时同步 data-theme 属性并持久化。
 */
export function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => readStoredTheme() ?? (prefersDark() ? 'dark' : 'light'))

  useEffect(() => {
    applyTheme(theme)
    try {
      window.localStorage.setItem(STORAGE_KEY, theme)
    } catch {
      // 隐私模式下 localStorage 可能不可写，忽略即可（主题仍在本会话生效）
    }
  }, [theme])

  const cycleTheme = useCallback(() => {
    setTheme((current) => THEMES[(THEMES.indexOf(current) + 1) % THEMES.length])
  }, [])

  return { theme, setTheme, cycleTheme, themes: THEMES }
}
