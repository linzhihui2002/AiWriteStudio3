import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import AppShell from './components/AppShell'
import ProjectChatWorkspace from './components/ProjectChatWorkspace'
import Bookshelf from './pages/Bookshelf'
import NotFound from './pages/NotFound'

const Chat = lazy(() => import('./pages/Chat'))
const Editor = lazy(() => import('./pages/Editor'))
const Outline = lazy(() => import('./pages/Outline'))
const Bible = lazy(() => import('./pages/Bible'))
const Cards = lazy(() => import('./pages/Cards'))
const Knowledge = lazy(() => import('./pages/Knowledge'))
const Teardown = lazy(() => import('./pages/Teardown'))
const Review = lazy(() => import('./pages/Review'))
const Workflows = lazy(() => import('./pages/Workflows'))
const Inbox = lazy(() => import('./pages/Inbox'))
const Dashboard = lazy(() => import('./pages/Dashboard'))
const ImageStudio = lazy(() => import('./pages/ImageStudio'))
const Settings = lazy(() => import('./pages/Settings'))
const BookSettings = lazy(() => import('./pages/BookSettings'))

export default function App() {
  return <Suspense fallback={<div className="workspace-route-loading" role="status">正在打开工作区…</div>}>
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<Bookshelf />} />
        <Route path="/workflows" element={<Workflows />} />
        <Route path="/inbox" element={<Inbox />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/images" element={<ImageStudio />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/project/:id" element={<ProjectChatWorkspace />}>
          <Route index element={<Navigate to="chat" replace />} />
          <Route path="chat" element={<Chat />} />
          <Route path="editor" element={<Editor />} />
          <Route path="outline" element={<Outline />} />
          <Route path="bible" element={<Bible />} />
          <Route path="cards" element={<Cards />} />
          <Route path="knowledge" element={<Knowledge />} />
          <Route path="teardown" element={<Teardown />} />
          <Route path="review" element={<Review />} />
          <Route path="settings" element={<BookSettings />} />
        </Route>
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  </Suspense>
}
