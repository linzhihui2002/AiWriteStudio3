export type ResourceScope = string | number
export interface ResourceToken { sequence: number; scope: ResourceScope }
export interface ResourceSnapshot { loading: boolean; error: string; loaded: boolean; scope: ResourceScope }

export class ResourceRequests {
  private sequence = 0
  begin(scope: ResourceScope): ResourceToken { return { sequence: ++this.sequence, scope } }
  accepts(token: ResourceToken, scope: ResourceScope): boolean { return token.sequence === this.sequence && token.scope === scope }
  invalidate(): void { this.sequence += 1 }
}

export function resourceSnapshotForScope(snapshot: ResourceSnapshot, scope: ResourceScope): ResourceSnapshot {
  return snapshot.scope === scope ? snapshot : { loading: true, error: '', loaded: false, scope }
}

export function resourcePresentation({ loading, error = '', hasData, loaded = hasData }: {
  loading: boolean; error?: string; hasData: boolean; loaded?: boolean
}): 'loading' | 'error' | 'content' | 'refreshing' | 'stale' {
  if (!hasData && !loaded && (loading || !error)) return 'loading'
  if (!hasData && !loaded && error) return 'error'
  if (error) return 'stale'
  return loading ? 'refreshing' : 'content'
}
