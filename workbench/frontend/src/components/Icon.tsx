import type { CSSProperties } from 'react'

export type IconName = 'book' | 'chat' | 'file' | 'outline' | 'cards' | 'search' | 'review' | 'inbox' | 'workflow' | 'chart' | 'image' | 'settings' | 'menu' | 'close' | 'chevron'
const paths: Record<IconName, string> = {
  book: 'M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-2H4z M20 4h-4a3 3 0 0 0-3 3v14a4 4 0 0 1 4-2h3z',
  chat: 'M20 15a3 3 0 0 1-3 3H9l-5 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z M8 8h8 M8 12h5',
  file: 'M14 3H5v18h14V8z M14 3v5h5 M8 12h8 M8 16h6',
  outline: 'M5 5h14 M5 12h14 M5 19h14 M3 5h.01 M3 12h.01 M3 19h.01',
  cards: 'M4 7h13v14H4z M8 3h13v14 M8 11h5 M8 15h3',
  search: 'M10 17a7 7 0 1 0 0-14 7 7 0 0 0 0 14z M15 15l6 6',
  review: 'M5 3h14v18H5z M9 7h6 M8 12l2 2 5-5 M9 18h6',
  inbox: 'M5 4h14l3 11v5H2v-5z M2 15h6l2 3h4l2-3h6',
  workflow: 'M4 3h6v6H4z M14 15h6v6h-6z M7 9v9h7 M17 15V6h-7',
  chart: 'M4 3v18h17 M8 17v-5 M13 17V8 M18 17V4',
  image: 'M3 4h18v16H3z M3 16l5-5 4 4 3-3 6 6 M16 8h.01',
  settings: 'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8z M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',
  menu: 'M4 6h16 M4 12h16 M4 18h16', close: 'M6 6l12 12 M18 6 6 18', chevron: 'M9 5l7 7-7 7',
}
export default function Icon({ name, size = 18, style }: { name: IconName; size?: number; style?: CSSProperties }) {
  return <svg className="ui-icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" style={style}><path d={paths[name]} /></svg>
}
