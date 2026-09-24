/**
 * 外观与护眼（M9）：把设置里的 appearance 参数落到 CSS 变量与文档属性。
 *
 * - 主题（亮/暗/纸感）走 data-theme；跟随系统暗色由 useTheme 处理；
 * - 护眼参数（色温 / 对比度 / 字号 / 行高 / 字距 / 页宽）落到 :root 内联变量；
 * - 减少动效：data-reduce-motion="on"。
 * （原「专注模式」已移除：分栏折叠由 AppShell / Editor 的 localStorage 状态负责。）
 */

import { useEffect } from 'react'
import type { Settings } from '../api/types'

export function applyAppearance(appearance: Settings['appearance']): void {
  const root = document.documentElement
  root.style.setProperty('--prose-font-size', `${appearance.fontSize}px`)
  root.style.setProperty('--prose-line-height', String(appearance.lineHeight))
  root.style.setProperty('--prose-letter-spacing', `${appearance.letterSpacing}px`)
  root.style.setProperty('--prose-max-width', `${appearance.pageWidth}px`)

  // 色温：暖色叠加（正值偏暖，负值偏冷）
  // ui-style-ok: 色温叠加层的 rgba 由运行时的用户设置计算，无法用静态 token 表达
  const warmth = Math.max(-50, Math.min(50, appearance.colorTemperature))
  if (warmth === 0) {
    root.style.setProperty('--warmth-overlay', 'transparent')
    root.style.removeProperty('--warmth-strength')
  } else {
    const color = warmth > 0 ? '255, 186, 116' : '160, 200, 255'
    // ui-style-ok: 色温叠加层按用户设置实时计算，运行时难以走静态 token
    root.style.setProperty('--warmth-overlay', `rgba(${color}, ${Math.abs(warmth) / 100})`)
  }

  // 对比度：通过 filter 微调（1.0 为不调整）
  const contrast = Math.max(0.8, Math.min(1.3, appearance.contrast))
  root.style.setProperty('--contrast-filter', contrast === 1 ? 'none' : `contrast(${contrast})`)

  root.setAttribute('data-reduce-motion', appearance.reduceMotion ? 'on' : 'off')
}

export function useAppearance(appearance: Settings['appearance'] | undefined): void {
  useEffect(() => {
    if (!appearance) return
    applyAppearance(appearance)
  }, [appearance])
}