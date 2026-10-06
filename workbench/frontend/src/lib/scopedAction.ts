export type ActionScope = string | number
export interface ActionToken { scope: ActionScope; sequence: number; kind: string }

/** A view may start one mutation at a time; changing its identity invalidates callbacks. */
export class ScopedActions {
  private scope: ActionScope
  private sequence = 0
  private active: ActionToken | null = null
  constructor(scope: ActionScope) { this.scope = scope }
  setScope(scope: ActionScope): void {
    if (scope !== this.scope) { this.scope = scope; this.invalidate() }
  }
  begin(scope: ActionScope, kind = 'action'): ActionToken | null {
    if (scope !== this.scope || this.active) return null
    return this.active = { scope, kind, sequence: ++this.sequence }
  }
  accepts(token: ActionToken): boolean {
    return this.active === token && token.scope === this.scope && token.sequence === this.sequence
  }
  finish(token: ActionToken): boolean {
    if (!this.accepts(token)) return false
    this.active = null
    return true
  }
  invalidate(): void { this.sequence += 1; this.active = null }
}
