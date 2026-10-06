/** Transport a task over stdin, then run the unchanged, pinned vendor CLI. */
import { pathToFileURL } from 'node:url'

const [entry, ...flags] = process.argv.slice(2)
if (!entry) throw new Error('Missing pinned dsh CLI entry')
const chunks = []
for await (const chunk of process.stdin) chunks.push(Buffer.from(chunk))
const task = Buffer.concat(chunks).toString('utf8')
process.argv = [process.execPath, entry, ...flags, task]
await import(pathToFileURL(entry).href)
