import { Link, useParams } from 'react-router-dom'
import { useProjectChat } from '../state/projectChat'

export default function Chat() {
  const { id } = useParams()
  const { setChatHost } = useProjectChat()
  return <div className="page page--wide page--chat">
    <header className="page-header page-header--compact">
      <div><h1 className="page-header__title">本书对话</h1><p className="page-header__desc">讨论、规划与创作，和本书材料一起推进。</p></div>
      <Link className="btn btn--sm" to={`/project/${id}/editor`}>打开正文编辑器</Link>
    </header>
    <div className="project-chat-page-host" ref={setChatHost} />
  </div>
}
