import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from './App'
import { ToastProvider } from './state/useToast'
import { SettingsProvider } from './state/useSettings'

// 顺序敏感：先定义 token，再覆盖主题，最后加载基础样式与组件样式。
import './styles/tokens.css'
import './styles/theme.css'
import './styles/global.css'
import './styles/app.css'

const container = document.getElementById('root')
if (!container) throw new Error('找不到挂载节点 #root')

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <ToastProvider>
        <SettingsProvider>
          <App />
        </SettingsProvider>
      </ToastProvider>
    </BrowserRouter>
  </StrictMode>,
)