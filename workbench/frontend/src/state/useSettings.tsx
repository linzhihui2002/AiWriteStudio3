/**
 * 全局设置（后端 `.workbench/settings.json`）+ 护眼/外观落地。
 *
 * 设计：设置由后端持有（跨设备/跨浏览器一致），前端启动时拉取一次，
 * 修改后立即 PATCH 并广播给所有消费者（上下文 + CSS 变量）。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { getSettings, updateSettings } from '../api/client'
import type { Settings } from '../api/types'
import { applyAppearance } from './useAppearance'
import { applyTheme, type Theme } from '../theme/useTheme'

interface SettingsContextValue {
  settings: Settings | null
  loading: boolean
  error: string
  refresh: () => Promise<void>
  patch: (patch: Record<string, unknown>) => Promise<Settings | null>
  setTheme: (theme: Theme) => Promise<void>
}

const SettingsContext = createContext<SettingsContextValue | null>(null)

const THEMES: Theme[] = ['light', 'dark', 'paper']

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    try {
      const data = await getSettings()
      setSettings(data)
      setError('')
    } catch (err) {
      setError(err instanceof Error ? err.message : '设置读取失败')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // 外观参数 → CSS 变量；主题 → data-theme
  useEffect(() => {
    if (!settings) return
    applyAppearance(settings.appearance)
    const theme = settings.appearance.theme as Theme
    if (THEMES.includes(theme)) applyTheme(theme)
  }, [settings])

  // 跟随系统暗色
  useEffect(() => {
    if (!settings?.appearance.followSystem) return
    const query = window.matchMedia('(prefers-color-scheme: dark)')
    const listener = () => applyTheme(query.matches ? 'dark' : 'light')
    listener()
    query.addEventListener('change', listener)
    return () => query.removeEventListener('change', listener)
  }, [settings?.appearance.followSystem])

  const patch = useCallback(async (payload: Record<string, unknown>) => {
    try {
      const data = await updateSettings(payload)
      setSettings(data)
      return data
    } catch (err) {
      setError(err instanceof Error ? err.message : '设置保存失败')
      return null
    }
  }, [])

  const setTheme = useCallback(
    async (theme: Theme) => {
      applyTheme(theme)
      try {
        // 同步写 localStorage：index.html 首屏内联脚本据此避免闪烁
        window.localStorage.setItem('aiw.theme', theme)
      } catch {
        // 隐私模式忽略
      }
      await patch({ appearance: { theme, followSystem: false } })
    },
    [patch],
  )

  const value = useMemo(
    () => ({ settings, loading, error, refresh, patch, setTheme }),
    [settings, loading, error, refresh, patch, setTheme],
  )

  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>
}

export function useSettings(): SettingsContextValue {
  const context = useContext(SettingsContext)
  if (!context) throw new Error('useSettings 必须在 SettingsProvider 内使用')
  return context
}