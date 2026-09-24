interface PlaceholderProps {
  /** 区块标题 */
  title: string
  /** 一句话说明该区块职责 */
  summary: string
  /** 后续里程碑规划的功能点 */
  items?: readonly string[]
  /** 当前项目 ID（项目内页面传入） */
  projectId?: string
}

/** M0 占位页：仅验证路由与布局，不承载任何业务逻辑。 */
export default function Placeholder({ title, summary, items, projectId }: PlaceholderProps) {
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">{title}</h1>
          <p className="page-header__desc">{summary}</p>
        </div>
      </header>

      <section className="notice">
        <p className="notice__label">待实装</p>
        <p className="notice__body prose">
          本页为 M0 骨架阶段的占位页，仅用于验证路由与布局壳，尚未接入业务逻辑与数据。
        </p>
        {items && items.length > 0 ? (
          <>
            <p className="notice__sub">规划内容</p>
            <ul className="notice__list">
              {items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </>
        ) : null}
      </section>

      {projectId ? (
        <p className="page-footnote">
          当前项目 ID：<code>{projectId}</code>
        </p>
      ) : null}
    </div>
  )
}
