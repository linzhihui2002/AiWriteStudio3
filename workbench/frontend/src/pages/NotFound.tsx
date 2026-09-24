import { Link } from 'react-router-dom'

/** 未匹配路由。 */
export default function NotFound() {
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">未找到页面</h1>
          <p className="page-header__desc">该地址不存在或已被移动。</p>
        </div>
      </header>
      <Link className="btn" to="/">
        返回书架
      </Link>
    </div>
  )
}
