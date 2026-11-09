// Node loader: resolve the only three specifiers a Desktop plugin may import to local stubs.
import { fileURLToPath, pathToFileURL } from 'node:url'
import path from 'node:path'
const here = path.dirname(fileURLToPath(import.meta.url))
const MAP = {
  '@hermes/plugin-sdk': 'stub-sdk.mjs',
  'react': 'stub-react.mjs',
  'react/jsx-runtime': 'stub-jsx.mjs'
}
export async function resolve(specifier, context, next) {
  if (MAP[specifier]) return { url: pathToFileURL(path.join(here, MAP[specifier])).href, shortCircuit: true }
  return next(specifier, context)
}
