// just enough React for the plugin's two components: hooks are driven by the test through `hooks`.
export const hooks = { state: [], effects: [] }
export function useState(init) { const cell = { v: init }; const set = v => { cell.v = v }; hooks.state.push(cell); return [cell.v, set] }
export function useEffect(fn) { hooks.effects.push(fn) }
export default { useState, useEffect }
