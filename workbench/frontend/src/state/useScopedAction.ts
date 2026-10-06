import { useCallback, useEffect, useRef, useState } from 'react'
import { ScopedActions, type ActionScope, type ActionToken } from '../lib/scopedAction'

/** Synchronous submit lock plus book/item identity checks for every async side effect. */
export function useScopedAction(scope: ActionScope) {
  const actions = useRef(new ScopedActions(scope))
  actions.current.setScope(scope)
  const [state, setState] = useState<{ scope: ActionScope; kind: string }>({ scope, kind: '' })
  useEffect(() => () => actions.current.invalidate(), [])
  const begin = useCallback((kind = 'action') => {
    const token = actions.current.begin(scope, kind)
    if (token) setState({ scope, kind })
    return token
  }, [scope])
  const accept = useCallback((token: ActionToken) => actions.current.accepts(token), [])
  const finish = useCallback((token: ActionToken) => {
    if (actions.current.finish(token)) setState({ scope: token.scope, kind: '' })
  }, [])
  return { pending: state.scope === scope ? state.kind : '', begin, accept, finish }
}
