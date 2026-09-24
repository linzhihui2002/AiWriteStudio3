import { Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell'
import Bible from './pages/Bible'
import Bookshelf from './pages/Bookshelf'
import Cards from './pages/Cards'
import Dashboard from './pages/Dashboard'
import Editor from './pages/Editor'
import ImageStudio from './pages/ImageStudio'
import Inbox from './pages/Inbox'
import NotFound from './pages/NotFound'
import Outline from './pages/Outline'
import Review from './pages/Review'
import Settings from './pages/Settings'
import Teardown from './pages/Teardown'
import Workflows from './pages/Workflows'

/** 路由表：全局区（书架/工作流/收件箱/仪表盘/生图工坊/设置）+ 项目区（编辑器/大纲/Bible/卡片/审稿）。 */
export default function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<Bookshelf />} />
        <Route path="/workflows" element={<Workflows />} />
        <Route path="/inbox" element={<Inbox />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/images" element={<ImageStudio />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/project/:id/editor" element={<Editor />} />
        <Route path="/project/:id/outline" element={<Outline />} />
        <Route path="/project/:id/bible" element={<Bible />} />
        <Route path="/project/:id/cards" element={<Cards />} />
        <Route path="/project/:id/teardown" element={<Teardown />} />
        <Route path="/project/:id/review" element={<Review />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}