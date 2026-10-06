import type { ChapterDetail } from '../api/types'

export type EditorDraft = { projectId: number; path: string; text: string; base: ChapterDetail }
const drafts = new Map<string, EditorDraft>()
const lastFiles = new Map<number, string>()
const key = (projectId: number, path: string) => `aiw.editor.draft.${projectId}.${encodeURIComponent(path)}`
const lastKey = (projectId: number) => `aiw.editor.lastFile.${projectId}`
const storage = (): Storage | null => { try { return typeof window === 'undefined' ? null : window.localStorage } catch { return null } }

/** Persist only author edits, with the original disk version for conflict checks. */
export function storeEditorDraft(draft: EditorDraft): void {
  if (!Number.isSafeInteger(draft.projectId) || draft.projectId <= 0 || draft.base.rel_path !== draft.path) return
  drafts.set(key(draft.projectId, draft.path), draft)
  try { storage()?.setItem(key(draft.projectId, draft.path), JSON.stringify(draft)) } catch { /* The live cache still survives route changes. */ }
}

export function readEditorDraft(projectId: number, path: string): EditorDraft | null {
  const identity = key(projectId, path)
  if (drafts.has(identity)) return drafts.get(identity)!
  try {
    const raw = storage()?.getItem(identity)
    if (!raw) return null
    const draft = JSON.parse(raw)
    if (draft?.projectId !== projectId || draft.path !== path || typeof draft.text !== 'string'
      || draft.base?.rel_path !== path || typeof draft.base.body !== 'string' || typeof draft.base.content !== 'string'
      || typeof draft.base.hash !== 'string') return null
    drafts.set(identity, draft)
    return draft
  } catch { return null }
}

export function clearEditorDraft(projectId: number, path: string): void {
  drafts.delete(key(projectId, path))
  try { storage()?.removeItem(key(projectId, path)) } catch { /* Keep the current editor usable. */ }
}

/** A save acknowledgment must preserve edits made while that save was pending. */
export function acknowledgeEditorSave(projectId: number, path: string, sentText: string, saved: ChapterDetail): void {
  const draft = readEditorDraft(projectId, path)
  if (!draft || saved.rel_path !== path) return
  if (draft.text === sentText) clearEditorDraft(projectId, path)
  else storeEditorDraft({ ...draft, base: saved })
}

export function rememberEditorFile(projectId: number, path: string): void {
  lastFiles.set(projectId, path)
  try { storage()?.setItem(lastKey(projectId), path) } catch { /* Memory fallback. */ }
}

export function lastEditorFile(projectId: number): string {
  if (lastFiles.has(projectId)) return lastFiles.get(projectId)!
  try { return storage()?.getItem(lastKey(projectId)) || '' } catch { return '' }
}
