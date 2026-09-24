/**
 * 面板宽度（px）：localStorage 记忆 + 上下限钳制。
 *
 * 三处复用（主导航 / 文档树 / 对话面板）：折叠状态与宽度都是纯前端 UI 偏好，
 * 不进后端设置；刷新后保持上一次的拖拽结果。
 */

import { useCallback, useEffect, useState } from 'react'

export function clampWidth(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min
  return Math.min(max, Math.max(min, Math.round(value)))
}

export function usePanelWidth(
  key: string,
  initial: number,
  min: number,
  max: number,
): [number, (next: number) => void] {
  const [width, setWidthState] = useState(() => {
    try {
      const stored = Number(window.localStorage.getItem(key))
      return clampWidth(stored || initial, min, max)
    } catch {
      return clampWidth(initial, min, max)
    }
  })

  useEffect(() => {
    try {
      window.localStorage.setItem(key, String(width))
    } catch {
      // 隐私模式等场景忽略写入失败
    }
  }, [key, width])

  const setWidth = useCallback(
    (next: number) => setWidthState(clampWidth(next, min, max)),
    [min, max],
  )

  return [width, setWidth]
}